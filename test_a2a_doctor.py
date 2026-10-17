"""Tests for a2a-doctor — deterministic, no network."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_doctor as dr
import a2a_card_lock


def su(tid, state):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "status-update", "taskId": tid, "contextId": "c1",
        "final": state in ("completed", "failed"),
        "status": {"state": state}}}


def au(tid, text="chunk"):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "artifact-update", "taskId": tid, "contextId": "c1",
        "append": False, "last_chunk": True,
        "artifact": {"artifactId": "a1", "parts": [{"kind": "text", "text": text}]}}}


def lines(*events):
    return [json.dumps(e) + "\n" for e in events]


CARD = {"name": "test-agent", "description": "a" * 40, "url": "https://x.example.com",
        "protocolVersion": "0.2.9", "skills": [{"id": "s1", "name": "S", "description": "d", "tags": []}]}


class TestCheck(unittest.TestCase):
    def test_all_clear(self):
        sections, code = dr.check(CARD, lines(su("t", "submitted"), su("t", "completed")))
        self.assertEqual(code, 0)
        self.assertTrue(all(s == "PASS" for _, s, _ in sections))

    def test_dirty_lifecycle_fails(self):
        sections, code = dr.check(CARD, lines(su("t", "failed"), su("t", "working")))
        self.assertEqual(code, 2)
        names = {n for n, s, _ in sections if s == "FAIL"}
        self.assertIn("lifecycle", names)

    def test_dirty_artifacts_fail(self):
        sections, code = dr.check(CARD, lines(su("t", "failed"), au("t", "late")))
        self.assertEqual(code, 2)

    def test_no_card_fails(self):
        sections, code = dr.check(None, lines(su("t", "completed")))
        names = {n for n, s, _ in sections if s == "FAIL"}
        self.assertIn("card", names)
        self.assertGreaterEqual(code, 1)

    def test_bad_card_type(self):
        sections, code = dr.check([1, 2], lines(su("t", "completed")))
        self.assertIn("card", {n for n, s, _ in sections if s == "FAIL"})

    def test_garbage_stream(self):
        sections, code = dr.check(CARD, ["not json\n", "[]\n"])
        names = {n for n, s, _ in sections if s == "FAIL"}
        self.assertIn("stream", names)

    def test_lock_drift_fails(self):
        lock = a2a_card_lock.pin(CARD)
        bad = json.loads(json.dumps(CARD))
        bad["url"] = "https://evil.example.com"
        sections, code = dr.check(bad, lines(su("t", "completed")),
                                  lock_path="x", lock_data=lock)
        names = {n for n, s, _ in sections if s == "FAIL"}
        self.assertIn("lock", names)
        self.assertEqual(code, 2)

    def test_lock_clean_passes(self):
        lock = a2a_card_lock.pin(CARD)
        sections, code = dr.check(CARD, lines(su("t", "completed")),
                                  lock_path="x", lock_data=lock)
        entry = [e for e in sections if e[0] == "lock"][0]
        self.assertEqual(entry[1], "PASS")

    def test_garbage_lines_skipped_not_crash(self):
        sections, code = dr.check(CARD, ["garbage\n", json.dumps(su("t", "completed")) + "\n", "{}\n"])
        self.assertNotIn("stream", {n for n, s, _ in sections if s == "FAIL"})


class TestHelpers(unittest.TestCase):
    def test_card_tokens_positive(self):
        self.assertGreater(dr._card_est_tokens(CARD), 0)

    def test_load_json_missing(self):
        data, err = dr._load_json("/nope.json", "card")
        self.assertIsNone(data)
        self.assertIn("cannot read", err)

    def test_load_json_bad(self):
        p = "/tmp/dr_bad.json"
        open(p, "w").write("{oops")
        data, err = dr._load_json(p, "card")
        self.assertIn("not valid JSON", err)


class TestCLI(unittest.TestCase):
    DIR = os.path.dirname(os.path.abspath(__file__))

    def _run(self, *args):
        return subprocess.run(["python3", "a2a_doctor.py", *args],
                              capture_output=True, text=True, cwd=self.DIR)

    def setUp(self):
        self.card_p = "/tmp/dr_t_card.json"
        self.stream_p = "/tmp/dr_t_stream.jsonl"
        json.dump(CARD, open(self.card_p, "w"))
        with open(self.stream_p, "w") as f:
            f.write(json.dumps(su("t", "submitted")) + "\n")
            f.write(json.dumps(su("t", "completed")) + "\n")

    def test_tax(self):
        r = self._run("tax", self.card_p)
        self.assertEqual(r.returncode, 0)
        self.assertIn("tok", r.stdout)

    def test_card_drift_subcommand_clean(self):
        r = self._run("card", self.card_p)
        self.assertEqual(r.returncode, 0)

    def test_card_drift_subcommand_dirty(self):
        bad = dict(CARD, protocolVersion="2025-06-18")
        p = "/tmp/dr_t_badcard.json"
        json.dump(bad, open(p, "w"))
        r = self._run("card", p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("MCP", r.stdout)

    def test_check_includes_drift_section(self):
        r = self._run("check", "--card", self.card_p, "--stream", self.stream_p)
        self.assertIn("drift", r.stdout)

    def test_check_drift_fail_exit2(self):
        bad = dict(CARD, protocolVersion="9.2.9")
        p = "/tmp/dr_t_badcard2.json"
        json.dump(bad, open(p, "w"))
        r = self._run("check", "--card", p, "--stream", self.stream_p)
        self.assertEqual(r.returncode, 2)

    def test_tax_bad_card(self):
        r = self._run("tax", "/nope.json")
        self.assertEqual(r.returncode, 2)

    def test_check_clean_exit0(self):
        r = self._run("check", "--card", self.card_p, "--stream", self.stream_p)
        self.assertEqual(r.returncode, 0)
        self.assertIn("ALL CLEAR", r.stdout)

    def test_check_dirty_exit2(self):
        bad = "/tmp/dr_t_bad.jsonl"
        with open(bad, "w") as f:
            f.write(json.dumps(su("t", "failed")) + "\n")
            f.write(json.dumps(su("t", "working")) + "\n")
        r = self._run("check", "--card", self.card_p, "--stream", bad)
        self.assertEqual(r.returncode, 2)

    def test_lifecycle_sub(self):
        r = self._run("lifecycle", self.stream_p)
        self.assertEqual(r.returncode, 0)

    def test_stream_sub(self):
        r = self._run("stream", self.stream_p)
        self.assertEqual(r.returncode, 0)

    def test_reconnect_sub(self):
        r = self._run("reconnect", self.stream_p)
        self.assertEqual(r.returncode, 0)

    def test_artifacts_sub_missing(self):
        r = self._run("artifacts", "/nope.jsonl")
        self.assertEqual(r.returncode, 2)

    def test_no_args_exit2(self):
        self.assertEqual(self._run().returncode, 2)


if __name__ == "__main__":
    unittest.main()
