#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import datetime


def parse_time_arg(s: str) -> float:
    """
    Parse a time argument as either:
      - Unix timestamp (integer or float string), or
      - ISO-8601 like '2025-12-08T10:23:00' (treated as local time).
    Returns Unix timestamp (float seconds).
    """
    # Try numeric (Unix timestamp)
    try:
        return float(s)
    except ValueError:
        pass

    # Try ISO-like datetime
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception as e:
        raise argparse.ArgumentTypeError(
            f"Cannot parse time '{s}' as unix timestamp or ISO datetime: {e}"
        )


def seconds_to_hms(sec: float) -> str:
    sec = int(round(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_log(path: str):
    """Yield parsed JSON objects from the log file."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Skip malformed lines
                continue
            yield rec


def analyze(log_path: str, t_start: float, t_end: float):
    """
    Analyze the log file between [t_start, t_end] (inclusive).

    Returns:
        stats: dict[hash] = {
            'title': str or None,
            'activations': int,
            'focus_seconds': float,
        }
        hash_to_title: dict[hash] = title
        totals: dict with 'idle', 'locked', 'stopped' (seconds)
    """
    hash_to_title = {}

    # Stats per window hash
    stats = {}

    def ensure_entry(h):
        if h not in stats:
            stats[h] = {
                "title": hash_to_title.get(h),
                "activations": 0,
                "focus_seconds": 0.0,
            }
        return stats[h]

    # Global state over time
    prev_ts = None
    prev_windows = {}          # hash -> focused(bool) for previous interval
    extension_running = False  # whether the logger is active
    idle = False
    locked = False

    total_idle = 0.0
    total_locked = 0.0
    total_stopped = 0.0

    # Iterate records in chronological order (log is append-only)
    for rec in load_log(log_path):
        ts = rec.get("ts")
        if ts is None:
            continue
        ts = float(ts)

        # 1) Attribute the interval from prev_ts to ts to the previous state
        if prev_ts is not None:
            seg_start = prev_ts
            seg_end = ts

            # overlap with analysis window
            if seg_end < t_start or seg_start > t_end:
                interval = 0.0
            else:
                overlap_start = max(seg_start, t_start)
                overlap_end = min(seg_end, t_end)
                interval = max(0.0, overlap_end - overlap_start)

            if interval > 0:
                if not extension_running:
                    total_stopped += interval
                elif locked:
                    total_locked += interval
                elif idle:
                    total_idle += interval
                else:
                    # Active time: attribute to focused windows in prev_windows
                    for h, focused in prev_windows.items():
                        if not focused:
                            continue
                        entry = ensure_entry(h)
                        entry["focus_seconds"] += interval

        # 2) Update state based on THIS record

        if "restart" in rec:
            # Extension has just (re)started. Reset state.
            extension_running = True
            idle = False
            locked = False
            prev_windows = {}

        elif "stopped" in rec:
            # Extension is stopping. No windows active until next restart.
            extension_running = False
            prev_windows = {}

        elif "windows" in rec:
            # A normal snapshot: windows/idle/locked state
            extension_running = True
            # idle/locked are booleans indicating current state
            if "idle" in rec:
                idle = bool(rec["idle"])
            if "locked" in rec:
                locked = bool(rec["locked"])

            state = {}
            windows = rec.get("windows") or []
            for w in windows:
                h = w.get("hash")
                if not h:
                    continue
                focused = bool(w.get("focused", False))
                state[h] = focused

                # Learn title if present
                title = w.get("title")
                if title and h not in hash_to_title:
                    hash_to_title[h] = title
                    # Also update existing stats entry if created earlier
                    if h in stats:
                        stats[h]["title"] = title

            # Count activations at this instant (if inside time window)
            if t_start <= ts <= t_end:
                prev_focused = {h for h, f in prev_windows.items() if f}
                curr_focused = {h for h, f in state.items() if f}
                for h in curr_focused - prev_focused:
                    entry = ensure_entry(h)
                    entry["activations"] += 1

            prev_windows = state

        # 3) Move forward in time
        prev_ts = ts

    totals = {
        "idle": total_idle,
        "locked": total_locked,
        "stopped": total_stopped,
    }

    return stats, hash_to_title, totals


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze window-logger log to compute activations and focus time "
            "per window, excluding idle/locked/stopped time."
        )
    )
    parser.add_argument(
        "--log",
        default=os.path.expanduser("~/.local/share/window-logger.log"),
        help="Path to log file (default: ~/.local/share/window-logger.log)",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--hours",
        type=float,
        help="Analyze the past N hours (relative to now).",
    )
    group.add_argument(
        "--range",
        nargs=2,
        metavar=("START", "END"),
        help=(
            "Analyze between START and END times. "
            "Each can be a unix timestamp or ISO datetime (e.g. 2025-12-08T10:23:00)."
        ),
    )

    args = parser.parse_args()

    now = time.time()

    if args.hours is not None:
        t_end = now
        t_start = now - args.hours * 3600.0
    elif args.range is not None:
        t_start = parse_time_arg(args.range[0])
        t_end = parse_time_arg(args.range[1])
    else:
        # Default: use full range of the log
        t_start = float("-inf")
        t_end = float("inf")

    if not os.path.exists(args.log):
        print(f"Log file not found: {args.log}", file=sys.stderr)
        sys.exit(1)

    stats, hash_to_title, totals = analyze(args.log, t_start, t_end)

    # If no stats and no time, bail early
    total_time = totals["idle"] + totals["locked"] + totals["stopped"]
    total_focus = sum(e["focus_seconds"] for e in stats.values())
    if not stats and total_time == 0 and total_focus == 0:
        print("No data in the specified time range.")
        return

    # Sort by total focus time, descending
    sorted_items = sorted(
        stats.items(),
        key=lambda kv: kv[1]["focus_seconds"],
        reverse=True,
    )

    print(f"Time window: {t_start:.0f} – {t_end:.0f} (unix timestamps)")
    print()
    print(f"{'Hash':<20}  {'Title':<40}  {'Activations':>11}  {'Focus Time':>10}")
    print("-" * 90)

    for h, entry in sorted_items:
        title = entry["title"] or "<unknown>"
        activations = entry["activations"]
        focus_sec = entry["focus_seconds"]
        focus_hms = seconds_to_hms(focus_sec)

        # Truncate title for display
        if len(title) > 40:
            title_disp = title[:37] + "..."
        else:
            title_disp = title

        print(f"{h:<20}  {title_disp:<40}  {activations:>11d}  {focus_hms:>10}")

    print()
    print("Totals (excluding any overlapping outside the time window):")
    print(f"  Idle time   : {seconds_to_hms(totals['idle'])}")
    print(f"  Locked time : {seconds_to_hms(totals['locked'])}")
    print(f"  Stopped time: {seconds_to_hms(totals['stopped'])}")


if __name__ == "__main__":
    main()
