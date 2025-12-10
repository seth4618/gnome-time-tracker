#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, Iterable, Optional


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


def load_cutoffs(path: Optional[str]) -> Dict[str, float]:
    if not path:
        return {}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cutoffs: Dict[str, float] = {}
    for k, v in (data or {}).items():
        try:
            cutoffs[k] = float(v)
        except (TypeError, ValueError):
            continue
    return cutoffs


def _attribute_focus(
    focused_hashes: Iterable[str],
    interval: float,
    ensure_entry,
):
    for h in focused_hashes:
        entry = ensure_entry(h)
        entry["focus_seconds"] += interval
        # focus attribution never records idle time


def analyze(
    log_path: str,
    t_start: float,
    t_end: float,
    cutoffs: Optional[Dict[str, float]] = None,
):
    """
    Analyze the log file between [t_start, t_end] (inclusive).

    Returns:
        stats: dict[hash] = {
            'title': str or None,
            'cmd': str or None,
            'activations': int,
            'focus_seconds': float,
        }
        hash_to_title: dict[hash] = title
        hash_to_cmd: dict[hash] = cmd
        totals: dict with 'idle', 'locked', 'stopped' (seconds)
    """
    hash_to_title = {}
    hash_to_cmd = {}

    # Stats per window hash
    stats = {}

    def ensure_entry(h):
        if h not in stats:
            stats[h] = {
                "title": hash_to_title.get(h),
                "cmd": hash_to_cmd.get(h),
                "activations": 0,
                "focus_seconds": 0.0,
                "idle_seconds": 0.0,
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

    cutoffs = cutoffs or {}

    idle_start_ts: Optional[float] = None
    idle_cmd: Optional[str] = None
    idle_focused_hashes: Iterable[str] = []
    idle_overlap = 0.0
    idle_duration = 0.0

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

            if not extension_running:
                if interval > 0:
                    total_stopped += interval
            elif locked:
                if interval > 0:
                    total_locked += interval
            elif idle:
                if idle_start_ts is None:
                    idle_start_ts = seg_start
                    idle_cmd = None
                    idle_focused_hashes = [h for h, f in prev_windows.items() if f]
                    idle_overlap = 0.0
                    idle_duration = 0.0
                idle_duration += seg_end - seg_start
                if interval > 0:
                    idle_overlap += interval
            else:
                if interval > 0:
                    _attribute_focus(
                        [h for h, focused in prev_windows.items() if focused],
                        interval,
                        ensure_entry,
                    )

        # Capture previous state for transition detection
        prev_idle_state = idle
        prev_windows_snapshot = prev_windows

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
            idle = bool(rec.get("idle", False))
            locked = bool(rec.get("locked", False))

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

                # Learn cmd if present
                cmd = w.get("cmd")
                if cmd and h not in hash_to_cmd:
                    hash_to_cmd[h] = cmd
                    if h in stats:
                        stats[h]["cmd"] = cmd

            # Count activations at this instant (if inside time window)
            if t_start <= ts <= t_end:
                prev_focused = {h for h, f in prev_windows.items() if f}
                curr_focused = {h for h, f in state.items() if f}
                for h in curr_focused - prev_focused:
                    entry = ensure_entry(h)
                    entry["activations"] += 1

            prev_windows = state

        # Detect idle transitions after state update
        # Transitions
        if not prev_idle_state and idle and extension_running and not locked:
            idle_start_ts = ts
            focused_cmds = [
                hash_to_cmd.get(h)
                for h, focused in prev_windows_snapshot.items()
                if focused and hash_to_cmd.get(h)
            ]
            idle_cmd = focused_cmds[0] if focused_cmds else None
            idle_focused_hashes = [
                h for h, focused in prev_windows_snapshot.items() if focused
            ]
            idle_overlap = 0.0
            idle_duration = 0.0

        if prev_idle_state and (not idle or not extension_running or locked):
            if idle_start_ts is not None:
                cutoff = cutoffs.get(idle_cmd, 0.0)
                treat_as_active = (
                    idle_cmd is not None
                    and idle_duration < cutoff
                    and extension_running
                    and not locked
                    and not idle
                )

                if treat_as_active:
                    _attribute_focus(idle_focused_hashes, idle_overlap, ensure_entry)
                else:
                    total_idle += idle_overlap
                    post_idle_focused = [h for h, focused in prev_windows.items() if focused]
                    shared_focus = set(idle_focused_hashes) & set(post_idle_focused)
                    for h in shared_focus:
                        entry = ensure_entry(h)
                        entry["idle_seconds"] += idle_overlap

            idle_start_ts = None
            idle_cmd = None
            idle_focused_hashes = []
            idle_overlap = 0.0
            idle_duration = 0.0


        # 3) Move forward in time
        prev_ts = ts

    totals = {
        "idle": total_idle,
        "locked": total_locked,
        "stopped": total_stopped,
    }

    return stats, hash_to_title, hash_to_cmd, totals


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze window-logger log to compute activations and focus time "
            "per window or per cmdline, excluding idle/locked/stopped time."
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
            "Each can be a unix timestamp or ISO datetime "
            "(e.g. 2025-12-08T10:23:00)."
        ),
    )

    parser.add_argument(
        "-w",
        "--window",
        action="store_true",
        help="Report per window (title/hash) instead of aggregating by cmdline.",
    )

    parser.add_argument(
        "-c",
        "--cutoffs",
        help=(
            "Optional path to a JSON file mapping command paths to minimum idle "
            "durations (seconds) that should be treated as idle."
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

    cutoffs = load_cutoffs(args.cutoffs)

    stats, hash_to_title, hash_to_cmd, totals = analyze(
        args.log, t_start, t_end, cutoffs=cutoffs
    )

    total_time = totals["idle"] + totals["locked"] + totals["stopped"]
    total_focus = sum(e["focus_seconds"] for e in stats.values())
    if not stats and total_time == 0 and total_focus == 0:
        print("No data in the specified time range.")
        return

    print(f"Time window: {t_start:.0f} – {t_end:.0f} (unix timestamps)")
    print()

    if args.window:
        # --- Per-window report (title/hash) ---
        sorted_items = sorted(
            stats.items(),
            key=lambda kv: kv[1]["focus_seconds"],
            reverse=True,
        )

        print(
            f"{'Hash':<20}  {'Title':<40}  {'Cmd':<40}  "
            f"{'Activations':>11}  {'Focus Time':>10}  {'Idle Time':>9}"
        )
        print("-" * 132)

        for h, entry in sorted_items:
            title = entry["title"] or "<unknown>"
            cmd = entry["cmd"] or "<unknown>"
            activations = entry["activations"]
            focus_sec = entry["focus_seconds"]
            idle_sec = entry["idle_seconds"]
            focus_hms = seconds_to_hms(focus_sec)
            idle_hms = seconds_to_hms(idle_sec)

            # Truncate fields for display
            if len(title) > 40:
                title_disp = title[:37] + "..."
            else:
                title_disp = title

            if len(cmd) > 40:
                cmd_disp = cmd[:37] + "..."
            else:
                cmd_disp = cmd

            print(
                f"{h:<20}  {title_disp:<40}  {cmd_disp:<40}  "
                f"{activations:>11d}  {focus_hms:>10}  {idle_hms:>9}"
            )

    else:
        # --- Aggregate by cmdline (default) ---
        agg = {}  # cmd -> { 'cmd', 'activations', 'focus_seconds', 'idle_seconds' }

        for h, entry in stats.items():
            cmd = entry["cmd"] or "<unknown>"
            rec = agg.setdefault(
                cmd,
                {
                    "cmd": cmd,
                    "activations": 0,
                    "focus_seconds": 0.0,
                    "idle_seconds": 0.0,
                },
            )
            rec["activations"] += entry["activations"]
            rec["focus_seconds"] += entry["focus_seconds"]
            rec["idle_seconds"] += entry["idle_seconds"]

        sorted_cmds = sorted(
            agg.values(),
            key=lambda e: e["focus_seconds"],
            reverse=True,
        )

        print(
            f"{'Cmd':<60}  {'Activations':>11}  "
            f"{'Focus Time':>10}  {'Idle Time':>9}"
        )
        print("-" * 103)

        for rec in sorted_cmds:
            cmd = rec["cmd"]
            activations = rec["activations"]
            focus_sec = rec["focus_seconds"]
            idle_sec = rec["idle_seconds"]
            focus_hms = seconds_to_hms(focus_sec)
            idle_hms = seconds_to_hms(idle_sec)

            if len(cmd) > 60:
                cmd_disp = cmd[:57] + "..."
            else:
                cmd_disp = cmd

            print(
                f"{cmd_disp:<60}  {activations:>11d}  "
                f"{focus_hms:>10}  {idle_hms:>9}"
            )

    print()
    print("Totals (excluding any overlapping outside the time window):")
    print(f"  Idle time   : {seconds_to_hms(totals['idle'])}")
    print(f"  Locked time : {seconds_to_hms(totals['locked'])}")
    print(f"  Stopped time: {seconds_to_hms(totals['stopped'])}")


if __name__ == "__main__":
    main()
