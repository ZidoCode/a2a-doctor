#!/usr/bin/env python3
"""a2a-artifact-audit — audit A2A artifact streaming for silent data loss.

A2A artifacts stream in chunks: `artifact-update` events with append=true,
a last_chunk flag, and artifactIds the client is trusted to track. The spec
leaves the hard cases open — can an artifact be replaced after last_chunk
(a2aproject/A2A#1231)? Should append to an unknown artifactId fail loudly
(silent drop today: a2a-python#1038)? Is there any content integrity at all
(proposal #1140)?

a2a-artifact-audit replays a recorded artifact stream and reports:
  - chunks appended to artifactIds that were never announced (silent drops)
  - duplicate part deliveries (appended twice)
  - events after last_chunk (post-completion writes)
  - artifacts never closed (missing last_chunk)
  - size accounting per artifact

Usage:
  python3 a2a_artifact_audit.py audit <stream.jsonl>
  python3 a2a_artifact_audit.py report <stream.jsonl>
  python3 a2a_artifact_audit.py simulate <card-url-or-file> [--artifacts N] [--chunks M]

Stream format: one JSON event per line (message/stream output).

Zero dependencies. Python 3.8+.
"""

import argparse
import json
import sys

from a2a_tax import est_tokens, fetch_card
from a2a_stream_tax import event_tokens, load_stream


# ---------------------------------------------------------------- audit

def audit(events):
    """Replay artifact-update events, detect data-integrity violations.
    Returns (findings, artifacts). findings: list of (severity, msg)."""
    CRIT, WARN, INFO = "CRITICAL", "WARNING", "INFO"
    findings = []
    artifacts = {}   # artifactId -> {"chunks": n, "parts_tokens": t, "closed": bool,
                     #                "seen_parts": set, "announced": bool}
    order = 0
    for ev in events:
        order += 1
        res = ev.get("result") if isinstance(ev.get("result"), dict) else {}
        if res.get("kind") != "artifact-update":
            continue
        art = res.get("artifact") or {}
        aid = art.get("artifactId")
        if not aid:
            findings.append((WARN, f"event {order}: artifact-update without artifactId"))
            continue
        append = bool(res.get("append"))
        last = bool(res.get("last_chunk"))
        parts = art.get("parts") or []
        part_sig = json.dumps(parts, sort_keys=True, separators=(",", ":"))
        ptoks = est_tokens(part_sig)

        a = artifacts.setdefault(aid, {"chunks": 0, "parts_tokens": 0,
                                       "closed": False, "seen_parts": [],
                                       "announced": False})
        # 1. protocol hygiene: first chunk should not be an append
        #    (server-side, append to an unannounced id is silently dropped — a2a-python#1038;
        #     from a client replay we flag it as hygiene, not data loss)
        if append and a["chunks"] == 0:
            findings.append((INFO, f"event {order}: first chunk of {aid!r} has append=true — "
                                   "should be a fresh write (servers drop appends to unknown ids)"))
        # 2. write after last_chunk (a2aproject/A2A#1231)
        if a["closed"]:
            findings.append((CRIT, f"event {order}: write to {aid!r} after last_chunk "
                                   "(artifact already closed)"))
        # 3. duplicate part delivery
        if part_sig in a["seen_parts"]:
            findings.append((WARN, f"event {order}: duplicate part delivered to {aid!r} "
                                   f"(~{ptoks} tokens re-sent)"))
        # 4. replacement without append after previously closed is #2; full
        #    replacement (append=false) after chunks is suspicious
        if not append and a["chunks"] > 0:
            findings.append((INFO, f"event {order}: artifact {aid!r} replaced wholesale "
                                   f"({a['chunks']} earlier chunk(s) discarded)"))
        a["chunks"] += 1
        a["parts_tokens"] += ptoks
        a["seen_parts"].append(part_sig)
        a["announced"] = True
        if last:
            a["closed"] = True

    # 5. never closed
    for aid, a in sorted(artifacts.items()):
        if not a["closed"]:
            findings.append((WARN, f"artifact {aid!r} never closed — missing last_chunk "
                                   f"({a['chunks']} chunk(s) held open)"))
    return findings, artifacts


# ---------------------------------------------------------------- simulate

def simulate(card, artifacts=3, chunks=5, gap=True):
    """Model an artifact stream: N artifacts x M chunks. With gap=True,
    one append targets an unannounced artifact (the silent-drop class)."""
    base = {"tokens": 0}
    ev = {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "artifact-update", "taskId": "t1", "contextId": "c1",
        "append": True, "last_chunk": False,
        "artifact": {"artifactId": "a", "parts": [{"kind": "text",
                     "text": "chunk payload — simulating artifact audit"}]}}}
    per = event_tokens(ev)
    total_events = artifacts * chunks
    base["tokens"] = per * total_events
    findings = 1 if gap else 0
    return {"artifact_events": total_events, "tokens_total": base["tokens"],
            "per_event_tokens": per, "simulated_findings": findings,
            "note": "1 first-chunk has append=true (protocol hygiene)" if gap else ""}


# ---------------------------------------------------------------- report

def report(events):
    findings, artifacts = audit(events)
    crit = sum(1 for s, _ in findings if s == "CRITICAL")
    warn = sum(1 for s, _ in findings if s == "WARNING")
    info = sum(1 for s, _ in findings if s == "INFO")
    lines = [f"a2a-artifact-audit report — {len(artifacts)} artifact(s), {len(findings)} finding(s)"]
    lines.append("")
    for aid, a in sorted(artifacts.items()):
        state = "closed" if a["closed"] else "OPEN (no last_chunk)"
        lines.append(f"  {aid:20s} {a['chunks']:>3} chunks  {a['parts_tokens']:>7,} tok  {state}")
    if findings:
        lines.append("")
        for s, m in findings:
            lines.append(f"  [{s:8s}] {m}")
        lines.append("")
        if crit:
            lines.append(f"  verdict: {crit} CRITICAL — chunks silently lost or written after close.")
        else:
            lines.append("  verdict: no critical loss — review warnings above.")
    else:
        lines.append("  clean — every chunk announced, ordered, and closed.")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a-artifact-audit",
                                 description="Audit A2A artifact streaming for silent data loss.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_a = sub.add_parser("audit", help="machine list of findings")
    p_a.add_argument("path")

    p_r = sub.add_parser("report", help="human-readable report")
    p_r.add_argument("path")

    p_s = sub.add_parser("simulate", help="model artifact stream from an AgentCard")
    p_s.add_argument("source")
    p_s.add_argument("--artifacts", type=int, default=3)
    p_s.add_argument("--chunks", type=int, default=5)
    p_s.add_argument("--no-gap", action="store_true", help="model a clean stream")

    args = ap.parse_args(argv)
    args.artifacts = max(0, getattr(args, "artifacts", 3))
    args.chunks = max(0, getattr(args, "chunks", 5))

    if args.cmd == "simulate":
        try:
            card, _ = fetch_card(args.source)
        except Exception as e:
            print(f"error: cannot load card from {args.source}: {e}", file=sys.stderr)
            return 2
        if not isinstance(card, dict):
            print(f"error: {args.source} is not an AgentCard (expected a JSON object, "
                  f"got {type(card).__name__})", file=sys.stderr)
            return 2
        m = simulate(card, args.artifacts, args.chunks, not args.no_gap)
        for k, v in m.items():
            print(f"{k:22s}: {v:,}" if isinstance(v, int) else f"{k:22s}: {v}")
        return 0

    try:
        events, bad = load_stream(args.path)
    except OSError as e:
        print(f"error: cannot read stream file {args.path}: {e}", file=sys.stderr)
        return 2
    if bad:
        print(f"note: skipped {len(bad)} unparseable line(s), first: line {bad[0][0]}: {bad[0][1]!r}",
              file=sys.stderr)
    if not events:
        print(f"error: no valid events in {args.path}", file=sys.stderr)
        return 2

    if args.cmd == "audit":
        findings, _ = audit(events)
        for s, m in findings:
            print(f"[{s:8s}] {m}")
        if not findings:
            print("clean — no artifact integrity violations")
        return 2 if any(s == "CRITICAL" for s, _ in findings) else 0

    print(report(events))
    return 2 if any(s == "CRITICAL" for s, _ in audit(events)[0]) else 0


if __name__ == "__main__":
    sys.exit(main())
