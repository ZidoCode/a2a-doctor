"""Tests for a2a-error-audit — deterministic, no network."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_error_audit as ea


def resp(result=None, error=None, id=1):
    r = {"jsonrpc": "2.0", "id": id}
    if result is not None:
        r["result"] = result
    if error is not None:
        r["error"] = error
    return r


def lines(*objs):
    return [json.dumps(o) + "\n" for o in objs]


def failed_task(state="failed", msg=None):
    status = {"state": state, "timestamp": "2026-10-17T00:00:00Z"}
    if msg is not None:
        status["message"] = msg
    return {"kind": "status-update", "taskId": "t1", "contextId": "c1", "final": True,
            "status": status}


class EnvelopeTests(unittest.TestCase):
    def test_clean_result(self):
        f, s = ea.audit(lines(resp(result={"taskId": "t1"})))
        self.assertEqual(f, [])

    def test_silent_void(self):
        f, _ = ea.audit(lines({"jsonrpc": "2.0", "id": 1}))
        self.assertEqual(f[0][0], "CRITICAL")
        self.assertIn("silent void", f[0][1])

    def test_both_result_and_error(self):
        f, _ = ea.audit(lines(resp(result={"x": 1}, error={"code": -32603, "message": "m"})))
        self.assertTrue(any("BOTH" in m for _, m in f))

    def test_error_missing_code(self):
        f, _ = ea.audit(lines(resp(error={"message": "m"})))
        self.assertTrue(any("missing code" in m for _, m in f))

    def test_error_code_not_int(self):
        f, _ = ea.audit(lines(resp(error={"code": "-32603", "message": "m"})))
        self.assertTrue(any("not an integer" in m for _, m in f))

    def test_error_code_positive(self):
        f, _ = ea.audit(lines(resp(error={"code": 42, "message": "m"})))
        self.assertTrue(any("not negative" in m for _, m in f))

    def test_error_code_unknown_range(self):
        f, _ = ea.audit(lines(resp(error={"code": -45000, "message": "m"})))
        self.assertEqual(f[0][0], "WARNING")

    def test_error_no_message(self):
        f, _ = ea.audit(lines(resp(error={"code": -32603})))
        self.assertTrue(any("no message" in m for _, m in f))

    def test_error_data_not_array(self):
        f, _ = ea.audit(lines(resp(error={"code": -32000, "message": "m", "data": {"x": 1}})))
        self.assertTrue(any("not an" in m and "array" in m for _, m in f))

    def test_error_data_missing_attype(self):
        data = [{"reason": "X", "domain": "d"}]
        f, _ = ea.audit(lines(resp(error={"code": -32000, "message": "m", "data": data})))
        self.assertTrue(any("missing @type" in m for _, m in f))

    def test_error_data_typed_ok(self):
        data = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "X"}]
        f, _ = ea.audit(lines(resp(error={"code": -32000, "message": "m", "data": data})))
        self.assertEqual(f, [])

    def test_error_not_object(self):
        f, _ = ea.audit(lines(resp(error="boom")))
        self.assertTrue(any("not an object" in m for _, m in f))


class FailedTaskTests(unittest.TestCase):
    def test_failed_no_message(self):
        f, s = ea.audit(lines(resp(result=failed_task())))
        self.assertEqual(s["failed_tasks"], 1)
        self.assertTrue(any("no status message" in m for _, m in f))

    def test_failed_with_message(self):
        msg = {"role": "agent", "parts": [{"kind": "text", "text": "rate limited"}]}
        f, s = ea.audit(lines(resp(result=failed_task(msg=msg))))
        self.assertEqual(s["failed_tasks"], 1)
        self.assertEqual(f, [])

    def test_completed_ignored(self):
        f, s = ea.audit(lines(resp(result=failed_task(state="completed"))))
        self.assertEqual(s["failed_tasks"], 0)
        self.assertEqual(f, [])


class WrappedTaskTests(unittest.TestCase):
    def test_wrapped_envelope_flagged(self):
        # {"task": ...} alone: WARNING (could be valid SendMessage oneof OR Locus GetTask bug)
        r = resp(result={"task": {"id": "t1", "contextId": "c1", "status": {"state": "completed"}}})
        f, _ = ea.audit(lines(r))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "WARNING")
        self.assertIn("Locus", f[0][1])

    def test_single_result_no_wrap_warning(self):
        # proper task result: no warning
        r = resp(result=failed_task(state="completed"))
        f, _ = ea.audit(lines(r))
        self.assertEqual(f, [])

    def test_no_double_findings(self):
        r = resp(result={"task": {"id": "t1", "contextId": "c1", "status": {"state": "completed"}}})
        f, _ = ea.audit(lines(r))
        self.assertEqual(len(f), 1)


class MiscTests(unittest.TestCase):
    def test_requests_skipped(self):
        f, s = ea.audit(lines({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}))
        self.assertEqual(f, [])
        self.assertEqual(s["responses"], 0)

    def test_junk_lines_ignored(self):
        f, _ = ea.audit(["not json\n", "[1,2]\n", "null\n", "\n"])
        self.assertEqual(f, [])

    def test_report_clean_exit0(self):
        self.assertEqual(ea.report(lines(resp(result={"taskId": "t1"}))), 0)

    def test_report_dirty_exit2(self):
        self.assertEqual(ea.report(lines({"jsonrpc": "2.0", "id": 1})), 2)

    def test_cli_missing_file(self):
        # OSError in __main__ guard; called as module function returns 2 via sys.exit wrapper
        try:
            rc = ea.main(["/nope.json"])
        except SystemExit as e:
            rc = e.code
        self.assertEqual(rc, 2)

    def test_cli_clean(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_err.jsonl")
        with open(p, "w") as f:
            f.writelines(lines(resp(result={"taskId": "t1"})))
        try:
            self.assertEqual(ea.main([p]), 0)
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main()
