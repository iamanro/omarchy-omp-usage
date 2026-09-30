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


class OtherProviderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(omp_usage.SCHEMA)

    def bucket(self, provider, tier, fraction, resets_at, status="ok"):
        return {"id": f"{provider}:{tier}", "label": tier.title(), "scope": {"provider": provider, "tier": tier},
                "window": {"id": "24h", "durationMs": DAY, "resetsAt": resets_at},
                "amount": {"usedFraction": fraction}, "status": status}

    def test_tier_only_buckets_headline_the_fullest_and_block_only_when_all_do(self):
        gemini = {"provider": "google-gemini-cli", "metadata": {"email": "g@x"}, "limits": [
            self.bucket("google-gemini-cli", "pro", 1.0, NOW + 3 * HOUR, "exhausted"),
            self.bucket("google-gemini-cli", "flash", 0.2, NOW + 5 * HOUR),
        ]}
        snap = omp_usage.build_snapshot(self.conn, {"reports": [gemini]}, {}, NOW, [])
        account = snap["accounts"][0]
        # A full bucket warns but does not make the account unusable.
        self.assertEqual((account["status"], account["percent"]), ("limited", 1.0))
        provider = snap["providers"][0]
        # One account: its fullest bucket, not the sum of both.
        self.assertEqual([(w["id"], w["percent"]) for w in provider["windows"]], [("24h", 1.0)])
        self.assertEqual((provider["name"], provider["icon"], provider["available"]), ("Gemini CLI", "gemini", 1))

        gemini["limits"][1] = self.bucket("google-gemini-cli", "flash", 1.0, NOW + 5 * HOUR, "exhausted")
        snap = omp_usage.build_snapshot(self.conn, {"reports": [gemini]}, {}, NOW, [])
        self.assertEqual(snap["accounts"][0]["status"], "exhausted")
        # Back once the first bucket resets.
        self.assertEqual(snap["providers"][0]["nextFreeAt"], NOW + 3 * HOUR)

    def test_api_key_account_maps_to_the_only_credential_and_counts_requests(self):
        self.conn.execute("INSERT INTO messages VALUES (1, 'a', ?, 'github-copilot', 'm', 7, 2.0)", (NOW - HOUR,))
        copilot = {"provider": "github-copilot", "metadata": {}, "limits": [{
            "id": "copilot:premium", "scope": {"provider": "github-copilot", "shared": True},
            "window": {"id": "monthly", "resetsAt": NOW + DAY},
            "amount": {"used": 150, "limit": 300, "unit": "requests"}}]}
        snap = omp_usage.build_snapshot(
            self.conn, {"reports": [copilot]}, {("github-copilot", "", ""): 7}, NOW, [])
        account = snap["accounts"][0]
        self.assertEqual((account["credentialId"], account["email"], account["percent"]), (7, "#7", 0.5))
        self.assertEqual(account["cost"]["today"], 2.0)

    def test_spend_without_an_account_still_lists_the_provider(self):
        self.conn.execute("INSERT INTO messages VALUES (1, 'a', ?, 'openrouter', 'm', NULL, 1.25)", (NOW - HOUR,))
        snap = omp_usage.build_snapshot(self.conn, {"reports": []}, {}, NOW, [])
        [provider] = snap["providers"]
        self.assertEqual((provider["name"], provider["accounts"], provider["percent"]), ("OpenRouter", 0, None))
        self.assertEqual(provider["cost"]["week"], 1.25)


if __name__ == "__main__":
    unittest.main()
