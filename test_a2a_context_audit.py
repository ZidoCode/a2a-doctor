"""Tests for a2a-context-audit — deterministic, no network."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_context_audit as ca


def su(tid, cid, state="working"):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "status-update", "taskId": tid, "contextId": cid,
        "final": state in ("completed", "failed"),
        "status": {"state": state}}}


def lines(*events):
    return [json.dumps(e) + "\n" for e in events]


def sev(findings, level):
    return [m for s, m in findings if s == level]


class TestAudit(unittest.TestCase):
    def test_clean(self):
        findings, stats = ca.audit(lines(su("t1", "c1"), su("t1", "c1", "completed")))
        self.assertEqual(sev(findings, "CRITICAL"), [])
        self.assertEqual(stats["tasks"], 1)
        self.assertEqual(stats["contexts"], 1)

    def test_context_mutation_critical(self):
        findings, _ = ca.audit(lines(su("t1", "c1"), su("t1", "c2")))
        self.assertEqual(len(sev(findings, "CRITICAL")), 1)
        self.assertIn("changed context", sev(findings, "CRITICAL")[0])

    def test_empty_taskid_critical(self):
        findings, _ = ca.audit(lines(su("", "c1")))
        self.assertTrue(any("empty taskId" in m for m in sev(findings, "CRITICAL")))

    def test_empty_contextid_critical(self):
        findings, _ = ca.audit(lines(su("t1", "")))
        self.assertTrue(any("empty contextId" in m for m in sev(findings, "CRITICAL")))

    def test_no_contextid_warning(self):
        findings, _ = ca.audit(lines(su("t1", None)))
        self.assertTrue(any("never carried a contextId" in m
                            for m in sev(findings, "WARNING")))

    def test_orphan_context_info(self):
        findings, _ = ca.audit(lines(su(None, "cx"), su(None, "cx", "completed")))
        self.assertTrue(any("never produced a task" in m for m in sev(findings, "INFO")))
        self.assertEqual(sev(findings, "CRITICAL"), [])

    def test_request_id_reuse_warning(self):
        a = {"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {}}
        b = {"jsonrpc": "2.0", "id": 7, "method": "tasks/get", "params": {}}
        findings, _ = ca.audit([json.dumps(x) + "\n" for x in (a, b)])
        self.assertTrue(any("request id 7 reused" in m for m in sev(findings, "WARNING")))

    def test_garbage_skipped(self):
        findings, _ = ca.audit(["not json\n", "[]\n", json.dumps(su("t", "c")) + "\n", "{}\n"])
        self.assertEqual(sev(findings, "CRITICAL"), [])

    def test_no_double_report(self):
        # one line with matching result ids must not produce duplicate findings
        findings, _ = ca.audit(lines(su("t1", "c1"), su("t1", "c2")))
        self.assertEqual(len(sev(findings, "CRITICAL")), 1)


class TestReport(unittest.TestCase):
    def test_clean_report(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ca.report(lines(su("t", "c"), su("t", "c", "completed")))
        self.assertEqual(rc, 0)
        self.assertIn("clean", buf.getvalue())

    def test_dirty_report(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ca.report(lines(su("t1", "c1"), su("t1", "c2")))
        self.assertEqual(rc, 2)


class TestCLI(unittest.TestCase):
    DIR = os.path.dirname(os.path.abspath(__file__))

    def _run(self, *args):
        return subprocess.run(["python3", "a2a_context_audit.py", *args],
                              capture_output=True, text=True, cwd=self.DIR)

    def test_audit_dirty_exit2(self):
        p = "/tmp/ctx_t1.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps(su("t1", "c1")) + "\n" + json.dumps(su("t1", "c2")) + "\n")
        self.assertEqual(self._run("audit", p).returncode, 2)

    def test_report_dirty_exit0(self):
        self.assertEqual(self._run("report", "/tmp/ctx_t1.jsonl").returncode, 0)

    def test_missing_file_exit2(self):
        self.assertEqual(self._run("audit", "/nope.jsonl").returncode, 2)

    def test_empty_file_exit2(self):
        p = "/tmp/ctx_t2.jsonl"
        open(p, "w").write("")
        self.assertEqual(self._run("audit", p).returncode, 2)


if __name__ == "__main__":
    unittest.main()
