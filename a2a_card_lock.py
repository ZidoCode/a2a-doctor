#!/usr/bin/env python3
"""a2a-card-lock — pin A2A Agent Cards, catch silent drift in CI.

An Agent Card is a trust anchor: you fetch it once, verify it, and build a
peering on top. But nothing stops the remote agent from quietly changing the
card tomorrow — a new skill with a different description, a rewritten URL, a
dropped auth scheme, a loosened security block. mcp-lock proved the pattern
for MCP tool chains; agent cards are the same attack surface and nobody pins
them.

a2a-card-lock hashes a card into a lockfile, diffs live cards against it,
and exits non-zero on breaking changes so CI catches drift before your
agents obey it.

Usage:
  python3 a2a_card_lock.py pin <card-url-or-file> [--out a2a-card.lock]
  python3 a2a_card_lock.py diff <card-url-or-file> [--lock a2a-card.lock]
  python3 a2a_card_lock.py report <card-url-or-file> [--lock a2a-card.lock]

Zero dependencies. Python 3.8+.
"""

import argparse
import hashlib
import json
import sys

from a2a_tax import est_tokens, fetch_card

BREAKING = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"


# ---------------------------------------------------------------- hashing

def canonical(card):
    """Deterministic serialization: sort keys recursively, compact separators.
    Two semantically identical cards must hash identically."""
    return json.dumps(card, sort_keys=True, separators=(",", ":"))


def card_hash(card):
    return hashlib.sha256(canonical(card).encode("utf-8")).hexdigest()


def field_hashes(card):
    """Per-field sha256, so the diff can name exactly what moved."""
    out = {}
    for k, v in card.items():
        out[k] = hashlib.sha256(json.dumps(v, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return out


def skill_map(card):
    """skill id -> {name, description-hash, tags-hash}"""
    out = {}
    for s in card.get("skills") or []:
        sid = s.get("id", s.get("name", "?"))
        out[sid] = {
            "name": s.get("name", ""),
            "desc_hash": hashlib.sha256((s.get("description") or "").encode()).hexdigest()[:16],
            "tags": sorted(s.get("tags") or []),
        }
    return out


# ---------------------------------------------------------------- pin

def pin(card):
    """Build a lockfile dict from a live card."""
    return {
        "lock_version": 1,
        "card_hash": card_hash(card),
        "protocolVersion": card.get("protocolVersion", ""),
        "fields": field_hashes(card),
        "skills": skill_map(card),
        "pinned_at": "",  # intentionally no timestamp: same card -> same lockfile
    }


def save_lock(lock, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2, sort_keys=True)
        f.write("\n")


def load_lock(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- diff

def diff(card, lock):
    """Compare a live card against a pinned lock. Returns (findings, summary).
    Breaking: card hash, protocolVersion, url/auth/security, skill removal,
    skill description changes. Warnings: skill additions, new fields."""
    findings = []
    live = pin(card)

    if live["card_hash"] == lock.get("card_hash"):
        return [], "card unchanged — hash matches"

    # protocolVersion
    if live["protocolVersion"] != lock.get("protocolVersion"):
        findings.append((BREAKING,
                         f"protocolVersion changed: {lock.get('protocolVersion')!r} -> {live['protocolVersion']!r}"))

    # per-field: url / auth / security are breaking on any change
    for key in ("url", "preferredTransport", "security", "securitySchemes", "provider"):
        old = (lock.get("fields") or {}).get(key)
        new = live["fields"].get(key)
        if old and new and old != new:
            findings.append((BREAKING, f"{key} changed (was pinned, now differs)"))

    # skills
    old_skills = lock.get("skills") or {}
    new_skills = live["skills"]
    for sid in sorted(set(old_skills) - set(new_skills)):
        findings.append((BREAKING, f"skill removed: {sid}"))
    for sid in sorted(set(new_skills) - set(old_skills)):
        findings.append((WARNING, f"skill added: {sid} — review its description before trusting"))
    for sid in sorted(set(old_skills) & set(new_skills)):
        if old_skills[sid]["desc_hash"] != new_skills[sid]["desc_hash"]:
            findings.append((BREAKING,
                             f"skill description changed: {sid} ({old_skills[sid]['name']!r})"))
        elif old_skills[sid]["tags"] != new_skills[sid]["tags"]:
            findings.append((INFO, f"skill tags changed: {sid}"))

    # other fields (informational)
    for k in sorted(set(live["fields"]) - set(lock.get("fields") or {})):
        findings.append((INFO, f"new field: {k}"))

    if not findings:
        findings.append((INFO, "card changed but only in untracked fields"))
    return findings, None


def classify_severity(findings):
    for sev, _ in findings:
        if sev == BREAKING:
            return 2
    for sev, _ in findings:
        if sev == WARNING:
            return 1
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2a-card-lock",
                                 description="Pin A2A Agent Cards, catch silent drift in CI.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pin = sub.add_parser("pin", help="pin a card into a lockfile")
    p_pin.add_argument("source")
    p_pin.add_argument("--out", default="a2a-card.lock")

    p_diff = sub.add_parser("diff", help="compare a live card against a lockfile")
    p_diff.add_argument("source")
    p_diff.add_argument("--lock", default="a2a-card.lock")

    p_rep = sub.add_parser("report", help="human-readable drift report")
    p_rep.add_argument("source")
    p_rep.add_argument("--lock", default="a2a-card.lock")

    args = ap.parse_args(argv)

    try:
        card, src = fetch_card(args.source)
    except Exception as e:
        print(f"error: cannot load card from {args.source}: {e}", file=sys.stderr)
        return 2
    if not isinstance(card, dict):
        print(f"error: {args.source} is not an AgentCard (expected a JSON object, "
              f"got {type(card).__name__})", file=sys.stderr)
        return 2

    if args.cmd == "pin":
        lock = pin(card)
        save_lock(lock, args.out)
        print(f"pinned {src}")
        print(f"  card hash : {lock['card_hash'][:32]}…")
        print(f"  skills    : {len(lock['skills'])}")
        print(f"  lockfile  : {args.out}")
        return 0

    # diff / report need a lockfile
    try:
        lock = load_lock(args.lock)
    except OSError as e:
        print(f"error: cannot read lockfile {args.lock}: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: lockfile {args.lock} is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(lock, dict) or "card_hash" not in lock:
        print(f"error: {args.lock} is not an a2a-card-lock lockfile", file=sys.stderr)
        return 2

    findings, unchanged = diff(card, lock)
    if unchanged:
        print(unchanged)
        return 0

    sev_max = classify_severity(findings)
    if args.cmd == "diff":
        icon = {BREAKING: "CRITICAL", WARNING: "WARNING", INFO: "INFO"}
        for sev, msg in findings:
            print(f"  {icon[sev]:8s}  {msg}")
        return 2 if sev_max == 2 else (1 if sev_max == 1 else 0)

    # report
    def usd(_):  # no pricing here; report is qualitative
        return ""
    print(f"a2a-card-lock report — {src}")
    print(f"  pinned hash : {lock.get('card_hash', '')[:32]}…")
    print(f"  live hash   : {card_hash(card)[:32]}…")
    print(f"  skills now  : {len(card.get('skills') or [])} (pinned: {len(lock.get('skills') or {})})")
    print()
    if not findings:
        print("  no drift detected")
        return 0
    counts = {s: sum(1 for sv, _ in findings if sv == s) for s in (BREAKING, WARNING, INFO)}
    print(f"  {counts[BREAKING]} breaking, {counts[WARNING]} warning, {counts[INFO]} info")
    for sev, msg in findings:
        print(f"  [{sev:8s}] {msg}")
    print()
    if counts[BREAKING]:
        print("  verdict: BREAKING — re-verify this agent out-of-band before your next task.")
    elif counts[WARNING]:
        print("  verdict: CHANGED — review new skills before trusting them.")
    else:
        print("  verdict: COSMETIC — safe to re-pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
