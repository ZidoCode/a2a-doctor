"""Tests for a2a-timeline-replay — deterministic, no network."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_timeline_replay as tr


def su(tid, state):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "status-update", "taskId": tid, "contextId": "c1",
        "final": state in ("completed", "failed"),
        "status": {"state": state}}}


def au(tid, aid="a1"):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "kind": "artifact-update", "taskId": tid, "contextId": "c1",
        "append": False, "last_chunk": True,
        "artifact": {"artifactId": aid, "parts": [{"kind": "text", "text": "x"}]}}}


def lines(*events):
    return [json.dumps(e) + "\n" for e in events]


class TestTimeline(unittest.TestCase):
    def test_basic_chain(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.timeline(lines(su("t1", "submitted"), su("t1", "completed")))
        self.assertEqual(rc, 0)
        self.assertIn("submitted → completed", buf.getvalue())

    def test_open_flag(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tr.timeline(lines(su("t1", "working")))
        self.assertIn("[OPEN]", buf.getvalue())

    def test_no_tasks(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.timeline(lines({"jsonrpc": "2.0", "id": 1, "result": {"kind": "message"}}))
        self.assertIn("no tasks", buf.getvalue())


class TestAround(unittest.TestCase):
    def test_found_task(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.around(lines(su("t1", "submitted"), su("t2", "submitted"),
                                 su("t1", "completed")), "t1", 1, 1)
        self.assertEqual(rc, 0)
        self.assertIn("t1", buf.getvalue())

    def test_marker_only_target(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tr.around(lines(su("t1", "submitted"), su("t2", "submitted"), su("t1", "completed")),
                      "t1", 1, 1)
        marked = [l for l in buf.getvalue().splitlines() if l.strip().endswith(()) and " <" in l]
        self.assertTrue(all("t1" in l for l in marked))
        self.assertFalse(any("t2" in l for l in marked))

    def test_not_found_exit2(self):
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = tr.around(lines(su("t1", "submitted")), "nope", 1, 1)
        self.assertEqual(rc, 2)


class TestAt(unittest.TestCase):
    def test_valid_line(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.at(lines(su("t1", "completed")), 1)
        self.assertEqual(rc, 0)
        self.assertIn("status-update", buf.getvalue())

    def test_out_of_range(self):
        rc = tr.at(lines(su("t1", "completed")), 99)
        self.assertEqual(rc, 2)

    def test_empty_line(self):
        rc = tr.at(["", "\n", ""], 2)
        self.assertEqual(rc, 0)


class TestGrep(unittest.TestCase):
    def test_basic(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.grep(lines(su("t1", "failed"), su("t2", "completed")), "failed")
        self.assertEqual(rc, 0)
        self.assertIn("1 hit(s)", buf.getvalue())

    def test_bad_pattern(self):
        rc = tr.grep(lines(su("t1", "failed")), "[invalid")
        self.assertEqual(rc, 2)


class TestCLI(unittest.TestCase):
    DIR = os.path.dirname(tr.__file__)

    def _run(self, *args):
        return subprocess.run(["python3", "a2a_timeline_replay.py", *args],
                              capture_output=True, text=True, cwd=self.DIR)

    def test_timeline_ok(self):
        self.assertEqual(self._run("timeline", "sample_stream.jsonl").returncode, 0)

    def test_around_ok(self):
        self.assertEqual(self._run("around", "sample_stream.jsonl", "zombie-task").returncode, 0)

    def test_around_missing_exit2(self):
        self.assertEqual(self._run("around", "sample_stream.jsonl", "nope").returncode, 2)

    def test_at_ok(self):
        self.assertEqual(self._run("at", "sample_stream.jsonl", "1").returncode, 0)

    def test_at_bad_exit2(self):
        self.assertEqual(self._run("at", "sample_stream.jsonl", "999").returncode, 2)

    def test_grep_ok(self):
        self.assertEqual(self._run("grep", "sample_stream.jsonl", "failed").returncode, 0)

    def test_missing_file(self):
        self.assertEqual(self._run("timeline", "/nope.jsonl").returncode, 2)

    def test_empty_file(self):
        p = "/tmp/tlr_empty.jsonl"
        open(p, "w").write("")
        self.assertEqual(self._run("timeline", p).returncode, 2)

    def test_no_args(self):
        self.assertEqual(self._run().returncode, 2)


if __name__ == "__main__":
    unittest.main()
