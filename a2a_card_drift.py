#!/usr/bin/env python3
"""a2a-card-drift — audit Agent Card protocol version drift + snapshot diff for breaking changes.

Two modes:
  check <card.json>                  sanity-audit one card (version format, interface consistency)
  diff   <old.json> <new.json>       diff two card snapshots, flag breaking changes

Offline, deterministic, zero dependencies. Exit codes: 0 clean, 1 warnings, 2 critical.
Pain: LiteLLM#30795 (card says 1.0, wire is 0.3), Agenstry crawl (41 cards put MCP spec
dates in protocolVersion, 9 declare versions that never existed), Apicurio#9455
(compatibility checker misses card-level version bumps and dropped interface versions).
"""

import json
import re
import sys

A2A_VERSIONS = {"0.2", "0.2.9", "0.3", "0.3.0", "1.0", "1.0.0", "1.1", "1.2"}
SEMVER_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
MCP_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WIRE_METHODS_03 = {"message/send", "message/stream", "tasks/get", "tasks/cancel"}
WIRE_METHODS_10 = {"SendMessage", "SendStreamingMessage", "GetTask", "CancelTask"}


def _iface_version(iface):
    return iface.get("protocolVersion")


def check_card(card):
    """Audit one card. Returns list of (severity, message)."""
    findings = []
    if not isinstance(card, dict):
        return [("critical", "card is not a JSON object")]

    pv = card.get("protocolVersion")

    # 1. protocolVersion format sanity
    if pv is None:
        findings.append(("critical", "protocolVersion missing — clients cannot negotiate"))
    elif MCP_DATE_RE.match(str(pv)):
        findings.append(("critical",
            f"protocolVersion '{pv}' is an MCP spec revision date, not an A2A version "
            f"(Agenstry: 41 public cards have this bug)"))
    elif not SEMVER_RE.match(str(pv)):
        findings.append(("critical",
            f"protocolVersion '{pv}' is not an A2A semver (known: 0.2/0.3/1.0)"))
    elif str(pv) not in A2A_VERSIONS:
        findings.append(("critical",
            f"protocolVersion '{pv}' is valid semver but not a known A2A version "
            f"(Agenstry: public cards declare '1.1', '2.0', '5.0', '9.2.9' — none exist)"))

    ifaces = card.get("supportedInterfaces")
    if pv and SEMVER_RE.match(str(pv)) and str(pv).startswith("1.") and ifaces is None:
        # 1.0 card shape requires supportedInterfaces[]
        if card.get("url") or card.get("preferredTransport"):
            findings.append(("critical",
                "card claims protocolVersion 1.0 but uses pre-1.0 shape (url/preferredTransport, "
                "no supportedInterfaces[]) — 1.0 clients will fail discovery"))

    if isinstance(ifaces, list):
        iface_versions = []
        for i, iface in enumerate(ifaces):
            if not isinstance(iface, dict):
                findings.append(("warning", f"supportedInterfaces[{i}] is not an object"))
                continue
            iv = _iface_version(iface)
            iface_versions.append(iv)
            if iv is None:
                findings.append(("critical",
                    f"supportedInterfaces[{i}] ({iface.get('url', '?')}) drops protocolVersion — "
                    f"clients lose the version guarantee (Apicurio#9455 class)"))
            elif MCP_DATE_RE.match(str(iv)):
                findings.append(("critical",
                    f"supportedInterfaces[{i}] protocolVersion '{iv}' is an MCP date, not A2A"))
            elif SEMVER_RE.match(str(iv)) and pv and SEMVER_RE.match(str(pv)):
                # card-level vs interface-level mismatch (LiteLLM#30795 class)
                if iv.split(".")[0] != str(pv).split(".")[0]:
                    findings.append(("critical",
                        f"card protocolVersion '{pv}' but interface {i} speaks '{iv}' — "
                        f"version drift: card promises what the wire does not deliver (LiteLLM#30795 class)"))
    # security scheme sanity
    for name, scheme in (card.get("securitySchemes") or {}).items():
        if isinstance(scheme, dict) and scheme.get("type") not in (
                "http", "apiKey", "oauth2", "openIdConnect", "mTLS"):
            findings.append(("warning",
                f"securitySchemes['{name}'].type '{scheme.get('type')}' is not an OpenAPI type"))

    # capabilities referenced but not declared
    caps = card.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        findings.append(("warning", "capabilities is not an object"))

    return findings


def _scheme_sig(schemes):
    out = {}
    for name, scheme in (schemes or {}).items():
        if isinstance(scheme, dict):
            out[name] = (scheme.get("type"), scheme.get("scheme"),
                         scheme.get("location"), scheme.get("name"))
    return out


def diff_cards(old, new):
    """Diff two card snapshots. Returns list of (severity, message)."""
    findings = []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [("critical", "snapshot is not a JSON object")]

    ov, nv = old.get("protocolVersion"), new.get("protocolVersion")
    if ov and nv and str(ov) != str(nv):
        findings.append(("critical",
            f"top-level protocolVersion changed '{ov}' -> '{nv}' — breaking for pinned clients "
            f"(Apicurio#9455 case 2: checkers miss this)"))

    old_if = {json.dumps({k: v for k, v in i.items() if k in ("url", "protocolBinding")}, sort_keys=True): i
              for i in (old.get("supportedInterfaces") or []) if isinstance(i, dict)}
    new_if = {json.dumps({k: v for k, v in i.items() if k in ("url", "protocolBinding")}, sort_keys=True): i
              for i in (new.get("supportedInterfaces") or []) if isinstance(i, dict)}

    for key, oiface in old_if.items():
        if key not in new_if:
            findings.append(("warning",
                f"interface {oiface.get('url', '?')} removed"))
            continue
        niface = new_if[key]
        ov2, nv2 = _iface_version(oiface), _iface_version(niface)
        if ov2 and not nv2:
            findings.append(("critical",
                f"interface {niface.get('url', '?')} dropped protocolVersion '{ov2}' "
                f"(Apicurio#9455 case 3)"))
        elif ov2 and nv2 and str(ov2) != str(nv2):
            findings.append(("critical",
                f"interface {niface.get('url', '?')} protocolVersion '{ov2}' -> '{nv2}'"))

    # security scheme definition drift (Apicurio#9455 case 1)
    o_sig, n_sig = _scheme_sig(old.get("securitySchemes")), _scheme_sig(new.get("securitySchemes"))
    for name, osig in o_sig.items():
        if name in n_sig and n_sig[name] != osig:
            findings.append(("critical",
                f"securitySchemes['{name}'] definition changed {osig} -> {n_sig[name]} — "
                f"same name, different auth = silent break"))

    # capability flipped off
    oc, nc = old.get("capabilities") or {}, new.get("capabilities") or {}
    for k in set(oc) & set(nc):
        if oc[k] is True and nc[k] is not True:
            findings.append(("warning", f"capability '{k}' flipped off ({oc[k]} -> {nc[k]})"))

    return findings


def _report(title, findings):
    order = {"critical": 0, "warning": 1, "info": 2}
    findings = sorted(findings, key=lambda f: order.get(f[0], 3))
    print(f"a2a-card-drift {title}")
    if not findings:
        print("  ✓ no drift detected")
        print("\n  verdict: ALL CLEAR.")
        return 0
    for sev, msg in findings:
        print(f"  [{sev.upper():8s}] {msg}")
    crit = sum(1 for s, _ in findings if s == "critical")
    warn = sum(1 for s, _ in findings if s == "warning")
    if crit:
        print(f"\n  verdict: {crit} CRITICAL — card drift will break clients at negotiation.")
        return 2
    print(f"\n  verdict: {warn} warning(s) — review above.")
    return 1


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        sys.exit(2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "check" and len(argv) == 2:
        return _report(f"check — {argv[1]}", check_card(_load(argv[1])))
    if argv[0] == "diff" and len(argv) == 3:
        return _report(f"diff — {argv[1]} vs {argv[2]}", diff_cards(_load(argv[1]), _load(argv[2])))
    print("usage: a2a-card-drift check <card.json> | diff <old.json> <new.json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
