#!/usr/bin/env python3
"""a2a-context-audit — audit context/task correlation in a recorded A2A stream.

The A2A spec leaves contextId semantics loose: a contextId groups related
tasks, a taskId is the unit of work. Clients and servers get this wrong
constantly — empty IDs, contexts that mutate mid-task, taskIds reused under
a different context, orphan contexts that never produce a task.

This tool replays a recorded JSONL stream and reports every correlation
violation. Dumb math, honest numbers, zero dependencies. MIT license.

Pain anchors:
  a2aproject/A2A#1309  — contextId behavior when sent with taskId, ambiguous
  a2aproject/A2A#2061  — multi-turn conversation semantics still open
  a2aproject/A2A#762   — community discussion: contextId vs taskId confusion
  a2a-java#770         — client sends empty task + context IDs

Usage:
  a2a-context-audit audit  <stream.jsonl>     exit 2 on CRITICAL findings
  a2a-context-audit report <stream.jsonl>     always exit 0
"""

import argparse
import json
import sys

CRIT, WARN, INFO = "CRITICAL", "WARNING", "INFO"


def _extract(lines):
    """Yield (lineno, result dict, params dict or None, obj) for parseable lines."""
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
        params = obj.get("params")
        yield i, (res if isinstance(res, dict) else None), (params if isinstance(params, dict) else None), obj


def _ids_from(obj):
    if obj is None:
        return None, None
    tid = obj.get("taskId")
    if tid is None:
        tid = obj.get("id")
    cid = obj.get("contextId")
    return tid, cid


def audit(lines):
    """Return (findings, stats). findings: list of (severity, message)."""
    findings = []
    tasks = {}       # taskId -> {"context": cid, "events": n}
    contexts = {}    # contextId -> {"tasks": set, "events": n}
    seen_request_ids = {}  # JSON-RPC request id -> lineno (reuse detection)

    for lineno, res, params, obj in _extract(lines):
        # each line: collect (taskId, contextId) from result and/or params ONCE
        sources = [s for s in (res, params) if s is not None]
        if not sources:
            sources = [None]
        seen_pairs = set()
        for src in sources:
            tid, cid = _ids_from(src)
            if tid is None and cid is None:
                continue
            # empty string IDs = critical (a2a-java#770 bug class)
            if tid == "" or cid == "":
                findings.append((CRIT, f"line {lineno}: empty {'taskId' if tid == '' else 'contextId'} "
                                       "(a2a-java#770 bug class)"))
            key = (tid, cid)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            if tid:
                t = tasks.setdefault(tid, {"context": cid, "events": 0})
                t["events"] += 1
                if t["context"] and cid and t["context"] != cid:
                    findings.append((CRIT, f"line {lineno}: task {tid!r} changed context "
                                           f"{t['context']!r} -> {cid!r} (task/context is immutable)"))
                if t["context"] is None and cid:
                    t["context"] = cid
            if cid:
                c = contexts.setdefault(cid, {"tasks": set(), "events": 0})
                c["events"] += 1
                if tid:
                    c["tasks"].add(tid)

        # JSON-RPC id reuse with different methods (hard to correlate replies)
        rid = obj.get("id")
        if rid is not None and obj.get("method"):
            key = json.dumps(rid)
            if key in seen_request_ids:
                findings.append((WARN, f"line {lineno}: JSON-RPC request id {rid} reused "
                                       f"(first at line {seen_request_ids[key]}) — replies are ambiguous"))
            else:
                seen_request_ids[key] = lineno

    # orphan contexts: never produced a task
    for cid, c in sorted(contexts.items()):
        if not c["tasks"]:
            findings.append((INFO, f"context {cid!r} has {c['events']} event(s) but never produced a task "
                                   "(chat-only or broken correlation)"))
    # tasks with no context at all
    for tid, t in sorted(tasks.items()):
        if t["context"] is None:
            findings.append((WARN, f"task {tid!r} never carried a contextId — "
                                   "multi-turn resume will not work (A2A#1309)"))
    return findings, {"tasks": len(tasks), "contexts": len(contexts)}


def report(lines):
    findings, stats = audit(lines)
    crit = sum(1 for s, _ in findings if s == CRIT)
    warn = sum(1 for s, _ in findings if s == WARN)
    print(f"a2a-context-audit report — {stats['tasks']} task(s), {stats['contexts']} context(s), "
          f"{len(findings)} finding(s)\n")
    for s, m in findings:
        print(f"  [{s:8s}] {m}")
    print()
    if crit:
        print(f"  verdict: {crit} CRITICAL — context/task correlation is broken; "
              "multi-turn and resume will silently misbehave.")
    elif warn:
        print("  verdict: no critical violations — review warnings above.")
    else:
        print("  verdict: clean — every task/context correlation is consistent.")
    return 2 if crit else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="a2a-context-audit",
                                description="Audit A2A context/task correlation from a recorded stream.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "report"):
        s = sub.add_parser(name)
        s.add_argument("stream")
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
    code = report(lines)
    return code if args.cmd == "audit" else 0


if __name__ == "__main__":
    sys.exit(main())
