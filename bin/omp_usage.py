#!/usr/bin/env python3
"""Snapshot of OMP subscription usage and API-equivalent cost for the bar.

`omp usage --json` supplies the live limit windows of every authenticated
account. Cost comes from the OMP session logs: every assistant message records
its API-priced `usage.cost.total` and, since OMP started tagging them, the
`credentialId` of the account that served it. The logs are several gigabytes,
so they are indexed incrementally into a small SQLite cache and every snapshot
only reads the bytes appended since the previous one.

Prints one JSON object on stdout and always exits 0; failures are reported in
the object's `errors` list so the panel can show them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
DAY_MS = 86_400_000
# OMP provider id -> (display name, icon in assets/ or ""). Order is display
# order; unknown providers follow alphabetically with a title-cased id.
PROVIDERS = {
    "anthropic": ("Claude", "claude"),
    "openai-codex": ("Codex", "codex"),
    "google-gemini-cli": ("Gemini CLI", "gemini"),
    "google-antigravity": ("Antigravity", "antigravity"),
    "github-copilot": ("Copilot", "copilot"),
    "cursor": ("Cursor", "cursor"),
    "xai-oauth": ("Grok", "grok"),
    "kimi-code": ("Kimi Code", "kimi"),
    "zai": ("Z.ai", "zai"),
    "zhipu-coding-plan": ("Zhipu", "zhipu"),
    "minimax-code": ("MiniMax", "minimax"),
    "minimax-code-cn": ("MiniMax CN", "minimax"),
    "alibaba-token-plan": ("Alibaba", "bailian"),
    "opencode-go": ("OpenCode Go", "opencode"),
    "cline-pass": ("Cline", "cline"),
    "devin": ("Devin", "devin"),
    "firepass": ("Firepass", "fireworks"),
    "fireworks": ("Fireworks", "fireworks"),
    "ollama-cloud": ("Ollama Cloud", "ollama"),
    "ollama": ("Ollama", "ollama"),
    "commandcode": ("Command Code", "commandcode"),
    "synthetic": ("Synthetic", ""),
    "charm-hyper": ("Hyper", ""),
    "muse-code": ("Muse Code", ""),
    "umans": ("Umans", ""),
    # Pay-as-you-go APIs: no quota, but their spend is in the session logs.
    "openai": ("OpenAI", "openai"),
    "google": ("Gemini API", "gemini"),
    "google-vertex": ("Vertex AI", "vertexai"),
    "openrouter": ("OpenRouter", "openrouter"),
    "xai": ("xAI", "xai"),
    "mistral": ("Mistral", "mistral"),
    "deepseek": ("DeepSeek", "deepseek"),
    "groq": ("Groq", "groq"),
    "together": ("Together", "together"),
    "cerebras": ("Cerebras", "cerebras"),
    "moonshot": ("Moonshot", "moonshot"),
    "huggingface": ("Hugging Face", "huggingface"),
    "nvidia": ("NVIDIA", "nvidia"),
    "azure": ("Azure OpenAI", "azure"),
    "amazon-bedrock": ("Bedrock", "bedrock"),
    "deepinfra": ("DeepInfra", "deepinfra"),
    "baseten": ("Baseten", "baseten"),
}
# Providers whose spend shows up without a logged-in account (API keys in the
# environment) are listed when they cost something within this window.
COST_ONLY_WINDOW_DAYS = 30
USAGE_TIMEOUT_SEC = 45
MARKER = b'"assistant"'

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  offset INTEGER NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  file_id INTEGER NOT NULL,
  entry_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  credential_id INTEGER,
  cost REAL NOT NULL,
  PRIMARY KEY (file_id, entry_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS messages_credential ON messages (credential_id, ts, cost);
CREATE INDEX IF NOT EXISTS messages_provider ON messages (provider, ts, model, cost);
"""


# --------------------------------------------------------------- session index


def parse_line(line: bytes):
    """One session-log line -> (entry_id, ts, provider, model, credential_id, cost) or None."""
    if MARKER not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    total = cost.get("total") if isinstance(cost, dict) else None
    if not isinstance(total, (int, float)):
        return None
    ts = message.get("timestamp")
    if not isinstance(ts, (int, float)):
        ts = _iso_ms(entry.get("timestamp"))
        if ts is None:
            return None
    entry_id = entry.get("id") or message.get("responseId") or str(int(ts))
    credential = message.get("credentialId")
    credential = credential if isinstance(credential, int) and not isinstance(credential, bool) else None
    return (
        str(entry_id),
        int(ts),
        str(message.get("provider") or "unknown"),
        str(message.get("model") or "unknown"),
        credential,
        float(total),
    )


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS messages;")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.executescript(SCHEMA)
    return conn


def session_files(root: Path):
    for directory, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".jsonl"):
                yield os.path.join(directory, name)


def index_sessions(conn: sqlite3.Connection, root: Path) -> None:
    """Bring the index up to date with every session log under `root`.

    Only complete lines are consumed: a line still being written keeps the
    stored offset in front of it. A log that shrank was rewritten, so its rows
    are dropped and it is read again from the start. Logs that disappear keep
    their rows, since the money was spent either way.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        known = {row[0]: row[1:] for row in conn.execute("SELECT path, id, offset, size, mtime_ns FROM files")}
        for path in session_files(root):
            try:
                st = os.stat(path)
            except OSError:
                continue
            previous = known.get(path)
            if previous and previous[2] == st.st_size and previous[3] == st.st_mtime_ns:
                continue
            if previous:
                file_id, offset = previous[0], previous[1]
            else:
                file_id = conn.execute(
                    "INSERT INTO files (path, offset, size, mtime_ns) VALUES (?, 0, 0, 0)", (path,)
                ).lastrowid
                offset = 0
            if st.st_size < offset:
                conn.execute("DELETE FROM messages WHERE file_id = ?", (file_id,))
                offset = 0
            rows = []
            try:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if not line.endswith(b"\n"):
                            break
                        offset += len(line)
                        parsed = parse_line(line)
                        if parsed:
                            rows.append((file_id, *parsed))
            except OSError:
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO messages (file_id, entry_id, ts, provider, model, credential_id, cost)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "UPDATE files SET offset = ?, size = ?, mtime_ns = ? WHERE id = ?",
                (offset, st.st_size, st.st_mtime_ns, file_id),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------- sources


def _iso_ms(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def resolve_omp(command: str) -> str | None:
    if command and os.sep in command:
        return command if os.access(command, os.X_OK) else None
    return shutil.which(command or "omp")


def fetch_usage(omp: str) -> dict:
    result = subprocess.run(
        [omp, "usage", "--json"],
        capture_output=True,
        text=True,
        timeout=USAGE_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"omp usage exited {result.returncode}: {tail[0]}")
    return json.loads(result.stdout)


def load_credentials(agent_db: Path) -> dict:
    """(provider, email, org) -> credential id, from OMP's auth store (read-only)."""
    out = {}
    if not agent_db.exists():
        return out
    conn = sqlite3.connect(f"file:{agent_db}?mode=ro", uri=True, timeout=5)
    try:
        for cid, provider, identity in conn.execute("SELECT id, provider, identity_key FROM auth_credentials"):
            parts = dict(p.split(":", 1) for p in str(identity or "").split("|") if ":" in p)
            out[(provider, parts.get("email", ""), parts.get("org", ""))] = cid
    finally:
        conn.close()
    return out


def credential_for(credentials: dict, provider: str, email: str, org: str):
    exact = credentials.get((provider, email, org))
    if exact is not None:
        return exact
    matches = [cid for (p, e, _o), cid in credentials.items() if p == provider and e == email]
    return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------------------ snapshot


def local_midnight_ms(now_ms: int) -> int:
    now = dt.datetime.fromtimestamp(now_ms / 1000).astimezone()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def is_primary(limit: dict) -> bool:
    """Account-wide limits; model tiers (e.g. Fable) only gate their own models."""
    scope = limit.get("scope") or {}
    return scope.get("shared") is True or not scope.get("tier")


def normalize_limit(limit: dict) -> dict:
    amount = limit.get("amount") or {}
    window = limit.get("window") or {}
    fraction = amount.get("usedFraction")
    if not isinstance(fraction, (int, float)):
        used, cap = amount.get("used"), amount.get("limit")
        fraction = used / cap if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap > 0 else None
    return {
        "id": str(limit.get("id") or ""),
        "label": str(limit.get("label") or window.get("label") or ""),
        "window": str(window.get("id") or (limit.get("scope") or {}).get("windowId") or ""),
        "durationMs": window.get("durationMs") if isinstance(window.get("durationMs"), (int, float)) else None,
        "percent": float(fraction) if fraction is not None else None,
        "resetsAt": window.get("resetsAt") if isinstance(window.get("resetsAt"), (int, float)) else None,
        "status": str(limit.get("status") or "ok"),
        "primary": is_primary(limit),
    }


def reset_credits(report: dict):
    credits = report.get("resetCredits") or {}
    count = credits.get("availableCount")
    expiries = [
        ms
        for ms in (_iso_ms(c.get("expiresAt")) for c in credits.get("credits") or [] if isinstance(c, dict))
        if ms is not None
    ]
    return (int(count) if isinstance(count, int) else 0), (min(expiries) if expiries else None)


def provider_name(pid: str) -> str:
    known = PROVIDERS.get(pid)
    return known[0] if known else pid.replace("-", " ").replace("_", " ").title()


def mark_primary(limits: list) -> bool:
    """Ensure the account has headline limits; returns True when every limit is tier-scoped.

    Some providers (Gemini CLI, Antigravity) report only per-model buckets.
    Their buckets then all count as primary, but the account is exhausted
    only once every bucket is, since the others still serve requests.
    """
    if any(l["primary"] for l in limits):
        return False
    for limit in limits:
        limit["primary"] = True
    return bool(limits)


def is_blocking(limit: dict) -> bool:
    return limit["status"] == "exhausted" or (limit["percent"] or 0) >= 1


def account_status(limits: list, tiered: bool = False) -> str:
    primary = [l for l in limits if l["primary"] and l["percent"] is not None]
    blocked = [is_blocking(l) for l in primary]
    if primary and (all(blocked) if tiered else any(blocked)):
        return "exhausted"
    if any(l["percent"] >= 0.9 for l in primary):
        return "limited"
    return "ok"


def free_at(account: dict):
    """When an exhausted account is usable again: its last blocking window reset."""
    blocking = [l["resetsAt"] for l in account["limits"] if l["primary"] and l["resetsAt"] and is_blocking(l)]
    if account["status"] != "exhausted" or not blocking:
        return None
    # A tier-only account is back as soon as its first bucket resets.
    return min(blocking) if account.get("tiered") else max(blocking)


def cost_sum(conn, since_ms, provider=None, credential=None) -> float:
    sql = "SELECT COALESCE(SUM(cost), 0) FROM messages WHERE ts >= ?"
    args = [since_ms]
    if provider is not None:
        sql += " AND provider = ?"
        args.append(provider)
    if credential is not None:
        sql += " AND credential_id = ?"
        args.append(credential)
    return round(conn.execute(sql, args).fetchone()[0], 4)


def build_snapshot(conn, usage: dict | None, credentials: dict, now_ms: int, errors: list) -> dict:
    today = local_midnight_ms(now_ms)
    week = now_ms - 7 * DAY_MS
    month = now_ms - 30 * DAY_MS
    reports = (usage or {}).get("reports") or []

    reports_per_provider = {}
    for report in reports:
        reports_per_provider[report.get("provider")] = reports_per_provider.get(report.get("provider"), 0) + 1

    accounts = []
    for report in reports:
        provider = str(report.get("provider") or "unknown")
        meta = report.get("metadata") or {}
        org = str(meta.get("orgId") or "")
        raw_email = str(meta.get("email") or "")
        cid = credential_for(credentials, provider, raw_email, org)
        if cid is None and reports_per_provider[provider] == 1:
            # API-key logins carry no e-mail; one report and one credential
            # for the provider can only belong together.
            only = [c for (p, _e, _o), c in credentials.items() if p == provider]
            cid = only[0] if len(only) == 1 else None
        email = raw_email or str(
            meta.get("accountName") or meta.get("username") or meta.get("login") or meta.get("accountId")
            or (f"#{cid}" if cid is not None else provider_name(provider))
        )
        limits = [normalize_limit(l) for l in report.get("limits") or [] if isinstance(l, dict)]
        tiered = mark_primary(limits)
        primary = [l for l in limits if l["primary"] and l["percent"] is not None]
        binding = max(primary, key=lambda l: l["percent"]) if primary else None
        # "This window" means the account's longest primary window, which is
        # the one the plan is sold by (weekly for both Claude and Codex).
        longest = max(primary, key=lambda l: l["durationMs"] or 0) if primary else None
        window_start = None
        if longest and longest["durationMs"]:
            window_start = int((longest["resetsAt"] or now_ms) - longest["durationMs"])
        credits, credits_expire = reset_credits(report)
        cost = None
        if cid is not None:
            cost = {
                "today": cost_sum(conn, today, credential=cid),
                "window": cost_sum(conn, window_start, credential=cid) if window_start else None,
                "week": cost_sum(conn, week, credential=cid),
                "month": cost_sum(conn, month, credential=cid),
                "total": cost_sum(conn, 0, credential=cid),
            }
        accounts.append({
            "key": f"{provider}:{cid if cid is not None else email}",
            "credentialId": cid,
            "provider": provider,
            "email": email,
            "org": str(meta.get("orgName") or ""),
            "plan": str(meta.get("planType") or ""),
            "status": account_status(limits, tiered),
            "tiered": tiered,
            "percent": binding["percent"] if binding else None,
            "bindingWindow": binding["window"] if binding else "",
            "limits": limits,
            "resetCredits": credits,
            "resetCreditsExpireAt": credits_expire,
            "fetchedAt": report.get("fetchedAt"),
            "windowStart": window_start,
            "cost": cost,
        })

    # Logged-in accounts OMP has no usage report for (no quota API, or the
    # fetch failed) and disabled credentials still list, without meters.
    for status, entries in (("ok", "accountsWithoutUsage"), ("disabled", "disabledCredentials")):
        for entry in (usage or {}).get(entries) or []:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider") or "unknown")
            email = str(entry.get("email") or entry.get("identity") or entry.get("label") or "")
            cid = entry.get("credentialId", entry.get("id"))
            cid = cid if isinstance(cid, int) and not isinstance(cid, bool) else credential_for(
                credentials, provider, email, str(entry.get("orgId") or ""))
            if status == "ok" and cid is not None and any(a["credentialId"] == cid for a in accounts):
                continue
            accounts.append({
                "key": f"{provider}:{status}:{cid if cid is not None else email}",
                "credentialId": cid,
                "provider": provider,
                "email": email or (f"#{cid}" if cid is not None else provider_name(provider)),
                "org": "", "plan": "", "status": status, "tiered": False, "percent": None, "bindingWindow": "",
                "limits": [], "resetCredits": 0, "resetCreditsExpireAt": None,
                "fetchedAt": None, "windowStart": None,
                "cost": None if cid is None else {
                    "today": cost_sum(conn, today, credential=cid), "window": None,
                    "week": cost_sum(conn, week, credential=cid), "month": cost_sum(conn, month, credential=cid),
                    "total": cost_sum(conn, 0, credential=cid),
                },
            })

    # Spend on providers without any OMP account (API keys from the
    # environment) still counts; they get the cost sections only.
    spending = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT provider FROM messages WHERE ts >= ? AND cost > 0",
            (now_ms - COST_ONLY_WINDOW_DAYS * DAY_MS,),
        )
    }
    order = list(PROVIDERS)
    provider_ids = sorted(
        {a["provider"] for a in accounts} | spending,
        key=lambda pid: (order.index(pid) if pid in order else len(order), provider_name(pid).lower()),
    )

    providers = []
    for pid in provider_ids:
        members = [a for a in accounts if a["provider"] == pid and a["status"] != "disabled"]
        # Pool usage per window: the share of the combined quota already
        # spent, i.e. OMP's capacity figure (used accounts / accounts).
        # Accounts that report no meters (no quota API) stay out of the
        # denominator; an account lacking one window counts 0 in it.
        metered = [a for a in members if any(l["primary"] and l["percent"] is not None for l in a["limits"])]
        windows = {}
        durations = {}
        for account in metered:
            # Several per-model buckets can share one window; the fullest
            # one is the account's figure for it.
            fullest = {}
            for limit in account["limits"]:
                if limit["primary"] and limit["percent"] is not None:
                    fullest[limit["window"]] = max(fullest.get(limit["window"], 0.0), min(1.0, limit["percent"]))
                    durations[limit["window"]] = durations.get(limit["window"]) or limit["durationMs"]
            for wid, value in fullest.items():
                windows.setdefault(wid, []).append(value)
        pool = [
            {"id": wid, "durationMs": durations.get(wid), "percent": sum(values) / len(metered),
             "accounts": len(values)}
            for wid, values in windows.items()
        ]
        pool.sort(key=lambda w: -w["percent"])
        resets = [
            (l["resetsAt"], a["email"], l["window"])
            for a in members
            for l in a["limits"]
            if l["primary"] and l["resetsAt"] and l["resetsAt"] > now_ms and (l["percent"] or 0) > 0
        ]
        freed = [(at, a["email"]) for a in members for at in [free_at(a)] if at]
        tracked = [a["credentialId"] for a in accounts if a["provider"] == pid and a["credentialId"] is not None]
        placeholders = ",".join("?" * len(tracked)) or "NULL"
        unattributed = conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN ts >= ? THEN cost END), 0), COALESCE(SUM(cost), 0) FROM messages"
            f" WHERE provider = ? AND (credential_id IS NULL OR credential_id NOT IN ({placeholders}))",
            [week, pid, *tracked],
        ).fetchone()
        days = {
            row[0]: round(row[1], 4)
            for row in conn.execute(
                "SELECT date(ts / 1000, 'unixepoch', 'localtime'), SUM(cost) FROM messages"
                " WHERE provider = ? AND ts >= ? GROUP BY 1",
                (pid, today - 6 * DAY_MS),
            )
        }
        day_list = []
        for back in range(6, -1, -1):
            date = (dt.datetime.fromtimestamp(today / 1000) - dt.timedelta(days=back)).strftime("%Y-%m-%d")
            day_list.append({"date": date, "cost": days.get(date, 0.0)})
        models = [
            {"model": row[0], "cost": round(row[1], 4)}
            for row in conn.execute(
                "SELECT model, SUM(cost) FROM messages WHERE provider = ? AND ts >= ?"
                " GROUP BY model ORDER BY 2 DESC LIMIT 5",
                (pid, week),
            )
            if row[1] > 0
        ]
        providers.append({
            "id": pid,
            "name": provider_name(pid),
            "icon": PROVIDERS.get(pid, ("", ""))[1],
            "accounts": len(members),
            "available": len([a for a in members if a["status"] != "exhausted"]),
            "percent": pool[0]["percent"] if pool else None,
            "window": pool[0]["id"] if pool else "",
            "windowDurationMs": pool[0]["durationMs"] if pool else None,
            "windows": pool,
            "nextResetAt": min(resets)[0] if resets else None,
            "nextResetAccount": min(resets)[1] if resets else "",
            "nextFreeAt": min(freed)[0] if freed else None,
            "nextFreeAccount": min(freed)[1] if freed else "",
            "cost": {
                "today": cost_sum(conn, today, provider=pid),
                "week": cost_sum(conn, week, provider=pid),
                "month": cost_sum(conn, month, provider=pid),
                "total": cost_sum(conn, 0, provider=pid),
            },
            "unattributed": {"week": round(unattributed[0], 4), "total": round(unattributed[1], 4)},
            "days": day_list,
            "models": models,
        })

    return {
        "generatedAt": now_ms,
        "usageFetchedAt": (usage or {}).get("generatedAt"),
        "trackedSince": conn.execute("SELECT MIN(ts) FROM messages WHERE credential_id IS NOT NULL").fetchone()[0],
        "errors": errors,
        "providers": providers,
        "accounts": accounts,
    }


def snapshot(args) -> dict:
    errors = []
    now_ms = int(time.time() * 1000)
    conn = open_index(Path(args.cache))
    try:
        try:
            index_sessions(conn, Path(args.sessions))
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"Session index: {exc}")
        usage = None
        omp = resolve_omp(args.omp)
        if omp is None:
            errors.append(f"'{args.omp}' not found on PATH; set ompCommand in the widget settings.")
        else:
            try:
                usage = fetch_usage(omp)
            except subprocess.TimeoutExpired:
                errors.append("omp usage timed out.")
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(str(exc))
        try:
            credentials = load_credentials(Path(args.agent_db))
        except sqlite3.Error as exc:
            credentials = {}
            errors.append(f"Credential map: {exc}")
        return build_snapshot(conn, usage, credentials, now_ms, errors)
    finally:
        conn.close()


def demo_snapshot(now_ms: int) -> dict:
    """Invented accounts and spend, for screenshots that must not show real data.

    Runs through the same build_snapshot path as live data, so the demo stays
    faithful to what the panel renders.
    """
    rng = random.Random(7)
    hour = 3_600_000

    windows = {"5h": (5 * hour, "5 Hour"), "24h": (DAY_MS, "Daily"), "7d": (7 * DAY_MS, "7 Day"),
               "30d": (30 * DAY_MS, "Monthly")}

    def limit(provider, window, fraction, resets_in_ms, tier=None, amount=None):
        duration, label = windows[window]
        scope = {"provider": provider, "windowId": window}
        scope.update({"tier": tier} if tier else {"shared": True})
        return {
            "id": f"{provider}:{window}" + (f":{tier}" if tier else ""),
            "label": label + (f" ({tier.title()})" if tier else ""),
            "scope": scope,
            "window": {"id": window, "durationMs": duration,
                       "resetsAt": now_ms + resets_in_ms if resets_in_ms else None},
            # Some providers report counts rather than a fraction.
            "amount": amount or {"usedFraction": fraction, "unit": "percent"},
            "status": "exhausted" if fraction >= 1 else "ok",
        }

    def credit(count, expires_in_days):
        expires = dt.datetime.fromtimestamp((now_ms + expires_in_days * DAY_MS) / 1000, dt.timezone.utc)
        return {"availableCount": count, "credits": [{"expiresAt": expires.isoformat()}] * count}

    accounts = [
        # (credential id, provider, email, plan, limits, reset credits)
        (1, "anthropic", "alice@example.com", "", [
            limit("anthropic", "5h", 0.0, None),
            limit("anthropic", "7d", 1.0, 30 * hour),
        ], credit(1, 20)),
        (2, "anthropic", "bob@example.com", "", [
            limit("anthropic", "5h", 0.18, 3 * hour + 40 * 60_000),
            limit("anthropic", "7d", 0.42, 4 * DAY_MS + 5 * hour),
            limit("anthropic", "7d", 0.05, 4 * DAY_MS + 5 * hour, tier="fable"),
        ], credit(0, 0)),
        (3, "anthropic", "carol@example.com", "", [
            limit("anthropic", "5h", 0.0, None),
            limit("anthropic", "7d", 0.08, 6 * DAY_MS + 2 * hour),
        ], credit(1, 27)),
        (4, "openai-codex", "dave@example.com", "pro", [
            limit("openai-codex", "7d", 0.71, 2 * DAY_MS + 9 * hour),
        ], credit(1, 12)),
        # Per-model buckets only, like Gemini CLI.
        (5, "google-gemini-cli", "erin@example.com", "", [
            limit("google-gemini-cli", "24h", 0.34, 9 * hour, tier="pro"),
            limit("google-gemini-cli", "24h", 0.12, 9 * hour, tier="flash"),
        ], credit(0, 0)),
        # A request count rather than a percentage, like Copilot.
        (6, "github-copilot", "frank@example.com", "pro", [
            limit("github-copilot", "30d", 0.0, 17 * DAY_MS,
                  amount={"used": 212, "limit": 300, "unit": "requests"}),
        ], credit(0, 0)),
    ]
    models = {"anthropic": ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"],
              "openai-codex": ["gpt-5.5", "gpt-6-sol"],
              "google-gemini-cli": ["gemini-3.1-pro", "gemini-3-flash"],
              "github-copilot": ["claude-sonnet-5", "gpt-5.5"]}

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    rows = []
    for cid, provider, *_ in accounts:
        for n in range(240):
            ts = now_ms - int(rng.random() ** 1.15 * 12 * DAY_MS)
            # Credential ids only exist in logs for the last eight days.
            credential = cid if ts > now_ms - 8 * DAY_MS else None
            model = rng.choice(models[provider])
            rows.append((0, f"{cid}-{n}", ts, provider, model, credential, round(rng.uniform(0.05, 1.9), 4)))
    # Spend through an API key in the environment, with no OMP account.
    for n in range(60):
        ts = now_ms - int(rng.random() * 10 * DAY_MS)
        rows.append((0, f"or-{n}", ts, "openrouter", "deepseek-v4", None, round(rng.uniform(0.01, 0.4), 4)))
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    usage = {
        "generatedAt": now_ms,
        "reports": [
            {"provider": provider, "fetchedAt": now_ms, "limits": limits, "resetCredits": credits,
             "metadata": {"email": email, "orgId": f"org-{cid}", "orgName": "Example Org", "planType": plan}}
            for cid, provider, email, plan, limits, credits in accounts
        ],
    }
    credentials = {(provider, email, f"org-{cid}"): cid for cid, provider, email, *_ in accounts}
    return build_snapshot(conn, usage, credentials, now_ms, [])


def main(argv=None) -> int:
    home = Path.home()
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="print the usage + cost snapshot as JSON")
    snap.add_argument("--omp", default="omp", help="omp executable (name on PATH or absolute path)")
    snap.add_argument("--sessions", default=str(home / ".omp/agent/sessions"))
    snap.add_argument("--agent-db", default=str(home / ".omp/agent/agent.db"))
    snap.add_argument("--cache", default=str(cache_home / "omp-usage/index.db"))
    snap.add_argument("--demo", action="store_true", help="invented accounts and spend; reads nothing local")
    args = parser.parse_args(argv)
    try:
        payload = demo_snapshot(int(time.time() * 1000)) if args.demo else snapshot(args)
    except Exception as exc:  # the panel needs JSON, never a traceback
        payload = {"generatedAt": int(time.time() * 1000), "errors": [f"{type(exc).__name__}: {exc}"],
                   "providers": [], "accounts": []}
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
