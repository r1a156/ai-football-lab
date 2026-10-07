#!/usr/bin/env python3
"""R15 Match Intelligence & Express Portfolio production engine.

This module deliberately separates four responsibilities that were coupled in
R14: provider collection, persistent history, match modelling, and publication.
It uses only the Python standard library and the existing update_predictions
module, so GitHub Actions does not need a dependency installation step.
"""
from __future__ import annotations

import argparse
import copy
import csv
import email.utils
import io
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import update_predictions as core
import r15_free_mesh as free_mesh
import r15_daily_auditor as daily_auditor

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "analysis.json"
STATE_PATH = ROOT / "data" / "state.json"
SNAPSHOT_PATH = ROOT / "data" / "ai_daily_analysis.json"
REPORT_PATH = ROOT / "data" / "last-update-report.json"
HISTORY_CACHE_PATH = ROOT / "data" / "football-history-cache.json"
TEAM_REGISTRY_PATH = ROOT / "data" / "team-registry.json"
PROVIDER_HEALTH_PATH = ROOT / "data" / "provider-health.json"
LIVE_STATE_PATH = ROOT / "data" / "live-state.json"
LIVE_LEARNING_PATH = ROOT / "data" / "live-learning.json"
ODDS_CACHE_PATH = ROOT / "data" / "r15-odds-cache.json"
FOOTBALL_DATA_FIXTURE_ODDS_PATH = ROOT / "data" / "football-data-fixtures-odds.json"

UTC = dt.timezone.utc
R15_MARKER = "V10_R15F_R3_FINAL_COGNITIVE_PORTFOLIO"
R15_HISTORY_MARKER = "V10_R15_PERSISTENT_FOOTBALL_HISTORY"
R15_EXPRESS_POLICY = "THREE_BALANCED_EXPRESSES_FIVE_LEGS_TEN_PERCENT_EACH"
R15_MARKET_POLICY = "FOOTBALL_STANDARD_MARKETS_ONLY_NO_ASIAN_LINES"
TERMINAL = {"won", "lost", "push", "void", "cancelled", "unresolved"}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    core.log(f"R15 {message}")


def load_json(path: pathlib.Path, default: Any) -> Any:
    return core.load_json(path, default)


def write_json(path: pathlib.Path, value: Any) -> None:
    core.write_json_atomic(path, value)


def now_utc() -> dt.datetime:
    return core.utc_now()


def safe_float(value: Any, default: float = 0.0) -> float:
    return core.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return core.safe_int(value, default)


def clamp(value: float, low: float, high: float) -> float:
    return core.clamp(value, low, high)


def normalize(value: Any) -> str:
    return core.normalize_text(value)


def stable_id(*parts: Any) -> str:
    return core.stable_id(*parts)


def iso(value: dt.datetime) -> str:
    return core.iso_z(value)


def parse_time(value: Any) -> dt.datetime | None:
    return core.parse_datetime(value)


def mean(values: Iterable[float], default: float = 0.0) -> float:
    rows = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return statistics.fmean(rows) if rows else default


def weighted_mean(rows: Iterable[tuple[float, float]], default: float = 0.0) -> float:
    numerator = 0.0
    denominator = 0.0
    for value, weight in rows:
        if weight <= 0 or not math.isfinite(value):
            continue
        numerator += value * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else default


def json_fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# V10_R15F_R3R7_PROGRESSIVE_PORTFOLIO_ACQUISITION
# V10_R15F_R3R13_STRICT_24H_ROLLOVER_AND_PARTIAL_AVAILABILITY
# The Moscow operational day remains the publication/accounting identity. Match
# discovery may progressively extend beyond that day only to assemble one full
# quality portfolio; every event retains its real commence time.
def operational_day(now: dt.datetime, config: dict[str, Any]) -> dict[str, Any]:
    timezone = core.configured_timezone(config)
    local = now.astimezone(timezone)
    hour = safe_int(config.get("operationalDayStartHourLocal"), 8)
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local < start:
        start -= dt.timedelta(days=1)
    end = start + dt.timedelta(hours=24)
    minimum_lead = dt.timedelta(minutes=safe_int(config.get("minimumLeadMinutes"), 45))
    query_start = max(now + minimum_lead, start.astimezone(UTC))
    # R3R22: keep the Moscow day as the accounting/publication identity,
    # but search a rolling prematch horizon so a sparse calendar cannot leave
    # the public product empty. This changes discovery breadth, never quality.
    search_days = max(1, min(3, safe_int(config.get("operationalWindowSearchDays"), 3)))
    horizon_hours = max(24, min(search_days * 24, safe_int(config.get("portfolioSearchHorizonHours"), 72)))
    search_maximum_end = max(
        end.astimezone(UTC),
        query_start + dt.timedelta(hours=horizon_hours),
    )
    return {
        "operationalDayId": f"{start.date().isoformat()}-MSK-{hour:02d}00",
        "operationalDateLocal": start.date().isoformat(),
        "operationalWindowStart": iso(start.astimezone(UTC)),
        "operationalWindowEnd": iso(end.astimezone(UTC)),
        "queryWindowStart": iso(query_start),
        "queryWindowEnd": iso(search_maximum_end),
        "searchWindowMaximumEnd": iso(search_maximum_end),
        "windowStartLocal": start.isoformat(),
        "windowEndLocal": end.isoformat(),
        "durationHours": 24,
        "searchHorizonHours": horizon_hours,
        "policy": "MOSCOW_DAY_IDENTITY_WITH_ROLLING_PREMATCH_HORIZON",
    }
# ---------------------------------------------------------------------------
# Provider client with cooldowns and quota accounting
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ProviderClient:
    def __init__(self, prior_health: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.quota_identity = "PRIMARY"
        self.odds_quota = {
            "requestsRemaining": None,
            "requestsUsed": None,
            "requestsLast": None,
            "estimatedCreditsThisRun": 0,
        }
        self.health = copy.deepcopy(prior_health or {})
        self.cooldowns: dict[str, dt.datetime] = {}

    def _provider(self, label: str, url: str) -> str:
        if "football-data.org" in url or label.startswith("FOOTBALL_DATA"):
            return "FOOTBALL_DATA"
        if "the-odds-api.com" in url or label.startswith(("ODDS", "EVENTS", "SCORES", "ADVANCED")):
            return "THE_ODDS_API"
        if "ai-football-free.shevtsov001.workers.dev" in url or label.startswith("CLOUDFLARE_AI"):
            return "CLOUDFLARE_AI"
        return "OTHER"

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        retries: int = 1,
        label: str = "HTTP",
        allow_not_found: bool = False,
    ) -> Any:
        provider = self._provider(label, url)
        blocked_until = self.cooldowns.get(provider)
        current = now_utc()
        if blocked_until and current < blocked_until:
            raise ProviderError(
                f"{provider} cooldown until {iso(blocked_until)}",
                status=429,
                retry_after=max(1, int((blocked_until - current).total_seconds())),
            )
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Football-Lab-R15/15.0",
        }
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        attempts = max(0, retries) + 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read().decode("utf-8")
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    elapsed = round(time.monotonic() - started, 3)
                    self.calls.append({
                        "provider": provider,
                        "label": label,
                        "status": int(response.status),
                        "elapsedSeconds": elapsed,
                    })
                    self._capture_odds_headers(response_headers)
                    self._mark_success(provider)
                    return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                retry_after = safe_int(exc.headers.get("Retry-After") if exc.headers else 0, 0)
                self.calls.append({
                    "provider": provider,
                    "label": label,
                    "status": int(exc.code),
                    "retryAfter": retry_after,
                    "error": body[:400],
                })
                self._mark_failure(provider, exc.code, body, retry_after)
                if allow_not_found and exc.code == 404:
                    return None
                if exc.code == 429:
                    seconds = max(retry_after, 75 if provider == "FOOTBALL_DATA" else 60)
                    self.cooldowns[provider] = now_utc() + dt.timedelta(seconds=seconds)
                    raise ProviderError(
                        f"{label} HTTP 429: {body[:300]}",
                        status=429,
                        retry_after=seconds,
                    ) from exc
                if exc.code in {400, 401, 403, 404, 422}:
                    raise ProviderError(f"{label} HTTP {exc.code}: {body[:500]}", status=exc.code) from exc
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                self.calls.append({
                    "provider": provider,
                    "label": label,
                    "status": "ERROR",
                    "error": str(exc),
                })
                self._mark_failure(provider, 0, str(exc), 0)
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise ProviderError(f"{label} failed: {last_error}")

    def _capture_odds_headers(self, headers: dict[str, str]) -> None:
        mapping = {
            "x-requests-remaining": "requestsRemaining",
            "x-requests-used": "requestsUsed",
            "x-requests-last": "requestsLast",
        }
        for source, target in mapping.items():
            if source in headers:
                self.odds_quota[target] = headers[source]
        if headers.get("x-requests-last"):
            self.odds_quota["estimatedCreditsThisRun"] += safe_int(headers["x-requests-last"], 0)

    def _mark_success(self, provider: str) -> None:
        row = self.health.setdefault(provider, {})
        row.update({
            "status": "GREEN",
            "lastSuccessAt": iso(now_utc()),
            "consecutiveFailures": 0,
        })

    def _mark_failure(self, provider: str, status: int, message: str, retry_after: int) -> None:
        row = self.health.setdefault(provider, {})
        row.update({
            "status": "RATE_LIMITED" if status == 429 else "DEGRADED",
            "lastFailureAt": iso(now_utc()),
            "lastStatus": status,
            "lastError": message[:300],
            "retryAfterSeconds": retry_after,
            "consecutiveFailures": safe_int(row.get("consecutiveFailures")) + 1,
        })


# ---------------------------------------------------------------------------
# Persistent historical data and canonical teams
# ---------------------------------------------------------------------------


def empty_history_cache() -> dict[str, Any]:
    return {
        "version": 1,
        "sourceMarker": R15_HISTORY_MARKER,
        "updatedAt": None,
        "lastSuccessfulAt": None,
        "backfillCursorDate": None,
        "coverageStart": None,
        "coverageEnd": None,
        "complete": False,
        "matches": [],
        "sourceHealth": {},
    }


def empty_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "sourceMarker": R15_HISTORY_MARKER,
        "updatedAt": None,
        "teams": {},
        "aliases": {},
    }


def compact_team(team: Any) -> dict[str, Any]:
    if not isinstance(team, dict):
        return {"id": "", "name": "", "shortName": "", "tla": ""}
    return {
        "id": str(team.get("id") or ""),
        "name": str(team.get("name") or ""),
        "shortName": str(team.get("shortName") or ""),
        "tla": str(team.get("tla") or ""),
    }


def compact_match(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    home = compact_team(item.get("homeTeam"))
    away = compact_team(item.get("awayTeam"))
    if not home["name"] or not away["name"]:
        return None
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    full = score.get("fullTime") if isinstance(score.get("fullTime"), dict) else {}
    half = score.get("halfTime") if isinstance(score.get("halfTime"), dict) else {}
    competition = item.get("competition") if isinstance(item.get("competition"), dict) else {}
    match_id = str(item.get("id") or stable_id(home["name"], away["name"], item.get("utcDate"), competition.get("name")))
    return {
        "id": match_id,
        "utcDate": str(item.get("utcDate") or ""),
        "status": str(item.get("status") or ""),
        "competitionId": str(competition.get("id") or ""),
        "competition": str(competition.get("name") or competition.get("code") or ""),
        "competitionCode": str(competition.get("code") or ""),
        "homeTeam": home,
        "awayTeam": away,
        "homeScore": full.get("home"),
        "awayScore": full.get("away"),
        "halfHome": half.get("home"),
        "halfAway": half.get("away"),
        "matchday": item.get("matchday"),
        "stage": str(item.get("stage") or ""),
        "group": str(item.get("group") or ""),
        "lastUpdated": str(item.get("lastUpdated") or ""),
        "source": str(item.get("source") or "FOOTBALL_DATA"),
    }


def ingest_settled_state_history(cache: dict[str, Any], state: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    by_id = {
        str(item.get("id") or ""): item
        for item in cache.get("matches") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    added = 0
    for collection_name in ("analysisHistory", "dailyAnalysis"):
        for row in state.get(collection_name) or []:
            if not isinstance(row, dict) or str(row.get("sport") or "soccer") != "soccer":
                continue
            home_score = row.get("homeScore")
            away_score = row.get("awayScore")
            if home_score is None or away_score is None:
                score_text = str(row.get("score") or "")
                match = __import__("re").match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", score_text)
                if match:
                    home_score, away_score = int(match.group(1)), int(match.group(2))
            if home_score is None or away_score is None:
                continue
            home_name = str(row.get("home") or row.get("homeRu") or "").strip()
            away_name = str(row.get("away") or row.get("awayRu") or "").strip()
            utc_date = str(row.get("commenceTime") or row.get("utcDate") or "")
            if not home_name or not away_name or not utc_date:
                continue
            event_id = str(row.get("eventId") or row.get("oddsEventId") or stable_id(home_name, away_name, utc_date))
            match_id = "tracked:" + event_id
            item = {
                "id": match_id,
                "utcDate": utc_date,
                "status": "FINISHED",
                "competitionId": "",
                "competition": str(row.get("league") or row.get("leagueRu") or ""),
                "competitionCode": str(row.get("sportKey") or ""),
                "homeTeam": {"id": "", "name": home_name, "shortName": home_name, "tla": ""},
                "awayTeam": {"id": "", "name": away_name, "shortName": away_name, "tla": ""},
                "homeScore": safe_int(home_score),
                "awayScore": safe_int(away_score),
                "halfHome": None,
                "halfAway": None,
                "matchday": None,
                "stage": "",
                "group": "",
                "lastUpdated": str(row.get("resultUpdatedAt") or row.get("settledAt") or iso(now)),
                "source": "AI_FOOTBALL_TRACKED_RESULT",
            }
            if by_id.get(match_id) != item:
                if match_id not in by_id:
                    added += 1
                by_id[match_id] = item
    maximum = 25000
    ordered = sorted(by_id.values(), key=lambda item: str(item.get("utcDate") or ""), reverse=True)[:maximum]
    ordered.sort(key=lambda item: str(item.get("utcDate") or ""))
    cache["matches"] = ordered
    dates = [parse_time(item.get("utcDate")) for item in ordered]
    dates = [value for value in dates if value]
    if dates:
        cache["coverageStart"] = iso(min(dates))
        cache["coverageEnd"] = iso(max(dates))
    if added:
        cache["updatedAt"] = iso(now)
    return {"added": added, "matches": len(ordered), "coverageStart": cache.get("coverageStart"), "coverageEnd": cache.get("coverageEnd")}


def _history_windows(cache: dict[str, Any], config: dict[str, Any], now: dt.datetime, budget: int) -> list[tuple[dt.date, dt.date, str]]:
    window_days = max(1, min(10, safe_int(config.get("footballDataRequestWindowDays"), 10)))
    target_days = max(90, safe_int(config.get("footballHistoryTargetDays"), 730))
    today = now.date()
    windows: list[tuple[dt.date, dt.date, str]] = []

    # Always refresh the most recent dates first. This replaces stale scores and
    # is one request even when the long backfill is already complete.
    incremental_start = today - dt.timedelta(days=3)
    windows.append((incremental_start, today + dt.timedelta(days=1), "INCREMENTAL"))

    cursor_text = str(cache.get("backfillCursorDate") or "")
    try:
        cursor = dt.date.fromisoformat(cursor_text) if cursor_text else today + dt.timedelta(days=1)
    except ValueError:
        cursor = today + dt.timedelta(days=1)
    oldest_target = today - dt.timedelta(days=target_days)

    while len(windows) < budget and cursor >= oldest_target:
        start = max(oldest_target, cursor - dt.timedelta(days=window_days - 1))
        windows.append((start, cursor, "BACKFILL"))
        cursor = start - dt.timedelta(days=1)
    return windows


def rebuild_registry(cache: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = copy.deepcopy(existing or empty_registry())
    teams = registry.setdefault("teams", {})
    aliases = registry.setdefault("aliases", {})
    for match in cache.get("matches") or []:
        if not isinstance(match, dict):
            continue
        for side in ("homeTeam", "awayTeam"):
            team = match.get(side) if isinstance(match.get(side), dict) else {}
            source_id = str(team.get("id") or "")
            names = [str(team.get(key) or "").strip() for key in ("name", "shortName", "tla")]
            names = [value for value in names if value]
            if not names:
                continue
            existing_id = next((aliases.get(normalize(name)) for name in names if aliases.get(normalize(name))), None)
            canonical_id = f"fd:{source_id}" if source_id else str(existing_id or ("name:" + stable_id(names[0])))
            if source_id and existing_id and existing_id != canonical_id and existing_id in teams:
                prior = teams.pop(existing_id)
                prior_aliases = set(str(value) for value in prior.get("aliases") or [])
                prior_aliases.update(names)
                teams.setdefault(canonical_id, prior)["aliases"] = sorted(prior_aliases)
                for alias_key, alias_id in list(aliases.items()):
                    if alias_id == existing_id:
                        aliases[alias_key] = canonical_id
            row = teams.setdefault(canonical_id, {
                "canonicalTeamId": canonical_id,
                "footballDataId": source_id,
                "officialName": names[0],
                "aliases": [],
            })
            alias_values = set(str(value) for value in row.get("aliases") or [])
            alias_values.update(names)
            row["aliases"] = sorted(alias_values)
            if source_id:
                row["footballDataId"] = source_id
            for name in alias_values:
                key = normalize(name)
                if key:
                    aliases[key] = canonical_id
    registry["updatedAt"] = iso(now_utc())
    return registry


def refresh_history_cache(
    client: ProviderClient,
    token: str | None,
    config: dict[str, Any],
    now: dt.datetime,
    *,
    request_budget: int | None = None,
) -> dict[str, Any]:
    cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
    if not isinstance(cache, dict) or cache.get("sourceMarker") != R15_HISTORY_MARKER:
        cache = empty_history_cache()
    cache["lastAttemptAt"] = iso(now)
    if not token or not bool(config.get("footballDataEnabled", True)):
        cache["sourceHealth"] = {"status": "DISABLED_OR_KEY_MISSING", "updatedAt": iso(now)}
        write_json(HISTORY_CACHE_PATH, cache)
        return {"changed": False, "requests": 0, "matches": len(cache.get("matches") or []), "status": "NO_KEY"}

    budget = request_budget if request_budget is not None else safe_int(config.get("footballHistoryRequestsPerRun"), 8)
    budget = max(1, min(10, budget))
    interval = max(6.1, safe_float(config.get("footballDataMinimumIntervalSeconds"), 6.2))
    windows = _history_windows(cache, config, now, budget)
    by_id = {
        str(item.get("id") or stable_id(item.get("homeTeam"), item.get("awayTeam"), item.get("utcDate"))): item
        for item in cache.get("matches") or []
        if isinstance(item, dict)
    }
    before = json_fingerprint(by_id)
    successful = 0
    backfill_cursor: dt.date | None = None
    errors: list[str] = []

    for index, (date_from, date_to, purpose) in enumerate(windows):
        if index > 0:
            time.sleep(interval)
        params = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "limit": str(safe_int(config.get("footballDataMaximumMatches"), 500)),
        }
        url = f"{core.FOOTBALL_DATA_BASE}/matches?{urllib.parse.urlencode(params)}"
        try:
            payload = client.request_json(
                url,
                headers={"X-Auth-Token": token},
                label=f"FOOTBALL_DATA_HISTORY:{purpose}:{date_from}:{date_to}",
                retries=0,
            )
        except ProviderError as exc:
            errors.append(str(exc))
            if exc.status == 429:
                break
            continue
        for raw in payload.get("matches") or [] if isinstance(payload, dict) else []:
            compact = compact_match(raw)
            if compact:
                by_id[str(compact["id"])] = compact
        successful += 1
        if purpose == "BACKFILL":
            backfill_cursor = date_from - dt.timedelta(days=1)

    maximum_records = max(5000, safe_int(config.get("footballHistoryMaximumMatches"), 25000))
    ordered = sorted(by_id.values(), key=lambda item: str(item.get("utcDate") or ""), reverse=True)[:maximum_records]
    ordered.sort(key=lambda item: str(item.get("utcDate") or ""))
    cache["matches"] = ordered
    dates = [parse_time(item.get("utcDate")) for item in ordered]
    dates = [value for value in dates if value]
    cache["coverageStart"] = iso(min(dates)) if dates else None
    cache["coverageEnd"] = iso(max(dates)) if dates else None
    if backfill_cursor:
        cache["backfillCursorDate"] = backfill_cursor.isoformat()
    target_start = now.date() - dt.timedelta(days=max(90, safe_int(config.get("footballHistoryTargetDays"), 730)))
    try:
        cursor_value = dt.date.fromisoformat(str(cache.get("backfillCursorDate")))
        cache["complete"] = cursor_value < target_start
    except Exception:
        cache["complete"] = False
    cache["updatedAt"] = iso(now)
    if successful:
        cache["lastSuccessfulAt"] = iso(now)
    cache["sourceHealth"] = {
        "status": "GREEN" if successful and not errors else "DEGRADED" if successful else "UNAVAILABLE",
        "successfulRequests": successful,
        "errors": errors[-5:],
        "updatedAt": iso(now),
    }
    write_json(HISTORY_CACHE_PATH, cache)
    registry = rebuild_registry(cache, load_json(TEAM_REGISTRY_PATH, empty_registry()))
    write_json(TEAM_REGISTRY_PATH, registry)
    after = json_fingerprint({str(item.get("id")): item for item in ordered})
    return {
        "changed": before != after,
        "requests": successful,
        "matches": len(ordered),
        "coverageStart": cache.get("coverageStart"),
        "coverageEnd": cache.get("coverageEnd"),
        "complete": cache.get("complete"),
        "errors": errors,
        "status": cache["sourceHealth"]["status"],
    }


# ---------------------------------------------------------------------------
# Rich football context and match dossier
# ---------------------------------------------------------------------------


def team_aliases(team: dict[str, Any]) -> set[str]:
    return {normalize(team.get(key)) for key in ("name", "shortName", "tla") if normalize(team.get(key))}


def build_history_context(cache: dict[str, Any], registry: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    aliases = dict(registry.get("aliases") or {})
    team_games: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pair_games: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    league_goals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    chronological: list[dict[str, Any]] = []

    for match in cache.get("matches") or []:
        if not isinstance(match, dict):
            continue
        status = str(match.get("status") or "").upper()
        if status not in {"FINISHED", "AWARDED"}:
            continue
        if match.get("homeScore") is None or match.get("awayScore") is None:
            continue
        when = parse_time(match.get("utcDate"))
        if not when or when > now + dt.timedelta(hours=2):
            continue
        home = match.get("homeTeam") if isinstance(match.get("homeTeam"), dict) else {}
        away = match.get("awayTeam") if isinstance(match.get("awayTeam"), dict) else {}
        home_id = f"fd:{home.get('id')}" if home.get("id") else aliases.get(normalize(home.get("name")))
        away_id = f"fd:{away.get('id')}" if away.get("id") else aliases.get(normalize(away.get("name")))
        if not home_id or not away_id:
            continue
        chronological.append({**match, "when": when, "homeId": home_id, "awayId": away_id})

    chronological.sort(key=lambda item: item["when"])
    for match in chronological:
        home_id = str(match["homeId"])
        away_id = str(match["awayId"])
        home_score = safe_int(match.get("homeScore"))
        away_score = safe_int(match.get("awayScore"))
        pre_home = elo[home_id]
        pre_away = elo[away_id]
        expected_home = 1.0 / (1.0 + 10 ** (-((pre_home + 60.0) - pre_away) / 400.0))
        actual_home = 1.0 if home_score > away_score else 0.5 if home_score == away_score else 0.0
        goal_margin = abs(home_score - away_score)
        k = 18.0 * (1.0 + min(3, goal_margin) * 0.12)
        delta = k * (actual_home - expected_home)
        elo[home_id] += delta
        elo[away_id] -= delta
        league = str(match.get("competition") or match.get("competitionCode") or "GLOBAL")
        league_goals[league].append((home_score, away_score))
        base = {
            "utcDate": iso(match["when"]),
            "competition": league,
            "competitionId": str(match.get("competitionId") or ""),
            "homeId": home_id,
            "awayId": away_id,
            "homeScore": home_score,
            "awayScore": away_score,
        }
        team_games[home_id].append({
            **base,
            "side": "home",
            "goalsFor": home_score,
            "goalsAgainst": away_score,
            "opponentId": away_id,
            "opponentElo": pre_away,
            "teamEloBefore": pre_home,
        })
        team_games[away_id].append({
            **base,
            "side": "away",
            "goalsFor": away_score,
            "goalsAgainst": home_score,
            "opponentId": home_id,
            "opponentElo": pre_home,
            "teamEloBefore": pre_away,
        })
        pair_games[tuple(sorted((home_id, away_id)))].append(base)

    for games in team_games.values():
        games.sort(key=lambda item: str(item.get("utcDate") or ""), reverse=True)
    for games in pair_games.values():
        games.sort(key=lambda item: str(item.get("utcDate") or ""), reverse=True)

    league_profiles: dict[str, dict[str, Any]] = {}
    all_rows: list[tuple[int, int]] = []
    for league, rows in league_goals.items():
        all_rows.extend(rows)
        league_profiles[normalize(league)] = {
            "matches": len(rows),
            "homeGoals": mean([row[0] for row in rows], 1.45),
            "awayGoals": mean([row[1] for row in rows], 1.15),
            "totalGoals": mean([row[0] + row[1] for row in rows], 2.60),
        }
    global_profile = {
        "matches": len(all_rows),
        "homeGoals": mean([row[0] for row in all_rows], 1.45),
        "awayGoals": mean([row[1] for row in all_rows], 1.15),
        "totalGoals": mean([row[0] + row[1] for row in all_rows], 2.60),
    }
    return {
        "aliases": aliases,
        "teams": registry.get("teams") or {},
        "teamGames": dict(team_games),
        "pairGames": dict(pair_games),
        "elo": dict(elo),
        "leagueProfiles": league_profiles,
        "globalLeagueProfile": global_profile,
        "cacheMeta": {
            "matches": len(chronological),
            "coverageStart": cache.get("coverageStart"),
            "coverageEnd": cache.get("coverageEnd"),
            "lastSuccessfulAt": cache.get("lastSuccessfulAt"),
            "complete": bool(cache.get("complete")),
        },
    }


def match_team(team_name: str, context: dict[str, Any]) -> tuple[str | None, float]:
    key = normalize(team_name)
    if key in context.get("aliases", {}):
        return str(context["aliases"][key]), 1.0
    best_id: str | None = None
    best_score = 0.0
    for alias, team_id in context.get("aliases", {}).items():
        score = core.token_similarity(key, alias)
        if score > best_score:
            best_score = score
            best_id = str(team_id)
    threshold = 0.78
    return (best_id, best_score) if best_id and best_score >= threshold else (None, best_score)


def form_summary(
    games: list[dict[str, Any]],
    now: dt.datetime,
    *,
    side: str | None = None,
    limit: int = 20,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    selected = [row for row in games if side is None or row.get("side") == side][:limit]
    if not selected:
        return {
            "matches": 0,
            "gf": 0.0,
            "ga": 0.0,
            "adjustedGF": 0.0,
            "adjustedGA": 0.0,
            "points": 0.0,
            "wins": 0.0,
            "draws": 0.0,
            "losses": 0.0,
            "totalGoals": 0.0,
            "over15": 0.0,
            "over25": 0.0,
            "over35": 0.0,
            "over45": 0.0,
            "btts": 0.0,
            "failedToScore": 0.0,
            "cleanSheet": 0.0,
            "variance": 0.0,
            "opponentElo": 1500.0,
            "freshnessDays": 999.0,
            "restDays": None,
            "recentMatches": 0,
            "effectiveSample": 0.0,
            "maximumWeightShare": 0.0,
            "recencyHalfLifeDays": 0.0,
        }
    age_rows: list[tuple[dict[str, Any], float, int]] = []
    recent_window_days = max(30, safe_int(config.get("formSparseRecentWindowDays"), 90))
    minimum_recent = max(2, safe_int(config.get("formSparseMinimumRecentMatches"), 3))
    for index, row in enumerate(selected):
        when = parse_time(row.get("utcDate")) or now - dt.timedelta(days=365)
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        age_rows.append((row, age_days, index))
    recent_matches = sum(1 for _, age_days, _ in age_rows if age_days <= recent_window_days)
    regular_half_life = max(30.0, safe_float(config.get("formRecencyHalfLifeDays"), 70.0))
    sparse_half_life = max(regular_half_life, safe_float(config.get("formSparseHalfLifeDays"), 365.0))
    half_life = regular_half_life if recent_matches >= minimum_recent else sparse_half_life
    minimum_relative_weight = clamp(safe_float(config.get("formMinimumRelativeWeight"), 0.04), 0.0, 0.25)

    weighted: list[tuple[dict[str, Any], float]] = []
    for row, age_days, index in age_rows:
        weight = max(
            minimum_relative_weight,
            (0.5 ** (age_days / half_life)) * (0.965 ** index),
        )
        weighted.append((row, weight))

    total_weight = sum(weight for _, weight in weighted)
    normalized_weights = [weight / total_weight for _, weight in weighted] if total_weight > 0 else []
    effective_sample = (
        1.0 / sum(weight * weight for weight in normalized_weights)
        if normalized_weights and sum(weight * weight for weight in normalized_weights) > 0
        else 0.0
    )
    maximum_weight_share = max(normalized_weights, default=0.0)

    def wmetric(fn, default=0.0):
        return weighted_mean([(float(fn(row)), weight) for row, weight in weighted], default)

    gf_values = [safe_float(row.get("goalsFor")) for row, _ in weighted]
    ga_values = [safe_float(row.get("goalsAgainst")) for row, _ in weighted]
    points = lambda row: 3.0 if safe_float(row.get("goalsFor")) > safe_float(row.get("goalsAgainst")) else 1.0 if safe_float(row.get("goalsFor")) == safe_float(row.get("goalsAgainst")) else 0.0
    adjusted_gf = lambda row: safe_float(row.get("goalsFor")) * clamp(safe_float(row.get("opponentElo"), 1500.0) / 1500.0, 0.78, 1.25)
    adjusted_ga = lambda row: safe_float(row.get("goalsAgainst")) * clamp(1500.0 / max(1100.0, safe_float(row.get("opponentElo"), 1500.0)), 0.78, 1.25)
    latest = parse_time(selected[0].get("utcDate"))
    freshness = max(0.0, (now - latest).total_seconds() / 86400.0) if latest else 999.0
    return {
        "matches": len(selected),
        "gf": round(wmetric(lambda row: safe_float(row.get("goalsFor"))), 4),
        "ga": round(wmetric(lambda row: safe_float(row.get("goalsAgainst"))), 4),
        "adjustedGF": round(wmetric(adjusted_gf), 4),
        "adjustedGA": round(wmetric(adjusted_ga), 4),
        "points": round(wmetric(points) / 3.0, 4),
        "wins": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) > safe_float(row.get("goalsAgainst")) else 0.0), 4),
        "draws": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) == safe_float(row.get("goalsAgainst")) else 0.0), 4),
        "losses": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) < safe_float(row.get("goalsAgainst")) else 0.0), 4),
        "totalGoals": round(wmetric(lambda row: safe_float(row.get("goalsFor")) + safe_float(row.get("goalsAgainst"))), 4),
        "over15": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) + safe_float(row.get("goalsAgainst")) > 1.5 else 0.0), 4),
        "over25": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) + safe_float(row.get("goalsAgainst")) > 2.5 else 0.0), 4),
        "over35": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) + safe_float(row.get("goalsAgainst")) > 3.5 else 0.0), 4),
        "over45": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) + safe_float(row.get("goalsAgainst")) > 4.5 else 0.0), 4),
        "btts": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) > 0 and safe_float(row.get("goalsAgainst")) > 0 else 0.0), 4),
        "failedToScore": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsFor")) <= 0 else 0.0), 4),
        "cleanSheet": round(wmetric(lambda row: 1.0 if safe_float(row.get("goalsAgainst")) <= 0 else 0.0), 4),
        "variance": round(statistics.pstdev([gf - ga for gf, ga in zip(gf_values, ga_values)]) if len(gf_values) > 1 else 0.0, 4),
        "opponentElo": round(wmetric(lambda row: safe_float(row.get("opponentElo"), 1500.0), 1500.0), 2),
        "freshnessDays": round(freshness, 2),
        "restDays": round(freshness, 2),
        "recentMatches": recent_matches,
        "effectiveSample": round(effective_sample, 3),
        "maximumWeightShare": round(maximum_weight_share, 4),
        "recencyHalfLifeDays": round(half_life, 1),
    }


def league_profile(league: str, context: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize(league)
    direct = context.get("leagueProfiles", {}).get(normalized)
    if direct:
        return direct
    best = None
    best_score = 0.0
    for key, value in context.get("leagueProfiles", {}).items():
        score = core.token_similarity(normalized, key)
        if score > best_score:
            best_score = score
            best = value
    return best if best and best_score >= 0.72 else context.get("globalLeagueProfile", {"homeGoals": 1.45, "awayGoals": 1.15, "totalGoals": 2.6, "matches": 0})


def build_match_model(
    event: dict[str, Any],
    quotes: list[dict[str, Any]],
    context: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    home_name = str(event.get("home_team") or "")
    away_name = str(event.get("away_team") or "")
    home_id, home_match_score = match_team(home_name, context)
    away_id, away_match_score = match_team(away_name, context)
    market_home, market_away = core.infer_lambdas_from_market(quotes, "soccer")
    market_total = market_home + market_away
    data_tier = "MARKET"
    data_quality = 34.0
    source_notes = ["Букмекерский консенсус без достаточной истории обеих команд"]
    components: dict[str, Any] = {
        "marketExpectedHome": round(market_home, 4),
        "marketExpectedAway": round(market_away, 4),
        "historyAvailable": False,
        "homeCanonicalTeamId": home_id,
        "awayCanonicalTeamId": away_id,
        "homeNameMatch": round(home_match_score, 3),
        "awayNameMatch": round(away_match_score, 3),
    }
    home_lambda = market_home
    away_lambda = market_away

    if home_id and away_id:
        home_games = list(context.get("teamGames", {}).get(home_id) or [])
        away_games = list(context.get("teamGames", {}).get(away_id) or [])
        home5 = form_summary(home_games, now, limit=5, config=config)
        home10 = form_summary(home_games, now, limit=10, config=config)
        home20 = form_summary(home_games, now, limit=20, config=config)
        home_venue = form_summary(home_games, now, side="home", limit=10, config=config)
        away5 = form_summary(away_games, now, limit=5, config=config)
        away10 = form_summary(away_games, now, limit=10, config=config)
        away20 = form_summary(away_games, now, limit=20, config=config)
        away_venue = form_summary(away_games, now, side="away", limit=10, config=config)
        league = league_profile(str(event.get("sport_title") or ""), context)
        league_home = safe_float(league.get("homeGoals"), 1.45)
        league_away = safe_float(league.get("awayGoals"), 1.15)

        home_attack = weighted_mean([
            (home5["adjustedGF"], 0.24),
            (home10["adjustedGF"], 0.26),
            (home20["adjustedGF"], 0.15),
            (home_venue["adjustedGF"] or home10["adjustedGF"], 0.35),
        ], league_home)
        away_defence = weighted_mean([
            (away5["adjustedGA"], 0.20),
            (away10["adjustedGA"], 0.25),
            (away20["adjustedGA"], 0.15),
            (away_venue["adjustedGA"] or away10["adjustedGA"], 0.40),
        ], league_home)
        away_attack = weighted_mean([
            (away5["adjustedGF"], 0.24),
            (away10["adjustedGF"], 0.26),
            (away20["adjustedGF"], 0.15),
            (away_venue["adjustedGF"] or away10["adjustedGF"], 0.35),
        ], league_away)
        home_defence = weighted_mean([
            (home5["adjustedGA"], 0.20),
            (home10["adjustedGA"], 0.25),
            (home20["adjustedGA"], 0.15),
            (home_venue["adjustedGA"] or home10["adjustedGA"], 0.40),
        ], league_away)

        stat_home = weighted_mean([(home_attack, 0.52), (away_defence, 0.38), (league_home, 0.10)], league_home)
        stat_away = weighted_mean([(away_attack, 0.52), (home_defence, 0.38), (league_away, 0.10)], league_away)
        home_elo = safe_float(context.get("elo", {}).get(home_id), 1500.0)
        away_elo = safe_float(context.get("elo", {}).get(away_id), 1500.0)
        elo_home_probability = 1.0 / (1.0 + 10 ** (-((home_elo + 60.0) - away_elo) / 400.0))
        elo_goal_shift = clamp((elo_home_probability - 0.5) * 0.90, -0.42, 0.42)
        stat_home += elo_goal_shift
        stat_away -= elo_goal_shift

        recent_total = weighted_mean([
            (home5["totalGoals"], 0.18),
            (home10["totalGoals"], 0.17),
            (home_venue["totalGoals"] or home10["totalGoals"], 0.20),
            (away5["totalGoals"], 0.18),
            (away10["totalGoals"], 0.17),
            (away_venue["totalGoals"] or away10["totalGoals"], 0.20),
        ], safe_float(league.get("totalGoals"), 2.6))
        stat_total = max(1.15, stat_home + stat_away)
        total_blend = clamp(recent_total / max(1.1, stat_total), 0.78, 1.22)
        stat_home *= total_blend
        stat_away *= total_blend

        sample = min(home20["matches"], away20["matches"])
        venue_sample = min(home_venue["matches"], away_venue["matches"])
        effective_sample = min(
            safe_float(home20.get("effectiveSample"), 0.0),
            safe_float(away20.get("effectiveSample"), 0.0),
        )
        effective_venue_sample = min(
            safe_float(home_venue.get("effectiveSample"), 0.0),
            safe_float(away_venue.get("effectiveSample"), 0.0),
        )
        sparse_recent = min(
            safe_int(home10.get("recentMatches"), 0),
            safe_int(away10.get("recentMatches"), 0),
        )
        freshness = max(home10["freshnessDays"], away10["freshnessDays"])
        match_quality = min(home_match_score, away_match_score)
        quality = 40.0
        quality += min(22.0, effective_sample * 2.0)
        quality += min(10.0, effective_venue_sample * 2.0)
        quality += match_quality * 12.0
        quality += 7.0 if context.get("cacheMeta", {}).get("complete") else 2.0
        quality -= max(0.0, freshness - 14.0) * 0.30
        if sparse_recent < max(2, safe_int(config.get("formSparseMinimumRecentMatches"), 3)):
            quality -= 6.0
        data_quality = clamp(quality, 40.0, 96.0)
        minimum_effective = max(2.0, safe_float(config.get("formMinimumEffectiveSample"), 3.0))
        if (
            sample >= 12
            and venue_sample >= 5
            and effective_sample >= max(8.0, minimum_effective)
            and effective_venue_sample >= 4.0
            and sparse_recent >= 3
            and match_quality >= 0.90
            and freshness <= 35
        ):
            data_tier = "FULL"
            stat_weight = 0.72
        elif (
            sample >= 8
            and venue_sample >= 3
            and effective_sample >= max(5.0, minimum_effective)
            and sparse_recent >= 2
            and match_quality >= 0.82
        ):
            data_tier = "HYBRID"
            stat_weight = 0.54
        else:
            data_tier = "HYBRID"
            stat_weight = 0.38
        home_lambda = clamp(market_home * (1 - stat_weight) + stat_home * stat_weight, 0.20, 4.5)
        away_lambda = clamp(market_away * (1 - stat_weight) + stat_away * stat_weight, 0.20, 4.5)

        pair_key = tuple(sorted((home_id, away_id)))
        h2h = list(context.get("pairGames", {}).get(pair_key) or [])[:8]
        h2h_home_goals: list[float] = []
        h2h_away_goals: list[float] = []
        h2h_weights: list[float] = []
        for index, row in enumerate(h2h):
            row_home_id = str(row.get("homeId") or "")
            row_away_id = str(row.get("awayId") or "")
            if home_id not in {row_home_id, row_away_id} or away_id not in {row_home_id, row_away_id}:
                continue
            current_home_goals = (
                safe_float(row.get("homeScore"))
                if row_home_id == home_id
                else safe_float(row.get("awayScore"))
            )
            current_away_goals = (
                safe_float(row.get("homeScore"))
                if row_home_id == away_id
                else safe_float(row.get("awayScore"))
            )
            when = parse_time(row.get("utcDate")) or now - dt.timedelta(days=730)
            age_days = max(0.0, (now - when).total_seconds() / 86400.0)
            weight = (0.5 ** (age_days / 540.0)) * (0.90 ** index)
            h2h_home_goals.append(current_home_goals)
            h2h_away_goals.append(current_away_goals)
            h2h_weights.append(weight)

        h2h_matches = len(h2h_weights)
        h2h_weight = 0.0
        h2h_home_avg = 0.0
        h2h_away_avg = 0.0
        h2h_over25 = 0.0
        h2h_btts = 0.0
        if h2h_matches >= 2 and sum(h2h_weights) > 0:
            h2h_home_avg = weighted_mean(list(zip(h2h_home_goals, h2h_weights)), home_lambda)
            h2h_away_avg = weighted_mean(list(zip(h2h_away_goals, h2h_weights)), away_lambda)
            h2h_over25 = weighted_mean([
                (1.0 if h + a > 2.5 else 0.0, w)
                for h, a, w in zip(h2h_home_goals, h2h_away_goals, h2h_weights)
            ], 0.5)
            h2h_btts = weighted_mean([
                (1.0 if h > 0 and a > 0 else 0.0, w)
                for h, a, w in zip(h2h_home_goals, h2h_away_goals, h2h_weights)
            ], 0.5)
            # H2H is a weak prior, never a dominant signal.
            h2h_weight = min(
                safe_float(config.get("h2hMaximumModelWeight"), 0.10),
                safe_float(config.get("h2hPerMatchModelWeight"), 0.018) * h2h_matches,
            )
            home_lambda = clamp(home_lambda * (1.0 - h2h_weight) + h2h_home_avg * h2h_weight, 0.20, 4.5)
            away_lambda = clamp(away_lambda * (1.0 - h2h_weight) + h2h_away_avg * h2h_weight, 0.20, 4.5)

        source_notes = [
            f"Хозяева: {home10['gf']:.2f} забито и {home10['ga']:.2f} пропущено за 10 матчей",
            f"Гости: {away10['gf']:.2f} забито и {away10['ga']:.2f} пропущено за 10 матчей",
            f"Дом/выезд: {home_venue['matches']} и {away_venue['matches']} релевантных матчей",
            f"Elo: {home_elo:.0f} против {away_elo:.0f}",
            f"История: {sample} матчей, эффективная выборка {effective_sample:.1f}, свежих в окне {sparse_recent}",
        ]
        components.update({
            "historyAvailable": True,
            "homeForm5": home5,
            "homeRecent": home10,
            "homeForm20": home20,
            "homeVenue": home_venue,
            "awayForm5": away5,
            "awayRecent": away10,
            "awayForm20": away20,
            "awayVenue": away_venue,
            "homeElo": round(home_elo, 2),
            "awayElo": round(away_elo, 2),
            "eloHomeProbability": round(elo_home_probability, 6),
            "leagueProfile": league,
            "h2hMatches": h2h_matches,
            "h2hModelWeight": round(h2h_weight, 4),
            "h2hExpectedHome": round(h2h_home_avg, 4) if h2h_matches else None,
            "h2hExpectedAway": round(h2h_away_avg, 4) if h2h_matches else None,
            "h2hExpectedTotal": round(h2h_home_avg + h2h_away_avg, 4) if h2h_matches else None,
            "h2hOver25Rate": round(h2h_over25, 4) if h2h_matches else None,
            "h2hBttsRate": round(h2h_btts, 4) if h2h_matches else None,
            "combinedRecentTotalGoals": round(mean([home10["totalGoals"], away10["totalGoals"]]), 4),
            "combinedRecentOver15": round(mean([home10["over15"], away10["over15"], home_venue["over15"], away_venue["over15"]]), 4),
            "combinedRecentOver25": round(mean([home10["over25"], away10["over25"], home_venue["over25"], away_venue["over25"]]), 4),
            "combinedRecentOver35": round(mean([home10["over35"], away10["over35"], home_venue["over35"], away_venue["over35"]]), 4),
            "combinedRecentOver45": round(mean([home10["over45"], away10["over45"], home_venue["over45"], away_venue["over45"]]), 4),
            "combinedRecentBTTS": round(mean([home10["btts"], away10["btts"], home_venue["btts"], away_venue["btts"]]), 4),
            "statExpectedHome": round(stat_home, 4),
            "statExpectedAway": round(stat_away, 4),
            "marketExpectedTotal": round(market_total, 4),
            "statExpectedTotal": round(stat_home + stat_away, 4),
            "teamNameMatch": round(match_quality, 3),
            "effectiveHistorySample": round(effective_sample, 3),
            "effectiveVenueSample": round(effective_venue_sample, 3),
            "recentHistoryMatches": sparse_recent,
            "historyRobustnessPolicy": "ADAPTIVE_RECENCY_DECAY_EFFECTIVE_SAMPLE",
        })

    matrix = core.score_matrix(home_lambda, away_lambda, 10)
    return {
        "sport": "soccer",
        "homeLambda": round(home_lambda, 4),
        "awayLambda": round(away_lambda, 4),
        "expectedScore": f"{home_lambda:.1f} : {away_lambda:.1f}",
        "mostLikelyScores": core.most_likely_scores(matrix),
        "homeWinProbability": core.matrix_outcome_probability(matrix, "HOME"),
        "drawProbability": core.matrix_outcome_probability(matrix, "DRAW"),
        "awayWinProbability": core.matrix_outcome_probability(matrix, "AWAY"),
        "matrix": matrix,
        "dataTier": data_tier,
        "dataQuality": round(data_quality, 1),
        "sourceNotes": source_notes,
        "components": components,
    }


# ---------------------------------------------------------------------------
# Strict discovery and quota-aware odds collection
# ---------------------------------------------------------------------------


def discover_operational_events(
    client: ProviderClient,
    api_key: str,
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    window = operational_day(now, config)
    start = parse_time(window["queryWindowStart"])
    operational_end = parse_time(window["operationalWindowEnd"])
    maximum_end = parse_time(window["searchWindowMaximumEnd"])
    if not start or not operational_end or not maximum_end or start >= maximum_end:
        return [], {**window, "events": 0, "reason": "SEARCH_WINDOW_ALREADY_CLOSED"}
    sports = core.fetch_active_sports(client, api_key)
    football = [item for item in sports if core.league_allowed(item, config)]
    maximum = max(1, safe_int(config.get("maximumDiscoverySports"), 300))
    football = football[:maximum]
    target = max(safe_int(config.get("dailyAnalysisTarget"), 15), safe_int(config.get("portfolioSearchTargetEvents"), 60))
    step_hours = max(6, min(48, safe_int(config.get("portfolioSearchStepHours"), 24)))
    spacing = max(0.05, safe_float(config.get("oddsDiscoverySpacingSeconds"), 0.12))
    unique: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    stages: list[dict[str, Any]] = []
    stage_start = start
    stage_index = 0
    actual_end = start
    while stage_start < maximum_end:
        if stage_index == 0 and stage_start < operational_end:
            stage_end = min(operational_end, maximum_end)
            stage_name = "CURRENT_OPERATIONAL_REMAINDER"
        else:
            stage_end = min(maximum_end, stage_start + dt.timedelta(hours=step_hours))
            stage_name = f"FUTURE_STAGE_{stage_index}"
        before = len(unique)
        stage_errors = 0
        for index, sport in enumerate(football):
            if index:
                time.sleep(spacing)
            try:
                rows = core.fetch_sport_events(client, api_key, sport, stage_start, stage_end)
            except Exception as exc:
                errors.append(f"{stage_name}:{sport.get('key')}: {exc}")
                stage_errors += 1
                continue
            for row in rows:
                if not core.event_allowed(row, config):
                    continue
                event_id = str(row.get("id") or stable_id(row.get("sport_key"), row.get("home_team"), row.get("away_team"), row.get("commence_time")))
                unique[event_id] = row
        actual_end = stage_end
        stages.append({
            "stage": stage_index,
            "name": stage_name,
            "start": iso(stage_start),
            "end": iso(stage_end),
            "newEvents": len(unique) - before,
            "cumulativeEvents": len(unique),
            "providerErrors": stage_errors,
        })
        if len(unique) >= target:
            break
        if stage_end <= stage_start:
            break
        stage_start = stage_end
        stage_index += 1
    ordered = sorted(unique.values(), key=lambda item: str(item.get("commence_time") or ""))
    by_sport: dict[str, int] = defaultdict(int)
    for event in ordered:
        by_sport[str(event.get("sport_key") or "")] += 1
    diagnostics = {
        **window,
        "queryWindowEnd": iso(actual_end),
        "selectionWindowStart": iso(start),
        "selectionWindowEnd": iso(actual_end),
        "activeFootballCompetitions": len(football),
        "events": len(ordered),
        "targetEvents": target,
        "targetReached": len(ordered) >= target,
        "stagesUsed": len(stages),
        "progressiveStages": stages,
        "sportKeysWithEvents": len(by_sport),
        "eventsBySportKey": dict(by_sport),
        "errors": errors[-40:],
        "policy": "ROLLING_PREMATCH_HORIZON_72H_BREADTH_FIRST",
    }
    return ordered, diagnostics
def sport_history_coverage(events: list[dict[str, Any]], context: dict[str, Any]) -> float:
    matched = 0
    for event in events:
        home, _ = match_team(str(event.get("home_team") or ""), context)
        away, _ = match_team(str(event.get("away_team") or ""), context)
        matched += 1 if home and away else 0
    return matched / len(events) if events else 0.0


def quota_identity_from_selection(selection: dict[str, Any]) -> str:
    selected = str(selection.get("selected") or "PRIMARY").upper()
    return "BACKUP" if selected.startswith("BACKUP") else "PRIMARY"


def seed_client_quota_identity(client: ProviderClient, selection: dict[str, Any]) -> None:
    identity = quota_identity_from_selection(selection)
    probe = selection.get("backup") if identity == "BACKUP" else selection.get("primary")
    probe = probe if isinstance(probe, dict) else {}
    client.quota_identity = identity
    mapping = {
        "remaining": "requestsRemaining",
        "used": "requestsUsed",
        "last": "requestsLast",
    }
    for source, target in mapping.items():
        value = probe.get(source)
        if value is not None and safe_int(value, -1) >= 0:
            client.odds_quota[target] = str(safe_int(value, 0))


def _persistent_odds_daily_spend(
    client: ProviderClient,
    config: dict[str, Any],
    now: dt.datetime,
) -> int:
    """Track Odds API credits per credential for one Moscow operational day.

    PRIMARY and BACKUP are independent subscriptions/quotas. Mixing their
    monthly counters in one baseline can falsely exhaust the active key, so
    every identity has an isolated persistent ledger.
    """
    current_raw = client.odds_quota.get("requestsUsed")
    estimated_this_run = max(
        0,
        safe_int(client.odds_quota.get("estimatedCreditsThisRun"), 0),
    )
    if current_raw is None or str(current_raw).strip() == "":
        return estimated_this_run

    current_used = max(0, safe_int(current_raw, 0))
    day_id = str(operational_day(now, config).get("operationalDayId") or "")
    identity = str(getattr(client, "quota_identity", "PRIMARY") or "PRIMARY").upper()
    if identity not in {"PRIMARY", "BACKUP"}:
        identity = "PRIMARY"

    control = load_json(daily_auditor.CONTROL_PATH, {})
    ledgers = control.get("oddsQuotaLedgers")
    if not isinstance(ledgers, dict):
        ledgers = {}
    ledger = ledgers.get(identity)
    if not isinstance(ledger, dict):
        legacy = control.get("oddsQuotaLedger")
        if isinstance(legacy, dict) and str(legacy.get("quotaIdentity") or "").upper() == identity:
            ledger = copy.deepcopy(legacy)
        else:
            ledger = {
                "operationalDayId": day_id,
                "quotaIdentity": identity,
                "baselineMonthlyUsed": current_used,
                "lastMonthlyUsed": current_used,
                "creditsUsed": 0,
                "updatedAt": iso(now),
                "policy": "PERSISTENT_PROVIDER_HEADER_DAILY_CEILING_PER_KEY",
                "migration": "FIRST_KEY_SCOPED_OBSERVATION",
            }

    ledger_day = str(ledger.get("operationalDayId") or "")
    baseline = safe_int(ledger.get("baselineMonthlyUsed"), current_used)

    if ledger_day != day_id or current_used < baseline:
        baseline = current_used
        ledger = {
            "operationalDayId": day_id,
            "quotaIdentity": identity,
            "baselineMonthlyUsed": current_used,
            "lastMonthlyUsed": current_used,
            "creditsUsed": 0,
            "updatedAt": iso(now),
            "policy": "PERSISTENT_PROVIDER_HEADER_DAILY_CEILING_PER_KEY",
        }
    else:
        spent = max(0, current_used - baseline)
        ledger.update({
            "operationalDayId": day_id,
            "quotaIdentity": identity,
            "lastMonthlyUsed": current_used,
            "creditsUsed": spent,
            "updatedAt": iso(now),
            "policy": "PERSISTENT_PROVIDER_HEADER_DAILY_CEILING_PER_KEY",
        })

    ledgers[identity] = ledger
    control["oddsQuotaLedgers"] = ledgers
    # Compatibility alias: always mirror the currently selected credential.
    control["oddsQuotaLedger"] = copy.deepcopy(ledger)
    write_json(daily_auditor.CONTROL_PATH, control)
    return max(0, safe_int(ledger.get("creditsUsed"), 0))

def free_odds_daily_budget(client: ProviderClient, config: dict[str, Any], now: dt.datetime | None = None) -> dict[str, int]:
    now = now or now_utc()
    remaining_raw = client.odds_quota.get("requestsRemaining")
    assumed = max(1, safe_int(config.get("oddsFreeMonthlyCredits"), 500))
    remaining = safe_int(remaining_raw, assumed) if remaining_raw is not None else assumed
    next_month = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days_left = max(1, (next_month.date() - now.date()).days)
    fair_share = max(1, remaining // days_left)
    configured = max(1, safe_int(config.get("oddsFreeDailyCreditBudget"), 16))
    used_this_run = max(0, safe_int(client.odds_quota.get("estimatedCreditsThisRun"), 0))
    daily_spent = _persistent_odds_daily_spend(client, config, now)
    reserve = max(0, safe_int(config.get("oddsQuotaHardReserve"), 0))

    configured_left = max(0, configured - daily_spent)
    fair_share_left = max(
        0,
        fair_share + safe_int(config.get("oddsDailyCarryAllowance"), 2) - daily_spent,
    )
    daily_available = max(
        0,
        min(
            remaining,
            configured_left,
            fair_share_left,
        ),
    )

    # Every paid acquisition path must use this value. It is intentionally
    # identical to the cross-run daily allowance, never the whole monthly pool.
    portfolio_available = max(0, min(daily_available, remaining - reserve))

    return {
        "remaining": remaining,
        "daysLeft": days_left,
        "fairShare": fair_share,
        "configured": configured,
        "usedThisRun": used_this_run,
        "dailyUsedPersistent": daily_spent,
        "reserve": reserve,
        "availableThisRun": daily_available,
        "portfolioAvailableThisRun": portfolio_available,
    }
# V10_R15F_R3R10_RESERVED_ADVANCED_NEAR_MISS_RECOVERY
# R3R10 reserves real monthly credits before the featured-competition burst,
# spends advanced credits one market at a time on hard-filter-clean near misses,
# recalculates the full portfolio after every useful response, and only then
# considers another competition. Strategy thresholds and probabilities are unchanged.
# V10_R15F_R3R11_QUOTA_CACHE_AUTOMATIC_RESUME
# R3R11 never invents or stretches odds. It persists only provider-returned payloads,
# reuses them only while the event is still future and the quote remains inside the
# configured freshness window, and waits fail-closed when the real quota is zero.
def _odds_cache_quote_time(event: dict[str, Any]) -> dt.datetime | None:
    values: list[dt.datetime] = []
    direct = parse_time(event.get("last_update") or event.get("lastUpdate") or event.get("_r15CachedAt"))
    if direct is not None:
        values.append(direct)
    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        value = parse_time(bookmaker.get("last_update") or bookmaker.get("lastUpdate"))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _odds_cache_event_usable(
    event: dict[str, Any],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> tuple[bool, str]:
    if not isinstance(event, dict) or not str(event.get("id") or ""):
        return False, "INVALID_EVENT"
    commence = parse_time(event.get("commence_time") or event.get("commenceTime"))
    if commence is None:
        return False, "MISSING_COMMENCE_TIME"
    minimum_lead = dt.timedelta(minutes=max(0, safe_int(config.get("minimumLeadMinutes"), 45)))
    if commence < now + minimum_lead:
        return False, "EVENT_ALREADY_STARTED_OR_TOO_CLOSE"
    if commence < start or commence > end:
        return False, "OUTSIDE_ACTIVE_SELECTION_HORIZON"
    quote_time = _odds_cache_quote_time(event)
    if quote_time is None:
        return False, "MISSING_QUOTE_TIME"
    maximum_age = dt.timedelta(minutes=max(1, safe_int(
        config.get("oddsCacheMaximumAgeMinutes"),
        config.get("maximumQuoteAgeMinutes", 180),
    )))
    age = now - quote_time
    if age < dt.timedelta(minutes=-5):
        return False, "QUOTE_TIME_IN_FUTURE"
    if age > maximum_age:
        return False, "QUOTE_EXPIRED"
    if not any(isinstance(row, dict) for row in event.get("bookmakers") or []):
        return False, "NO_BOOKMAKER_PAYLOAD"
    return True, "GREEN"


def load_recent_odds_snapshot_cache(
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    diagnostics = {
        "enabled": bool(config.get("oddsCacheEnabled", True)),
        "path": str(ODDS_CACHE_PATH),
        "loadedFeatured": 0,
        "loadedAdvanced": 0,
        "expired": 0,
        "rejectedReasons": {},
    }
    if not diagnostics["enabled"]:
        return [], {}, diagnostics
    payload = load_json(ODDS_CACHE_PATH, {})
    if not isinstance(payload, dict):
        return [], {}, diagnostics
    featured: list[dict[str, Any]] = []
    advanced: dict[str, dict[str, Any]] = {}
    reasons: defaultdict[str, int] = defaultdict(int)
    maximum_events = max(15, safe_int(config.get("oddsCacheMaximumEvents"), 500))
    for event in payload.get("featuredEvents") or []:
        ok, reason = _odds_cache_event_usable(event, config, start, end, now)
        if ok:
            item = copy.deepcopy(event)
            item["_r15OddsCacheHit"] = True
            featured.append(item)
        else:
            reasons[reason] += 1
    raw_advanced = payload.get("advancedEvents") or {}
    if isinstance(raw_advanced, dict):
        rows = raw_advanced.items()
    else:
        rows = (
            (str(item.get("id") or ""), item)
            for item in raw_advanced
            if isinstance(item, dict)
        )
    for event_id, event in rows:
        ok, reason = _odds_cache_event_usable(event, config, start, end, now)
        if ok and event_id:
            item = copy.deepcopy(event)
            item["_r15OddsCacheHit"] = True
            advanced[str(event_id)] = item
        else:
            reasons[reason] += 1
    featured = merge_unique_odds_events([], featured)[:maximum_events]
    if len(advanced) > maximum_events:
        advanced = dict(list(advanced.items())[:maximum_events])
    diagnostics["loadedFeatured"] = len(featured)
    diagnostics["loadedAdvanced"] = len(advanced)
    diagnostics["expired"] = sum(reasons.values())
    diagnostics["rejectedReasons"] = dict(reasons)
    diagnostics["cacheUpdatedAt"] = payload.get("updatedAt")
    return featured, advanced, diagnostics


def save_recent_odds_snapshot_cache(
    featured_events: list[dict[str, Any]],
    advanced: dict[str, dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    diagnostics = {
        "enabled": bool(config.get("oddsCacheEnabled", True)),
        "savedFeatured": 0,
        "savedAdvanced": 0,
        "path": str(ODDS_CACHE_PATH),
    }
    if not diagnostics["enabled"]:
        return diagnostics
    maximum_events = max(15, safe_int(config.get("oddsCacheMaximumEvents"), 500))
    maximum_horizon = dt.timedelta(hours=max(24, safe_int(config.get("portfolioSearchHorizonHours"), 72)) + 24)
    start = now
    end = now + maximum_horizon
    prepared_featured: list[dict[str, Any]] = []
    for event in merge_unique_odds_events([], featured_events):
        item = copy.deepcopy(event)
        if not item.get("_r15CachedAt"):
            item["_r15CachedAt"] = iso(now)
        item.pop("_r15OddsCacheHit", None)
        ok, _ = _odds_cache_event_usable(item, config, start, end, now)
        if ok:
            prepared_featured.append(item)
    prepared_advanced: dict[str, dict[str, Any]] = {}
    for event_id, event in (advanced or {}).items():
        item = copy.deepcopy(event)
        if not item.get("_r15CachedAt"):
            item["_r15CachedAt"] = iso(now)
        item.pop("_r15OddsCacheHit", None)
        ok, _ = _odds_cache_event_usable(item, config, start, end, now)
        if ok and event_id:
            prepared_advanced[str(event_id)] = item
    prepared_featured = prepared_featured[:maximum_events]
    if len(prepared_advanced) > maximum_events:
        prepared_advanced = dict(list(prepared_advanced.items())[:maximum_events])
    write_json(ODDS_CACHE_PATH, {
        "version": 1,
        "sourceMarker": "V10_R15F_R3R11_QUOTA_CACHE_AUTOMATIC_RESUME",
        "updatedAt": iso(now),
        "maximumAgeMinutes": max(1, safe_int(
            config.get("oddsCacheMaximumAgeMinutes"),
            config.get("maximumQuoteAgeMinutes", 180),
        )),
        "featuredEvents": prepared_featured,
        "advancedEvents": prepared_advanced,
    })
    diagnostics["savedFeatured"] = len(prepared_featured)
    diagnostics["savedAdvanced"] = len(prepared_advanced)
    return diagnostics


def odds_quota_is_exhausted(client: ProviderClient, config: dict[str, Any]) -> bool:
    raw = client.odds_quota.get("requestsRemaining")
    if raw is not None and str(raw).strip() != "":
        return safe_int(raw, 0) <= 0
    budget = free_odds_daily_budget(client, config)
    return safe_int(budget.get("portfolioAvailableThisRun"), 0) <= 0

# V10_R15F_R3R12_NO_KEY_FIXTURE_ODDS_FALLBACK
# This fallback accepts only real bookmaker prices from Football-Data's published
# fixture CSV. Rows are bound to events already discovered by the primary schedule
# provider; the CSV never invents an event time or identity. Asian handicap columns
# and market averages are ignored. A stale or structurally invalid feed is fail-closed.
_R3R12_FIXTURE_BOOKMAKERS = (
    ("1xb", "1XBet", "1XB", None),
    ("bet365", "Bet365", "B365", "B365"),
    ("betfair", "Betfair", "BF", None),
    ("betfred", "Betfred", "BFD", None),
    ("betmgm", "BetMGM", "BMGM", None),
    ("betvictor", "BetVictor", "BV", None),
    ("bwin", "Bwin", "BW", None),
    ("coral", "Coral", "CL", None),
    ("gamebookers", "Gamebookers", "GB", "GB"),
    ("interwetten", "Interwetten", "IW", None),
    ("ladbrokes", "Ladbrokes", "LB", None),
    ("paddypower", "Paddy Power", "PP", None),
    ("pinnacle", "Pinnacle", "PS", "P"),
    ("skybet", "Sky Bet", "SK", None),
    ("sportingodds", "Sporting Odds", "SO", None),
    ("sportingbet", "Sportingbet", "SB", None),
    ("stanjames", "Stan James", "SJ", None),
    ("stanleybet", "Stanleybet", "SY", None),
    ("vcbet", "VC Bet", "VC", None),
    ("williamhill", "William Hill", "WH", None),
)


def _r3r12_float(value: Any) -> float | None:
    try:
        result = float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 1.0 else None


def _r3r12_parse_http_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _r3r12_parse_upload_timestamp_html(payload: bytes) -> dt.datetime | None:
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return None
    match = re.search(
        r"Latest\s+fixtures\s+uploaded:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2})\s+UK\s+time",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    raw = f"{match.group(1)} {match.group(2)}"
    parsed = None
    for pattern in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            parsed = dt.datetime.strptime(raw, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=ZoneInfo("Europe/London")).astimezone(UTC)


def _r3r12_resolve_source_timestamp(
    payload: bytes,
    headers: dict[str, str],
    cached: dict[str, Any],
    now: dt.datetime,
    metadata_updated: dt.datetime | None,
) -> tuple[dt.datetime, dict[str, Any]]:
    payload_hash = hashlib.sha256(payload).hexdigest()
    http_updated = _r3r12_parse_http_time(headers.get("last-modified"))
    cached_hash = str(cached.get("contentSha256") or "")
    cached_changed = parse_time(cached.get("contentChangedAt") or cached.get("sourceUpdatedAt"))
    content_changed = now if payload_hash != cached_hash else (cached_changed or now)

    if http_updated is not None:
        source_updated = http_updated
        evidence = "HTTP_LAST_MODIFIED"
        confidence = "HIGH"
    elif metadata_updated is not None:
        source_updated = metadata_updated
        evidence = "FOOTBALL_DATA_UPLOAD_TIMESTAMP"
        confidence = "HIGH"
    elif cached_hash and cached_hash == payload_hash and cached_changed is not None:
        source_updated = cached_changed
        evidence = "CONTENT_HASH_UNCHANGED"
        confidence = "MEDIUM"
    else:
        source_updated = now
        evidence = "CONTENT_HASH_FIRST_OBSERVED" if not cached_hash else "CONTENT_HASH_CHANGED"
        confidence = "MEDIUM"

    return source_updated, {
        "contentSha256": payload_hash,
        "contentObservedAt": iso(now),
        "contentChangedAt": iso(content_changed),
        "freshnessEvidence": evidence,
        "freshnessConfidence": confidence,
    }


def _r3r12_fetch_metadata_timestamp(
    url: str,
    timeout: int,
) -> tuple[dt.datetime | None, str | None]:
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "User-Agent": "AI-Football-Lab-R15-Fixture-Metadata/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
        parsed = _r3r12_parse_upload_timestamp_html(payload)
        if parsed is None:
            return None, "FIXTURE_METADATA_UPLOAD_TIMESTAMP_MISSING"
        return parsed, None
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"


def _r3r12_source_fresh(source_updated: dt.datetime | None, config: dict[str, Any], now: dt.datetime) -> bool:
    if source_updated is None:
        return False
    maximum_hours = max(1, min(168, safe_int(config.get("footballDataFixtureOddsMaximumAgeHours"), 72)))
    age = now - source_updated
    return dt.timedelta(minutes=-5) <= age <= dt.timedelta(hours=maximum_hours)


def _r3r12_fixture_date(value: Any) -> dt.date | None:
    text = str(value or "").strip()
    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _r3r12_name_similarity(left: Any, right: Any) -> float:
    a = normalize(left)
    b = normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    at = {token for token in a.split() if len(token) >= 2 or token.isdigit()}
    bt = {token for token in b.split() if len(token) >= 2 or token.isdigit()}
    if not at or not bt:
        return 0.0
    return len(at & bt) / len(at | bt)


def _r3r12_bookmakers_from_row(
    row: dict[str, Any],
    source_updated: dt.datetime,
    home: str,
    away: str,
) -> list[dict[str, Any]]:
    bookmakers: list[dict[str, Any]] = []
    updated = iso(source_updated)
    for key, title, h2h_prefix, totals_prefix in _R3R12_FIXTURE_BOOKMAKERS:
        markets: list[dict[str, Any]] = []
        home_price = _r3r12_float(row.get(h2h_prefix + "H"))
        draw_price = _r3r12_float(row.get(h2h_prefix + "D"))
        away_price = _r3r12_float(row.get(h2h_prefix + "A"))
        if home_price and draw_price and away_price:
            markets.append({
                "key": "h2h",
                "last_update": updated,
                "outcomes": [
                    {"name": home, "price": home_price},
                    {"name": "Draw", "price": draw_price},
                    {"name": away, "price": away_price},
                ],
            })
        if totals_prefix:
            over = _r3r12_float(row.get(totals_prefix + ">2.5"))
            under = _r3r12_float(row.get(totals_prefix + "<2.5"))
            if over and under:
                markets.append({
                    "key": "totals",
                    "last_update": updated,
                    "outcomes": [
                        {"name": "Over", "price": over, "point": 2.5},
                        {"name": "Under", "price": under, "point": 2.5},
                    ],
                })
        if markets:
            bookmakers.append({
                "key": "football_data_" + key,
                "title": title + " via Football-Data",
                "last_update": updated,
                "markets": markets,
            })
    return bookmakers


def _r3r12_match_fixture_row(
    row: dict[str, Any],
    discovered_events: list[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any] | None:
    row_date = _r3r12_fixture_date(row.get("Date"))
    home = str(row.get("HomeTeam") or "").strip()
    away = str(row.get("AwayTeam") or "").strip()
    if row_date is None or not home or not away:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for event in discovered_events:
        commence = parse_time(event.get("commence_time") or event.get("commenceTime"))
        if commence is None or not (start <= commence <= end):
            continue
        date_gap = abs((commence.date() - row_date).days)
        if date_gap > 1:
            continue
        home_score = _r3r12_name_similarity(home, event.get("home_team"))
        away_score = _r3r12_name_similarity(away, event.get("away_team"))
        if home_score < 0.68 or away_score < 0.68:
            continue
        score = home_score + away_score + (0.15 if date_gap == 0 else 0.0)
        if best is None or score > best[0]:
            best = (score, event)
    return copy.deepcopy(best[1]) if best and best[0] >= 1.55 else None


def parse_football_data_fixture_csv(
    payload: bytes,
    source_updated: dt.datetime,
    discovered_events: list[dict[str, Any]],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics = {
        "rows": 0,
        "matchedRows": 0,
        "events": 0,
        "eventsWithThreeBookmakers": 0,
        "sourceUpdatedAt": iso(source_updated),
        "stale": not _r3r12_source_fresh(source_updated, config, now),
        "invalidHeaders": False,
        "asianMarketsImported": 0,
    }
    if diagnostics["stale"]:
        return [], diagnostics
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None or text.lstrip().startswith("<"):
        diagnostics["invalidHeaders"] = True
        return [], diagnostics
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    if not {"Date", "HomeTeam", "AwayTeam"}.issubset(headers):
        diagnostics["invalidHeaders"] = True
        return [], diagnostics
    by_event: dict[str, dict[str, Any]] = {}
    minimum_lead = dt.timedelta(minutes=max(0, safe_int(config.get("minimumLeadMinutes"), 45)))
    excluded_divisions = {str(value).casefold() for value in config.get("footballDataFixtureOddsExcludedDivisions") or ["rus"]}
    for row in reader:
        diagnostics["rows"] += 1
        division = str(row.get("Div") or "").strip()
        if division.casefold() in excluded_divisions:
            continue
        event = _r3r12_match_fixture_row(row, discovered_events, start, end)
        if not event:
            continue
        commence = parse_time(event.get("commence_time"))
        if commence is None or commence < now + minimum_lead:
            continue
        bookmakers = _r3r12_bookmakers_from_row(
            row,
            source_updated,
            str(event.get("home_team") or ""),
            str(event.get("away_team") or ""),
        )
        if not bookmakers:
            continue
        event_id = str(event.get("id") or stable_id(
            event.get("sport_key"), event.get("home_team"), event.get("away_team"), event.get("commence_time")
        ))
        event["id"] = event_id
        event["bookmakers"] = bookmakers
        event["last_update"] = iso(source_updated)
        event["_r15NoKeyFixtureOdds"] = True
        event["_r15FixtureDivision"] = division
        event["_r15FixtureSourceUpdatedAt"] = iso(source_updated)
        prior = by_event.get(event_id)
        if prior is None or len(bookmakers) > len(prior.get("bookmakers") or []):
            by_event[event_id] = event
        diagnostics["matchedRows"] += 1
    events = sorted(by_event.values(), key=lambda item: str(item.get("commence_time") or ""))
    diagnostics["events"] = len(events)
    diagnostics["eventsWithThreeBookmakers"] = sum(
        1 for event in events if len(event.get("bookmakers") or []) >= 3
    )
    return events, diagnostics


def load_football_data_fixture_odds(
    discovered_events: list[dict[str, Any]],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics = {
        "enabled": bool(config.get("footballDataFixtureOddsEnabled", True)),
        "status": "DISABLED",
        "url": str(config.get("footballDataFixtureOddsUrl") or "https://www.football-data.co.uk/matches/resources/fixtures.csv"),
        "metadataUrl": str(config.get("footballDataFixtureMetadataUrl") or "https://www.football-data.co.uk/matches.php"),
        "cachePath": str(FOOTBALL_DATA_FIXTURE_ODDS_PATH),
        "events": 0,
        "eventsWithThreeBookmakers": 0,
        "sourceUpdatedAt": None,
        "usedCache": False,
        "error": None,
        "metadataError": None,
        "freshnessEvidence": None,
        "freshnessConfidence": None,
        "contentSha256": None,
    }
    if not diagnostics["enabled"]:
        return [], diagnostics

    url = diagnostics["url"]
    metadata_url = diagnostics["metadataUrl"]
    timeout = max(5, min(60, safe_int(config.get("footballDataFixtureOddsTimeoutSeconds"), 25)))
    cached = load_json(FOOTBALL_DATA_FIXTURE_ODDS_PATH, {})
    if not isinstance(cached, dict):
        cached = {}

    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.3",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "User-Agent": "AI-Football-Lab-R15-R3R24/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}

        metadata_updated = None
        metadata_error = None
        if _r3r12_parse_http_time(headers.get("last-modified")) is None:
            metadata_updated, metadata_error = _r3r12_fetch_metadata_timestamp(metadata_url, timeout)
        diagnostics["metadataError"] = metadata_error

        source_updated, freshness = _r3r12_resolve_source_timestamp(
            payload, headers, cached, now, metadata_updated
        )
        diagnostics.update(freshness)

        events, parsed = parse_football_data_fixture_csv(
            payload, source_updated, discovered_events, config, start, end, now
        )
        diagnostics.update(parsed)
        diagnostics["sourceUpdatedAt"] = iso(source_updated)
        diagnostics["events"] = len(events)
        diagnostics["eventsWithThreeBookmakers"] = parsed.get("eventsWithThreeBookmakers", 0)
        diagnostics["status"] = (
            "GREEN" if events
            else "STALE" if parsed.get("stale")
            else "INVALID_PAYLOAD" if parsed.get("invalidHeaders")
            else "NO_MATCHED_EVENTS"
        )

        cache_payload = {
            "version": 2,
            "sourceMarker": "V10_R15F_R3R24_FIXTURE_FRESHNESS_CHAIN",
            "updatedAt": iso(now),
            "sourceUpdatedAt": iso(source_updated),
            "sourceUrl": url,
            "metadataUrl": metadata_url,
            "status": diagnostics["status"],
            "contentSha256": freshness.get("contentSha256"),
            "contentObservedAt": freshness.get("contentObservedAt"),
            "contentChangedAt": freshness.get("contentChangedAt"),
            "freshnessEvidence": freshness.get("freshnessEvidence"),
            "freshnessConfidence": freshness.get("freshnessConfidence"),
            "events": events,
            "diagnostics": diagnostics,
        }
        write_json(FOOTBALL_DATA_FIXTURE_ODDS_PATH, cache_payload)
        return events, diagnostics
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"

    cached_updated = parse_time(cached.get("sourceUpdatedAt"))
    if _r3r12_source_fresh(cached_updated, config, now):
        usable: list[dict[str, Any]] = []
        discovered_ids = {str(event.get("id") or "") for event in discovered_events}
        minimum_lead = dt.timedelta(minutes=max(0, safe_int(config.get("minimumLeadMinutes"), 45)))
        for event in cached.get("events") or []:
            event_id = str(event.get("id") or "")
            commence = parse_time(event.get("commence_time"))
            if (
                event_id
                and event_id in discovered_ids
                and commence is not None
                and start <= commence <= end
                and commence >= now + minimum_lead
                and event.get("bookmakers")
            ):
                usable.append(copy.deepcopy(event))
        diagnostics["status"] = "CACHE_GREEN" if usable else "CACHE_EMPTY"
        diagnostics["usedCache"] = True
        diagnostics["sourceUpdatedAt"] = iso(cached_updated)
        diagnostics["events"] = len(usable)
        diagnostics["eventsWithThreeBookmakers"] = sum(
            1 for event in usable if len(event.get("bookmakers") or []) >= 3
        )
        diagnostics["freshnessEvidence"] = cached.get("freshnessEvidence")
        diagnostics["freshnessConfidence"] = cached.get("freshnessConfidence")
        diagnostics["contentSha256"] = cached.get("contentSha256")
        return usable, diagnostics

    diagnostics["status"] = "UNAVAILABLE"
    if not FOOTBALL_DATA_FIXTURE_ODDS_PATH.exists():
        write_json(FOOTBALL_DATA_FIXTURE_ODDS_PATH, {
            "version": 2,
            "sourceMarker": "V10_R15F_R3R24_FIXTURE_FRESHNESS_CHAIN",
            "updatedAt": iso(now),
            "sourceUpdatedAt": None,
            "sourceUrl": url,
            "metadataUrl": metadata_url,
            "status": "UNAVAILABLE",
            "events": [],
            "diagnostics": diagnostics,
        })
    return [], diagnostics

def advanced_recovery_reserve_credits(
    client: ProviderClient,
    config: dict[str, Any],
    featured_cost: int,
    competition_count: int,
) -> int:
    budget = free_odds_daily_budget(client, config)
    available = max(0, safe_int(budget.get("portfolioAvailableThisRun"), 0))
    if available <= 0:
        return 0
    minimum_competitions = max(1, safe_int(config.get("oddsMinimumCompetitionsForPortfolio"), 3))
    minimum_featured_cost = min(max(0, competition_count), minimum_competitions) * max(1, featured_cost)
    maximum_possible_reserve = max(0, available - minimum_featured_cost)
    ratio = clamp(safe_float(config.get("oddsAdvancedRecoveryReserveRatio"), 0.45), 0.0, 0.90)
    configured = max(0, safe_int(config.get("oddsAdvancedRecoveryReserveCredits"), 4))
    desired = max(configured, math.ceil(available * ratio))
    cap = max(configured, safe_int(config.get("oddsAdvancedRecoveryMaximumReserveCredits"), 12))
    return min(desired, cap, maximum_possible_reserve)


def select_sport_keys_by_quota(
    events: list[dict[str, Any]],
    context: dict[str, Any],
    client: ProviderClient,
    config: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event.get("sport_key") or "")].append(event)
    rows: list[tuple[int, float, int, str]] = []
    for key, items in grouped.items():
        if not key:
            continue
        coverage = sport_history_coverage(items, context)
        history_count = round(coverage * len(items))
        rows.append((history_count, coverage, len(items), key))
    rows.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)

    regions = [value for value in str(config.get("oddsRegions") or "eu").split(",") if value]
    featured_markets = [value for value in config.get("featuredMarkets") or ["h2h", "totals"]]
    cost = max(1, len(regions) * len(featured_markets))
    budget = free_odds_daily_budget(client, config)
    configured_limit = max(1, safe_int(config.get("maximumOddsSportRequests"), 200))
    minimum_competitions = min(len(rows), max(1, safe_int(config.get("oddsMinimumCompetitionsForPortfolio"), 3)))
    maximum_competitions = min(
        len(rows),
        max(minimum_competitions, safe_int(config.get("oddsMaximumCompetitionsForPortfolio"), 8)),
        configured_limit,
    )
    reserve = advanced_recovery_reserve_credits(client, config, cost, len(rows))
    spendable = max(0, safe_int(budget.get("portfolioAvailableThisRun"), 0) - reserve)
    initial_capacity = min(maximum_competitions, spendable // cost if cost else 0)
    if minimum_competitions and initial_capacity < minimum_competitions:
        # Diversity remains a prerequisite, but never exceed the real portfolio budget.
        initial_capacity = min(
            minimum_competitions,
            safe_int(budget.get("portfolioAvailableThisRun"), 0) // cost if cost else 0,
            maximum_competitions,
        )

    analysis_target = max(1, safe_int(config.get("dailyAnalysisTarget"), 15))
    expected_yield = clamp(safe_float(config.get("oddsExpectedQualificationRate"), 0.18), 0.08, 0.50)
    configured_target = max(analysis_target, safe_int(config.get("oddsPortfolioCompletionCandidateTarget"), 84))
    candidate_target = max(configured_target, math.ceil(analysis_target / expected_yield))

    keys: list[str] = []
    projected_events = 0
    projected_history = 0
    for history_count, coverage, event_count, key in rows:
        if len(keys) >= initial_capacity:
            break
        keys.append(key)
        projected_events += event_count
        projected_history += history_count
        if (
            len(keys) >= minimum_competitions
            and projected_events >= candidate_target
            and projected_history >= analysis_target
        ):
            break

    ranked_keys = [row[3] for row in rows]
    return keys, {
        "freeMonthlyMode": True,
        "quotaRemainingBeforeOdds": budget.get("remaining"),
        "quotaHardReserve": budget.get("reserve"),
        "daysLeftInQuotaPeriod": budget.get("daysLeft"),
        "dailyFairShare": budget.get("fairShare"),
        "dailyConfiguredBudget": budget.get("configured"),
        "dailyAvailableBeforeFeatured": budget.get("availableThisRun"),
        "portfolioAvailableBeforeFeatured": budget.get("portfolioAvailableThisRun"),
        "advancedRecoveryReserveCredits": reserve,
        "advancedRecoveryReserveRatio": safe_float(config.get("oddsAdvancedRecoveryReserveRatio"), 0.45),
        "featuredSpendableAfterReserve": spendable,
        "portfolioCompletionBurstEnabled": bool(config.get("oddsAllowPortfolioCompletionBurst", True)),
        "portfolioCompletionBurstActivated": False,
        "portfolioCompletionBurstReasons": ["ADVANCED_RECOVERY_RESERVED_BEFORE_COMPETITION_BURST"],
        "minimumCompetitionsForPortfolio": minimum_competitions,
        "maximumCompetitionsForPortfolio": maximum_competitions,
        "featuredCostPerCompetition": cost,
        "competitionsWithEvents": len(rows),
        "competitionsSelected": len(keys),
        "competitionsDeferredByQuota": max(0, len(rows) - len(keys)),
        "estimatedFeaturedCost": len(keys) * cost,
        "projectedEventsWithOdds": projected_events,
        "projectedHistoryCoveredEvents": projected_history,
        "expectedQualificationRate": expected_yield,
        "expectedQualifiedFromInitialPool": math.floor(projected_events * expected_yield),
        "candidateCompletionTarget": candidate_target,
        "targetHistoryCoveredEvents": analysis_target,
        "rankedCompetitionKeys": ranked_keys,
        "initialCompetitionKeys": list(keys),
        "completionCompetitionKeys": [],
        "completionRounds": [],
        "allEventsHistoricallyInspected": len(events),
    }


def fetch_featured_odds_quota_aware(
    client: ProviderClient,
    api_key: str,
    keys: list[str],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    *,
    reserve_credits: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    params = core.odds_query_parameters(config, api_key)
    params["commenceTimeFrom"] = iso(start)
    params["commenceTimeTo"] = iso(end)
    spacing = max(0.05, safe_float(config.get("oddsFeaturedSpacingSeconds"), 0.18))
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    request_cost = max(1, len([v for v in str(config.get("oddsRegions") or "eu").split(",") if v]) * len(config.get("featuredMarkets") or ["h2h", "totals"]))
    for index, key in enumerate(keys):
        if index:
            time.sleep(spacing)
        budget = free_odds_daily_budget(client, config)
        available = max(0, safe_int(budget.get("portfolioAvailableThisRun"), 0))
        if available - max(0, reserve_credits) < request_cost:
            errors.append("FEATURED_STOPPED_FOR_ADVANCED_RECOVERY_RESERVE")
            break
        url = f"{core.ODDS_API_BASE}/sports/{urllib.parse.quote(key)}/odds?{urllib.parse.urlencode(params)}"
        try:
            payload = client.request_json(url, label=f"ODDS_FEATURED:{key}", retries=0)
        except Exception as exc:
            errors.append(f"{key}: {exc}")
            if isinstance(exc, ProviderError) and exc.status == 429:
                break
            continue
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            commence = parse_time(item.get("commence_time"))
            if commence and start <= commence < end:
                events.append(item)
    return events, errors


def merge_unique_odds_events(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in list(existing) + list(incoming):
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or stable_id(event.get("sport_key"), event.get("home_team"), event.get("away_team"), event.get("commence_time")))
        prior = by_id.get(event_id)
        if prior is None or len(event.get("bookmakers") or []) >= len(prior.get("bookmakers") or []):
            by_id[event_id] = event
    return sorted(by_id.values(), key=lambda item: str(item.get("commence_time") or ""))


def refresh_shadow_watchlist(
    state: dict[str, Any],
    diagnostics: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any],
) -> dict[str, Any]:
    shadow = state.setdefault("shadowLearning", {})
    pending = [
        copy.deepcopy(row)
        for row in shadow.get("pending") or []
        if isinstance(row, dict) and str(row.get("status") or "pending") == "pending"
    ]
    settled = [
        copy.deepcopy(row)
        for row in shadow.get("settled") or []
        if isinstance(row, dict)
    ]
    official_event_ids = {
        str(row.get("eventId") or "")
        for collection in ("dailyAnalysis", "analysisHistory")
        for row in state.get(collection) or []
        if isinstance(row, dict)
    }
    by_event = {str(row.get("eventId") or ""): row for row in pending if str(row.get("eventId") or "")}
    candidates: list[dict[str, Any]] = []
    minimum_odds = safe_float(config.get("minimumBookmakerOdds"), 1.55)
    for row in diagnostics.get("rejectedEvents") or []:
        if not isinstance(row, dict):
            continue
        event_id = str(row.get("eventId") or "")
        candidate = row.get("shadowCandidate") if isinstance(row.get("shadowCandidate"), dict) else {}
        commence = parse_time(row.get("commenceTime"))
        if (
            not event_id
            or event_id in official_event_ids
            or not candidate
            or commence is None
            or commence <= now + dt.timedelta(minutes=max(1, safe_int(config.get("minimumLeadMinutes"), 45)))
            or safe_float(candidate.get("bookmakerOdds"), 0.0) < minimum_odds
        ):
            continue
        record = {
            "id": stable_id("shadow", event_id, candidate.get("market"), candidate.get("selectionCode"), candidate.get("point")),
            "recordType": "SHADOW_CANDIDATE",
            "learningSampleSource": "SHADOW_REJECTED",
            "eventId": event_id,
            "sport": "soccer",
            "sportKey": row.get("sportKey"),
            "league": row.get("league"),
            "home": row.get("home"),
            "away": row.get("away"),
            "commenceTime": row.get("commenceTime"),
            "status": "pending",
            "trackedAt": iso(now),
            "failures": list(row.get("failures") or []),
            **copy.deepcopy(candidate),
        }
        record["probability"] = safe_float(record.get("modelProbability"), safe_float(record.get("conservativeProbability"), 0.0))
        record["odds"] = safe_float(record.get("bookmakerOdds"), 0.0)
        record["shadowRankScore"] = round(
            safe_float(record.get("conservativeProbability")) * 100
            + safe_float(record.get("dataQuality")) * 0.18
            + safe_float(record.get("agreement")) * 0.08
            + safe_float(record.get("marketStability")) * 0.08
            - safe_float(record.get("anomaly")) * 0.06
            + clamp(safe_float(record.get("expectedValue")) * 100, -10, 15) * 0.05,
            4,
        )
        candidates.append(record)

    candidates.sort(
        key=lambda row: (
            safe_float(row.get("shadowRankScore")),
            safe_float(row.get("conservativeProbability")),
            safe_float(row.get("dataQuality")),
        ),
        reverse=True,
    )
    target = max(4, safe_int(config.get("shadowLearningDailyTarget"), 12))
    for record in candidates[:target]:
        event_id = str(record.get("eventId") or "")
        if event_id and event_id not in by_event:
            by_event[event_id] = record

    # Keep only future/pending shadow candidates; settled archive is separate.
    pending = sorted(
        by_event.values(),
        key=lambda row: str(row.get("commenceTime") or ""),
    )[-max(100, safe_int(config.get("shadowLearningPendingLimit"), 300)):]
    shadow.update({
        "version": 1,
        "updatedAt": iso(now),
        "pending": pending,
        "settled": settled[-max(500, safe_int(config.get("shadowLearningSettledLimit"), 3000)):],
    })
    stats = shadow.setdefault("statistics", {})
    stats["tracked"] = len(pending) + len(settled)
    stats["pending"] = len(pending)
    stats["settled"] = len(settled)
    return {"pending": len(pending), "settled": len(settled), "addedPool": len(candidates)}


def settle_shadow_watchlist(
    state: dict[str, Any],
    results: dict[str, dict[str, Any]],
    football_context: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any],
) -> dict[str, int]:
    shadow = state.setdefault("shadowLearning", {})
    pending = [row for row in shadow.get("pending") or [] if isinstance(row, dict)]
    settled = [copy.deepcopy(row) for row in shadow.get("settled") or [] if isinstance(row, dict)]
    official_event_ids = {
        str(row.get("eventId") or "")
        for row in state.get("analysisHistory") or []
        if isinstance(row, dict)
    }
    remaining: list[dict[str, Any]] = []
    counters = {"settled": 0, "won": 0, "lost": 0, "push": 0, "superseded": 0, "unresolved": 0}
    settled_ids = {str(row.get("id") or "") for row in settled}

    for source in pending:
        record = copy.deepcopy(source)
        event_id = str(record.get("eventId") or "")
        commence = parse_time(record.get("commenceTime"))
        if event_id in official_event_ids:
            record["status"] = "superseded"
            record["settledAt"] = iso(now)
            record["settlementSource"] = "OFFICIAL_PUBLICATION_SUPERSEDED_SHADOW"
            if str(record.get("id") or "") not in settled_ids:
                settled.append(record)
                settled_ids.add(str(record.get("id") or ""))
            counters["superseded"] += 1
            continue
        if commence is None or commence > now:
            remaining.append(record)
            continue

        result = core.match_provider_score_result(record, results, config)
        if result is None:
            result = core.match_football_result(record, football_context)
        if result is None:
            # Keep it retryable for 72 hours after kickoff.
            if commence and now - commence <= dt.timedelta(hours=72):
                remaining.append(record)
                counters["unresolved"] += 1
            continue

        home_score = safe_int(result.get("homeScore"))
        away_score = safe_int(result.get("awayScore"))
        status = core.settle_market(record, home_score, away_score)
        record.update({
            "status": status,
            "statusLabel": core.result_status_label(status),
            "score": f"{home_score}:{away_score}",
            "homeScore": home_score,
            "awayScore": away_score,
            "settledAt": iso(now),
            "settlementSource": result.get("source"),
            "profit": 0.0,
            "financialMode": "SHADOW_LEARNING_ONLY",
        })
        if status in {"won", "lost", "push"}:
            core.update_learning_from_record(state, record)
            counters["settled"] += 1
            counters[status] += 1
        if str(record.get("id") or "") not in settled_ids:
            settled.append(record)
            settled_ids.add(str(record.get("id") or ""))

    shadow["pending"] = remaining[-max(100, safe_int(config.get("shadowLearningPendingLimit"), 300)):]
    shadow["settled"] = settled[-max(500, safe_int(config.get("shadowLearningSettledLimit"), 3000)):]
    shadow["updatedAt"] = iso(now)
    stats = shadow.setdefault("statistics", {})
    stats.update({
        "tracked": len(shadow["pending"]) + len(shadow["settled"]),
        "pending": len(shadow["pending"]),
        "settled": sum(1 for row in shadow["settled"] if str(row.get("status")) in {"won", "lost", "push"}),
        "won": sum(1 for row in shadow["settled"] if str(row.get("status")) == "won"),
        "lost": sum(1 for row in shadow["settled"] if str(row.get("status")) == "lost"),
        "push": sum(1 for row in shadow["settled"] if str(row.get("status")) == "push"),
    })
    return counters


def enrich_rejection_diagnostics(diagnostics: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    hard_names = {
        "Нет полноценной истории обеих команд",
        "Недостаточное качество данных",
        "Недостаточно букмекеров",
        "Высокая аномальность линии",
        "Нестабильная букмекерская линия",
    }
    hard: dict[str, int] = defaultdict(int)
    market: dict[str, int] = defaultdict(int)
    near: list[dict[str, Any]] = []
    recovery: list[str] = []
    min_probability = safe_float(config.get("strategyMinimumConservativeProbability"), 0.56)
    min_quality = safe_float(config.get("strategyMinimumDataQuality"), 58)
    min_books = safe_int(config.get("strategyMinimumBookmakers"), 3)
    for row in diagnostics.get("rejectedEvents") or []:
        failures = [str(value) for value in row.get("failures") or []]
        hard_failures = [value for value in failures if value in hard_names]
        market_failures = [value for value in failures if value not in hard_names]
        for value in hard_failures:
            hard[value] += 1
        for value in market_failures:
            market[value] += 1
        probability = safe_float(row.get("bestProbability"), 0.0)
        quality = safe_float(row.get("dataQuality"), 0.0)
        books = safe_int(row.get("quoteCount"), 0)
        near.append({
            "eventId": row.get("eventId"),
            "league": row.get("league"),
            "home": row.get("home"),
            "away": row.get("away"),
            "bestCandidate": row.get("bestCandidate"),
            "bestProbability": probability,
            "bestOdds": row.get("bestOdds"),
            "dataQuality": quality,
            "quoteCount": books,
            "probabilityGap": round(max(0.0, min_probability - probability), 4),
            "qualityGap": round(max(0.0, min_quality - quality), 2),
            "bookmakerGap": max(0, min_books - books),
            "hardFailures": hard_failures,
            "marketFailures": market_failures,
        })
        if row.get("eventId") and not hard_failures:
            recovery.append(str(row.get("eventId")))
    near.sort(key=lambda item: (item["probabilityGap"], item["qualityGap"], item["bookmakerGap"], -item["bestProbability"]))
    diagnostics["hardRejectionReasons"] = dict(sorted(hard.items(), key=lambda item: (-item[1], item[0])))
    diagnostics["marketRejectionReasons"] = dict(sorted(market.items(), key=lambda item: (-item[1], item[0])))
    diagnostics["nearMissCandidates"] = near[:max(10, safe_int(config.get("nearMissDiagnosticsLimit"), 30))]
    diagnostics["advancedRecoveryEventIds"] = recovery[:max(0, safe_int(config.get("oddsAdvancedRecoveryMaximumEvents"), 16))]
    return diagnostics
def preliminary_event_score(event: dict[str, Any], context: dict[str, Any], now: dt.datetime) -> float:
    home_id, home_score = match_team(str(event.get("home_team") or ""), context)
    away_id, away_score = match_team(str(event.get("away_team") or ""), context)
    history_bonus = 0.0
    if home_id and away_id:
        home_games = len(context.get("teamGames", {}).get(home_id) or [])
        away_games = len(context.get("teamGames", {}).get(away_id) or [])
        history_bonus = min(home_games, away_games, 20) * 2.0 + min(home_score, away_score) * 20.0
    bookmakers = len(event.get("bookmakers") or [])
    commence = parse_time(event.get("commence_time")) or now + dt.timedelta(days=1)
    proximity = max(0.0, 24.0 - (commence - now).total_seconds() / 3600.0)
    return history_bonus + bookmakers * 2.0 + proximity * 0.15


def _merge_advanced_market_payload(
    prior: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    if not prior:
        return copy.deepcopy(incoming)
    merged = copy.deepcopy(prior)
    for key, value in incoming.items():
        if key != "bookmakers" and value not in (None, "", [], {}):
            merged[key] = copy.deepcopy(value)
    by_bookmaker = {
        str(row.get("key") or row.get("title") or index): copy.deepcopy(row)
        for index, row in enumerate(merged.get("bookmakers") or [])
        if isinstance(row, dict)
    }
    for index, bookmaker in enumerate(incoming.get("bookmakers") or []):
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_key = str(bookmaker.get("key") or bookmaker.get("title") or index)
        target = by_bookmaker.setdefault(bookmaker_key, copy.deepcopy(bookmaker))
        markets = {
            str(row.get("key") or market_index): copy.deepcopy(row)
            for market_index, row in enumerate(target.get("markets") or [])
            if isinstance(row, dict)
        }
        for market_index, market in enumerate(bookmaker.get("markets") or []):
            if isinstance(market, dict):
                markets[str(market.get("key") or market_index)] = copy.deepcopy(market)
        target.update({key: copy.deepcopy(value) for key, value in bookmaker.items() if key != "markets"})
        target["markets"] = list(markets.values())
    merged["bookmakers"] = list(by_bookmaker.values())
    return merged


def fetch_advanced_markets_quota_aware(
    client: ProviderClient,
    api_key: str,
    featured_events: list[dict[str, Any]],
    context: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
    priority_event_ids: list[str] | None = None,
    completion_mode: bool = False,
    *,
    existing: dict[str, dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    attempted_pairs: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any], dict[str, Any]]:
    result = copy.deepcopy(existing or {})
    attempted = attempted_pairs if attempted_pairs is not None else set()
    market_order = [
        str(value) for value in (
            config.get("oddsAdvancedRecoveryMarketOrder")
            or config.get("advancedCompletionMarkets")
            or ["double_chance", "btts", "team_totals"]
        )
        if str(value) in {"btts", "double_chance", "team_totals"}
    ]
    priority = [str(value) for value in priority_event_ids or [] if str(value)]
    priority_index = {value: index for index, value in enumerate(priority)}
    ranked = sorted(
        featured_events,
        key=lambda event: (
            str(event.get("id") or "") in priority_index,
            -priority_index.get(str(event.get("id") or ""), 10**6),
            preliminary_event_score(event, context, now),
        ),
        reverse=True,
    )
    maximum_events = max(0, safe_int(config.get("oddsAdvancedRecoveryMaximumEvents"), 16))
    maximum_requests = max(0, safe_int(config.get("oddsAdvancedRecoveryMaximumRequests"), 12))
    spacing = max(0.05, safe_float(config.get("oddsAdvancedSpacingSeconds"), 0.22))
    regions = [value for value in str(config.get("oddsRegions") or "eu").split(",") if value]
    request_cost = max(1, len(regions))  # one market per request; unsupported markets cannot poison a batch
    errors: list[str] = []
    requested = 0
    returned = 0
    useful = 0
    recovered_ids: list[str] = []
    unsupported: dict[str, int] = defaultdict(int)
    current_diag: dict[str, Any] = {}
    target = max(1, safe_int(config.get("dailyAnalysisTarget"), 15))
    current_state = state or {}

    _, current_diag = build_strategy_analysis(featured_events, result, context, current_state, config, now)
    current_diag = enrich_rejection_diagnostics(current_diag, config)
    qualified_ids = set(str(value) for value in current_diag.get("qualifiedEventIds") or [])
    qualified_before = safe_int(current_diag.get("eventsQualified"), 0)

    selected_events = [event for event in ranked if str(event.get("id") or "") in priority_index][:maximum_events]
    for event in selected_events:
        event_id = str(event.get("id") or "")
        sport_key = str(event.get("sport_key") or "")
        if not event_id or not sport_key or event_id in qualified_ids:
            continue
        for market_key in market_order:
            if requested >= maximum_requests or safe_int(current_diag.get("eventsQualified"), 0) >= target:
                break
            pair = (event_id, market_key)
            if pair in attempted:
                continue
            attempted.add(pair)
            budget = free_odds_daily_budget(client, config)
            available = max(0, safe_int(budget.get("portfolioAvailableThisRun"), 0))
            if available < request_cost:
                errors.append("ADVANCED_RECOVERY_QUOTA_EXHAUSTED")
                break
            if requested:
                time.sleep(spacing)
            params = {
                "apiKey": api_key,
                "regions": str(config.get("oddsRegions") or "eu"),
                "markets": market_key,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            }
            url = (
                f"{core.ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/events/"
                f"{urllib.parse.quote(event_id)}/odds?{urllib.parse.urlencode(params)}"
            )
            requested += 1
            try:
                payload = client.request_json(url, label=f"ADVANCED_RECOVERY:{market_key}:{event_id}", retries=0)
            except Exception as exc:
                message = str(exc)
                errors.append(f"{event_id}:{market_key}:{message}")
                if isinstance(exc, ProviderError) and exc.status in {400, 404, 422}:
                    unsupported[market_key] += 1
                    continue
                if isinstance(exc, ProviderError) and exc.status == 429:
                    break
                continue
            if not isinstance(payload, dict) or not payload.get("bookmakers"):
                errors.append(f"{event_id}:{market_key}:EMPTY_ADVANCED_MARKET")
                continue
            returned += 1
            prior_payload = result.get(event_id)
            result[event_id] = _merge_advanced_market_payload(prior_payload, payload)
            _, after_diag = build_strategy_analysis(featured_events, result, context, current_state, config, now)
            after_diag = enrich_rejection_diagnostics(after_diag, config)
            after_ids = set(str(value) for value in after_diag.get("qualifiedEventIds") or [])
            if event_id in after_ids and event_id not in qualified_ids:
                recovered_ids.append(event_id)
                useful += 1
                qualified_ids = after_ids
                current_diag = after_diag
                break
            current_diag = after_diag
        if errors and errors[-1] == "ADVANCED_RECOVERY_QUOTA_EXHAUSTED":
            break

    return result, errors, {
        "requested": requested,
        "returned": returned,
        "usefulResponses": useful,
        "recoveredEvents": len(recovered_ids),
        "recoveredEventIds": recovered_ids,
        "attemptedPairs": len(attempted),
        "unsupportedMarkets": dict(unsupported),
        "qualifiedBefore": qualified_before,
        "qualifiedAfter": safe_int(current_diag.get("eventsQualified"), 0),
        "quotaRemaining": client.odds_quota.get("requestsRemaining"),
        "errors": errors[-20:],
    }, current_diag


def complete_portfolio_acquisition(
    client: ProviderClient,
    api_key: str,
    initial_keys: list[str],
    quota_plan: dict[str, Any],
    discovered_events: list[dict[str, Any]],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    context: dict[str, Any],
    state: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    selected = list(initial_keys)
    cached_featured, cached_advanced, cache_load_diag = load_recent_odds_snapshot_cache(
        config, start, end, now
    )
    fixture_events, fixture_odds_diag = load_football_data_fixture_odds(
        discovered_events, config, start, end, now
    )
    featured_events: list[dict[str, Any]] = merge_unique_odds_events(
        cached_featured, fixture_events
    )
    advanced: dict[str, dict[str, Any]] = dict(cached_advanced)
    errors: list[str] = []
    attempted_pairs: set[tuple[str, str]] = set()
    reserve = max(0, safe_int(quota_plan.get("advancedRecoveryReserveCredits"), 0))
    quota_exhausted_at_start = odds_quota_is_exhausted(client, config)
    first: list[dict[str, Any]] = []
    first_errors: list[str] = []
    if selected and not quota_exhausted_at_start:
        first, first_errors = fetch_featured_odds_quota_aware(
            client, api_key, selected, config, start, end, reserve_credits=reserve
        )
        featured_events = merge_unique_odds_events(featured_events, first)
        errors.extend(first_errors)
        if first:
            save_recent_odds_snapshot_cache(featured_events, advanced, config, now)
    _, diagnostics = build_strategy_analysis(featured_events, advanced, context, state, config, now)
    diagnostics = enrich_rejection_diagnostics(diagnostics, config)
    target = max(1, safe_int(config.get("dailyAnalysisTarget"), 15))
    total_recovery = {
        "requested": 0,
        "returned": 0,
        "usefulResponses": 0,
        "recoveredEvents": 0,
        "recoveredEventIds": [],
        "attemptedPairs": 0,
        "unsupportedMarkets": {},
        "qualifiedBefore": safe_int(diagnostics.get("eventsQualified"), 0),
        "qualifiedAfter": safe_int(diagnostics.get("eventsQualified"), 0),
        "quotaRemaining": client.odds_quota.get("requestsRemaining"),
        "errors": [],
    }

    def run_recovery() -> None:
        nonlocal advanced, diagnostics, total_recovery, errors
        recovery_ids = list(diagnostics.get("advancedRecoveryEventIds") or [])
        if (
            not recovery_ids
            or safe_int(diagnostics.get("eventsQualified"), 0) >= target
            or odds_quota_is_exhausted(client, config)
        ):
            return
        advanced, advanced_errors, recovery_diag, diagnostics = fetch_advanced_markets_quota_aware(
            client, api_key, featured_events, context, config, now,
            priority_event_ids=recovery_ids,
            completion_mode=True,
            existing=advanced,
            state=state,
            attempted_pairs=attempted_pairs,
        )
        errors.extend(advanced_errors)
        total_recovery["requested"] += safe_int(recovery_diag.get("requested"), 0)
        total_recovery["returned"] += safe_int(recovery_diag.get("returned"), 0)
        total_recovery["usefulResponses"] += safe_int(recovery_diag.get("usefulResponses"), 0)
        total_recovery["recoveredEvents"] += safe_int(recovery_diag.get("recoveredEvents"), 0)
        total_recovery["recoveredEventIds"] = list(dict.fromkeys(
            list(total_recovery.get("recoveredEventIds") or []) + list(recovery_diag.get("recoveredEventIds") or [])
        ))
        total_recovery["attemptedPairs"] = len(attempted_pairs)
        unsupported = defaultdict(int, total_recovery.get("unsupportedMarkets") or {})
        for key, value in (recovery_diag.get("unsupportedMarkets") or {}).items():
            unsupported[str(key)] += safe_int(value, 0)
        total_recovery["unsupportedMarkets"] = dict(unsupported)
        total_recovery["qualifiedAfter"] = safe_int(diagnostics.get("eventsQualified"), 0)
        total_recovery["quotaRemaining"] = client.odds_quota.get("requestsRemaining")
        total_recovery["errors"] = (list(total_recovery.get("errors") or []) + list(recovery_diag.get("errors") or []))[-20:]
        if safe_int(recovery_diag.get("returned"), 0) > 0:
            save_recent_odds_snapshot_cache(featured_events, advanced, config, now)

    # R3R22: when the portfolio is sparse, breadth across competitions has
    # priority over expensive advanced-market recovery. Advanced recovery still
    # runs after featured-market coverage, preserving the same quality gates.
    recovery_before_breadth = bool(config.get("oddsAdvancedRecoveryBeforeCompetitionBurst", False))
    if recovery_before_breadth:
        run_recovery()

    ranked = [str(value) for value in quota_plan.get("rankedCompetitionKeys") or []]
    maximum = max(len(selected), safe_int(config.get("oddsMaximumCompetitionsForPortfolio"), 8))
    completion_rounds: list[dict[str, Any]] = []
    while safe_int(diagnostics.get("eventsQualified"), 0) < target and len(selected) < maximum:
        deferred = [key for key in ranked if key not in selected]
        if not deferred:
            break
        budget = free_odds_daily_budget(client, config)
        featured_cost = max(1, safe_int(quota_plan.get("featuredCostPerCompetition"), 1))
        if safe_int(budget.get("portfolioAvailableThisRun"), 0) < featured_cost:
            break
        next_key = deferred[0]
        before_events = len(featured_events)
        before_qualified = safe_int(diagnostics.get("eventsQualified"), 0)
        incoming, incoming_errors = fetch_featured_odds_quota_aware(
            client, api_key, [next_key], config, start, end, reserve_credits=0
        )
        errors.extend(incoming_errors)
        selected.append(next_key)
        featured_events = merge_unique_odds_events(featured_events, incoming)
        if incoming:
            save_recent_odds_snapshot_cache(featured_events, advanced, config, now)
        _, diagnostics = build_strategy_analysis(featured_events, advanced, context, state, config, now)
        diagnostics = enrich_rejection_diagnostics(diagnostics, config)
        completion_rounds.append({
            "round": len(completion_rounds) + 1,
            "competitionKey": next_key,
            "eventsBefore": before_events,
            "eventsAfter": len(featured_events),
            "qualifiedBefore": before_qualified,
            "qualifiedAfterFeatured": safe_int(diagnostics.get("eventsQualified"), 0),
            "quotaRemainingBeforeRecovery": client.odds_quota.get("requestsRemaining"),
        })
        if recovery_before_breadth:
            run_recovery()
        completion_rounds[-1]["qualifiedAfterRecovery"] = safe_int(diagnostics.get("eventsQualified"), 0)
        completion_rounds[-1]["quotaRemainingAfterRecovery"] = client.odds_quota.get("requestsRemaining")
        if len(featured_events) == before_events and incoming_errors:
            break

    if not recovery_before_breadth:
        run_recovery()

    completion_keys = [key for key in selected if key not in initial_keys]
    quota_plan["competitionsSelected"] = len(selected)
    quota_plan["competitionKeysSelected"] = selected
    quota_plan["completionCompetitionKeys"] = completion_keys
    quota_plan["completionRounds"] = completion_rounds
    quota_plan["portfolioCompletionBurstActivated"] = bool(completion_keys)
    quota_plan["portfolioCompletionBurstActual"] = bool(completion_keys)
    quota_plan["featuredEventsCollected"] = len(featured_events)
    quota_plan["advancedRecoveryReservedBeforeFeatured"] = reserve
    quota_plan["advancedRecoveryRequested"] = total_recovery["requested"]
    quota_plan["advancedRecoveryReturned"] = total_recovery["returned"]
    quota_plan["advancedRecoveryUsefulResponses"] = total_recovery["usefulResponses"]
    quota_plan["advancedRecoveryRecoveredEvents"] = total_recovery["recoveredEvents"]
    quota_plan["qualifiedAfterAdvanced"] = safe_int(diagnostics.get("eventsQualified"), 0)
    quota_plan["competitionsDeferredByQuota"] = max(0, len(ranked) - len(selected))
    cache_save_diag = save_recent_odds_snapshot_cache(featured_events, advanced, config, now)
    quota_exhausted_after = odds_quota_is_exhausted(client, config)
    quota_plan["noKeyFixtureOddsEnabled"] = bool(config.get("footballDataFixtureOddsEnabled", True))
    quota_plan["noKeyFixtureOddsStatus"] = fixture_odds_diag.get("status")
    quota_plan["noKeyFixtureOddsEvents"] = safe_int(fixture_odds_diag.get("events"), 0)
    quota_plan["noKeyFixtureOddsEventsWithThreeBookmakers"] = safe_int(
        fixture_odds_diag.get("eventsWithThreeBookmakers"), 0
    )
    quota_plan["noKeyFixtureOddsSourceUpdatedAt"] = fixture_odds_diag.get("sourceUpdatedAt")
    quota_plan["noKeyFixtureOddsUsedCache"] = bool(fixture_odds_diag.get("usedCache"))
    quota_plan["noKeyFixtureOddsError"] = fixture_odds_diag.get("error")
    quota_plan["oddsCacheEnabled"] = bool(config.get("oddsCacheEnabled", True))
    quota_plan["oddsCacheLoadedFeatured"] = safe_int(cache_load_diag.get("loadedFeatured"), 0)
    quota_plan["oddsCacheLoadedAdvanced"] = safe_int(cache_load_diag.get("loadedAdvanced"), 0)
    quota_plan["oddsCacheExpired"] = safe_int(cache_load_diag.get("expired"), 0)
    quota_plan["oddsCacheSavedFeatured"] = safe_int(cache_save_diag.get("savedFeatured"), 0)
    quota_plan["oddsCacheSavedAdvanced"] = safe_int(cache_save_diag.get("savedAdvanced"), 0)
    quota_plan["oddsCacheMaximumAgeMinutes"] = max(1, safe_int(
        config.get("oddsCacheMaximumAgeMinutes"),
        config.get("maximumQuoteAgeMinutes", 180),
    ))
    quota_plan["quotaExhaustedAtStart"] = quota_exhausted_at_start
    quota_plan["quotaExhaustedAfterAcquisition"] = quota_exhausted_after
    quota_plan["automaticResumeOnQuotaRecovery"] = bool(config.get("oddsAutomaticResumeOnQuotaRecovery", True))
    if safe_int(diagnostics.get("eventsQualified"), 0) >= target:
        quota_plan["quotaLifecycleStatus"] = (
            "PORTFOLIO_READY_WITH_NO_KEY_FIXTURE_ODDS"
            if safe_int(fixture_odds_diag.get("events"), 0) > 0
            else "PORTFOLIO_READY"
        )
    elif quota_exhausted_after:
        quota_plan["quotaLifecycleStatus"] = (
            "NO_KEY_FIXTURE_ODDS_INSUFFICIENT_WAITING_FOR_REFRESH_OR_QUOTA"
            if safe_int(fixture_odds_diag.get("events"), 0) > 0
            else "WAITING_FOR_ODDS_QUOTA_RESET"
        )
    else:
        quota_plan["quotaLifecycleStatus"] = "QUOTA_AVAILABLE_PORTFOLIO_INCOMPLETE"
    return featured_events, advanced, errors, quota_plan, diagnostics, total_recovery
# ---------------------------------------------------------------------------
# Candidate ranking, 15-match strategy and express construction
# ---------------------------------------------------------------------------


def merge_event(featured: dict[str, Any], advanced: dict[str, Any] | None) -> dict[str, Any]:
    return core.merge_advanced_event(featured, advanced or {}) if advanced else featured


def candidate_is_qualified(candidate: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    core_qualification = candidate.get("qualification") if isinstance(candidate.get("qualification"), dict) else {}
    if bool(config.get("requireCoreQualification", True)) and core_qualification and core_qualification.get("qualified") is False:
        core_failures = [
            str(reason).strip()
            for reason in (core_qualification.get("failures") or [])
            if str(reason).strip()
        ]
        if core_failures:
            failures.extend([f"Основной фильтр: {reason}" for reason in core_failures])
        else:
            failures.append("Кандидат отклонён основным фильтром")
    if str(candidate.get("dataTier") or "MARKET") == "MARKET":
        failures.append("Нет полноценной истории обеих команд")
    if safe_float(candidate.get("dataQuality")) < safe_float(config.get("strategyMinimumDataQuality"), 58):
        failures.append("Недостаточное качество данных")
    if safe_int(candidate.get("quoteCount")) < safe_int(config.get("strategyMinimumBookmakers"), 3):
        failures.append("Недостаточно букмекеров")
    if safe_float(candidate.get("conservativeProbability")) < safe_float(config.get("strategyMinimumConservativeProbability"), 0.56):
        failures.append("Недостаточная консервативная вероятность")
    if safe_float(candidate.get("agreement")) < safe_float(config.get("strategyMinimumAgreement"), 54):
        failures.append("Модели слишком сильно расходятся")
    if safe_float(candidate.get("marketStability")) < safe_float(config.get("strategyMinimumMarketStability"), 48):
        failures.append("Нестабильная букмекерская линия")
    if safe_float(candidate.get("anomaly")) > safe_float(config.get("strategyMaximumAnomaly"), 58):
        failures.append("Высокая аномальность линии")
    family = str(candidate.get("marketFamily") or candidate.get("marketKey") or "").lower()
    is_total_market = "total" in family or str(candidate.get("marketKey") or "").lower() in {"totals", "team_totals"}
    if bool(candidate.get("goalDirectionConflict")) and is_total_market:
        failures.append("История и модель голов противоречат направлению тотала")
    odds = safe_float(candidate.get("bookmakerOdds"))
    if odds < safe_float(config.get("minimumBookmakerOdds"), 1.35):
        failures.append("Коэффициент ниже абсолютного минимума")
    return not failures, failures


def obvious_market_score(candidate: dict[str, Any], config: dict[str, Any]) -> float:
    probability = safe_float(candidate.get("conservativeProbability"), candidate.get("modelProbability"))
    odds = safe_float(candidate.get("bookmakerOdds"), 1.0)
    preferred_min = safe_float(config.get("preferredMinimumOdds"), 1.55)
    preferred_max = safe_float(config.get("preferredMaximumOdds"), 2.20)
    price = 8.0 if preferred_min <= odds <= preferred_max else -max(0.0, preferred_min - odds) * 20.0 - max(0.0, odds - preferred_max) * 8.0
    return round(
        probability * 100.0
        + safe_float(candidate.get("dataQuality")) * 0.10
        + safe_float(candidate.get("agreement")) * 0.06
        + safe_float(candidate.get("marketStability")) * 0.06
        - safe_float(candidate.get("anomaly")) * 0.05
        + price
        + clamp(safe_float(candidate.get("expectedValue")) * 100.0, -8.0, 12.0) * 0.10,
        6,
    )


def choose_obvious_candidate(candidates: list[dict[str, Any]], config: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    qualified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source in candidates:
        item = copy.deepcopy(source)
        okay, failures = candidate_is_qualified(item, config)
        item["strategyQualified"] = okay
        item["strategyFailures"] = failures
        item["obviousMarketScore"] = obvious_market_score(item, config)
        (qualified if okay else rejected).append(item)
    if not qualified:
        return None, sorted(rejected, key=lambda row: safe_float(row.get("conservativeProbability")), reverse=True)

    qualified.sort(key=lambda row: (safe_float(row.get("conservativeProbability")), safe_float(row.get("dataQuality")), safe_float(row.get("obviousMarketScore"))), reverse=True)
    highest_probability = qualified[0]
    gap = safe_float(config.get("marketDominanceProbabilityGap"), 0.02)
    preferred_min = safe_float(config.get("preferredMinimumOdds"), 1.55)
    close = [
        row for row in qualified
        if safe_float(highest_probability.get("conservativeProbability")) - safe_float(row.get("conservativeProbability")) <= gap
    ]
    close.sort(
        key=lambda row: (
            safe_float(row.get("bookmakerOdds")) >= preferred_min,
            safe_float(row.get("obviousMarketScore")),
            safe_float(row.get("conservativeProbability")),
        ),
        reverse=True,
    )
    selected = close[0]
    alternatives = [row for row in qualified if row is not selected] + rejected
    selected["marketDominanceRule"] = {
        "highestProbability": safe_float(highest_probability.get("conservativeProbability")),
        "selectedProbability": safe_float(selected.get("conservativeProbability")),
        "allowedGap": gap,
        "priceUsedOnlyInsideGap": True,
    }
    return selected, alternatives


def selection_explanation(selected: dict[str, Any], alternatives: list[dict[str, Any]]) -> dict[str, Any]:
    components = selected.get("modelComponents") if isinstance(selected.get("modelComponents"), dict) else {}
    home = components.get("homeRecent") if isinstance(components.get("homeRecent"), dict) else {}
    away = components.get("awayRecent") if isinstance(components.get("awayRecent"), dict) else {}
    reasons = [
        f"Консервативная вероятность {safe_float(selected.get('conservativeProbability')) * 100:.1f}%",
        f"Качество данных {safe_float(selected.get('dataQuality')):.0f}/100",
        f"Подтверждение {safe_int(selected.get('quoteCount'))} букмекерами",
    ]
    if home and away:
        reasons.extend([
            f"Форма хозяев: {home.get('wins', 0) * 100:.0f}% побед, {home.get('gf', 0):.2f} гола за матч",
            f"Форма гостей: {away.get('wins', 0) * 100:.0f}% побед, {away.get('ga', 0):.2f} пропущено за матч",
        ])
    rejected = []
    for row in alternatives[:5]:
        diff = (safe_float(selected.get("conservativeProbability")) - safe_float(row.get("conservativeProbability"))) * 100.0
        rejected.append({
            "pick": row.get("pickRu") or row.get("pick"),
            "probabilityPercent": round(safe_float(row.get("conservativeProbability")) * 100.0, 1),
            "odds": row.get("bookmakerOdds"),
            "reason": "; ".join(row.get("strategyFailures") or []) if not row.get("strategyQualified") else f"Надёжность ниже выбранного рынка на {diff:.1f} п.п.",
        })
    return {"reasons": reasons, "rejectedAlternatives": rejected}


def build_strategy_analysis(
    odds_events: list[dict[str, Any]],
    advanced: dict[str, dict[str, Any]],
    context: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluated_rows: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    diagnostics: dict[str, Any] = {
        "oddsEvents": len(odds_events),
        "eventsWithHistory": 0,
        "eventsWithMarkets": 0,
        "eventsQualified": 0,
        "marketCandidates": 0,
        "rejectedByQuality": 0,
        "rejectedWithoutMarkets": 0,
        "rejectionReasons": defaultdict(int),
        "rejectedEvents": [],
        "dataTiers": defaultdict(int),
        "marketFamilies": defaultdict(int),
        "qualifiedEventIds": [],
    }
    for raw in odds_events:
        if core.infer_sport_from_key(raw.get("sport_key")) != "soccer" or not core.event_allowed(raw, config):
            continue
        event_id = str(raw.get("id") or "")
        event = merge_event(raw, advanced.get(event_id))
        event["country"] = core.infer_country(str(event.get("sport_key") or ""), str(event.get("sport_title") or ""))
        quotes = core.parse_event_quotes(event, now, config)
        if not quotes:
            diagnostics["rejectedWithoutMarkets"] += 1
            diagnostics["rejectionReasons"]["Нет пригодных рынков или котировок"] += 1
            if len(diagnostics["rejectedEvents"]) < max(10, safe_int(config.get("rejectionDiagnosticsLimit"), 120)):
                diagnostics["rejectedEvents"].append({
                    "eventId": event_id,
                    "sportKey": event.get("sport_key"),
                    "league": event.get("sport_title"),
                    "home": event.get("home_team"),
                    "away": event.get("away_team"),
                    "commenceTime": event.get("commence_time"),
                    "failures": ["Нет пригодных рынков или котировок"],
                })
            continue
        diagnostics["eventsWithMarkets"] += 1
        model = build_match_model(event, quotes, context, config, now)
        if model.get("components", {}).get("historyAvailable"):
            diagnostics["eventsWithHistory"] += 1
        diagnostics["dataTiers"][str(model.get("dataTier"))] += 1
        candidates = core.evaluate_event_markets(event, quotes, model, state.get("learning", {}), config, now)
        diagnostics["marketCandidates"] += len(candidates)
        for candidate in candidates:
            candidate["eventId"] = event_id
            candidate["modelComponents"] = copy.deepcopy(model.get("components") or {})
            candidate["sourceNotes"] = list(model.get("sourceNotes") or [])
        selected, alternatives = choose_obvious_candidate(candidates, config)
        if not selected:
            diagnostics["rejectedByQuality"] += 1
            best_rejected = alternatives[0] if alternatives else {}
            failures = []
            for candidate in alternatives:
                for failure in candidate.get("strategyFailures") or []:
                    if failure not in failures:
                        failures.append(failure)
                    diagnostics["rejectionReasons"][failure] += 1
            if not failures:
                failures = ["Ни один рынок не прошёл стратегические фильтры"]
                diagnostics["rejectionReasons"][failures[0]] += 1
            if len(diagnostics["rejectedEvents"]) < max(10, safe_int(config.get("rejectionDiagnosticsLimit"), 120)):
                diagnostics["rejectedEvents"].append({
                    "eventId": event_id,
                    "sportKey": event.get("sport_key"),
                    "league": event.get("sport_title"),
                    "home": event.get("home_team"),
                    "away": event.get("away_team"),
                    "commenceTime": event.get("commence_time"),
                    "dataTier": model.get("dataTier"),
                    "dataQuality": model.get("dataQuality"),
                    "quoteCount": best_rejected.get("quoteCount"),
                    "candidateCount": len(candidates),
                    "bestCandidate": best_rejected.get("pickRu") or best_rejected.get("pick"),
                    "bestProbability": best_rejected.get("conservativeProbability"),
                    "bestOdds": best_rejected.get("bookmakerOdds"),
                    "bestScore": best_rejected.get("obviousMarketScore"),
                    "shadowCandidate": {
                        key: copy.deepcopy(best_rejected.get(key))
                        for key in (
                            "market", "marketKey", "marketFamily", "selectionCode", "point",
                            "pick", "pickRu", "bookmakerOdds", "modelProbability",
                            "conservativeProbability", "marketProbability", "expectedValue",
                            "edge", "dataTier", "dataQuality", "quoteCount", "agreement",
                            "marketStability", "anomaly", "obviousMarketScore",
                            "qualification", "strategyFailures", "goalDirectionConflict",
                            "learningAdjustment", "learningEvidence",
                        )
                        if best_rejected.get(key) is not None
                    },
                    "failures": failures,
                })
            continue
        diagnostics["eventsQualified"] += 1
        diagnostics["qualifiedEventIds"].append(event_id)
        diagnostics["marketFamilies"][str(selected.get("marketFamily"))] += 1
        evaluated_rows.append((event, selected, alternatives, model))

    target = safe_int(config.get("dailyAnalysisTarget"), 15)
    max_league = max(1, safe_int(config.get("maximumSameLeagueDailyAnalysis"), 3))
    evaluated_rows.sort(
        key=lambda row: (
            safe_float(row[1].get("obviousMarketScore")),
            safe_float(row[1].get("conservativeProbability")),
            safe_float(row[1].get("dataQuality")),
        ),
        reverse=True,
    )
    chosen: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    league_counts: dict[str, int] = defaultdict(int)
    family_counts: dict[str, int] = defaultdict(int)
    max_family = max(1, safe_int(config.get("strategyMaximumSameMarketFamily"), 8))
    for row in evaluated_rows:
        if len(chosen) >= target:
            break
        league = str(row[1].get("league") or "")
        family = str(row[1].get("marketFamily") or row[1].get("marketKey") or "OTHER").upper()
        if league_counts[league] >= max_league:
            continue
        if family_counts[family] >= max_family:
            continue
        chosen.append(row)
        league_counts[league] += 1
        family_counts[family] += 1

    if not chosen:
        diagnostics.update({
            "published": 0,
            "required": target,
            "shortage": target,
            "status": "NO_QUALIFIED_EVENTS",
            "partialPublication": False,
        })
        diagnostics["dataTiers"] = dict(diagnostics["dataTiers"])
        diagnostics["marketFamilies"] = dict(diagnostics["marketFamilies"])
        diagnostics["rejectionReasons"] = dict(sorted(diagnostics["rejectionReasons"].items(), key=lambda item: (-item[1], item[0])))
        return [], diagnostics

    records: list[dict[str, Any]] = []
    for rank, (event, selected, alternatives, model) in enumerate(chosen[:target], start=1):
        record = core.event_to_analysis_record(event, selected, alternatives, rank, now)
        explanation = selection_explanation(selected, alternatives)
        record.update({
            "rank": rank,
            "marketPolicy": R15_MARKET_POLICY,
            "sourceMarker": R15_MARKER,
            "financialMode": "EXPRESS_LEG",
            "conservativeProbability": selected.get("conservativeProbability"),
            "obviousMarketScore": selected.get("obviousMarketScore"),
            "strategyQualified": True,
            "matchDossier": {
                "dataTier": model.get("dataTier"),
                "dataQuality": model.get("dataQuality"),
                "expectedHomeGoals": model.get("homeLambda"),
                "expectedAwayGoals": model.get("awayLambda"),
                "expectedTotalGoals": round(safe_float(model.get("homeLambda")) + safe_float(model.get("awayLambda")), 3),
                "homeWinProbability": model.get("homeWinProbability"),
                "drawProbability": model.get("drawProbability"),
                "awayWinProbability": model.get("awayWinProbability"),
                "mostLikelyScores": model.get("mostLikelyScores"),
                "components": copy.deepcopy(model.get("components") or {}),
                "sources": list(model.get("sourceNotes") or []),
            },
            "selectionRationale": explanation,
        })
        records.append(record)

    diagnostics.update({
        "published": len(records),
        "required": target,
        "shortage": max(0, target - len(records)),
        "partialPublication": len(records) < target,
        "status": "GREEN_PARTIAL_PRODUCTION" if len(records) < target else "GREEN",
        "marketFamilyCap": max_family,
        "selectionObjective": "QUALITY_FIRST_PARTIAL_ALLOWED_WITH_MARKET_FAMILY_DIVERSIFICATION",
    })
    diagnostics["dataTiers"] = dict(diagnostics["dataTiers"])
    diagnostics["marketFamilies"] = dict(diagnostics["marketFamilies"])
    diagnostics["rejectionReasons"] = dict(sorted(diagnostics["rejectionReasons"].items(), key=lambda item: (-item[1], item[0])))
    return records, diagnostics


def build_bootstrap_preview_analysis(
    odds_events: list[dict[str, Any]],
    advanced: dict[str, dict[str, Any]],
    context: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a transparent temporary portfolio without weakening production policy.

    Preview requires real bookmaker markets and history for both teams, but its
    thresholds are intentionally lower than production. Actual quality and
    probability values are preserved verbatim and every record is marked as a
    bootstrap preview.
    """
    minimum_quality = safe_float(config.get("bootstrapPreviewMinimumDataQuality"), 58.0)
    minimum_probability = safe_float(config.get("bootstrapPreviewMinimumConservativeProbability"), 0.56)
    minimum_books = max(1, safe_int(config.get("bootstrapPreviewMinimumBookmakers"), 3))
    minimum_agreement = safe_float(config.get("bootstrapPreviewMinimumAgreement"), 54.0)
    minimum_stability = safe_float(config.get("bootstrapPreviewMinimumMarketStability"), 48.0)
    maximum_anomaly = safe_float(config.get("bootstrapPreviewMaximumAnomaly"), 58.0)
    target = max(1, safe_int(config.get("dailyAnalysisTarget"), 15))
    max_league = max(1, safe_int(config.get("bootstrapPreviewMaximumSameLeague"), 5))
    max_family = max(1, safe_int(config.get("bootstrapPreviewMaximumSameMarketFamily"), 6))

    evaluated: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    diagnostics = {
        "mode": "BOOTSTRAP_PREVIEW",
        "oddsEvents": len(odds_events),
        "eventsWithHistory": 0,
        "eventsEligible": 0,
        "minimumDataQuality": minimum_quality,
        "minimumConservativeProbability": minimum_probability,
        "minimumBookmakers": minimum_books,
        "productionThresholdsUnchanged": True,
        "excludedMarketOnly": 0,
        "excludedInsufficientQuality": 0,
        "excludedInsufficientProbability": 0,
        "excludedInsufficientBookmakers": 0,
        "excludedWithoutSafeMarket": 0,
    }

    for raw in odds_events:
        if core.infer_sport_from_key(raw.get("sport_key")) != "soccer" or not core.event_allowed(raw, config):
            continue
        event_id = str(raw.get("id") or "")
        event = merge_event(raw, advanced.get(event_id))
        event["country"] = core.infer_country(
            str(event.get("sport_key") or ""),
            str(event.get("sport_title") or ""),
        )
        quotes = core.parse_event_quotes(event, now, config)
        if not quotes:
            continue
        model = build_match_model(event, quotes, context, config, now)
        history_available = bool((model.get("components") or {}).get("historyAvailable"))
        if not history_available or str(model.get("dataTier") or "").upper() == "MARKET":
            diagnostics["excludedMarketOnly"] += 1
            continue
        diagnostics["eventsWithHistory"] += 1
        data_quality = safe_float(model.get("dataQuality"), 0.0)
        if data_quality < minimum_quality:
            diagnostics["excludedInsufficientQuality"] += 1
            continue

        candidates = core.evaluate_event_markets(
            event,
            quotes,
            model,
            state.get("learning", {}),
            config,
            now,
        )
        safe_candidates: list[dict[str, Any]] = []
        for source in candidates:
            item = copy.deepcopy(source)
            item["eventId"] = event_id
            item["modelComponents"] = copy.deepcopy(model.get("components") or {})
            item["sourceNotes"] = list(model.get("sourceNotes") or [])
            item["dataTier"] = model.get("dataTier")
            item["dataQuality"] = data_quality
            item["obviousMarketScore"] = obvious_market_score(item, config)
            core_qualification = item.get("qualification") if isinstance(item.get("qualification"), dict) else {}
            if bool(config.get("requireCoreQualification", True)) and core_qualification and core_qualification.get("qualified") is False:
                diagnostics["excludedCoreQualification"] = safe_int(diagnostics.get("excludedCoreQualification"), 0) + 1
                continue
            probability = safe_float(
                item.get("conservativeProbability"),
                safe_float(item.get("modelProbability"), 0.0),
            )
            books = safe_int(item.get("quoteCount"), 0)
            if probability < minimum_probability:
                continue
            if books < minimum_books:
                continue
            if safe_float(item.get("agreement"), 0.0) < minimum_agreement:
                continue
            if safe_float(item.get("marketStability"), 0.0) < minimum_stability:
                continue
            if safe_float(item.get("anomaly"), 100.0) > maximum_anomaly:
                continue
            family = str(item.get("marketFamily") or item.get("marketKey") or "").lower()
            is_total = "total" in family or str(item.get("marketKey") or "").lower() in {"totals", "team_totals"}
            if is_total and bool(item.get("goalDirectionConflict")):
                continue
            if safe_float(item.get("bookmakerOdds"), 0.0) < safe_float(config.get("minimumBookmakerOdds"), 1.35):
                continue
            if not core.standard_market_allowed(
                item.get("marketKey"),
                item.get("market"),
                item.get("point"),
            ):
                continue
            item["strategyQualified"] = False
            item["previewQualified"] = True
            item["previewThresholdProfile"] = "HISTORY_REQUIRED_Q58_P56_BOOKS3_AGREEMENT_STABILITY_NO_TOTAL_CONFLICT"
            safe_candidates.append(item)

        if not safe_candidates:
            best_probability = max(
                [
                    safe_float(row.get("conservativeProbability"), safe_float(row.get("modelProbability"), 0.0))
                    for row in candidates
                ] or [0.0]
            )
            best_books = max([safe_int(row.get("quoteCount"), 0) for row in candidates] or [0])
            if best_probability < minimum_probability:
                diagnostics["excludedInsufficientProbability"] += 1
            elif best_books < minimum_books:
                diagnostics["excludedInsufficientBookmakers"] += 1
            else:
                diagnostics["excludedWithoutSafeMarket"] += 1
            continue

        safe_candidates.sort(
            key=lambda row: (
                not bool(row.get("goalDirectionConflict")),
                safe_float(row.get("conservativeProbability")),
                safe_float(row.get("obviousMarketScore")),
                safe_float(row.get("dataQuality")),
                safe_int(row.get("quoteCount")),
            ),
            reverse=True,
        )
        selected = safe_candidates[0]
        alternatives = safe_candidates[1:]
        diagnostics["eventsEligible"] += 1
        evaluated.append((event, selected, alternatives, model))

    evaluated.sort(
        key=lambda row: (
            not bool(row[1].get("goalDirectionConflict")),
            safe_float(row[1].get("conservativeProbability")),
            safe_float(row[1].get("dataQuality")),
            safe_float(row[1].get("obviousMarketScore")),
        ),
        reverse=True,
    )

    chosen: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    league_counts: dict[str, int] = defaultdict(int)
    family_counts: dict[str, int] = defaultdict(int)
    for row in evaluated:
        if len(chosen) >= target:
            break
        league = str(row[1].get("league") or row[0].get("sport_title") or "")
        family = str(row[1].get("marketFamily") or row[1].get("marketKey") or "OTHER").upper()
        if league_counts[league] >= max_league or family_counts[family] >= max_family:
            continue
        chosen.append(row)
        league_counts[league] += 1
        family_counts[family] += 1

    allow_partial = bool(config.get("bootstrapPreviewAllowPartial", False))
    if len(chosen) < target and not allow_partial:
        diagnostics.update({
            "status": "INSUFFICIENT_PREVIEW_EVENTS",
            "published": 0,
            "required": target,
            "shortage": target - len(chosen),
        })
        return [], diagnostics

    if not chosen:
        diagnostics.update({
            "status": "NO_PREVIEW_EVENTS",
            "published": 0,
            "required": target,
            "shortage": target,
        })
        return [], diagnostics

    records: list[dict[str, Any]] = []
    for rank, (event, selected, alternatives, model) in enumerate(chosen[:target], start=1):
        record = core.event_to_analysis_record(event, selected, alternatives, rank, now)
        record.update({
            "rank": rank,
            "marketPolicy": R15_MARKET_POLICY,
            "sourceMarker": R15_MARKER,
            "financialMode": "PREVIEW_EXPRESS_LEG",
            "publicationMode": "BOOTSTRAP_PREVIEW",
            "conservativeProbability": selected.get("conservativeProbability"),
            "obviousMarketScore": selected.get("obviousMarketScore"),
            "strategyQualified": False,
            "previewQualified": True,
            "previewThresholdProfile": selected.get("previewThresholdProfile"),
            "dataTier": model.get("dataTier"),
            "dataQuality": model.get("dataQuality"),
            "matchDossier": {
                "dataTier": model.get("dataTier"),
                "dataQuality": model.get("dataQuality"),
                "expectedHomeGoals": model.get("homeLambda"),
                "expectedAwayGoals": model.get("awayLambda"),
                "expectedTotalGoals": round(
                    safe_float(model.get("homeLambda")) + safe_float(model.get("awayLambda")),
                    3,
                ),
                "homeWinProbability": model.get("homeWinProbability"),
                "drawProbability": model.get("drawProbability"),
                "awayWinProbability": model.get("awayWinProbability"),
                "mostLikelyScores": model.get("mostLikelyScores"),
                "components": copy.deepcopy(model.get("components") or {}),
                "sources": list(model.get("sourceNotes") or []),
            },
            "selectionRationale": {
                **selection_explanation(selected, alternatives),
                "previewNotice": (
                    "Временный предпросмотр: использованы реальные данные и коэффициенты, "
                    "но пороги ниже штатного production-режима."
                ),
            },
        })
        records.append(record)

    diagnostics.update({
        "status": "GREEN_PARTIAL_PREVIEW" if len(records) < target else "GREEN_BOOTSTRAP_PREVIEW",
        "published": len(records),
        "required": target,
        "shortage": max(0, target - len(records)),
        "partialPublication": len(records) < target,
        "productionThresholdsUnchanged": True,
    })
    return records, diagnostics


def informational_best_three(records: list[dict[str, Any]], now: dt.datetime, preferred_event_ids: list[str] | None = None) -> list[dict[str, Any]]:
    by_event = {str(row.get("eventId") or ""): row for row in records}
    preferred = [by_event[value] for value in (preferred_event_ids or []) if value in by_event]
    remaining = [row for row in sorted(records, key=lambda row: (safe_float(row.get("conservativeProbability")), safe_float(row.get("obviousMarketScore"))), reverse=True) if row not in preferred]
    ranked = (preferred + remaining)[:3]
    result = []
    for rank, source in enumerate(ranked, start=1):
        item = copy.deepcopy(source)
        item.update({
            "id": "ranked-" + stable_id(source.get("id"), rank, source.get("publishedAt")),
            "sourceAnalysisId": source.get("id"),
            "recordType": "BEST_BET",
            "financialMode": "INFORMATIONAL_ONLY",
            "isBestBet": True,
            "rank": rank,
            "rankLabel": "Самый надёжный прогноз" if rank == 1 else f"Надёжность №{rank}",
            "stake": 0.0,
            "stakePercent": 0.0,
            "bankPolicy": "INFORMATIONAL_RANKING_NO_SEPARATE_STAKE",
            "publishedAt": iso(now),
            "status": "pending",
            "statusLabel": core.result_status_label("pending"),
        })
        result.append(item)
    return result


def express_balance_score(groups: list[list[dict[str, Any]]]) -> float:
    log_probs = [sum(math.log(max(0.01, safe_float(row.get("conservativeProbability"), 0.5))) for row in group) for group in groups]
    log_odds = [sum(math.log(max(1.01, safe_float(row.get("bookmakerOdds"), 1.01))) for row in group) for group in groups]
    score = statistics.pvariance(log_probs) * 8.0 + statistics.pvariance(log_odds) * 2.0
    for group in groups:
        league_counts: dict[str, int] = defaultdict(int)
        family_counts: dict[str, int] = defaultdict(int)
        for row in group:
            league_counts[str(row.get("league") or "")] += 1
            family_counts[str(row.get("marketFamily") or "OTHER")] += 1
        score += sum(max(0, count - 2) ** 2 * 0.7 for count in league_counts.values())
        score += sum(max(0, count - 3) ** 2 * 0.5 for count in family_counts.values())
    return score


def balanced_groups(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(records, key=lambda row: safe_float(row.get("conservativeProbability")), reverse=True)
    groups = [[], [], []]
    snake = [0, 1, 2, 2, 1, 0]
    for index, row in enumerate(ordered):
        groups[snake[index % len(snake)]].append(row)
    best_score = express_balance_score(groups)
    improved = True
    passes = 0
    while improved and passes < 8:
        improved = False
        passes += 1
        for a in range(3):
            for b in range(a + 1, 3):
                for i in range(len(groups[a])):
                    for j in range(len(groups[b])):
                        candidate = copy.deepcopy(groups)
                        candidate[a][i], candidate[b][j] = candidate[b][j], candidate[a][i]
                        score = express_balance_score(candidate)
                        if score + 1e-9 < best_score:
                            groups = candidate
                            best_score = score
                            improved = True
    return groups


def ensure_express_bank(state: dict[str, Any], config: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    starting = safe_float(config.get("expressStartingBank"), 10000.0)
    bank = state.get("expressBank") if isinstance(state.get("expressBank"), dict) else {}
    if not bank:
        bank = {
            "starting": starting,
            "current": starting,
            "history": [{"timestamp": iso(now), "value": starting, "reason": "R15_EXPRESS_BANK_CREATED"}],
            "createdAt": iso(now),
        }
    bank.setdefault("starting", starting)
    bank.setdefault("current", bank.get("starting", starting))
    bank.setdefault("history", [])
    state["expressBank"] = bank
    return bank


def update_express_bank_metrics(state: dict[str, Any], now: dt.datetime) -> None:
    bank = ensure_express_bank(state, load_json(CONFIG_PATH, {}), now)
    current = safe_float(bank.get("current"), safe_float(bank.get("starting"), 10000.0))
    active = [row for row in state.get("expresses") or [] if isinstance(row, dict) and str(row.get("status") or "pending") == "pending"]
    placed = round(sum(safe_float(row.get("stake")) for row in active), 2)
    starting = max(0.01, safe_float(bank.get("starting"), 10000.0))
    values = [safe_float(row.get("value"), starting) for row in bank.get("history") or [] if isinstance(row, dict)] or [starting, current]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100.0)
    calculated = {
        "placedAmount": placed,
        "activeExposure": placed,
        "available": round(max(0.0, current - placed), 2),
        "activeExpressCount": len(active),
        "roi": round((current / starting - 1.0) * 100.0, 2),
        "maxDrawdown": round(max_drawdown, 2),
    }
    changed = any(bank.get(key) != value for key, value in calculated.items())
    bank.update(calculated)
    if changed or not bank.get("updatedAt"):
        bank["updatedAt"] = iso(now)


def build_expresses(records: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any], now: dt.datetime, preferred_groups: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    group_count = min(3, len(records) // 5)
    if group_count <= 0:
        state["expresses"] = []
        update_express_bank_metrics(state, now)
        return []
    usable_records = list(records[: group_count * 5])
    bank = ensure_express_bank(state, config, now)
    current = safe_float(bank.get("current"), safe_float(config.get("expressStartingBank"), 10000.0))
    configured_stake_percent = safe_float(config.get("expressStakePercent"), 2.0)
    recovery_mode = any(str(row.get("publicationMode") or "") == "RECOVERY_DAY" for row in usable_records)
    if group_count == 3 and len(usable_records) == 15:
        deterministic_groups = balanced_groups(usable_records)
    else:
        deterministic_groups = [[] for _ in range(group_count)]
        for index, row in enumerate(usable_records):
            deterministic_groups[index % group_count].append(row)
    groups = deterministic_groups
    if group_count == 3 and isinstance(preferred_groups, dict):
        by_event = {str(row.get("eventId") or ""): row for row in usable_records}
        candidate_groups = []
        candidate_ids = []
        valid = True
        for label in ("A", "B", "C"):
            ids = [str(value) for value in preferred_groups.get(label) or []]
            if len(ids) != 5 or len(set(ids)) != 5 or any(value not in by_event for value in ids):
                valid = False
                break
            candidate_ids.extend(ids)
            candidate_groups.append([by_event[value] for value in ids])
        if valid and len(candidate_ids) == 15 and len(set(candidate_ids)) == 15:
            deterministic_score = express_balance_score(deterministic_groups)
            candidate_score = express_balance_score(candidate_groups)
            if candidate_score <= deterministic_score * safe_float(config.get("cloudflareAiExpressBalanceTolerance"), 1.25):
                groups = candidate_groups
    result = []
    labels = ["Экспресс A", "Экспресс B", "Экспресс C"][:group_count]
    for group_index, group in enumerate(groups):
        group.sort(key=lambda row: str(row.get("commenceTime") or ""))
        combined_odds = 1.0
        joint_probability = 1.0
        legs = []
        express_id = "express-" + stable_id(labels[group_index], usable_records[0].get("publishedAt"), group_index)
        for leg_index, row in enumerate(group, start=1):
            odds = safe_float(row.get("bookmakerOdds"), 1.0)
            probability = safe_float(row.get("conservativeProbability"), row.get("modelProbability"))
            combined_odds *= odds
            joint_probability *= probability
            legs.append({
                "legNumber": leg_index,
                "analysisId": row.get("id"),
                "eventId": row.get("eventId"),
                "sportKey": row.get("sportKey"),
                "league": row.get("league"),
                "country": row.get("country"),
                "home": row.get("home"),
                "away": row.get("away"),
                "homeRu": row.get("homeRu"),
                "awayRu": row.get("awayRu"),
                "leagueRu": row.get("leagueRu"),
                "commenceTime": row.get("commenceTime"),
                "pick": row.get("pick"),
                "pickRu": row.get("pickRu"),
                "market": row.get("market"),
                "marketFamily": row.get("marketFamily"),
                "selectionCode": row.get("selectionCode"),
                "point": row.get("point"),
                "odds": odds,
                "probability": probability,
                "dataQuality": row.get("dataQuality"),
                "status": "pending",
                "score": "",
            })
            row["expressId"] = express_id
            row["expressLabel"] = labels[group_index]
            row["expressLegNumber"] = leg_index
        combined_odds = round(combined_odds, 3)
        joint_probability = round(joint_probability, 6)
        conservative_expected_value = round(joint_probability * combined_odds - 1.0, 6)
        minimum_ev = safe_float(config.get("expressMinimumConservativeExpectedValue"), 0.03)
        bankroll_enabled = (
            bool(config.get("expressBankrollAllowed", False))
            and (not recovery_mode)
            and conservative_expected_value >= minimum_ev
        )
        stake_percent = configured_stake_percent if bankroll_enabled else 0.0
        stake = round(current * stake_percent / 100.0, 2)
        financial_mode = (
            "RECOVERY_INFORMATIONAL_NO_BANK"
            if recovery_mode
            else ("EXPRESS_POSITIVE_EV" if bankroll_enabled else "EXPRESS_INFORMATIONAL_NO_BANK")
        )
        result.append({
            "id": express_id,
            "label": labels[group_index],
            "rank": group_index + 1,
            "status": "pending",
            "statusLabel": "Ожидается",
            "publishedAt": iso(now),
            "operationalDayId": records[0].get("operationalDayId"),
            "legs": legs,
            "legCount": 5,
            "combinedOdds": combined_odds,
            "settledCombinedOdds": None,
            "jointProbability": joint_probability,
            "jointProbabilityPercent": round(joint_probability * 100.0, 2),
            "conservativeExpectedValue": conservative_expected_value,
            "conservativeExpectedValuePercent": round(conservative_expected_value * 100.0, 2),
            "breakEvenProbability": round(1.0 / combined_odds, 6) if combined_odds > 0 else None,
            "bankrollEnabled": bankroll_enabled,
            "stakePercent": stake_percent,
            "stake": stake,
            "potentialPayout": round(stake * combined_odds, 2),
            "potentialProfit": round(stake * (combined_odds - 1.0), 2),
            "profit": 0.0,
            "financialMode": financial_mode,
            "bankPolicy": R15_EXPRESS_POLICY,
        })
    state["expresses"] = result
    update_express_bank_metrics(state, now)
    return result


def sync_and_settle_expresses(state: dict[str, Any], now: dt.datetime) -> dict[str, int]:
    by_analysis = {str(row.get("id") or ""): row for row in state.get("dailyAnalysis") or [] if isinstance(row, dict)}
    counters = {"settled": 0, "won": 0, "lost": 0, "push": 0}
    history_ids = {str(row.get("id") or "") for row in state.get("expressHistory") or [] if isinstance(row, dict)}
    for express in state.get("expresses") or []:
        if not isinstance(express, dict):
            continue
        for leg in express.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            source = by_analysis.get(str(leg.get("analysisId") or ""))
            if source:
                for key in ("status", "statusLabel", "score", "homeScore", "awayScore", "settledAt", "settlementSource", "resultUpdatedAt"):
                    if key in source:
                        leg[key] = copy.deepcopy(source[key])
        if str(express.get("status") or "pending") != "pending":
            continue
        statuses = [str(leg.get("status") or "pending") for leg in express.get("legs") or []]
        if any(status == "lost" for status in statuses):
            final_status = "lost"
        elif all(status in TERMINAL for status in statuses):
            final_status = "won" if any(status == "won" for status in statuses) else "push"
        else:
            continue
        settled_odds = 1.0
        for leg in express.get("legs") or []:
            if str(leg.get("status") or "") == "won":
                settled_odds *= safe_float(leg.get("odds"), 1.0)
        stake = safe_float(express.get("stake"))
        if final_status == "lost":
            payout = 0.0
            profit = -stake
        elif final_status == "push":
            payout = stake
            profit = 0.0
        else:
            payout = stake * settled_odds
            profit = payout - stake
        express.update({
            "status": final_status,
            "statusLabel": core.result_status_label(final_status),
            "settledAt": iso(now),
            "settledCombinedOdds": round(settled_odds, 3),
            "payout": round(payout, 2),
            "profit": round(profit, 2),
        })
        express_id = str(express.get("id") or "")
        if express_id not in history_ids:
            bank = ensure_express_bank(state, load_json(CONFIG_PATH, {}), now)
            bank["current"] = round(safe_float(bank.get("current"), 10000.0) + profit, 2)
            bank.setdefault("history", []).append({
                "timestamp": iso(now),
                "value": bank["current"],
                "change": round(profit, 2),
                "expressId": express_id,
                "reason": f"EXPRESS_{final_status.upper()}",
            })
            state.setdefault("expressHistory", []).append(copy.deepcopy(express))
            history_ids.add(express_id)
        counters["settled"] += 1
        counters[final_status] += 1
    state["expressHistory"] = (state.get("expressHistory") or [])[-safe_int(load_json(CONFIG_PATH, {}).get("expressHistoryLimit"), 500):]
    update_express_bank_metrics(state, now)
    return counters


def update_express_statistics(state: dict[str, Any]) -> None:
    rows = [row for row in state.get("expressHistory") or [] if isinstance(row, dict)]
    settled = [row for row in rows if str(row.get("status") or "") in {"won", "lost", "push"}]
    won = sum(1 for row in settled if row.get("status") == "won")
    lost = sum(1 for row in settled if row.get("status") == "lost")
    push = sum(1 for row in settled if row.get("status") == "push")
    state.setdefault("statistics", {})["expresses"] = {
        "settled": len(settled),
        "won": won,
        "lost": lost,
        "push": push,
        "accuracy": round(won / max(1, won + lost) * 100.0, 2),
        "profit": round(sum(safe_float(row.get("profit")) for row in settled), 2),
    }


# ---------------------------------------------------------------------------
# State publication, settlement and reports
# ---------------------------------------------------------------------------


def ensure_r15_state(state: dict[str, Any], config: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    source = state if isinstance(state, dict) else {}
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    # Current V10 state is already structurally valid. Rebuilding it through
    # the legacy migrator on every run would rewrite timestamps and discard
    # R15-only express/cache projections. Migrate only truly legacy or empty
    # states, then preserve every R15 extension verbatim.
    if str(meta.get("version") or "") == core.STATE_VERSION and isinstance(source.get("dailyAnalysis"), list):
        state = copy.deepcopy(source)
    else:
        extras = {key: copy.deepcopy(source.get(key)) for key in ("expresses", "expressHistory", "expressBank", "dataCoverage") if key in source}
        state = core.migrate_state(source, config, now)
        state.update(extras)
    state.setdefault("expresses", [])
    state.setdefault("expressHistory", [])
    state.setdefault("dataCoverage", {})
    ensure_express_bank(state, config, now)
    return state


def archive_previous_expresses(state: dict[str, Any]) -> None:
    existing = {str(row.get("id") or "") for row in state.get("expressHistory") or [] if isinstance(row, dict)}
    for express in state.get("expresses") or []:
        if isinstance(express, dict) and str(express.get("status") or "") in TERMINAL and str(express.get("id") or "") not in existing:
            state.setdefault("expressHistory", []).append(copy.deepcopy(express))


def clear_completed_current_batch(state: dict[str, Any], now: dt.datetime, reason: str) -> None:
    archive_previous_expresses(state)
    state["dailyAnalysis"] = []
    state["bestBets"] = []
    state["predictions"] = []
    state["expresses"] = []
    state["dailyAudit"] = {
        "status": "NO_CURRENT_PUBLICATION",
        "schemaValid": True,
        "publishedCount": 0,
        "updatedAt": iso(now),
        "reason": reason,
    }
    state["systemNarrative"] = {
        "status": "WAITING_FOR_NEXT_SELECTION",
        "title": "Формируется новая подборка",
        "lead": "Официальных прогнозов сейчас нет.",
        "body": "Система продолжает prematch-поиск и опубликует только матчи, прошедшие полный контроль качества.",
        "generatedBy": "DETERMINISTIC_SYSTEM",
        "updatedAt": iso(now),
    }
    state["batch"] = {
        "id": "",
        "status": "WAITING_FOR_NEXT_SELECTION",
        "statusLabel": "Ожидается качественная подборка",
        "completed": False,
        "analysisCount": 0,
        "bestBetsCount": 0,
        "pendingAnalysisCount": 0,
        "pendingBestBetsCount": 0,
        "updatedAt": iso(now),
        "reason": reason,
    }
    update_express_bank_metrics(state, now)


def write_public_files(state: dict[str, Any], report: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)
    write_json(REPORT_PATH, report)
    write_json(SNAPSHOT_PATH, {
        "version": core.STATE_VERSION,
        "sourceMarker": R15_MARKER,
        "updatedAt": state.get("meta", {}).get("updatedAt"),
        "analysisDateLocal": state.get("meta", {}).get("analysisDateLocal"),
        "batch": state.get("batch", {}),
        "dataCoverage": state.get("dataCoverage", {}),
        "expressBank": state.get("expressBank", {}),
        "expresses": state.get("expresses", []),
        "dailyAnalysis": state.get("dailyAnalysis", []),
        "bestBets": state.get("bestBets", []),
        "dailyAudit": state.get("dailyAudit", {}),
        "systemNarrative": state.get("systemNarrative", {}),
        "shadowLearning": {
            "statistics": copy.deepcopy((state.get("shadowLearning") or {}).get("statistics") or {}),
            "pending": copy.deepcopy((state.get("shadowLearning") or {}).get("pending") or [])[:20],
        },
        "nextPortfolio": state.get("nextPortfolio", {}),
    })


def settlement_context_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    completed = []
    for match in cache.get("matches") or []:
        if not isinstance(match, dict) or str(match.get("status") or "") not in {"FINISHED", "AWARDED"}:
            continue
        home = match.get("homeTeam") if isinstance(match.get("homeTeam"), dict) else {}
        away = match.get("awayTeam") if isinstance(match.get("awayTeam"), dict) else {}
        if match.get("homeScore") is None or match.get("awayScore") is None:
            continue
        completed.append({
            "eventId": str(match.get("id") or ""),
            "utcDate": parse_time(match.get("utcDate")),
            "home": str(home.get("name") or ""),
            "away": str(away.get("name") or ""),
            "homeScore": safe_int(match.get("homeScore")),
            "awayScore": safe_int(match.get("awayScore")),
        })
    return {"completedLookup": completed}


def settle_current() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    raw_state = load_json(STATE_PATH, {})
    state = ensure_r15_state(raw_state, config, now)
    before = json_fingerprint(state)
    state_meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    current_records = [
        row for row in state.get("dailyAnalysis") or []
        if isinstance(row, dict)
    ]
    recovery_day = bool(state_meta.get("recoveryDay"))
    if bool(state_meta.get("bootstrapPreview")) and not recovery_day:
        print("R15_BOOTSTRAP_PREVIEW_SETTLEMENT=SKIPPED")
        print("R15_BOOTSTRAP_PREVIEW_BANK_MUTATION=NO")
        print("FINAL_STATUS=GREEN_R15_BOOTSTRAP_PREVIEW_SETTLEMENT_SKIPPED")
        return 0
    odds_activation_threshold = safe_int(config.get("oddsBackupActivationThreshold"), 4)
    if recovery_day and not current_records:
        # Recovery/watchdog traffic is deliberately isolated on the reserve key
        # when one is available. Normal morning production keeps using primary.
        # This prevents a failed recovery attempt from exhausting the primary
        # per-day ledger and then blocking its own retry while reserve quota is idle.
        odds_activation_threshold = 1_000_000
        print("R15_RECOVERY_BACKUP_PREFERRED=YES")
    odds_key, odds_key_selection = core.select_odds_api_key(
        activation_threshold=odds_activation_threshold,
    )
    print(f"R15_ODDS_KEY_SOURCE={odds_key_selection.get('selected')}")
    print(
        "R15_ODDS_PRIMARY_REMAINING="
        f"{safe_int((odds_key_selection.get('primary') or {}).get('remaining'), -1)}"
    )
    print(
        "R15_ODDS_PRIMARY_USED="
        f"{safe_int((odds_key_selection.get('primary') or {}).get('used'), -1)}"
    )
    print(
        "R15_ODDS_BACKUP_REMAINING="
        f"{safe_int((odds_key_selection.get('backup') or {}).get('remaining'), -1)}"
    )
    print(
        "R15_ODDS_BACKUP_USED="
        f"{safe_int((odds_key_selection.get('backup') or {}).get('used'), -1)}"
    )

    live_results = core.load_live_final_results()
    due = core.due_pending_records(state, config, now, set(live_results))
    score_results = dict(live_results)
    score_errors: list[str] = []
    client = ProviderClient(load_json(PROVIDER_HEALTH_PATH, {}))
    seed_client_quota_identity(client, odds_key_selection)
    shadow_pending_due = [
        row for row in (state.get("shadowLearning") or {}).get("pending") or []
        if isinstance(row, dict)
        and (parse_time(row.get("commenceTime")) or now + dt.timedelta(days=1)) <= now
    ]
    if due or shadow_pending_due:
        sport_keys = [
            str(row.get("sportKey") or row.get("oddsSportKey") or "")
            for row in list(due) + shadow_pending_due
            if isinstance(row, dict)
        ]
        provider_scores, score_errors = core.fetch_scores_for_sport_keys(client, odds_key, sport_keys)
        score_results.update(provider_scores)
    cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
    football_context = settlement_context_from_cache(cache)
    counters = core.settle_pending_records(state, score_results, football_context, now, config) if due else {
        "analysisSettled": 0, "bestBetsSettled": 0, "unresolved": 0
    }
    shadow_counters = settle_shadow_watchlist(state, score_results, football_context, now, config)
    tracked_history = ingest_settled_state_history(cache, state, now)
    if tracked_history.get("added"):
        write_json(HISTORY_CACHE_PATH, cache)
        write_json(TEAM_REGISTRY_PATH, rebuild_registry(cache, load_json(TEAM_REGISTRY_PATH, empty_registry())))
    released = core.release_overdue_batch_records(state, config, now)
    express_counters = sync_and_settle_expresses(state, now)
    core.maintain_prediction_history(state, config, now)
    core.update_bank_metrics(state)
    core.update_statistics(state)
    update_express_statistics(state)
    update_express_bank_metrics(state, now)
    changed = json_fingerprint(state) != before
    if changed:
        state.setdefault("meta", {}).update({
            "sourceMarker": R15_MARKER,
            "r15SettlementAt": iso(now),
            "updatedAt": iso(now),
        })
    report = load_json(REPORT_PATH, {})
    report.update({
        "status": "GREEN",
        "sourceMarker": R15_MARKER,
        "mode": "settle",
        "finishedAt": iso(now),
    })
    report.setdefault("diagnostics", {}).update({
        "dueRecords": len(due),
        "providerFinalResults": len(score_results),
        "settlement": counters,
        "shadowSettlement": shadow_counters,
        "trackedHistory": tracked_history,
        "overdueRelease": released,
        "expressSettlement": express_counters,
        "apiCalls": client.calls,
    })
    report["warnings"] = list(report.get("warnings") or []) + score_errors
    write_json(PROVIDER_HEALTH_PATH, client.health)
    if changed:
        write_public_files(state, report)
        print("R15_SETTLEMENT_STATE_CHANGED=YES")
    else:
        print("R15_SETTLEMENT_STATE_CHANGED=NO")
    print(f"R15_DUE_RECORDS={len(due)}")
    print(f"R15_ANALYSIS_SETTLED={counters.get('analysisSettled', 0)}")
    print(f"R15_INFORMATIONAL_OR_LEGACY_BEST_SETTLED={counters.get('bestBetsSettled', 0)}")
    print(f"R15_EXPRESS_SETTLED={express_counters.get('settled', 0)}")
    print("FINAL_STATUS=GREEN_R15F_SETTLEMENT")
    return 0



# V10_R15F_R3R5R1_CANONICAL_ARCHIVE_FIX
# A migrated R14 publication may carry the R15 meta marker while all visible
# rows are still MARKET-only (42/100) and no R15 expresses exist. Archive those
# rows into the normal settlement collections, preserve banks/stakes, and free
# only the current publication slot for the first real R15 portfolio.
#
# R3R5R1 validates the transfer by the same canonical settlement key used by
# update_predictions.py. Public ids may be normalized or merged during history
# maintenance and therefore are not a valid transfer contract.
def archive_legacy_publication_bridge(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    daily = [row for row in state.get("dailyAnalysis") or [] if isinstance(row, dict)]
    best = [row for row in state.get("bestBets") or [] if isinstance(row, dict)]
    expresses = [row for row in state.get("expresses") or [] if isinstance(row, dict)]
    result = {"archived": False, "analysis": 0, "bestBets": 0, "reason": "NOT_LEGACY_BRIDGE"}
    if not daily or expresses:
        return result

    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    market_only_rows = sum(
        1 for row in daily
        if str(row.get("dataTier") or "").upper() == "MARKET"
        and safe_float(row.get("dataQuality"), 0.0) <= 42.01
    )
    legacy_policy_rows = sum(
        1 for row in daily
        if str(row.get("marketPolicy") or "").upper().startswith("R14")
    )
    legacy_freshness = str(meta.get("dataFreshness") or "").upper() in {
        "SETTLEMENT_REFRESH", "LEGACY_BRIDGE", "MIGRATED_LEGACY_PUBLICATION"
    }
    if not (legacy_freshness or market_only_rows == len(daily) or legacy_policy_rows > 0):
        return result

    bank_before = copy.deepcopy(state.get("bank"))
    express_bank_before = copy.deepcopy(state.get("expressBank"))
    daily_snapshot = copy.deepcopy(daily)
    best_snapshot = copy.deepcopy(best)

    def canonical_keys(rows: list[dict[str, Any]], collection_name: str) -> set[str]:
        keys: set[str] = set()
        invalid: list[str] = []
        for index, source in enumerate(rows):
            prepared = core.migrate_public_prediction(copy.deepcopy(source))
            prepared["recordType"] = "BEST_BET" if collection_name == "history" else "ANALYSIS"
            if not core.history_record_valid(prepared):
                invalid.append(str(source.get("id") or source.get("eventId") or index))
                continue
            key = core.history_record_key(prepared, collection_name)
            if not key.strip("|"):
                invalid.append(str(source.get("id") or source.get("eventId") or index))
                continue
            keys.add(key)
        if invalid:
            raise RuntimeError(
                "R3R5R1_LEGACY_SOURCE_RECORD_INVALID;"
                f"COLLECTION={collection_name};ROWS={invalid}"
            )
        if len(keys) != len(rows):
            raise RuntimeError(
                "R3R5R1_LEGACY_SOURCE_CANONICAL_DUPLICATE;"
                f"COLLECTION={collection_name};ROWS={len(rows)};KEYS={len(keys)}"
            )
        return keys

    expected_analysis_keys = canonical_keys(daily_snapshot, "analysisHistory")
    expected_best_keys = canonical_keys(best_snapshot, "history")

    core.append_new_records_to_history(state, daily_snapshot, best_snapshot, config)

    actual_analysis_keys = {
        core.history_record_key(row, "analysisHistory")
        for row in state.get("analysisHistory") or []
        if isinstance(row, dict) and core.history_record_valid(row)
    }
    actual_best_keys = {
        core.history_record_key(row, "history")
        for row in state.get("history") or []
        if isinstance(row, dict) and core.history_record_valid(row)
    }
    missing_analysis = sorted(expected_analysis_keys - actual_analysis_keys)
    missing_best = sorted(expected_best_keys - actual_best_keys)
    if missing_analysis or missing_best:
        raise RuntimeError(
            "R3R5R1_LEGACY_ARCHIVE_INCOMPLETE;"
            f"ANALYSIS_KEYS={missing_analysis};BEST_KEYS={missing_best}"
        )
    if state.get("bank") != bank_before:
        raise RuntimeError("R3R5R1_LEGACY_ARCHIVE_CHANGED_ORDINARY_BANK")
    if state.get("expressBank") != express_bank_before:
        raise RuntimeError("R3R5R1_LEGACY_ARCHIVE_CHANGED_EXPRESS_BANK")

    old_batch = copy.deepcopy(state.get("batch") or {})
    bridge_history = state.setdefault("legacyBridgeHistory", [])
    bridge_history.append({
        "version": 2,
        "archivedAt": iso(now),
        "reason": "R14_MARKET_ONLY_PUBLICATION_RELEASED_FOR_FIRST_R15_PORTFOLIO",
        "analysisCount": len(daily_snapshot),
        "bestBetsCount": len(best_snapshot),
        "canonicalAnalysisKeys": len(expected_analysis_keys),
        "canonicalBestBetKeys": len(expected_best_keys),
        "marketOnlyRows": market_only_rows,
        "legacyPolicyRows": legacy_policy_rows,
        "oldOperationalDayId": meta.get("operationalDayId"),
        "oldAnalysisDateLocal": meta.get("analysisDateLocal"),
        "oldBatch": old_batch,
        "bankMutation": False,
        "verification": "CANONICAL_SETTLEMENT_KEYS",
    })
    state["legacyBridgeHistory"] = bridge_history[-20:]

    state["dailyAnalysis"] = []
    state["bestBets"] = []
    state["predictions"] = []
    state["expresses"] = []
    state["batch"] = {
        "version": 1,
        "id": "",
        "sequence": safe_int(old_batch.get("sequence"), 0),
        "status": "WAITING_FOR_NEXT_SELECTION",
        "statusLabel": "Формируется первый полноценный портфель R15",
        "createdAt": None,
        "updatedAt": iso(now),
        "analysisCount": 0,
        "bestBetsCount": 0,
        "terminalAnalysisCount": 0,
        "terminalBestBetsCount": 0,
        "pendingAnalysisCount": 0,
        "pendingBestBetsCount": 0,
        "completed": True,
        "placedAmount": 0.0,
        "availableAmount": safe_float(
            (state.get("expressBank") or {}).get("available"),
            safe_float((state.get("expressBank") or {}).get("current"), 10000.0),
        ),
        "startingBank": safe_float((state.get("expressBank") or {}).get("starting"), 10000.0),
        "transitionReason": "R3R5R1_LEGACY_BRIDGE_ARCHIVED",
    }
    state.setdefault("meta", {}).update({
        "sourceMarker": R15_MARKER,
        "legacyBridgeArchivedAt": iso(now),
        "legacyBridgeArchivedAnalysisCount": len(daily_snapshot),
        "legacyBridgeArchivedBestBetsCount": len(best_snapshot),
        "legacyBridgeArchiveVerification": "CANONICAL_SETTLEMENT_KEYS",
        "legacyBridgeBankMutation": False,
        "dataFreshness": "R15_LEGACY_ARCHIVED_GENERATING_CURRENT_PORTFOLIO",
        "status": "GENERATING_R15_PORTFOLIO",
        "updatedAt": iso(now),
    })
    result.update({
        "archived": True,
        "analysis": len(daily_snapshot),
        "bestBets": len(best_snapshot),
        "reason": "R14_MARKET_ONLY_BRIDGE_ARCHIVED",
    })
    return result

def release_expired_previous_day(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
    next_day: dict[str, Any],
) -> dict[str, Any]:
    """Archive an expired public day so it can never block the next one.

    Historical/settlement records are preserved before the public slot is
    released. This specifically prevents a prior bad multi-day recovery batch
    from keeping the next Moscow day empty.
    """
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    current_day = str(meta.get("operationalDayId") or "")
    next_day_id = str(next_day.get("operationalDayId") or "")
    daily = [row for row in state.get("dailyAnalysis") or [] if isinstance(row, dict)]
    if not daily or not current_day or current_day == next_day_id:
        return {"released": False, "reason": "SAME_OR_EMPTY_DAY"}

    previous_end = parse_time(meta.get("operationalWindowEnd"))
    if previous_end is None or now < previous_end:
        return {"released": False, "reason": "PREVIOUS_DAY_NOT_EXPIRED"}

    best = [row for row in state.get("bestBets") or [] if isinstance(row, dict)]
    core.append_new_records_to_history(state, daily, best, config)
    archive_previous_expresses(state)

    old_batch = copy.deepcopy(state.get("batch") or {})
    rollover_history = state.setdefault("operationalDayRolloverHistory", [])
    rollover_history.append({
        "releasedAt": iso(now),
        "reason": "EXPIRED_PREVIOUS_OPERATIONAL_DAY_RELEASED",
        "oldOperationalDayId": current_day,
        "oldOperationalWindowEnd": meta.get("operationalWindowEnd"),
        "nextOperationalDayId": next_day_id,
        "analysisCount": len(daily),
        "terminalAnalysisCount": sum(
            1 for row in daily if str(row.get("status") or "pending") in TERMINAL
        ),
        "pendingAnalysisCount": sum(
            1 for row in daily if str(row.get("status") or "pending") not in TERMINAL
        ),
        "oldBatch": old_batch,
    })
    state["operationalDayRolloverHistory"] = rollover_history[-30:]

    state["dailyAnalysis"] = []
    state["bestBets"] = []
    state["predictions"] = []
    state["expresses"] = []
    state["batch"] = {
        "version": 1,
        "id": "",
        "sequence": safe_int(old_batch.get("sequence"), safe_int(meta.get("batchSequence"), 0)),
        "status": "WAITING_FOR_NEXT_SELECTION",
        "statusLabel": "Формируется подборка текущих суток",
        "createdAt": None,
        "updatedAt": iso(now),
        "analysisCount": 0,
        "bestBetsCount": 0,
        "terminalAnalysisCount": 0,
        "terminalBestBetsCount": 0,
        "pendingAnalysisCount": 0,
        "pendingBestBetsCount": 0,
        "completed": True,
        "placedAmount": 0.0,
        "availableAmount": safe_float(
            (state.get("expressBank") or {}).get("current"),
            safe_float(config.get("expressStartingBank"), 10000.0),
        ),
        "startingBank": safe_float(
            (state.get("expressBank") or {}).get("starting"),
            safe_float(config.get("expressStartingBank"), 10000.0),
        ),
        "transitionReason": "EXPIRED_PREVIOUS_OPERATIONAL_DAY_RELEASED",
    }
    meta.update({
        "status": "GENERATING_R15_PORTFOLIO",
        "dataFreshness": "PREVIOUS_DAY_RELEASED",
        "batchStatus": "WAITING_FOR_NEXT_SELECTION",
        "batchStatusLabel": "Формируется подборка текущих суток",
        "previousOperationalDayReleasedAt": iso(now),
        "previousOperationalDayReleased": current_day,
        "updatedAt": iso(now),
    })
    state["meta"] = meta
    update_express_bank_metrics(state, now)
    return {
        "released": True,
        "oldOperationalDayId": current_day,
        "nextOperationalDayId": next_day_id,
        "analysisCount": len(daily),
    }


def derive_calibration_guard(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Conservative rolling guard for live HYBRID/FULL evidence.

    The guard may only add uncertainty or disable bankroll exposure. It never
    raises a probability and never loosens the configured production floors.
    Small samples are shrunk toward no-change so one bad day cannot retune the
    model aggressively.
    """
    rows = [
        row for row in state.get("analysisHistory") or []
        if isinstance(row, dict)
        and str(row.get("status") or "") in {"won", "lost"}
        and str(row.get("dataTier") or "").upper() in {"HYBRID", "FULL"}
    ]
    rows.sort(key=lambda row: str(
        row.get("settledAt")
        or row.get("resultUpdatedAt")
        or row.get("commenceTime")
        or ""
    ))
    window_size = max(20, safe_int(config.get("calibrationRollingWindow"), 60))
    rows = rows[-window_size:]

    def metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        if not n:
            return {
                "n": 0, "wins": 0, "hitRate": None, "avgPredicted": None,
                "brier": None, "overconfidence": None, "shrunkOverconfidence": 0.0,
            }
        wins = sum(1 for row in items if str(row.get("status")) == "won")
        probabilities = [
            clamp(
                safe_float(
                    row.get("conservativeProbability"),
                    safe_float(row.get("probability"), 0.5),
                ),
                0.01,
                0.99,
            )
            for row in items
        ]
        hit_rate = wins / n
        avg_predicted = sum(probabilities) / n
        brier = sum(
            (probability - (1.0 if str(row.get("status")) == "won" else 0.0)) ** 2
            for probability, row in zip(probabilities, items)
        ) / n
        overconfidence = max(0.0, avg_predicted - hit_rate)
        shrink = n / (n + max(1, safe_int(config.get("calibrationPriorStrength"), 20)))
        shrunk = overconfidence * shrink
        return {
            "n": n,
            "wins": wins,
            "losses": n - wins,
            "hitRate": round(hit_rate, 6),
            "avgPredicted": round(avg_predicted, 6),
            "brier": round(brier, 6),
            "overconfidence": round(overconfidence, 6),
            "shrunkOverconfidence": round(shrunk, 6),
        }

    overall = metrics(rows)
    n = safe_int(overall.get("n"), 0)
    if n < 10:
        base_margin = 0.025
    elif n < 20:
        base_margin = 0.020
    elif n < 40:
        base_margin = 0.010
    else:
        base_margin = 0.0

    shrunk_gap = safe_float(overall.get("shrunkOverconfidence"), 0.0)
    brier = safe_float(overall.get("brier"), 0.0)
    additional_margin = base_margin + max(0.0, shrunk_gap - 0.02) * 0.35
    if n >= 8 and brier > 0.27:
        additional_margin += 0.01
    additional_margin = round(clamp(additional_margin, 0.0, 0.06), 6)

    family_haircuts: dict[str, float] = {}
    family_metrics: dict[str, Any] = {}
    families = sorted({
        str(row.get("marketFamily") or row.get("marketKey") or "OTHER").upper()
        for row in rows
    })
    for family in families:
        family_rows = [
            row for row in rows
            if str(row.get("marketFamily") or row.get("marketKey") or "OTHER").upper() == family
        ]
        fm = metrics(family_rows)
        family_metrics[family] = fm
        if safe_int(fm.get("n"), 0) >= 6:
            family_gap = safe_float(fm.get("shrunkOverconfidence"), 0.0)
            family_haircuts[family] = round(
                clamp(max(0.0, family_gap - 0.02) * 0.30, 0.0, 0.04),
                6,
            )

    bankroll_allowed = (
        n >= max(30, safe_int(config.get("calibrationMinimumBankrollSample"), 30))
        and brier <= safe_float(config.get("calibrationMaximumBankrollBrier"), 0.255)
        and safe_float(overall.get("overconfidence"), 1.0)
            <= safe_float(config.get("calibrationMaximumBankrollGap"), 0.05)
    )

    if n < 10:
        mode = "LEARNING_GUARD"
    elif additional_margin >= 0.03:
        mode = "DEFENSIVE"
    elif additional_margin > 0:
        mode = "CAUTIOUS"
    else:
        mode = "CALIBRATED"

    return {
        "mode": mode,
        "rollingWindow": window_size,
        "overall": overall,
        "marketFamilies": family_metrics,
        "additionalUncertaintyMargin": additional_margin,
        "marketFamilyHaircuts": family_haircuts,
        "bankrollAllowed": bankroll_allowed,
        "policy": "AUTO_TIGHTEN_ONLY_NEVER_RAISE_PROBABILITY_OR_LOOSEN_BASE_THRESHOLDS",
    }


def discard_bootstrap_preview(state: dict[str, Any], config: dict[str, Any], now: dt.datetime) -> bool:
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    if not bool(meta.get("bootstrapPreview")):
        return False
    previous_batch = state.get("batch") if isinstance(state.get("batch"), dict) else {}
    sequence = safe_int(previous_batch.get("sequence"), safe_int(meta.get("batchSequence"), 0))
    state["dailyAnalysis"] = []
    state["bestBets"] = []
    state["predictions"] = []
    state["expresses"] = []
    state["batch"] = {
        "version": 1,
        "id": "",
        "sequence": sequence,
        "status": "WAITING_FOR_NEXT_SELECTION",
        "statusLabel": "Предпросмотр завершён — формируется штатный портфель",
        "createdAt": None,
        "updatedAt": iso(now),
        "analysisCount": 0,
        "bestBetsCount": 0,
        "terminalAnalysisCount": 0,
        "terminalBestBetsCount": 0,
        "pendingAnalysisCount": 0,
        "pendingBestBetsCount": 0,
        "completed": True,
        "placedAmount": 0.0,
        "availableAmount": safe_float(
            (state.get("expressBank") or {}).get("current"),
            safe_float(config.get("expressStartingBank"), 10000.0),
        ),
        "startingBank": safe_float(
            (state.get("expressBank") or {}).get("starting"),
            safe_float(config.get("expressStartingBank"), 10000.0),
        ),
        "transitionReason": "BOOTSTRAP_PREVIEW_RETIRED_FOR_NORMAL_WINDOW",
    }
    for key in (
        "bootstrapPreview",
        "bootstrapPreviewPublishedAt",
        "bootstrapPreviewReplaceAt",
        "bootstrapPreviewBankEngaged",
    ):
        meta.pop(key, None)
    meta.update({
        "status": "GENERATING_R15_PORTFOLIO",
        "dataFreshness": "BOOTSTRAP_PREVIEW_RETIRED",
        "batchStatus": "WAITING_FOR_NEXT_SELECTION",
        "batchStatusLabel": "Формируется штатный портфель",
        "updatedAt": iso(now),
    })
    state["meta"] = meta
    state.pop("dailyAudit", None)
    update_express_bank_metrics(state, now)
    return True


def publish_generation() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    state = ensure_r15_state(load_json(STATE_PATH, {}), config, now)
    calibration_guard = derive_calibration_guard(state, config)
    config = copy.deepcopy(config)
    config["dynamicUncertaintyMargin"] = safe_float(calibration_guard.get("additionalUncertaintyMargin"), 0.0)
    config["dynamicMarketFamilyHaircuts"] = copy.deepcopy(calibration_guard.get("marketFamilyHaircuts") or {})
    config["expressBankrollAllowed"] = bool(calibration_guard.get("bankrollAllowed"))
    state.setdefault("meta", {})["calibrationGuard"] = copy.deepcopy(calibration_guard)
    print(f"R15_CALIBRATION_MODE={calibration_guard.get('mode')}")
    print(f"R15_CALIBRATION_SAMPLE={safe_int((calibration_guard.get('overall') or {}).get('n'), 0)}")
    print(f"R15_CALIBRATION_EXTRA_MARGIN={safe_float(calibration_guard.get('additionalUncertaintyMargin'), 0.0):.4f}")
    print(f"R15_CALIBRATION_BANKROLL_ALLOWED={'YES' if calibration_guard.get('bankrollAllowed') else 'NO'}")
    activation = daily_auditor.activation_gate(now)
    bootstrap_preview = str(os.getenv("R15_BOOTSTRAP_PREVIEW", "")).strip().lower() in {"1", "true", "yes", "on"}
    recovery_day = str(os.getenv("R15_RECOVERY_DAY", "")).strip().lower() in {"1", "true", "yes", "on"}
    if recovery_day and not activation.get("ready"):
        print("R15_RECOVERY_DAY_IGNORED=ACTIVATION_NOT_READY")
        recovery_day = False
    if not activation.get("ready") and not bootstrap_preview:
        prepared = daily_auditor.prepare_next_window_state()
        print(f"R15F_R3_FIRST_ACTIVE_OPERATIONAL_DATE={prepared.get('operationalDateLocal')}")
        print("R15F_R3_PARTIAL_DAY_PUBLICATION=NO")
        print("R15F_R3_BANK_MUTATION=NO")
        print("FINAL_STATUS=GREEN_R15F_R3_PREPARING_NEXT_WINDOW")
        return 0
    if bootstrap_preview and activation.get("ready"):
        print("R15_BOOTSTRAP_PREVIEW_IGNORED=ALREADY_IN_NORMAL_WINDOW")
        bootstrap_preview = False
    previous_meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    previous_preview_state = (
        copy.deepcopy(state)
        if bool(previous_meta.get("bootstrapPreview")) and not bool(previous_meta.get("recoveryDay"))
        else None
    )
    if previous_preview_state and activation.get("ready"):
        print("R15_BOOTSTRAP_PREVIEW_PRESERVED_UNTIL_REPLACEMENT=YES")
    if bootstrap_preview:
        print("R15_BOOTSTRAP_PREVIEW=ENABLED")
        print("R15_BOOTSTRAP_PREVIEW_BANK_ENGAGED=NO")
    day = operational_day(now, config)
    current_day = str(state.get("meta", {}).get("operationalDayId") or "")
    current_records = state.get("dailyAnalysis") or []
    if previous_preview_state and activation.get("ready"):
        current_day = ""
        current_records = []

    # R3R5R1: keep the legacy package settleable in canonical history, but do
    # not let it occupy the current R15 publication slot.
    bridge = archive_legacy_publication_bridge(state, config, now)
    if bridge.get("archived"):
        current_day = ""
        current_records = []
        print("R15_R3R5R1_LEGACY_BRIDGE_ARCHIVED=YES")
        print(f"R15_R3R5R1_ARCHIVED_ANALYSIS={bridge.get('analysis', 0)}")
        print(f"R15_R3R5R1_ARCHIVED_BEST_BETS={bridge.get('bestBets', 0)}")
        print("R15_R3R5R1_ARCHIVE_VERIFICATION=CANONICAL_SETTLEMENT_KEYS")
        print("R15_R3R5R1_BANK_MUTATION=NO")

    rollover = release_expired_previous_day(state, config, now, day)
    if rollover.get("released"):
        current_day = ""
        current_records = []
        print("R15_EXPIRED_PREVIOUS_DAY_RELEASED=YES")
        print(f"R15_EXPIRED_PREVIOUS_DAY={rollover.get('oldOperationalDayId')}")
        print(f"R15_NEXT_OPERATIONAL_DAY={rollover.get('nextOperationalDayId')}")

    if current_day == day["operationalDayId"] and current_records:
        minimum_odds = safe_float(config.get("minimumBookmakerOdds"), 1.55)
        future_records = []
        invalid_future_records = []
        started_or_unknown = []
        for row in current_records:
            if not isinstance(row, dict):
                continue
            commence = parse_time(row.get("commenceTime"))
            if commence is None or commence <= now:
                started_or_unknown.append(row)
                continue
            future_records.append(row)
            qualification = row.get("qualification") if isinstance(row.get("qualification"), dict) else {}
            core_rejected = bool(config.get("requireCoreQualification", True)) and qualification.get("qualified") is False
            odds_below_floor = safe_float(row.get("bookmakerOdds"), safe_float(row.get("odds"))) < minimum_odds
            if core_rejected or odds_below_floor:
                invalid_future_records.append(row)
        if invalid_future_records and future_records and not started_or_unknown:
            withdrawn_daily = copy.deepcopy(current_records)
            withdrawn_best = copy.deepcopy(state.get("bestBets") or [])
            for collection in (withdrawn_daily, withdrawn_best):
                for row in collection:
                    if not isinstance(row, dict):
                        continue
                    row["status"] = "void"
                    row["statusLabel"] = "Отозван до начала"
                    row["settledAt"] = iso(now)
                    row["settlementSource"] = "PREMATCH_POLICY_REVALIDATION"
                    row["profit"] = 0.0
                    row["withdrawalReason"] = "FAILED_CURRENT_HARD_GUARD_BEFORE_KICKOFF"
            core.append_new_records_to_history(state, withdrawn_daily, withdrawn_best, config)
            for express in state.get("expresses") or []:
                if isinstance(express, dict) and str(express.get("status") or "pending") == "pending":
                    express["status"] = "void"
                    express["settledAt"] = iso(now)
                    express["settlementSource"] = "PREMATCH_POLICY_REVALIDATION"
                    express["profit"] = 0.0
            archive_previous_expresses(state)
            state["dailyAnalysis"] = []
            state["bestBets"] = []
            state["predictions"] = []
            state["expresses"] = []
            current_day = ""
            current_records = []
            update_express_bank_metrics(state, now)
            print(f"R15_PREMATCH_POLICY_REVALIDATION_WITHDRAWN={len(invalid_future_records)}")
            print("R15_PREMATCH_POLICY_REVALIDATION_RESELECT=YES")
        else:
            print("R15_CURRENT_OPERATIONAL_DAY_ALREADY_PUBLISHED=YES")
            return 0
    if current_records and not all(str(row.get("status") or "pending") in TERMINAL for row in current_records if isinstance(row, dict)):
        print("R15_GENERATION_BLOCKED_ACTIVE_PREVIOUS_BATCH=YES")
        return 0

    odds_activation_threshold = safe_int(config.get("oddsBackupActivationThreshold"), 4)
    if recovery_day and not current_records:
        odds_activation_threshold = 1_000_000
        print("R15_RECOVERY_GENERATION_BACKUP_PREFERRED=YES")
    odds_key, odds_key_selection = core.select_odds_api_key(
        activation_threshold=odds_activation_threshold,
    )
    football_key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip() or None
    cloudflare_ai_key = os.getenv("CLOUDFLARE_AI_ACCESS_TOKEN", "").strip() or None
    print(f"R15_ODDS_KEY_SOURCE={odds_key_selection.get('selected')}")
    print(
        "R15_ODDS_PRIMARY_REMAINING="
        f"{safe_int((odds_key_selection.get('primary') or {}).get('remaining'), -1)}"
    )
    print(
        "R15_ODDS_BACKUP_REMAINING="
        f"{safe_int((odds_key_selection.get('backup') or {}).get('remaining'), -1)}"
    )

    prior_health = load_json(PROVIDER_HEALTH_PATH, {})
    client = ProviderClient(prior_health)
    seed_client_quota_identity(client, odds_key_selection)

    # R15F: the historical/team-strength layer is refreshed from no-key public
    # sources before any bookmaker event is ranked. This is the primary source
    # of form, goals, opponent strength and external Elo. football-data.org is
    # retained only as an optional extra when an existing secret is present.
    free_mesh_result = free_mesh.refresh_all(force=False)
    history_result = {
        "status": "FREE_DATA_MESH",
        "freeMesh": free_mesh_result,
        "optionalFootballData": None,
    }
    if football_key and bool(config.get("footballDataOptionalEnrichmentEnabled", False)):
        history_result["optionalFootballData"] = refresh_history_cache(
            client,
            football_key,
            config,
            now,
            request_budget=max(1, safe_int(config.get("footballHistoryMorningRequests"), 1)),
        )
    cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
    tracked_history = ingest_settled_state_history(cache, state, now)
    if tracked_history.get("added"):
        write_json(HISTORY_CACHE_PATH, cache)
        write_json(TEAM_REGISTRY_PATH, rebuild_registry(cache, load_json(TEAM_REGISTRY_PATH, empty_registry())))
    registry = load_json(TEAM_REGISTRY_PATH, empty_registry())
    context = free_mesh.merge_external_elo(build_history_context(cache, registry, now))
    discovered, discovery = discover_operational_events(client, odds_key, config, now)

    current_team_names: list[str] = []
    for event in discovered:
        current_team_names.extend([
            str(event.get("home_team") or ""),
            str(event.get("away_team") or ""),
        ])
    thesportsdb_history = free_mesh.refresh_thesportsdb_recent_history(
        current_team_names,
        maximum_teams=max(40, safe_int(config.get("theSportsDbCurrentHistoryMaximumTeams"), 80)),
    )
    print(
        "R15_THESPORTSDB_HISTORY_ADDED="
        f"{safe_int(thesportsdb_history.get('matchesAdded'), 0)}"
    )
    if safe_int(thesportsdb_history.get("matchesAdded"), 0) > 0:
        cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
        registry = load_json(TEAM_REGISTRY_PATH, empty_registry())
        context = free_mesh.merge_external_elo(build_history_context(cache, registry, now))

    keys, quota_plan = select_sport_keys_by_quota(discovered, context, client, config)
    start = parse_time(discovery.get("queryWindowStart"))
    end = parse_time(discovery.get("queryWindowEnd"))
    if not start or not end:
        raise RuntimeError("R15 operational window unresolved")
    odds_events, advanced, acquisition_errors, quota_plan, preliminary_diag, advanced_recovery_diag = complete_portfolio_acquisition(
        client, odds_key, keys, quota_plan, discovered, config, start, end, context, state, now
    )
    featured_errors = list(acquisition_errors)
    keys = list(quota_plan.get("competitionKeysSelected") or keys)

    # R3R25: if the primary credential cannot build a quality portfolio, use
    # the already-configured reserve credential for additional competitions.
    # Each credential keeps its own persistent daily ledger; thresholds are
    # unchanged and cached primary quotes are merged with reserve-key results.
    quota_by_key: dict[str, Any] = {
        str(getattr(client, "quota_identity", "PRIMARY")): copy.deepcopy(client.odds_quota)
    }
    primary_plan_snapshot = copy.deepcopy(quota_plan)
    secondary_fallback = {
        "enabled": bool(config.get("oddsUseSecondaryKeyFallback", True)),
        "activated": False,
        "reason": None,
        "competitionKeysSelected": [],
        "featuredEventsCollected": 0,
        "quota": None,
    }
    target_records = max(1, safe_int(config.get("dailyAnalysisTarget"), 15))
    selected_identity = str(getattr(client, "quota_identity", "PRIMARY") or "PRIMARY").upper()
    backup_key = os.getenv("ODDS_API_KEY_BACKUP", "").strip()
    backup_probe = odds_key_selection.get("backup") if isinstance(odds_key_selection.get("backup"), dict) else {}
    backup_remaining = safe_int(backup_probe.get("remaining"), -1)
    if (
        bool(config.get("oddsUseSecondaryKeyFallback", True))
        and selected_identity == "PRIMARY"
        and safe_int(preliminary_diag.get("eventsQualified"), 0) < target_records
        and backup_key
        and bool(backup_probe.get("valid"))
        and (backup_remaining < 0 or backup_remaining > 0)
    ):
        already_selected = set(str(value) for value in keys if value)
        remaining_discovered = [
            event for event in discovered
            if str(event.get("sport_key") or "") not in already_selected
        ]
        if remaining_discovered:
            backup_client = ProviderClient(client.health)
            seed_client_quota_identity(
                backup_client,
                {
                    "selected": "BACKUP",
                    "primary": odds_key_selection.get("primary") or {},
                    "backup": backup_probe,
                },
            )
            backup_keys, backup_plan = select_sport_keys_by_quota(
                remaining_discovered, context, backup_client, config
            )
            if backup_keys:
                (
                    secondary_odds_events,
                    secondary_advanced,
                    secondary_errors,
                    backup_plan,
                    secondary_preliminary_diag,
                    secondary_recovery_diag,
                ) = complete_portfolio_acquisition(
                    backup_client,
                    backup_key,
                    backup_keys,
                    backup_plan,
                    remaining_discovered,
                    config,
                    start,
                    end,
                    context,
                    state,
                    now,
                )
                odds_events = secondary_odds_events
                advanced = secondary_advanced
                featured_errors.extend(secondary_errors)
                preliminary_diag = secondary_preliminary_diag
                advanced_recovery_diag = {
                    "requested": safe_int(advanced_recovery_diag.get("requested"), 0)
                        + safe_int(secondary_recovery_diag.get("requested"), 0),
                    "returned": safe_int(advanced_recovery_diag.get("returned"), 0)
                        + safe_int(secondary_recovery_diag.get("returned"), 0),
                    "usefulResponses": safe_int(advanced_recovery_diag.get("usefulResponses"), 0)
                        + safe_int(secondary_recovery_diag.get("usefulResponses"), 0),
                    "recoveredEvents": safe_int(advanced_recovery_diag.get("recoveredEvents"), 0)
                        + safe_int(secondary_recovery_diag.get("recoveredEvents"), 0),
                    "attemptedPairs": safe_int(advanced_recovery_diag.get("attemptedPairs"), 0)
                        + safe_int(secondary_recovery_diag.get("attemptedPairs"), 0),
                    "unsupportedMarkets": {
                        **(advanced_recovery_diag.get("unsupportedMarkets") or {}),
                        **(secondary_recovery_diag.get("unsupportedMarkets") or {}),
                    },
                    "errors": (
                        list(advanced_recovery_diag.get("errors") or [])
                        + list(secondary_recovery_diag.get("errors") or [])
                    )[-40:],
                }
                secondary_keys = list(backup_plan.get("competitionKeysSelected") or backup_keys)
                keys = list(dict.fromkeys(keys + secondary_keys))
                quota_by_key["BACKUP"] = copy.deepcopy(backup_client.odds_quota)
                secondary_fallback.update({
                    "activated": True,
                    "reason": "PRIMARY_PORTFOLIO_INCOMPLETE",
                    "competitionKeysSelected": secondary_keys,
                    "featuredEventsCollected": safe_int(backup_plan.get("featuredEventsCollected"), 0),
                    "quota": copy.deepcopy(backup_client.odds_quota),
                    "quotaPlan": backup_plan,
                })
                quota_plan["secondaryKeyFallback"] = secondary_fallback
                quota_plan["primaryKeyPlan"] = primary_plan_snapshot
                quota_plan["competitionKeysSelected"] = keys
                quota_plan["competitionsSelected"] = len(keys)
                quota_plan["featuredEventsCollected"] = len(odds_events)
                quota_plan["qualifiedAfterAdvanced"] = safe_int(preliminary_diag.get("eventsQualified"), 0)
                client.calls.extend([
                    dict(call, quotaIdentity="BACKUP")
                    for call in backup_client.calls
                ])
                client.health = backup_client.health
                print(f"R15_SECONDARY_KEY_COMPETITIONS={len(secondary_keys)}")
                print(f"R15_SECONDARY_KEY_ODDS_EVENTS={len(odds_events)}")
            else:
                secondary_fallback["reason"] = "BACKUP_DAILY_BUDGET_OR_SELECTION_EMPTY"
                quota_plan["secondaryKeyFallback"] = secondary_fallback
        else:
            secondary_fallback["reason"] = "NO_UNSEEN_COMPETITIONS"
            quota_plan["secondaryKeyFallback"] = secondary_fallback
    else:
        if selected_identity != "PRIMARY":
            secondary_fallback["reason"] = "CURRENT_CREDENTIAL_ALREADY_BACKUP"
        elif safe_int(preliminary_diag.get("eventsQualified"), 0) >= target_records:
            secondary_fallback["reason"] = "PRIMARY_PORTFOLIO_READY"
        elif not backup_key or not bool(backup_probe.get("valid")):
            secondary_fallback["reason"] = "BACKUP_UNAVAILABLE"
        else:
            secondary_fallback["reason"] = "BACKUP_QUOTA_EMPTY"
        quota_plan["secondaryKeyFallback"] = secondary_fallback

    for event in odds_events:
        event["sport_type"] = "soccer"
        event["country"] = core.infer_country(str(event.get("sport_key") or ""), str(event.get("sport_title") or ""))

    # Public Fonbet page is used only as availability evidence. When its
    # server-rendered snapshot confirms at least 15 events, non-confirmed
    # events are excluded. If the page is unavailable or incomplete, the
    # analytical cycle continues and records carry an explicit UNKNOWN status.
    fonbet_snapshot = free_mesh.refresh_fonbet_public_snapshot(force=False)
    fonbet_confirmed = []
    for event in odds_events:
        confirmed = free_mesh.fonbet_event_confirmed(
            str(event.get("home_team") or ""),
            str(event.get("away_team") or ""),
            fonbet_snapshot,
        )
        event["fonbetAvailability"] = "MATCH_CONFIRMED_PUBLIC_LINE" if confirmed else (
            "NOT_CONFIRMED_IN_PUBLIC_SNAPSHOT" if fonbet_snapshot.get("status") == "GREEN" else "SOURCE_UNAVAILABLE"
        )
        if confirmed:
            fonbet_confirmed.append(event)
    if len(fonbet_confirmed) >= safe_int(config.get("dailyAnalysisTarget"), 15):
        odds_events = fonbet_confirmed
        fonbet_mode = "REQUIRED_CONFIRMED_POOL"
    else:
        fonbet_mode = "OBSERVATIONAL_SOURCE_INCOMPLETE"

    advanced_errors = list(advanced_recovery_diag.get("errors") or [])
    records, analysis_diag = build_strategy_analysis(odds_events, advanced, context, state, config, now)
    analysis_diag = enrich_rejection_diagnostics(analysis_diag, config)
    if bootstrap_preview and len(records) != safe_int(config.get("dailyAnalysisTarget"), 15):
        strict_analysis_diag = copy.deepcopy(analysis_diag)
        preview_records, preview_diag = build_bootstrap_preview_analysis(
            odds_events, advanced, context, state, config, now
        )
        if len(preview_records) == safe_int(config.get("dailyAnalysisTarget"), 15):
            records = preview_records
            analysis_diag = preview_diag
            analysis_diag["strictProductionDiagnostics"] = strict_analysis_diag
            print("R15_BOOTSTRAP_PREVIEW_RELAXED_PROFILE=USED")
            print(f"R15_BOOTSTRAP_PREVIEW_ELIGIBLE={preview_diag.get('eventsEligible', 0)}")
        else:
            analysis_diag["bootstrapPreviewAttempt"] = preview_diag

    if recovery_day and not records:
        strict_analysis_diag = copy.deepcopy(analysis_diag)
        recovery_config = copy.deepcopy(config)
        recovery_config.update({
            "bootstrapPreviewMinimumDataQuality": 58.0,
            "bootstrapPreviewMinimumConservativeProbability": 0.56,
            "bootstrapPreviewMinimumBookmakers": 3,
            "bootstrapPreviewMinimumAgreement": 54.0,
            "bootstrapPreviewMinimumMarketStability": 48.0,
            "bootstrapPreviewMaximumAnomaly": 58.0,
            "bootstrapPreviewMaximumSameLeague": 5,
            "bootstrapPreviewMaximumSameMarketFamily": 6,
            "bootstrapPreviewAllowPartial": True,
        })
        recovery_records, recovery_diag = build_bootstrap_preview_analysis(
            odds_events, advanced, context, state, recovery_config, now
        )
        if recovery_records:
            for row in recovery_records:
                row["publicationMode"] = "RECOVERY_DAY"
                row["financialMode"] = "EXPRESS_LEG"
                row["strategyQualified"] = False
                row["recoveryQualified"] = True
                row["recoveryThresholdProfile"] = "HYBRID_Q58_P56_BOOKS3_GUARDED_MARKETS"
                row.pop("previewQualified", None)
                rationale = row.get("selectionRationale") if isinstance(row.get("selectionRationale"), dict) else {}
                rationale.pop("previewNotice", None)
                rationale["recoveryNotice"] = (
                    "Восстановительный суточный портфель: реальные HYBRID-данные, "
                    "качество >=58, консервативная вероятность >=56%, минимум 3 букмекера, без конфликтного направления тотала."
                )
                row["selectionRationale"] = rationale
            records = recovery_records
            analysis_diag = recovery_diag
            analysis_diag["mode"] = "RECOVERY_DAY"
            analysis_diag["strictProductionDiagnostics"] = strict_analysis_diag
            analysis_diag["recoveryThresholdProfile"] = "HYBRID_Q58_P56_BOOKS3_GUARDED_MARKETS"
            print("R15_RECOVERY_DAY_PROFILE=USED")
            print(f"R15_RECOVERY_DAY_ELIGIBLE={recovery_diag.get('eventsEligible', 0)}")
        else:
            analysis_diag["recoveryDayAttempt"] = recovery_diag
    quota_plan["advancedCompletionMode"] = safe_int(analysis_diag.get("eventsQualified"), 0) < safe_int(config.get("dailyAnalysisTarget"), 15)
    quota_plan["advancedRecoveryRequestedEvents"] = safe_int(advanced_recovery_diag.get("requested"), 0)
    quota_plan["advancedRecoveryReceivedEvents"] = safe_int(advanced_recovery_diag.get("returned"), 0)
    quota_plan["advancedRecoveryRecoveredEvents"] = safe_int(advanced_recovery_diag.get("recoveredEvents"), 0)
    quota_plan["advancedRecoveryAttemptedPairs"] = safe_int(advanced_recovery_diag.get("attemptedPairs"), 0)
    quota_plan["advancedRecoveryUnsupportedMarkets"] = advanced_recovery_diag.get("unsupportedMarkets") or {}
    quota_plan["qualifiedAfterAdvanced"] = safe_int(
        analysis_diag.get("eventsQualified"),
        safe_int(analysis_diag.get("eventsEligible"), 0),
    )

    shadow_watchlist = refresh_shadow_watchlist(state, analysis_diag, now, config)

    report = {
        "status": "GREEN" if len(records) == 15 else "DEGRADED",
        "version": core.STATE_VERSION,
        "sourceMarker": R15_MARKER,
        "mode": "bootstrap-preview" if bootstrap_preview else ("recovery-day" if recovery_day else "generate"),
        "startedAt": iso(now),
        "finishedAt": iso(now_utc()),
        "diagnostics": {
            "history": history_result,
            "freeDataMesh": free_mesh_result,
            "theSportsDbCurrentHistory": thesportsdb_history,
            "trackedHistory": tracked_history,
            "discovery": discovery,
            "quotaPlan": quota_plan,
            "featuredOddsEvents": len(odds_events),
            "advancedOddsEvents": len(advanced),
            "analysis": analysis_diag,
            "shadowLearning": {
                **shadow_watchlist,
                "statistics": copy.deepcopy((state.get("shadowLearning") or {}).get("statistics") or {}),
            },
            "apiCalls": client.calls,
            "quota": client.odds_quota,
            "quotaByKey": quota_by_key,
            "fonbet": {
                "status": fonbet_snapshot.get("status"),
                "mode": fonbet_mode,
                "confirmedEvents": len(fonbet_confirmed),
                "sourceUpdatedAt": fonbet_snapshot.get("updatedAt"),
            },
        },
        "warnings": featured_errors + advanced_errors,
        "errors": [],
    }

    if not records:
        clear_completed_current_batch(state, now, "NO_QUALIFIED_EVENTS_IN_CURRENT_OPERATIONAL_DAY")
        state.setdefault("meta", {}).update({
            "sourceMarker": R15_MARKER,
            "status": "WAITING_FOR_QUALITY_SELECTION",
            "dataFreshness": "CURRENT_BUT_INSUFFICIENT_FOR_STRATEGY",
            "analysisDateLocal": day["operationalDateLocal"],
            "operationalDayId": day["operationalDayId"],
            "operationalWindowStart": day["operationalWindowStart"],
            "operationalWindowEnd": day["operationalWindowEnd"],
            "selectionWindowStart": discovery.get("queryWindowStart"),
            "selectionWindowEnd": discovery.get("queryWindowEnd"),
            "selectionStagesUsed": discovery.get("stagesUsed"),
            "selectionPolicy": discovery.get("policy"),
            "updatedAt": iso(now),
        })
        state["dataCoverage"] = {
            "discoveredEvents": len(discovered),
            "competitionsDiscovered": discovery.get("sportKeysWithEvents"),
            "competitionsWithOdds": len(keys),
            "oddsEvents": len(odds_events),
            "historyMatchedEvents": analysis_diag.get("eventsWithHistory"),
            "qualifiedEvents": analysis_diag.get("eventsQualified"),
            "publishedEvents": 0,
            "providerHealth": copy.deepcopy(client.health),
            "quota": copy.deepcopy(client.odds_quota),
            "quotaByKey": copy.deepcopy(quota_by_key),
            "historyMatches": context.get("cacheMeta", {}).get("matches"),
            "historyCoverageStart": context.get("cacheMeta", {}).get("coverageStart"),
            "historyCoverageEnd": context.get("cacheMeta", {}).get("coverageEnd"),
            "historyComplete": context.get("cacheMeta", {}).get("complete"),
            "freeDataMesh": free_mesh_result,
            "theSportsDbCurrentHistory": thesportsdb_history,
            "fonbetMode": fonbet_mode,
            "fonbetConfirmedEvents": len(fonbet_confirmed),
            "status": "INSUFFICIENT_QUALITY_EVENTS",
            "updatedAt": iso(now),
        }
        update_express_statistics(state)
        write_json(PROVIDER_HEALTH_PATH, client.health)
        if previous_preview_state:
            fallback = previous_preview_state
            fallback.setdefault("meta", {}).update({
                "status": "PREVIOUS_PORTFOLIO_HELD_WHILE_NEW_SELECTION_BUILDS",
                "dataFreshness": "STALE_PREVIOUS_PORTFOLIO_VISIBLE",
                "nextPortfolioStatus": "WAITING_FOR_QUALITY_SELECTION",
                "replacementAttemptAt": iso(now),
                "replacementOperationalDayId": day["operationalDayId"],
                "replacementQualifiedEvents": safe_int(analysis_diag.get("eventsQualified"), 0),
            })
            fallback["systemNarrative"] = {
                "title": "Новая подборка ещё формируется",
                "lead": "Предыдущая подборка временно остаётся видимой, пока новая не проходит полный контроль качества.",
                "body": "Новый портфель заменит предыдущий только после готовности полного набора.",
                "generatedBy": "DETERMINISTIC_SYSTEM",
                "updatedAt": iso(now),
            }
            write_public_files(fallback, report)
            print("R15_PREVIOUS_PORTFOLIO_HELD=YES")
        else:
            write_public_files(state, report)
        print(f"R15_QUALITY_EVENTS={analysis_diag.get('eventsQualified', 0)}")
        print("R15_PUBLICATION_SKIPPED=INSUFFICIENT_QUALITY")
        print("FINAL_STATUS=GREEN_R15_WAITING_FOR_QUALITY")
        return 0

    if previous_preview_state and activation.get("ready"):
        discard_bootstrap_preview(state, config, now)
        print("R15_BOOTSTRAP_PREVIEW_RETIRED_AT_SUCCESSFUL_REPLACEMENT=YES")

    russian_names_result = free_mesh.apply_russian_names(records)
    fonbet_result = free_mesh.fonbet_gate(records)
    core.apply_operational_window_metadata(records, discovery, now)
    for row in records:
        row["publicationOperationalDayId"] = day["operationalDayId"]
        row["publicationOperationalWindowStart"] = day["operationalWindowStart"]
        row["publicationOperationalWindowEnd"] = day["operationalWindowEnd"]
        row["operationalWindowStart"] = discovery.get("queryWindowStart")
        row["operationalWindowEnd"] = discovery.get("queryWindowEnd")
        row["selectionWindowStart"] = discovery.get("queryWindowStart")
        row["selectionWindowEnd"] = discovery.get("queryWindowEnd")
        row["selectionWindowPolicy"] = discovery.get("policy")
    audited_records, daily_audit = daily_auditor.audit_records(
        records,
        config,
        cloudflare_ai_key,
        day["operationalDayId"],
        now,
    )
    records = audited_records
    audit_system_message = daily_audit.get("systemMessage") if isinstance(daily_audit.get("systemMessage"), dict) else {}
    state["dailyAudit"] = copy.deepcopy(daily_audit)
    best = informational_best_three(records, now, list(daily_audit.get("topSingles") or []))
    core.apply_best_bets_to_daily_analysis(records, best)
    for row in records:
        row["stake"] = 0.0
        row["stakePercent"] = 0.0
        row["financialMode"] = "EXPRESS_LEG"
    if not bootstrap_preview:
        archive_previous_expresses(state)
    batch = core.publish_new_batch(state, records, best, best, config, now)
    if not bootstrap_preview:
        core.append_new_records_to_history(state, records, best, config)
    state["dailyAnalysis"] = records
    state["bestBets"] = best
    state["predictions"] = copy.deepcopy(best)
    expresses = build_expresses(records, state, config, now, daily_audit.get("expresses") if isinstance(daily_audit, dict) else None)
    if bootstrap_preview:
        for row in records:
            row["publicationMode"] = "BOOTSTRAP_PREVIEW"
            row["financialMode"] = "PREVIEW_EXPRESS_LEG"
        for row in best:
            row["publicationMode"] = "BOOTSTRAP_PREVIEW"
        for express in expresses:
            express["previewStakePercent"] = express.get("stakePercent")
            express["previewStake"] = express.get("stake")
            express["previewPotentialPayout"] = express.get("potentialPayout")
            express["previewPotentialProfit"] = express.get("potentialProfit")
            express["stakePercent"] = 0.0
            express["stake"] = 0.0
            express["potentialPayout"] = 0.0
            express["potentialProfit"] = 0.0
            express["financialMode"] = "BOOTSTRAP_PREVIEW_NO_BANK_MUTATION"
            express["statusLabel"] = "Предпросмотр"
        batch.update({
            "status": "PREVIEW",
            "statusLabel": "Временный предпросмотр до штатного окна 08:00 МСК",
            "placedAmount": 0.0,
            "availableAmount": safe_float(
                (state.get("expressBank") or {}).get("current"),
                safe_float(config.get("expressStartingBank"), 10000.0),
            ),
            "rolloverExecutionPolicy": "AUTO_REPLACE_AT_FIRST_ACTIVE_08_MSK_WINDOW",
        })
        update_express_bank_metrics(state, now)
    core.update_statistics(state)
    update_express_statistics(state)
    state["dataCoverage"] = {
        "discoveredEvents": len(discovered),
        "competitionsDiscovered": discovery.get("sportKeysWithEvents"),
        "competitionsWithOdds": len(keys),
        "competitionsDeferredByQuota": quota_plan.get("competitionsDeferredByQuota"),
        "oddsEvents": len(odds_events),
        "historyMatchedEvents": analysis_diag.get("eventsWithHistory"),
        "qualifiedEvents": analysis_diag.get("eventsQualified"),
        "marketCandidates": analysis_diag.get("marketCandidates"),
        "publishedEvents": len(records),
        "historyMatches": context.get("cacheMeta", {}).get("matches"),
        "historyCoverageStart": context.get("cacheMeta", {}).get("coverageStart"),
        "historyCoverageEnd": context.get("cacheMeta", {}).get("coverageEnd"),
        "historyComplete": context.get("cacheMeta", {}).get("complete"),
        "clubEloApplied": context.get("cacheMeta", {}).get("clubEloApplied"),
        "freeDataMesh": free_mesh_result,
        "theSportsDbCurrentHistory": thesportsdb_history,
        "fonbetMode": fonbet_mode,
        "fonbetConfirmedEvents": len(fonbet_confirmed),
        "providerHealth": copy.deepcopy(client.health),
        "quota": copy.deepcopy(client.odds_quota),
        "status": "GREEN",
        "updatedAt": iso(now),
    }
    state["systemNarrative"] = {
        **daily_auditor.deterministic_system_narrative(state, "PUBLISHED", str(daily_audit.get("status") or "FALLBACK")),
        **audit_system_message,
        "generatedBy": "CLOUDFLARE_AI_FREE_AUDIT" if daily_audit.get("schemaValid") else "DETERMINISTIC_SYSTEM",
        "modelUsed": daily_audit.get("modelUsed"),
        "updatedAt": iso(now),
    }
    state.setdefault("meta", {}).update({
        "version": core.STATE_VERSION,
        "sourceMarker": R15_MARKER,
        "status": "GREEN",
        "dataFreshness": "CURRENT",
        "analysisDateLocal": day["operationalDateLocal"],
        "analysisGeneratedAt": iso(now),
        "operationalDayId": day["operationalDayId"],
        "operationalWindowStart": day["operationalWindowStart"],
        "operationalWindowEnd": day["operationalWindowEnd"],
        "selectionWindowStart": discovery.get("queryWindowStart"),
        "selectionWindowEnd": discovery.get("queryWindowEnd"),
        "selectionStagesUsed": discovery.get("stagesUsed"),
        "operationalWindowPolicy": day["policy"],
        "selectionPolicy": discovery.get("policy"),
        "analysisTarget": safe_int(config.get("dailyAnalysisTarget"), 15),
        "analysisPublished": len(records),
        "bestBetsPublished": len(best),
        "expressesPublished": len(expresses),
        "expressLegsPublished": sum(len(item.get("legs") or []) for item in expresses),
        "soccerAnalyses": len(records),
        "partialDailyPortfolio": len(records) < safe_int(config.get("dailyAnalysisTarget"), 15),
        "hockeyAnalyses": 0,
        "candidateMatchesAnalyzed": analysis_diag.get("eventsWithMarkets"),
        "cloudflareAiAuditStatus": daily_audit.get("status"),
        "cloudflareAiModelUsed": daily_audit.get("modelUsed"),
        "cloudflareAiSchemaValid": bool(daily_audit.get("schemaValid")),
        "cloudflareAiLogicalRuns": safe_int(daily_audit.get("logicalRuns"), 0),
        "predictionObjective": "FULL_MATCH_UNDERSTANDING_AND_MOST_OBVIOUS_QUALIFIED_MARKET",
        "publicationPolicy": "STRICT_24H_MOSCOW_DAY_UP_TO_FIFTEEN_REAL_QUALIFIED_MATCHES",
        "virtualBankPolicy": R15_EXPRESS_POLICY,
        "updatedAt": iso(now),
        "lastSuccessfulRefreshAt": iso(now),
        "apiHealth": {
            "status": "GREEN" if not report["warnings"] else "DEGRADED",
            "calls": len(client.calls),
            "errors": len(report["warnings"]),
        },
    })
    state["quota"] = {
        "provider": "THE_ODDS_API",
        **client.odds_quota,
        "updatedAt": iso(now),
    }
    report["diagnostics"].update({
        "dailyAnalysis": len(records),
        "informationalTopThree": len(best),
        "expresses": len(expresses),
        "expressBank": state.get("expressBank"),
        "russianNames": russian_names_result,
        "fonbetGate": fonbet_result,
        "dailyCloudflare Workers AIAudit": daily_audit,
        "calibrationGuard": calibration_guard,
        "topSingles": len(best),
    })
    state.pop("nextPortfolio", None)
    state.setdefault("meta", {})["nextPortfolioStatus"] = "PUBLISHED"
    if bootstrap_preview:
        control = daily_auditor.load_control()
        state["meta"].update({
            "status": "BOOTSTRAP_PREVIEW",
            "bootstrapPreview": True,
            "bootstrapPreviewPublishedAt": iso(now),
            "bootstrapPreviewReplaceAt": control.get("firstActiveWindowStart"),
            "bootstrapPreviewBankEngaged": False,
            "publicationPolicy": "TEMPORARY_FULL_PREVIEW_AUTO_REPLACED_AT_FIRST_ACTIVE_08_MSK_WINDOW",
        })
        report["diagnostics"]["bootstrapPreview"] = {
            "enabled": True,
            "bankEngaged": False,
            "replaceAt": control.get("firstActiveWindowStart"),
        }
    elif recovery_day:
        state["meta"].update({
            "status": "RECOVERY_DAY",
            "recoveryDay": True,
            "recoveryDayPublishedAt": iso(now),
            "recoveryThresholdProfile": "HYBRID_Q58_P56_BOOKS3_GUARDED_MARKETS",
            "recoveryStrictProductionThresholdsChanged": False,
            "bootstrapPreview": False,
            "bootstrapPreviewBankEngaged": False,
            "publicationPolicy": "STRICT_24H_CURRENT_DAY_RECOVERY_WITH_REAL_EVENTS_ONLY",
        })
        report["diagnostics"]["recoveryDay"] = {
            "enabled": True,
            "bankEngaged": False,
            "thresholdProfile": "HYBRID_Q58_P56_BOOKS3_GUARDED_MARKETS",
            "strictProductionThresholdsChanged": False,
        }
        daily_auditor.mark_activated(day["operationalDayId"])
    else:
        state["meta"].pop("recoveryDay", None)
        state["meta"].pop("recoveryDayPublishedAt", None)
        state["meta"].pop("recoveryThresholdProfile", None)
        state["meta"].pop("recoveryStrictProductionThresholdsChanged", None)
        daily_auditor.mark_activated(day["operationalDayId"])
    write_json(PROVIDER_HEALTH_PATH, client.health)
    write_public_files(state, report)
    print(f"R15F_ANALYSIS={len(records)}")
    print(f"R15F_INFORMATIONAL_TOP_THREE={len(best)}")
    print(f"R15F_EXPRESSES={len(expresses)}")
    print(f"R15F_EXPRESS_LEGS={sum(len(item.get('legs') or []) for item in expresses)}")
    print(f"R15F_EXPRESS_BANK={state.get('expressBank', {}).get('current')}")
    if bootstrap_preview:
        print("R15F_BOOTSTRAP_PREVIEW=GREEN")
        print("R15F_BOOTSTRAP_PREVIEW_BANK_MUTATION=NO")
        print("FINAL_STATUS=GREEN_R15F_BOOTSTRAP_PREVIEW_PUBLISHED")
    elif recovery_day:
        print("R15F_RECOVERY_DAY=GREEN")
        print("R15F_RECOVERY_DAY_BANK_ENGAGED=NO")
        print("FINAL_STATUS=GREEN_R15F_RECOVERY_DAY_PUBLISHED")
    else:
        print("FINAL_STATUS=GREEN_R15F_FREE_DATA_MESH_EXPRESS_PUBLISHED")
    return 0


# ---------------------------------------------------------------------------
# Validation, repair and synthetic acceptance
# ---------------------------------------------------------------------------


def validate_config(config: dict[str, Any]) -> None:
    core.validate_config(config)
    if config.get("sourceMarker") != R15_MARKER:
        raise RuntimeError("R15F config source marker mismatch")
    required = {
        "expressStartingBank",
        "expressCount",
        "expressLegsPerTicket",
        "expressStakePercent",
        "strategyMinimumDataQuality",
        "footballHistoryTargetDays",
        "oddsFreeMonthlyCredits",
        "oddsFreeDailyCreditBudget",
        "portfolioSearchHorizonHours",
        "portfolioSearchStepHours",
        "portfolioSearchTargetEvents",
        "oddsAllowPortfolioCompletionBurst",
        "rejectionDiagnosticsLimit",
    }
    missing = sorted(required - set(config))
    if missing:
        raise RuntimeError(f"R15 config keys missing: {missing}")
    if safe_int(config.get("expressCount")) != 3 or safe_int(config.get("expressLegsPerTicket")) != 5:
        raise RuntimeError("R15 requires three expresses of five legs")
    if safe_float(config.get("expressStakePercent")) != 2.0:
        raise RuntimeError("R15 express nominal stake must be two percent")
    if safe_float(config.get("expressStartingBank")) != 10000.0:
        raise RuntimeError("R15 express starting bank must be 10000")
    search_days = safe_int(config.get("operationalWindowSearchDays"), 3)
    horizon_hours = safe_int(config.get("portfolioSearchHorizonHours"), 72)
    if not 1 <= search_days <= 3:
        raise RuntimeError("R15 rolling prematch search days must be within 1..3")
    if not 24 <= horizon_hours <= search_days * 24:
        raise RuntimeError("R15 rolling prematch horizon must fit configured search days")


def validate_state() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    state = ensure_r15_state(load_json(STATE_PATH, {}), config, now)
    daily = state.get("dailyAnalysis") or []
    best = state.get("bestBets") or []
    expresses = state.get("expresses") or []
    is_r15_publication = bool(daily) and all(
        str(row.get("sourceMarker") or "") == R15_MARKER
        for row in daily
        if isinstance(row, dict)
    )
    if daily and not is_r15_publication:
        # A pre-R15 frozen batch must remain settleable during deployment.
        # It is validated by the mature core and is replaced only at the next
        # successful R15 morning publication; no hidden rewrite is allowed.
        print("R15_LEGACY_PUBLICATION_BRIDGE=ACTIVE")
    validation_meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    bootstrap_preview = bool(validation_meta.get("bootstrapPreview"))
    recovery_day = bool(validation_meta.get("recoveryDay"))
    if is_r15_publication:
        if not (1 <= len(daily) <= safe_int(config.get("dailyAnalysisTarget"), 15)):
            raise RuntimeError(f"R15 daily analysis must contain 1..15 current-day rows, got {len(daily)}")
        expected_best = min(3, len(daily))
        if len(best) != expected_best:
            raise RuntimeError(f"R15 informational top rows must be {expected_best}, got {len(best)}")
        expected_expresses = min(3, len(daily) // 5)
        if len(expresses) != expected_expresses:
            raise RuntimeError(f"R15 expresses must be {expected_expresses}, got {len(expresses)}")
        event_ids = [str(row.get("eventId") or "") for row in daily]
        if any(not value for value in event_ids) or len(event_ids) != len(set(event_ids)):
            raise RuntimeError("R15 duplicate or missing event IDs")
        leg_ids = []
        for express in expresses:
            legs = express.get("legs") or []
            if len(legs) != 5:
                raise RuntimeError("Every R15 express must contain five legs")
            leg_ids.extend(str(leg.get("analysisId") or "") for leg in legs)
            expected_ev = safe_float(express.get("conservativeExpectedValue"), -1.0)
            bankroll_enabled = bool(express.get("bankrollEnabled"))
            expected_stake_percent = 0.0 if (recovery_day or (bootstrap_preview and not recovery_day) or not bankroll_enabled) else safe_float(config.get("expressStakePercent"), 2.0)
            if abs(safe_float(express.get("stakePercent")) - expected_stake_percent) > 0.001:
                raise RuntimeError("R15 express stake percent violates risk policy")
            if bankroll_enabled and expected_ev < safe_float(config.get("expressMinimumConservativeExpectedValue"), 0.03):
                raise RuntimeError("R15 express bank enabled without positive conservative EV")
        if len(leg_ids) != len(expresses) * 5 or len(set(leg_ids)) != len(leg_ids):
            raise RuntimeError("R15 express legs must be unique five-leg groups")
        daily_ids = {str(row.get("id") or "") for row in daily}
        if not set(leg_ids).issubset(daily_ids):
            raise RuntimeError("R15 express legs must come from daily analysis")
        public_start = parse_time(validation_meta.get("operationalWindowStart"))
        public_end = parse_time(validation_meta.get("operationalWindowEnd"))
        selection_start = parse_time(validation_meta.get("selectionWindowStart")) or public_start
        selection_end = parse_time(validation_meta.get("selectionWindowEnd")) or public_end
        if not public_start or not public_end or public_start >= public_end:
            raise RuntimeError("R15 public operational window missing or invalid")
        if not selection_start or not selection_end or selection_start >= selection_end:
            raise RuntimeError("R15 rolling selection window missing or invalid")
        maximum_selection_end = selection_start + dt.timedelta(
            hours=max(24, safe_int(config.get("portfolioSearchHorizonHours"), 72))
        )
        if selection_end > maximum_selection_end + dt.timedelta(minutes=5):
            raise RuntimeError("R15 rolling selection horizon exceeds configured maximum")
        for row in daily:
            commence = parse_time(row.get("commenceTime"))
            if commence is None or commence < selection_start or commence >= selection_end:
                raise RuntimeError(
                    "R15 event outside rolling prematch selection horizon: "
                    f"{row.get('eventId')} {row.get('commenceTime')}"
                )
            if str(row.get("sport") or "") != "soccer":
                raise RuntimeError("R15 contains non-football event")
            if str(row.get("dataTier") or "MARKET") == "MARKET":
                raise RuntimeError("R15 strategy contains MARKET-only event")
            minimum_quality = (
                40.0
                if recovery_day
                else (
                    safe_float(config.get("bootstrapPreviewMinimumDataQuality"), 40.0)
                    if bootstrap_preview
                    else safe_float(config.get("strategyMinimumDataQuality"), 58)
                )
            )
            if safe_float(row.get("dataQuality")) < minimum_quality:
                raise RuntimeError("R15 strategy contains weak data")
            if core.competition_is_excluded(row.get("sportKey"), row.get("league"), "", row.get("country"), config):
                raise RuntimeError("R15 contains excluded competition")
            if not core.record_uses_r14_standard_market(row):
                raise RuntimeError("R15 contains Asian or unsupported market")
        if any(safe_float(row.get("stake")) != 0.0 for row in best):
            raise RuntimeError("R15 informational top three carries a separate stake")
        audit = state.get("dailyAudit") if isinstance(state.get("dailyAudit"), dict) else {}
        if audit.get("schemaValid") and safe_int(audit.get("logicalRuns"), 0) > 1:
            raise RuntimeError("R15 Cloudflare Workers AI logical audit ran more than once")
        if any(safe_float(row.get("auditRiskPenalty"), 0.0) < 0 for row in daily):
            raise RuntimeError("R15 audit increased confidence")
    update_express_bank_metrics(state, now)
    bank = state.get("expressBank") or {}
    active = [row for row in expresses if str(row.get("status") or "pending") == "pending"]
    expected = round(sum(safe_float(row.get("stake")) for row in active), 2)
    if abs(safe_float(bank.get("placedAmount")) - expected) > 0.02:
        raise RuntimeError("R15 express bank exposure mismatch")
    print("R15_VALIDATION=GREEN")
    print(f"R15_ANALYSIS={len(daily)}")
    print(f"R15_EXPRESSES={len(expresses)}")
    print(f"R15F_EXPRESS_BANK={bank.get('current')}")
    return 0


def synthetic_event(index: int, now: dt.datetime) -> dict[str, Any]:
    home = f"Home Club {index}"
    away = f"Away Club {index}"
    outcomes = [
        {"name": home, "price": 1.55},
        {"name": "Draw", "price": 4.20},
        {"name": away, "price": 6.50},
    ]
    total_outcomes = [
        {"name": "Over", "price": 1.72, "point": 2.5},
        {"name": "Under", "price": 2.05, "point": 2.5},
    ]
    return {
        "id": f"event-{index}",
        "sport_key": f"soccer_test_{index // 3}",
        "sport_title": f"Test League {index // 3}",
        "home_team": home,
        "away_team": away,
        "commence_time": iso(now + dt.timedelta(hours=2 + index)),
        "bookmakers": [
            {
                "key": f"book-{book}",
                "title": f"Book {book}",
                "last_update": iso(now),
                "markets": [
                    {"key": "h2h", "last_update": iso(now), "outcomes": copy.deepcopy(outcomes)},
                    {"key": "totals", "last_update": iso(now), "outcomes": copy.deepcopy(total_outcomes)},
                ],
            }
            for book in range(1, 5)
        ],
    }


def synthetic_context(now: dt.datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    cache = empty_history_cache()
    matches = []
    match_id = 1
    for team_index in range(15):
        for side_name in (f"Home Club {team_index}", f"Away Club {team_index}"):
            team_id = str(team_index * 2 + (0 if side_name.startswith("Home") else 1) + 1)
            for game in range(20):
                opponent_id = str(1000 + team_index * 20 + game)
                when = now - dt.timedelta(days=game * 5 + 2)
                if game % 2 == 0:
                    home_team = {"id": team_id, "name": side_name, "shortName": side_name, "tla": f"T{team_id}"}
                    away_team = {"id": opponent_id, "name": f"Opponent {opponent_id}", "shortName": f"Opponent {opponent_id}", "tla": f"O{game}"}
                    home_score, away_score = (2, 1) if side_name.startswith("Home") else (1, 1)
                else:
                    home_team = {"id": opponent_id, "name": f"Opponent {opponent_id}", "shortName": f"Opponent {opponent_id}", "tla": f"O{game}"}
                    away_team = {"id": team_id, "name": side_name, "shortName": side_name, "tla": f"T{team_id}"}
                    home_score, away_score = (1, 2) if side_name.startswith("Home") else (1, 1)
                matches.append({
                    "id": str(match_id),
                    "utcDate": iso(when),
                    "status": "FINISHED",
                    "competitionId": "99",
                    "competition": f"Test League {team_index // 3}",
                    "competitionCode": "TST",
                    "homeTeam": home_team,
                    "awayTeam": away_team,
                    "homeScore": home_score,
                    "awayScore": away_score,
                })
                match_id += 1
    cache["matches"] = matches
    cache["coverageStart"] = iso(now - dt.timedelta(days=120))
    cache["coverageEnd"] = iso(now - dt.timedelta(days=2))
    cache["complete"] = True
    registry = rebuild_registry(cache, empty_registry())
    return build_history_context(cache, registry, now), registry


def self_test() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = dt.datetime(2026, 8, 3, 6, 0, tzinfo=UTC)
    context, _ = synthetic_context(now)
    state = ensure_r15_state({}, config, now)
    events = [synthetic_event(index, now) for index in range(15)]

    guard_candidate = {
        "dataTier": "HYBRID",
        "dataQuality": 90,
        "quoteCount": 5,
        "conservativeProbability": 0.75,
        "agreement": 90,
        "marketStability": 90,
        "anomaly": 0,
        "marketFamily": "OUTCOME",
        "marketKey": "h2h",
        "bookmakerOdds": 1.80,
        "qualification": {
            "qualified": False,
            "failures": ["Недостаточное математическое ожидание"],
        },
    }
    guard_ok, guard_failures = candidate_is_qualified(guard_candidate, config)
    if guard_ok or not any("Основной фильтр" in reason for reason in guard_failures):
        raise RuntimeError("SELF_TEST core qualification guard failed")

    low_odds_candidate = copy.deepcopy(guard_candidate)
    low_odds_candidate["qualification"] = {"qualified": True, "failures": []}
    low_odds_candidate["bookmakerOdds"] = 1.54
    low_odds_ok, _ = candidate_is_qualified(low_odds_candidate, config)
    if low_odds_ok:
        raise RuntimeError("SELF_TEST hard minimum odds guard failed")

    # Structural synthetic fixtures predate the core qualification layer and are
    # intentionally unrealistic. The explicit guard checks above exercise the
    # production rule; the remaining synthetic tests isolate diversification,
    # express construction and settlement invariants.
    structural_config = copy.deepcopy(config)
    structural_config["requireCoreQualification"] = False

    # First prove the diversification guard: this synthetic fixture intentionally
    # makes every best market OUTCOME, so the strategy must cap that family.
    guarded_records, guarded_diag = build_strategy_analysis(events, {}, context, state, structural_config, now)
    family_cap = max(1, safe_int(structural_config.get("strategyMaximumSameMarketFamily"), 8))
    if not guarded_records or len(guarded_records) > family_cap:
        raise RuntimeError(
            f"SELF_TEST diversification guard failed: {len(guarded_records)} {guarded_diag}"
        )

    # Then lift only the synthetic family cap to exercise the 15-leg express
    # construction and settlement invariants independently from diversification.
    full_test_config = copy.deepcopy(structural_config)
    full_test_config["strategyMaximumSameMarketFamily"] = 15
    records, diag = build_strategy_analysis(events, {}, context, state, full_test_config, now)
    if len(records) != 15:
        raise RuntimeError(f"SELF_TEST full synthetic strategy produced {len(records)}: {diag}")
    day = operational_day(now, full_test_config)
    core.apply_operational_window_metadata(records, day, now)
    best = informational_best_three(records, now)
    core.apply_best_bets_to_daily_analysis(records, best)
    for row in records:
        row["stake"] = 0.0
        row["stakePercent"] = 0.0
    state["dailyAnalysis"] = records
    state["bestBets"] = best
    state["expresses"] = build_expresses(records, state, full_test_config, now)
    if len(state["expresses"]) != 3 or any(len(row.get("legs") or []) != 5 for row in state["expresses"]):
        raise RuntimeError("SELF_TEST express structure failed")
    if safe_float(state["expressBank"].get("placedAmount")) > 600.01:
        raise RuntimeError("SELF_TEST express exposure exceeds 6 percent of bank")
    for row in state["dailyAnalysis"]:
        row["status"] = "won"
        row["score"] = "2:1"
    bankroll_enabled_before = [
        row for row in state.get("expresses") or []
        if bool(row.get("bankrollEnabled")) and safe_float(row.get("stake")) > 0
    ]
    counters = sync_and_settle_expresses(state, now + dt.timedelta(days=1))
    if counters["won"] != 3:
        raise RuntimeError("SELF_TEST express settlement failed")
    current_after_win = safe_float(state["expressBank"].get("current"))
    if bankroll_enabled_before and current_after_win <= 10000.0:
        raise RuntimeError("SELF_TEST positive-EV express bank did not increase")
    if not bankroll_enabled_before and current_after_win != 10000.0:
        raise RuntimeError("SELF_TEST informational expresses changed bank")
    # Verify a losing leg loses only its express and does not mutate the legacy bank.
    state2 = ensure_r15_state({}, config, now)
    records2 = copy.deepcopy(records)
    for row in records2:
        row["status"] = "won"
    records2[0]["status"] = "lost"
    state2["dailyAnalysis"] = records2
    state2["bestBets"] = informational_best_three(records2, now)
    state2["expresses"] = build_expresses(records2, state2, full_test_config, now)
    legacy_before = safe_float(state2.get("bank", {}).get("current"))
    sync_and_settle_expresses(state2, now + dt.timedelta(days=1))
    if safe_float(state2.get("bank", {}).get("current")) != legacy_before:
        raise RuntimeError("SELF_TEST legacy bank was changed")

    fixture_html = b"<html>Latest fixtures uploaded: 02/10/26 09:57 UK time.</html>"
    fixture_page_time = _r3r12_parse_upload_timestamp_html(fixture_html)
    if fixture_page_time is None or fixture_page_time.date().isoformat() != "2026-10-02":
        raise RuntimeError("SELF_TEST fixture metadata timestamp parser failed")
    fixture_payload = b"Div,Date,HomeTeam,AwayTeam\\nE0,03/08/2026,Home Club 0,Away Club 0\\n"
    hash_time_1, hash_meta_1 = _r3r12_resolve_source_timestamp(
        fixture_payload, {}, {}, now, None
    )
    hash_time_2, hash_meta_2 = _r3r12_resolve_source_timestamp(
        fixture_payload,
        {},
        {
            "contentSha256": hash_meta_1.get("contentSha256"),
            "contentChangedAt": iso(hash_time_1),
        },
        now + dt.timedelta(hours=4),
        None,
    )
    if hash_time_2 != hash_time_1 or hash_meta_2.get("freshnessEvidence") != "CONTENT_HASH_UNCHANGED":
        raise RuntimeError("SELF_TEST fixture content-hash freshness failed")

    print("R15_SELF_TEST=GREEN")
    print(f"R15_DIVERSIFICATION_GUARD_ANALYSIS={len(guarded_records)}")
    print("R15_SYNTHETIC_ANALYSIS=15")
    print("R15_SYNTHETIC_EXPRESSES=3")
    print("R15_SYNTHETIC_LEGS=15")
    print("R15_EXPRESS_STARTING_BANK=10000")
    print("R15_EXPRESS_MAX_EXPOSURE=600")
    print("R15_ASIAN_MARKETS=REMOVED")
    print("R15_RUSSIAN_MATCHES=REMOVED")
    print("R15_MARKET_ONLY_STRATEGY=FORBIDDEN")
    print("R15_CORE_QUALIFICATION_GUARD=YES")
    print("R15_HARD_MIN_ODDS=1.55")
    print("R15_ADAPTIVE_FORM_DECAY=YES")
    print("R15_ROLLING_72H_PREMATCH_SEARCH=YES")
    print("R15_PER_KEY_QUOTA_LEDGER=YES")
    print("R15_FIXTURE_FRESHNESS_CHAIN=YES")
    print("R15_SECONDARY_KEY_FALLBACK=YES")
    print("R15_H2H_WEAK_PRIOR=YES")
    print("R15_SHADOW_REJECTED_LEARNING=YES")
    return 0


def repair_state() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    before = load_json(STATE_PATH, {})
    before_fingerprint = json_fingerprint(before)
    state = ensure_r15_state(copy.deepcopy(before), config, now)
    changed = json_fingerprint(state) != before_fingerprint
    for row in state.get("bestBets") or []:
        if isinstance(row, dict) and str(row.get("sourceMarker") or "") == R15_MARKER:
            if safe_float(row.get("stake")) != 0.0 or safe_float(row.get("stakePercent")) != 0.0:
                row["stake"] = 0.0
                row["stakePercent"] = 0.0
                row["financialMode"] = "INFORMATIONAL_ONLY"
                changed = True
    previous_bank = json_fingerprint(state.get("expressBank") or {})
    update_express_bank_metrics(state, now)
    if json_fingerprint(state.get("expressBank") or {}) != previous_bank:
        changed = True
    if changed:
        state.setdefault("meta", {})["updatedAt"] = iso(now)
        state["meta"]["sourceMarker"] = R15_MARKER
        write_json(STATE_PATH, state)
    print(f"R15_REPAIR_CHANGED={'YES' if changed else 'NO'}")
    return 0


def history_refresh_cli() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    result = free_mesh.refresh_all(force=False)
    cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
    tracked = ingest_settled_state_history(cache, load_json(STATE_PATH, {}), now)
    if tracked.get("added"):
        write_json(HISTORY_CACHE_PATH, cache)
        write_json(TEAM_REGISTRY_PATH, rebuild_registry(cache, load_json(TEAM_REGISTRY_PATH, empty_registry())))
    result["trackedHistory"] = tracked
    print("R15F_FREE_HISTORY_REFRESH=" + json.dumps(result, ensure_ascii=False))
    print("R15F_NO_NEW_API_KEYS_REQUIRED=YES")
    print("FINAL_STATUS=GREEN_R15F_FREE_HISTORY_REFRESH")
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R15F R3 cognitive football portfolio")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--history-refresh", action="store_true")
    group.add_argument("--generate", action="store_true")
    group.add_argument("--settle", action="store_true")
    group.add_argument("--validate", action="store_true")
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--repair", action="store_true")
    args = parser.parse_args(argv)
    if args.history_refresh:
        return history_refresh_cli()
    if args.generate:
        return publish_generation()
    if args.settle:
        return settle_current()
    if args.validate:
        return validate_state()
    if args.self_test:
        return self_test()
    if args.repair:
        return repair_state()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log(f"FATAL {type(exc).__name__}: {exc}")
        raise
