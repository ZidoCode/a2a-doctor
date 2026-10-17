#!/usr/bin/env python3
"""a2a-doctor — the umbrella health check for the ZidoCode A2A audit suite.

One command runs every audit in the family against recorded streams and
agent cards, and prints one verdict. Dumb math, honest numbers, zero deps.

Subcommands map 1:1 to the suite members:
  tax        (a2a-tax)          token cost of a card + polling model
  stream     (a2a-stream-tax)   token cost of a recorded stream
  reconnect  (a2a-reconnect-tax) replayed-event tax across reconnects
  artifacts  (a2a-artifact-audit) chunk gaps, dupes, post-close writes
  lifecycle  (a2a-task-state-audit) resurrection, illegal transitions
  lock       (a2a-card-lock)    pin + drift gate
  check      run the checks above against given files

Usage:
  a2a-doctor tax <card.json> [--price-per-mtok 3.0]
  a2a-doctor stream <stream.jsonl> [--price-per-mtok 3.0]
  a2a-doctor reconnect <stream.jsonl>
  a2a-doctor artifacts <stream.jsonl>
  a2a-doctor lifecycle <stream.jsonl>
  a2a-doctor lock <card.json> [lockfile]
  a2a-doctor check --card card.json --stream stream.jsonl [--lock a2a-card.lock]

`check` is the CI gate: exit 0 clean, 1 warnings, 2 critical.
"""

import argparse
import json
import sys

import a2a_tax
import a2a_stream_tax
import a2a_reconnect_tax
import a2a_artifact_audit
import a2a_task_state_audit
import a2a_context_audit

try:
    import a2a_card_lock
except ImportError:  # lock check is optional
    a2a_card_lock = None


def _load_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except OSError as e:
        return None, f"cannot read {what}: {e}"
    except json.JSONDecodeError as e:
        return None, f"{what} is not valid JSON: {e}"


def _load_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines(), None
    except OSError as e:
        return None, f"cannot read stream: {e}"


def _card_is_object(card):
    return isinstance(card, dict)


def _has_card_fields(card):
    return isinstance(card.get("name"), str) and isinstance(card.get("description"), str)


def _card_est_tokens(card):
    """Rough card token estimate (same dumb math as a2a-tax scan)."""
    return len(json.dumps(card, default=str)) // 4


def check(card, stream_lines, lock_path=None, lock_data=None, price=3.0):
    """Run every check. Returns (sections, exit_code)."""
    sections = []

    # --- card ---
    if card is None:
        sections.append(("card", "FAIL", "no card provided"))
    elif not _card_is_object(card):
        sections.append(("card", "FAIL", f"expected a JSON object, got {type(card).__name__}"))
    elif not _has_card_fields(card):
        sections.append(("card", "WARN", "missing name/description"))
    else:
        sections.append(("card", "PASS", f"{_card_est_tokens(card)} tok estimated"))

    # --- polling tax (card-based) ---
    if card is not None and _card_is_object(card):
        per_hop = _card_est_tokens(card)
        if per_hop > 2000:
            sections.append(("polling", "WARN", f"~{per_hop} tok/task — heavy card, trim fields"))
        else:
            sections.append(("polling", "PASS", f"~{per_hop} tok/task estimated"))

    # --- stream checks ---
    if stream_lines is None:
        sections.append(("stream", "FAIL", "no stream provided"))
    else:
        import json as _json
        def _parse(ls):
            out = []
            for l in ls:
                l = l.strip()
                if not l:
                    continue
                try:
                    o = _json.loads(l)
                    if isinstance(o, dict):
                        out.append(o)
                except _json.JSONDecodeError:
                    pass
            return out
        events = _parse(stream_lines)
        if not events:
            sections.append(("stream", "FAIL", "stream has no parseable events"))
        else:
            sections.append(("stream", "PASS", f"{len(events)} event(s) recorded"))

        # artifacts
        if events:
            findings, arts = a2a_artifact_audit.audit(events)
            crit = [m for s, m in findings if s == "CRITICAL"]
            warn = [m for s, m in findings if s == "WARNING"]
            if crit:
                sections.append(("artifacts", "FAIL", f"{len(crit)} critical — {crit[0]}"))
            elif warn:
                sections.append(("artifacts", "WARN", f"{len(warn)} warning(s) — {warn[0]}"))
            else:
                sections.append(("artifacts", "PASS", f"{len(arts)} artifact(s) clean"))

        # lifecycle
        if events:
            findings, tasks = a2a_task_state_audit.audit(events)
            crit = [m for s, m in findings if s == "CRITICAL"]
            warn = [m for s, m in findings if s == "WARNING"]
            if crit:
                sections.append(("lifecycle", "FAIL", f"{len(crit)} critical — {crit[0]}"))
            elif warn:
                sections.append(("lifecycle", "WARN", f"{len(warn)} warning(s) — {warn[0]}"))
            else:
                sections.append(("lifecycle", "PASS", f"{len(tasks)} task(s) legal"))

        # context correlation
        if events:
            findings, cstats = a2a_context_audit.audit(stream_lines)
            crit = [m for s, m in findings if s == "CRITICAL"]
            warn = [m for s, m in findings if s == "WARNING"]
            if crit:
                sections.append(("context", "FAIL", f"{len(crit)} critical — {crit[0]}"))
            elif warn:
                sections.append(("context", "WARN", f"{len(warn)} warning(s) — {warn[0]}"))
            else:
                sections.append(("context", "PASS", f"{cstats['tasks']} task(s), {cstats['contexts']} context(s) consistent"))

        # reconnect
        if events:
            try:
                summary = a2a_reconnect_tax.audit(events)
                reps = summary.get("replayed_events", summary.get("replayed", 0))
                if reps and reps > 0:
                    sections.append(("reconnect", "WARN", f"{reps} replayed event(s)"))
                else:
                    sections.append(("reconnect", "PASS", "no replayed events"))
            except Exception as e:
                sections.append(("reconnect", "WARN", f"could not measure: {e}"))

    # --- lock drift ---
    if lock_path is not None:
        if a2a_card_lock is None:
            sections.append(("lock", "WARN", "a2a_card_lock not importable — skipped"))
        elif card is None or not _card_is_object(card):
            sections.append(("lock", "WARN", "card unavailable — drift not checkable"))
        elif lock_data is None:
            sections.append(("lock", "FAIL", f"lock unreadable at {lock_path}"))
        else:
            try:
                findings, summary = a2a_card_lock.diff(card, lock_data)
                if not findings:
                    sections.append(("lock", "PASS", summary or "card matches pin"))
                else:
                    breaking = [m for s, m in findings if s == "CRITICAL"]
                    minor = [m for s, m in findings if s != "CRITICAL"]
                    if breaking:
                        sections.append(("lock", "FAIL", f"{len(breaking)} breaking drift — {breaking[0]}"))
                    else:
                        sections.append(("lock", "WARN", f"{len(minor)} minor drift — {minor[0]}"))
            except Exception as e:
                sections.append(("lock", "WARN", f"diff error: {e}"))

    sev_rank = {"FAIL": 2, "WARN": 1, "PASS": 0}
    code = max((sev_rank[s] for _, s, _ in sections), default=0)
    return sections, code


def print_check(sections, code):
    icons = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
    width = max(len(n) for n, _, _ in sections) if sections else 5
    print(f"a2a-doctor check — {len(sections)} section(s)\n")
    for name, sev, msg in sections:
        print(f"  {icons[sev]} {name:{width}s}  {msg}")
    print()
    crit = [n for n, s, _ in sections if s == "FAIL"]
    if crit:
        print(f"  verdict: FAIL ({', '.join(crit)})")
    elif any(s == "WARN" for _, s, _ in sections):
        print("  verdict: PASS with warnings — review above.")
    else:
        print("  verdict: ALL CLEAR.")
    return 2 if code == 2 else (1 if code == 1 else 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="a2a-doctor",
                                description="Umbrella health check for the ZidoCode A2A audit suite.")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("tax", "stream", "reconnect", "artifacts", "lifecycle", "context"):
        s = sub.add_parser(name)
        s.add_argument("file")
    s = sub.add_parser("lock")
    s.add_argument("card")
    s.add_argument("lockfile", nargs="?")
    s = sub.add_parser("check")
    s.add_argument("--card", required=True)
    s.add_argument("--stream", required=True)
    s.add_argument("--lock")
    args = p.parse_args(argv)

    if args.cmd == "tax":
        card, err = _load_json(args.file, "card")
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        if not isinstance(card, dict):
            print(f"error: expected a JSON object, got {type(card).__name__}", file=sys.stderr)
            return 2
        tok = _card_est_tokens(card)
        print(f"card: ~{tok} tok (~${tok / 1_000_000 * 3.0:.6f} per scan @ $3/MTok)")
        return 0

    if args.cmd in ("stream", "reconnect", "artifacts", "lifecycle", "context"):
        lines, err = _load_lines(args.file)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        if not any(l.strip() for l in lines):
            print(f"error: {args.file} has no events", file=sys.stderr)
            return 2
        if args.cmd == "context":
            findings, cstats = a2a_context_audit.audit(lines)
            for s, m in findings:
                print(f"  [{s:8s}] {m}")
            if not findings:
                print("clean — every task/context correlation is consistent.")
            crit = sum(1 for s, _ in findings if s == "CRITICAL")
            return 2 if crit else 0
        if args.cmd == "stream":
            events, notes = a2a_stream_tax.load_stream(args.file)
            m = a2a_stream_tax.measure(events)
            print(json.dumps(m, indent=2, default=str))
            return 0
        elif args.cmd == "reconnect":
            events, notes = a2a_stream_tax.load_stream(args.file)
            summary = a2a_reconnect_tax.audit(events)
            print(json.dumps(summary, indent=2, default=str))
            return 0
        elif args.cmd == "artifacts":
            evs = [o for o in (json.loads(l) for l in lines if l.strip())
                   if isinstance(o, dict) and isinstance((o.get("result") or {}), dict)]
            out = a2a_artifact_audit.report(evs)
            if isinstance(out, str):
                print(out)
                crit = "CRITICAL" in out
            else:
                print(out)
                crit = bool(out)
            return 2 if crit else 0
        else:
            evs = [o for o in (json.loads(l) for l in lines if l.strip())
                   if isinstance(o, dict) and isinstance((o.get("result") or {}), dict)]
            code = a2a_task_state_audit.report(evs)
            return 2 if (code or 0) >= 2 else 0

    if args.cmd == "lock":
        card, err = _load_json(args.card, "card")
        if err or not isinstance(card, dict):
            print(f"error: {err or 'card is not an object'}", file=sys.stderr)
            return 2
        lp = args.lockfile or "a2a-card.lock"
        lock_data, lerr = _load_json(lp, "lock")
        if lerr:
            print(f"error: {lerr}", file=sys.stderr)
            return 2
        if a2a_card_lock is None:
            print("error: a2a_card_lock not available", file=sys.stderr)
            return 2
        try:
            findings, summary = a2a_card_lock.diff(card, lock_data)
        except Exception as e:
            print(f"error: diff failed: {e}", file=sys.stderr)
            return 2
        if not findings:
            print(summary or "card matches pin — no drift.")
            return 0
        for s, m in findings:
            print(f"  [{s}] {m}")
        return 2

    # check
    card, cerr = _load_json(args.card, "card")
    lines, serr = _load_lines(args.stream)
    lock_data, lock_path = None, None
    if args.lock:
        lock_data, lerr = _load_json(args.lock, "lock")
        if lerr:
            print(f"error: {lerr}", file=sys.stderr)
            return 2
        lock_path = args.lock
    if cerr and card is None and cerr.startswith("cannot read"):
        print(f"error: {cerr}", file=sys.stderr)
        return 2
    if serr and lines is None and serr.startswith("cannot read"):
        print(f"error: {serr}", file=sys.stderr)
        return 2
    if card is not None and not isinstance(card, dict):
        print(f"error: card is not a JSON object (got {type(card).__name__})", file=sys.stderr)
        return 2
    sections, code = check(card, lines, lock_path=lock_path, lock_data=lock_data)
    return print_check(sections, code)


if __name__ == "__main__":
    sys.exit(main())
