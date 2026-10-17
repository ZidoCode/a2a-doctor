#!/usr/bin/env python3
"""a2a-fleet-compare — audit a fleet of A2A Agent Cards for overlap and conflicts.

Multi-agent fleets fail in ways single-agent checks never see: two agents
declare the same skill id and the orchestrator routes to the wrong one
(A2A discussion #921), a caller assumes streaming that half the fleet lacks
(A2A#1377 — silent failure mid-orchestration), and protocol versions drift
apart across members.

a2a-fleet-compare takes N card JSON files and reports every fleet-level
violation. Offline, deterministic, zero dependencies.

Detected classes (pain anchors):
  - duplicate skill id across agents (A2A#921: wrong-agent routing)
  - near-identical skill names (ambiguous intent for orchestrators)
  - capability conflicts (streaming/push declared by some, absent in others —
    A2A#1377: silent failure mid-orchestration when a caller assumes uniformity)
  - protocol version heterogeneity across the fleet (card-drift, fleet-wide)
  - duplicate agent names or duplicate interface URLs
"""

import json
import os
import re
import sys

SEMVER_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
A2A_VERSIONS = {"0.2", "0.2.9", "0.3", "0.3.0", "1.0", "1.0.0", "1.1", "1.2"}
CAP_KEYS = ["streaming", "pushNotifications", "stateTransitionHistory"]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _skills(card):
    out = {}
    for sk in card.get("skills") or []:
        if isinstance(sk, dict) and sk.get("id"):
            out[sk["id"]] = sk
    return out


def audit(cards):
    """cards: list of (name, card_dict). Returns (findings, stats)."""
    findings = []
    stats = {"agents": len(cards), "skills": 0, "dup_skill_ids": 0,
             "near_dupes": 0, "cap_conflicts": 0}

    if len(cards) < 2:
        findings.append(("CRITICAL", "fleet needs at least 2 cards to compare"))
        return findings, stats

    # --- identity uniqueness ---
    names = {}
    urls = {}
    for cname, card in cards:
        if not isinstance(card, dict):
            findings.append(("CRITICAL", f"{cname}: card is not a JSON object"))
            continue
        n = _norm(card.get("name", ""))
        if n:
            names.setdefault(n, []).append(cname)
        for iface in ([card.get("supportedInterfaces")] if card.get("supportedInterfaces") else []) or []:
            if isinstance(iface, dict) and iface.get("url"):
                urls.setdefault(iface["url"], []).append(cname)
        if card.get("url"):
            urls.setdefault(card["url"], []).append(cname)
    for n, owners in names.items():
        if len(owners) > 1:
            findings.append(("CRITICAL",
                f"agent name '{owners[0]}' declared by {len(owners)} cards "
                f"({', '.join(owners)}) — discovery cannot disambiguate"))
    for u, owners in urls.items():
        if len(owners) > 1:
            findings.append(("CRITICAL",
                f"interface URL {u} declared by {len(owners)} cards "
                f"({', '.join(owners)}) — two agents, one endpoint"))

    # --- duplicate skill ids across agents ---
    by_id = {}
    total_skills = 0
    for cname, card in cards:
        if not isinstance(card, dict):
            continue
        sk = _skills(card)
        total_skills += len(sk)
        for sid, s in sk.items():
            by_id.setdefault(sid, []).append((cname, s))
    stats["skills"] = total_skills
    for sid, owners in sorted(by_id.items()):
        if len(owners) > 1:
            stats["dup_skill_ids"] += 1
            names_list = ", ".join(o[0] for o in owners)
            findings.append(("CRITICAL",
                f"skill id '{sid}' declared by {len(owners)} agents ({names_list}) — "
                f"orchestrators routing on skill id will pick one arbitrarily "
                f"(A2A#921 wrong-agent routing)"))

    # --- near-identical skill names (ambiguous intent) ---
    by_name = {}
    for cname, card in cards:
        if not isinstance(card, dict):
            continue
        for sid, s in _skills(card).items():
            nm = _norm(s.get("name", ""))
            if nm:
                by_name.setdefault(nm, []).append((cname, sid))
    for nm, owners in sorted(by_name.items()):
        if len(owners) > 1:
            srcs = {o[0] for o in owners}
            if len(srcs) > 1:
                stats["near_dupes"] += 1
                findings.append(("WARNING",
                    f"skill name '{nm}' used by agents {', '.join(sorted(srcs))} — "
                    f"intent-based routing cannot disambiguate (A2A#921)"))

    # --- capability conflicts (A2A#1377) ---
    for cap in CAP_KEYS:
        have = [c for c, card in cards if isinstance(card, dict)
                and (card.get("capabilities") or {}).get(cap)]
        lack = [c for c, card in cards if isinstance(card, dict)
                and isinstance(card.get("capabilities"), dict)
                and not (card.get("capabilities") or {}).get(cap)]
        if have and lack:
            stats["cap_conflicts"] += 1
            findings.append(("WARNING",
                f"capability '{cap}': {', '.join(have)} declare it, "
                f"{', '.join(lack)} do not — callers assuming fleet-wide support "
                f"will hit silent failures mid-orchestration (A2A#1377)"))

    # --- protocol version heterogeneity ---
    versions = {}
    for cname, card in cards:
        if isinstance(card, dict):
            pv = card.get("protocolVersion")
            if pv is not None:
                versions.setdefault(str(pv), []).append(cname)
    if len(versions) > 1:
        detail = "; ".join(f"{v}: {', '.join(cs)}" for v, cs in sorted(versions.items()))
        findings.append(("WARNING",
            f"fleet speaks {len(versions)} protocol versions ({detail}) — "
            f"one negotiator cannot serve all members uniformly"))
    for v, cs in versions.items():
        if SEMVER_RE.match(v) and v not in A2A_VERSIONS:
            findings.append(("CRITICAL",
                f"{', '.join(cs)}: protocolVersion '{v}' is not a known A2A version — "
                f"the whole fleet's negotiation with these members is broken"))

    return findings, stats


def report(card_paths):
    cards = []
    for p in card_paths:
        try:
            with open(p, encoding="utf-8") as f:
                cards.append((os.path.basename(p), json.load(f)))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read {p}: {e}", file=sys.stderr)
            return 2
    findings, stats = audit(cards)
    print(f"a2a-fleet-compare — {stats['agents']} agent(s), {stats['skills']} skill(s) total")
    if not findings:
        print("  ✓ fleet is coherent — no overlap, no capability conflicts, versions aligned")
        print("\n  verdict: ALL CLEAR.")
        return 0
    order = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "MEDIUM": 3, "INFO": 4}
    for s, m in sorted(findings, key=lambda f: order.get(f[0], 9)):
        print(f"  [{s:8s}] {m}")
    crit = sum(1 for s, _ in findings if s in ("CRITICAL", "HIGH"))
    warn = sum(1 for s, _ in findings if s in ("WARNING", "MEDIUM"))
    if crit:
        print(f"\n  verdict: {crit} CRITICAL — the fleet will misroute or silently fail.")
        return 2
    print(f"\n  verdict: {warn} warning(s) — review above.")
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print("usage: a2a-fleet-compare <card1.json> <card2.json> [...]", file=sys.stderr)
        return 2
    return report(argv)


if __name__ == "__main__":
    sys.exit(main())
