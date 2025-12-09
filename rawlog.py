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
      - ISO-8601 like '2025-12-08T10:23:00' (treated as local time)
    Returns Unix timestamp (float seconds).
    """
    # Try numeric (Unix timestamp)
    try:
        return float(s)
    except ValueError:
        pass

    # Try ISO-like
    try:
        dt = datetime.fromisoformat(s)
        # Interpret naive datetimes as local time
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
    """
    # Map hash -> title, built from any record that includes a title
    hash_to_title = {}

    # We walk the log in chronological order (file is append-only).
    snapshots = []  # list of (ts, {hash: focused_bool})

    for rec in load_log(log_path):
        if "restart" in rec:
            # Restart marker, ignore for timing but keep for hash->title map continuity
            continue

        ts = rec.get("ts")
        if ts is None:
            continue

        windows = rec.get("windows")
        if not isinstance(windows, list):
            continue

        state = {}
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

        # Record snapshot
        snapshots.append((float(ts), state))

    # No snapshots means nothing to report
    if not snapshots:
        return {}, hash_to_title

    # Stats structure
    stats = {}  # hash -> dict

    def ensure_entry(h):
        if h not in stats:
            stats[h] = {
                "title": hash_to_title.get(h),
                "activations": 0,
                "focus_seconds": 0.0,
            }
        return stats[h]

    # We need to track previous focused set across snapshots
    prev_ts = None
    prev_state = {}  # hash -> focused (bool)

    # Process snapshots in time order (already in order since appended)
    for ts, state in snapshots:
        # Compute interval contribution from previous snapshot
        if prev_ts is not None:
            seg_start = prev_ts
            seg_end = ts
            if seg_end < t_start or seg_start > t_end:
                # Interval completely outside window
                interval = 0.0
            else:
                # Overlap with [t_start, t_end]
                overlap_start = max(seg_start, t_start)
                overlap_end = min(seg_end, t_end)
                interval = max(0.0, overlap_end - overlap_start)

            if interval > 0:
                # Attribute interval to any window that was focused in prev_state
                for h, focused in prev_state.items():
                    if not focused:
                        continue
                    entry = ensure_entry(h)
                    entry["focus_seconds"] += interval

        # Detect activations at this snapshot
        # A window is "activated" when it goes from not focused to focused.
        prev_focused = {h for h, f in prev_state.items() if f}
        curr_focused = {h for h, f in state.items() if f}

        # If ts within our window, count activations here
        if t_start <= ts <= t_end:
            for h in curr_focused - prev_focused:
                entry = ensure_entry(h)
                entry["activations"] += 1

        prev_ts = ts
        prev_state = state

    return stats, hash_to_title


def main():
    parser = argparse.ArgumentParser(
        description="Analyze GNOME window-logger log to compute activations and focus time per window."
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

    stats, hash_to_title = analyze(args.log, t_start, t_end)

    if not stats:
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


if __name__ == "__main__":
    main()
