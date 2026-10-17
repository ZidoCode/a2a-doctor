#!/usr/bin/env python3
"""a2a-task-state-audit — audit A2A task lifecycle transitions from a recorded stream.

The A2A spec (life-of-a-task) says terminal states (completed, failed,
canceled, rejected) are FINAL. Buggy servers resurrect tasks, emit updates
after a terminal state, or invent status values that don't exist in the spec.

This tool replays a recorded JSONL stream and reports every violation.
Dumb math, honest numbers, zero dependencies. MIT license.

Usage:
  a2a-task-state-audit audit  <stream.jsonl>     exit 2 if violations
  a2a-task-state-audit report <stream.jsonl>     always exit 0
  a2a-task-state-audit simulate <card.json> [--tasks N] [--steps N]
                                                 model the cost of status chatter
"""

import argparse
import json
import sys

# A2A spec: TaskState enum + the transition graph from life-of-a-task.md
VALID_STATES = {
    "submitted", "working", "input-required", "completed",
    "canceled", "failed", "rejected", "auth-required", "unknown",
}
TERMINAL = {"completed", "canceled", "failed", "rejected"}
# Legal forward transitions (spec is forgiving mid-flight; terminal states are NOT)
LEGAL = {
    "submitted": {"working", "input-required", "auth-required", "completed",
                  "canceled", "failed", "rejected", "unknown"},
    "working": {"working", "input-required", "auth-required", "completed",
                "canceled", "failed", "rejected", "unknown"},
    "input-required": {"working", "completed", "canceled", "failed", "unknown"},
    "auth-required": {"working", "completed", "canceled", "failed", "unknown"},
    "unknown": {"working", "completed", "canceled", "failed", "unknown"},
    "completed": set(), "canceled": set(), "failed": set(), "rejected": set(),
}

CRIT, WARN, INFO = "CRITICAL", "WARNING", "INFO"


def _parts_tokens(result):
    a = result.get("artifact") or {}
    return sum(len(p.get("text", "")) // 4 for p in (a.get("parts") or [])
               if isinstance(p, dict))


def extract_events(lines):
    """Yield (lineno, event dict) for parseable status-update / artifact lines."""
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
        res = obj.get("result") or {}
        if not isinstance(res, dict):
            continue
        yield i, res


def audit(lines):
    """Return (findings, tasks). findings: list of (severity, message)."""
    findings, tasks = [], {}
    for lineno, res in extract_events(lines):
        kind = res.get("kind")
        tid = res.get("taskId")
        if kind == "status-update":
            if not tid:
                findings.append((WARN, f"line {lineno}: status-update without taskId"))
                continue
            status = res.get("status") or {}
            state = status.get("state")
            if state is None:
                findings.append((WARN, f"line {lineno}: status-update for {tid!r} without state"))
                continue
            if state not in VALID_STATES:
                findings.append((CRIT, f"line {lineno}: unknown state {state!r} for task {tid!r} "
                                       "(not in the A2A TaskState enum)"))
                continue
            t = tasks.setdefault(tid, {"states": [], "final": False, "tokens": 0})
            t["tokens"] += len(json.dumps(status)) // 4
            if t["final"]:
                prev = t["states"][-1] if t["states"] else "?"
                findings.append((CRIT, f"line {lineno}: task {tid!r} resumed after terminal state "
                                       f"{prev!r} -> {state!r} (terminal states are final)"))
                t["states"].append(state)
                continue
            prev = t["states"][-1] if t["states"] else None
            if prev and state not in LEGAL.get(prev, set()):
                findings.append((WARN, f"line {lineno}: illegal transition {prev!r} -> {state!r} "
                                       f"for task {tid!r}"))
            t["states"].append(state)
            if state in TERMINAL:
                t["final"] = True
        elif kind == "artifact-update":
            if not tid:
                findings.append((WARN, f"line {lineno}: artifact-update without taskId"))
                continue
            t = tasks.setdefault(tid, {"states": [], "final": False, "tokens": 0})
            t["tokens"] += _parts_tokens(res)
            if t["final"]:
                findings.append((CRIT, f"line {lineno}: artifact-update for terminal task {tid!r} "
                                       "(content delivered after the task closed)"))
    # tasks that never reached a terminal state
    for tid, t in sorted(tasks.items()):
        if not t["final"] and t["states"]:
            findings.append((WARN, f"task {tid!r} never reached a terminal state "
                                   f"(last: {t['states'][-1]!r})"))
    return findings, tasks


def report(lines):
    findings, tasks = audit(lines)
    crit = sum(1 for s, _ in findings if s == CRIT)
    print(f"a2a-task-state-audit report — {len(tasks)} task(s), {len(findings)} finding(s)\n")
    for tid, t in sorted(tasks.items()):
        chain = " -> ".join(t["states"]) if t["states"] else "(none)"
        print(f"  {tid[:24]:24s} {len(t['states'])} states  {t['tokens']:6d} tok  {chain}")
    if findings:
        print()
        for s, m in findings:
            print(f"  [{s:8s}] {m}")
    print()
    if crit:
        print(f"  verdict: {crit} CRITICAL — task lifecycle violated (terminal state not respected).")
    elif findings:
        print("  verdict: no critical violations — review warnings above.")
    else:
        print("  verdict: clean — every task followed a legal lifecycle.")
    return 2 if crit else (1 if findings else 0)


def simulate(card, tasks=3, steps=4):
    """Model status-chatter token cost of a task lifecycle (dumb estimate)."""
    if tasks < 0:
        tasks = 0
    if steps < 0:
        steps = 0
    name = str(card.get("name", "")) if isinstance(card, dict) else ""
    desc = str(card.get("description", "")) if isinstance(card, dict) else ""
    per_state = len(json.dumps({"kind": "status-update", "taskId": "t" * 16,
                                "contextId": "c" * 16, "status": {"state": "working"}})) // 4
    # tasks pay submitted + working... + terminal; steps-1 mid updates each
    chatter = tasks * (per_state * 2 + max(0, steps - 1) * per_state)
    return {"tasks": tasks, "per_task_updates": max(1, steps + 1),
            "per_update_tokens": per_state, "tokens_total": chatter,
            "note": f"{name}: {desc[:40]}" if name else ""}


def main(argv=None):
    p = argparse.ArgumentParser(prog="a2a-task-state-audit",
                                description="Audit A2A task lifecycle transitions from a recorded stream.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "report"):
        s = sub.add_parser(name)
        s.add_argument("stream")
    s = sub.add_parser("simulate")
    s.add_argument("card")
    s.add_argument("--tasks", type=int, default=3)
    s.add_argument("--steps", type=int, default=4)
    args = p.parse_args(argv)

    if args.cmd == "simulate":
        try:
            with open(args.card, encoding="utf-8") as f:
                card = json.load(f)
        except OSError as e:
            print(f"error: cannot read card {args.card}: {e}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as e:
            print(f"error: {args.card} is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(card, dict):
            print(f"error: {args.card} is not an AgentCard (expected a JSON object, "
                  f"got {type(card).__name__})", file=sys.stderr)
            return 2
        m = simulate(card, args.tasks, args.steps)
        print(json.dumps(m, indent=2))
        return 0

    try:
        with open(args.stream, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"error: cannot read stream file: {e}", file=sys.stderr)
        return 2
    if not any(l.strip() for l in lines):
        print(f"error: {args.stream} contains no events", file=sys.stderr)
        return 2
    code = report(lines)
    if args.cmd == "report":
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
