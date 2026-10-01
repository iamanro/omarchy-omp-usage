"""Hostile and malformed input for the snapshot helper.

Everything here models data the helper does not control: session logs being
written while read, rewritten or corrupted, and `omp usage` reports from
providers whose shapes drift. The contract under test is that one bad record
never takes down the rest of the snapshot and the output is always JSON the
panel can parse.
"""

import importlib.util
import json
import math
import random
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


def line(entry_id, cost, ts=NOW, provider="anthropic", credential=None, **message):
    body = {"role": "assistant", "provider": provider, "model": "m", "timestamp": ts,
            "usage": {"cost": {"total": cost}}}
    if credential is not None:
        body["credentialId"] = credential
    body.update(message)
    return json.dumps({"type": "message", "id": entry_id, "message": body}) + "\n"


def strict_json(payload):
    """What JSON.parse in QML accepts: no NaN/Infinity."""
    return json.loads(json.dumps(payload, allow_nan=False))


class ParseLineTests(unittest.TestCase):
    def test_rejects_values_that_would_poison_sums(self):
        for cost in ["NaN", "Infinity", "-Infinity", "true", '"1.5"', "null", "[]"]:
            raw = ('{"type":"message","id":"x","message":{"role":"assistant","provider":"p",'
                   f'"timestamp":{NOW},"usage":{{"cost":{{"total":{cost}}}}}}}}}').encode()
            self.assertIsNone(omp_usage.parse_line(raw), cost)

    def test_negative_cost_is_rejected(self):
        self.assertIsNone(omp_usage.parse_line(line("x", -3.0).encode()))

    def test_bool_timestamp_and_bool_credential(self):
        self.assertIsNone(omp_usage.parse_line(line("x", 1.0, ts=True).encode()))
        parsed = omp_usage.parse_line(line("x", 1.0, credential=True).encode())
        self.assertIsNone(parsed[4])

    def test_iso_timestamp_fallback_and_missing_ids(self):
        raw = json.dumps({"type": "message", "timestamp": "2026-09-30T10:00:00Z", "message": {
            "role": "assistant", "usage": {"cost": {"total": 0.5}}}}).encode()
        entry_id, ts, provider, model, credential, cost = omp_usage.parse_line(raw)
        self.assertEqual((provider, model, credential, cost), ("unknown", "unknown", None, 0.5))
        self.assertEqual(ts, 1790762400000)
        self.assertTrue(entry_id)

    def test_garbage_never_raises(self):
        rng = random.Random(1)
        good = line("x", 1.0).encode()
        for _ in range(2000):
            data = bytearray(good)
            for _ in range(rng.randint(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            omp_usage.parse_line(bytes(data))  # must not raise


class IndexHardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "sessions")
        self.root.mkdir()
        self.db = Path(self.tmp.name, "cache", "index.db")
        self.conn = omp_usage.open_index(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def total(self):
        return self.conn.execute("SELECT COALESCE(SUM(cost), 0), COUNT(*) FROM messages").fetchone()

    def test_byte_by_byte_appends_index_each_message_exactly_once(self):
        log = self.root / "a.jsonl"
        payload = "".join(line(f"e{i}", 1.0) for i in range(5)).encode()
        log.write_bytes(b"")
        for i in range(0, len(payload), 7):
            with log.open("ab") as handle:
                handle.write(payload[i:i + 7])
            omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (5.0, 5))

    def test_multibyte_utf8_split_across_appends(self):
        log = self.root / "u.jsonl"
        entry = json.loads(line("e1", 2.0, model="模型-ž"))
        text = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        cut = text.index("模".encode()) + 1  # inside the 3-byte sequence
        log.write_bytes(text[:cut])
        omp_usage.index_sessions(self.conn, self.root)
        with log.open("ab") as handle:
            handle.write(text[cut:])
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.conn.execute("SELECT model, cost FROM messages").fetchall(), [("模型-ž", 2.0)])

    def test_crlf_and_blank_lines(self):
        (self.root / "c.jsonl").write_bytes(b"\r\n\n" + line("e1", 1.0).encode().replace(b"\n", b"\r\n") + b"\n")
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (1.0, 1))

    def test_same_entry_id_rewritten_does_not_double_count(self):
        log = self.root / "d.jsonl"
        log.write_text(line("e1", 1.0) + line("e1", 1.0))
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (1.0, 1))

    def test_rewrite_to_same_length_with_new_content_is_reindexed(self):
        log = self.root / "s.jsonl"
        log.write_text(line("e1", 1.0))
        omp_usage.index_sessions(self.conn, self.root)
        replacement = line("e9", 7.0)
        self.assertEqual(len(replacement), len(line("e1", 1.0)))
        log.write_text(replacement)
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (7.0, 1))

    def test_deleted_log_keeps_its_spend_and_recreated_log_starts_over(self):
        log = self.root / "r.jsonl"
        log.write_text(line("e1", 1.0) + line("e2", 1.0))
        omp_usage.index_sessions(self.conn, self.root)
        log.unlink()
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (2.0, 2))
        log.write_text(line("n1", 5.0))
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (5.0, 1))

    def test_unreadable_log_is_skipped_and_retried(self):
        good, bad = self.root / "good.jsonl", self.root / "bad.jsonl"
        good.write_text(line("g", 1.0))
        bad.write_text(line("b", 2.0))
        bad.chmod(0)
        try:
            omp_usage.index_sessions(self.conn, self.root)
            self.assertEqual(self.total(), (1.0, 1))
        finally:
            bad.chmod(0o644)
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (3.0, 2))

    def test_missing_sessions_dir_is_empty_not_an_error(self):
        omp_usage.index_sessions(self.conn, Path(self.tmp.name, "nope"))
        self.assertEqual(self.total(), (0, 0))

    def test_schema_version_bump_rebuilds(self):
        (self.root / "a.jsonl").write_text(line("e1", 1.0))
        omp_usage.index_sessions(self.conn, self.root)
        self.conn.execute("PRAGMA user_version=999")
        self.conn.close()
        self.conn = omp_usage.open_index(self.db)
        self.assertEqual(self.total(), (0, 0))
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (1.0, 1))

    def test_corrupt_cache_file_is_rebuilt(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.db.write_bytes(b"this is not a sqlite database" * 100)
        (self.root / "a.jsonl").write_text(line("e1", 1.0))
        self.conn = omp_usage.open_index(self.db)
        omp_usage.index_sessions(self.conn, self.root)
        self.assertEqual(self.total(), (1.0, 1))


def report(provider="anthropic", email="a@x", org="o", limits=None, **extra):
    data = {"provider": provider, "metadata": {"email": email, "orgId": org}, "limits": limits or []}
    data.update(extra)
    return data


def limit(window="7d", fraction=0.5, resets_at=NOW + DAY, duration=7 * DAY, **extra):
    data = {"id": f"x:{window}", "scope": {"shared": True, "windowId": window},
            "window": {"id": window, "durationMs": duration, "resetsAt": resets_at},
            "amount": {"usedFraction": fraction}}
    data.update(extra)
    return data


class SnapshotHardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(omp_usage.SCHEMA)

    def build(self, usage, credentials=None):
        return omp_usage.build_snapshot(self.conn, usage, credentials or {}, NOW, [])

    def test_malformed_reports_are_skipped_not_fatal(self):
        usage = {"reports": [
            "nonsense", None, 42,
            {"provider": "anthropic", "metadata": ["not", "a", "dict"], "limits": "nope"},
            {"provider": "anthropic", "metadata": {"email": "ok@x"}, "limits": [None, "x", limit()]},
        ], "accountsWithoutUsage": "bad", "disabledCredentials": [None, 3]}
        snap = strict_json(self.build(usage))
        emails = [a["email"] for a in snap["accounts"]]
        self.assertIn("ok@x", emails)
        ok = next(a for a in snap["accounts"] if a["email"] == "ok@x")
        self.assertEqual(ok["percent"], 0.5)

    def test_non_finite_and_out_of_range_fractions(self):
        usage = {"reports": [
            report(email="nan@x", limits=[limit(fraction=float("nan"))]),
            report(email="inf@x", limits=[limit(fraction=float("inf"))]),
            report(email="neg@x", limits=[limit(fraction=-0.4)]),
            report(email="over@x", limits=[limit(fraction=1.7)]),
            report(email="zero-cap@x", limits=[limit(amount={"used": 5, "limit": 0})]),
            report(email="bool@x", limits=[limit(amount={"usedFraction": True})]),
        ]}
        snap = strict_json(self.build(usage))
        by = {a["email"]: a for a in snap["accounts"]}
        for email in ("nan@x", "inf@x", "zero-cap@x", "bool@x"):
            self.assertIsNone(by[email]["percent"], email)
        self.assertEqual(by["neg@x"]["percent"], 0.0)
        self.assertEqual(by["over@x"]["status"], "exhausted")
        pool = snap["providers"][0]["windows"][0]["percent"]
        self.assertTrue(0.0 <= pool <= 1.0)

    def test_window_without_reset_or_duration(self):
        snap = strict_json(self.build({"reports": [
            report(limits=[limit(resets_at=None, duration=None, fraction=1.0, status="exhausted")])]}))
        account = snap["accounts"][0]
        self.assertEqual((account["status"], account["windowStart"]), ("exhausted", None))
        self.assertIsNone(snap["providers"][0]["nextFreeAt"])

    def test_resets_in_the_past_are_not_next(self):
        snap = self.build({"reports": [report(limits=[limit(resets_at=NOW - HOUR, fraction=0.3)])]})
        self.assertIsNone(snap["providers"][0]["nextResetAt"])

    def test_same_email_in_two_orgs_maps_each_to_its_credential(self):
        creds = {("anthropic", "a@x", "o1"): 1, ("anthropic", "a@x", "o2"): 2}
        snap = self.build({"reports": [report(org="o1", limits=[limit()]), report(org="o2", limits=[limit()])]}, creds)
        self.assertEqual(sorted(a["credentialId"] for a in snap["accounts"]), [1, 2])
        self.assertEqual(len({a["key"] for a in snap["accounts"]}), 2)

    def test_ambiguous_email_without_org_is_not_guessed(self):
        creds = {("anthropic", "a@x", "o1"): 1, ("anthropic", "a@x", "o2"): 2}
        snap = self.build({"reports": [report(org="", limits=[limit()])]}, creds)
        self.assertIsNone(snap["accounts"][0]["credentialId"])

    def test_account_without_usage_is_not_duplicated_and_disabled_has_no_meters(self):
        creds = {("anthropic", "a@x", "o"): 1, ("anthropic", "b@x", ""): 2}
        usage = {"reports": [report(limits=[limit()])],
                 "accountsWithoutUsage": [{"provider": "anthropic", "credentialId": 1, "email": "a@x"}],
                 "disabledCredentials": [{"provider": "anthropic", "id": 2, "email": "b@x"}]}
        snap = self.build(usage, creds)
        self.assertEqual([(a["email"], a["status"]) for a in snap["accounts"]],
                         [("a@x", "ok"), ("b@x", "disabled")])
        provider = snap["providers"][0]
        self.assertEqual((provider["accounts"], provider["available"]), (1, 1))

    def test_provider_with_only_unmetered_accounts(self):
        snap = strict_json(self.build({"reports": [report(provider="ollama-cloud", limits=[])]}))
        provider = snap["providers"][0]
        self.assertEqual((provider["percent"], provider["windows"], provider["accounts"]), (None, [], 1))

    def test_usage_none_with_spend_still_reports_cost(self):
        self.conn.execute("INSERT INTO messages VALUES (1, 'a', ?, 'anthropic', 'm', NULL, 4.0)", (NOW - HOUR,))
        snap = strict_json(self.build(None))
        self.assertEqual(snap["providers"][0]["cost"]["week"], 4.0)

    def test_hostile_strings_pass_through_as_data(self):
        evil = "<img src='file:///etc/passwd'>\u202e\x00" + "x" * 10_000
        snap = strict_json(self.build({"reports": [
            report(provider=evil[:40], email=evil, limits=[limit(label=evil)])]}))
        self.assertEqual(snap["accounts"][0]["email"], evil)
        self.assertEqual(snap["accounts"][0]["limits"][0]["label"], evil)

    def test_randomized_reports_keep_invariants(self):
        rng = random.Random(42)
        windows = [("5h", 5 * HOUR), ("24h", DAY), ("7d", 7 * DAY), ("monthly", None), ("", None)]
        providers = ["anthropic", "openai-codex", "google-gemini-cli", "github-copilot", "zz-new"]
        for round_ in range(300):
            self.conn.execute("DELETE FROM messages")
            reports, creds = [], {}
            for i in range(rng.randint(0, 7)):
                provider = rng.choice(providers)
                limits = []
                for _ in range(rng.randint(0, 4)):
                    wid, duration = rng.choice(windows)
                    value = rng.choice([rng.random(), 0, 1, 1.3, -0.1, None, float("nan")])
                    data = limit(window=wid, duration=duration, fraction=value,
                                 resets_at=rng.choice([None, NOW - HOUR, NOW + rng.randint(1, 10) * HOUR]),
                                 status=rng.choice(["ok", "exhausted", None]))
                    if rng.random() < 0.4:
                        data["scope"] = {"tier": rng.choice(["pro", "flash"])}
                    limits.append(data)
                email = rng.choice(["a@x", "b@x", "", None])
                reports.append(report(provider=provider, email=email, org=str(i), limits=limits))
                if rng.random() < 0.7:
                    creds[(provider, email or "", str(i))] = i + 1
                    for n in range(rng.randint(0, 5)):
                        self.conn.execute("INSERT INTO messages VALUES (1, ?, ?, ?, 'm', ?, ?)",
                                          (f"{i}-{n}", NOW - rng.randint(0, 40) * DAY, provider, i + 1,
                                           rng.random() * 5))
            snap = strict_json(self.build({"reports": reports}, creds))
            total = self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM messages").fetchone()[0]
            self.assertAlmostEqual(sum(p["cost"]["total"] for p in snap["providers"]), total, places=2,
                                   msg=f"round {round_}")
            for p in snap["providers"]:
                self.assertLessEqual(p["available"], p["accounts"])
                for w in p["windows"]:
                    self.assertTrue(0.0 <= w["percent"] <= 1.0, (round_, w))
                if p["percent"] is not None:
                    self.assertEqual(p["percent"], max(w["percent"] for w in p["windows"]))
                for key in ("today", "week", "month", "total"):
                    self.assertGreaterEqual(p["cost"][key], 0)
                self.assertLessEqual(p["cost"]["today"], p["cost"]["week"] + 1e-9)
                self.assertLessEqual(p["cost"]["week"], p["cost"]["month"] + 1e-9)
                self.assertLessEqual(p["cost"]["month"], p["cost"]["total"] + 1e-9)
                if p["nextResetAt"] is not None:
                    self.assertGreater(p["nextResetAt"], NOW)
            for a in snap["accounts"]:
                if a["percent"] is not None:
                    self.assertTrue(math.isfinite(a["percent"]) and a["percent"] >= 0)
                self.assertIn(a["status"], {"ok", "limited", "exhausted", "disabled"})


if __name__ == "__main__":
    unittest.main()
