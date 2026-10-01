"""The helper as the panel runs it: a subprocess whose stdout must be one JSON object.

`omp` is replaced by small scripts that succeed, fail, hang or print garbage.
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "bin" / "omp_usage.py"
NOW = int(time.time() * 1000)

USAGE = {
    "generatedAt": NOW,
    "reports": [{
        "provider": "anthropic", "fetchedAt": NOW,
        "metadata": {"email": "a@example.com", "orgId": "o1"},
        "limits": [{"id": "anthropic:7d", "scope": {"shared": True, "windowId": "7d"},
                    "window": {"id": "7d", "durationMs": 604800000, "resetsAt": NOW + 3600000},
                    "amount": {"usedFraction": 0.25}}],
    }],
}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sessions = self.dir / "sessions"
        (self.sessions / "proj").mkdir(parents=True)
        message = {"type": "message", "id": "e1", "message": {
            "role": "assistant", "provider": "anthropic", "model": "claude-x", "timestamp": NOW - 60000,
            "credentialId": 7, "usage": {"cost": {"total": 1.5}}}}
        (self.sessions / "proj" / "s.jsonl").write_text(json.dumps(message) + "\n")
        self.agent_db = self.dir / "agent.db"
        db = sqlite3.connect(self.agent_db)
        db.execute("CREATE TABLE auth_credentials (id INTEGER, provider TEXT, identity_key TEXT)")
        db.execute("INSERT INTO auth_credentials VALUES (7, 'anthropic', 'email:a@example.com|org:o1')")
        db.commit()
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_omp(self, body):
        path = self.dir / "omp"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def run_helper(self, omp, *extra, timeout=30):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.dir)}
        result = subprocess.run(
            [sys.executable, "-I", str(HELPER), "snapshot", "--omp", str(omp), "--sessions", str(self.sessions),
             "--agent-db", str(self.agent_db), "--cache", str(self.dir / "cache" / "index.db"), *extra],
            capture_output=True, text=True, timeout=timeout, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        # parse_constant rejects NaN/Infinity, like JSON.parse in QML.
        return json.loads(lines[0], parse_constant=lambda c: self.fail(f"non-standard JSON {c}"))

    def test_success_combines_usage_and_attributed_cost(self):
        snap = self.run_helper(self.fake_omp(f"cat <<'EOF'\n{json.dumps(USAGE)}\nEOF\n"))
        self.assertEqual(snap["errors"], [])
        [account] = snap["accounts"]
        self.assertEqual((account["credentialId"], account["percent"], account["cost"]["today"]), (7, 0.25, 1.5))

    def test_failing_omp_reports_error_and_keeps_cost(self):
        snap = self.run_helper(self.fake_omp("echo 'token expired' >&2\nexit 3\n"))
        self.assertEqual(len(snap["errors"]), 1)
        self.assertIn("exited 3", snap["errors"][0])
        self.assertIn("token expired", snap["errors"][0])
        self.assertEqual(snap["providers"][0]["cost"]["today"], 1.5)

    def test_garbage_output(self):
        snap = self.run_helper(self.fake_omp("echo '<html>502 Bad Gateway</html>'\n"))
        self.assertEqual(len(snap["errors"]), 1)
        self.assertEqual(snap["accounts"], [])

    def test_valid_json_of_wrong_type(self):
        for payload in ("[]", "null", '"text"', "42", '{"reports": "nope"}'):
            snap = self.run_helper(self.fake_omp(f"echo '{payload}'\n"))
            self.assertIsInstance(snap["providers"], list, payload)

    def test_hanging_omp_times_out_without_hanging_the_helper(self):
        omp = self.fake_omp("sleep 30\n")
        started = time.monotonic()
        snap = self.run_helper(omp, "--usage-timeout", "1", timeout=20)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("timed out", snap["errors"][0])

    def test_missing_and_non_executable_omp(self):
        snap = self.run_helper(self.dir / "does-not-exist")
        self.assertIn("not found", snap["errors"][0])
        plain = self.dir / "plain"
        plain.write_text("#!/bin/sh\necho {}\n")
        snap = self.run_helper(plain)
        self.assertIn("not found", snap["errors"][0])

    def test_unwritable_cache_still_returns_usage(self):
        cache_dir = self.dir / "ro"
        cache_dir.mkdir()
        cache_dir.chmod(0o500)
        try:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self.dir)}
            result = subprocess.run(
                [sys.executable, "-I", str(HELPER), "snapshot",
                 "--omp", str(self.fake_omp(f"cat <<'EOF'\n{json.dumps(USAGE)}\nEOF\n")),
                 "--sessions", str(self.sessions), "--agent-db", str(self.agent_db),
                 "--cache", str(cache_dir / "sub" / "index.db")],
                capture_output=True, text=True, timeout=30, env=env)
        finally:
            cache_dir.chmod(0o700)
        snap = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(snap["errors"])
        self.assertEqual(snap["accounts"][0]["percent"], 0.25)

    def test_missing_agent_db(self):
        self.agent_db.unlink()
        snap = self.run_helper(self.fake_omp(f"cat <<'EOF'\n{json.dumps(USAGE)}\nEOF\n"))
        self.assertIsNone(snap["accounts"][0]["credentialId"])

    def test_concurrent_snapshots_share_one_cache(self):
        # One panel per monitor: several helpers can start at the same moment.
        for n in range(40):
            message = {"type": "message", "id": f"m{n}", "message": {
                "role": "assistant", "provider": "anthropic", "timestamp": NOW - 1000,
                "usage": {"cost": {"total": 1.0}}}}
            (self.sessions / "proj" / f"f{n}.jsonl").write_text(json.dumps(message) + "\n")
        omp = self.fake_omp(f"cat <<'EOF'\n{json.dumps(USAGE)}\nEOF\n")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.dir)}
        command = [sys.executable, "-I", str(HELPER), "snapshot", "--omp", str(omp), "--sessions", str(self.sessions),
                   "--agent-db", str(self.agent_db), "--cache", str(self.dir / "cache" / "index.db")]
        procs = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(6)]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            snap = json.loads(out)
            self.assertEqual(snap["errors"], [], err)
            self.assertEqual(snap["providers"][0]["cost"]["total"], 41.5)

    def test_demo_reads_nothing_local(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.dir / "nonexistent")}
        result = subprocess.run([sys.executable, "-I", str(HELPER), "snapshot", "--demo", "--omp", "/nonexistent"],
                                capture_output=True, text=True, timeout=30, env=env)
        snap = json.loads(result.stdout)
        self.assertEqual(snap["errors"], [])
        self.assertTrue(all(a["email"].endswith("@example.com") for a in snap["accounts"]))
        self.assertFalse((self.dir / "nonexistent").exists())


if __name__ == "__main__":
    unittest.main()
