#!/usr/bin/env python3
"""a2a-error-audit — audit error envelopes in a recorded A2A stream.

Errors are where A2A implementations fall apart: the same failure surfaces as
three different shapes across JSON-RPC, gRPC and REST transports, clients
swallow streaming errors that arrive as HTTP-200 bodies (a2a-rs#146), failed
tasks leak into conversation history as ordinary content (adk-python#4309),
and SDKs collapse distinct JSON-RPC codes into one exception, destroying the
retry signal.

a2a-error-audit replays a recorded `message/stream` session (JSONL) and
reports every error-handling violation. Offline, deterministic, zero deps.

Detected classes (pain anchors):
  - malformed JSON-RPC error (no message / bad code / code not negative)
  - error.data not an array, or detail objects missing @type (spec 9.5)
  - failed task with no status message — failure without a signal (ADK#4309)
  - wrapped task envelope in GetTask/CancelTask result (Locus bug class)
  - response with neither result nor error — the silent void (a2a-rs#146 class)
"""

import json
import sys

# JSON-RPC 2.0 + A2A spec error codes (section 5.4 / 9.5)
KNOWN_CODES = {
    -32700, -32600, -32601, -32602, -32603,          # JSON-RPC 2.0 reserved
    -32000, -32001, -32002, -32003, -32004, -32005,  # A2A server range
    -32006, -32007, -32008, -32009,
}
TERMINAL_FAILED = {"failed"}
WIRE_TASK_METHODS = {"GetTask", "CancelTask", "tasks/get", "tasks/cancel"}


def audit(lines):
    """lines: JSONL strings. Returns (findings, stats).
    findings: list of (severity, message); severity in CRITICAL / WARNING / INFO."""
    findings = []
    stats = {"responses": 0, "errors": 0, "failed_tasks": 0, "checked": 0}

    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        stats["checked"] += 1

        method = obj.get("method") or ""

        # --- response shape: result XOR error ---
        has_result = "result" in obj and obj["result"] is not None
        has_error = "error" in obj and obj["error"] is not None
        if method:
            # request/notification: no findings unless params broken
            continue
        stats["responses"] += 1
        if not has_result and not has_error:
            findings.append(("CRITICAL",
                f"line {lineno}: response has neither result nor error — the silent void "
                f"(a2a-rs#146 class: clients cannot tell failure from success)"))
            continue
        if has_result and has_error:
            findings.append(("CRITICAL",
                f"line {lineno}: response has BOTH result and error — ambiguous envelope"))

        # --- wrapped task envelope (Locus bug class): result is ONLY {"task": ...} ---
        result = obj.get("result")
        if isinstance(result, dict) and has_result:
            inner = result.get("task")
            if (isinstance(inner, dict) and "task" in result
                    and "id" not in result and "status" not in result
                    and "kind" not in result
                    and isinstance(inner.get("status"), dict)):
                findings.append(("WARNING",
                    f"line {lineno}: result is a bare {{\"task\": ...}} envelope — valid oneof "
                    f"for SendMessage responses, but a parse failure for GetTask/CancelTask "
                    f"(Locus bug class) — verify which method this answers"))

        # --- error envelope checks ---
        if has_error:
            stats["errors"] += 1
            err = obj["error"]
            if not isinstance(err, dict):
                findings.append(("CRITICAL",
                    f"line {lineno}: error is {type(err).__name__}, not an object"))
                continue
            code = err.get("code")
            if code is None:
                findings.append(("CRITICAL", f"line {lineno}: error missing code"))
            elif not isinstance(code, int) or isinstance(code, bool):
                findings.append(("CRITICAL",
                    f"line {lineno}: error code {code!r} is not an integer"))
            elif code >= 0:
                findings.append(("CRITICAL",
                    f"line {lineno}: error code {code} is not negative — JSON-RPC codes "
                    f"must be negative (spec 9.5)"))
            elif code not in KNOWN_CODES and not (-32099 <= code <= -32000):
                findings.append(("WARNING",
                    f"line {lineno}: error code {code} is outside the JSON-RPC and A2A "
                    f"server ranges — clients will not recognize it"))
            msg = err.get("message")
            if msg is None or (isinstance(msg, str) and not msg.strip()):
                findings.append(("CRITICAL",
                    f"line {lineno}: error {code} has no message — the failure is "
                    f"unexplainable to the caller"))
            data = err.get("data")
            if data is not None:
                if not isinstance(data, list):
                    findings.append(("WARNING",
                        f"line {lineno}: error.data is {type(data).__name__}, not an "
                        f"array — spec 9.5 requires a detail array"))
                else:
                    for i, d in enumerate(data):
                        if isinstance(d, dict) and "@type" not in d:
                            findings.append(("WARNING",
                                f"line {lineno}: error.data[{i}] missing @type — SDKs "
                                f"drop untyped details, the retry signal dies here"))

        # --- failed task without a signal (ADK#4309 class) ---
        result = obj.get("result")
        if isinstance(result, dict):
            res = result.get("result") if isinstance(result.get("result"), dict) else result
            status = res.get("status")
            if isinstance(status, dict) and status.get("state") in TERMINAL_FAILED:
                stats["failed_tasks"] += 1
                sm = status.get("message")
                has_msg = isinstance(sm, dict) and (
                    sm.get("parts") or sm.get("content"))
                if not has_msg:
                    findings.append(("CRITICAL",
                        f"line {lineno}: task failed with no status message — callers "
                        f"cannot classify retry vs escalate (ADK#4309 class)"))

    return findings, stats


def report(lines):
    findings, stats = audit(lines)
    print(f"a2a-error-audit — {stats['checked']} message(s), "
          f"{stats['responses']} response(s), {stats['errors']} error envelope(s), "
          f"{stats['failed_tasks']} failed task(s)")
    if not findings:
        print("  ✓ every error envelope is well-formed and every failure carries its signal")
        print("\n  verdict: ALL CLEAR.")
        return 0
    order = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "MEDIUM": 3, "INFO": 4}
    for s, m in sorted(findings, key=lambda f: order.get(f[0], 9)):
        print(f"  [{s:8s}] {m}")
    crit = sum(1 for s, _ in findings if s in ("CRITICAL", "HIGH"))
    warn = sum(1 for s, _ in findings if s in ("WARNING", "MEDIUM"))
    if crit:
        print(f"\n  verdict: {crit} CRITICAL — error handling will lose failures in production.")
        return 2
    print(f"\n  verdict: {warn} warning(s) — review above.")
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    try:
        with open(argv[0], encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"error: cannot read stream: {e}", file=sys.stderr)
        return 2
    return report(lines)


if __name__ == "__main__":
    sys.exit(main())
