#!/usr/bin/env python3
"""
Summarize and visualize idle durations per command.

The script reads the window-logger log, extracts idle periods, and plots
box plots of idle durations grouped by the command that was focused when the
system became idle. By default, only idle periods where the same command regains
focus after idling are included; use --include-switches to keep idle periods
that end on a different command.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
import statistics
from typing import Dict, List, Optional

import matplotlib


DEFAULT_LOG_PATH = os.path.expanduser("~/.local/share/window-logger.log")
DEFAULT_CUTOFF_PATH = os.path.expanduser("~/.local/share/appmap.json")


def load_cutoffs(path: str) -> Dict[str, float]:
    """Load a mapping of command -> minimum idle duration from JSON.

    If the file cannot be read or parsed, an error is raised so the caller can
    surface it to the user.
    """

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cutoffs: Dict[str, float] = {}
    for cmd, seconds in data.items():
        try:
            cutoffs[str(cmd)] = float(seconds)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid cutoff value for command '{cmd}': {seconds}")

    return cutoffs


def parse_time_arg(s: str) -> float:
    """Parse a time argument as unix timestamp or ISO-8601 string."""
    try:
        return float(s)
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception as e:  # noqa: BLE001
        raise argparse.ArgumentTypeError(
            f"Cannot parse time '{s}' as unix timestamp or ISO datetime: {e}"
        )


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
                continue
            yield rec


def extract_idle_durations(
    log_path: str,
    t_start: float,
    t_end: float,
    include_switches: bool = False,
    cutoffs: Optional[Dict[str, float]] = None,
) -> Dict[str, List[float]]:
    """
    Return a mapping of command -> list of idle durations (seconds).

    Idle periods are measured while the logger is running and the system is
    idle (not locked). By default, an idle period is counted only when the
    command focused before idling matches the command focused after resuming.
    If ``include_switches`` is True, idle periods are counted even when the
    focus changes during the idle period.
    If ``cutoffs`` is provided, it should map command paths to minimum idle
    durations (seconds) that should be treated as idle time. Idle periods that
    are shorter than the configured cutoff for their command are ignored so the
    brief pause counts as active time.
    """

    hash_to_cmd: Dict[str, str] = {}
    prev_windows: Dict[str, Dict[str, object]] = {}

    extension_running = False
    idle = False
    locked = False

    idle_start_ts: Optional[float] = None
    idle_cmd: Optional[str] = None

    durations_by_cmd: Dict[str, List[float]] = {}

    for rec in load_log(log_path):
        ts = rec.get("ts")
        if ts is None:
            continue
        ts = float(ts)

        new_extension_running = extension_running
        new_idle = idle
        new_locked = locked
        windows_new: Dict[str, Dict[str, object]] = prev_windows

        if "restart" in rec:
            new_extension_running = True
            new_idle = False
            new_locked = False
            windows_new = {}
        elif "stopped" in rec:
            new_extension_running = False
            windows_new = {}
        elif "windows" in rec:
            new_extension_running = True
            new_idle = bool(rec.get("idle", False))
            new_locked = bool(rec.get("locked", False))
            windows_new = {}

            for w in rec.get("windows") or []:
                h = w.get("hash")
                if not h:
                    continue
                focused = bool(w.get("focused", False))
                cmd = w.get("cmd")
                if cmd and h not in hash_to_cmd:
                    hash_to_cmd[h] = cmd
                windows_new[h] = {
                    "focused": focused,
                    "cmd": cmd or hash_to_cmd.get(h),
                }

        # If we are currently in an idle period, check if it ends at this record.
        if idle_start_ts is not None:
            idle_ends = (not new_extension_running) or new_locked or (not new_idle)
            if idle_ends:
                idle_end_ts = ts

                if idle_end_ts >= t_start and idle_start_ts <= t_end:
                    overlap_start = max(idle_start_ts, t_start)
                    overlap_end = min(idle_end_ts, t_end)
                    duration = overlap_end - overlap_start

                    if duration > 0:
                        end_cmd: Optional[str] = None
                        if new_extension_running and not new_idle and not new_locked:
                            focused_cmds = [
                                info.get("cmd")
                                for info in windows_new.values()
                                if info.get("focused") and info.get("cmd")
                            ]
                            end_cmd = focused_cmds[0] if focused_cmds else None

                        include = idle_cmd is not None
                        if include:
                            if not include_switches and idle_cmd != end_cmd:
                                include = False

                        if include:
                            cutoff = (cutoffs or {}).get(idle_cmd, 0)
                            if duration >= cutoff:
                                durations_by_cmd.setdefault(idle_cmd, []).append(
                                    duration
                                )

                idle_start_ts = None
                idle_cmd = None

        # Detect the start of a new idle period.
        starts_idle = (
            idle_start_ts is None
            and not idle
            and new_idle
            and new_extension_running
            and not new_locked
        )
        if starts_idle:
            focused_cmds = [
                info.get("cmd") or hash_to_cmd.get(h)
                for h, info in prev_windows.items()
                if info.get("focused")
            ]
            start_cmd = focused_cmds[0] if focused_cmds else None
            if start_cmd:
                idle_start_ts = ts
                idle_cmd = start_cmd

        extension_running = new_extension_running
        idle = new_idle
        locked = new_locked
        prev_windows = windows_new

    return durations_by_cmd


def plot_boxplot(durations_by_cmd: Dict[str, List[float]], output: Optional[str] = None):
    if not durations_by_cmd:
        print("No idle durations found for the specified time window.")
        return

    if output:
        matplotlib.use("Agg")
    elif not os.environ.get("DISPLAY"):
        # Fall back to a non-interactive backend when no display is available so
        # the script can still render to a file if the caller supplies --output.
        matplotlib.use("Agg")

    import matplotlib.pyplot as plt  # noqa: E402

    cmds = sorted(durations_by_cmd.keys())
    data = [durations_by_cmd[c] for c in cmds]

    plt.figure(figsize=(10, 6))
    plt.boxplot(data, tick_labels=cmds, showmeans=True)
    plt.ylabel("Idle duration (seconds)")
    plt.xticks(rotation=45, ha="right")
    plt.title("Idle durations per command")
    plt.tight_layout()

    if output:
        plt.savefig(output)
        print(f"Saved box plot to {output}")
    else:
        backend = matplotlib.get_backend().lower()
        if backend.endswith("agg"):
            print("No interactive backend available; use --output to save the plot.")
        else:
            plt.show()


def print_summary_table(durations_by_cmd: Dict[str, List[float]]) -> bool:
    """Print a table summarizing idle durations per command.

    Returns True when there is data to show; False otherwise.
    """

    if not durations_by_cmd:
        print("No idle durations found for the specified time window.")
        return False

    headers = ["Command", "Count", "Mean (s)", "Median (s)", "25% (s)", "75% (s)"]
    rows = []

    def fmt_num(value: float) -> str:
        """Format a numeric value with one decimal place."""

        return f"{value:7.1f}"

    for cmd in sorted(durations_by_cmd.keys()):
        durations = sorted(durations_by_cmd[cmd])
        count = len(durations)
        mean_val = statistics.mean(durations)
        median_val = statistics.median(durations)

        if count >= 2:
            q1, _, q3 = statistics.quantiles(
                durations, n=4, method="inclusive"
            )
        else:
            # With a single sample, treat both quartiles as the lone value to
            # avoid statistics.StatisticsError while keeping the table useful.
            q1 = q3 = durations[0]
        rows.append(
            [
                cmd,
                str(count),
                fmt_num(mean_val),
                fmt_num(median_val),
                fmt_num(q1),
                fmt_num(q3),
            ]
        )

    col_widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(cell))

    def format_row(row_vals):
        formatted_cells = []
        for idx, val in enumerate(row_vals):
            if idx == 0:
                formatted_cells.append(val.ljust(col_widths[idx]))
            else:
                formatted_cells.append(val.rjust(col_widths[idx]))
        return "  ".join(formatted_cells)

    print(format_row(headers))
    print(format_row(["-" * w for w in col_widths]))
    for row in rows:
        print(format_row(row))

    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Summarize idle durations per command from the window-logger log and "
            "plot box plots"
        ),
    )
    parser.add_argument(
        "--log",
        default=DEFAULT_LOG_PATH,
        help=f"Path to log file (default: {DEFAULT_LOG_PATH})",
    )
    parser.add_argument(
        "-c",
        "--cutoff-file",
        help=(
            "Path to a JSON file mapping command paths to minimum idle durations "
            "(seconds). Idle periods shorter than the cutoff for the focused "
            "command are treated as active time."
        ),
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
            "Analyze between START and END times. Each can be a unix timestamp or "
            "ISO datetime (e.g. 2025-12-08T10:23:00)."
        ),
    )

    parser.add_argument(
        "--include-switches",
        action="store_true",
        help=(
            "Include idle periods where focus resumes on a different command "
            "than the one that was active before idling."
        ),
    )
    parser.add_argument(
        "--output",
        help="Save the box plot to the given file path instead of displaying it.",
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
        t_start = float("-inf")
        t_end = float("inf")

    if not os.path.exists(args.log):
        print(f"Log file not found: {args.log}", file=sys.stderr)
        sys.exit(1)

    cutoffs: Optional[Dict[str, float]] = None
    if args.cutoff_file:
        try:
            cutoffs = load_cutoffs(args.cutoff_file)
        except FileNotFoundError:
            print(f"Cutoff file not found: {args.cutoff_file}", file=sys.stderr)
            sys.exit(1)
        except (OSError, ValueError) as exc:
            print(f"Failed to read cutoff file: {exc}", file=sys.stderr)
            sys.exit(1)

    durations_by_cmd = extract_idle_durations(
        args.log,
        t_start,
        t_end,
        include_switches=args.include_switches,
        cutoffs=cutoffs,
    )

    has_data = print_summary_table(durations_by_cmd)
    if has_data:
        print()
        plot_boxplot(durations_by_cmd, output=args.output)


if __name__ == "__main__":
    main()
