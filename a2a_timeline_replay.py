#!/usr/bin/env python3
"""a2a-timeline-replay — deterministic time-travel explorer for a recorded A2A stream.

All the audit tools tell you WHERE the violation is ("line 14"). This tool
tells you what AROUND it looks like: per-task state timelines, event windows
around any task, and full raw JSON at any line. The `git log` for agent
sessions. Offline, deterministic, zero dependencies. MIT license.

Usage:
  a2a-timeline-replay timeline <stream.jsonl>            per-task state summary
  a2a-timeline-replay around <stream.jsonl> <taskId>     window around a task
  a2a-timeline-replay at <stream.jsonl> <line>           full JSON at a line
  a2a-timeline-replay grep <stream.jsonl> <pattern>      find lines matching
"""

import argparse
import json
import sys

TERMINAL = {"completed", "canceled", "failed", "rejected"}


def _iter_events(lines):
    for i, line in enumerate(lines, 1):
        if not isinstance(line, str):
            line = json.dumps(line)
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        res = obj.get("result")
        yield i, obj, (res if isinstance(res, dict) else None)


def _describe(res):
    kind = res.get("kind") or res.get("method") or "event"
    if kind == "status-update":
        st = res.get("status") or {}
        return f"status-update {res.get('taskId','?')} → {st.get('state','?')}"
    if kind == "artifact-update":
        art = res.get("artifact") or {}
        parts = art.get("parts") or []
        n = len(parts) if isinstance(parts, list) else 0
        return f"artifact-update {res.get('taskId','?')} → {art.get('artifactId','?')} ({n} part(s))"
    if kind == "message":
        return f"message {res.get('taskId','?') or '(no task)'}"
    return kind


def timeline(lines):
    tasks = {}
    for lineno, obj, res in _iter_events(lines):
        if not res:
            continue
        tid = res.get("taskId")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"states": [], "artifacts": 0, "lines": []})
        t["lines"].append(lineno)
        if res.get("kind") == "status-update":
            st = res.get("status") or {}
            state = st.get("state")
            if state:
                t["states"].append(state)
        elif res.get("kind") == "artifact-update":
            t["artifacts"] += 1
    if not tasks:
        print("no tasks found in stream")
        return 0
    print(f"a2a-timeline-replay timeline — {len(tasks)} task(s)\n")
    for tid, t in tasks.items():
        chain = " → ".join(t["states"]) if t["states"] else "(no status events)"
        flag = ""
        if t["states"] and t["states"][-1] not in TERMINAL:
            flag = "  [OPEN]"
        print(f"  {tid[:28]:28s} {chain}{flag}")
        print(f"  {'':28s} lines {t['lines'][0]}–{t['lines'][-1]}, {t['artifacts']} artifact(s)")
    return 0


def around(lines, task_id, before=3, after=3):
    # find the task's line range
    lo, hi, found = None, None, False
    for lineno, obj, res in _iter_events(lines):
        if res and res.get("taskId") == task_id:
            found = True
            if lo is None:
                lo = lineno
            hi = lineno
    if not found:
        print(f"error: task {task_id!r} not found in stream", file=sys.stderr)
        return 2
    start = max(1, lo - before)
    end = min(len(lines), hi + after)
    print(f"a2a-timeline-replay around — task {task_id!r} spans lines {lo}–{hi}, "
          f"showing {start}–{end}\n")
    for lineno, obj, res in _iter_events(lines):
        if start <= lineno <= end:
            desc = _describe(res) if res else "(not an A2A result)"
            is_target = bool(res and res.get("taskId") == task_id)
            marker = " <" if is_target else "  "
            print(f"  {marker} line {lineno:5d}  {desc}")
    print("\nuse `at <line>` for the full JSON of any line above")
    return 0


def at(lines, lineno):
    if lineno < 1 or lineno > len(lines):
        print(f"error: line {lineno} out of range (1–{len(lines)})", file=sys.stderr)
        return 2
    raw = lines[lineno - 1].strip()
    if not raw:
        print(f"line {lineno} is empty")
        return 0
    try:
        obj = json.loads(raw)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return 0
    except json.JSONDecodeError:
        print(f"line {lineno} is not JSON:\n{raw}")
        return 0


def grep(lines, pattern):
    hits = 0
    try:
        rx = re.compile(pattern)
    except re.error as e:
        print(f"error: bad pattern: {e}", file=sys.stderr)
        return 2
    for lineno, obj, res in _iter_events(lines):
        desc = _describe(res) if res else ""
        blob = json.dumps(obj) if not desc else f"line {lineno}: {desc}"
        if rx.search(blob) or rx.search(json.dumps(obj)):
            print(f"  line {lineno:5d}  {desc or '(raw match)'}")
            hits += 1
    print(f"\n{hits} hit(s)")
    return 0


import re


def main(argv=None):
    p = argparse.ArgumentParser(prog="a2a-timeline-replay",
                                description="Deterministic time-travel explorer for a recorded A2A stream.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("timeline"); s.add_argument("stream")
    s = sub.add_parser("around"); s.add_argument("stream"); s.add_argument("task_id")
    s.add_argument("--before", type=int, default=3); s.add_argument("--after", type=int, default=3)
    s = sub.add_parser("at"); s.add_argument("stream"); s.add_argument("line", type=int)
    s = sub.add_parser("grep"); s.add_argument("stream"); s.add_argument("pattern")
    args = p.parse_args(argv)
    try:
        with open(args.stream, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"error: cannot read stream: {e}", file=sys.stderr)
        return 2
    if not any(l.strip() for l in lines):
        print(f"error: {args.stream} has no events", file=sys.stderr)
        return 2
    if args.cmd == "timeline":
        return timeline(lines)
    if args.cmd == "around":
        return around(lines, args.task_id, max(0, args.before), max(0, args.after))
    if args.cmd == "at":
        return at(lines, args.line)
    return grep(lines, args.pattern)


if __name__ == "__main__":
    sys.exit(main())
