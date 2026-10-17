#!/usr/bin/env python3
"""a2a-webhook-audit — audit A2A push-notification webhook URLs for SSRF risk.

A2A push-notification configs carry a client-supplied webhook URL that the
agent SERVER will call back. If the server does not validate it, an attacker
points the webhook at 169.254.169.254 (cloud metadata), localhost admin
endpoints, or internal services. a2a-go#373: URLs are NOT SSRF-validated.
a2a-python#786/#1023: protection is still being built.

This tool audits recorded push-notification configs (from a stream, a card,
or a config file) — offline, no requests are made. Dumb math, honest numbers,
zero dependencies. MIT license.

Usage:
  a2a-webhook-audit audit  <file.json|stream.jsonl>     exit 2 on HIGH risk
  a2a-webhook-audit report <file.json|stream.jsonl>     always exit 0
"""

import argparse
import ipaddress
import json
import re
import sys
from urllib.parse import urlparse

CRIT, WARN, INFO = "HIGH", "MEDIUM", "INFO"

# hostnames that are almost always dangerous callback targets
DANGEROUS_HOSTS = {
    "metadata.google.internal",       # GCP metadata
    "metadata.goog",
    "instance-data",                   # AWS legacy
    "169.254.169.254",
}
DANGEROUS_SUFFIXES = (".internal", ".local", ".localhost", ".zone")
RISKY_PORTS = {22, 23, 25, 445, 3306, 5432, 6379, 9200, 2375, 2376, 8500}


def _walk_urls(obj):
    """Yield every string that looks like an http(s) URL, with its path."""
    if isinstance(obj, str):
        if obj.startswith(("http://", "https://")):
            yield obj, ()
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            for u, path in _walk_urls(v):
                yield u, path + (k,)
    elif isinstance(obj, list):
        for v in obj:
            for u, path in _walk_urls(v):
                yield u, path


def _is_private_ip(host):
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


def audit_url(url):
    """Return list of (severity, message) for one webhook URL."""
    findings = []
    try:
        p = urlparse(url)
    except ValueError as e:
        return [(CRIT, f"unparseable URL: {e}")]
    host = (p.hostname or "").lower()

    # credentials in URL
    if p.username or p.password:
        findings.append((CRIT, "credentials embedded in URL (user:pass@host) — "
                               "leaks into logs and referrers"))

    # raw private/loopback/metadata IP
    if host and _is_private_ip(host):
        findings.append((CRIT, f"webhook targets private/loopback/metadata IP {host} "
                               "(SSRF — cloud metadata, internal services)"))
    if host in DANGEROUS_HOSTS:
        findings.append((CRIT, f"webhook targets known metadata endpoint {host}"))

    # internal-suffix hostnames
    if any(host.endswith(s) for s in DANGEROUS_SUFFIXES):
        findings.append((CRIT, f"webhook targets internal hostname {host} "
                               "(resolves inside the server's network)"))

    # plain http
    if p.scheme == "http":
        if host in ("localhost", "127.0.0.1", "::1"):
            findings.append((INFO, "plain http to localhost — acceptable for local dev only"))
        else:
            findings.append((CRIT, "plain http webhook — config and tokens travel in cleartext"))

    # risky ports
    if p.port and p.port in RISKY_PORTS:
        findings.append((CRIT, f"webhook targets risky port {p.port} "
                               "(db/ssh/redis/etc — classic SSRF pivot)"))

    # no port but scheme default on odd host shapes
    if not host:
        findings.append((CRIT, "webhook URL has no hostname"))

    # IDN / homograph hint
    if host and "xn--" in host:
        findings.append((WARN, "punycode hostname (xn--) — verify it is not a homograph attack"))

    if not findings:
        findings.append((INFO, "no obvious SSRF indicators (offline check — DNS still resolves "
                               "at callback time, see a2a-go#373)"))
    return findings


def _extract_push_configs(lines):
    """Yield (label, config dict) from a JSONL stream or a plain JSON file."""
    text = "\n".join(l for l in lines if l.strip())
    # whole file may be one JSON doc
    try:
        doc = json.loads(text)
        if isinstance(doc, dict):
            yield "config", doc
            return
        if isinstance(doc, list):
            for i, v in enumerate(doc):
                if isinstance(v, dict):
                    yield f"item[{i}]", v
            return
    except json.JSONDecodeError:
        pass
    # else treat as JSONL
    for i, l in enumerate(lines, 1):
        l = l.strip()
        if not l:
            continue
        try:
            o = json.loads(l)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            res = o.get("result") or o
            label = f"line {i}"
            tid = res.get("taskId") if isinstance(res, dict) else None
            if tid:
                label += f" (task {tid!r})"
            yield label, o


def audit(lines):
    findings, audited = [], 0
    for label, cfg in _extract_push_configs(lines):
        for url, path in _walk_urls(cfg):
            audited += 1
            for sev, msg in audit_url(url):
                loc = ".".join(path) if path else "url"
                findings.append((sev, f"{label} [{loc}] {url[:80]!r}: {msg}"))
    if audited == 0:
        findings.append((WARN, "no webhook URLs found in input"))
    return findings, {"urls_audited": audited}


def report(lines):
    findings, stats = audit(lines)
    crit = sum(1 for s, _ in findings if s == CRIT)
    warn = sum(1 for s, _ in findings if s == WARN)
    print(f"a2a-webhook-audit report — {stats['urls_audited']} URL(s) audited, "
          f"{len(findings)} finding(s)\n")
    for s, m in findings:
        print(f"  [{s:6s}] {m}")
    print()
    if crit:
        print(f"  verdict: {crit} HIGH — webhook URLs can be abused for SSRF "
              "(a2a-go#373: servers do not validate).")
    elif warn:
        print("  verdict: no high-risk URLs — review medium findings.")
    else:
        print("  verdict: clean — no offline SSRF indicators.")
    return 2 if crit else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="a2a-webhook-audit",
                                description="Audit A2A push-notification webhook URLs for SSRF risk (offline).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "report"):
        s = sub.add_parser(name)
        s.add_argument("file")
    args = p.parse_args(argv)
    try:
        with open(args.file, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"error: cannot read file: {e}", file=sys.stderr)
        return 2
    if not any(l.strip() for l in lines):
        print(f"error: {args.file} is empty", file=sys.stderr)
        return 2
    code = report(lines)
    return code if args.cmd == "audit" else 0


if __name__ == "__main__":
    sys.exit(main())
