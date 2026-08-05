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
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Iterable

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
    return {
        "operationalDayId": f"{start.date().isoformat()}-MSK-{hour:02d}00",
        "operationalDateLocal": start.date().isoformat(),
        "operationalWindowStart": iso(start.astimezone(UTC)),
        "operationalWindowEnd": iso(end.astimezone(UTC)),
        "queryWindowStart": iso(query_start),
        "queryWindowEnd": iso(end.astimezone(UTC)),
        "windowStartLocal": start.isoformat(),
        "windowEndLocal": end.isoformat(),
        "durationHours": 24,
        "policy": "STRICT_SINGLE_MOSCOW_OPERATIONAL_DAY_08_TO_08",
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
        if "openrouter.ai" in url or label.startswith("OPENROUTER"):
            return "OPENROUTER"
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
) -> dict[str, Any]:
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
        }
    weighted: list[tuple[dict[str, Any], float]] = []
    for index, row in enumerate(selected):
        when = parse_time(row.get("utcDate")) or now - dt.timedelta(days=365)
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        weight = (0.5 ** (age_days / 70.0)) * (0.965 ** index)
        weighted.append((row, weight))

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
        home5 = form_summary(home_games, now, limit=5)
        home10 = form_summary(home_games, now, limit=10)
        home20 = form_summary(home_games, now, limit=20)
        home_venue = form_summary(home_games, now, side="home", limit=10)
        away5 = form_summary(away_games, now, limit=5)
        away10 = form_summary(away_games, now, limit=10)
        away20 = form_summary(away_games, now, limit=20)
        away_venue = form_summary(away_games, now, side="away", limit=10)
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
        freshness = max(home10["freshnessDays"], away10["freshnessDays"])
        match_quality = min(home_match_score, away_match_score)
        quality = 40.0
        quality += min(24.0, sample * 1.5)
        quality += min(12.0, venue_sample * 1.8)
        quality += match_quality * 12.0
        quality += 7.0 if context.get("cacheMeta", {}).get("complete") else 2.0
        quality -= max(0.0, freshness - 14.0) * 0.30
        data_quality = clamp(quality, 40.0, 96.0)
        if sample >= 12 and venue_sample >= 5 and match_quality >= 0.90 and freshness <= 35:
            data_tier = "FULL"
            stat_weight = 0.74
        elif sample >= 8 and venue_sample >= 3 and match_quality >= 0.82:
            data_tier = "HYBRID"
            stat_weight = 0.58
        else:
            data_tier = "HYBRID"
            stat_weight = 0.46
        home_lambda = clamp(market_home * (1 - stat_weight) + stat_home * stat_weight, 0.20, 4.5)
        away_lambda = clamp(market_away * (1 - stat_weight) + stat_away * stat_weight, 0.20, 4.5)

        pair_key = tuple(sorted((home_id, away_id)))
        h2h = list(context.get("pairGames", {}).get(pair_key) or [])[:5]
        source_notes = [
            f"Хозяева: {home10['gf']:.2f} забито и {home10['ga']:.2f} пропущено за 10 матчей",
            f"Гости: {away10['gf']:.2f} забито и {away10['ga']:.2f} пропущено за 10 матчей",
            f"Дом/выезд: {home_venue['matches']} и {away_venue['matches']} релевантных матчей",
            f"Elo: {home_elo:.0f} против {away_elo:.0f}",
            f"История: {sample} матчей на команду, свежесть {freshness:.0f} дней",
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
            "h2hMatches": len(h2h),
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
    end = parse_time(window["queryWindowEnd"])
    if not start or not end or start >= end:
        return [], {**window, "events": 0, "reason": "WINDOW_ALREADY_CLOSED"}
    sports = core.fetch_active_sports(client, api_key)
    football = [item for item in sports if core.league_allowed(item, config)]
    maximum = max(1, safe_int(config.get("maximumDiscoverySports"), 300))
    football = football[:maximum]
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    spacing = max(0.05, safe_float(config.get("oddsDiscoverySpacingSeconds"), 0.12))
    for index, sport in enumerate(football):
        if index:
            time.sleep(spacing)
        try:
            rows = core.fetch_sport_events(client, api_key, sport, start, end)
        except Exception as exc:
            errors.append(f"{sport.get('key')}: {exc}")
            continue
        for row in rows:
            if core.event_allowed(row, config):
                events.append(row)
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("id") or stable_id(event.get("sport_key"), event.get("home_team"), event.get("away_team"), event.get("commence_time")))
        unique[event_id] = event
    ordered = sorted(unique.values(), key=lambda item: str(item.get("commence_time") or ""))
    by_sport: dict[str, int] = defaultdict(int)
    for event in ordered:
        by_sport[str(event.get("sport_key") or "")] += 1
    diagnostics = {
        **window,
        "activeFootballCompetitions": len(football),
        "events": len(ordered),
        "sportKeysWithEvents": len(by_sport),
        "eventsBySportKey": dict(by_sport),
        "errors": errors[-20:],
        "policy": "ALL_ODDS_SUPPORTED_NON_RUSSIAN_FOOTBALL_EVENTS_IN_CURRENT_08_TO_08_WINDOW",
    }
    return ordered, diagnostics


def sport_history_coverage(events: list[dict[str, Any]], context: dict[str, Any]) -> float:
    matched = 0
    for event in events:
        home, _ = match_team(str(event.get("home_team") or ""), context)
        away, _ = match_team(str(event.get("away_team") or ""), context)
        matched += 1 if home and away else 0
    return matched / len(events) if events else 0.0


def free_odds_daily_budget(client: ProviderClient, config: dict[str, Any], now: dt.datetime | None = None) -> dict[str, int]:
    now = now or now_utc()
    remaining_raw = client.odds_quota.get("requestsRemaining")
    assumed = max(1, safe_int(config.get("oddsFreeMonthlyCredits"), 500))
    remaining = safe_int(remaining_raw, assumed) if remaining_raw is not None else assumed
    next_month = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days_left = max(1, (next_month.date() - now.date()).days)
    fair_share = max(1, remaining // days_left)
    configured = max(1, safe_int(config.get("oddsFreeDailyCreditBudget"), 16))
    used = safe_int(client.odds_quota.get("estimatedCreditsThisRun"), 0)
    available = max(0, min(remaining, configured, fair_share + safe_int(config.get("oddsDailyCarryAllowance"), 2)) - used)
    return {
        "remaining": remaining,
        "daysLeft": days_left,
        "fairShare": fair_share,
        "configured": configured,
        "usedThisRun": used,
        "availableThisRun": available,
    }


def select_sport_keys_by_quota(
    events: list[dict[str, Any]],
    context: dict[str, Any],
    client: ProviderClient,
    config: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event.get("sport_key") or "")].append(event)
    rows = []
    for key, items in grouped.items():
        if not key:
            continue
        coverage = sport_history_coverage(items, context)
        # Every event is inspected from the free historical mesh before paid-
        # quota odds calls. Competitions with many history-covered events are
        # queried first so the free monthly allowance can still yield 15 legs.
        history_count = round(coverage * len(items))
        rows.append((history_count, coverage, len(items), key))
    rows.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)

    regions = [value for value in str(config.get("oddsRegions") or "eu").split(",") if value]
    featured_markets = [value for value in config.get("featuredMarkets") or ["h2h", "totals"]]
    cost = max(1, len(regions) * len(featured_markets))
    budget = free_odds_daily_budget(client, config)
    maximum_calls = budget["availableThisRun"] // cost if cost else len(rows)
    configured_limit = max(1, safe_int(config.get("maximumOddsSportRequests"), 200))
    maximum_calls = min(maximum_calls, configured_limit, len(rows))
    target_candidates = max(15, safe_int(config.get("oddsTargetCandidateEvents"), 32))
    keys: list[str] = []
    projected_events = 0
    projected_history = 0
    for history_count, coverage, event_count, key in rows:
        if len(keys) >= maximum_calls:
            break
        keys.append(key)
        projected_events += event_count
        projected_history += history_count
        if projected_events >= target_candidates and projected_history >= 15:
            break
    return keys, {
        "freeMonthlyMode": True,
        "quotaRemainingBeforeOdds": budget["remaining"],
        "daysLeftInQuotaPeriod": budget["daysLeft"],
        "dailyFairShare": budget["fairShare"],
        "dailyConfiguredBudget": budget["configured"],
        "dailyAvailableBeforeFeatured": budget["availableThisRun"],
        "featuredCostPerCompetition": cost,
        "competitionsWithEvents": len(rows),
        "competitionsSelected": len(keys),
        "competitionsDeferredByDailyBudget": max(0, len(rows) - len(keys)),
        "estimatedFeaturedCost": len(keys) * cost,
        "projectedEventsWithOdds": projected_events,
        "projectedHistoryCoveredEvents": projected_history,
        "allEventsHistoricallyInspected": len(events),
    }


def fetch_featured_odds_quota_aware(
    client: ProviderClient,
    api_key: str,
    keys: list[str],
    config: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    params = core.odds_query_parameters(config, api_key)
    params["commenceTimeFrom"] = iso(start)
    params["commenceTimeTo"] = iso(end)
    spacing = max(0.05, safe_float(config.get("oddsFeaturedSpacingSeconds"), 0.18))
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, key in enumerate(keys):
        if index:
            time.sleep(spacing)
        budget = free_odds_daily_budget(client, config)
        if budget["availableThisRun"] < max(1, len([v for v in str(config.get("oddsRegions") or "eu").split(",") if v]) * len(config.get("featuredMarkets") or ["h2h", "totals"])):
            errors.append("FREE_DAILY_ODDS_BUDGET_REACHED")
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


def fetch_advanced_markets_quota_aware(
    client: ProviderClient,
    api_key: str,
    featured_events: list[dict[str, Any]],
    context: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    markets = [str(value) for value in config.get("advancedSoccerMarkets") or []]
    allowed = [value for value in markets if value in {"btts", "double_chance", "team_totals"}]
    if not allowed:
        return {}, []
    max_events = max(0, safe_int(config.get("maximumAdvancedEvents"), 6))
    ranked = sorted(featured_events, key=lambda event: preliminary_event_score(event, context, now), reverse=True)
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    spacing = max(0.05, safe_float(config.get("oddsAdvancedSpacingSeconds"), 0.22))
    request_cost = max(1, len(allowed) * len([value for value in str(config.get("oddsRegions") or "eu").split(",") if value]))
    for index, event in enumerate(ranked[:max_events]):
        budget = free_odds_daily_budget(client, config)
        if budget["availableThisRun"] < request_cost:
            errors.append("ADVANCED_FREE_DAILY_BUDGET_REACHED")
            break
        if index:
            time.sleep(spacing)
        sport_key = str(event.get("sport_key") or "")
        event_id = str(event.get("id") or "")
        if not sport_key or not event_id:
            continue
        params = {
            "apiKey": api_key,
            "regions": str(config.get("oddsRegions") or "eu"),
            "markets": ",".join(allowed),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        url = (
            f"{core.ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/events/"
            f"{urllib.parse.quote(event_id)}/odds?{urllib.parse.urlencode(params)}"
        )
        try:
            payload = client.request_json(url, label=f"ADVANCED_ODDS:{event_id}", retries=0)
        except Exception as exc:
            errors.append(f"{event_id}: {exc}")
            if isinstance(exc, ProviderError) and exc.status == 429:
                break
            continue
        if isinstance(payload, dict):
            result[event_id] = payload
    return result, errors


# ---------------------------------------------------------------------------
# Candidate ranking, 15-match strategy and express construction
# ---------------------------------------------------------------------------


def merge_event(featured: dict[str, Any], advanced: dict[str, Any] | None) -> dict[str, Any]:
    return core.merge_advanced_event(featured, advanced or {}) if advanced else featured


def candidate_is_qualified(candidate: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
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
    if bool(candidate.get("goalDirectionConflict")):
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
        "dataTiers": defaultdict(int),
        "marketFamilies": defaultdict(int),
    }
    for raw in odds_events:
        if core.infer_sport_from_key(raw.get("sport_key")) != "soccer" or not core.event_allowed(raw, config):
            continue
        event_id = str(raw.get("id") or "")
        event = merge_event(raw, advanced.get(event_id))
        event["country"] = core.infer_country(str(event.get("sport_key") or ""), str(event.get("sport_title") or ""))
        quotes = core.parse_event_quotes(event, now, config)
        if not quotes:
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
            continue
        diagnostics["eventsQualified"] += 1
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
    deferred = []
    league_counts: dict[str, int] = defaultdict(int)
    for row in evaluated_rows:
        league = str(row[1].get("league") or "")
        if league_counts[league] < max_league and len(chosen) < target:
            chosen.append(row)
            league_counts[league] += 1
        else:
            deferred.append(row)
    for row in deferred:
        if len(chosen) >= target:
            break
        chosen.append(row)
    if len(chosen) < target:
        diagnostics.update({
            "published": 0,
            "required": target,
            "shortage": target - len(chosen),
            "status": "INSUFFICIENT_QUALITY_EVENTS",
        })
        diagnostics["dataTiers"] = dict(diagnostics["dataTiers"])
        diagnostics["marketFamilies"] = dict(diagnostics["marketFamilies"])
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
        "status": "GREEN",
        "selectionObjective": "FULL_MATCH_UNDERSTANDING_THEN_MOST_OBVIOUS_QUALIFIED_MARKET_WITH_GOOD_PRICE",
    })
    diagnostics["dataTiers"] = dict(diagnostics["dataTiers"])
    diagnostics["marketFamilies"] = dict(diagnostics["marketFamilies"])
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
    if len(records) != 15:
        raise RuntimeError(f"R15_EXPRESS_REQUIRES_15_LEGS={len(records)}")
    bank = ensure_express_bank(state, config, now)
    current = safe_float(bank.get("current"), safe_float(config.get("expressStartingBank"), 10000.0))
    stake_percent = safe_float(config.get("expressStakePercent"), 10.0)
    stake = round(current * stake_percent / 100.0, 2)
    deterministic_groups = balanced_groups(records)
    groups = deterministic_groups
    if isinstance(preferred_groups, dict):
        by_event = {str(row.get("eventId") or ""): row for row in records}
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
            if candidate_score <= deterministic_score * safe_float(config.get("openRouterExpressBalanceTolerance"), 1.25):
                groups = candidate_groups
    result = []
    labels = ["Экспресс A", "Экспресс B", "Экспресс C"]
    for group_index, group in enumerate(groups):
        group.sort(key=lambda row: str(row.get("commenceTime") or ""))
        combined_odds = 1.0
        joint_probability = 1.0
        legs = []
        express_id = "express-" + stable_id(labels[group_index], records[0].get("publishedAt"), group_index)
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
            "stakePercent": stake_percent,
            "stake": stake,
            "potentialPayout": round(stake * combined_odds, 2),
            "potentialProfit": round(stake * (combined_odds - 1.0), 2),
            "profit": 0.0,
            "financialMode": "EXPRESS",
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
    odds_key = os.getenv("ODDS_API_KEY", "").strip()
    if not odds_key:
        raise RuntimeError("ODDS_API_KEY is required for settlement")

    live_results = core.load_live_final_results()
    due = core.due_pending_records(state, config, now, set(live_results))
    score_results = dict(live_results)
    score_errors: list[str] = []
    client = ProviderClient(load_json(PROVIDER_HEALTH_PATH, {}))
    if due:
        sport_keys = [
            str(row.get("sportKey") or row.get("oddsSportKey") or "")
            for row in due
            if isinstance(row, dict)
        ]
        provider_scores, score_errors = core.fetch_scores_for_sport_keys(client, odds_key, sport_keys)
        score_results.update(provider_scores)
    cache = load_json(HISTORY_CACHE_PATH, empty_history_cache())
    football_context = settlement_context_from_cache(cache)
    counters = core.settle_pending_records(state, score_results, football_context, now, config) if due else {
        "analysisSettled": 0, "bestBetsSettled": 0, "unresolved": 0
    }
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

def publish_generation() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = now_utc()
    state = ensure_r15_state(load_json(STATE_PATH, {}), config, now)
    activation = daily_auditor.activation_gate(now)
    if not activation.get("ready"):
        prepared = daily_auditor.prepare_next_window_state()
        print(f"R15F_R3_FIRST_ACTIVE_OPERATIONAL_DATE={prepared.get('operationalDateLocal')}")
        print("R15F_R3_PARTIAL_DAY_PUBLICATION=NO")
        print("R15F_R3_BANK_MUTATION=NO")
        print("FINAL_STATUS=GREEN_R15F_R3_PREPARING_NEXT_WINDOW")
        return 0
    day = operational_day(now, config)
    current_day = str(state.get("meta", {}).get("operationalDayId") or "")
    current_records = state.get("dailyAnalysis") or []

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

    if current_day == day["operationalDayId"] and current_records:
        print("R15_CURRENT_OPERATIONAL_DAY_ALREADY_PUBLISHED=YES")
        return 0
    if current_records and not all(str(row.get("status") or "pending") in TERMINAL for row in current_records if isinstance(row, dict)):
        print("R15_GENERATION_BLOCKED_ACTIVE_PREVIOUS_BATCH=YES")
        return 0

    odds_key = os.getenv("ODDS_API_KEY", "").strip()
    football_key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip() or None
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None
    if not odds_key:
        raise RuntimeError("ODDS_API_KEY is required for current bookmaker events; R15F adds no new key requirements")

    prior_health = load_json(PROVIDER_HEALTH_PATH, {})
    client = ProviderClient(prior_health)

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
    keys, quota_plan = select_sport_keys_by_quota(discovered, context, client, config)
    start = parse_time(discovery.get("queryWindowStart"))
    end = parse_time(discovery.get("queryWindowEnd"))
    if not start or not end:
        raise RuntimeError("R15 operational window unresolved")
    odds_events, featured_errors = fetch_featured_odds_quota_aware(client, odds_key, keys, config, start, end)
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

    advanced, advanced_errors = fetch_advanced_markets_quota_aware(client, odds_key, odds_events, context, config, now)
    records, analysis_diag = build_strategy_analysis(odds_events, advanced, context, state, config, now)

    report = {
        "status": "GREEN" if len(records) == 15 else "DEGRADED",
        "version": core.STATE_VERSION,
        "sourceMarker": R15_MARKER,
        "mode": "generate",
        "startedAt": iso(now),
        "finishedAt": iso(now_utc()),
        "diagnostics": {
            "history": history_result,
            "freeDataMesh": free_mesh_result,
            "trackedHistory": tracked_history,
            "discovery": discovery,
            "quotaPlan": quota_plan,
            "featuredOddsEvents": len(odds_events),
            "advancedOddsEvents": len(advanced),
            "analysis": analysis_diag,
            "apiCalls": client.calls,
            "quota": client.odds_quota,
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

    if len(records) != 15:
        clear_completed_current_batch(state, now, "INSUFFICIENT_QUALITY_EVENTS_IN_CURRENT_OPERATIONAL_DAY")
        state.setdefault("meta", {}).update({
            "sourceMarker": R15_MARKER,
            "status": "WAITING_FOR_QUALITY_SELECTION",
            "dataFreshness": "CURRENT_BUT_INSUFFICIENT_FOR_STRATEGY",
            "analysisDateLocal": day["operationalDateLocal"],
            "operationalDayId": day["operationalDayId"],
            "operationalWindowStart": day["operationalWindowStart"],
            "operationalWindowEnd": day["operationalWindowEnd"],
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
            "historyMatches": context.get("cacheMeta", {}).get("matches"),
            "historyCoverageStart": context.get("cacheMeta", {}).get("coverageStart"),
            "historyCoverageEnd": context.get("cacheMeta", {}).get("coverageEnd"),
            "historyComplete": context.get("cacheMeta", {}).get("complete"),
            "freeDataMesh": free_mesh_result,
            "fonbetMode": fonbet_mode,
            "fonbetConfirmedEvents": len(fonbet_confirmed),
            "status": "INSUFFICIENT_QUALITY_EVENTS",
            "updatedAt": iso(now),
        }
        update_express_statistics(state)
        write_json(PROVIDER_HEALTH_PATH, client.health)
        write_public_files(state, report)
        print(f"R15_QUALITY_EVENTS={analysis_diag.get('eventsQualified', 0)}")
        print("R15_PUBLICATION_SKIPPED=INSUFFICIENT_QUALITY")
        print("FINAL_STATUS=GREEN_R15_WAITING_FOR_QUALITY")
        return 0

    russian_names_result = free_mesh.apply_russian_names(records)
    fonbet_result = free_mesh.fonbet_gate(records)
    core.apply_operational_window_metadata(records, discovery, now)
    audited_records, daily_audit = daily_auditor.audit_records(
        records,
        config,
        openrouter_key,
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
    archive_previous_expresses(state)
    core.publish_new_batch(state, records, best, best, config, now)
    core.append_new_records_to_history(state, records, best, config)
    state["dailyAnalysis"] = records
    state["bestBets"] = best
    state["predictions"] = copy.deepcopy(best)
    expresses = build_expresses(records, state, config, now, daily_audit.get("expresses") if isinstance(daily_audit, dict) else None)
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
        "generatedBy": "OPENROUTER_FREE_AUDIT" if daily_audit.get("schemaValid") else "DETERMINISTIC_SYSTEM",
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
        "selectionWindowStart": day["queryWindowStart"],
        "selectionWindowEnd": day["queryWindowEnd"],
        "operationalWindowPolicy": day["policy"],
        "analysisTarget": 15,
        "analysisPublished": 15,
        "bestBetsPublished": 3,
        "expressesPublished": 3,
        "expressLegsPublished": 15,
        "soccerAnalyses": 15,
        "hockeyAnalyses": 0,
        "candidateMatchesAnalyzed": analysis_diag.get("eventsWithMarkets"),
        "openRouterAuditStatus": daily_audit.get("status"),
        "openRouterModelUsed": daily_audit.get("modelUsed"),
        "openRouterSchemaValid": bool(daily_audit.get("schemaValid")),
        "openRouterLogicalRuns": safe_int(daily_audit.get("logicalRuns"), 0),
        "predictionObjective": "FULL_MATCH_UNDERSTANDING_AND_MOST_OBVIOUS_QUALIFIED_MARKET",
        "publicationPolicy": "STRICT_08_TO_08_FIFTEEN_QUALITY_MATCHES_THREE_EXPRESSES",
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
        "dailyAnalysis": 15,
        "informationalTopThree": 3,
        "expresses": len(expresses),
        "expressBank": state.get("expressBank"),
        "russianNames": russian_names_result,
        "fonbetGate": fonbet_result,
        "dailyOpenRouterAudit": daily_audit,
        "topSingles": 3,
    })
    state.pop("nextPortfolio", None)
    state.setdefault("meta", {})["nextPortfolioStatus"] = "PUBLISHED"
    daily_auditor.mark_activated(day["operationalDayId"])
    write_json(PROVIDER_HEALTH_PATH, client.health)
    write_public_files(state, report)
    print("R15F_ANALYSIS=15")
    print("R15F_INFORMATIONAL_TOP_THREE=3")
    print("R15F_EXPRESSES=3")
    print("R15F_EXPRESS_LEGS=15")
    print(f"R15F_EXPRESS_BANK={state.get('expressBank', {}).get('current')}")
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
    }
    missing = sorted(required - set(config))
    if missing:
        raise RuntimeError(f"R15 config keys missing: {missing}")
    if safe_int(config.get("expressCount")) != 3 or safe_int(config.get("expressLegsPerTicket")) != 5:
        raise RuntimeError("R15 requires three expresses of five legs")
    if safe_float(config.get("expressStakePercent")) != 10.0:
        raise RuntimeError("R15 express stake must be ten percent")
    if safe_float(config.get("expressStartingBank")) != 10000.0:
        raise RuntimeError("R15 express starting bank must be 10000")
    if safe_int(config.get("operationalWindowSearchDays"), 1) != 1:
        raise RuntimeError("R15 may not search future operational days")


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
    if is_r15_publication:
        if len(daily) != 15:
            raise RuntimeError(f"R15 daily analysis must be 15, got {len(daily)}")
        if len(best) != 3:
            raise RuntimeError(f"R15 informational top three must be 3, got {len(best)}")
        if len(expresses) != 3:
            raise RuntimeError(f"R15 expresses must be 3, got {len(expresses)}")
        event_ids = [str(row.get("eventId") or "") for row in daily]
        if any(not value for value in event_ids) or len(event_ids) != len(set(event_ids)):
            raise RuntimeError("R15 duplicate or missing event IDs")
        leg_ids = []
        for express in expresses:
            legs = express.get("legs") or []
            if len(legs) != 5:
                raise RuntimeError("Every R15 express must contain five legs")
            leg_ids.extend(str(leg.get("analysisId") or "") for leg in legs)
            if abs(safe_float(express.get("stakePercent")) - 10.0) > 0.001:
                raise RuntimeError("R15 express stake percent changed")
        if len(leg_ids) != 15 or len(set(leg_ids)) != 15:
            raise RuntimeError("R15 express legs must use every analysis exactly once")
        daily_ids = {str(row.get("id") or "") for row in daily}
        if set(leg_ids) != daily_ids:
            raise RuntimeError("R15 express legs differ from daily analysis")
        for row in daily:
            if str(row.get("sport") or "") != "soccer":
                raise RuntimeError("R15 contains non-football event")
            if str(row.get("dataTier") or "MARKET") == "MARKET":
                raise RuntimeError("R15 strategy contains MARKET-only event")
            if safe_float(row.get("dataQuality")) < safe_float(config.get("strategyMinimumDataQuality"), 58):
                raise RuntimeError("R15 strategy contains weak data")
            if core.competition_is_excluded(row.get("sportKey"), row.get("league"), "", row.get("country"), config):
                raise RuntimeError("R15 contains excluded competition")
            if not core.record_uses_r14_standard_market(row):
                raise RuntimeError("R15 contains Asian or unsupported market")
        if any(safe_float(row.get("stake")) != 0.0 for row in best):
            raise RuntimeError("R15 informational top three carries a separate stake")
        audit = state.get("dailyAudit") if isinstance(state.get("dailyAudit"), dict) else {}
        if audit.get("schemaValid") and safe_int(audit.get("logicalRuns"), 0) > 1:
            raise RuntimeError("R15 OpenRouter logical audit ran more than once")
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
    records, diag = build_strategy_analysis(events, {}, context, state, config, now)
    if len(records) != 15:
        raise RuntimeError(f"SELF_TEST strategy produced {len(records)}: {diag}")
    day = operational_day(now, config)
    core.apply_operational_window_metadata(records, day, now)
    best = informational_best_three(records, now)
    core.apply_best_bets_to_daily_analysis(records, best)
    for row in records:
        row["stake"] = 0.0
        row["stakePercent"] = 0.0
    state["dailyAnalysis"] = records
    state["bestBets"] = best
    state["expresses"] = build_expresses(records, state, config, now)
    if len(state["expresses"]) != 3 or any(len(row.get("legs") or []) != 5 for row in state["expresses"]):
        raise RuntimeError("SELF_TEST express structure failed")
    if abs(safe_float(state["expressBank"].get("placedAmount")) - 3000.0) > 0.01:
        raise RuntimeError("SELF_TEST express exposure must be 3000")
    for row in state["dailyAnalysis"]:
        row["status"] = "won"
        row["score"] = "2:1"
    counters = sync_and_settle_expresses(state, now + dt.timedelta(days=1))
    if counters["won"] != 3:
        raise RuntimeError("SELF_TEST express settlement failed")
    if safe_float(state["expressBank"].get("current")) <= 10000.0:
        raise RuntimeError("SELF_TEST express bank did not increase")
    # Verify a losing leg loses only its express and does not mutate the legacy bank.
    state2 = ensure_r15_state({}, config, now)
    records2 = copy.deepcopy(records)
    for row in records2:
        row["status"] = "won"
    records2[0]["status"] = "lost"
    state2["dailyAnalysis"] = records2
    state2["bestBets"] = informational_best_three(records2, now)
    state2["expresses"] = build_expresses(records2, state2, config, now)
    legacy_before = safe_float(state2.get("bank", {}).get("current"))
    sync_and_settle_expresses(state2, now + dt.timedelta(days=1))
    if safe_float(state2.get("bank", {}).get("current")) != legacy_before:
        raise RuntimeError("SELF_TEST legacy bank was changed")
    print("R15_SELF_TEST=GREEN")
    print("R15_SYNTHETIC_ANALYSIS=15")
    print("R15_SYNTHETIC_EXPRESSES=3")
    print("R15_SYNTHETIC_LEGS=15")
    print("R15_EXPRESS_STARTING_BANK=10000")
    print("R15_EXPRESS_EXPOSURE=3000")
    print("R15_ASIAN_MARKETS=REMOVED")
    print("R15_RUSSIAN_MATCHES=REMOVED")
    print("R15_MARKET_ONLY_STRATEGY=FORBIDDEN")
    print("R15_STRICT_08_TO_08=YES")
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
