#!/usr/bin/env python3
"""a2a-reconnect-tax — measure the token cost of SSE reconnect replays.

When an A2A message/stream connection drops, the client resubscribes. The
server may replay events the client already received (or the client may
re-request the task, pulling the full history again). Every replayed event is
a token the caller pays for twice. A2A issue #1635 leaves replay semantics
open — so reconnects silently double-bill.

a2a-reconnect-tax compares two event streams (before and after a reconnect)
and reports exactly how many tokens were replayed, duplicated, and wasted.
Dumb math, honest numbers, zero dependencies.

Usage:
  python3 a2a_reconnect_tax.py audit <stream.jsonl>
  python3 a2a_reconnect_tax.py simulate <card-url-or-file> [--drops N] [--stream-len N]
  python3 a2a_reconnect_tax.py report <stream.jsonl> [--price-per-mtok 3.0]

Stream format: one JSON event per line (message/stream output). A reconnect
is marked by a line that is exactly "---" or {"reconnect": true}.

Zero dependencies. Python 3.8+.
"""

import argparse
import os
import json
import sys

from a2a_tax import est_tokens, fetch_card
from a2a_stream_tax import event_tokens, classify, load_stream


# ---------------------------------------------------------------- audit

def split_segments(events, reconnect_marker=False):
    """Split an event list into segments at reconnect markers."""
    segments, current = [], []
    for ev in events:
        if ev == {"reconnect": True} or ev == "---":
            segments.append(current)
            current = []
        else:
            current.append(ev)
    segments.append(current)
    return [s for s in segments if s]


def event_key(ev):
    """Identity of an event for dedup: the full semantic payload. Two events
    are the same only if everything the client would consume is identical."""
    return json.dumps(ev, sort_keys=True, separators=(",", ":"))


def audit(events):
    """Find replayed tokens across reconnect boundaries.
    A replayed event = an event after a marker whose key was already seen
    in a previous segment. Duplicated tokens = tokens of those events."""
    segments = split_segments(events)
    # Replays only exist across reconnect boundaries: within one unbroken
    # segment, identical payloads are legitimate repeats, not waste.
    seen_before = set()
    replayed_tokens = 0
    replayed_count = 0
    fresh_tokens = 0
    fresh_count = 0
    for seg_idx, seg in enumerate(segments):
        seen_in_seg = set()
        for ev in seg:
            t = event_tokens(ev)
            k = event_key(ev)
            if seg_idx > 0 and k in seen_before:
                replayed_tokens += t
                replayed_count += 1
            else:
                fresh_tokens += t
                fresh_count += 1
                seen_in_seg.add(k)
        seen_before |= seen_in_seg
    total = replayed_tokens + fresh_tokens
    return {
        "reconnects": len(segments) - 1,
        "events_total": fresh_count + replayed_count,
        "events_fresh": fresh_count,
        "events_replayed": replayed_count,
        "tokens_total": total,
        "tokens_fresh": fresh_tokens,
        "tokens_replayed": replayed_tokens,
        "replay_pct": round(100 * replayed_tokens / total, 1) if total else 0.0,
    }


# ---------------------------------------------------------------- simulate

def simulate(card, drops=3, stream_len=200):
    """Model a stream with `drops` reconnects. On each reconnect, the server
    replays the last `replay_window` events (conservative: last 10% of stream)."""
    from a2a_stream_tax import simulate as sim_stream
    base = sim_stream(card, updates=max(1, stream_len - 5), artifacts=5)
    per_ev = base["per_status_tokens"]
    total_events = base["status_events"] + base["artifact_events"]
    # each drop replays the last 10% of events so far (no Last-Event-ID support)
    replay_window = max(1, int(total_events * 0.1))
    replayed = drops * replay_window
    replayed_tokens = replayed * per_ev
    total = base["total"] + replayed_tokens
    return {
        "events_total": total_events,
        "drops": drops,
        "replay_window_each": replay_window,
        "events_replayed": replayed,
        "tokens_base": base["total"],
        "tokens_replayed": replayed_tokens,
        "tokens_total": total,
        "replay_pct": round(100 * replayed_tokens / total, 1) if total else 0.0,
    }


# ---------------------------------------------------------------- report

def report(events, price_per_mtok):
    m = audit(events)
    if m["reconnects"] == 0:
        return ("no reconnect markers found — add a '---' line at each reconnect "
                "to audit replay waste"), m

    def usd(t):
        return t / 1_000_000 * price_per_mtok

    # the fix: Last-Event-ID support = zero replay; or client-side dedup = free
    lines = []
    lines.append(f"a2a-reconnect-tax report — {m['reconnects']} reconnects, {m['events_total']} events")
    lines.append("")
    lines.append(f"  events replayed          : {m['events_replayed']:>8,}")
    lines.append(f"  tokens replayed (wasted) : {m['tokens_replayed']:>8,} tok  (~${usd(m['tokens_replayed']):.4f})")
    lines.append(f"  tokens fresh             : {m['tokens_fresh']:>8,} tok")
    lines.append(f"  TOTAL billed             : {m['tokens_total']:>8,} tok")
    lines.append(f"  replay share             : {m['replay_pct']:>7.1f}% of the bill is double-billing")
    lines.append("")
    lines.append("  the fix:")
    lines.append("    client: remember Last-Event-ID, discard duplicates on resume (free)")
    lines.append("    server: honor Last-Event-ID, replay nothing (protocol)")
    lines.append(f"    savings               : {m['tokens_replayed']:>8,} tok per session (100% of replay)")
    return "\n".join(lines), m


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a-reconnect-tax",
                                 description="Measure the token cost of SSE reconnect replays.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_a = sub.add_parser("audit", help="find replayed tokens in a recorded stream")
    p_a.add_argument("path")

    p_s = sub.add_parser("simulate", help="model reconnect waste from an AgentCard")
    p_s.add_argument("source")
    p_s.add_argument("--drops", type=int, default=3)
    p_s.add_argument("--stream-len", type=int, default=200)

    p_r = sub.add_parser("report", help="full bill + the fix")
    p_r.add_argument("path")
    p_r.add_argument("--price-per-mtok", type=float, default=3.0,
                     help="input token price, USD per million (default 3.00)")

    args = ap.parse_args(argv)
    args.price_per_mtok = max(0.0, getattr(args, "price_per_mtok", 3.0))
    args.drops = max(0, getattr(args, "drops", 3))
    args.stream_len = max(1, getattr(args, "stream_len", 200))

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
        m = simulate(card, args.drops, args.stream_len)
        for k, v in m.items():
            print(f"{k:22s}: {v:,}" if isinstance(v, int) else f"{k:22s}: {v}")
        return 0

    try:
        raw = open(args.path, "r", encoding="utf-8").read().splitlines()
    except OSError as e:
        print(f"error: cannot read stream file {args.path}: {e}", file=sys.stderr)
        return 2
    # replace '---' markers with a JSON marker BEFORE parsing
    raw = ['{"reconnect": true}' if ln.strip() == "---" else ln for ln in raw]
    tmp_marker_file = args.path + ".markers"
    with open(tmp_marker_file, "w", encoding="utf-8") as f:
        f.write("\n".join(raw) + "\n")
    try:
        events, bad = load_stream(tmp_marker_file)
    except OSError as e:
        print(f"error: cannot read stream file {args.path}: {e}", file=sys.stderr)
        return 2
    if bad:
        print(f"note: skipped {len(bad)} unparseable line(s), first: line {bad[0][0]}: {bad[0][1]!r}",
              file=sys.stderr)
    try:
        os.remove(tmp_marker_file)
    except OSError:
        pass
    events = [{"reconnect": True} if e == "---" else e for e in events]
    if not any(e != {"reconnect": True} for e in events):
        print(f"error: no valid events in {args.path}", file=sys.stderr)
        return 2

    if args.cmd == "audit":
        m = audit(events)
        for k, v in m.items():
            print(f"{k:22s}: {v:,}" if isinstance(v, int) else f"{k:22s}: {v}")
        return 0

    out, _ = report(events, args.price_per_mtok)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
