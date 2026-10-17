"""Tests for a2a-webhook-audit — deterministic, no network, no requests."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a2a_webhook_audit as wa


def cfg(url):
    return {"result": {"metadata": {"pushConfig": {"url": url}}}}


def lines(*events):
    return [json.dumps(e) + "\n" for e in events]


def sev(findings, level):
    return [m for s, m in findings if s == level]


class TestAuditUrl(unittest.TestCase):
    def test_clean_url(self):
        f = wa.audit_url("https://hooks.example.com/cb")
        self.assertEqual([s for s, _ in f], ["INFO"])

    def test_metadata_ip_high(self):
        f = wa.audit_url("http://169.254.169.254/latest/meta-data/")
        self.assertGreaterEqual(len(sev(f, "HIGH")), 2)

    def test_localhost_ip_high(self):
        f = wa.audit_url("http://127.0.0.1:8080/admin")
        self.assertTrue(any("private/loopback" in m for m in sev(f, "HIGH")))

    def test_private_range_high(self):
        f = wa.audit_url("https://10.1.2.3/hook")
        self.assertTrue(any("private/loopback" in m for m in sev(f, "HIGH")))

    def test_ipv6_loopback_high(self):
        f = wa.audit_url("http://[::1]/hook")
        self.assertTrue(any("private/loopback" in m for m in sev(f, "HIGH")))

    def test_gcp_metadata_hostname(self):
        f = wa.audit_url("https://metadata.google.internal/computeMetadata/v1/")
        self.assertTrue(any("metadata endpoint" in m for m in sev(f, "HIGH")))

    def test_internal_suffix(self):
        f = wa.audit_url("https://redis.prod.internal/hook")
        self.assertTrue(any("internal hostname" in m for m in sev(f, "HIGH")))

    def test_credentials_high(self):
        f = wa.audit_url("https://user:pass@hooks.example.com/cb")
        self.assertTrue(any("credentials embedded" in m for m in sev(f, "HIGH")))

    def test_plain_http_high(self):
        f = wa.audit_url("http://hooks.example.com/cb")
        self.assertTrue(any("cleartext" in m for m in sev(f, "HIGH")))

    def test_localhost_http_info(self):
        f = wa.audit_url("http://localhost:8080/dev")
        self.assertEqual([s for s, _ in f], ["INFO"])

    def test_risky_port_high(self):
        f = wa.audit_url("https://hooks.example.com:6379/cb")
        self.assertTrue(any("risky port 6379" in m for m in sev(f, "HIGH")))

    def test_no_host_high(self):
        f = wa.audit_url("https:///path-only")
        self.assertTrue(any("no hostname" in m for m in sev(f, "HIGH")))

    def test_punycode_medium(self):
        f = wa.audit_url("https://xn--e1afmkfd.example.com/cb")
        self.assertTrue(any("punycode" in m for m in sev(f, "MEDIUM")))

    def test_unparseable(self):
        f = wa.audit_url("http://[not-an-ip")
        self.assertEqual([s for s, _ in f], ["HIGH"])


class TestAuditStream(unittest.TestCase):
    def test_multiple_urls(self):
        findings, stats = wa.audit(lines(cfg("https://a.example.com"), cfg("http://10.0.0.1")))
        self.assertEqual(stats["urls_audited"], 2)
        self.assertTrue(any("10.0.0.1" in m for m in sev(findings, "HIGH")))

    def test_no_urls_warning(self):
        findings, stats = wa.audit(lines({"result": {"kind": "status-update"}}))
        self.assertEqual(stats["urls_audited"], 0)
        self.assertTrue(any("no webhook URLs" in m for m in sev(findings, "MEDIUM")))

    def test_plain_json_file(self):
        findings, stats = wa.audit(lines({"url": "http://169.254.169.254/"}))
        self.assertEqual(stats["urls_audited"], 1)
        self.assertTrue(any("169.254.169.254" in m for m in sev(findings, "HIGH")))

    def test_garbage_skipped(self):
        findings, _ = wa.audit(["garbage\n", json.dumps(cfg("https://ok.example.com")) + "\n"])
        self.assertEqual(sev(findings, "HIGH"), [])


class TestCLI(unittest.TestCase):
    DIR = os.path.dirname(os.path.abspath(__file__))

    def _run(self, *args):
        return subprocess.run(["python3", "a2a_webhook_audit.py", *args],
                              capture_output=True, text=True, cwd=self.DIR)

    def test_audit_high_exit2(self):
        p = "/tmp/wh_t1.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps(cfg("http://169.254.169.254/")) + "\n")
        self.assertEqual(self._run("audit", p).returncode, 2)

    def test_report_high_exit0(self):
        self.assertEqual(self._run("report", "/tmp/wh_t1.jsonl").returncode, 0)

    def test_clean_exit0(self):
        p = "/tmp/wh_t2.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps(cfg("https://hooks.example.com/cb")) + "\n")
        self.assertEqual(self._run("audit", p).returncode, 0)

    def test_missing_file_exit2(self):
        self.assertEqual(self._run("audit", "/nope.json").returncode, 2)

    def test_empty_file_exit2(self):
        p = "/tmp/wh_t3.json"
        open(p, "w").write("")
        self.assertEqual(self._run("audit", p).returncode, 2)

    def test_no_subcommand_exit2(self):
        self.assertEqual(self._run().returncode, 2)


if __name__ == "__main__":
    unittest.main()
