import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("omp_usage", ROOT / "bin" / "omp_usage.py")
omp_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(omp_usage)

HOUR = 3_600_000
DAY = 24 * HOUR
NOW = 1_790_800_000_000


def assistant(entry_id, ts, cost, provider="anthropic", credential=None, model="claude-x"):
    message = {"role": "assistant", "provider": provider, "model": model, "timestamp": ts,
               "usage": {"cost": {"total": cost}}}
    if credential is not None:
        message["credentialId"] = credential
    return json.dumps({"type": "message", "id": entry_id, "message": message}) + "\n"


def limit(window, fraction, resets_at, status="ok", tier=None):
    scope = {"provider": "anthropic", "windowId": window}
    if tier:
        scope["tier"] = tier
    else:
        scope["shared"] = True
    duration = {"5h": 5 * HOUR, "7d": 7 * DAY}[window]
    return {"id": f"anthropic:{window}" + (f":{tier}" if tier else ""), "label": window, "scope": scope,
            "window": {"id": window, "durationMs": duration, "resetsAt": resets_at},
            "amount": {"usedFraction": fraction}, "status": status}


def report(email, org, limits):
    return {"provider": "anthropic", "fetchedAt": NOW, "limits": limits,
            "metadata": {"email": email, "orgId": org}}


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name, "sessions")
        self.sessions.mkdir()
        self.conn = omp_usage.open_index(Path(self.tmp.name, "index.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def costs(self):
        return sorted(r[0] for r in self.conn.execute("SELECT cost FROM messages"))

    def test_partial_line_waits_until_complete_and_rescan_adds_nothing(self):
        log = self.sessions / "a.jsonl"
        whole = assistant("e1", NOW, 1.0)
        half = assistant("e2", NOW, 2.0)
        log.write_text(whole + half[:20])
        omp_usage.index_sessions(self.conn, self.sessions)
        self.assertEqual(self.costs(), [1.0])

        with log.open("a") as handle:
            handle.write(half[20:])
        omp_usage.index_sessions(self.conn, self.sessions)
        omp_usage.index_sessions(self.conn, self.sessions)
        self.assertEqual(self.costs(), [1.0, 2.0])

    def test_rewritten_shorter_log_replaces_its_rows(self):
        log = self.sessions / "nested" / "b.jsonl"
        log.parent.mkdir()
        log.write_text(assistant("e1", NOW, 1.0) + assistant("e2", NOW, 2.0))
        omp_usage.index_sessions(self.conn, self.sessions)
        log.write_text(assistant("e3", NOW, 5.0))
        omp_usage.index_sessions(self.conn, self.sessions)
        self.assertEqual(self.costs(), [5.0])

    def test_non_assistant_and_malformed_lines_are_ignored(self):
        user = json.dumps({"type": "message", "id": "u", "message": {"role": "user", "content": "assistant"}})
        (self.sessions / "c.jsonl").write_text(user + "\n{not json \"assistant\"\n" + assistant("e1", NOW, 3.0))
        omp_usage.index_sessions(self.conn, self.sessions)
        self.assertEqual(self.costs(), [3.0])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(omp_usage.SCHEMA)
        rows = [
            (1, "a", NOW - 1 * HOUR, "anthropic", "opus", 10, 4.0),   # account 10, inside its window
            (1, "b", NOW - 3 * DAY, "anthropic", "opus", 10, 6.0),    # account 10, before its window
            (1, "c", NOW - 2 * HOUR, "anthropic", "sonnet", 11, 1.5),
            (1, "d", NOW - 2 * HOUR, "anthropic", "sonnet", None, 2.5),  # pre-tracking message
            (1, "e", NOW - 2 * HOUR, "anthropic", "sonnet", 99, 0.5),    # removed account
        ]
        self.conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self.credentials = {("anthropic", "a@x", "o1"): 10, ("anthropic", "b@x", "o2"): 11}

    def snapshot(self, reports):
        return omp_usage.build_snapshot(self.conn, {"reports": reports}, self.credentials, NOW, [])

    def test_pool_percent_status_and_resets(self):
        snap = self.snapshot([
            # Window started one day ago (resets in six days).
            report("a@x", "o1", [limit("5h", 0.2, NOW + HOUR), limit("7d", 0.5, NOW + 6 * DAY),
                                 limit("7d", 1.0, NOW + 6 * DAY, "exhausted", tier="fable")]),
            report("b@x", "o2", [limit("5h", 0.0, None), limit("7d", 1.0, NOW + 2 * DAY, "exhausted")]),
        ])
        a, b = snap["accounts"]
        # A tier-only meter does not make the account unusable or set the headline.
        self.assertEqual((a["status"], a["percent"], a["bindingWindow"]), ("ok", 0.5, "7d"))
        self.assertEqual(b["status"], "exhausted")

        claude = snap["providers"][0]
        self.assertEqual((claude["window"], claude["percent"]), ("7d", 0.75))
        self.assertEqual(claude["available"], 1)
        self.assertEqual(claude["nextResetAt"], NOW + HOUR)
        self.assertEqual((claude["nextFreeAt"], claude["nextFreeAccount"]), (NOW + 2 * DAY, "b@x"))

    def test_costs_are_attributed_per_account_window(self):
        snap = self.snapshot([
            report("a@x", "o1", [limit("7d", 0.5, NOW + 6 * DAY)]),
            report("b@x", "o2", [limit("7d", 0.1, NOW + 6 * DAY)]),
        ])
        a, b = snap["accounts"]
        self.assertEqual((a["cost"]["window"], a["cost"]["week"]), (4.0, 10.0))
        self.assertEqual(b["cost"]["week"], 1.5)
        claude = snap["providers"][0]
        self.assertEqual(claude["cost"]["week"], 14.5)
        # Untagged and removed-account spend stays visible at provider level.
        self.assertEqual(claude["unattributed"]["week"], 3.0)


if __name__ == "__main__":
    unittest.main()
