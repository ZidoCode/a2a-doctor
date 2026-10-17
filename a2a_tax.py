#!/usr/bin/env python3
"""a2a-tax — measure the protocol token tax of Agent2Agent (A2A) endpoints.

The A2A protocol (Linux Foundation, 2025) lets independent agents discover each
other through an AgentCard at /.well-known/agent-card.json and hand off tasks
via JSON-RPC. Every hop bills metadata nobody reads:

  1. the AgentCard itself, fetched per session
  2. the JSON-RPC + Task envelope on every message and poll
  3. the task history array, replayed in full on every tasks/get call

a2a-tax scans a card, models a task lifecycle, and reports the token bill with
the three-line fix (cache the card, cap the history, trim the envelope) priced
before and after. Estimates are deterministic — chars/4 heuristics, no model
calls, no network dependency beyond the card you point it at.

Usage:
  python3 a2a_tax.py scan <card-url-or-file>
  python3 a2a_tax.py hop <card-url-or-file> [--polls N] [--history-len N]
  python3 a2a_tax.py report <card-url-or-file> [--polls N] [--price-in-per-mtok 3000]

Zero dependencies. Python 3.8+.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------- tokenizer

def est_tokens(text):
    """Deterministic token estimate. chars/4 for mixed prose (OpenAI-era
    rule of thumb), with a floor of 1 for non-empty strings."""
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


def est_json_tokens(obj):
    """Tokens of a JSON-serialized object, exactly as it would ride the wire
    (compact separators — what json.dumps produces by default)."""
    return est_tokens(json.dumps(obj, separators=(",", ":")))


# ---------------------------------------------------------------- card scan

def fetch_card(source):
    """Load an AgentCard from an http(s) URL or a local file path."""
    if source.startswith("http://") or source.startswith("https://"):
        url = source.rstrip("/")
        if not url.endswith(".json") and not url.endswith("/agent-card.json"):
            url += "/.well-known/agent-card.json"
        req = urllib.request.Request(url, headers={"User-Agent": "a2a-tax/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")), url
    with open(source, "r", encoding="utf-8") as f:
        return json.load(f), source


def scan_card(card):
    """Per-field and per-skill token cost of an AgentCard.
    Returns (total, breakdown dict)."""
    breakdown = {}
    total = 0

    # scalar fields
    for key in ("name", "description", "version", "protocolVersion", "url",
                "preferredTransport", "documentationUrl"):
        if key in card:
            t = est_json_tokens({key: card[key]})
            breakdown[key] = t
            total += t

    # capabilities object
    if "capabilities" in card:
        t = est_json_tokens({"capabilities": card["capabilities"]})
        breakdown["capabilities"] = t
        total += t

    # skills — the usual bulk
    skills = card.get("skills") or []
    per_skill = []
    for s in skills:
        t = est_json_tokens(s)
        per_skill.append({"name": s.get("name", "?"), "tokens": t})
        total += t
    breakdown["skills"] = {"count": len(skills), "items": per_skill}

    # security schemes / auth blocks — often verbose
    if "securitySchemes" in card:
        t = est_json_tokens({"securitySchemes": card["securitySchemes"]})
        breakdown["securitySchemes"] = t
        total += t
    if "security" in card:
        t = est_json_tokens({"security": card["security"]})
        breakdown["security"] = t
        total += t

    # default transport fields the spec requires but clients rarely read
    for key in ("defaultInputModes", "defaultOutputModes", "provider",
                "additionalInterfaces", "signing"):
        if key in card:
            t = est_json_tokens({key: card[key]})
            breakdown[key] = t
            total += t

    breakdown["TOTAL"] = total
    return total, breakdown


# ---------------------------------------------------------------- hop model

def envelope_tokens(card, history_len, cap=None):
    """Tokens billed for ONE tasks/get poll of a task with `history_len`
    accumulated messages. Applies the history cap if given."""
    # JSON-RPC wrapper (method, jsonrpc, id, params.taskId)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get",
           "params": {"id": "task-123"}}
    rpc_t = est_json_tokens(rpc)

    # Task object: id, contextId, status, kind — plus history
    task = {"id": "task-123", "contextId": "ctx-456", "kind": "task",
            "status": {"state": "working"}}
    effective = history_len if cap is None else min(history_len, cap)
    history = []
    for i in range(effective):
        history.append({
            "role": "agent" if i % 2 else "user",
            "parts": [{"kind": "text",
                       "text": f"message chunk {i} — simulated payload for tax modeling"}],
            "messageId": f"msg-{i}",
            "taskId": "task-123",
            "contextId": "ctx-456",
            "kind": "message",
        })
    task["history"] = history
    resp = {"jsonrpc": "2.0", "id": 1, "result": task}
    resp_t = est_json_tokens(resp)

    return rpc_t + resp_t


def model_task(card, polls, history_len, cap=None):
    """Total token bill for one full task lifecycle:
    card fetch + initial message/send + polls."""
    card_t, _ = scan_card(card)

    # message/send envelope (roughly: rpc + one user message + reply task)
    send = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user",
                                   "parts": [{"kind": "text", "text": "do the thing"}],
                                   "messageId": "m-1", "kind": "message"}}}
    send_t = est_json_tokens(send) + envelope_tokens(card, 2)

    poll_t = envelope_tokens(card, history_len, cap)
    total = card_t + send_t + poll_t * (polls - 1)
    return {
        "card_tokens": card_t,
        "first_send_tokens": send_t,
        "poll_tokens_each": poll_t,
        "polls": polls,
        "total": total,
        "history_len": history_len,
        "cap": cap,
    }


# ---------------------------------------------------------------- report

def report(card, polls, history_len, price_per_mtok):
    base = model_task(card, polls, history_len, cap=None)
    capped = model_task(card, polls, history_len, cap=10)
    cached = model_task(card, polls, history_len, cap=10)
    # cached card: card billed once per N sessions, amortize to zero per-task
    cached["total"] -= cached["card_tokens"]

    def usd(tokens):
        return tokens / 1_000_000 * price_per_mtok

    lines = []
    lines.append(f"a2a-tax report — {polls} polls, history grows to {history_len} messages")
    lines.append("")
    lines.append(f"  card fetch (per session) : {base['card_tokens']:>7,} tok")
    lines.append(f"  first message/send       : {base['first_send_tokens']:>7,} tok")
    lines.append(f"  each tasks/get poll      : {base['poll_tokens_each']:>7,} tok")
    lines.append(f"  TOTAL per task           : {base['total']:>7,} tok  (~${usd(base['total']):.4f})")
    lines.append("")
    lines.append("  with the three-line fix:")
    lines.append(f"    cache the card         : -{base['card_tokens']:>7,} tok/task")
    lines.append(f"    cap history at 10      : {capped['total']:>7,} tok total")
    savings = base["total"] - capped["total"]
    pct = 100 * savings / base["total"] if base["total"] else 0
    lines.append(f"    TOTAL after fix        : {capped['total']:>7,} tok  (~${usd(capped['total']):.4f})")
    lines.append(f"    savings                : {savings:>7,} tok/task  ({pct:.0f}%)")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a-tax",
                                 description="Measure the A2A protocol token tax.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="token cost of an AgentCard")
    p_scan.add_argument("source")

    p_hop = sub.add_parser("hop", help="model one task lifecycle")
    p_hop.add_argument("source")
    p_hop.add_argument("--polls", type=int, default=45)
    p_hop.add_argument("--history-len", type=int, default=50)
    p_hop.add_argument("--cap", type=int, default=None)

    p_rep = sub.add_parser("report", help="full bill + the three-line fix")
    p_rep.add_argument("source")
    p_rep.add_argument("--polls", type=int, default=45)
    p_rep.add_argument("--history-len", type=int, default=50)
    p_rep.add_argument("--price-per-mtok", type=float, default=3.0,
                       help="input token price, USD per million (default 3.00)")

    args = ap.parse_args(argv)
    args.polls = max(1, getattr(args, "polls", 1))          # a task is at least one send
    args.history_len = max(0, getattr(args, "history_len", 50))
    if getattr(args, "cap", None) is not None:
        args.cap = max(0, args.cap)
    args.price_per_mtok = max(0.0, getattr(args, "price_per_mtok", 3.0))

    try:
        card, src = fetch_card(args.source)
    except Exception as e:
        print(f"error: cannot load card from {args.source}: {e}", file=sys.stderr)
        return 2

    if not isinstance(card, dict):
        print(f"error: {args.source} is not an AgentCard (expected a JSON object, "
              f"got {type(card).__name__})", file=sys.stderr)
        return 2

    if args.cmd == "scan":
        total, br = scan_card(card)
        print(f"card: {src}")
        print(f"total card tax: {total:,} tokens\n")
        for k, v in br.items():
            if k == "skills":
                print(f"  skills ({v['count']})       : {sum(i['tokens'] for i in v['items']):>6,} tok")
                for item in v["items"]:
                    print(f"    - {item['name'][:36]:36s} {item['tokens']:>6,} tok")
            elif k == "TOTAL":
                continue
            else:
                print(f"  {k:22s}: {v:>6,} tok")
        print(f"\n  TOTAL                    : {total:>6,} tok")
        return 0

    if args.cmd == "hop":
        m = model_task(card, args.polls, args.history_len, args.cap)
        for k, v in m.items():
            print(f"{k:22s}: {v:,}" if isinstance(v, int) else f"{k:22s}: {v}")
        return 0

    if args.cmd == "report":
        print(report(card, args.polls, args.history_len, args.price_per_mtok))
        return 0


if __name__ == "__main__":
    sys.exit(main())
