#!/usr/bin/env python3
"""a2a-stream-tax — measure the token tax of A2A streaming (SSE) sessions.

A2A's message/stream sends a JSON-RPC StatusUpdateEvent or ArtifactEvent on
every state change and every artifact chunk. Each event carries the full
envelope (jsonrpc, id, metadata) plus the task snapshot. A chatty agent
streaming a 20-minute task can push more tokens through the event stream
than the final answer costs. Nobody measures this.

a2a-stream-tax audits a recorded stream (JSONL, one event per line) or
simulates one from an AgentCard. Dumb math, honest numbers, zero deps.

Usage:
  python3 a2a_stream_tax.py measure <stream.jsonl>
  python3 a2a_stream_tax.py simulate <card-url-or-file> [--updates N] [--artifacts N]
  python3 a2a_stream_tax.py report <stream.jsonl> [--price-per-mtok 3.0]

Stream format: one JSON object per line, as emitted by message/stream:
  {"jsonrpc":"2.0","id":1,"result":{"kind":"status-update","status":{"state":"working",...}}}

Zero dependencies. Python 3.8+.
"""

import argparse
import json
import sys
import urllib.request

from a2a_tax import est_tokens, fetch_card, scan_card


# ---------------------------------------------------------------- measure

def load_stream(path):
    """Load a recorded stream: one JSON event per line. Returns list of dicts.
    Lines that fail to parse are collected as (lineno, raw) for reporting —
    a half-written last line is normal when a stream was cut mid-write."""
    events, bad = [], []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                bad.append((lineno, line[:60]))
                continue
            if isinstance(ev, dict):
                events.append(ev)
            else:
                bad.append((lineno, str(line)[:60]))
    return events, bad


def event_tokens(ev):
    """Tokens of one SSE event, exactly as serialized on the wire."""
    return est_tokens(json.dumps(ev, separators=(",", ":")))


def classify(ev):
    """Bucket an event by its A2A result kind (or error)."""
    if not isinstance(ev, dict):
        return "other"
    if "error" in ev:
        return "error"
    res = ev.get("result")
    if not isinstance(res, dict):
        return "other"
    kind = res.get("kind", "other")
    return {
        "status-update": "status",
        "artifact-update": "artifact",
        "message": "message",
        "task": "task",
    }.get(kind, kind)


def measure(events):
    """Per-kind token totals + envelope overhead for a recorded stream."""
    buckets = {}
    envelope = 0
    total = 0
    for ev in events:
        t = event_tokens(ev)
        total += t
        k = classify(ev)
        b = buckets.setdefault(k, {"count": 0, "tokens": 0})
        b["count"] += 1
        b["tokens"] += t
        # envelope = the JSON-RPC frame + task/status boilerplate around the payload
        res = ev.get("result") if isinstance(ev.get("result"), dict) else {}
        payload = res.get("artifact") or res.get("history")
        if payload is not None:
            envelope += t - est_tokens(json.dumps(payload, separators=(",", ":")))
        else:
            envelope += t
    return {"total": total, "events": len(events), "buckets": buckets,
            "envelope": max(0, envelope)}


# ---------------------------------------------------------------- simulate

def simulate(card, updates=30, artifacts=5):
    """Model a stream from an AgentCard: `updates` status events at ~1/s,
    `artifacts` artifact chunks. Status events re-ship the task snapshot."""
    _, br = scan_card(card)
    skills = br["skills"]["count"]
    # status event: jsonrpc frame + status-update + task snapshot w/ context
    status = {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "status-update", "taskId": "task-1", "contextId": "ctx-1",
        "status": {"state": "working",
                   "message": {"role": "agent",
                               "parts": [{"kind": "text",
                                          "text": "progress update — simulating stream tax"}]},
                   "timestamp": "2026-10-17T00:00:00Z"},
        "final": False}}
    art = {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "artifact-update", "taskId": "task-1", "contextId": "ctx-1",
        "artifact": {"artifactId": "art-1", "name": "result",
                     "parts": [{"kind": "text",
                                "text": "artifact chunk — simulating stream tax payload"}]}}}
    per_status = event_tokens(status)
    per_art = event_tokens(art)
    # card re-fetch is billed per session before the stream starts
    card_t = br["TOTAL"]
    total = card_t + per_status * updates + per_art * artifacts
    return {"card_tokens": card_t, "skills": skills,
            "status_events": updates, "per_status_tokens": per_status,
            "artifact_events": artifacts, "per_artifact_tokens": per_art,
            "envelope_share_pct": round(100 * (per_status * updates) / total, 1) if total else 0,
            "total": total}


# ---------------------------------------------------------------- report

def report(events, price_per_mtok):
    m = measure(events)
    if not m["events"]:
        return "empty stream — nothing to bill", None
    total = m["total"]

    # the fix: trim envelope, coalesce status events (only state changes),
    # stream artifacts without re-shipping metadata per chunk
    fix_lines = []
    status_b = m["buckets"].get("status", {"count": 0, "tokens": 0})
    art_b = m["buckets"].get("artifact", {"count": 0, "tokens": 0})

    # coalesce: keep first/last status, drop intermediate chatter
    coalesced = total - max(0, status_b["tokens"] - 2 * (status_b["tokens"] // max(1, status_b["count"])))
    # envelope trim: metadata repetition is ~re-shipping status blocks
    after = coalesced - m["envelope"] // 2
    after = max(total // 10, after)  # never model below 10% of measured
    savings = total - after
    pct = round(100 * savings / total, 0) if total else 0

    def usd(t):
        return t / 1_000_000 * price_per_mtok

    lines = []
    lines.append(f"a2a-stream-tax report — {m['events']} events")
    lines.append("")
    for k in sorted(m["buckets"], key=lambda x: -m["buckets"][x]["tokens"]):
        b = m["buckets"][k]
        lines.append(f"  {k:10s} ({b['count']:>4} ev)  : {b['tokens']:>8,} tok")
    lines.append(f"  of which envelope overhead: {m['envelope']:>8,} tok")
    lines.append(f"  TOTAL streamed           : {total:>8,} tok  (~${usd(total):.4f})")
    lines.append("")
    lines.append("  with the fix (coalesce status + trim envelope):")
    lines.append(f"    after fix              : {after:>8,} tok  (~${usd(after):.4f})")
    lines.append(f"    savings                : {savings:>8,} tok  ({pct:.0f}%)")
    return "\n".join(lines), m


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a-stream-tax",
                                 description="Measure the token tax of A2A SSE streaming.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_m = sub.add_parser("measure", help="token cost of a recorded stream (JSONL)")
    p_m.add_argument("path")

    p_s = sub.add_parser("simulate", help="model a stream from an AgentCard")
    p_s.add_argument("source")
    p_s.add_argument("--updates", type=int, default=30)
    p_s.add_argument("--artifacts", type=int, default=5)

    p_r = sub.add_parser("report", help="full bill + the fix")
    p_r.add_argument("path")
    p_r.add_argument("--price-per-mtok", type=float, default=3.0,
                     help="input token price, USD per million (default 3.00)")

    args = ap.parse_args(argv)
    args.price_per_mtok = max(0.0, getattr(args, "price_per_mtok", 3.0))
    args.updates = max(0, getattr(args, "updates", 30))
    args.artifacts = max(0, getattr(args, "artifacts", 5))

    if args.cmd in ("simulate",):
        try:
            card, _ = fetch_card(args.source)
        except Exception as e:
            print(f"error: cannot load card from {args.source}: {e}", file=sys.stderr)
            return 2
        if not isinstance(card, dict):
            print(f"error: {args.source} is not an AgentCard (expected a JSON object, "
                  f"got {type(card).__name__})", file=sys.stderr)
            return 2
        m = simulate(card, args.updates, args.artifacts)
        for k, v in m.items():
            print(f"{k:22s}: {v:,}" if isinstance(v, int) else f"{k:22s}: {v}")
        return 0

    # measure / report need a stream file
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

    if args.cmd == "measure":
        m = measure(events)
        print(f"events: {m['events']}")
        for k in sorted(m["buckets"], key=lambda x: -m["buckets"][x]["tokens"]):
            b = m["buckets"][k]
            print(f"  {k:10s} ({b['count']:>4} ev)  : {b['tokens']:>8,} tok")
        print(f"  envelope overhead   : {m['envelope']:>8,} tok")
        print(f"  TOTAL               : {m['total']:>8,} tok")
        return 0

    out, _ = report(events, args.price_per_mtok)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
