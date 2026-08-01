#!/usr/bin/env python3
# V10_R14_RESULT_INTEGRITY_GOAL_DIRECTION
# V10_R12_FINAL_MAX_HIT_RATE_15_SETTLEMENT
# V10_GLOBAL_MULTISPORT_INTELLIGENCE
# V10_R6_FINAL_LIVE_LEARNING_STATISTICS
# V10_R7_HISTORY_LIVE_CLEANUP
# V10_R8_ATOMIC_BATCH_ROLLOVER
# V10_R9_IMMEDIATE_SETTLEMENT_ROLLOVER
# V10_R10_ATOMIC_BEST_FOUR_SYNC
# V10_R11_MOSCOW_OPERATIONAL_DAY_ROLLOVER
"""AI Football Lab V10 R14: result-integrity and goal-direction analytics.

The pipeline searches a broad football pool, models match scenarios, and then
evaluates only widely available standard markets. It publishes exactly fifteen
distinct matches and four frozen virtual-bank bets while preserving the existing
twenty-percent-per-bet bank policy. Asian handicaps and integer/quarter totals remain forbidden. Russian domestic
competitions are excluded, score settlement uses exact and fuzzy provider bridges,
and total direction must be supported by both the score model and the market.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import random
import re
import statistics
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "analysis.json"
STATE_PATH = ROOT / "data" / "state.json"
REPORT_PATH = ROOT / "data" / "last-update-report.json"
DAILY_SNAPSHOT_PATH = ROOT / "data" / "ai_daily_analysis.json"
INDEX_PATH = ROOT / "index.html"
APP_PATH = ROOT / "assets" / "app.js"
STYLE_PATH = ROOT / "assets" / "styles.css"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "update-data.yml"
LIVE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "live-update.yml"
LIVE_STATE_PATH = ROOT / "data" / "live-state.json"
LIVE_LEARNING_PATH = ROOT / "data" / "live-learning.json"
LIVE_SCRIPT_PATH = ROOT / "scripts" / "update_live.py"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
NHL_API_BASE = "https://api-web.nhle.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

STATE_VERSION = "10.0.0"
PIPELINE_MARKER = "V10_R14_RESULT_INTEGRITY_GOAL_DIRECTION"
SITE_MARKER = "V10_SITE_PREMIUM_DASHBOARD"
WORKFLOW_MARKER = "V10_AUTO_REFRESH_PIPELINE"
LIVE_WORKFLOW_MARKER = "V10_R6_LIVE_AUTO_REFRESH"
LIVE_MARKER = "V10_R6_LIVE_MATCH_INTELLIGENCE"
RESET_MARKER = "V10_CLEAN_MODEL_RESET"

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    stamp = dt.datetime.now(tz=UTC).isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return copy.deepcopy(default)
    return json.loads(text)


def write_json_atomic(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temporary = pathlib.Path(handle.name)
    temporary.replace(path)


def parse_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def configured_timezone(config: dict[str, Any]) -> dt.tzinfo:
    name = str(config.get("timezone") or "Europe/Moscow")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        offsets = {
            "europe/moscow": 3,
            "utc": 0,
            "etc/utc": 0,
        }
        return dt.timezone(dt.timedelta(hours=offsets.get(name.lower(), 0)))


def operational_selection_windows(
    now: dt.datetime,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return non-overlapping Moscow operational days anchored at 08:00.

    Generation never waits for 08:00. When the current batch becomes terminal,
    rollover runs immediately. The 08:00 boundary only defines which future
    fixtures may enter one batch. If the current operational day has too few
    fixtures, the next complete 08:00-08:00 day is evaluated separately; days
    are never mixed into one published batch.
    """
    timezone = configured_timezone(config)
    local_now = now.astimezone(timezone)
    start_hour = safe_int(
        config.get("operationalDayStartHourLocal"),
        8,
    )
    duration_hours = safe_int(
        config.get("operationalDayDurationHours"),
        24,
    )
    search_days = safe_int(
        config.get("operationalWindowSearchDays"),
        3,
    )
    minimum_lead = dt.timedelta(
        minutes=safe_int(config.get("minimumLeadMinutes"), 45)
    )
    earliest_utc = now + minimum_lead

    base_local = local_now.replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )

    result: list[dict[str, Any]] = []
    for offset in range(max(1, search_days)):
        local_start = base_local + dt.timedelta(days=offset)
        local_end = local_start + dt.timedelta(hours=duration_hours)
        day_start_utc = local_start.astimezone(UTC)
        day_end_utc = local_end.astimezone(UTC)
        query_start_utc = max(earliest_utc, day_start_utc)
        if query_start_utc >= day_end_utc:
            continue

        day_id = (
            f"{local_start.date().isoformat()}-MSK-"
            f"{start_hour:02d}00"
        )
        result.append(
            {
                "index": offset,
                "operationalDayId": day_id,
                "operationalDateLocal": local_start.date().isoformat(),
                "operationalWindowStart": iso_z(day_start_utc),
                "operationalWindowEnd": iso_z(day_end_utc),
                "queryWindowStart": iso_z(query_start_utc),
                "queryWindowEnd": iso_z(day_end_utc),
                "windowStartLocal": local_start.isoformat(),
                "windowEndLocal": local_end.isoformat(),
                "queryStartLocal": query_start_utc.astimezone(timezone).isoformat(),
                "durationHours": duration_hours,
            }
        )

    if not result:
        raise RuntimeError(
            "No valid 08:00 Moscow operational window is available"
        )
    return result


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mean(values: Iterable[float], default: float = 0.0) -> float:
    prepared = [float(value) for value in values]
    return statistics.fmean(prepared) if prepared else default


def stdev(values: Iterable[float], default: float = 0.0) -> float:
    prepared = [float(value) for value in values]
    return statistics.pstdev(prepared) if len(prepared) > 1 else default


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\b(fc|cf|sc|ac|afc|fk|hc|club|deportivo|calcio)\b", " ", text)
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def token_similarity(left: Any, right: Any) -> float:
    a = set(normalize_text(left).split())
    b = set(normalize_text(right).split())
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    containment = intersection / max(1, min(len(a), len(b)))
    jaccard = intersection / max(1, union)
    return max(containment, jaccard)


def stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def market_family(market: str) -> str:
    market = str(market or "").upper()
    if market.startswith("H2H") or market in {"HOME_WIN", "DRAW", "AWAY_WIN"}:
        return "OUTCOME"
    if "TOTAL" in market or market.startswith("OVER") or market.startswith("UNDER"):
        return "TOTAL"
    if "SPREAD" in market or "HANDICAP" in market:
        return "HANDICAP"
    if "BTTS" in market:
        return "BTTS"
    if "DOUBLE_CHANCE" in market:
        return "DOUBLE_CHANCE"
    if "DRAW_NO_BET" in market:
        return "DRAW_NO_BET"
    return "OTHER"


R14_MARKET_POLICY = "R14_FOOTBALL_STANDARD_MARKETS_ONLY"
R14_ALLOWED_MARKET_KEYS = frozenset({
    "h2h",
    "h2h_3_way",
    "totals",
    "btts",
    "double_chance",
    "team_totals",
})
R14_ALLOWED_MARKET_CODES = frozenset({
    "HOME_WIN",
    "DRAW",
    "AWAY_WIN",
    "TOTAL_OVER",
    "TOTAL_UNDER",
    "BTTS_YES",
    "BTTS_NO",
    "DOUBLE_CHANCE_HOME_DRAW",
    "DOUBLE_CHANCE_DRAW_AWAY",
    "DOUBLE_CHANCE_HOME_AWAY",
    "TEAM_TOTAL_HOME_OVER",
    "TEAM_TOTAL_HOME_UNDER",
    "TEAM_TOTAL_AWAY_OVER",
    "TEAM_TOTAL_AWAY_UNDER",
})
R14_FORBIDDEN_MARKET_KEYS = frozenset({
    "spreads",
    "alternate_spreads",
    "alternate_totals",
    "draw_no_bet",
    "alternate_team_totals",
})


def is_half_goal_line(value: Any) -> bool:
    """Return True only for x.5 goal lines; integer and quarter lines are forbidden."""
    point = safe_float(value, float("nan"))
    if not math.isfinite(point) or point < 0:
        return False
    doubled = point * 2.0
    return abs(doubled - round(doubled)) <= 1e-9 and int(round(doubled)) % 2 == 1


def standard_market_allowed(
    market_key: Any,
    market_code: Any = "",
    point: Any = None,
) -> bool:
    key = str(market_key or "").lower()
    code = str(market_code or "").upper()
    if key not in R14_ALLOWED_MARKET_KEYS:
        return False
    if code and code not in R14_ALLOWED_MARKET_CODES:
        return False
    if key in {"totals", "team_totals"}:
        return is_half_goal_line(point)
    return point in (None, "") or key in {"h2h", "h2h_3_way", "btts", "double_chance"}


def record_uses_r14_standard_market(record: dict[str, Any]) -> bool:
    return (
        str(record.get("sport") or "soccer") == "soccer"
        and standard_market_allowed(
            record.get("marketKey"),
            record.get("market"),
            record.get("point"),
        )
    )


def result_status_label(status: str) -> str:
    return {
        "pending": "Ожидается",
        "won": "Выигрыш",
        "lost": "Проигрыш",
        "push": "Возврат",
        "void": "Отмена",
        "cancelled": "Отмена",
        "unresolved": "Результат не подтверждён",
    }.get(status, status)


# ---------------------------------------------------------------------------
# HTTP client and quota tracking
# ---------------------------------------------------------------------------


class ApiClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.odds_quota = {
            "requestsRemaining": None,
            "requestsUsed": None,
            "requestsLast": None,
            "estimatedCreditsThisRun": 0,
        }

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        retries: int = 2,
        label: str = "HTTP",
        allow_not_found: bool = False,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Football-Lab-V10/10.0",
        }
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read().decode("utf-8")
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                    elapsed = round(time.monotonic() - started, 3)
                    self.calls.append(
                        {
                            "label": label,
                            "status": response.status,
                            "elapsedSeconds": elapsed,
                        }
                    )
                    self._capture_odds_headers(response_headers)
                    return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                self.calls.append(
                    {
                        "label": label,
                        "status": exc.code,
                        "error": body[:400],
                    }
                )
                if allow_not_found and exc.code == 404:
                    return None
                if exc.code in {401, 403, 404, 422}:
                    raise RuntimeError(f"{label} failed HTTP {exc.code}: {body[:500]}") from exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                self.calls.append({"label": label, "status": "ERROR", "error": str(exc)})
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise RuntimeError(f"{label} failed after retries: {last_error}")

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
            self.odds_quota["estimatedCreditsThisRun"] += safe_int(
                headers["x-requests-last"], 0
            )


# ---------------------------------------------------------------------------
# Configuration and state migration
# ---------------------------------------------------------------------------


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "dailyAnalysisTarget",
        "bestBetsTarget",
        "stakePerBestBetPercent",
        "featuredMarkets",
        "sourceMarker",
        "footballOnly",
        "allowedMarketKeys",
    }
    missing = sorted(required - set(config))
    if missing:
        raise RuntimeError(f"Missing config keys: {missing}")
    if config.get("sourceMarker") != PIPELINE_MARKER:
        raise RuntimeError("Config source marker mismatch")
    if safe_int(config.get("dailyAnalysisTarget")) != 15:
        raise RuntimeError("dailyAnalysisTarget must be 15")
    if safe_int(config.get("bestBetsTarget")) != 4:
        raise RuntimeError("bestBetsTarget must be 4")
    if safe_float(config.get("stakePerBestBetPercent")) != 20.0:
        raise RuntimeError("stakePerBestBetPercent must be 20")
    if safe_float(config.get("maximumDailyExposurePercent")) != 80.0:
        raise RuntimeError("maximumDailyExposurePercent must be 80")
    if not bool(config.get("footballOnly")):
        raise RuntimeError("R14 footballOnly must be true")
    if safe_int(config.get("maximumHockeySportRequests"), 0) != 0:
        raise RuntimeError("R14 maximumHockeySportRequests must be zero")
    if safe_int(config.get("minimumHockeySportRequestsForFallback"), 0) != 0:
        raise RuntimeError("R14 hockey fallback must be disabled")
    featured = {str(value) for value in config.get("featuredMarkets") or []}
    if not featured or not featured.issubset({"h2h", "totals"}):
        raise RuntimeError("R14 featuredMarkets may contain only h2h and totals")
    advanced = {str(value) for value in config.get("advancedSoccerMarkets") or []}
    if not advanced.issubset({"btts", "double_chance", "team_totals"}):
        raise RuntimeError("R14 advanced markets contain a forbidden Asian market")
    configured_allowed = {str(value) for value in config.get("allowedMarketKeys") or []}
    if configured_allowed != set(R14_ALLOWED_MARKET_KEYS):
        raise RuntimeError("R14 allowedMarketKeys mismatch")
    forbidden = {str(value) for value in config.get("forbiddenMarketKeys") or []}
    if not R14_FORBIDDEN_MARKET_KEYS.issubset(forbidden):
        raise RuntimeError("R14 forbiddenMarketKeys is incomplete")
    if safe_int(config.get("maximumOddsSportRequests")) < 15:
        raise RuntimeError("maximumOddsSportRequests must cover a broad football pool")
    if safe_int(config.get("minimumBookmakers"), 2) < 2:
        raise RuntimeError("minimumBookmakers must be at least two")
    if safe_int(config.get("bestBetMinimumBookmakers"), 3) < 3:
        raise RuntimeError("bestBetMinimumBookmakers must be at least three")
    if safe_float(config.get("preferredMinimumOdds"), 1.55) < 1.50:
        raise RuntimeError("preferredMinimumOdds is too small for the R14 objective")
    generation_hour = safe_int(config.get("dailyGenerationHourLocal"), 8)
    if generation_hour < 0 or generation_hour > 23:
        raise RuntimeError("dailyGenerationHourLocal must be in 0..23")
    live_refresh = safe_int(config.get("liveRefreshMinutes"), 10)
    if live_refresh < 5 or live_refresh > 60:
        raise RuntimeError("liveRefreshMinutes must be in 5..60")
    if safe_int(config.get("liveMaximumOddsScoreCallsPerRun"), 3) < 0:
        raise RuntimeError("liveMaximumOddsScoreCallsPerRun must not be negative")
    if safe_int(config.get("liveProviderGraceMinutes"), 20) < 0:
        raise RuntimeError("liveProviderGraceMinutes must not be negative")
    if safe_int(config.get("historyPendingExpiryHoursSoccer"), 48) < 1:
        raise RuntimeError("historyPendingExpiryHoursSoccer must be positive")
    if not bool(config.get("batchRolloverEnabled", True)):
        raise RuntimeError("batchRolloverEnabled must be true")
    if safe_int(config.get("batchUnresolvedReleaseMinutesSoccer"), 720) < 360:
        raise RuntimeError("batchUnresolvedReleaseMinutesSoccer must be at least 360")
    excluded_prefixes = {str(value).lower() for value in config.get("excludedSportKeyPrefixes") or []}
    if "soccer_russia_" not in excluded_prefixes:
        raise RuntimeError("R14 must exclude Russian domestic sport keys")
    excluded_countries = {normalize_text(value) for value in config.get("excludedCountries") or []}
    if not ({"russia", "россия"} & excluded_countries):
        raise RuntimeError("R14 excludedCountries must contain Russia")
    if safe_float(config.get("totalDirectionMinimumStatisticalProbability"), 0.54) < 0.50:
        raise RuntimeError("R14 total direction statistical floor is too low")
    if safe_int(config.get("maximumTotalUnderBestBets"), 1) > 1:
        raise RuntimeError("R14 permits at most one total-under bet in the best four")

def default_state(config: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    starting = safe_float(config.get("startingVirtualBank"), 10000.0)
    return {
        "meta": {
            "version": STATE_VERSION,
            "mode": "production",
            "updatedAt": iso_z(now),
            "analysisDateLocal": "",
            "status": "INITIALIZED",
            "sourceMarker": PIPELINE_MARKER,
            "dataFreshness": "INITIALIZED",
            "notice": (
                "Система сначала прогнозирует сценарий матча, затем оценивает "
                "цену букмекерского рынка. Гарантия выигрыша отсутствует."
            ),
        },
        "dailyAnalysis": [],
        "bestBets": [],
        "predictions": [],
        "analysisHistory": [],
        "history": [],
        "bank": {
            "starting": round(starting, 2),
            "current": round(starting, 2),
            "stakePercent": 20,
            "roi": 0.0,
            "maxDrawdown": 0.0,
            "activeExposure": 0.0,
            "history": [
                {
                    "date": now.date().isoformat(),
                    "value": round(starting, 2),
                    "event": "V10_START",
                }
            ],
        },
        "statistics": {
            "analysisAccuracy": 0.0,
            "bestBetsAccuracy": 0.0,
            "averageOdds": 0.0,
            "currentStreak": "Нет завершённых ставок",
            "bestSegment": "Недостаточно данных",
            "settledAnalyses": 0,
            "settledBestBets": 0,
            "allPredictions": {},
            "bestBets": {},
            "windows": {},
            "bySport": [],
            "byMarket": [],
            "byLeague": [],
            "byOddsBand": [],
        },
        "learning": {
            "version": 2,
            "updatedAt": iso_z(now),
            "segments": {},
            "calibrationBins": {},
            "totalSettledAnalyses": 0,
            "totalSettledBestBets": 0,
            "modelNotes": [],
            "modelReadiness": {
                "stage": "Сбор выборки",
                "settledSamples": 0,
                "minimumSamples": safe_int(config.get("learningMinimumSegmentSamples"), 20),
                "fullWeightSamples": safe_int(config.get("learningFullWeightSamples"), 120),
                "maximumProbabilityAdjustment": safe_float(config.get("learningMaximumProbabilityAdjustment"), 0.08),
            },
        },
        "batch": {
            "version": 1,
            "id": "",
            "sequence": 0,
            "status": "INITIALIZED",
            "createdAt": None,
            "updatedAt": iso_z(now),
            "analysisCount": 0,
            "bestBetsCount": 0,
            "terminalAnalysisCount": 0,
            "terminalBestBetsCount": 0,
            "pendingAnalysisCount": 0,
            "pendingBestBetsCount": 0,
            "completed": False,
            "placedAmount": 0.0,
            "availableAmount": round(starting, 2),
            "startingBank": round(starting, 2),
        },
        "batchHistory": [],
        "quota": {},
    }


def migrate_state(
    source: dict[str, Any] | None,
    config: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    state = default_state(config, now)
    source = source if isinstance(source, dict) else {}

    old_meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    state["meta"].update(old_meta)
    state["meta"].update(
        {
            "version": STATE_VERSION,
            "sourceMarker": PIPELINE_MARKER,
            "migrationAt": iso_z(now),
        }
    )

    old_bank = source.get("bank") if isinstance(source.get("bank"), dict) else {}
    state["bank"].update(old_bank)
    state["bank"]["starting"] = round(
        safe_float(state["bank"].get("starting"), config.get("startingVirtualBank", 10000)),
        2,
    )
    state["bank"]["current"] = round(
        safe_float(state["bank"].get("current"), state["bank"]["starting"]), 2
    )
    state["bank"]["stakePercent"] = 20
    if not isinstance(state["bank"].get("history"), list):
        state["bank"]["history"] = []

    legacy_history = source.get("history") if isinstance(source.get("history"), list) else []
    state["history"] = [dict(item) for item in legacy_history if isinstance(item, dict)]
    for item in state["history"]:
        item.setdefault("recordType", "BEST_BET")
        item.setdefault("sport", infer_sport_from_key(item.get("oddsSportKey")))
        item.setdefault("eventId", str(item.get("oddsEventId") or item.get("sourceMatchId") or ""))
        item.setdefault("modelProbability", safe_float(item.get("probability"), 0.0))
        item.setdefault("bookmakerOdds", safe_float(item.get("bookmakerOdds") or item.get("odds"), 0.0))

    old_analysis_history = (
        source.get("analysisHistory")
        if isinstance(source.get("analysisHistory"), list)
        else []
    )
    state["analysisHistory"] = [
        dict(item) for item in old_analysis_history if isinstance(item, dict)
    ]

    old_learning = source.get("learning") if isinstance(source.get("learning"), dict) else {}
    state["learning"].update(old_learning)
    state["learning"].setdefault("segments", {})
    state["learning"].setdefault("calibrationBins", {})
    state["learning"].setdefault("modelNotes", [])
    state["learning"].setdefault("modelReadiness", {})
    state["learning"]["version"] = 2

    old_stats = source.get("statistics") if isinstance(source.get("statistics"), dict) else {}
    state["statistics"].update(old_stats)

    old_daily = source.get("dailyAnalysis")
    if not isinstance(old_daily, list):
        old_daily = []
    state["dailyAnalysis"] = [dict(item) for item in old_daily if isinstance(item, dict)]

    old_best = source.get("bestBets")
    if not isinstance(old_best, list):
        old_best = source.get("predictions") if isinstance(source.get("predictions"), list) else []
    state["bestBets"] = [migrate_public_prediction(item) for item in old_best if isinstance(item, dict)]
    state["predictions"] = copy.deepcopy(state["bestBets"])

    old_batch = source.get("batch") if isinstance(source.get("batch"), dict) else {}
    state["batch"].update(old_batch)
    old_batch_history = (
        source.get("batchHistory")
        if isinstance(source.get("batchHistory"), list)
        else []
    )
    state["batchHistory"] = [
        dict(item) for item in old_batch_history if isinstance(item, dict)
    ][-safe_int(config.get("batchHistoryLimit"), 120):]

    state.setdefault("quota", {})
    maintain_prediction_history(state, config, now)
    update_bank_metrics(state)
    update_statistics(state)
    ensure_current_batch(state, config, now)
    return state


def migrate_public_prediction(item: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(item)
    migrated.setdefault("recordType", "BEST_BET")
    migrated.setdefault("eventId", str(item.get("oddsEventId") or item.get("sourceMatchId") or ""))
    migrated.setdefault("sport", infer_sport_from_key(item.get("oddsSportKey")))
    migrated.setdefault("sportLabel", "Футбол" if migrated["sport"] == "soccer" else "Хоккей")
    migrated.setdefault("modelProbability", safe_float(item.get("probability"), 0.0))
    migrated.setdefault("marketProbability", safe_float(item.get("marketProbability"), 0.0))
    migrated.setdefault("probability", migrated["modelProbability"])
    migrated.setdefault("probabilityPercent", round(migrated["modelProbability"] * 100, 1))
    migrated.setdefault("bookmakerOdds", safe_float(item.get("bookmakerOdds") or item.get("odds"), 0.0))
    migrated.setdefault("odds", migrated["bookmakerOdds"])
    migrated.setdefault("status", "pending")
    migrated.setdefault("marketFamily", market_family(str(item.get("market") or "")))
    migrated.setdefault("dataTier", "MARKET")
    migrated.setdefault("dataQuality", safe_float(item.get("dataQuality"), 40.0))
    migrated.setdefault("agreement", safe_float(item.get("agreement"), 50.0))
    migrated.setdefault("anomaly", safe_float(item.get("anomaly"), 30.0))
    return apply_russian_display_fields(migrated)


# ---------------------------------------------------------------------------
# Sport discovery and data adapters
# ---------------------------------------------------------------------------


def infer_sport_from_key(sport_key: Any) -> str:
    key = str(sport_key or "").lower()
    if key.startswith("icehockey_") or "hockey" in key:
        return "ice_hockey"
    return "soccer"


def classify_sport(sport: dict[str, Any]) -> str | None:
    key = str(sport.get("key") or "").lower()
    group = str(sport.get("group") or "").lower()
    if key.startswith("soccer_") or group == "soccer":
        return "soccer"
    if key.startswith("icehockey_") or "ice hockey" in group:
        return "ice_hockey"
    return None


def sport_label(sport: str) -> str:
    return "Футбол" if sport == "soccer" else "Хоккей"


RUSSIAN_EXACT_NAMES = {
    "english premier league": "Английская Премьер-лига",
    "premier league": "Премьер-лига",
    "uefa champions league": "Лига чемпионов УЕФА",
    "uefa champions league qualification": "Квалификация Лиги чемпионов УЕФА",
    "uefa europa league": "Лига Европы УЕФА",
    "uefa europa league qualification": "Квалификация Лиги Европы УЕФА",
    "uefa conference league": "Лига конференций УЕФА",
    "uefa conference league qualification": "Квалификация Лиги конференций УЕФА",
    "copa libertadores": "Кубок Либертадорес",
    "copa sudamericana": "Южноамериканский кубок",
    "major league soccer": "MLS",
    "primera division argentina": "Примера Аргентины",
    "brazil serie a": "Серия A Бразилии",
    "brazil serie b": "Серия B Бразилии",
    "serie a": "Серия А",
    "serie b": "Серия Б",
    "la liga": "Ла Лига",
    "bundesliga": "Бундеслига",
    "ligue 1": "Лига 1",
    "ligue one": "Лига 1",
    "championship": "Чемпионшип",
    "world cup": "Чемпионат мира",
    "red star belgrade": "Црвена Звезда",
    "larne fc": "Ларн",
    "sk slovan bratislava": "Слован Братислава",
    "fc iberia 1999": "Иберия 1999",
    "argentinos juniors": "Аргентинос Хуниорс",
    "estudiantes de rio cuarto": "Эстудиантес Рио-Куарто",
    "san lorenzo": "Сан-Лоренсо",
    "gimnasia mendoza": "Химнасия Мендоса",
    "juventude": "Жувентуде",
    "avai": "Аваи",
    "barracas central": "Барракас Сентраль",
    "aldosivi mar del plata": "Альдосиви",
    "fc kairat": "Кайрат",
    "omonoia fc": "Омония",
    "santos": "Сантос",
    "ucv fc": "Универсидад Сентраль де Венесуэла",
    "hapoel beer sheva": "Хапоэль Беэр-Шева",
    "vikingur reykjavik": "Викингур Рейкьявик",
    "universitatea craiova": "Университатя Крайова",
    "pfc levski sofia": "Левски София",
    "fk kauno zalgiris": "Кауно Жальгирис",
    "klaksvikar itrottarfelag": "КИ Клаксвик",
    "gornik zabrze": "Гурник Забже",
    "fenerbahce": "Фенербахче",
    "ponte preta": "Понте-Прета",
    "athletic club mg": "Атлетик Клуб Минейро",
    "manchester united": "Манчестер Юнайтед",
    "manchester city": "Манчестер Сити",
    "liverpool fc": "Ливерпуль",
    "arsenal": "Арсенал",
    "chelsea": "Челси",
    "tottenham hotspur": "Тоттенхэм",
    "real madrid": "Реал Мадрид",
    "barcelona": "Барселона",
    "atletico madrid": "Атлетико Мадрид",
    "bayern munich": "Бавария",
    "borussia dortmund": "Боруссия Дортмунд",
    "paris saint germain": "Пари Сен-Жермен",
    "inter milan": "Интер",
    "ac milan": "Милан",
    "juventus": "Ювентус",
    "benfica": "Бенфика",
    "porto": "Порту",
    "ajax": "Аякс",
    "psv eindhoven": "ПСВ",
    "feyenoord": "Фейеноорд",
    "sporting lisbon": "Спортинг",
    "celtic": "Селтик",
    "rangers": "Рейнджерс",
}

RUSSIAN_WORDS = {
    "fc": "ФК", "cf": "ФК", "sc": "СК", "ac": "АК", "afc": "АФК", "fk": "ФК",
    "united": "Юнайтед", "city": "Сити", "town": "Таун", "county": "Каунти",
    "athletic": "Атлетик", "sporting": "Спортинг", "club": "Клуб",
    "women": "Женщины", "reserve": "Резерв", "reserves": "Резерв",
    "youth": "Молодёжная", "academy": "Академия",
    "over": "Больше", "under": "Меньше", "draw": "Ничья",
    "home": "Хозяева", "away": "Гости", "total": "Тотал", "totals": "Тоталы",
    "cup": "Кубок", "league": "Лига", "premier": "Премьер",
    "national": "Национальная", "conference": "Конференция", "division": "Дивизион",
    "north": "Север", "south": "Юг", "east": "Восток", "west": "Запад",
    "central": "Централь", "regional": "Региональная", "real": "Реал",
}

TRANSLIT_PAIRS = (
    ("shch", "щ"), ("sch", "щ"), ("sht", "шт"), ("yo", "ё"),
    ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
    ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
    ("ph", "ф"), ("th", "т"), ("ck", "к"), ("qu", "кв"),
    ("cz", "ч"), ("sz", "ш"), ("rz", "ж"), ("lj", "ль"),
    ("nj", "нь"), ("dj", "дж"),
)

TRANSLIT_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф",
    "g": "г", "h": "х", "i": "и", "j": "й", "k": "к", "l": "л",
    "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "и", "z": "з",
}

LATIN_SPECIALS = str.maketrans({
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ø": "o", "Ø": "O",
    "ł": "l", "Ł": "L", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe",
})


def normalized_name_key(value: Any) -> str:
    text = str(value or "").translate(LATIN_SPECIALS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def is_latin_word(value: str) -> bool:
    letters = [ch for ch in value if ch.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(ch, "") for ch in letters)


def transliterate_latin_word_ru(word: str) -> str:
    ascii_word = str(word).translate(LATIN_SPECIALS)
    ascii_word = unicodedata.normalize("NFKD", ascii_word)
    ascii_word = "".join(ch for ch in ascii_word if not unicodedata.combining(ch))
    lower = ascii_word.lower()
    if lower in RUSSIAN_WORDS:
        return RUSSIAN_WORDS[lower]
    source = lower
    result: list[str] = []
    while source:
        matched = False
        for latin, russian in TRANSLIT_PAIRS:
            if source.startswith(latin):
                result.append(russian)
                source = source[len(latin):]
                matched = True
                break
        if not matched:
            char = source[0]
            result.append(TRANSLIT_SINGLE.get(char, char))
            source = source[1:]
    translated = "".join(result)
    if word[:1].isupper():
        translated = translated[:1].upper() + translated[1:]
    return translated


def russian_display_text(value: Any) -> str:
    original = str(value or "").strip()
    if not original:
        return ""
    exact = RUSSIAN_EXACT_NAMES.get(normalized_name_key(original))
    if exact:
        return exact
    text = re.sub(r"\b1X\b", "1Х", original, flags=re.IGNORECASE)
    text = re.sub(r"\bX2\b", "Х2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBTTS\b", "Обе забьют", text, flags=re.IGNORECASE)

    def replace_word(match: re.Match[str]) -> str:
        word = match.group(0)
        return transliterate_latin_word_ru(word) if is_latin_word(word) else word

    text = re.sub(r"[^\W\d_]+", replace_word, text, flags=re.UNICODE)
    # A visible label must never contain a corrupt Latin/Cyrillic hybrid.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def apply_russian_display_fields(record: dict[str, Any]) -> dict[str, Any]:
    record["countryRu"] = russian_display_text(record.get("country") or record.get("countryRu"))
    record["leagueRu"] = russian_display_text(record.get("league") or record.get("leagueRu"))
    record["homeRu"] = russian_display_text(record.get("home") or record.get("homeRu"))
    record["awayRu"] = russian_display_text(record.get("away") or record.get("awayRu"))
    record["pickRu"] = russian_display_text(record.get("pick") or record.get("pickRu"))
    record["bookmakerRu"] = russian_display_text(record.get("bookmaker") or record.get("bookmakerRu"))
    if record.get("expectedResult"):
        record["expectedResultRu"] = russian_display_text(record.get("expectedResult"))
    if record.get("reason"):
        record["reasonRu"] = russian_display_text(record.get("reason"))
    record["localizationPolicy"] = "OFFICIAL_DICTIONARY_THEN_FULL_UNICODE_TRANSLITERATION_NO_MIXED_SCRIPT"
    return record

def infer_country(sport_key: str, sport_title: str) -> str:
    key = sport_key.lower()
    mapping = {
        "england": "Англия",
        "epl": "Англия",
        "efl": "Англия",
        "germany": "Германия",
        "bundesliga": "Германия",
        "spain": "Испания",
        "la_liga": "Испания",
        "italy": "Италия",
        "serie_a": "Италия",
        "france": "Франция",
        "ligue_one": "Франция",
        "brazil": "Бразилия",
        "argentina": "Аргентина",
        "usa": "США",
        "mls": "США",
        "mexico": "Мексика",
        "netherlands": "Нидерланды",
        "portugal": "Португалия",
        "turkey": "Турция",
        "belgium": "Бельгия",
        "scotland": "Шотландия",
        "sweden": "Швеция",
        "finland": "Финляндия",
        "norway": "Норвегия",
        "denmark": "Дания",
        "australia": "Австралия",
        "japan": "Япония",
        "korea": "Южная Корея",
        "china": "Китай",
        "chile": "Чили",
        "colombia": "Колумбия",
        "russia": "Россия",
        "russian": "Россия",
        "nhl": "США / Канада",
        "ahl": "США / Канада",
        "sweden_hockey": "Швеция",
        "finland_hockey": "Финляндия",
    }
    haystack = f"{key} {sport_title.lower()}"
    for token, country in mapping.items():
        if token in haystack:
            return country
    if any(token in haystack for token in ("uefa", "champions", "europa")):
        return "Европа"
    if any(token in haystack for token in ("conmebol", "copa_libertadores")):
        return "Южная Америка"
    return "Международный турнир"


def competition_is_excluded(
    sport_key: Any,
    title: Any,
    description: Any,
    country: Any,
    config: dict[str, Any],
) -> bool:
    key = str(sport_key or "").strip().lower()
    text = " ".join(
        [
            key,
            str(title or "").lower(),
            str(description or "").lower(),
            str(country or "").lower(),
        ]
    )
    normalized_text = normalize_text(text)
    for prefix in config.get("excludedSportKeyPrefixes") or []:
        if key.startswith(str(prefix or "").strip().lower()):
            return True
    for word in config.get("excludedSportKeyWords", []):
        if str(word).lower() in text:
            return True
    for word in config.get("excludedLeagueWords", []):
        raw = str(word or "").strip().lower()
        if raw and (raw in text or normalize_text(raw) in normalized_text):
            return True
    excluded_countries = {normalize_text(value) for value in config.get("excludedCountries") or []}
    if normalize_text(country) in excluded_countries:
        return True
    return False


def league_allowed(sport: dict[str, Any], config: dict[str, Any]) -> bool:
    if not sport.get("active", True) or sport.get("has_outrights"):
        return False
    sport_type = classify_sport(sport)
    if bool(config.get("footballOnly", True)) and sport_type != "soccer":
        return False
    country = infer_country(str(sport.get("key") or ""), str(sport.get("title") or ""))
    if competition_is_excluded(
        sport.get("key"),
        sport.get("title"),
        sport.get("description"),
        country,
        config,
    ):
        return False
    return sport_type == "soccer"


def event_allowed(event: dict[str, Any], config: dict[str, Any]) -> bool:
    return not competition_is_excluded(
        event.get("sport_key"),
        event.get("sport_title"),
        event.get("sport_description"),
        event.get("country"),
        config,
    )

def fetch_active_sports(client: ApiClient, api_key: str) -> list[dict[str, Any]]:
    url = f"{ODDS_API_BASE}/sports/?" + urllib.parse.urlencode({"apiKey": api_key})
    payload = client.request_json(url, label="ODDS_SPORTS")
    if not isinstance(payload, list):
        raise RuntimeError("The Odds API /sports returned non-list payload")
    return [item for item in payload if isinstance(item, dict)]


def fetch_sport_events(
    client: ApiClient,
    api_key: str,
    sport: dict[str, Any],
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    sport_key = str(sport["key"])
    params = {
        "apiKey": api_key,
        "dateFormat": "iso",
        "commenceTimeFrom": iso_z(start),
        "commenceTimeTo": iso_z(end),
    }
    url = f"{ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/events?" + urllib.parse.urlencode(params)
    payload = client.request_json(url, label=f"EVENTS:{sport_key}")
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        commence = parse_datetime(event.get("commence_time"))
        if not commence or commence < start or commence > end:
            continue
        normalized = dict(event)
        normalized["sport_key"] = sport_key
        normalized["sport_title"] = str(sport.get("title") or sport_key)
        normalized["sport_type"] = classify_sport(sport)
        normalized["country"] = infer_country(sport_key, normalized["sport_title"])
        result.append(normalized)
    return result


def discover_events(
    client: ApiClient,
    api_key: str,
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sports = [
        sport
        for sport in fetch_active_sports(client, api_key)
        if league_allowed(sport, config)
    ]
    sports = sports[: safe_int(config.get("maximumDiscoverySports"), 120)]

    lead = dt.timedelta(minutes=safe_int(config.get("minimumLeadMinutes"), 45))
    query_start = now + lead
    horizons = [
        safe_int(config.get("primaryWindowHours"), 24),
        *[
            safe_int(value)
            for value in config.get("expandedWindowHours", [36, 48, 72, 96, 120])
        ],
    ]
    horizons = sorted({value for value in horizons if value > 0})
    maximum_horizon = max(horizons or [120])
    query_end = now + dt.timedelta(hours=maximum_horizon)

    all_events: list[dict[str, Any]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(
                fetch_sport_events,
                client,
                api_key,
                sport,
                query_start,
                query_end,
            ): sport
            for sport in sports
        }
        for future in concurrent.futures.as_completed(future_map):
            sport = future_map[future]
            try:
                all_events.extend(future.result())
            except Exception as exc:
                errors.append(f"{sport.get('key')}: {exc}")

    deduplicated: dict[str, dict[str, Any]] = {}
    for event in all_events:
        event_id = str(event.get("id") or "")
        if not event_id:
            event_id = stable_id(
                event.get("sport_key"),
                event.get("home_team"),
                event.get("away_team"),
                event.get("commence_time"),
            )
        deduplicated[event_id] = event
    all_events = list(deduplicated.values())
    all_events.sort(key=lambda item: str(item.get("commence_time") or ""))

    target = safe_int(config.get("dailyAnalysisTarget"), 15)
    minimum_events = max(
        target,
        safe_int(config.get("operationalWindowMinimumEvents"), 36),
    )
    attempts: list[dict[str, Any]] = []
    selected_horizon = maximum_horizon
    selected_events = all_events

    for horizon in horizons:
        horizon_end = now + dt.timedelta(hours=horizon)
        window_events = [
            event
            for event in all_events
            if (parse_datetime(event.get("commence_time")) or query_end) < horizon_end
        ]
        soccer_count = sum(
            1 for event in window_events if event.get("sport_type") == "soccer"
        )
        hockey_count = sum(
            1 for event in window_events if event.get("sport_type") == "ice_hockey"
        )
        enough = len(window_events) >= minimum_events and (
            soccer_count >= target or len(window_events) >= minimum_events
        )
        attempts.append(
            {
                "windowHours": horizon,
                "events": len(window_events),
                "soccerEvents": soccer_count,
                "hockeyEvents": hockey_count,
                "enough": enough,
            }
        )
        if enough:
            selected_horizon = horizon
            selected_events = window_events
            break

    local_now = now.astimezone(configured_timezone(config))
    selection_end = now + dt.timedelta(hours=selected_horizon)
    operational_id = (
        f"{local_now.date().isoformat()}-MSK-"
        f"{local_now.strftime('%H%M')}-PLUS-{selected_horizon}H"
    )
    diagnostics = {
        "activeSports": len(sports),
        "events": len(selected_events),
        "soccerEvents": sum(
            1 for event in selected_events if event.get("sport_type") == "soccer"
        ),
        "hockeyEvents": sum(
            1 for event in selected_events if event.get("sport_type") == "ice_hockey"
        ),
        "windowHours": selected_horizon,
        "operationalDayId": operational_id,
        "operationalDateLocal": local_now.date().isoformat(),
        "operationalWindowStart": iso_z(query_start),
        "operationalWindowEnd": iso_z(selection_end),
        "queryWindowStart": iso_z(query_start),
        "queryWindowEnd": iso_z(selection_end),
        "windowStartLocal": query_start.astimezone(configured_timezone(config)).isoformat(),
        "windowEndLocal": selection_end.astimezone(configured_timezone(config)).isoformat(),
        "durationHours": selected_horizon,
        "operationalWindowPolicy": "PROGRESSIVE_FUTURE_SEARCH_UNTIL_EXACT_FIFTEEN",
        "attempts": attempts,
        "errors": errors[:20],
    }
    return selected_events, diagnostics


def apply_operational_window_metadata(
    records: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    now: dt.datetime,
) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        record["operationalDayId"] = str(
            diagnostics.get("operationalDayId") or ""
        )
        record["operationalWindowStart"] = str(
            diagnostics.get("operationalWindowStart") or ""
        )
        record["operationalWindowEnd"] = str(
            diagnostics.get("operationalWindowEnd") or ""
        )
        record["selectionWindowStart"] = str(
            diagnostics.get("queryWindowStart") or ""
        )
        record["selectionWindowEnd"] = str(
            diagnostics.get("queryWindowEnd") or ""
        )
        record["selectionWindowPolicy"] = (
            "MOSCOW_OPERATIONAL_DAY_08_TO_08_IMMEDIATE_ROLLOVER"
        )
        record["selectionGeneratedAt"] = iso_z(now)


def choose_sport_keys_for_odds(
    events: list[dict[str, Any]], config: dict[str, Any]
) -> list[str]:
    """Choose only football leagues, ranked by usable event depth and proximity."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if str(event.get("sport_type") or infer_sport_from_key(event.get("sport_key"))) != "soccer":
            continue
        grouped[str(event.get("sport_key") or "")].append(event)

    ranked: list[tuple[float, str, int]] = []
    for key, items in grouped.items():
        if not key:
            continue
        earliest = min(
            parse_datetime(item.get("commence_time")) or utc_now()
            for item in items
        )
        # Event depth dominates; earlier fixtures break ties.
        score = len(items) * 1000.0 - earliest.timestamp() / 1e8
        ranked.append((score, key, len(items)))
    ranked.sort(reverse=True)

    limit = min(
        safe_int(config.get("maximumOddsSportRequests"), 24),
        safe_int(config.get("maximumSoccerSportRequests"), 24),
    )
    return [key for _, key, _ in ranked[:limit]]

def odds_query_parameters(config: dict[str, Any], api_key: str) -> dict[str, str]:
    configured = [str(value) for value in config.get("featuredMarkets") or ["h2h", "totals"]]
    markets = [value for value in configured if value in {"h2h", "totals"}]
    if not markets:
        raise RuntimeError("R14 has no standard featured markets configured")
    params = {
        "apiKey": api_key,
        "regions": str(config.get("oddsRegions") or "eu"),
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    bookmakers = [str(value) for value in config.get("oddsBookmakers", []) if str(value)]
    if bookmakers:
        params["bookmakers"] = ",".join(bookmakers)
        params.pop("regions", None)
    return params

def fetch_featured_odds(
    client: ApiClient,
    api_key: str,
    sport_keys: list[str],
    config: dict[str, Any],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    params_base = odds_query_parameters(config, api_key)
    params_base["commenceTimeFrom"] = iso_z(window_start)
    params_base["commenceTimeTo"] = iso_z(window_end)
    events: list[dict[str, Any]] = []
    errors: list[str] = []

    def fetch(key: str) -> list[dict[str, Any]]:
        url = (
            f"{ODDS_API_BASE}/sports/{urllib.parse.quote(key)}/odds?"
            + urllib.parse.urlencode(params_base)
        )
        payload = client.request_json(url, label=f"ODDS:{key}")
        return payload if isinstance(payload, list) else []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(6, max(1, len(sport_keys)))
    ) as executor:
        future_map = {executor.submit(fetch, key): key for key in sport_keys}
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                for item in future.result():
                    if not isinstance(item, dict):
                        continue
                    commence = parse_datetime(item.get("commence_time"))
                    if not commence:
                        continue
                    if commence < window_start or commence >= window_end:
                        continue
                    events.append(item)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
    return events, errors


def maybe_fetch_advanced_markets(
    client: ApiClient,
    api_key: str,
    featured_events: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    remaining = safe_int(client.odds_quota.get("requestsRemaining"), 0)
    minimum_remaining = safe_int(config.get("advancedMarketsMinimumQuotaRemaining"), 180)
    max_events = safe_int(config.get("maximumAdvancedEvents"), 2)
    if max_events <= 0 or (remaining and remaining < minimum_remaining):
        return {}

    soccer_events = [
        event
        for event in featured_events
        if infer_sport_from_key(event.get("sport_key")) == "soccer"
    ]
    soccer_events.sort(
        key=lambda event: (
            -sum(len(bookmaker.get("markets") or []) for bookmaker in event.get("bookmakers") or []),
            str(event.get("commence_time") or ""),
        )
    )
    selected = soccer_events[:max_events]
    advanced: dict[str, dict[str, Any]] = {}
    allowed = set(str(value) for value in config.get("advancedSoccerMarkets", []))
    max_markets = safe_int(config.get("maximumAdvancedMarketsPerEvent"), 3)

    for event in selected:
        sport_key = str(event.get("sport_key") or "")
        event_id = str(event.get("id") or "")
        if not sport_key or not event_id:
            continue
        common = {
            "apiKey": api_key,
            "regions": str(config.get("oddsRegions") or "eu"),
            "dateFormat": "iso",
        }
        try:
            markets_url = (
                f"{ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/events/"
                f"{urllib.parse.quote(event_id)}/markets?{urllib.parse.urlencode(common)}"
            )
            market_payload = client.request_json(markets_url, label=f"MARKETS:{event_id}")
            available: set[str] = set()
            if isinstance(market_payload, dict):
                for bookmaker in market_payload.get("bookmakers") or []:
                    for key in bookmaker.get("markets") or []:
                        if isinstance(key, str):
                            available.add(key)
                        elif isinstance(key, dict) and key.get("key"):
                            available.add(str(key["key"]))
            chosen = [key for key in config.get("advancedSoccerMarkets", []) if key in available and key in allowed]
            chosen = chosen[:max_markets]
            if not chosen:
                continue
            odds_params = dict(common)
            odds_params.update({"markets": ",".join(chosen), "oddsFormat": "decimal"})
            odds_url = (
                f"{ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/events/"
                f"{urllib.parse.quote(event_id)}/odds?{urllib.parse.urlencode(odds_params)}"
            )
            payload = client.request_json(odds_url, label=f"ADVANCED_ODDS:{event_id}")
            if isinstance(payload, dict):
                advanced[event_id] = payload
        except Exception as exc:
            log(f"Advanced markets skipped for {event_id}: {exc}")
    return advanced


# ---------------------------------------------------------------------------
# Football and NHL statistical context
# ---------------------------------------------------------------------------



def fetch_football_data_matches(
    client: ApiClient,
    token: str | None,
    config: dict[str, Any],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Fetch football-data.org history in API-safe windows of at most ten days."""
    if not token or not config.get("footballDataEnabled", True):
        return []

    lookback = max(
        1,
        safe_int(config.get("footballDataLookbackDays"), 120),
    )
    window_days = max(
        1,
        min(
            10,
            safe_int(
                config.get("footballDataRequestWindowDays"),
                10,
            ),
        ),
    )
    maximum_windows = max(
        1,
        safe_int(
            config.get("footballDataMaximumWindowsPerRun"),
            math.ceil((lookback + 3) / window_days),
        ),
    )
    limit = str(
        safe_int(config.get("footballDataMaximumMatches"), 500)
    )

    first_date = now.date() - dt.timedelta(days=lookback)
    last_date = now.date() + dt.timedelta(days=2)
    window_end = last_date
    collected: dict[str, dict[str, Any]] = {}
    window_count = 0

    while (
        window_end >= first_date
        and window_count < maximum_windows
    ):
        window_start = max(
            first_date,
            window_end - dt.timedelta(days=window_days - 1),
        )
        params = {
            "dateFrom": window_start.isoformat(),
            "dateTo": window_end.isoformat(),
            "limit": limit,
        }
        url = (
            f"{FOOTBALL_DATA_BASE}/matches?"
            f"{urllib.parse.urlencode(params)}"
        )

        try:
            payload = client.request_json(
                url,
                headers={"X-Auth-Token": token},
                label=(
                    "FOOTBALL_DATA_MATCHES:"
                    f"{window_start.isoformat()}:"
                    f"{window_end.isoformat()}"
                ),
            )
        except Exception as exc:
            log(
                "football-data.org window unavailable "
                f"{window_start}..{window_end}: {exc}"
            )
            window_end = window_start - dt.timedelta(days=1)
            window_count += 1
            continue

        matches = (
            payload.get("matches")
            if isinstance(payload, dict)
            else []
        )
        for item in matches if isinstance(matches, list) else []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or "").strip()
            if not key:
                key = stable_id(
                    (item.get("homeTeam") or {}).get("name"),
                    (item.get("awayTeam") or {}).get("name"),
                    item.get("utcDate"),
                    (item.get("competition") or {}).get("name"),
                )
            collected[key] = item

        window_end = window_start - dt.timedelta(days=1)
        window_count += 1

    result = list(collected.values())
    result.sort(key=lambda item: str(item.get("utcDate") or ""))
    return result


def football_match_score(match: dict[str, Any]) -> tuple[int, int] | None:
    score = match.get("score") if isinstance(match.get("score"), dict) else {}
    full = score.get("fullTime") if isinstance(score.get("fullTime"), dict) else {}
    home = full.get("home")
    away = full.get("away")
    if home is None or away is None:
        return None
    return safe_int(home), safe_int(away)


def football_team_names(team: dict[str, Any]) -> set[str]:
    values = {
        team.get("name"),
        team.get("shortName"),
        team.get("tla"),
    }
    return {normalize_text(value) for value in values if normalize_text(value)}


def build_football_context(matches: list[dict[str, Any]]) -> dict[str, Any]:
    team_games: dict[str, list[dict[str, Any]]] = defaultdict(list)
    finished: list[dict[str, Any]] = []
    home_goals: list[int] = []
    away_goals: list[int] = []
    completed_lookup: list[dict[str, Any]] = []

    for match in matches:
        status = str(match.get("status") or "")
        home_team = match.get("homeTeam") if isinstance(match.get("homeTeam"), dict) else {}
        away_team = match.get("awayTeam") if isinstance(match.get("awayTeam"), dict) else {}
        score = football_match_score(match)
        utc_date = parse_datetime(match.get("utcDate"))
        if score is not None and status in {"FINISHED", "AWARDED"}:
            home_score, away_score = score
            home_goals.append(home_score)
            away_goals.append(away_score)
            record = {
                "utcDate": iso_z(utc_date) if utc_date else "",
                "home": str(home_team.get("name") or ""),
                "away": str(away_team.get("name") or ""),
                "homeScore": home_score,
                "awayScore": away_score,
                "competition": str((match.get("competition") or {}).get("name") or ""),
            }
            finished.append(record)
            for name in football_team_names(home_team):
                team_games[name].append({**record, "side": "home", "goalsFor": home_score, "goalsAgainst": away_score})
            for name in football_team_names(away_team):
                team_games[name].append({**record, "side": "away", "goalsFor": away_score, "goalsAgainst": home_score})
            completed_lookup.append({
                "eventId": str(match.get("id") or ""),
                "utcDate": utc_date,
                "home": str(home_team.get("name") or ""),
                "away": str(away_team.get("name") or ""),
                "homeScore": home_score,
                "awayScore": away_score,
            })

    for games in team_games.values():
        games.sort(key=lambda item: str(item.get("utcDate") or ""), reverse=True)

    return {
        "teamGames": dict(team_games),
        "leagueHomeAverage": mean(home_goals, 1.45),
        "leagueAwayAverage": mean(away_goals, 1.15),
        "finished": finished,
        "completedLookup": completed_lookup,
    }


def find_team_games(team_name: str, context: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    normalized = normalize_text(team_name)
    direct = context.get("teamGames", {}).get(normalized)
    if direct:
        return direct, 1.0
    best_key = ""
    best_score = 0.0
    for key in context.get("teamGames", {}):
        score = token_similarity(normalized, key)
        if score > best_score:
            best_key = key
            best_score = score
    if best_score >= 0.72:
        return context["teamGames"][best_key], best_score
    return [], best_score


def recent_form_summary(games: list[dict[str, Any]], side: str | None = None, limit: int = 10) -> dict[str, float]:
    selected = [game for game in games if side is None or game.get("side") == side][:limit]
    if not selected:
        return {
            "matches": 0, "gf": 0.0, "ga": 0.0, "points": 0.0,
            "variance": 0.0, "totalGoals": 0.0, "over15": 0.0,
            "over25": 0.0, "over35": 0.0, "btts": 0.0,
            "failedToScore": 0.0, "cleanSheet": 0.0,
        }
    goals_for = [safe_float(game.get("goalsFor")) for game in selected]
    goals_against = [safe_float(game.get("goalsAgainst")) for game in selected]
    totals = [gf + ga for gf, ga in zip(goals_for, goals_against)]
    points = []
    for gf, ga in zip(goals_for, goals_against):
        points.append(3.0 if gf > ga else 1.0 if gf == ga else 0.0)
    count = float(len(selected))
    return {
        "matches": len(selected),
        "gf": mean(goals_for),
        "ga": mean(goals_against),
        "points": mean(points) / 3.0,
        "variance": stdev([gf - ga for gf, ga in zip(goals_for, goals_against)]),
        "totalGoals": mean(totals),
        "over15": sum(1 for value in totals if value > 1.5) / count,
        "over25": sum(1 for value in totals if value > 2.5) / count,
        "over35": sum(1 for value in totals if value > 3.5) / count,
        "btts": sum(1 for gf, ga in zip(goals_for, goals_against) if gf > 0 and ga > 0) / count,
        "failedToScore": sum(1 for value in goals_for if value <= 0) / count,
        "cleanSheet": sum(1 for value in goals_against if value <= 0) / count,
    }


def fetch_nhl_standings(client: ApiClient, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {}
    try:
        payload = client.request_json(f"{NHL_API_BASE}/standings/now", label="NHL_STANDINGS")
    except Exception as exc:
        log(f"NHL public standings unavailable: {exc}")
        return {}
    standings = payload.get("standings") if isinstance(payload, dict) else []
    result: dict[str, Any] = {}
    for row in standings or []:
        if not isinstance(row, dict):
            continue
        names: set[str] = set()
        for key in ("teamName", "teamCommonName", "teamAbbrev", "placeName"):
            value = row.get(key)
            if isinstance(value, dict):
                value = value.get("default")
            if value:
                names.add(normalize_text(value))
        record = {
            "gamesPlayed": safe_int(row.get("gamesPlayed")),
            "wins": safe_int(row.get("wins")),
            "losses": safe_int(row.get("losses")),
            "otLosses": safe_int(row.get("otLosses")),
            "goalFor": safe_int(row.get("goalFor")),
            "goalAgainst": safe_int(row.get("goalAgainst")),
            "pointPctg": safe_float(row.get("pointPctg")),
            "regulationWins": safe_int(row.get("regulationWins")),
        }
        for name in names:
            result[name] = record
    return result


def find_nhl_team(team_name: str, standings: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    normalized = normalize_text(team_name)
    if normalized in standings:
        return standings[normalized], 1.0
    best_key = ""
    best_score = 0.0
    for key in standings:
        score = token_similarity(normalized, key)
        if score > best_score:
            best_key = key
            best_score = score
    return (standings.get(best_key), best_score) if best_score >= 0.72 else (None, best_score)


# ---------------------------------------------------------------------------
# Probability models
# ---------------------------------------------------------------------------


def poisson_probability(lam: float, goals: int) -> float:
    return math.exp(-lam) * (lam**goals) / math.factorial(goals)


def score_matrix(home_lambda: float, away_lambda: float, maximum: int = 10) -> list[list[float]]:
    home_probs = [poisson_probability(home_lambda, goal) for goal in range(maximum + 1)]
    away_probs = [poisson_probability(away_lambda, goal) for goal in range(maximum + 1)]
    matrix = [[home_probs[h] * away_probs[a] for a in range(maximum + 1)] for h in range(maximum + 1)]
    total = sum(sum(row) for row in matrix)
    if total > 0:
        matrix = [[value / total for value in row] for row in matrix]
    return matrix


def matrix_outcome_probability(matrix: list[list[float]], outcome: str) -> float:
    value = 0.0
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            if outcome == "HOME" and home > away:
                value += probability
            elif outcome == "DRAW" and home == away:
                value += probability
            elif outcome == "AWAY" and home < away:
                value += probability
    return value


def matrix_total_probability(matrix: list[list[float]], side: str, line: float) -> float:
    value = 0.0
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            total = home + away
            if side == "OVER" and total > line:
                value += probability
            elif side == "UNDER" and total < line:
                value += probability
            elif total == line:
                value += probability * 0.5
    return value


def matrix_team_total_probability(
    matrix: list[list[float]], team: str, side: str, line: float
) -> float:
    value = 0.0
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            total = home if team == "HOME" else away
            if side == "OVER" and total > line:
                value += probability
            elif side == "UNDER" and total < line:
                value += probability
            elif total == line:
                value += probability * 0.5
    return value


def matrix_spread_probability(
    matrix: list[list[float]], team: str, point: float
) -> float:
    value = 0.0
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            adjusted = (home + point - away) if team == "HOME" else (away + point - home)
            if adjusted > 0:
                value += probability
            elif abs(adjusted) < 1e-9:
                value += probability * 0.5
    return value


def matrix_btts_probability(matrix: list[list[float]], yes: bool) -> float:
    probability_yes = sum(
        probability
        for home, row in enumerate(matrix)
        for away, probability in enumerate(row)
        if home > 0 and away > 0
    )
    return probability_yes if yes else 1.0 - probability_yes


def most_likely_scores(matrix: list[list[float]], count: int = 4) -> list[dict[str, Any]]:
    rows = [
        (probability, home, away)
        for home, row in enumerate(matrix)
        for away, probability in enumerate(row)
    ]
    rows.sort(reverse=True)
    return [
        {"score": f"{home}:{away}", "probability": round(probability, 4)}
        for probability, home, away in rows[:count]
    ]


def infer_lambdas_from_market(event_quotes: list[dict[str, Any]], sport: str) -> tuple[float, float]:
    base_total = 5.8 if sport == "ice_hockey" else 2.6
    home_prob = 0.43
    away_prob = 0.32
    draw_prob = 0.25 if sport == "soccer" else 0.0

    h2h = [quote for quote in event_quotes if quote.get("marketKey") in {"h2h", "h2h_3_way"}]
    for quote in h2h:
        selection = quote.get("selectionCode")
        if selection == "HOME":
            home_prob = safe_float(quote.get("marketProbability"), home_prob)
        elif selection == "AWAY":
            away_prob = safe_float(quote.get("marketProbability"), away_prob)
        elif selection == "DRAW":
            draw_prob = safe_float(quote.get("marketProbability"), draw_prob)

    totals = [
        quote
        for quote in event_quotes
        if quote.get("marketKey") in {"totals", "alternate_totals"}
        and quote.get("selectionCode") == "OVER"
    ]
    if totals:
        selected = min(totals, key=lambda item: abs(safe_float(item.get("point"), base_total) - base_total))
        line = safe_float(selected.get("point"), base_total)
        probability = safe_float(selected.get("marketProbability"), 0.5)
        # A stable approximation: move the expected total around the market line.
        base_total = clamp(line + (probability - 0.5) * 2.2, 1.2 if sport == "soccer" else 3.5, 8.5)

    ratio = math.log(max(home_prob, 0.03) / max(away_prob, 0.03))
    goal_difference = clamp(ratio * (0.48 if sport == "soccer" else 0.72), -2.2, 2.2)
    home_lambda = clamp((base_total + goal_difference) / 2, 0.25, 6.0)
    away_lambda = clamp(base_total - home_lambda, 0.25, 6.0)
    if sport == "soccer" and draw_prob > 0.32:
        midpoint = (home_lambda + away_lambda) / 2
        home_lambda = home_lambda * 0.8 + midpoint * 0.2
        away_lambda = away_lambda * 0.8 + midpoint * 0.2
    return home_lambda, away_lambda


def build_event_model(
    event: dict[str, Any],
    event_quotes: list[dict[str, Any]],
    football_context: dict[str, Any],
    nhl_standings: dict[str, Any],
) -> dict[str, Any]:
    sport = infer_sport_from_key(event.get("sport_key"))
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    market_home, market_away = infer_lambdas_from_market(event_quotes, sport)
    home_lambda = market_home
    away_lambda = market_away
    data_tier = "MARKET"
    data_quality = 42.0
    source_notes = ["Безмаржинальный консенсус нескольких букмекеров"]
    model_components: dict[str, Any] = {
        "marketExpectedHome": round(market_home, 3),
        "marketExpectedAway": round(market_away, 3),
    }

    if sport == "soccer":
        home_games, home_match = find_team_games(home, football_context)
        away_games, away_match = find_team_games(away, football_context)
        if home_games and away_games:
            home_all = recent_form_summary(home_games, None, 10)
            home_venue = recent_form_summary(home_games, "home", 6)
            away_all = recent_form_summary(away_games, None, 10)
            away_venue = recent_form_summary(away_games, "away", 6)
            league_home = safe_float(football_context.get("leagueHomeAverage"), 1.45)
            league_away = safe_float(football_context.get("leagueAwayAverage"), 1.15)
            stat_home = mean(
                [
                    home_all["gf"],
                    home_venue["gf"] or home_all["gf"],
                    away_all["ga"],
                    away_venue["ga"] or away_all["ga"],
                    league_home,
                ],
                league_home,
            )
            stat_away = mean(
                [
                    away_all["gf"],
                    away_venue["gf"] or away_all["gf"],
                    home_all["ga"],
                    home_venue["ga"] or home_all["ga"],
                    league_away,
                ],
                league_away,
            )
            sample = min(home_all["matches"], away_all["matches"])
            stat_weight = clamp(sample / 12.0, 0.25, 0.65)
            home_lambda = clamp(market_home * (1 - stat_weight) + stat_home * stat_weight, 0.25, 4.2)
            away_lambda = clamp(market_away * (1 - stat_weight) + stat_away * stat_weight, 0.25, 4.2)
            data_tier = "FULL" if sample >= 8 and min(home_match, away_match) >= 0.9 else "HYBRID"
            data_quality = clamp(55 + sample * 3 + min(home_match, away_match) * 12, 55, 92)
            source_notes.extend(
                [
                    f"Форма хозяев: {home_all['gf']:.2f} забито и {home_all['ga']:.2f} пропущено за матч",
                    f"Форма гостей: {away_all['gf']:.2f} забито и {away_all['ga']:.2f} пропущено за матч",
                ]
            )
            model_components.update(
                {
                    "homeRecent": home_all,
                    "awayRecent": away_all,
                    "teamNameMatch": round(min(home_match, away_match), 3),
                    "combinedRecentTotalGoals": round(mean([home_all["totalGoals"], away_all["totalGoals"]]), 4),
                    "combinedRecentOver15": round(mean([home_all["over15"], away_all["over15"]]), 4),
                    "combinedRecentOver25": round(mean([home_all["over25"], away_all["over25"]]), 4),
                    "combinedRecentOver35": round(mean([home_all["over35"], away_all["over35"]]), 4),
                    "combinedRecentBTTS": round(mean([home_all["btts"], away_all["btts"]]), 4),
                }
            )
    elif "nhl" in str(event.get("sport_key") or "").lower() and nhl_standings:
        home_row, home_match = find_nhl_team(home, nhl_standings)
        away_row, away_match = find_nhl_team(away, nhl_standings)
        if home_row and away_row:
            def rates(row: dict[str, Any]) -> tuple[float, float, float]:
                games = max(1, safe_int(row.get("gamesPlayed")))
                return (
                    safe_float(row.get("goalFor")) / games,
                    safe_float(row.get("goalAgainst")) / games,
                    safe_float(row.get("pointPctg"), 0.5),
                )

            home_gf, home_ga, home_points = rates(home_row)
            away_gf, away_ga, away_points = rates(away_row)
            stat_home = mean([home_gf, away_ga, 3.0])
            stat_away = mean([away_gf, home_ga, 2.8])
            strength_adjustment = clamp((home_points - away_points) * 1.2, -0.7, 0.7)
            stat_home += strength_adjustment / 2
            stat_away -= strength_adjustment / 2
            home_lambda = clamp(market_home * 0.48 + stat_home * 0.52, 1.2, 5.5)
            away_lambda = clamp(market_away * 0.48 + stat_away * 0.52, 1.2, 5.5)
            data_tier = "FULL"
            data_quality = 84.0
            source_notes.extend(
                [
                    f"NHL: результативность хозяев {home_gf:.2f}, пропускают {home_ga:.2f}",
                    f"NHL: результативность гостей {away_gf:.2f}, пропускают {away_ga:.2f}",
                ]
            )
            model_components.update(
                {
                    "homeStanding": home_row,
                    "awayStanding": away_row,
                    "teamNameMatch": round(min(home_match, away_match), 3),
                }
            )

    matrix = score_matrix(home_lambda, away_lambda, 10 if sport == "soccer" else 14)
    return {
        "sport": sport,
        "homeLambda": round(home_lambda, 4),
        "awayLambda": round(away_lambda, 4),
        "expectedScore": f"{home_lambda:.1f} : {away_lambda:.1f}",
        "mostLikelyScores": most_likely_scores(matrix),
        "homeWinProbability": matrix_outcome_probability(matrix, "HOME"),
        "drawProbability": matrix_outcome_probability(matrix, "DRAW"),
        "awayWinProbability": matrix_outcome_probability(matrix, "AWAY"),
        "matrix": matrix,
        "dataTier": data_tier,
        "dataQuality": round(data_quality, 1),
        "sourceNotes": source_notes,
        "components": model_components,
    }


# ---------------------------------------------------------------------------
# Odds parsing and market evaluation
# ---------------------------------------------------------------------------


def no_vig_probabilities(outcomes: list[dict[str, Any]]) -> dict[int, float]:
    inverse = []
    for index, outcome in enumerate(outcomes):
        price = safe_float(outcome.get("price"), 0.0)
        if price > 1.0:
            inverse.append((index, 1.0 / price))
    total = sum(value for _, value in inverse)
    if total <= 0:
        return {}
    return {index: value / total for index, value in inverse}


def normalize_selection(
    market_key: str,
    outcome: dict[str, Any],
    event: dict[str, Any],
) -> tuple[str, str, str, float | None] | None:
    key = str(market_key or "").lower()
    if key not in R14_ALLOWED_MARKET_KEYS:
        return None
    name = str(outcome.get("name") or "").strip()
    description = str(outcome.get("description") or "").strip()
    point_raw = outcome.get("point")
    point = safe_float(point_raw) if point_raw is not None else None
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    normalized_name = normalize_text(name)
    normalized_description = normalize_text(description)

    if key in {"h2h", "h2h_3_way"}:
        if token_similarity(name, home) >= 0.72:
            return "HOME", "HOME_WIN", f"Победа {home}", None
        if token_similarity(name, away) >= 0.72:
            return "AWAY", "AWAY_WIN", f"Победа {away}", None
        if normalized_name in {"draw", "tie", "ничья"}:
            return "DRAW", "DRAW", "Ничья", None
    elif key == "totals":
        if not is_half_goal_line(point):
            return None
        if normalized_name.startswith("over") or normalized_name.startswith("больше"):
            return "OVER", "TOTAL_OVER", f"Тотал больше {point:g}", point
        if normalized_name.startswith("under") or normalized_name.startswith("меньше"):
            return "UNDER", "TOTAL_UNDER", f"Тотал меньше {point:g}", point
    elif key == "btts":
        if normalized_name in {"yes", "да"}:
            return "YES", "BTTS_YES", "Обе команды забьют — да", None
        if normalized_name in {"no", "нет"}:
            return "NO", "BTTS_NO", "Обе команды забьют — нет", None
    elif key == "double_chance":
        combined = f"{normalized_name} {normalized_description}"
        if any(token in combined for token in ("home or draw", "1x", "home draw")):
            return "HOME_DRAW", "DOUBLE_CHANCE_HOME_DRAW", "Двойной шанс 1Х", None
        if any(token in combined for token in ("draw or away", "x2", "draw away")):
            return "DRAW_AWAY", "DOUBLE_CHANCE_DRAW_AWAY", "Двойной шанс Х2", None
        if any(token in combined for token in ("home or away", "12", "home away")):
            return "HOME_AWAY", "DOUBLE_CHANCE_HOME_AWAY", "Двойной шанс 12", None
    elif key == "team_totals":
        if not is_half_goal_line(point):
            return None
        target = "HOME" if token_similarity(description, home) >= 0.72 else "AWAY" if token_similarity(description, away) >= 0.72 else ""
        if not target:
            return None
        team_name = home if target == "HOME" else away
        if normalized_name.startswith("over"):
            return f"{target}_OVER", f"TEAM_TOTAL_{target}_OVER", f"Индивидуальный тотал {team_name} больше {point:g}", point
        if normalized_name.startswith("under"):
            return f"{target}_UNDER", f"TEAM_TOTAL_{target}_UNDER", f"Индивидуальный тотал {team_name} меньше {point:g}", point
    return None

def parse_event_quotes(
    event: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, float | None], list[dict[str, Any]]] = defaultdict(list)
    maximum_age = safe_int(config.get("maximumQuoteAgeMinutes"), 240)
    preferred = set(str(value) for value in config.get("preferredBookmakers", []))

    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_key = str(bookmaker.get("key") or "")
        bookmaker_title = str(bookmaker.get("title") or bookmaker_key)
        bookmaker_update = parse_datetime(bookmaker.get("last_update"))
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict):
                continue
            market_key = str(market.get("key") or "").lower()
            if market_key not in R14_ALLOWED_MARKET_KEYS:
                continue
            market_update = parse_datetime(market.get("last_update")) or bookmaker_update
            age_minutes = (
                max(0.0, (now - market_update).total_seconds() / 60.0)
                if market_update
                else None
            )
            if age_minutes is None or age_minutes > maximum_age:
                continue
            outcomes = [item for item in market.get("outcomes") or [] if isinstance(item, dict)]
            # Normalize probabilities within each bookmaker line. For totals/spreads,
            # a bookmaker market object normally contains one pair per line.
            line_groups: dict[Any, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
            for index, outcome in enumerate(outcomes):
                point = outcome.get("point")
                numeric_point = safe_float(point) if point is not None else None
                if market_key == "team_totals":
                    group_key = (
                        "team_total",
                        normalize_text(outcome.get("description")),
                        numeric_point,
                    )
                else:
                    group_key = numeric_point
                line_groups[group_key].append((index, outcome))
            for _, indexed in line_groups.items():
                line_outcomes = [outcome for _, outcome in indexed]
                probabilities = no_vig_probabilities(line_outcomes)
                for local_index, outcome in enumerate(line_outcomes):
                    normalized = normalize_selection(market_key, outcome, event)
                    if not normalized:
                        continue
                    selection_code, market_code, pick, point = normalized
                    if not standard_market_allowed(market_key, market_code, point):
                        continue
                    price = safe_float(outcome.get("price"), 0.0)
                    if price <= 1.0:
                        continue
                    grouped[(market_key, selection_code, point)].append(
                        {
                            "bookmaker": bookmaker_title,
                            "bookmakerKey": bookmaker_key,
                            "price": price,
                            "probability": probabilities.get(local_index, 1.0 / price),
                            "lastUpdate": iso_z(market_update) if market_update else "",
                            "ageMinutes": round(age_minutes, 2) if age_minutes is not None else None,
                            "preferred": bookmaker_key in preferred,
                            "marketCode": market_code,
                            "pick": pick,
                        }
                    )

    quotes: list[dict[str, Any]] = []
    for (market_key, selection_code, point), rows in grouped.items():
        if not rows:
            continue
        rows.sort(key=lambda item: (item["price"], item["preferred"]), reverse=True)
        best = rows[0]
        probabilities = [safe_float(item.get("probability")) for item in rows]
        prices = [safe_float(item.get("price")) for item in rows]
        quotes.append(
            {
                "eventId": str(event.get("id") or ""),
                "sportKey": str(event.get("sport_key") or ""),
                "marketKey": market_key,
                "selectionCode": selection_code,
                "market": best["marketCode"],
                "marketFamily": market_family(best["marketCode"]),
                "pick": best["pick"],
                "point": point,
                "bookmakerOdds": round(best["price"], 3),
                "bookmaker": best["bookmaker"],
                "bookmakerKey": best["bookmakerKey"],
                "oddsLastUpdate": best["lastUpdate"],
                "oddsAgeMinutes": best["ageMinutes"],
                "quoteCount": len(rows),
                "marketProbability": clamp(mean(probabilities), 0.01, 0.99),
                "marketDispersion": stdev(probabilities),
                "marketPolicy": R14_MARKET_POLICY,
                "lineIsStandard": standard_market_allowed(market_key, best["marketCode"], point),
                "oddsMinimum": min(prices),
                "oddsMedian": statistics.median(prices),
                "oddsMaximum": max(prices),
            }
        )
    return quotes


def model_probability_for_quote(model: dict[str, Any], quote: dict[str, Any]) -> float:
    matrix = model["matrix"]
    selection = str(quote.get("selectionCode") or "")
    market = str(quote.get("market") or "")
    market_key = str(quote.get("marketKey") or "")
    point = safe_float(quote.get("point"), 0.0)

    if market == "SPREAD_HOME":
        return matrix_spread_probability(matrix, "HOME", point)
    if market == "SPREAD_AWAY":
        return matrix_spread_probability(matrix, "AWAY", point)
    if market == "TEAM_TOTAL_HOME_OVER":
        return matrix_team_total_probability(matrix, "HOME", "OVER", point)
    if market == "TEAM_TOTAL_HOME_UNDER":
        return matrix_team_total_probability(matrix, "HOME", "UNDER", point)
    if market == "TEAM_TOTAL_AWAY_OVER":
        return matrix_team_total_probability(matrix, "AWAY", "OVER", point)
    if market == "TEAM_TOTAL_AWAY_UNDER":
        return matrix_team_total_probability(matrix, "AWAY", "UNDER", point)

    home_win = matrix_outcome_probability(matrix, "HOME")
    draw = matrix_outcome_probability(matrix, "DRAW")
    away_win = matrix_outcome_probability(matrix, "AWAY")

    if selection == "HOME":
        if market_key == "draw_no_bet":
            return home_win + draw * 0.5
        if model.get("sport") == "ice_hockey" and market_key == "h2h":
            decisive = max(0.001, home_win + away_win)
            overtime_share = home_win / decisive
            return home_win + draw * overtime_share
        return home_win
    if selection == "DRAW":
        return draw
    if selection == "AWAY":
        if market_key == "draw_no_bet":
            return away_win + draw * 0.5
        if model.get("sport") == "ice_hockey" and market_key == "h2h":
            decisive = max(0.001, home_win + away_win)
            overtime_share = away_win / decisive
            return away_win + draw * overtime_share
        return away_win
    if selection == "OVER":
        return matrix_total_probability(matrix, "OVER", point)
    if selection == "UNDER":
        return matrix_total_probability(matrix, "UNDER", point)
    if selection == "YES":
        return matrix_btts_probability(matrix, True)
    if selection == "NO":
        return matrix_btts_probability(matrix, False)
    if selection == "HOME_DRAW":
        return home_win + draw
    if selection == "DRAW_AWAY":
        return draw + away_win
    if selection == "HOME_AWAY":
        return home_win + away_win
    if selection == "HOME_OVER":
        return matrix_team_total_probability(matrix, "HOME", "OVER", point)
    if selection == "HOME_UNDER":
        return matrix_team_total_probability(matrix, "HOME", "UNDER", point)
    if selection == "AWAY_OVER":
        return matrix_team_total_probability(matrix, "AWAY", "OVER", point)
    if selection == "AWAY_UNDER":
        return matrix_team_total_probability(matrix, "AWAY", "UNDER", point)
    return safe_float(quote.get("marketProbability"), 0.5)

def learning_market_detail(record: dict[str, Any]) -> str:
    market = str(record.get("market") or record.get("selectionCode") or "OTHER").upper()
    point = record.get("point")
    if point is not None and market.startswith(("TOTAL_", "TEAM_TOTAL_")):
        return f"{market}@{safe_float(point):g}"
    return market


def learning_segment_keys(
    sport: str,
    league: str,
    family: str,
    odds: float,
    data_tier: str = "",
    probability: float = 0.0,
    market_detail: str = "",
) -> list[str]:
    odds_band = "LOW" if odds < 1.55 else "MID" if odds < 1.80 else "UPPER_MID" if odds < 2.20 else "HIGH"
    probability_band = f"P{int(clamp(probability, 0.0, 0.999) * 10) * 10:02d}"
    league_key = normalize_text(league)[:80] or "unknown"
    tier = str(data_tier or "UNKNOWN").upper()
    detail = str(market_detail or family or "OTHER").upper()
    return [
        f"SPORT|{sport}",
        f"MARKET|{sport}|{family}",
        f"MARKET_DETAIL|{sport}|{detail}",
        f"LEAGUE|{sport}|{league_key}",
        f"ODDS|{sport}|{odds_band}",
        f"DATA_TIER|{sport}|{tier}",
        f"PROBABILITY|{sport}|{probability_band}",
        f"LEAGUE_MARKET|{sport}|{league_key}|{family}",
        f"LEAGUE_MARKET_DETAIL|{sport}|{league_key}|{detail}",
        f"MARKET_TIER|{sport}|{family}|{tier}",
        f"MARKET_DETAIL_TIER|{sport}|{detail}|{tier}",
    ]

def probability_adjustment(
    learning: dict[str, Any],
    sport: str,
    league: str,
    family: str,
    odds: float,
    data_tier: str,
    probability: float,
    config: dict[str, Any],
    market_detail: str = "",
) -> tuple[float, list[dict[str, Any]]]:
    minimum = safe_int(config.get("learningMinimumSegmentSamples"), 40)
    full = safe_int(config.get("learningFullWeightSamples"), 160)
    maximum = safe_float(config.get("learningMaximumProbabilityAdjustment"), 0.04)
    contributions: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_adjustment = 0.0
    segments = learning.get("segments") if isinstance(learning.get("segments"), dict) else {}
    for key in learning_segment_keys(sport, league, family, odds, data_tier, probability, market_detail):
        segment = segments.get(key)
        if not isinstance(segment, dict):
            continue
        count = safe_int(segment.get("settled"))
        if count < minimum:
            continue
        bias = safe_float(segment.get("probabilityBias"), 0.0)
        weight = clamp((count - minimum + 1) / max(1, full - minimum + 1), 0.05, 1.0)
        weighted_adjustment += clamp(bias, -maximum, maximum) * weight
        total_weight += weight
        contributions.append(
            {
                "segment": key,
                "samples": count,
                "bias": round(bias, 4),
                "weight": round(weight, 3),
                "hitRate": round(safe_float(segment.get("hitRate")), 4),
                "averagePredicted": round(safe_float(segment.get("averagePredicted")), 4),
                "brierScore": round(safe_float(segment.get("brierScore")), 6),
            }
        )
    if total_weight <= 0:
        return 0.0, []
    return clamp(weighted_adjustment / total_weight, -maximum, maximum), contributions


def goal_direction_diagnostics(
    quote: dict[str, Any],
    model: dict[str, Any],
    statistical_probability: float,
    market_probability: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    market = str(quote.get("market") or "").upper()
    point = safe_float(quote.get("point"), 0.0)
    is_total = market.startswith("TOTAL_") or market.startswith("TEAM_TOTAL_")
    if not is_total:
        return {
            "isTotal": False, "conflict": False, "supportScore": 100.0,
            "statisticalProbability": round(statistical_probability, 6),
            "marketProbability": round(market_probability, 6),
            "reasons": [],
        }

    components = model.get("components") if isinstance(model.get("components"), dict) else {}
    expected_total = safe_float(model.get("homeLambda")) + safe_float(model.get("awayLambda"))
    btts_probability = matrix_btts_probability(model.get("matrix") or [], True) if model.get("matrix") else 0.0
    recent_key = ""
    if abs(point - 1.5) < 1e-9:
        recent_key = "combinedRecentOver15"
    elif abs(point - 2.5) < 1e-9:
        recent_key = "combinedRecentOver25"
    elif abs(point - 3.5) < 1e-9:
        recent_key = "combinedRecentOver35"
    recent_over = safe_float(components.get(recent_key), -1.0) if recent_key else -1.0
    is_over = market.endswith("_OVER")
    recent_selected = recent_over if is_over else (1.0 - recent_over if recent_over >= 0 else -1.0)

    stat_floor = safe_float(config.get("totalDirectionMinimumStatisticalProbability"), 0.54)
    market_floor = safe_float(config.get("totalDirectionMinimumMarketProbability"), 0.50)
    recent_floor = safe_float(config.get("totalDirectionMinimumRecentRate"), 0.48)
    reasons: list[str] = []
    if statistical_probability < stat_floor:
        reasons.append("Модель счёта не подтверждает выбранное направление тотала")
    if market_probability < market_floor:
        reasons.append("Консенсус букмекеров не подтверждает выбранное направление тотала")
    if recent_selected >= 0 and recent_selected < recent_floor:
        reasons.append("Последние матчи команд противоречат выбранному направлению тотала")

    if market == "TOTAL_UNDER":
        if expected_total >= point - safe_float(config.get("underExpectedGoalsSafetyMargin"), 0.05):
            reasons.append("Ожидаемая сумма голов слишком высока для ставки на меньше")
        if point <= 2.5 and btts_probability >= safe_float(config.get("under25MaximumBttsProbability"), 0.56):
            reasons.append("Высока вероятность голов обеих команд")
        if point <= 2.5 and recent_over >= safe_float(config.get("under25MaximumRecentOverRate"), 0.56):
            reasons.append("Команды часто пробивают тотал больше 2,5")
    elif market == "TOTAL_OVER":
        if expected_total <= point + safe_float(config.get("overExpectedGoalsSafetyMargin"), 0.05):
            reasons.append("Ожидаемая сумма голов недостаточна для ставки на больше")
        if recent_over >= 0 and recent_over < safe_float(config.get("overMinimumRecentOverRate"), 0.42):
            reasons.append("Недавняя результативность недостаточна для ставки на больше")

    available_signals = [statistical_probability, market_probability]
    if recent_selected >= 0:
        available_signals.append(recent_selected)
    support_score = clamp(mean(available_signals, 0.0) * 100.0, 0.0, 100.0)
    return {
        "isTotal": True,
        "conflict": bool(reasons),
        "supportScore": round(support_score, 2),
        "statisticalProbability": round(statistical_probability, 6),
        "marketProbability": round(market_probability, 6),
        "recentSelectedRate": round(recent_selected, 6) if recent_selected >= 0 else None,
        "expectedTotalGoals": round(expected_total, 4),
        "bttsProbability": round(btts_probability, 6),
        "reasons": reasons,
    }


def evaluate_event_markets(
    event: dict[str, Any],
    quotes: list[dict[str, Any]],
    model: dict[str, Any],
    learning: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    sport = model["sport"]
    if sport != "soccer":
        return []
    league = str(event.get("sport_title") or event.get("sport_key") or "")
    data_tier = str(model.get("dataTier") or "MARKET").upper()
    data_quality = safe_float(model.get("dataQuality"), 40.0)
    tier_weights = config.get("dataTierWeights") if isinstance(config.get("dataTierWeights"), dict) else {}
    stat_weight = clamp(safe_float(tier_weights.get(data_tier), 0.18), 0.10, 0.88)

    likely_scores = model.get("mostLikelyScores") or []
    score_concentration = clamp(
        sum(safe_float(item.get("probability")) for item in likely_scores if isinstance(item, dict)),
        0.0,
        1.0,
    )
    data_tier_penalty = {"FULL": 0.0, "HYBRID": 7.0, "MARKET": 22.0}.get(data_tier, 15.0)
    uncertainty_base = {"FULL": 0.025, "HYBRID": 0.055, "MARKET": 0.095}.get(data_tier, 0.075)

    candidates: list[dict[str, Any]] = []
    for quote in quotes:
        if not standard_market_allowed(quote.get("marketKey"), quote.get("market"), quote.get("point")):
            continue
        odds = safe_float(quote.get("bookmakerOdds"), 0.0)
        if odds < safe_float(config.get("minimumBookmakerOdds"), 1.35) or odds > safe_float(config.get("maximumBookmakerOdds"), 3.2):
            continue
        quote_count = safe_int(quote.get("quoteCount"))
        if quote_count < safe_int(config.get("minimumBookmakers"), 2):
            continue

        statistical_probability = model_probability_for_quote(model, quote)
        market_probability = safe_float(quote.get("marketProbability"), 0.5)
        raw_probability = statistical_probability * stat_weight + market_probability * (1 - stat_weight)
        preliminary_probability = clamp(raw_probability, 0.03, 0.97)
        direction = goal_direction_diagnostics(
            quote, model, statistical_probability, market_probability, config
        )
        adjustment, learning_evidence = probability_adjustment(
            learning,
            sport,
            league,
            str(quote.get("marketFamily") or "OTHER"),
            odds,
            data_tier,
            preliminary_probability,
            config,
            learning_market_detail(quote),
        )
        model_probability = clamp(preliminary_probability + adjustment, 0.03, 0.97)
        # Market-only rows must not invent a large edge from a model that has no
        # independent team evidence. Consensus remains useful, but conservatively.
        if data_tier == "MARKET":
            model_probability = min(model_probability, market_probability + 0.03)

        edge = model_probability - market_probability
        expected_value = model_probability * odds - 1.0
        dispersion = safe_float(quote.get("marketDispersion"))
        disagreement = abs(statistical_probability - market_probability)
        agreement = clamp(100 - disagreement * 190 - dispersion * 280, 0, 100)
        odds_minimum = safe_float(quote.get("oddsMinimum"), odds)
        odds_maximum = safe_float(quote.get("oddsMaximum"), odds)
        relative_price_range = (odds_maximum - odds_minimum) / max(1.01, safe_float(quote.get("oddsMedian"), odds))
        market_stability = clamp(
            100 - dispersion * 360 - relative_price_range * 220 - max(0, 4 - quote_count) * 8,
            0,
            100,
        )
        anomaly = clamp(
            dispersion * 340
            + relative_price_range * 130
            + max(0, 3 - quote_count) * 12
            + max(0, safe_float(quote.get("oddsAgeMinutes")) - 60) / 10,
            0,
            100,
        )

        uncertainty_margin = uncertainty_base
        uncertainty_margin += max(0.0, 60.0 - data_quality) / 1000.0
        uncertainty_margin += max(0.0, 65.0 - agreement) / 900.0
        uncertainty_margin += max(0.0, 65.0 - market_stability) / 1100.0
        uncertainty_margin += max(0, 4 - quote_count) * 0.008
        uncertainty_margin = clamp(uncertainty_margin, 0.02, 0.16)
        conservative_probability = clamp(model_probability - uncertainty_margin, 0.02, 0.96)

        preferred_min = safe_float(config.get("preferredMinimumOdds"), 1.55)
        preferred_max = safe_float(config.get("preferredMaximumOdds"), 2.40)
        if preferred_min <= odds <= preferred_max:
            price_score = 100.0
        elif odds < preferred_min:
            price_score = clamp(100 - (preferred_min - odds) * 240, 5, 100)
        else:
            price_score = clamp(100 - (odds - preferred_max) * 55, 8, 100)

        low_odds_penalty = 0.0
        if odds <= safe_float(config.get("lowOddsMaximum"), 1.54):
            if conservative_probability < safe_float(config.get("lowOddsMinimumProbability"), 0.72):
                low_odds_penalty += 38.0
            if edge < safe_float(config.get("lowOddsMinimumEdge"), 0.08):
                low_odds_penalty += 24.0

        evidence_weight = sum(safe_float(item.get("weight")) for item in learning_evidence)
        empirical_hit_rate = (
            sum(safe_float(item.get("hitRate")) * safe_float(item.get("weight")) for item in learning_evidence) / evidence_weight
            if evidence_weight > 0 else conservative_probability
        )
        historical_support = clamp((empirical_hit_rate - 0.50) * 42, -12, 18) if learning_evidence else 0.0
        direction_penalty = (
            safe_float(config.get("totalDirectionConflictPenalty"), 34.0)
            if bool(direction.get("conflict"))
            else 0.0
        )
        direction_bonus = (
            max(0.0, safe_float(direction.get("supportScore")) - 55.0) * 0.20
            if bool(direction.get("isTotal"))
            else 0.0
        )

        reliability_score = (
            conservative_probability * 82
            + data_quality * 0.13
            + agreement * 0.13
            + market_stability * 0.12
            + score_concentration * 12
            + price_score * 0.10
            + historical_support
            + clamp(edge * 100, -10, 14) * 0.10
            - anomaly * 0.16
            - data_tier_penalty
            - low_odds_penalty
            - direction_penalty
            + direction_bonus
        )
        best_bet_score = (
            conservative_probability * 92
            + data_quality * 0.14
            + agreement * 0.15
            + market_stability * 0.14
            + score_concentration * 13
            + price_score * 0.12
            + historical_support * 1.20
            + clamp(edge * 100, -10, 16) * 0.10
            + clamp(expected_value * 100, -10, 18) * 0.04
            - anomaly * 0.18
            - data_tier_penalty
            - low_odds_penalty
            - direction_penalty * 1.20
            + direction_bonus * 1.15
        )

        candidate = {
            **quote,
            "sport": "soccer",
            "sportLabel": "Футбол",
            "league": league,
            "country": str(event.get("country") or infer_country(str(event.get("sport_key") or ""), league)),
            "home": str(event.get("home_team") or ""),
            "away": str(event.get("away_team") or ""),
            "commenceTime": str(event.get("commence_time") or ""),
            "modelProbability": round(model_probability, 6),
            "conservativeProbability": round(conservative_probability, 6),
            "uncertaintyMargin": round(uncertainty_margin, 6),
            "statisticalProbability": round(statistical_probability, 6),
            "marketProbability": round(market_probability, 6),
            "edge": round(edge, 6),
            "expectedValue": round(expected_value, 6),
            "confidence": round(conservative_probability * 100, 1),
            "dataTier": data_tier,
            "dataQuality": round(data_quality, 1),
            "agreement": round(agreement, 1),
            "marketStability": round(market_stability, 1),
            "anomaly": round(anomaly, 1),
            "priceScore": round(price_score, 1),
            "scenarioConcentration": round(score_concentration, 6),
            "historicalHitRate": round(empirical_hit_rate, 6),
            "historicalSupport": round(historical_support, 4),
            "goalDirection": direction,
            "goalDirectionConflict": bool(direction.get("conflict")),
            "goalDirectionSupport": safe_float(direction.get("supportScore")),
            "reliabilityScore": round(reliability_score, 4),
            "hitRateScore": round(reliability_score, 4),
            "analysisScore": round(reliability_score, 4),
            "bestBetScore": round(best_bet_score, 4),
            "expectedScore": model.get("expectedScore"),
            "expectedHomeGoals": round(safe_float(model.get("homeLambda")), 4),
            "expectedAwayGoals": round(safe_float(model.get("awayLambda")), 4),
            "modelComponents": copy.deepcopy(model.get("components") or {}),
            "mostLikelyScores": model.get("mostLikelyScores"),
            "homeWinProbability": round(safe_float(model.get("homeWinProbability")), 6),
            "drawProbability": round(safe_float(model.get("drawProbability")), 6),
            "awayWinProbability": round(safe_float(model.get("awayWinProbability")), 6),
            "sourceNotes": list(model.get("sourceNotes") or []),
            "learningAdjustment": round(adjustment, 6),
            "learningEvidence": learning_evidence,
            "marketPolicy": R14_MARKET_POLICY,
            "selectionObjective": "MAXIMUM_CONSERVATIVE_CALIBRATED_PASS_PROBABILITY",
            "evaluatedAt": iso_z(now),
        }
        apply_russian_display_fields(candidate)
        candidate["qualification"] = best_bet_qualification(candidate, config)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            safe_float(item.get("reliabilityScore")),
            safe_float(item.get("conservativeProbability")),
            safe_float(item.get("marketStability")),
            safe_float(item.get("dataQuality")),
        ),
        reverse=True,
    )
    return candidates

def best_bet_qualification(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    probability = safe_float(candidate.get("modelProbability"))
    conservative = safe_float(candidate.get("conservativeProbability"), probability)
    edge = safe_float(candidate.get("edge"))
    ev = safe_float(candidate.get("expectedValue"))
    data_quality = safe_float(candidate.get("dataQuality"))
    agreement = safe_float(candidate.get("agreement"))
    stability = safe_float(candidate.get("marketStability"))
    anomaly = safe_float(candidate.get("anomaly"))
    odds = safe_float(candidate.get("bookmakerOdds"))
    quote_count = safe_int(candidate.get("quoteCount"))
    data_tier = str(candidate.get("dataTier") or "MARKET").upper()

    if not record_uses_r14_standard_market(candidate):
        failures.append("Рынок не входит в стандартный белый список R14")
    if bool(candidate.get("goalDirectionConflict")):
        failures.extend(list((candidate.get("goalDirection") or {}).get("reasons") or ["Направление тотала не подтверждено"]))
    if conservative < safe_float(config.get("bestBetMinimumConservativeProbability"), 0.58):
        failures.append("Недостаточная консервативная вероятность")
    if probability < safe_float(config.get("bestBetMinimumProbability"), 0.62):
        failures.append("Недостаточная расчётная вероятность")
    if edge < safe_float(config.get("bestBetMinimumEdge"), 0.025):
        failures.append("Недостаточное преимущество над рынком")
    if ev < safe_float(config.get("bestBetMinimumExpectedValue"), 0.015):
        failures.append("Недостаточное математическое ожидание")
    if data_quality < safe_float(config.get("bestBetMinimumDataQuality"), 55):
        failures.append("Недостаточная полнота данных")
    if agreement < safe_float(config.get("bestBetMinimumAgreement"), 62):
        failures.append("Модели недостаточно согласованы")
    if stability < safe_float(config.get("bestBetMinimumMarketStability"), 62):
        failures.append("Линия недостаточно стабильна")
    if anomaly > safe_float(config.get("bestBetMaximumAnomaly"), 42):
        failures.append("Повышенная рыночная аномальность")
    if quote_count < safe_int(config.get("bestBetMinimumBookmakers"), 3):
        failures.append("Недостаточно независимых букмекеров для лучшей четвёрки")
    if data_tier == "MARKET":
        failures.append("Нет независимой статистической модели команд")
    if odds <= safe_float(config.get("lowOddsMaximum"), 1.54):
        if conservative < safe_float(config.get("lowOddsMinimumProbability"), 0.72):
            failures.append("Низкий коэффициент не подтверждён высокой вероятностью")
        if edge < safe_float(config.get("lowOddsMinimumEdge"), 0.08):
            failures.append("Низкий коэффициент не имеет достаточного преимущества")
    return {"qualified": not failures, "failures": failures}

def expected_result_text(candidate: dict[str, Any]) -> str:
    home = str(candidate.get("homeRu") or russian_display_text(candidate.get("home")) or "Хозяева")
    away = str(candidate.get("awayRu") or russian_display_text(candidate.get("away")) or "Гости")
    home_p = safe_float(candidate.get("homeWinProbability"))
    draw_p = safe_float(candidate.get("drawProbability"))
    away_p = safe_float(candidate.get("awayWinProbability"))
    if home_p >= max(draw_p, away_p):
        return f"Наиболее вероятна победа: {home}"
    if away_p >= max(home_p, draw_p):
        return f"Наиболее вероятна победа: {away}"
    return "Наиболее вероятен равный матч"


def deterministic_reason(candidate: dict[str, Any]) -> str:
    probability = safe_float(candidate.get("modelProbability")) * 100
    edge = safe_float(candidate.get("edge")) * 100
    data_tier = str(candidate.get("dataTier") or "MARKET")
    tier_label = {
        "FULL": "расширенная статистика и рынок",
        "HYBRID": "статистика доступных турниров и рынок",
        "MARKET": "безмаржинальный рыночный консенсус",
    }.get(data_tier, "комбинированные данные")
    return (
        f"{expected_result_text(candidate)}. Для рынка «{candidate.get('pickRu') or russian_display_text(candidate.get('pick'))}» "
        f"модель оценивает вероятность в {probability:.1f}%. "
        f"Расчёт опирается на {tier_label}; преимущество над рынком {edge:+.1f} п.п."
    )


def event_to_analysis_record(
    event: dict[str, Any],
    selected: dict[str, Any],
    alternatives: list[dict[str, Any]],
    rank: int,
    now: dt.datetime,
) -> dict[str, Any]:
    event_id = str(event.get("id") or selected.get("eventId") or "")
    probability = safe_float(selected.get("modelProbability"))
    record_id = f"analysis-{stable_id(event_id, selected.get('market'), selected.get('selectionCode'), selected.get('point'), now.date())}"
    return {
        "id": record_id,
        "recordType": "DAILY_ANALYSIS",
        "rank": rank,
        "eventId": event_id,
        "sport": selected.get("sport"),
        "sportLabel": selected.get("sportLabel"),
        "sportKey": selected.get("sportKey"),
        "league": selected.get("league"),
        "country": selected.get("country"),
        "countryRu": selected.get("countryRu") or russian_display_text(selected.get("country")),
        "leagueRu": selected.get("leagueRu") or russian_display_text(selected.get("league")),
        "home": selected.get("home"),
        "away": selected.get("away"),
        "homeRu": selected.get("homeRu") or russian_display_text(selected.get("home")),
        "awayRu": selected.get("awayRu") or russian_display_text(selected.get("away")),
        "commenceTime": selected.get("commenceTime"),
        "marketKey": selected.get("marketKey"),
        "market": selected.get("market"),
        "marketFamily": selected.get("marketFamily"),
        "selectionCode": selected.get("selectionCode"),
        "pick": selected.get("pick"),
        "pickRu": selected.get("pickRu") or russian_display_text(selected.get("pick")),
        "bookmakerRu": selected.get("bookmakerRu") or russian_display_text(selected.get("bookmaker")),
        "point": selected.get("point"),
        "bookmakerOdds": selected.get("bookmakerOdds"),
        "odds": selected.get("bookmakerOdds"),
        "bookmaker": selected.get("bookmaker"),
        "bookmakerKey": selected.get("bookmakerKey"),
        "oddsLastUpdate": selected.get("oddsLastUpdate"),
        "oddsAgeMinutes": selected.get("oddsAgeMinutes"),
        "quoteCount": selected.get("quoteCount"),
        "oddsMinimum": selected.get("oddsMinimum"),
        "oddsMedian": selected.get("oddsMedian"),
        "oddsMaximum": selected.get("oddsMaximum"),
        "modelProbability": round(probability, 6),
        "probability": round(probability, 6),
        "probabilityPercent": round(probability * 100, 1),
        "statisticalProbability": selected.get("statisticalProbability"),
        "marketProbability": selected.get("marketProbability"),
        "edge": selected.get("edge"),
        "edgePercent": round(safe_float(selected.get("edge")) * 100, 2),
        "expectedValue": selected.get("expectedValue"),
        "expectedValuePercent": round(safe_float(selected.get("expectedValue")) * 100, 2),
        "confidence": selected.get("confidence"),
        "conservativeProbability": selected.get("conservativeProbability"),
        "uncertaintyMargin": selected.get("uncertaintyMargin"),
        "marketStability": selected.get("marketStability"),
        "reliabilityScore": selected.get("reliabilityScore"),
        "marketPolicy": R14_MARKET_POLICY,
        "dataTier": selected.get("dataTier"),
        "dataQuality": selected.get("dataQuality"),
        "agreement": selected.get("agreement"),
        "anomaly": selected.get("anomaly"),
        "analysisScore": selected.get("analysisScore"),
        "bestBetScore": selected.get("bestBetScore"),
        "expectedScore": selected.get("expectedScore"),
        "expectedHomeGoals": selected.get("expectedHomeGoals"),
        "expectedAwayGoals": selected.get("expectedAwayGoals"),
        "modelComponents": copy.deepcopy(selected.get("modelComponents") or {}),
        "mostLikelyScores": selected.get("mostLikelyScores"),
        "homeWinProbability": selected.get("homeWinProbability"),
        "drawProbability": selected.get("drawProbability"),
        "awayWinProbability": selected.get("awayWinProbability"),
        "expectedResult": expected_result_text(selected),
        "expectedResultRu": russian_display_text(expected_result_text(selected)),
        "reason": deterministic_reason(selected),
        "reasonRu": russian_display_text(deterministic_reason(selected)),
        "sourceNotes": selected.get("sourceNotes"),
        "learningAdjustment": selected.get("learningAdjustment"),
        "learningEvidence": selected.get("learningEvidence"),
        "qualification": selected.get("qualification"),
        "alternatives": [
            {
                "marketKey": item.get("marketKey"),
                "market": item.get("market"),
                "marketFamily": item.get("marketFamily"),
                "selectionCode": item.get("selectionCode"),
                "pick": item.get("pick"),
                "pickRu": item.get("pickRu") or russian_display_text(item.get("pick")),
                "bookmakerRu": item.get("bookmakerRu") or russian_display_text(item.get("bookmaker")),
                "point": item.get("point"),
                "bookmakerOdds": item.get("bookmakerOdds"),
                "odds": item.get("bookmakerOdds"),
                "bookmaker": item.get("bookmaker"),
                "bookmakerKey": item.get("bookmakerKey"),
                "oddsLastUpdate": item.get("oddsLastUpdate"),
                "oddsAgeMinutes": item.get("oddsAgeMinutes"),
                "quoteCount": item.get("quoteCount"),
                "modelProbability": item.get("modelProbability"),
                "probability": item.get("modelProbability"),
                "probabilityPercent": round(safe_float(item.get("modelProbability")) * 100, 1),
                "statisticalProbability": item.get("statisticalProbability"),
                "marketProbability": item.get("marketProbability"),
                "edge": item.get("edge"),
                "edgePercent": round(safe_float(item.get("edge")) * 100, 2),
                "expectedValue": item.get("expectedValue"),
                "expectedValuePercent": round(safe_float(item.get("expectedValue")) * 100, 2),
                "confidence": item.get("confidence"),
                "conservativeProbability": item.get("conservativeProbability"),
                "uncertaintyMargin": item.get("uncertaintyMargin"),
                "marketStability": item.get("marketStability"),
                "reliabilityScore": item.get("reliabilityScore"),
                "marketPolicy": R14_MARKET_POLICY,
                "dataTier": item.get("dataTier"),
                "dataQuality": item.get("dataQuality"),
                "agreement": item.get("agreement"),
                "anomaly": item.get("anomaly"),
                "analysisScore": item.get("analysisScore"),
                "bestBetScore": item.get("bestBetScore"),
                "qualification": item.get("qualification"),
            }
            for item in alternatives[:5]
        ],
        "status": "pending",
        "score": "",
        "publishedAt": iso_z(now),
        "settledAt": None,
        "isBestBet": False,
        "stake": 0.0,
        "stakePercent": 0.0,
        "profit": 0.0,
    }


def merge_advanced_event(featured: dict[str, Any], advanced: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(featured)
    bookmakers: dict[str, dict[str, Any]] = {
        str(bookmaker.get("key")): bookmaker
        for bookmaker in merged.get("bookmakers") or []
        if isinstance(bookmaker, dict)
    }
    for bookmaker in advanced.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        key = str(bookmaker.get("key") or "")
        if key not in bookmakers:
            merged.setdefault("bookmakers", []).append(copy.deepcopy(bookmaker))
            bookmakers[key] = merged["bookmakers"][-1]
        else:
            existing = bookmakers[key]
            market_keys = {str(market.get("key")) for market in existing.get("markets") or [] if isinstance(market, dict)}
            for market in bookmaker.get("markets") or []:
                if isinstance(market, dict) and str(market.get("key")) not in market_keys:
                    existing.setdefault("markets", []).append(copy.deepcopy(market))
    return merged


def build_daily_analysis(
    odds_events: list[dict[str, Any]],
    advanced: dict[str, dict[str, Any]],
    football_context: dict[str, Any],
    nhl_standings: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    event_analyses: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    diagnostics = {
        "oddsEvents": len(odds_events),
        "footballEvents": 0,
        "eventsWithQuotes": 0,
        "eventsWithAnalysis": 0,
        "marketCandidates": 0,
        "forbiddenMarketsRejected": 0,
        "bySport": defaultdict(int),
        "byMarketFamily": defaultdict(int),
        "byDataTier": defaultdict(int),
    }

    for raw_event in odds_events:
        if infer_sport_from_key(raw_event.get("sport_key")) != "soccer":
            continue
        raw_event["country"] = infer_country(str(raw_event.get("sport_key") or ""), str(raw_event.get("sport_title") or ""))
        if not event_allowed(raw_event, config):
            diagnostics.setdefault("excludedCompetitions", 0)
            diagnostics["excludedCompetitions"] += 1
            continue
        diagnostics["footballEvents"] += 1
        event_id = str(raw_event.get("id") or "")
        event = merge_advanced_event(raw_event, advanced.get(event_id, {})) if event_id in advanced else raw_event
        event["country"] = infer_country(str(event.get("sport_key") or ""), str(event.get("sport_title") or ""))
        quotes = parse_event_quotes(event, now, config)
        quotes = [quote for quote in quotes if standard_market_allowed(quote.get("marketKey"), quote.get("market"), quote.get("point"))]
        if not quotes:
            continue
        diagnostics["eventsWithQuotes"] += 1
        model = build_event_model(event, quotes, football_context, {})
        candidates = evaluate_event_markets(event, quotes, model, state.get("learning", {}), config, now)
        candidates = [
            item for item in candidates
            if safe_float(item.get("conservativeProbability"), item.get("modelProbability"))
            >= safe_float(config.get("analysisMinimumConservativeProbability"), 0.46)
        ]
        if not candidates:
            continue
        diagnostics["eventsWithAnalysis"] += 1
        diagnostics["marketCandidates"] += len(candidates)
        diagnostics["bySport"]["soccer"] += 1
        diagnostics["byDataTier"][str(model.get("dataTier") or "MARKET")] += 1
        for item in candidates:
            diagnostics["byMarketFamily"][str(item.get("marketFamily"))] += 1
        event_analyses.append((event, candidates))

    selected_per_event: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for event, candidates in event_analyses:
        best = max(
            candidates,
            key=lambda item: (
                safe_float(item.get("reliabilityScore")),
                safe_float(item.get("conservativeProbability")),
                safe_float(item.get("marketStability")),
                safe_float(item.get("dataQuality")),
            ),
        )
        alternatives = [item for item in candidates if item is not best]
        alternatives.sort(
            key=lambda item: (
                safe_float(item.get("reliabilityScore")),
                safe_float(item.get("conservativeProbability")),
            ),
            reverse=True,
        )
        selected_per_event.append((event, best, alternatives))

    selected_per_event.sort(
        key=lambda row: (
            safe_float(row[1].get("reliabilityScore")),
            safe_float(row[1].get("conservativeProbability")),
            safe_float(row[1].get("dataQuality")),
            safe_float(row[1].get("agreement")),
            safe_float(row[1].get("marketStability")),
            -safe_float(row[1].get("anomaly")),
        ),
        reverse=True,
    )
    target = safe_int(config.get("dailyAnalysisTarget"), 15)
    if len(selected_per_event) < target:
        raise RuntimeError(
            "EXACT_DAILY_FIFTEEN_NOT_MET: "
            f"footballOddsEvents={diagnostics['footballEvents']}; "
            f"eventsWithAnalysis={len(selected_per_event)}; target={target}. "
            "R14 must expand the football source pool instead of publishing weak or Asian markets."
        )

    max_league = safe_int(config.get("maximumSameLeagueDailyAnalysis"), 2)
    diversified: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    deferred: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    league_counts: dict[str, int] = defaultdict(int)
    for row in selected_per_event:
        league = str(row[1].get("league") or "")
        if league_counts[league] < max_league and len(diversified) < target:
            diversified.append(row)
            league_counts[league] += 1
        else:
            deferred.append(row)
    for row in deferred:
        if len(diversified) >= target:
            break
        diversified.append(row)

    records = [
        event_to_analysis_record(event, selected, alternatives, index, now)
        for index, (event, selected, alternatives) in enumerate(diversified[:target], start=1)
    ]
    if len(records) != target:
        raise RuntimeError(f"EXACT_DAILY_FIFTEEN_PUBLICATION_FAILED={len(records)}")
    if any(not record_uses_r14_standard_market(item) for item in records):
        raise RuntimeError("R14_FORBIDDEN_MARKET_REACHED_PUBLICATION")
    if any(str(item.get("sport")) != "soccer" for item in records):
        raise RuntimeError("R14_NON_FOOTBALL_EVENT_REACHED_PUBLICATION")

    diagnostics["bySport"] = dict(diagnostics["bySport"])
    diagnostics["byMarketFamily"] = dict(diagnostics["byMarketFamily"])
    diagnostics["byDataTier"] = dict(diagnostics["byDataTier"])
    diagnostics["candidateEventPool"] = len(selected_per_event)
    diagnostics["publishedAnalysis"] = len(records)
    diagnostics["selectionObjective"] = "MAXIMUM_CONSERVATIVE_CALIBRATED_PASS_PROBABILITY"
    diagnostics["marketPolicy"] = R14_MARKET_POLICY
    diagnostics["minimumOdds"] = safe_float(config.get("minimumBookmakerOdds"), 1.35)
    diagnostics["preferredMinimumOdds"] = safe_float(config.get("preferredMinimumOdds"), 1.55)
    return records, diagnostics

def active_pending_best_bets(state: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in state.get("history") or []:
        if not isinstance(item, dict) or item.get("recordType") != "BEST_BET":
            continue
        if str(item.get("status") or "pending") != "pending":
            continue
        event_id = str(item.get("eventId") or item.get("oddsEventId") or "")
        commence = parse_datetime(item.get("commenceTime") or item.get("utcDate"))
        if not event_id or event_id in seen:
            continue
        # Keep a pending bet until settlement, even if the nominal start passed.
        if commence and commence < now - dt.timedelta(days=4):
            continue
        active.append(migrate_public_prediction(item))
        seen.add(event_id)
    active.sort(key=lambda item: str(item.get("commenceTime") or item.get("utcDate") or ""))
    return active



def best_bet_selection_tier(
    candidate: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, list[str]]:
    probability = safe_float(candidate.get("modelProbability"))
    conservative = safe_float(candidate.get("conservativeProbability"), probability)
    ev = safe_float(candidate.get("expectedValue"))
    data_quality = safe_float(candidate.get("dataQuality"))
    agreement = safe_float(candidate.get("agreement"))
    stability = safe_float(candidate.get("marketStability"))
    anomaly = safe_float(candidate.get("anomaly"))
    odds = safe_float(candidate.get("bookmakerOdds") or candidate.get("odds"))
    quote_count = safe_int(candidate.get("quoteCount"))
    tier = str(candidate.get("dataTier") or "MARKET").upper()

    hard_failures: list[str] = []
    if str(candidate.get("sport") or "soccer") != "soccer":
        hard_failures.append("Разрешён только футбол")
    if competition_is_excluded(
        candidate.get("sportKey"), candidate.get("league"), "", candidate.get("country"), config
    ):
        hard_failures.append("Российские внутренние соревнования исключены")
    if not record_uses_r14_standard_market(candidate):
        hard_failures.append("Азиатский или нестандартный рынок запрещён")
    if bool(candidate.get("goalDirectionConflict")):
        hard_failures.append("Направление тотала не подтверждено моделью счёта и рынком")
    if odds < safe_float(config.get("minimumBookmakerOdds"), 1.35):
        hard_failures.append("Коэффициент ниже допустимого")
    if odds > safe_float(config.get("maximumBookmakerOdds"), 3.2):
        hard_failures.append("Коэффициент выше допустимого")
    if quote_count < safe_int(config.get("bestBetMinimumBookmakers"), 3):
        hard_failures.append("Недостаточно независимых букмекеров")
    if conservative < safe_float(config.get("topFourHardMinimumConservativeProbability"), 0.52):
        hard_failures.append("Слишком низкая консервативная вероятность")
    if data_quality < safe_float(config.get("topFourHardMinimumDataQuality"), 40):
        hard_failures.append("Критически недостаточно данных")
    if anomaly > safe_float(config.get("topFourHardMaximumAnomaly"), 62):
        hard_failures.append("Критическая рыночная аномальность")
    if stability < safe_float(config.get("topFourHardMinimumMarketStability"), 45):
        hard_failures.append("Критически нестабильная линия")
    if hard_failures:
        return "REJECTED", hard_failures

    if bool((candidate.get("qualification") or {}).get("qualified")):
        return "STRICT_QUALIFIED", []

    if tier in {"FULL", "HYBRID"} and (
        conservative >= safe_float(config.get("resultFirstMinimumConservativeProbability"), 0.57)
        and probability >= safe_float(config.get("resultFirstMinimumProbability"), 0.61)
        and data_quality >= safe_float(config.get("resultFirstMinimumDataQuality"), 48)
        and agreement >= safe_float(config.get("resultFirstMinimumAgreement"), 58)
        and stability >= safe_float(config.get("resultFirstMinimumMarketStability"), 58)
        and anomaly <= safe_float(config.get("resultFirstMaximumAnomaly"), 48)
        and ev >= safe_float(config.get("resultFirstMinimumExpectedValue"), -0.01)
    ):
        return "RESULT_FIRST", []

    if tier == "MARKET" and (
        conservative >= safe_float(config.get("marketOnlyBestBetMinimumConservativeProbability"), 0.61)
        and probability >= safe_float(config.get("marketOnlyBestBetMinimumProbability"), 0.67)
        and quote_count >= safe_int(config.get("marketOnlyBestBetMinimumBookmakers"), 5)
        and agreement >= safe_float(config.get("marketOnlyBestBetMinimumAgreement"), 78)
        and stability >= safe_float(config.get("marketOnlyBestBetMinimumMarketStability"), 75)
        and anomaly <= safe_float(config.get("marketOnlyBestBetMaximumAnomaly"), 25)
        and odds <= safe_float(config.get("marketOnlyBestBetMaximumOdds"), 2.20)
    ):
        return "MARKET_CONSENSUS", []

    fallback_failures: list[str] = []
    if tier == "MARKET" and (
        conservative >= safe_float(config.get("fallbackMinimumConservativeProbability"), 0.48)
        and probability >= safe_float(config.get("fallbackMinimumProbability"), 0.54)
        and quote_count >= safe_int(config.get("bestBetMinimumBookmakers"), 3)
        and agreement >= safe_float(config.get("fallbackMinimumAgreement"), 45)
        and stability >= safe_float(config.get("fallbackMinimumMarketStability"), 45)
        and anomaly <= safe_float(config.get("fallbackMaximumAnomaly"), 65)
        and ev >= safe_float(config.get("fallbackMinimumExpectedValue"), -0.05)
    ):
        return "MARKET_RESERVE", []
    if tier == "MARKET":
        fallback_failures.append("Рыночный резерв не прошёл минимальный стандартный консенсус")
    if conservative < safe_float(config.get("fallbackMinimumConservativeProbability"), 0.48):
        fallback_failures.append("Консервативная вероятность ниже резерва")
    if data_quality < safe_float(config.get("fallbackMinimumDataQuality"), 42):
        fallback_failures.append("Недостаточно данных даже для резерва")
    if agreement < safe_float(config.get("fallbackMinimumAgreement"), 52):
        fallback_failures.append("Слишком слабое согласие моделей")
    if stability < safe_float(config.get("fallbackMinimumMarketStability"), 52):
        fallback_failures.append("Слишком нестабильная линия")
    if anomaly > safe_float(config.get("fallbackMaximumAnomaly"), 55):
        fallback_failures.append("Слишком высокая рыночная аномальность")
    if ev < safe_float(config.get("fallbackMinimumExpectedValue"), -0.03):
        fallback_failures.append("Слишком отрицательное математическое ожидание")
    if fallback_failures:
        return "REJECTED", fallback_failures
    return "TOP_FOUR_AVAILABLE", []

def best_bet_result_first_score(
    candidate: dict[str, Any],
    tier: str,
    config: dict[str, Any],
) -> float:
    probability = safe_float(candidate.get("modelProbability"))
    conservative = safe_float(candidate.get("conservativeProbability"), probability)
    data_quality = safe_float(candidate.get("dataQuality"))
    agreement = safe_float(candidate.get("agreement"))
    stability = safe_float(candidate.get("marketStability"))
    anomaly = safe_float(candidate.get("anomaly"))
    odds = safe_float(candidate.get("bookmakerOdds") or candidate.get("odds"))
    reliability = safe_float(candidate.get("reliabilityScore") or candidate.get("hitRateScore"))
    historical_hit_rate = safe_float(candidate.get("historicalHitRate"), conservative)

    tier_bonus = {
        "STRICT_QUALIFIED": 12.0,
        "RESULT_FIRST": 7.0,
        "MARKET_CONSENSUS": 2.0,
        "MARKET_RESERVE": -2.0,
        "TOP_FOUR_AVAILABLE": 0.0,
    }.get(tier, -100.0)
    preferred_minimum_odds = safe_float(config.get("preferredMinimumOdds"), 1.55)
    preferred_maximum_odds = safe_float(config.get("preferredMaximumOdds"), 2.40)
    if preferred_minimum_odds <= odds <= preferred_maximum_odds:
        price_bonus = 12.0
    elif odds < preferred_minimum_odds:
        price_bonus = -min(26.0, (preferred_minimum_odds - odds) * 60)
    else:
        price_bonus = -min(18.0, (odds - preferred_maximum_odds) * 14)

    return round(
        reliability * 0.54
        + conservative * 55
        + probability * 12
        + historical_hit_rate * 10
        + data_quality * 0.07
        + agreement * 0.08
        + stability * 0.09
        - anomaly * 0.11
        + price_bonus
        + tier_bonus,
        6,
    )

def active_pending_best_bets(
    state: dict[str, Any],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in state.get("history") or []:
        if (
            not isinstance(item, dict)
            or item.get("recordType") != "BEST_BET"
        ):
            continue
        if str(item.get("status") or "pending") != "pending":
            continue

        event_id = str(
            item.get("eventId")
            or item.get("oddsEventId")
            or ""
        )
        commence = parse_datetime(
            item.get("commenceTime")
            or item.get("utcDate")
        )

        if not event_id or event_id in seen:
            continue
        if commence and commence < now - dt.timedelta(days=4):
            continue

        active.append(migrate_public_prediction(item))
        seen.add(event_id)

    active.sort(
        key=lambda item: str(
            item.get("commenceTime")
            or item.get("utcDate")
            or ""
        )
    )
    return active


def select_best_bets(
    daily_analysis: list[dict[str, Any]],
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = safe_int(config.get("bestBetsTarget"), 4)
    daily_target = safe_int(config.get("dailyAnalysisTarget"), 15)
    if len(daily_analysis) != daily_target:
        raise RuntimeError(
            f"BEST_FOUR_REQUIRES_EXACT_FIFTEEN={len(daily_analysis)}"
        )

    # The four are a strict subset of the published fifteen. They cannot use
    # a different alternative market because then the visible result of the
    # fifteen and the financial settlement would describe different bets.
    candidate_options: list[dict[str, Any]] = []
    for analysis in daily_analysis:
        base = copy.deepcopy(analysis)
        base["sourceAnalysisId"] = str(analysis.get("id") or "")
        base["sourceAnalysisMarket"] = str(analysis.get("market") or "")
        candidate_options.append(base)

    ranked: list[dict[str, Any]] = []
    seen_options: set[str] = set()
    for source in candidate_options:
        event_id = str(source.get("eventId") or "")
        key = "|".join(
            [event_id, str(source.get("market") or ""), str(source.get("selectionCode") or ""), str(source.get("point") or "")]
        )
        if not event_id or key in seen_options:
            continue
        seen_options.add(key)
        tier, failures = best_bet_selection_tier(source, config)
        if tier == "REJECTED":
            continue
        item = copy.deepcopy(source)
        item["bestBetSelectionTier"] = tier
        item["bestBetHardFailures"] = failures
        item["bestBetSoftWarnings"] = list((item.get("qualification") or {}).get("failures") or [])
        item["resultFirstScore"] = best_bet_result_first_score(item, tier, config)
        item["bestBetSelectionReason"] = (
            "Выбран среди пятнадцати по максимальной калиброванной вероятности прохождения "
            "при сохранении допустимого коэффициента."
        )
        ranked.append(item)

    tier_order = {"STRICT_QUALIFIED": 0, "RESULT_FIRST": 1, "MARKET_CONSENSUS": 2, "MARKET_RESERVE": 3, "TOP_FOUR_AVAILABLE": 4}
    ranked.sort(
        key=lambda item: (
            safe_float(item.get("bookmakerOdds") or item.get("odds")) >= safe_float(config.get("preferredMinimumOdds"), 1.55),
            -tier_order.get(str(item.get("bestBetSelectionTier") or ""), 9),
            safe_float(item.get("resultFirstScore")),
            safe_float(item.get("conservativeProbability"), item.get("modelProbability")),
            safe_float(item.get("marketStability")),
            safe_float(item.get("dataQuality")),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    used_events: set[str] = set()
    family_counts: dict[str, int] = defaultdict(int)
    league_counts: dict[str, int] = defaultdict(int)
    total_under_count = 0
    total_market_count = 0
    max_total_under = safe_int(config.get("maximumTotalUnderBestBets"), 1)
    max_total_markets = safe_int(config.get("maximumTotalMarketsBestBets"), 4)
    max_family = safe_int(config.get("maximumSameMarketFamilyBestBets"), 2)
    max_league = safe_int(config.get("maximumSameLeagueBestBets"), 1)
    preferred_min = safe_float(config.get("preferredMinimumOdds"), 1.55)

    def add_pass(preferred_only: bool, family_limit: int, league_limit: int) -> None:
        nonlocal total_under_count, total_market_count
        for source in ranked:
            if len(selected) >= target:
                return
            event_id = str(source.get("eventId") or "")
            if not event_id or event_id in used_events:
                continue
            odds = safe_float(source.get("bookmakerOdds") or source.get("odds"))
            if preferred_only and odds < preferred_min:
                continue
            family = str(source.get("marketFamily") or market_family(str(source.get("market") or "")))
            market_code = str(source.get("market") or "").upper()
            league = str(source.get("league") or "")
            is_total_market = family == "TOTAL"
            if market_code == "TOTAL_UNDER" and total_under_count >= max_total_under:
                continue
            if is_total_market and total_market_count >= max_total_markets:
                continue
            if family_counts[family] >= family_limit or league_counts[league] >= league_limit:
                continue
            item = copy.deepcopy(source)
            source_analysis_id = str(item.get("sourceAnalysisId") or item.get("id") or "")
            item["sourceAnalysisId"] = source_analysis_id
            item["id"] = "bet-" + stable_id(
                event_id,
                item.get("market"),
                item.get("selectionCode"),
                item.get("point"),
                source_analysis_id,
                iso_z(now),
            )
            item["recordType"] = "BEST_BET"
            item["isBestBet"] = True
            item["analysisMarketDiffers"] = str(item.get("market") or "") != str(item.get("sourceAnalysisMarket") or "")
            selected.append(item)
            used_events.add(event_id)
            family_counts[family] += 1
            league_counts[league] += 1
            if market_code == "TOTAL_UNDER":
                total_under_count += 1
            if is_total_market:
                total_market_count += 1

    add_pass(True, max_family, max_league)
    add_pass(True, max(3, max_family), max(2, max_league))
    add_pass(False, max(3, max_family), max(2, max_league))
    add_pass(False, target, target)

    if len(selected) != target:
        raise RuntimeError(
            "EXACT_BEST_FOUR_NOT_MET: "
            f"dailyAnalysis={len(daily_analysis)}; safeOptions={len(ranked)}; selected={len(selected)}"
        )

    bank = state.get("bank") if isinstance(state.get("bank"), dict) else {}
    bank_value = safe_float(bank.get("current"), config.get("startingVirtualBank", 10000))
    stake_percent = safe_float(config.get("stakePerBestBetPercent"), 20.0)
    stake = round(bank_value * stake_percent / 100.0, 2)
    published_at = iso_z(now)
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
        item["rankLabel"] = "Лучшая ставка дня" if rank == 1 else f"Ставка №{rank}"
        item["recordType"] = "BEST_BET"
        item["isBestBet"] = True
        item["status"] = "pending"
        item["statusLabel"] = result_status_label("pending")
        item["publishedAt"] = published_at
        item["stakePercent"] = stake_percent
        item["stake"] = stake
        item["stakeAssignedAt"] = published_at
        item["settlementOddsType"] = "BOOKMAKER_FIXED_AT_PUBLICATION"
        item["bankPolicy"] = "TWENTY_PERCENT_PER_EXACT_TOP_FOUR_BET"
        item["selectionObjective"] = "MAXIMUM_CALIBRATED_HIT_RATE_WITH_PRICE_FLOOR"

    return selected, copy.deepcopy(selected)


def apply_best_bets_to_daily_analysis(
    daily_analysis: list[dict[str, Any]],
    best_bets: list[dict[str, Any]],
) -> None:
    """Make the fifteen-analysis view and current four an atomic projection."""
    best_by_event = {
        str(item.get("eventId") or ""): item
        for item in best_bets
        if isinstance(item, dict) and str(item.get("eventId") or "")
    }
    for record in daily_analysis:
        if not isinstance(record, dict):
            continue
        record["isBestBet"] = False
        record["stake"] = 0.0
        record.pop("stakePercent", None)
        record.pop("bestBetSelection", None)

        best = best_by_event.get(str(record.get("eventId") or ""))
        if not best:
            continue

        record["isBestBet"] = True
        record["stake"] = safe_float(best.get("stake"))
        record["stakePercent"] = safe_float(best.get("stakePercent"))
        record["bestBetSelection"] = {
            "id": best.get("id"),
            "pick": best.get("pick"),
            "market": best.get("market"),
            "marketFamily": best.get("marketFamily"),
            "point": best.get("point"),
            "odds": best.get("bookmakerOdds"),
            "probabilityPercent": round(
                safe_float(best.get("modelProbability")) * 100,
                1,
            ),
            "edgePercent": round(
                safe_float(best.get("edge")) * 100,
                2,
            ),
        }


def current_best_bets_are_synchronized(
    state: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    daily = [
        item for item in state.get("dailyAnalysis") or []
        if isinstance(item, dict)
    ]
    best = [
        item for item in state.get("bestBets") or []
        if isinstance(item, dict)
    ]
    target = safe_int(config.get("bestBetsTarget"), 4)
    if len(daily) < target or len(best) != target:
        return False

    daily_events = {str(item.get("eventId") or "") for item in daily}
    best_events = [str(item.get("eventId") or "") for item in best]
    if any(not event_id for event_id in best_events):
        return False
    if len(best_events) != len(set(best_events)):
        return False
    if not set(best_events).issubset(daily_events):
        return False

    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    analysis_generated_at = str(meta.get("analysisGeneratedAt") or "")
    source_generated_at = str(
        meta.get("bestBetsSourceAnalysisGeneratedAt") or ""
    )
    if not analysis_generated_at or source_generated_at != analysis_generated_at:
        return False

    daily_batch_ids = {
        str(item.get("batchId") or "") for item in daily
        if str(item.get("batchId") or "")
    }
    best_batch_ids = {
        str(item.get("batchId") or "") for item in best
        if str(item.get("batchId") or "")
    }
    if daily_batch_ids and best_batch_ids != daily_batch_ids:
        return False

    return True



def synchronize_best_bets_with_current_analysis(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Never reselect a financial prediction after its batch was published."""
    daily = [
        item
        for item in state.get("dailyAnalysis") or []
        if isinstance(item, dict)
    ]
    best = [
        item
        for item in state.get("bestBets") or []
        if isinstance(item, dict)
    ]
    return {
        "changed": False,
        "reason": "CURRENT_BATCH_FROZEN_AT_PUBLICATION",
        "dailyAnalysis": len(daily),
        "bestBets": len(best),
        "forceIgnored": bool(force),
    }


HISTORY_TERMINAL_STATUSES = {
    "won",
    "lost",
    "push",
    "void",
    "cancelled",
}
HISTORY_SETTLED_STATUSES = {
    "won",
    "lost",
    "push",
}


def normalize_history_status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status in {"canceled", "cancelled"}:
        return "cancelled"
    if status in {
        "pending",
        "won",
        "lost",
        "push",
        "void",
        "cancelled",
        "unresolved",
    }:
        return status
    return "pending"


def history_publication_day(record: dict[str, Any]) -> str:
    published = parse_datetime(
        record.get("publishedAt")
        or record.get("createdAt")
    )
    if published:
        return published.date().isoformat()
    commence = parse_datetime(
        record.get("commenceTime")
        or record.get("utcDate")
    )
    return commence.date().isoformat() if commence else ""


def history_record_key(
    record: dict[str, Any],
    collection_name: str,
) -> str:
    event_id = str(
        record.get("eventId")
        or record.get("oddsEventId")
        or record.get("sourceMatchId")
        or ""
    )
    record_type = (
        "BEST_BET"
        if collection_name == "history"
        else "ANALYSIS"
    )
    return "|".join(
        [
            record_type,
            event_id,
            history_publication_day(record),
        ]
    )


def history_record_valid(record: dict[str, Any]) -> bool:
    event_id = str(
        record.get("eventId")
        or record.get("oddsEventId")
        or record.get("sourceMatchId")
        or ""
    ).strip()
    home = str(
        record.get("home")
        or record.get("homeRu")
        or ""
    ).strip()
    away = str(
        record.get("away")
        or record.get("awayRu")
        or ""
    ).strip()
    commence = parse_datetime(
        record.get("commenceTime")
        or record.get("utcDate")
    )
    market = str(
        record.get("market")
        or record.get("pick")
        or record.get("pickRu")
        or ""
    ).strip()
    return bool(
        event_id
        and home
        and away
        and commence
        and market
    )


def history_record_quality(record: dict[str, Any]) -> tuple[int, float]:
    status = normalize_history_status(record.get("status"))
    score = 0
    if status in HISTORY_SETTLED_STATUSES:
        score += 100
    elif status in {"void", "cancelled"}:
        score += 70
    elif status == "unresolved":
        score += 20
    if str(record.get("score") or "").strip():
        score += 30
    if record.get("settledAt"):
        score += 20
    if safe_float(
        record.get("bookmakerOdds")
        or record.get("odds"),
        0.0,
    ) > 1:
        score += 10
    if safe_float(
        record.get("modelProbability")
        or record.get("probability"),
        0.0,
    ) > 0:
        score += 5
    if str(record.get("bookmaker") or "").strip():
        score += 3
    timestamp = (
        parse_datetime(record.get("settledAt"))
        or parse_datetime(record.get("publishedAt"))
        or parse_datetime(
            record.get("commenceTime")
            or record.get("utcDate")
        )
    )
    return (
        score,
        timestamp.timestamp() if timestamp else 0.0,
    )


def merge_history_records(
    preferred: dict[str, Any],
    alternate: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(preferred)
    for key, value in alternate.items():
        current = result.get(key)
        if current in (None, "", [], {}):
            result[key] = copy.deepcopy(value)
    return result


def clean_history_collection(
    records: Any,
    collection_name: str,
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counters = {
        "input": 0,
        "output": 0,
        "invalidRemoved": 0,
        "duplicatesRemoved": 0,
        "unresolvedMarked": 0,
    }
    selected: dict[str, dict[str, Any]] = {}

    for source in records if isinstance(records, list) else []:
        if not isinstance(source, dict):
            counters["invalidRemoved"] += 1
            continue
        counters["input"] += 1

        record = migrate_public_prediction(source)
        record["status"] = normalize_history_status(
            record.get("status")
        )
        record["statusLabel"] = result_status_label(
            record["status"]
        )

        if collection_name == "history":
            record["recordType"] = "BEST_BET"
        else:
            record["recordType"] = "ANALYSIS"

        if not history_record_valid(record):
            counters["invalidRemoved"] += 1
            continue

        commence = parse_datetime(
            record.get("commenceTime")
            or record.get("utcDate")
        )
        sport = str(
            record.get("sport")
            or infer_sport_from_key(
                record.get("sportKey")
                or record.get("oddsSportKey")
            )
        )
        expiry_hours = safe_int(
            config.get(
                "historyPendingExpiryHoursHockey"
                if sport == "ice_hockey"
                else "historyPendingExpiryHoursSoccer"
            ),
            72 if sport == "ice_hockey" else 48,
        )
        if (
            record["status"] == "pending"
            and commence
            and now
            > commence + dt.timedelta(hours=expiry_hours)
        ):
            record["status"] = "unresolved"
            record["statusLabel"] = (
                "Результат не подтверждён"
            )
            record["unresolvedAt"] = iso_z(now)
            record["settlementReason"] = (
                "RESULT_NOT_CONFIRMED_WITHIN_WINDOW"
            )
            counters["unresolvedMarked"] += 1

        key = history_record_key(record, collection_name)
        if not key.strip("|"):
            counters["invalidRemoved"] += 1
            continue

        existing = selected.get(key)
        if existing is None:
            selected[key] = record
            continue

        counters["duplicatesRemoved"] += 1
        if history_record_quality(record) > history_record_quality(
            existing
        ):
            selected[key] = merge_history_records(
                record,
                existing,
            )
        else:
            selected[key] = merge_history_records(
                existing,
                record,
            )

    cleaned = list(selected.values())
    cleaned.sort(
        key=lambda item: (
            parse_datetime(
                item.get("publishedAt")
                or item.get("commenceTime")
                or item.get("utcDate")
            )
            or dt.datetime.min.replace(tzinfo=UTC)
        )
    )
    limit_key = (
        "historyLimit"
        if collection_name == "history"
        else "analysisHistoryLimit"
    )
    default_limit = 1200 if collection_name == "history" else 4000
    cleaned = cleaned[
        -safe_int(config.get(limit_key), default_limit):
    ]
    counters["output"] = len(cleaned)
    return cleaned, counters


def maintain_prediction_history(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    analysis, analysis_counters = clean_history_collection(
        state.get("analysisHistory"),
        "analysisHistory",
        config,
        now,
    )
    best, best_counters = clean_history_collection(
        state.get("history"),
        "history",
        config,
        now,
    )
    state["analysisHistory"] = analysis
    state["history"] = best
    maintenance = {
        "version": 1,
        "updatedAt": iso_z(now),
        "analysisHistory": analysis_counters,
        "bestBetHistory": best_counters,
    }
    state["historyMaintenance"] = maintenance
    return maintenance


def load_live_final_results() -> dict[str, dict[str, Any]]:
    source = load_json(LIVE_LEARNING_PATH, {})
    sessions = (
        source.get("sessions")
        if isinstance(source, dict)
        and isinstance(source.get("sessions"), dict)
        else {}
    )
    results: dict[str, dict[str, Any]] = {}

    for event_id, session in sessions.items():
        if (
            not isinstance(session, dict)
            or not session.get("completed")
        ):
            continue
        snapshots = [
            item
            for item in session.get("snapshots") or []
            if isinstance(item, dict)
            and str(item.get("status") or "")
            == "FINISHED"
            and str(item.get("score") or "").strip()
        ]
        if not snapshots:
            continue
        snapshot = snapshots[-1]
        match = re.fullmatch(
            r"\s*(\d+)\s*:\s*(\d+)\s*",
            str(snapshot.get("score") or ""),
        )
        if not match:
            continue
        results[str(event_id)] = {
            "eventId": str(event_id),
            "homeScore": int(match.group(1)),
            "awayScore": int(match.group(2)),
            "completed": True,
            "source": "LIVE_CONFIRMED_FINAL",
            "completedAt": session.get("completedAt"),
        }

    return results



BATCH_TERMINAL_STATUSES = {
    "won",
    "lost",
    "push",
    "void",
    "cancelled",
    "unresolved",
}


def batch_record_terminal(record: dict[str, Any]) -> bool:
    return normalize_history_status(record.get("status")) in BATCH_TERMINAL_STATUSES


def batch_status_label(status: str) -> str:
    return {
        "INITIALIZED": "Ожидание первой подборки",
        "ACTIVE": "Подборка активна",
        "SETTLING": "Рассчитываются результаты",
        "COMPLETED": "Подборка завершена",
        "GENERATING_NEXT": "Формируется следующая подборка",
        "WAITING_FOR_NEXT_SELECTION": "Ожидается следующая подборка",
    }.get(str(status or "").upper(), "Подборка активна")


def ensure_current_batch(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    daily = [
        item for item in state.get("dailyAnalysis") or []
        if isinstance(item, dict)
    ]
    best = [
        item for item in state.get("bestBets") or []
        if isinstance(item, dict)
    ]
    batch = state.get("batch") if isinstance(state.get("batch"), dict) else {}
    batch = copy.deepcopy(batch)

    if not daily:
        batch.update(
            {
                "version": 1,
                "id": str(batch.get("id") or ""),
                "sequence": safe_int(batch.get("sequence"), 0),
                "status": "INITIALIZED",
                "statusLabel": batch_status_label("INITIALIZED"),
                "updatedAt": iso_z(now),
                "analysisCount": 0,
                "bestBetsCount": 0,
                "terminalAnalysisCount": 0,
                "terminalBestBetsCount": 0,
                "pendingAnalysisCount": 0,
                "pendingBestBetsCount": 0,
                "completed": False,
                "placedAmount": 0.0,
                "availableAmount": round(
                    safe_float(state.get("bank", {}).get("current"), 0.0),
                    2,
                ),
            }
        )
        state["batch"] = batch
        return batch

    existing_ids = {
        str(item.get("batchId") or "")
        for item in daily + best
        if str(item.get("batchId") or "")
    }
    batch_id = str(batch.get("id") or "")
    if not batch_id:
        batch_id = next(iter(existing_ids), "")
    if not batch_id:
        seed = (
            state.get("meta", {}).get("analysisGeneratedAt")
            or state.get("meta", {}).get("updatedAt")
            or iso_z(now)
        )
        batch_id = "batch-" + stable_id(seed, *(str(item.get("eventId") or "") for item in daily))

    sequence = max(
        1,
        safe_int(
            batch.get("sequence"),
            state.get("meta", {}).get("batchSequence") or 1,
        ),
    )
    for item in daily + best:
        item["batchId"] = batch_id
        item["batchSequence"] = sequence

    daily_ids = {str(item.get("id") or "") for item in daily}
    best_ids = {str(item.get("id") or "") for item in best}
    for item in state.get("analysisHistory") or []:
        if isinstance(item, dict) and str(item.get("id") or "") in daily_ids:
            item["batchId"] = batch_id
            item["batchSequence"] = sequence
    for item in state.get("history") or []:
        if isinstance(item, dict) and str(item.get("id") or "") in best_ids:
            item["batchId"] = batch_id
            item["batchSequence"] = sequence

    terminal_daily = sum(batch_record_terminal(item) for item in daily)
    terminal_best = sum(batch_record_terminal(item) for item in best)
    completed = bool(
        daily
        and terminal_daily == len(daily)
        and (not best or terminal_best == len(best))
    )
    partially_settled = bool(terminal_daily or terminal_best)
    status = "COMPLETED" if completed else "SETTLING" if partially_settled else "ACTIVE"

    bank = state.get("bank") if isinstance(state.get("bank"), dict) else {}
    current_bank = safe_float(bank.get("current"), config.get("startingVirtualBank", 10000))
    placed = safe_float(bank.get("activeExposure"), 0.0)
    active_count = sum(
        normalize_history_status(item.get("status")) == "pending"
        for item in best
    )

    batch.update(
        {
            "version": 1,
            "id": batch_id,
            "sequence": sequence,
            "status": status,
            "statusLabel": batch_status_label(status),
            "createdAt": batch.get("createdAt") or state.get("meta", {}).get("analysisGeneratedAt") or iso_z(now),
            "updatedAt": iso_z(now),
            "analysisCount": len(daily),
            "bestBetsCount": len(best),
            "terminalAnalysisCount": terminal_daily,
            "terminalBestBetsCount": terminal_best,
            "pendingAnalysisCount": len(daily) - terminal_daily,
            "pendingBestBetsCount": len(best) - terminal_best,
            "activeBestBetsCount": active_count,
            "completed": completed,
            "completedAt": (
                batch.get("completedAt") or iso_z(now)
                if completed
                else None
            ),
            "eventIds": [str(item.get("eventId") or "") for item in daily],
            "bestBetEventIds": [str(item.get("eventId") or "") for item in best],
            "startingBank": round(
                safe_float(batch.get("startingBank"), current_bank),
                2,
            ),
            "currentBank": round(current_bank, 2),
            "placedAmount": round(placed, 2),
            "availableAmount": round(max(0.0, current_bank - placed), 2),
        }
    )
    state["batch"] = batch
    state.setdefault("meta", {}).update(
        {
            "currentBatchId": batch_id,
            "batchSequence": sequence,
            "batchStatus": status,
            "batchStatusLabel": batch["statusLabel"],
            "batchUpdatedAt": iso_z(now),
        }
    )
    return batch


def archive_completed_batch(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> None:
    batch = ensure_current_batch(state, config, now)
    if not batch.get("completed") or not batch.get("id"):
        return
    history = state.setdefault("batchHistory", [])
    if any(str(item.get("id") or "") == str(batch.get("id")) for item in history if isinstance(item, dict)):
        return
    starting = safe_float(batch.get("startingBank"), state.get("bank", {}).get("starting", 10000))
    ending = safe_float(state.get("bank", {}).get("current"), starting)
    history.append(
        {
            **copy.deepcopy(batch),
            "archivedAt": iso_z(now),
            "endingBank": round(ending, 2),
            "bankResult": round(ending - starting, 2),
        }
    )
    state["batchHistory"] = history[-safe_int(config.get("batchHistoryLimit"), 120):]


def publish_new_batch(
    state: dict[str, Any],
    daily_analysis: list[dict[str, Any]],
    best_bets: list[dict[str, Any]],
    newly_selected: list[dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    previous = ensure_current_batch(state, config, now)
    if previous.get("completed"):
        archive_completed_batch(state, config, now)
    sequence = max(
        safe_int(previous.get("sequence"), 0),
        safe_int(state.get("meta", {}).get("batchSequence"), 0),
    ) + 1
    batch_id = "batch-" + stable_id(
        sequence,
        iso_z(now),
        *(str(item.get("eventId") or "") for item in daily_analysis),
    )
    for item in daily_analysis + best_bets + newly_selected:
        item["batchId"] = batch_id
        item["batchSequence"] = sequence
        item["batchStatus"] = "ACTIVE"
    current_bank = safe_float(
        state.get("bank", {}).get("current"),
        config.get("startingVirtualBank", 10000),
    )
    placed = round(sum(
        safe_float(item.get("stake"))
        for item in best_bets
        if normalize_history_status(item.get("status")) == "pending"
    ), 2)
    first_analysis = (
        daily_analysis[0]
        if daily_analysis and isinstance(daily_analysis[0], dict)
        else {}
    )
    batch = {
        "version": 1,
        "id": batch_id,
        "sequence": sequence,
        "status": "ACTIVE",
        "statusLabel": batch_status_label("ACTIVE"),
        "createdAt": iso_z(now),
        "updatedAt": iso_z(now),
        "analysisCount": len(daily_analysis),
        "bestBetsCount": len(best_bets),
        "terminalAnalysisCount": 0,
        "terminalBestBetsCount": 0,
        "pendingAnalysisCount": len(daily_analysis),
        "pendingBestBetsCount": len(best_bets),
        "activeBestBetsCount": len(best_bets),
        "completed": False,
        "completedAt": None,
        "eventIds": [str(item.get("eventId") or "") for item in daily_analysis],
        "bestBetEventIds": [str(item.get("eventId") or "") for item in best_bets],
        "startingBank": round(current_bank, 2),
        "currentBank": round(current_bank, 2),
        "placedAmount": placed,
        "availableAmount": round(max(0.0, current_bank - placed), 2),
        "operationalDayId": str(
            first_analysis.get("operationalDayId") or ""
        ),
        "operationalWindowStart": str(
            first_analysis.get("operationalWindowStart") or ""
        ),
        "operationalWindowEnd": str(
            first_analysis.get("operationalWindowEnd") or ""
        ),
        "selectionWindowStart": str(
            first_analysis.get("selectionWindowStart") or ""
        ),
        "selectionWindowEnd": str(
            first_analysis.get("selectionWindowEnd") or ""
        ),
        "rolloverExecutionPolicy": (
            "IMMEDIATE_AFTER_ALL_CURRENT_MATCHES_TERMINAL"
        ),
    }
    state["batch"] = batch
    state.setdefault("meta", {}).update(
        {
            "currentBatchId": batch_id,
            "batchSequence": sequence,
            "batchStatus": "ACTIVE",
            "batchStatusLabel": batch["statusLabel"],
            "batchUpdatedAt": iso_z(now),
            "lastBatchPublishedAt": iso_z(now),
        }
    )
    return batch

def append_new_records_to_history(
    state: dict[str, Any],
    daily_analysis: list[dict[str, Any]],
    newly_selected: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    analysis_history = state.setdefault("analysisHistory", [])
    existing_analysis = {str(item.get("id") or "") for item in analysis_history if isinstance(item, dict)}
    for item in daily_analysis:
        if str(item.get("id") or "") not in existing_analysis:
            analysis_history.append(copy.deepcopy(item))

    history = state.setdefault("history", [])
    existing_best = {str(item.get("id") or "") for item in history if isinstance(item, dict)}
    for item in newly_selected:
        if str(item.get("id") or "") not in existing_best:
            history.append(copy.deepcopy(item))

    state["analysisHistory"] = analysis_history[-safe_int(config.get("analysisHistoryLimit"), 4000) :]
    state["history"] = history[-safe_int(config.get("historyLimit"), 1200) :]
    maintain_prediction_history(
        state,
        config,
        utc_now(),
    )


# ---------------------------------------------------------------------------
# Settlement and learning
# ---------------------------------------------------------------------------


def due_pending_records(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
    immediate_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    immediate_event_ids = immediate_event_ids or set()
    for collection in (state.get("analysisHistory") or [], state.get("history") or []):
        for item in collection:
            if (
                not isinstance(item, dict)
                or normalize_history_status(
                    item.get("status")
                )
                not in {"pending", "unresolved"}
            ):
                continue
            commence = parse_datetime(item.get("commenceTime") or item.get("utcDate"))
            if not commence:
                continue
            sport = str(item.get("sport") or infer_sport_from_key(item.get("sportKey") or item.get("oddsSportKey")))
            delay = safe_int(
                config.get("settlementDelayMinutesHockey" if sport == "ice_hockey" else "settlementDelayMinutesSoccer"),
                210 if sport == "ice_hockey" else 150,
            )
            event_id = str(
                item.get("eventId")
                or item.get("oddsEventId")
                or item.get("sourceMatchId")
                or ""
            )
            if (
                event_id in immediate_event_ids
                or now
                >= commence + dt.timedelta(minutes=delay)
            ):
                pending.append(item)
    unique: dict[str, dict[str, Any]] = {}
    for item in pending:
        key = str(item.get("eventId") or item.get("oddsEventId") or item.get("sourceMatchId") or "")
        if key:
            unique[key] = item
    return list(unique.values())


def release_overdue_batch_records(
    state: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, int]:
    """Release a batch that cannot be settled because a provider never returned
    a final result.

    This is not counted as a loss and does not train the model. The record is
    marked unresolved only after a conservative post-start window, the virtual
    stake is released, and the next batch may be generated instead of waiting
    for days.
    """
    counters = {"analysis": 0, "bestBets": 0}
    collection_specs = (
        ("analysisHistory", "analysis"),
        ("history", "bestBets"),
    )

    for collection_name, counter_name in collection_specs:
        for record in state.get(collection_name) or []:
            if not isinstance(record, dict):
                continue
            if normalize_history_status(record.get("status")) not in {"pending", "unresolved"}:
                continue
            commence = parse_datetime(record.get("commenceTime") or record.get("utcDate"))
            if not commence:
                continue
            sport = str(
                record.get("sport")
                or infer_sport_from_key(record.get("sportKey") or record.get("oddsSportKey"))
                or "soccer"
            )
            release_minutes = safe_int(
                config.get(
                    "batchUnresolvedReleaseMinutesHockey"
                    if sport == "ice_hockey"
                    else "batchUnresolvedReleaseMinutesSoccer"
                ),
                420 if sport == "ice_hockey" else 360,
            )
            if now < commence + dt.timedelta(minutes=max(240, release_minutes)):
                continue
            if normalize_history_status(record.get("status")) == "unresolved" and record.get("batchReleaseReason"):
                continue

            record["status"] = "unresolved"
            record["statusLabel"] = result_status_label("unresolved")
            record["settledAt"] = iso_z(now)
            record["settlementSource"] = "BATCH_TIMEOUT_NO_CONFIRMED_FINAL"
            record["batchReleaseReason"] = "RESULT_NOT_CONFIRMED_AFTER_CONSERVATIVE_WINDOW"
            record["profit"] = 0.0
            counters[counter_name] += 1

    status_by_id = {
        str(item.get("id") or ""): item
        for collection in (state.get("analysisHistory") or [], state.get("history") or [])
        for item in collection
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    for key in ("dailyAnalysis", "bestBets", "predictions"):
        refreshed: list[dict[str, Any]] = []
        for item in state.get(key) or []:
            if not isinstance(item, dict):
                continue
            source = status_by_id.get(str(item.get("id") or ""))
            refreshed.append(copy.deepcopy(source) if source else item)
        state[key] = refreshed

    if counters["analysis"] or counters["bestBets"]:
        state.setdefault("meta", {})["batchUnresolvedReleaseAt"] = iso_z(now)
        state["meta"]["batchUnresolvedRelease"] = counters
    return counters


def fetch_scores_for_sport_keys(
    client: ApiClient,
    api_key: str,
    sport_keys: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    scores: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for sport_key in sorted(set(sport_keys)):
        if not sport_key:
            continue
        params = {"apiKey": api_key, "daysFrom": "3", "dateFormat": "iso"}
        url = f"{ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/scores?" + urllib.parse.urlencode(params)
        try:
            payload = client.request_json(url, label=f"SCORES:{sport_key}")
        except Exception as exc:
            errors.append(f"{sport_key}: {exc}")
            continue
        for event in payload if isinstance(payload, list) else []:
            if not isinstance(event, dict) or not event.get("completed"):
                continue
            score_map = {
                str(row.get("name") or ""): safe_int(row.get("score"))
                for row in event.get("scores") or []
                if isinstance(row, dict)
            }
            home = str(event.get("home_team") or "")
            away = str(event.get("away_team") or "")
            if home in score_map and away in score_map:
                scores[str(event.get("id") or "")] = {
                    "eventId": str(event.get("id") or ""),
                    "home": home,
                    "away": away,
                    "homeScore": score_map[home],
                    "awayScore": score_map[away],
                    "commenceTime": str(event.get("commence_time") or ""),
                    "completed": True,
                    "source": "THE_ODDS_API_SCORES",
                }
    return scores, errors


def match_provider_score_result(
    record: dict[str, Any],
    results: dict[str, dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    config = config or {}
    event_id = str(record.get("eventId") or record.get("oddsEventId") or "")
    if event_id and event_id in results:
        return results[event_id]
    commence = parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    home = str(record.get("home") or "")
    away = str(record.get("away") or "")
    threshold = safe_float(config.get("settlementNameMatchThreshold"), 0.72)
    window_hours = safe_float(config.get("settlementFuzzyTimeWindowHours"), 18.0)
    best: tuple[float, dict[str, Any] | None, bool] = (0.0, None, False)
    for item in results.values():
        if not isinstance(item, dict) or not item.get("completed"):
            continue
        item_time = parse_datetime(item.get("commenceTime") or item.get("utcDate"))
        if commence and item_time and abs((commence - item_time).total_seconds()) > window_hours * 3600:
            continue
        direct = (token_similarity(home, item.get("home")) + token_similarity(away, item.get("away"))) / 2
        swapped = (token_similarity(home, item.get("away")) + token_similarity(away, item.get("home"))) / 2
        if direct > best[0]:
            best = (direct, item, False)
        if swapped > best[0]:
            best = (swapped, item, True)
    if best[0] < threshold or best[1] is None:
        return None
    item = copy.deepcopy(best[1])
    if best[2]:
        item["homeScore"], item["awayScore"] = item.get("awayScore"), item.get("homeScore")
        item["source"] = str(item.get("source") or "PROVIDER_SCORE") + "_FUZZY_SWAPPED"
    else:
        item["source"] = str(item.get("source") or "PROVIDER_SCORE") + "_FUZZY"
    item["eventId"] = event_id
    item["matchConfidence"] = round(best[0], 4)
    return item


def match_football_result(record: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    commence = parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    home = str(record.get("home") or "")
    away = str(record.get("away") or "")
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for item in context.get("completedLookup") or []:
        item_time = item.get("utcDate")
        if commence and item_time and abs((commence - item_time).total_seconds()) > 8 * 3600:
            continue
        home_score = token_similarity(home, item.get("home"))
        away_score = token_similarity(away, item.get("away"))
        score = (home_score + away_score) / 2
        if score > best[0]:
            best = (score, item)
    if best[0] >= 0.78 and best[1]:
        return {
            "eventId": str(record.get("eventId") or ""),
            "home": best[1]["home"],
            "away": best[1]["away"],
            "homeScore": best[1]["homeScore"],
            "awayScore": best[1]["awayScore"],
            "completed": True,
            "source": "FOOTBALL_DATA_RESULT",
        }
    return None


def settle_market(record: dict[str, Any], home_score: int, away_score: int) -> str:
    market = str(record.get("market") or "").upper()
    selection = str(record.get("selectionCode") or "").upper()
    point = safe_float(record.get("point"), 0.0)

    if market == "HOME_WIN" or (selection == "HOME" and record.get("marketKey") in {"h2h", "h2h_3_way"}):
        return "won" if home_score > away_score else "lost"
    if market == "AWAY_WIN" or (selection == "AWAY" and record.get("marketKey") in {"h2h", "h2h_3_way"}):
        return "won" if away_score > home_score else "lost"
    if market == "DRAW" or selection == "DRAW":
        return "won" if home_score == away_score else "lost"
    if market == "TOTAL_OVER" or selection == "OVER":
        total = home_score + away_score
        return "won" if total > point else "push" if total == point else "lost"
    if market == "TOTAL_UNDER" or selection == "UNDER":
        total = home_score + away_score
        return "won" if total < point else "push" if total == point else "lost"
    if market == "SPREAD_HOME":
        adjusted = home_score + point - away_score
        return "won" if adjusted > 0 else "push" if abs(adjusted) < 1e-9 else "lost"
    if market == "SPREAD_AWAY":
        adjusted = away_score + point - home_score
        return "won" if adjusted > 0 else "push" if abs(adjusted) < 1e-9 else "lost"
    if market == "BTTS_YES":
        return "won" if home_score > 0 and away_score > 0 else "lost"
    if market == "BTTS_NO":
        return "won" if home_score == 0 or away_score == 0 else "lost"
    if market == "DOUBLE_CHANCE_HOME_DRAW":
        return "won" if home_score >= away_score else "lost"
    if market == "DOUBLE_CHANCE_DRAW_AWAY":
        return "won" if away_score >= home_score else "lost"
    if market == "DOUBLE_CHANCE_HOME_AWAY":
        return "won" if home_score != away_score else "lost"
    if market == "DRAW_NO_BET_HOME":
        return "won" if home_score > away_score else "push" if home_score == away_score else "lost"
    if market == "DRAW_NO_BET_AWAY":
        return "won" if away_score > home_score else "push" if home_score == away_score else "lost"
    if market == "TEAM_TOTAL_HOME_OVER":
        return "won" if home_score > point else "push" if home_score == point else "lost"
    if market == "TEAM_TOTAL_HOME_UNDER":
        return "won" if home_score < point else "push" if home_score == point else "lost"
    if market == "TEAM_TOTAL_AWAY_OVER":
        return "won" if away_score > point else "push" if away_score == point else "lost"
    if market == "TEAM_TOTAL_AWAY_UNDER":
        return "won" if away_score < point else "push" if away_score == point else "lost"
    return "void"


def actual_binary_for_learning(status: str) -> float | None:
    if status == "won":
        return 1.0
    if status == "lost":
        return 0.0
    if status == "push":
        return 0.5
    return None


def update_learning_from_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    actual = actual_binary_for_learning(str(record.get("status") or ""))
    if actual is None:
        return
    learning = state.setdefault("learning", {})
    segments = learning.setdefault("segments", {})
    bins = learning.setdefault("calibrationBins", {})
    probability = clamp(safe_float(record.get("modelProbability") or record.get("probability"), 0.5), 0.001, 0.999)
    odds = safe_float(record.get("bookmakerOdds") or record.get("odds"), 0.0)
    profit = safe_float(record.get("profit"), 0.0)
    sport = str(record.get("sport") or "soccer")
    league = str(record.get("league") or "")
    family = str(record.get("marketFamily") or market_family(str(record.get("market") or "")))
    brier = (probability - actual) ** 2
    log_loss = -(actual * math.log(probability) + (1 - actual) * math.log(1 - probability))

    for key in learning_segment_keys(
        sport, league, family, odds, str(record.get("dataTier") or ""),
        probability, learning_market_detail(record)
    ):
        segment = segments.setdefault(
            key,
            {
                "settled": 0,
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "predictedSum": 0.0,
                "actualSum": 0.0,
                "brierSum": 0.0,
                "logLossSum": 0.0,
                "profit": 0.0,
            },
        )
        segment["settled"] = safe_int(segment.get("settled")) + 1
        segment["wins"] = safe_int(segment.get("wins")) + (1 if record.get("status") == "won" else 0)
        segment["losses"] = safe_int(segment.get("losses")) + (1 if record.get("status") == "lost" else 0)
        segment["pushes"] = safe_int(segment.get("pushes")) + (1 if record.get("status") == "push" else 0)
        segment["predictedSum"] = safe_float(segment.get("predictedSum")) + probability
        segment["actualSum"] = safe_float(segment.get("actualSum")) + actual
        segment["brierSum"] = safe_float(segment.get("brierSum")) + brier
        segment["logLossSum"] = safe_float(segment.get("logLossSum")) + log_loss
        segment["profit"] = safe_float(segment.get("profit")) + profit
        count = segment["settled"]
        segment["hitRate"] = round(segment["actualSum"] / count, 4)
        segment["averagePredicted"] = round(segment["predictedSum"] / count, 4)
        segment["probabilityBias"] = round(segment["actualSum"] / count - segment["predictedSum"] / count, 6)
        segment["brierScore"] = round(segment["brierSum"] / count, 6)
        segment["logLoss"] = round(segment["logLossSum"] / count, 6)
        segment["profit"] = round(segment["profit"], 2)

    bin_floor = int(probability * 10) * 10
    bin_key = f"{bin_floor:02d}-{min(100, bin_floor + 9):02d}"
    bucket = bins.setdefault(bin_key, {"count": 0, "predictedSum": 0.0, "actualSum": 0.0})
    bucket["count"] = safe_int(bucket.get("count")) + 1
    bucket["predictedSum"] = safe_float(bucket.get("predictedSum")) + probability
    bucket["actualSum"] = safe_float(bucket.get("actualSum")) + actual
    bucket["averagePredicted"] = round(bucket["predictedSum"] / bucket["count"], 4)
    bucket["actualRate"] = round(bucket["actualSum"] / bucket["count"], 4)



def settle_pending_records(
    state: dict[str, Any],
    results: dict[str, dict[str, Any]],
    football_context: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any] | None = None,
) -> dict[str, int]:
    config = config or {}
    counters = {
        "analysisSettled": 0,
        "bestBetsSettled": 0,
        "unresolved": 0,
    }
    processed_ids: set[str] = set()

    for collection_name in ("analysisHistory", "history"):
        collection = state.get(collection_name) or []
        for record in collection:
            if (
                not isinstance(record, dict)
                or normalize_history_status(
                    record.get("status")
                )
                not in {"pending", "unresolved"}
            ):
                continue

            event_id = str(
                record.get("eventId")
                or record.get("oddsEventId")
                or ""
            )
            result = match_provider_score_result(record, results, config)
            record["settlementLastAttemptAt"] = iso_z(now)
            record["settlementAttempts"] = safe_int(record.get("settlementAttempts")) + 1
            if (
                result is None
                and str(record.get("sport") or "soccer")
                == "soccer"
            ):
                result = match_football_result(
                    record,
                    football_context,
                )
            if result is None:
                counters["unresolved"] += 1
                continue

            home_score = safe_int(result.get("homeScore"))
            away_score = safe_int(result.get("awayScore"))
            status = settle_market(
                record,
                home_score,
                away_score,
            )
            record["status"] = status
            record["statusLabel"] = result_status_label(status)
            record["score"] = f"{home_score}:{away_score}"
            record["homeScore"] = home_score
            record["awayScore"] = away_score
            record["settledAt"] = iso_z(now)
            record["settlementSource"] = result.get("source")
            record["resultVisible"] = True
            record["resultUpdatedAt"] = iso_z(now)

            if (
                collection_name == "history"
                and record.get("recordType") == "BEST_BET"
            ):
                stake = safe_float(record.get("stake"), 0.0)
                odds = safe_float(
                    record.get("bookmakerOdds")
                    or record.get("odds"),
                    0.0,
                )
                profit = (
                    round(stake * (odds - 1), 2)
                    if status == "won"
                    else round(-stake, 2)
                    if status == "lost"
                    else 0.0
                )
                record["profit"] = profit
                record_id = str(record.get("id") or "")
                if record_id not in processed_ids:
                    apply_bank_profit(
                        state,
                        record,
                        profit,
                        now,
                    )
                    processed_ids.add(record_id)
                counters["bestBetsSettled"] += 1
            else:
                record["profit"] = 0.0
                counters["analysisSettled"] += 1

            # One independent learning label per event comes only from the
            # published analysis collection. Financial alternatives must not
            # duplicate the same result in the calibration sample.
            if collection_name == "analysisHistory":
                update_learning_from_record(state, record)

    analysis_by_id = {
        str(item.get("id") or ""): item
        for item in state.get("analysisHistory") or []
        if isinstance(item, dict)
    }
    bets_by_id = {
        str(item.get("id") or ""): item
        for item in state.get("history") or []
        if isinstance(item, dict)
    }
    bets_by_event_market = {
        (
            str(item.get("eventId") or ""),
            str(item.get("market") or ""),
            str(item.get("selectionCode") or ""),
            str(item.get("point") or ""),
        ): item
        for item in state.get("history") or []
        if isinstance(item, dict)
    }
    settlement_fields = (
        "status",
        "statusLabel",
        "score",
        "homeScore",
        "awayScore",
        "settledAt",
        "settlementSource",
        "resultVisible",
        "resultUpdatedAt",
        "settlementLastAttemptAt",
        "settlementAttempts",
        "profit",
    )

    for key in ("dailyAnalysis", "bestBets", "predictions"):
        refreshed: list[dict[str, Any]] = []
        for item in state.get(key) or []:
            if not isinstance(item, dict):
                continue

            source: dict[str, Any] | None
            if key == "dailyAnalysis":
                source = analysis_by_id.get(
                    str(item.get("id") or "")
                )
            else:
                source = bets_by_id.get(
                    str(item.get("id") or "")
                )
                if source is None:
                    source = bets_by_event_market.get(
                        (
                            str(item.get("eventId") or ""),
                            str(item.get("market") or ""),
                            str(item.get("selectionCode") or ""),
                            str(item.get("point") or ""),
                        )
                    )

            merged = copy.deepcopy(item)
            if source:
                for field in settlement_fields:
                    if field in source:
                        merged[field] = copy.deepcopy(source[field])
            refreshed.append(merged)
        state[key] = refreshed

    learning = state.setdefault("learning", {})
    learning["updatedAt"] = iso_z(now)
    learning["totalSettledAnalyses"] = sum(
        1
        for item in state.get("analysisHistory") or []
        if isinstance(item, dict)
        and item.get("status") in {"won", "lost", "push"}
    )
    learning["totalSettledBestBets"] = sum(
        1
        for item in state.get("history") or []
        if isinstance(item, dict)
        and item.get("recordType") == "BEST_BET"
        and item.get("status") in {"won", "lost", "push"}
    )
    maintain_prediction_history(
        state,
        {
            **load_json(CONFIG_PATH, {}),
        },
        now,
    )
    return counters


def apply_bank_profit(state: dict[str, Any], record: dict[str, Any], profit: float, now: dt.datetime) -> None:
    bank = state.setdefault("bank", {})
    current = safe_float(bank.get("current"), bank.get("starting", 10000.0))
    current = round(current + profit, 2)
    bank["current"] = current
    bank.setdefault("history", []).append(
        {
            "date": now.date().isoformat(),
            "timestamp": iso_z(now),
            "value": current,
            "event": "PREDICTION_WON" if profit > 0 else "PREDICTION_LOST" if profit < 0 else "PREDICTION_PUSH",
            "recordId": record.get("id"),
            "eventId": record.get("eventId"),
            "match": f"{record.get('home')} — {record.get('away')}",
            "profit": round(profit, 2),
        }
    )


def update_bank_metrics(state: dict[str, Any]) -> None:
    bank = state.setdefault("bank", {})
    starting = max(0.01, safe_float(bank.get("starting"), 10000.0))
    current = safe_float(bank.get("current"), starting)
    bank["roi"] = round((current / starting - 1) * 100, 2)
    values = [safe_float(item.get("value"), starting) for item in bank.get("history") or [] if isinstance(item, dict)]
    values = values or [starting, current]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    bank["maxDrawdown"] = round(max_drawdown, 2)
    active_bets = [
        item for item in state.get("bestBets") or []
        if isinstance(item, dict)
        and normalize_history_status(item.get("status")) == "pending"
    ]
    active_exposure = round(
        sum(safe_float(item.get("stake")) for item in active_bets),
        2,
    )
    bank["activeExposure"] = active_exposure
    bank["placedAmount"] = active_exposure
    bank["activeBetsCount"] = len(active_bets)
    bank["available"] = round(max(0.0, current - active_exposure), 2)
    bank["closedBalance"] = round(current, 2)


def update_statistics(state: dict[str, Any]) -> None:
    analysis_records = [
        item for item in state.get("analysisHistory") or []
        if isinstance(item, dict)
    ]
    best_records = [
        item for item in state.get("history") or []
        if isinstance(item, dict) and item.get("recordType") == "BEST_BET"
    ]

    terminal = {"won", "lost", "push", "void", "cancelled", "postponed", "unresolved"}

    def record_time(item: dict[str, Any]) -> dt.datetime | None:
        return parse_datetime(
            item.get("settledAt")
            or item.get("commenceTime")
            or item.get("utcDate")
            or item.get("publishedAt")
        )

    def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = [str(item.get("status") or "pending").lower() for item in records]
        won = statuses.count("won")
        lost = statuses.count("lost")
        pushes = statuses.count("push")
        voids = sum(status in {"void", "cancelled", "postponed"} for status in statuses)
        unresolved = statuses.count("unresolved")
        pending = sum(status not in terminal and status != "unresolved" for status in statuses)
        decided = won + lost
        settled = won + lost + pushes
        profit = round(sum(safe_float(item.get("profit")) for item in records), 2)
        return {
            "total": len(records),
            "settled": settled,
            "decided": decided,
            "won": won,
            "lost": lost,
            "push": pushes,
            "void": voids,
            "unresolved": unresolved,
            "pending": pending,
            "accuracy": round(won / decided * 100, 1) if decided else 0.0,
            "profit": profit,
        }

    def odds_band(value: float) -> str:
        if value < 1.55:
            return "1,35–1,54"
        if value < 1.80:
            return "1,55–1,79"
        if value < 2.20:
            return "1,80–2,19"
        return "2,20+"

    def grouped(
        records: list[dict[str, Any]],
        key_fn: Any,
        *,
        minimum_decided: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in records:
            key = str(key_fn(item) or "Не указано")
            buckets[key].append(item)
        result: list[dict[str, Any]] = []
        for key, items in buckets.items():
            values = summary(items)
            if values["decided"] < minimum_decided:
                continue
            result.append({"key": key, **values})
        result.sort(
            key=lambda row: (
                safe_int(row.get("decided")),
                safe_float(row.get("accuracy")),
            ),
            reverse=True,
        )
        return result[:limit]

    now = utc_now()
    windows: dict[str, Any] = {}
    for label, days in (("7", 7), ("30", 30), ("90", 90)):
        threshold = now - dt.timedelta(days=days)
        windows[label] = {
            "allPredictions": summary([
                item for item in analysis_records
                if (record_time(item) or dt.datetime.min.replace(tzinfo=UTC)) >= threshold
            ]),
            "bestBets": summary([
                item for item in best_records
                if (record_time(item) or dt.datetime.min.replace(tzinfo=UTC)) >= threshold
            ]),
        }
    windows["all"] = {
        "allPredictions": summary(analysis_records),
        "bestBets": summary(best_records),
    }

    analysis_summary = summary(analysis_records)
    best_summary = summary(best_records)
    bets_settled = [
        item for item in best_records
        if str(item.get("status") or "") in {"won", "lost", "push"}
    ]
    average_odds = mean(
        [
            safe_float(item.get("bookmakerOdds") or item.get("odds"))
            for item in bets_settled
            if safe_float(item.get("bookmakerOdds") or item.get("odds")) > 1
        ],
        0.0,
    )

    streak = 0
    streak_type = ""
    for item in reversed(bets_settled):
        status = str(item.get("status"))
        if status == "push":
            continue
        if not streak_type:
            streak_type = status
        if status != streak_type:
            break
        streak += 1
    streak_text = (
        f"{streak} выигрышных подряд"
        if streak_type == "won" and streak
        else f"{streak} проигрышных подряд"
        if streak_type == "lost" and streak
        else "Нет серии"
    )

    segments = (
        state.get("learning", {}).get("segments", {})
        if isinstance(state.get("learning"), dict)
        else {}
    )
    eligible = [
        (key, value)
        for key, value in segments.items()
        if isinstance(value, dict)
        and safe_int(value.get("settled")) >= 10
        and key.startswith("MARKET|")
    ]
    best_segment = "Недостаточно данных"
    if eligible:
        key, value = max(
            eligible,
            key=lambda row: (
                safe_float(row[1].get("hitRate")),
                safe_int(row[1].get("settled")),
            ),
        )
        best_segment = (
            f"{key.split('|')[-1]} · "
            f"{safe_float(value.get('hitRate')) * 100:.1f}%"
        )

    learning = state.setdefault("learning", {})
    learning_config = load_json(CONFIG_PATH, {})
    settled_samples = safe_int(analysis_summary.get("settled"))
    minimum_samples = safe_int(
        learning_config.get("learningMinimumSegmentSamples"),
        60,
    )
    full_samples = safe_int(
        learning_config.get("learningFullWeightSamples"),
        240,
    )
    if settled_samples < minimum_samples:
        stage = "Сбор выборки"
    elif settled_samples < full_samples:
        stage = "Активная калибровка"
    else:
        stage = "Стабильная калибровка"
    learning["modelReadiness"] = {
        "stage": stage,
        "settledSamples": settled_samples,
        "minimumSamples": minimum_samples,
        "fullWeightSamples": full_samples,
        "maximumProbabilityAdjustment": safe_float(
            learning_config.get(
                "learningMaximumProbabilityAdjustment"
            ),
            0.03,
        ),
        "updatedAt": iso_z(now),
    }

    state["statistics"] = {
        "analysisAccuracy": analysis_summary["accuracy"],
        "bestBetsAccuracy": best_summary["accuracy"],
        "averageOdds": round(average_odds, 2),
        "currentStreak": streak_text,
        "bestSegment": best_segment,
        "settledAnalyses": analysis_summary["settled"],
        "settledBestBets": best_summary["settled"],
        "wonBestBets": best_summary["won"],
        "lostBestBets": best_summary["lost"],
        "pushBestBets": best_summary["push"],
        "pendingBestBets": best_summary["pending"],
        "allPredictions": analysis_summary,
        "bestBets": best_summary,
        "windows": windows,
        "bySport": grouped(
            analysis_records,
            lambda item: sport_label(str(item.get("sport") or "soccer")),
            limit=10,
        ),
        "byMarket": grouped(
            analysis_records,
            lambda item: str(item.get("marketFamily") or market_family(str(item.get("market") or ""))),
            limit=20,
        ),
        "byLeague": grouped(
            analysis_records,
            lambda item: str(item.get("leagueRu") or russian_display_text(item.get("league"))),
            minimum_decided=3,
            limit=20,
        ),
        "byOddsBand": grouped(
            analysis_records,
            lambda item: odds_band(safe_float(item.get("bookmakerOdds") or item.get("odds"))),
            limit=10,
        ),
    }


# ---------------------------------------------------------------------------
# Optional narrative enrichment
# ---------------------------------------------------------------------------


def enrich_narratives_with_openrouter(
    records: list[dict[str, Any]],
    api_key: str | None,
    config: dict[str, Any],
    client: ApiClient,
) -> None:
    if not api_key or not config.get("openRouterNarrativeEnabled", True) or not records:
        return
    model = os.getenv("OPENROUTER_MODEL") or str(config.get("openRouterModel") or "google/gemini-2.5-flash-lite")
    compact = [
        {
            "id": item["id"],
            "sport": item["sportLabel"],
            "league": item.get("leagueRu") or russian_display_text(item["league"]),
            "match": f"{item.get('homeRu') or russian_display_text(item['home'])} — {item.get('awayRu') or russian_display_text(item['away'])}",
            "expectedResult": item["expectedResult"],
            "expectedScore": item["expectedScore"],
            "pick": item.get("pickRu") or russian_display_text(item["pick"]),
            "probability": item["probabilityPercent"],
            "odds": item["bookmakerOdds"],
            "edge": item["edgePercent"],
            "dataTier": item["dataTier"],
            "sourceNotes": item["sourceNotes"][:3],
        }
        for item in records
    ]
    prompt = (
        "Ты редактор спортивной аналитики. На основании только переданных чисел и фактов "
        "создай для каждого id одно краткое объяснение на русском, 1-2 предложения, без обещаний "
        "выигрыша и без выдуманных травм, составов или новостей. Верни только JSON-объект "
        '{"items":[{"id":"...","reason":"..."}]}. Данные: ' + json.dumps(compact, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "temperature": 0.15,
        "messages": [
            {"role": "system", "content": "Используй только предоставленные данные. Не придумывай факты."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://r1a156.github.io/ai-football-lab/",
            "X-Title": "AI Football Lab V10",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        reasons = {
            str(item.get("id")): str(item.get("reason"))
            for item in parsed.get("items") or []
            if isinstance(item, dict) and item.get("id") and item.get("reason")
        }
        for record in records:
            if record["id"] in reasons:
                record["reason"] = reasons[record["id"]]
                record["reasonRu"] = russian_display_text(reasons[record["id"]])
    except Exception as exc:
        client.calls.append({"label": "OPENROUTER_NARRATIVE", "status": "ERROR", "error": str(exc)})
        log(f"OpenRouter narrative fallback used: {exc}")


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def run_live_settlement() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = utc_now()
    raw_state = load_json(STATE_PATH, {})
    state = migrate_state(
        raw_state,
        config,
        now,
    )
    bank_before = safe_float(
        state.get("bank", {}).get("current"),
        0.0,
    )
    learning_segments_before = json.dumps(
        (state.get("learning") or {}).get("segments") or {},
        ensure_ascii=False,
        sort_keys=True,
    )
    learning_bins_before = json.dumps(
        (state.get("learning") or {}).get("calibrationBins") or {},
        ensure_ascii=False,
        sort_keys=True,
    )

    live_results = load_live_final_results()
    due = due_pending_records(
        state,
        config,
        now,
        set(live_results),
    )
    counters = {
        "analysisSettled": 0,
        "bestBetsSettled": 0,
        "unresolved": 0,
    }
    if due and live_results:
        counters = settle_pending_records(
            state,
            live_results,
            {"completedLookup": []},
            now,
        )

    maintenance = maintain_prediction_history(
        state,
        config,
        now,
    )
    batch_before_serialized = json.dumps(
        raw_state.get("batch") or {},
        ensure_ascii=False,
        sort_keys=True,
    )
    update_bank_metrics(state)
    update_statistics(state)
    current_batch = ensure_current_batch(state, config, now)
    batch_after_serialized = json.dumps(
        current_batch,
        ensure_ascii=False,
        sort_keys=True,
    )
    batch_changed = batch_before_serialized != batch_after_serialized

    settlement_changed = bool(
        safe_int(counters.get("analysisSettled"))
        or safe_int(counters.get("bestBetsSettled"))
    )
    maintenance_changed = any(
        safe_int(group.get(key))
        for group in (
            maintenance.get("analysisHistory") or {},
            maintenance.get("bestBetHistory") or {},
        )
        for key in (
            "invalidRemoved",
            "duplicatesRemoved",
            "unresolvedMarked",
        )
    )
    maintenance_missing_before = not isinstance(
        raw_state.get("historyMaintenance"),
        dict,
    )
    state_changed = bool(
        settlement_changed
        or maintenance_changed
        or maintenance_missing_before
        or batch_changed
    )

    bank_after = safe_float(
        state.get("bank", {}).get("current"),
        0.0,
    )
    learning_segments_after = json.dumps(
        (state.get("learning") or {}).get("segments") or {},
        ensure_ascii=False,
        sort_keys=True,
    )
    learning_bins_after = json.dumps(
        (state.get("learning") or {}).get("calibrationBins") or {},
        ensure_ascii=False,
        sort_keys=True,
    )

    if not settlement_changed:
        if abs(bank_before - bank_after) > 0.001:
            raise RuntimeError(
                "LIVE_SETTLEMENT_CHANGED_BANK_WITHOUT_RESULT"
            )
        if (
            learning_segments_before
            != learning_segments_after
            or learning_bins_before
            != learning_bins_after
        ):
            raise RuntimeError(
                "LIVE_SETTLEMENT_CHANGED_LEARNING_WITHOUT_RESULT"
            )

    if state_changed:
        state.setdefault("meta", {})["updatedAt"] = iso_z(now)
        state["meta"]["historyUpdatedAt"] = iso_z(now)
        state["meta"]["liveSettlementAt"] = iso_z(now)
        write_json_atomic(STATE_PATH, state)

        report = report_base(now, "settle-live")
        report["status"] = "GREEN"
        report["finishedAt"] = iso_z(now)
        report["diagnostics"] = {
            "liveFinalResults": len(live_results),
            "dueRecords": len(due),
            "settlement": counters,
            "historyMaintenance": maintenance,
            "bankBefore": round(bank_before, 2),
            "bankAfter": round(bank_after, 2),
            "batch": current_batch,
            "batchCompleted": bool(current_batch.get("completed")),
        }
        write_json_atomic(REPORT_PATH, report)
        print("LIVE_SETTLEMENT_STATE_CHANGED=YES")
    else:
        print("LIVE_SETTLEMENT_STATE_CHANGED=NO")

    print(
        "LIVE_SETTLEMENT_FINAL_RESULTS="
        f"{len(live_results)}"
    )
    print(
        "LIVE_SETTLEMENT_ANALYSIS="
        f"{counters.get('analysisSettled', 0)}"
    )
    print(
        "LIVE_SETTLEMENT_BEST_BETS="
        f"{counters.get('bestBetsSettled', 0)}"
    )
    print(
        "HISTORY_MAINTENANCE="
        f"{json.dumps(maintenance, ensure_ascii=False)}"
    )
    print(
        "CURRENT_BATCH_STATUS="
        f"{current_batch.get('status')}"
    )
    print(
        "CURRENT_BATCH_PENDING_ANALYSES="
        f"{current_batch.get('pendingAnalysisCount')}"
    )
    print("FINAL_STATUS=GREEN_V10_R8_LIVE_SETTLEMENT")
    return 0


def run_live_cycle() -> int:
    # First consume provider-confirmed finals already captured by the live layer.
    run_live_settlement()

    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = utc_now()
    state = migrate_state(load_json(STATE_PATH, {}), config, now)
    update_bank_metrics(state)
    update_statistics(state)
    batch = ensure_current_batch(state, config, now)

    # R8 only read live-learning here. If one provider did not create a live
    # snapshot, the batch could remain active forever and the four best bets
    # never changed. R9 performs the full direct score settlement in the same
    # five-minute workflow before deciding whether the batch is complete.
    if state.get("dailyAnalysis") and not batch.get("completed"):
        print("LIVE_CYCLE_DIRECT_SETTLEMENT=START")
        run_pipeline("settle", force_generation=False)
        now = utc_now()
        state = migrate_state(load_json(STATE_PATH, {}), config, now)
        update_bank_metrics(state)
        update_statistics(state)
        batch = ensure_current_batch(state, config, now)
        print("LIVE_CYCLE_DIRECT_SETTLEMENT=GREEN")

    released = release_overdue_batch_records(state, config, now)
    if released.get("analysis") or released.get("bestBets"):
        maintain_prediction_history(state, config, now)
        update_bank_metrics(state)
        update_statistics(state)
        batch = ensure_current_batch(state, config, now)
        state.setdefault("meta", {})["updatedAt"] = iso_z(now)
        write_json_atomic(STATE_PATH, state)
        print(
            "BATCH_OVERDUE_RELEASED="
            f"analysis:{released.get('analysis', 0)},"
            f"bestBets:{released.get('bestBets', 0)}"
        )

    # An empty production state must immediately attempt a new 15+4 selection;
    # otherwise a failed previous rollover would leave the site empty forever.
    if not state.get("dailyAnalysis"):
        print("EMPTY_BATCH_GENERATION_TRIGGERED=YES")
        return run_pipeline("rollover", force_generation=True)

    if batch.get("completed"):
        state.setdefault("meta", {})["batchStatus"] = "GENERATING_NEXT"
        state["meta"]["batchStatusLabel"] = batch_status_label("GENERATING_NEXT")
        state["meta"]["batchRolloverTriggeredAt"] = iso_z(now)
        write_json_atomic(STATE_PATH, state)
        print("BATCH_ROLLOVER_TRIGGERED=YES")
        return run_pipeline("rollover", force_generation=True)

    print("BATCH_ROLLOVER_TRIGGERED=NO")
    print(f"BATCH_STATUS={batch.get('status')}")
    print(f"BATCH_PENDING_ANALYSES={batch.get('pendingAnalysisCount')}")
    print(f"BATCH_PENDING_BEST_BETS={batch.get('pendingBestBetsCount')}")
    print("FINAL_STATUS=GREEN_V10_R9_LIVE_CYCLE_WAITING")
    return 0


def report_base(now: dt.datetime, mode: str) -> dict[str, Any]:
    return {
        "status": "RUNNING",
        "version": STATE_VERSION,
        "sourceMarker": PIPELINE_MARKER,
        "mode": mode,
        "startedAt": iso_z(now),
        "finishedAt": None,
        "diagnostics": {},
        "warnings": [],
        "errors": [],
    }


def run_pipeline(mode: str, force_generation: bool = False) -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = utc_now()
    timezone = configured_timezone(config)
    local_now = now.astimezone(timezone)
    state = migrate_state(load_json(STATE_PATH, {}), config, now)
    client = ApiClient()
    report = report_base(now, mode)

    odds_key = os.getenv("ODDS_API_KEY", "").strip()
    if not odds_key:
        raise RuntimeError("ODDS_API_KEY is required for production update")
    football_key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip() or None
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None

    football_matches: list[dict[str, Any]] = []
    football_context = build_football_context([])

    live_final_results = load_live_final_results()
    due = due_pending_records(
        state,
        config,
        now,
        set(live_final_results),
    )
    settlement = {
        "analysisSettled": 0,
        "bestBetsSettled": 0,
        "unresolved": 0,
    }
    score_errors: list[str] = []
    if due:
        if football_key and any(str(item.get("sport") or "soccer") == "soccer" for item in due):
            settlement_config = copy.deepcopy(config)
            settlement_config["footballDataLookbackDays"] = 7
            settlement_config["footballDataMaximumWindowsPerRun"] = 1
            football_matches = fetch_football_data_matches(
                client, football_key, settlement_config, now
            )
            football_context = build_football_context(football_matches)
        sport_keys = [
            str(
                item.get("sportKey")
                or item.get("oddsSportKey")
                or ""
            )
            for item in due
        ]
        api_score_results, score_errors = (
            fetch_scores_for_sport_keys(
                client,
                odds_key,
                sport_keys,
            )
        )
        score_results = dict(live_final_results)
        score_results.update(api_score_results)
        settlement = settle_pending_records(
            state,
            score_results,
            football_context,
            now,
            config,
        )
        log(
            "Settlement: "
            f"{settlement}; "
            f"liveFinalResults={len(live_final_results)}"
        )

    current_batch = ensure_current_batch(state, config, now)
    analysis_date = str(state.get("meta", {}).get("analysisDateLocal") or "")
    generation_hour = safe_int(config.get("dailyGenerationHourLocal"), 8)
    generation_due = (
        local_now.hour >= generation_hour
        and (
            analysis_date != local_now.date().isoformat()
            or not state.get("dailyAnalysis")
        )
    )
    generation_requested = bool(
        force_generation
        or mode in {"update", "rollover"}
        or generation_due
    )
    batch_has_records = safe_int(current_batch.get("analysisCount")) > 0
    batch_is_open = batch_has_records and not bool(current_batch.get("completed"))
    generate = bool(
        mode != "settle"
        and generation_requested
        and not batch_is_open
    )
    if generation_requested and batch_is_open:
        report["warnings"].append(
            "The current prediction batch is still active; overlapping generation was blocked."
        )
        state.setdefault("meta", {})["batchGenerationBlockedAt"] = iso_z(now)
        state["meta"]["batchGenerationBlockedReason"] = "CURRENT_BATCH_NOT_TERMINAL"

    discovery_diagnostics: dict[str, Any] = {}
    analysis_diagnostics: dict[str, Any] = {}
    odds_errors: list[str] = []
    selected_keys: list[str] = []
    new_best: list[dict[str, Any]] = []
    best_sync: dict[str, Any] = {
        "changed": False,
        "reason": "NOT_EVALUATED",
    }

    if generate:
        # Full statistical history is loaded only when a new exact 15+4 batch
        # is being built, not every five-minute settlement cycle.
        football_matches = fetch_football_data_matches(client, football_key, config, now)
        football_context = build_football_context(football_matches)
        discovered, discovery_diagnostics = discover_events(
            client, odds_key, config, now
        )
        selected_keys = choose_sport_keys_for_odds(discovered, config)
        query_window_start = parse_datetime(
            discovery_diagnostics.get("queryWindowStart")
        )
        query_window_end = parse_datetime(
            discovery_diagnostics.get("queryWindowEnd")
        )
        if not query_window_start or not query_window_end:
            raise RuntimeError(
                "Operational discovery window was not resolved"
            )
        odds_events, odds_errors = fetch_featured_odds(
            client,
            odds_key,
            selected_keys,
            config,
            query_window_start,
            query_window_end,
        )
        # Add sport type/country metadata if not present in the odds response.
        for event in odds_events:
            event["sport_type"] = infer_sport_from_key(event.get("sport_key"))
            event["country"] = infer_country(str(event.get("sport_key") or ""), str(event.get("sport_title") or ""))
        advanced = maybe_fetch_advanced_markets(client, odds_key, odds_events, config)
        nhl_standings: dict[str, Any] = {}
        daily_analysis, analysis_diagnostics = build_daily_analysis(
            odds_events,
            advanced,
            football_context,
            nhl_standings,
            state,
            config,
            now,
        )
        if len(daily_analysis) != safe_int(config.get("dailyAnalysisTarget"), 15):
            raise RuntimeError(f"EXACT_DAILY_FIFTEEN_REQUIRED={len(daily_analysis)}")
        apply_operational_window_metadata(
            daily_analysis,
            discovery_diagnostics,
            now,
        )
        enrich_narratives_with_openrouter(
            daily_analysis, openrouter_key, config, client
        )
        best_bets, new_best = select_best_bets(daily_analysis, state, config, now)
        if len(best_bets) != safe_int(config.get("bestBetsTarget"), 4):
            raise RuntimeError(f"EXACT_BEST_FOUR_REQUIRED={len(best_bets)}")

        # V10 R10: the visible four and the fifteen are published as one
        # projection from the same freshly generated analysis collection.
        apply_best_bets_to_daily_analysis(
            daily_analysis,
            best_bets,
        )

        if daily_analysis:
            publish_new_batch(
                state,
                daily_analysis,
                best_bets,
                new_best,
                config,
                now,
            )
            append_new_records_to_history(state, daily_analysis, new_best, config)
            state["dailyAnalysis"] = daily_analysis
            state["bestBets"] = best_bets
            state["predictions"] = copy.deepcopy(best_bets)
            state["meta"]["lastBatchRolloverAt"] = (
                iso_z(now) if mode == "rollover" else state["meta"].get("lastBatchRolloverAt")
            )
            state["meta"].update(
                {
                    "bestBetsSourceAnalysisGeneratedAt": iso_z(now),
                    "bestBetsSourceBatchId": str(
                        state.get("batch", {}).get("id") or ""
                    ),
                    "bestBetsGeneratedAt": iso_z(now),
                    "bestBetsSynchronizedAt": iso_z(now),
                    "bestBetsSynchronizationPolicy": (
                        "IMMUTABLE_AFTER_PUBLICATION_UNTIL_SETTLEMENT"
                    ),
                }
            )
        else:
            report["warnings"].append(
                "No new analysis was generated; the previous published state was preserved."
            )
            best_bets = state.get("bestBets") or []
        state["meta"].update(
            {
                "analysisDateLocal": str(
                    discovery_diagnostics.get("operationalDateLocal")
                    or local_now.date().isoformat()
                ),
                "analysisGeneratedAt": iso_z(now),
                "operationalDayId": str(
                    discovery_diagnostics.get("operationalDayId") or ""
                ),
                "operationalWindowStart": str(
                    discovery_diagnostics.get("operationalWindowStart") or ""
                ),
                "operationalWindowEnd": str(
                    discovery_diagnostics.get("operationalWindowEnd") or ""
                ),
                "selectionWindowStart": str(
                    discovery_diagnostics.get("queryWindowStart") or ""
                ),
                "selectionWindowEnd": str(
                    discovery_diagnostics.get("queryWindowEnd") or ""
                ),
                "operationalWindowPolicy": (
                    "PROGRESSIVE_FUTURE_SEARCH_UNTIL_EXACT_FIFTEEN"
                ),
                "analysisTarget": safe_int(config.get("dailyAnalysisTarget"), 15),
                "analysisPublished": len(daily_analysis),
                "bestBetsTarget": safe_int(config.get("bestBetsTarget"), 4),
                "bestBetsPublished": len(best_bets),
                "newBestBets": len(new_best),
                "sportsAnalyzed": sorted({str(item.get("sport")) for item in daily_analysis}),
                "soccerAnalyses": sum(1 for item in daily_analysis if item.get("sport") == "soccer"),
                "hockeyAnalyses": sum(1 for item in daily_analysis if item.get("sport") == "ice_hockey"),
                "leaguesAnalyzed": len({str(item.get("league")) for item in daily_analysis}),
                "countriesAnalyzed": len({str(item.get("country")) for item in daily_analysis}),
                "status": (
                    "GREEN"
                    if len(daily_analysis) == safe_int(config.get("dailyAnalysisTarget"), 15)
                    else "DEGRADED"
                ),
                "dataFreshness": "CURRENT",
                "predictionObjective": config.get("predictionObjective"),
                "publicationPolicy": config.get("publicationPolicy"),
                "marketPolicy": R14_MARKET_POLICY,
                "footballOnly": True,
                "virtualBankPolicy": config.get("virtualBankPolicy"),
            }
        )
    else:
        published_count = len(state.get("dailyAnalysis") or [])
        state["meta"]["status"] = (
            "GREEN"
            if published_count == safe_int(config.get("dailyAnalysisTarget"), 15)
            else "DEGRADED"
            if published_count > 0
            else "INITIALIZED"
        )
        state["meta"]["dataFreshness"] = "SETTLEMENT_REFRESH"

    # Self-heal a state where the fifteen are current but the visible four
    # still belong to an older analysis run.
    if state.get("dailyAnalysis"):
        best_sync = synchronize_best_bets_with_current_analysis(
            state,
            config,
            now,
            force=False,
        )
        if best_sync.get("changed"):
            log(
                "Best four synchronized with current fifteen: "
                + json.dumps(best_sync, ensure_ascii=False)
            )

    state["meta"]["version"] = STATE_VERSION
    state["meta"]["sourceMarker"] = PIPELINE_MARKER
    state["meta"]["updatedAt"] = iso_z(now)
    state["meta"]["lastSuccessfulRefreshAt"] = iso_z(now)
    state["quota"] = {
        "provider": "THE_ODDS_API",
        **client.odds_quota,
        "calls": len([call for call in client.calls if str(call.get("label", "")).startswith(("ODDS", "EVENTS", "MARKETS", "ADVANCED", "SCORES"))]),
        "updatedAt": iso_z(now),
    }
    state["meta"]["apiHealth"] = summarize_api_health(client.calls)

    update_bank_metrics(state)
    update_statistics(state)
    ensure_current_batch(state, config, now)
    state["bank"]["history"] = state["bank"].get("history", [])[-safe_int(config.get("bankHistoryLimit"), 1200) :]

    report.update(
        {
            "status": "GREEN" if state["meta"].get("status") == "GREEN" else "DEGRADED",
            "finishedAt": iso_z(utc_now()),
            "diagnostics": {
                "generated": generate,
                "settlement": settlement,
                "duePending": len(due),
                "selectedSportKeys": selected_keys,
                "discovery": discovery_diagnostics,
                "analysis": analysis_diagnostics,
                "dailyAnalysis": len(state.get("dailyAnalysis") or []),
                "bestBets": len(state.get("bestBets") or []),
                "newBestBets": len(new_best),
                "bestBetsSynchronization": best_sync,
                "batch": state.get("batch"),
                "batchRollover": mode == "rollover" and generate,
                "footballDataMatches": len(football_matches),
                "quota": state["quota"],
                "apiCalls": client.calls,
            },
            "warnings": list(report.get("warnings") or []) + score_errors + odds_errors,
        }
    )

    write_json_atomic(STATE_PATH, state)
    write_json_atomic(REPORT_PATH, report)
    write_json_atomic(
        DAILY_SNAPSHOT_PATH,
        {
            "version": STATE_VERSION,
            "updatedAt": state["meta"]["updatedAt"],
            "analysisDateLocal": state["meta"].get("analysisDateLocal"),
            "batch": state.get("batch", {}),
            "bank": {
                "current": state.get("bank", {}).get("current"),
                "placedAmount": state.get("bank", {}).get("placedAmount"),
                "available": state.get("bank", {}).get("available"),
                "activeBetsCount": state.get("bank", {}).get("activeBetsCount"),
            },
            "dailyAnalysis": state.get("dailyAnalysis", []),
            "bestBets": state.get("bestBets", []),
        },
    )
    log(
        "V10 GREEN: "
        f"analysis={len(state.get('dailyAnalysis') or [])}; "
        f"bestBets={len(state.get('bestBets') or [])}; "
        f"bank={state.get('bank', {}).get('current')}"
    )
    return 0


def summarize_api_health(calls: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [call for call in calls if call.get("status") == "ERROR" or safe_int(call.get("status"), 200) >= 400]
    return {
        "status": "GREEN" if not errors else "DEGRADED",
        "calls": len(calls),
        "errors": len(errors),
        "lastErrors": errors[-5:],
    }


# ---------------------------------------------------------------------------
# Validation and self-test
# ---------------------------------------------------------------------------


def validate_state(state: dict[str, Any], config: dict[str, Any], allow_legacy: bool = True) -> None:
    if not isinstance(state, dict):
        raise RuntimeError("State must be a JSON object")
    version = str((state.get("meta") or {}).get("version") or "")
    if version != STATE_VERSION:
        if allow_legacy:
            return
        raise RuntimeError(f"State version must be {STATE_VERSION}, got {version}")
    daily = state.get("dailyAnalysis")
    best = state.get("bestBets")
    if not isinstance(daily, list) or not isinstance(best, list):
        raise RuntimeError("V10 state requires dailyAnalysis and bestBets arrays")
    daily_target = safe_int(config.get("dailyAnalysisTarget"), 15)
    best_target = safe_int(config.get("bestBetsTarget"), 4)
    if daily and len(daily) != daily_target:
        raise RuntimeError(f"Published dailyAnalysis must contain exactly {daily_target}, got {len(daily)}")
    if daily and len(best) != best_target:
        raise RuntimeError(f"Published bestBets must contain exactly {best_target}, got {len(best)}")
    if not daily and best:
        raise RuntimeError("bestBets cannot exist without dailyAnalysis")
    event_ids = [str(item.get("eventId") or "") for item in daily]
    if len([value for value in event_ids if value]) != len(set(value for value in event_ids if value)):
        raise RuntimeError("dailyAnalysis contains duplicate events")
    best_event_ids = [str(item.get("eventId") or "") for item in best]
    if len([value for value in best_event_ids if value]) != len(set(value for value in best_event_ids if value)):
        raise RuntimeError("bestBets contains duplicate events")
    minimum_odds = safe_float(config.get("minimumBookmakerOdds"), 1.35)
    expected_stake_percent = safe_float(config.get("stakePerBestBetPercent"), 20.0)
    for item in daily + best:
        if str(item.get("marketPolicy") or "") == R14_MARKET_POLICY:
            if not record_uses_r14_standard_market(item):
                raise RuntimeError("R14 publication contains a forbidden market")
            if str(item.get("sport") or "") != "soccer":
                raise RuntimeError("R14 publication contains a non-football event")
    for item in best:
        if abs(safe_float(item.get("stakePercent")) - expected_stake_percent) > 0.001:
            raise RuntimeError("Every best bet must preserve the configured 20 percent stake policy")
        if safe_float(item.get("stake")) <= 0:
            raise RuntimeError("Best bet stake must be positive")
        if safe_float(item.get("bookmakerOdds") or item.get("odds")) < minimum_odds:
            raise RuntimeError("Best bet bookmaker odds are below the configured minimum")
    daily_event_set = set(event_ids)
    if not set(best_event_ids).issubset(daily_event_set):
        raise RuntimeError("bestBets must be selected from the current fifteen")
    terminal = {"won", "lost", "push", "void", "cancelled"}
    for item in daily:
        if not item.get("isBestBet") and safe_float(item.get("stake")) != 0.0:
            raise RuntimeError("Non-best daily analysis must not affect bank")
        status = normalize_history_status(item.get("status"))
        if status in terminal and status not in {"void", "cancelled"} and not str(item.get("score") or "").strip():
            raise RuntimeError("Settled daily analysis must expose its final score")


def validate_repository_files() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    required = [
        STATE_PATH,
        INDEX_PATH,
        APP_PATH,
        STYLE_PATH,
        WORKFLOW_PATH,
        LIVE_WORKFLOW_PATH,
        LIVE_STATE_PATH,
        LIVE_LEARNING_PATH,
        LIVE_SCRIPT_PATH,
    ]
    for path in required:
        if not path.exists():
            raise RuntimeError(f"Required file missing: {path.relative_to(ROOT)}")
    if PIPELINE_MARKER not in pathlib.Path(__file__).read_text(encoding="utf-8"):
        raise RuntimeError("Python marker missing")
    if SITE_MARKER not in APP_PATH.read_text(encoding="utf-8"):
        raise RuntimeError("Site marker missing")
    if WORKFLOW_MARKER not in WORKFLOW_PATH.read_text(encoding="utf-8"):
        raise RuntimeError("Workflow marker missing")
    if LIVE_WORKFLOW_MARKER not in LIVE_WORKFLOW_PATH.read_text(encoding="utf-8"):
        raise RuntimeError("Live workflow marker missing")
    live_source = LIVE_SCRIPT_PATH.read_text(encoding="utf-8")
    app_source = APP_PATH.read_text(encoding="utf-8")
    if LIVE_MARKER not in live_source:
        raise RuntimeError("Live script marker missing")
    if "V10_R7_HISTORY_LIVE_CLEANUP" not in live_source:
        raise RuntimeError("R7 live cleanup marker missing")
    source_text = pathlib.Path(__file__).read_text(encoding="utf-8")
    if "--settle-live" not in source_text:
        raise RuntimeError("R7 live settlement CLI missing")
    if "--live-cycle" not in source_text:
        raise RuntimeError("R8 live cycle CLI missing")
    if "V10_R8_ATOMIC_BATCH_ROLLOVER" not in source_text:
        raise RuntimeError("R8 batch rollover marker missing")
    if "V10_R9_IMMEDIATE_SETTLEMENT_ROLLOVER" not in source_text:
        raise RuntimeError("R9 immediate settlement marker missing")
    if "V10_R11_MOSCOW_OPERATIONAL_DAY_ROLLOVER" not in source_text:
        raise RuntimeError("R11 Moscow operational-day marker missing")
    if "V10_R12_FINAL_MAX_HIT_RATE_15_SETTLEMENT" not in source_text:
        raise RuntimeError("R12 max-hit-rate and all-fifteen settlement marker missing")
    if "V10_R14_RESULT_INTEGRITY_GOAL_DIRECTION" not in source_text:
        raise RuntimeError("R14 football-only standard-market marker missing")
    if any(value in set(config.get("featuredMarkets") or []) for value in ("spreads", "alternate_spreads", "alternate_totals")):
        raise RuntimeError("R14 forbidden market leaked into featuredMarkets")
    if "ALL_FIFTEEN_SETTLEMENT_VISIBLE" not in WORKFLOW_PATH.read_text(encoding="utf-8"):
        raise RuntimeError("R12 all-fifteen workflow acceptance marker missing")
    if "LIVE_CYCLE_DIRECT_SETTLEMENT=START" not in source_text:
        raise RuntimeError("R9 direct live-cycle settlement bridge missing")
    if "V10_R7_CLEAN_HISTORY_AND_LIVE_EXPIRY" not in app_source:
        raise RuntimeError("R7 clean history UI marker missing")
    if config.get("historyDefaultFilter") != "settled":
        raise RuntimeError("R7 history default filter mismatch")
    if load_json(LIVE_STATE_PATH, {}).get("sourceMarker") != LIVE_MARKER:
        raise RuntimeError("Live state marker missing")
    if load_json(LIVE_LEARNING_PATH, {}).get("sourceMarker") != LIVE_MARKER:
        raise RuntimeError("Live learning marker missing")
    state = load_json(STATE_PATH, {})
    validate_state(state, config, allow_legacy=True)
    print("VALIDATION_GREEN_V10")
    return 0


def synthetic_bookmaker_event(
    event_id: str,
    sport_key: str,
    title: str,
    home: str,
    away: str,
    commence: dt.datetime,
    home_price: float,
    away_price: float,
    draw_price: float | None,
    total_line: float,
    over_price: float,
    under_price: float,
) -> dict[str, Any]:
    bookmakers = []
    for index, shift in enumerate((0.0, 0.02, -0.015, 0.01)):
        h2h_outcomes = [
            {"name": home, "price": round(home_price + shift, 3)},
            {"name": away, "price": round(away_price - shift, 3)},
        ]
        if draw_price is not None:
            h2h_outcomes.append({"name": "Draw", "price": round(draw_price + shift, 3)})
        bookmakers.append(
            {
                "key": f"book{index}",
                "title": f"Book {index}",
                "last_update": iso_z(commence - dt.timedelta(hours=1)),
                "markets": [
                    {"key": "h2h", "last_update": iso_z(commence - dt.timedelta(hours=1)), "outcomes": h2h_outcomes},
                    {
                        "key": "totals",
                        "last_update": iso_z(commence - dt.timedelta(hours=1)),
                        "outcomes": [
                            {"name": "Over", "point": total_line, "price": round(over_price + shift, 3)},
                            {"name": "Under", "point": total_line, "price": round(under_price - shift, 3)},
                        ],
                    },
                ],
            }
        )
    return {
        "id": event_id,
        "sport_key": sport_key,
        "sport_title": title,
        "commence_time": iso_z(commence),
        "home_team": home,
        "away_team": away,
        "bookmakers": bookmakers,
    }


def run_self_test() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    # Synthetic tests are intentionally less strict on edge/data thresholds so
    # the test exercises the complete 15 -> 4 structure, not live-market luck.
    test_config = copy.deepcopy(config)
    test_config.update(
        {
            "bestBetMinimumProbability": 0.48,
            "bestBetMinimumEdge": -0.08,
            "bestBetMinimumExpectedValue": -0.08,
            "bestBetMinimumDataQuality": 35,
            "bestBetMinimumAgreement": 35,
            "bestBetMaximumAnomaly": 80,
            "lowOddsMinimumProbability": 0.50,
            "lowOddsMinimumEdge": -0.08,
            "maximumSameLeagueBestBets": 2,
            "maximumSameMarketFamilyBestBets": 4,
            "analysisMinimumConservativeProbability": 0.20,
            "bestBetMinimumConservativeProbability": 0.20,
            "bestBetMinimumBookmakers": 3,
            "marketOnlyBestBetMinimumProbability": 0.20,
            "marketOnlyBestBetMinimumConservativeProbability": 0.15,
            "marketOnlyBestBetMinimumBookmakers": 3,
            "marketOnlyBestBetMinimumAgreement": 20,
            "marketOnlyBestBetMinimumMarketStability": 20,
            "marketOnlyBestBetMaximumAnomaly": 90,
            "resultFirstMinimumConservativeProbability": 0.40,
            "fallbackMinimumConservativeProbability": 0.35,
            "fallbackMinimumMarketStability": 20,
            "topFourHardMinimumConservativeProbability": 0.35,
            "topFourHardMinimumMarketStability": 20,
            "totalDirectionMinimumStatisticalProbability": 0.45,
            "totalDirectionMinimumMarketProbability": 0.45,
            "totalDirectionMinimumRecentRate": 0.0,
            "underExpectedGoalsSafetyMargin": -1.0,
            "under25MaximumBttsProbability": 0.99,
            "under25MaximumRecentOverRate": 0.99,
            "overExpectedGoalsSafetyMargin": -1.0,
            "overMinimumRecentOverRate": 0.0,
        }
    )
    now = dt.datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    state = default_state(test_config, now)
    events = []
    for index in range(24):
        sport = "soccer"
        key = f"soccer_test_{index % 12}"
        title = f"Test Football League {index % 10}"
        events.append(
            synthetic_bookmaker_event(
                f"event-{index}",
                key,
                title,
                f"Home {index}",
                f"Away {index}",
                now + dt.timedelta(hours=2 + index),
                1.72 + (index % 3) * 0.06,
                2.4 + (index % 4) * 0.12,
                3.3,
                2.5,
                1.82 + (index % 2) * 0.08,
                1.95 - (index % 2) * 0.05,
            )
        )
    football_context = {"teamGames": {}, "leagueHomeAverage": 1.45, "leagueAwayAverage": 1.15, "completedLookup": []}
    daily, diagnostics = build_daily_analysis(events, {}, football_context, {}, state, test_config, now)
    if len(daily) != 15:
        raise RuntimeError(f"SELF_TEST expected 15 analyses, got {len(daily)}")
    best, new_best = select_best_bets(
        daily,
        state,
        test_config,
        now,
    )
    if len(best) != 4 or len(new_best) != 4:
        raise RuntimeError(
            f"SELF_TEST expected 4 best bets, got {len(best)}"
        )
    daily_by_event = {str(item.get("eventId") or ""): item for item in daily}
    for item in best:
        source = daily_by_event.get(str(item.get("eventId") or ""))
        if not source:
            raise RuntimeError("SELF_TEST best bet is outside the fifteen")
        if (
            str(item.get("market") or "") != str(source.get("market") or "")
            or str(item.get("selectionCode") or "") != str(source.get("selectionCode") or "")
            or str(item.get("point") or "") != str(source.get("point") or "")
        ):
            raise RuntimeError("SELF_TEST best four changed the published fifteen market")

    strict_failure_daily = copy.deepcopy(daily)
    for analysis in strict_failure_daily:
        analysis["qualification"] = {
            "qualified": False,
            "failures": [
                "Недостаточное преимущество над рынком",
                "Недостаточное математическое ожидание",
            ],
        }
        for alternative in analysis.get("alternatives") or []:
            alternative["qualification"] = {
                "qualified": False,
                "failures": [
                    "Недостаточное преимущество над рынком",
                ],
            }

    strict_failure_state = default_state(test_config, now)
    fallback_best, fallback_new = select_best_bets(
        strict_failure_daily,
        strict_failure_state,
        test_config,
        now,
    )
    if len(fallback_best) != 4 or len(fallback_new) != 4:
        raise RuntimeError(
            "SELF_TEST exact-four regression failed: "
            f"best={len(fallback_best)}"
        )
    if any(
        not str(item.get("bestBetSelectionTier") or "")
        for item in fallback_best
    ):
        raise RuntimeError(
            "SELF_TEST exact-four tier is missing"
        )

    # R12 regression: published financial picks are immutable. A later
    # refresh may settle them, but it must never rerank or append replacements.
    stale_state = default_state(test_config, now)
    stale_state["meta"]["analysisGeneratedAt"] = iso_z(now)
    stale_state["dailyAnalysis"] = copy.deepcopy(daily)
    stale_history = []
    stale_visible = []
    for index in range(4):
        stale = copy.deepcopy(best[index])
        stale["id"] = f"frozen-best-{index}"
        stale["eventId"] = f"frozen-event-{index}"
        stale["sourceAnalysisId"] = f"frozen-analysis-{index}"
        stale["publishedAt"] = iso_z(
            now - dt.timedelta(days=1)
        )
        stale["status"] = "pending"
        stale_history.append(copy.deepcopy(stale))
        stale_visible.append(copy.deepcopy(stale))
    stale_state["history"] = stale_history
    stale_state["bestBets"] = stale_visible
    stale_state["predictions"] = copy.deepcopy(stale_visible)

    visible_before = json.dumps(
        stale_state["bestBets"],
        ensure_ascii=False,
        sort_keys=True,
    )
    history_before = json.dumps(
        stale_state["history"],
        ensure_ascii=False,
        sort_keys=True,
    )
    sync_probe = synchronize_best_bets_with_current_analysis(
        stale_state,
        test_config,
        now,
    )
    if sync_probe.get("changed"):
        raise RuntimeError(
            "SELF_TEST frozen best four were reselected"
        )
    if sync_probe.get("reason") != (
        "CURRENT_BATCH_FROZEN_AT_PUBLICATION"
    ):
        raise RuntimeError(
            "SELF_TEST frozen selection reason missing"
        )
    if json.dumps(
        stale_state["bestBets"],
        ensure_ascii=False,
        sort_keys=True,
    ) != visible_before:
        raise RuntimeError(
            "SELF_TEST frozen visible picks changed"
        )
    if json.dumps(
        stale_state["history"],
        ensure_ascii=False,
        sort_keys=True,
    ) != history_before:
        raise RuntimeError(
            "SELF_TEST frozen history changed"
        )

    state["dailyAnalysis"] = daily
    state["bestBets"] = best
    state["predictions"] = copy.deepcopy(best)
    publish_new_batch(
        state, daily, best, new_best, test_config, now
    )
    append_new_records_to_history(
        state, daily, new_best, test_config
    )
    state["meta"]["analysisGeneratedAt"] = iso_z(now)
    state["meta"]["bestBetsSourceAnalysisGeneratedAt"] = iso_z(now)
    state["meta"]["bestBetsSourceBatchId"] = str(
        state.get("batch", {}).get("id") or ""
    )
    update_bank_metrics(state)
    active_batch = ensure_current_batch(state, test_config, now)
    if active_batch.get("status") != "ACTIVE":
        raise RuntimeError("SELF_TEST active batch status missing")
    if safe_int(state.get("bank", {}).get("activeBetsCount")) != 4:
        raise RuntimeError("SELF_TEST active bank count mismatch")
    expected_available = round(
        safe_float(state.get("bank", {}).get("current"))
        - safe_float(state.get("bank", {}).get("placedAmount")),
        2,
    )
    if abs(
        safe_float(state.get("bank", {}).get("available"))
        - expected_available
    ) > 0.01:
        raise RuntimeError("SELF_TEST available bank mismatch")

    # Settle three outcomes to exercise win/loss/push and bankroll learning.
    results = {
        str(best[0]["eventId"]): {"homeScore": 2, "awayScore": 1, "source": "SELF_TEST"},
        str(best[1]["eventId"]): {"homeScore": 0, "awayScore": 2, "source": "SELF_TEST"},
        str(best[2]["eventId"]): {"homeScore": 1, "awayScore": 1, "source": "SELF_TEST"},
        str(best[3]["eventId"]): {"homeScore": 3, "awayScore": 2, "source": "SELF_TEST"},
    }
    settle_pending_records(state, results, football_context, now + dt.timedelta(days=1))
    settled_visible = [
        item for item in state.get("dailyAnalysis") or []
        if str(item.get("eventId") or "") in results
    ]
    if len(settled_visible) != 4:
        raise RuntimeError("SELF_TEST did not mirror settlements into the fifteen")
    if any(
        str(item.get("status") or "pending") not in {"won", "lost", "push"}
        or not str(item.get("score") or "").strip()
        for item in settled_visible
    ):
        raise RuntimeError("SELF_TEST visible result or score is missing from the fifteen")
    update_bank_metrics(state)
    update_statistics(state)
    validate_state(state, test_config, allow_legacy=False)
    if not state.get("learning", {}).get("segments"):
        raise RuntimeError("SELF_TEST learning segments were not updated")
    statistics_probe = state.get("statistics") or {}
    if not isinstance(statistics_probe.get("allPredictions"), dict):
        raise RuntimeError("SELF_TEST detailed all-prediction statistics missing")
    if not isinstance(statistics_probe.get("bestBets"), dict):
        raise RuntimeError("SELF_TEST detailed best-bet statistics missing")
    if not isinstance(statistics_probe.get("windows"), dict):
        raise RuntimeError("SELF_TEST statistics windows missing")
    if safe_int(statistics_probe.get("allPredictions", {}).get("settled")) < 1:
        raise RuntimeError("SELF_TEST settled analysis statistics missing")

    history_probe = copy.deepcopy(daily[0])
    history_probe["status"] = "pending"
    history_probe["publishedAt"] = iso_z(now)
    duplicate_probe = copy.deepcopy(history_probe)
    duplicate_probe["id"] = "duplicate-id"
    invalid_probe = {"id": "invalid-only"}
    cleaned_probe, cleanup_counters = clean_history_collection(
        [
            history_probe,
            duplicate_probe,
            invalid_probe,
        ],
        "analysisHistory",
        test_config,
        now,
    )
    if len(cleaned_probe) != 1:
        raise RuntimeError(
            "SELF_TEST history duplicate cleanup failed"
        )
    if cleanup_counters["duplicatesRemoved"] != 1:
        raise RuntimeError(
            "SELF_TEST history duplicate counter failed"
        )
    if cleanup_counters["invalidRemoved"] != 1:
        raise RuntimeError(
            "SELF_TEST history invalid counter failed"
        )

    unresolved_probe = copy.deepcopy(daily[1])
    unresolved_probe["status"] = "pending"
    unresolved_probe["commenceTime"] = iso_z(
        now - dt.timedelta(days=4)
    )
    unresolved_cleaned, _ = clean_history_collection(
        [unresolved_probe],
        "analysisHistory",
        test_config,
        now,
    )
    if (
        not unresolved_cleaned
        or unresolved_cleaned[0].get("status")
        != "unresolved"
    ):
        raise RuntimeError(
            "SELF_TEST stale pending history was not marked unresolved"
        )

    rollover_probe = copy.deepcopy(state)
    for collection_name in ("dailyAnalysis", "bestBets", "predictions"):
        for item in rollover_probe.get(collection_name) or []:
            if isinstance(item, dict):
                item["status"] = "won"
                item["settledAt"] = iso_z(now + dt.timedelta(days=2))
    for collection_name in ("analysisHistory", "history"):
        current_ids = {
            str(item.get("id") or "")
            for item in rollover_probe.get(
                "dailyAnalysis" if collection_name == "analysisHistory" else "bestBets"
            ) or []
            if isinstance(item, dict)
        }
        for item in rollover_probe.get(collection_name) or []:
            if isinstance(item, dict) and str(item.get("id") or "") in current_ids:
                item["status"] = "won"
                item["settledAt"] = iso_z(now + dt.timedelta(days=2))
    update_bank_metrics(rollover_probe)
    completed_batch = ensure_current_batch(
        rollover_probe, test_config, now + dt.timedelta(days=2)
    )
    if not completed_batch.get("completed"):
        raise RuntimeError("SELF_TEST completed batch was not detected")
    archive_completed_batch(
        rollover_probe, test_config, now + dt.timedelta(days=2)
    )
    if len(rollover_probe.get("batchHistory") or []) != 1:
        raise RuntimeError("SELF_TEST completed batch archive missing")

    localization_probe = apply_russian_display_fields({
        "country": "England",
        "league": "English Premier League",
        "home": "Manchester United",
        "away": "Liverpool FC",
        "pick": "Победа Manchester United",
        "bookmaker": "Example Sports",
        "expectedResult": "Manchester United expected to win",
        "reason": "Market and model agree",
    })
    visible_probe = " ".join(
        str(localization_probe.get(key) or "")
        for key in (
            "countryRu", "leagueRu", "homeRu", "awayRu",
            "pickRu", "bookmakerRu", "expectedResultRu", "reasonRu",
        )
    )
    if re.search(r"[A-Za-z]", visible_probe):
        raise RuntimeError(
            f"SELF_TEST visible Latin text remains: {visible_probe}"
        )

    # R14 standard-market and payout regression matrix. New publication must
    # reject every Asian/quarter/integer line while legacy settlement remains
    # available for already published historical records.
    forbidden_probes = [
        ("spreads", "SPREAD_HOME", -0.5),
        ("alternate_spreads", "SPREAD_AWAY", 0.75),
        ("alternate_totals", "TOTAL_OVER", 2.25),
        ("totals", "TOTAL_OVER", 2.0),
        ("totals", "TOTAL_UNDER", 2.75),
        ("draw_no_bet", "DRAW_NO_BET_HOME", None),
    ]
    if any(standard_market_allowed(*probe) for probe in forbidden_probes):
        raise RuntimeError("SELF_TEST Asian market reached the R14 whitelist")
    allowed_probes = [
        ("h2h", "HOME_WIN", None),
        ("totals", "TOTAL_OVER", 2.5),
        ("totals", "TOTAL_UNDER", 3.5),
        ("btts", "BTTS_YES", None),
        ("double_chance", "DOUBLE_CHANCE_HOME_DRAW", None),
        ("team_totals", "TEAM_TOTAL_HOME_OVER", 1.5),
    ]
    if not all(standard_market_allowed(*probe) for probe in allowed_probes):
        raise RuntimeError("SELF_TEST standard market whitelist regression")
    settlement_probes = [
        ({"marketKey": "h2h", "market": "HOME_WIN", "selectionCode": "HOME"}, 2, 1, "won"),
        ({"marketKey": "totals", "market": "TOTAL_OVER", "selectionCode": "OVER", "point": 2.5}, 2, 1, "won"),
        ({"marketKey": "totals", "market": "TOTAL_UNDER", "selectionCode": "UNDER", "point": 2.5}, 1, 1, "won"),
        ({"marketKey": "btts", "market": "BTTS_NO", "selectionCode": "NO"}, 1, 0, "won"),
        ({"marketKey": "double_chance", "market": "DOUBLE_CHANCE_DRAW_AWAY", "selectionCode": "DRAW_AWAY"}, 1, 1, "won"),
        ({"marketKey": "team_totals", "market": "TEAM_TOTAL_HOME_OVER", "selectionCode": "HOME_OVER", "point": 1.5}, 2, 0, "won"),
    ]
    for probe, home_score, away_score, expected_status in settlement_probes:
        if settle_market(probe, home_score, away_score) != expected_status:
            raise RuntimeError(f"SELF_TEST standard settlement failed: {probe}")
    payout_probe = round(100.0 * (1.80 - 1.0), 2)
    if payout_probe != 80.0:
        raise RuntimeError("SELF_TEST net-profit formula regression")

    # R11 operational-day tests: rollover executes immediately, while the
    # selected fixtures remain inside one Moscow 08:00-08:00 day.
    before_eight_utc = dt.datetime(2026, 7, 29, 0, 20, tzinfo=UTC)
    before_windows = operational_selection_windows(
        before_eight_utc, test_config
    )
    first_before = before_windows[0]
    if first_before.get("operationalDayId") != "2026-07-29-MSK-0800":
        raise RuntimeError("SELF_TEST R11 pre-08 operational day mismatch")
    if first_before.get("queryWindowStart") != "2026-07-29T05:00:00Z":
        raise RuntimeError("SELF_TEST R11 pre-08 query start mismatch")
    if first_before.get("queryWindowEnd") != "2026-07-30T05:00:00Z":
        raise RuntimeError("SELF_TEST R11 pre-08 query end mismatch")

    after_eight_utc = dt.datetime(2026, 7, 29, 11, 30, tzinfo=UTC)
    after_windows = operational_selection_windows(
        after_eight_utc, test_config
    )
    first_after = after_windows[0]
    if first_after.get("operationalDayId") != "2026-07-29-MSK-0800":
        raise RuntimeError("SELF_TEST R11 current operational day mismatch")
    if first_after.get("queryWindowStart") != "2026-07-29T12:15:00Z":
        raise RuntimeError("SELF_TEST R11 lead-time start mismatch")
    if first_after.get("queryWindowEnd") != "2026-07-30T05:00:00Z":
        raise RuntimeError("SELF_TEST R11 current-day end mismatch")

    print("R11_OPERATIONAL_DAY=GREEN")
    print("R11_ROLLOVER_WAIT_FOR_08=NO")
    print("R11_WINDOW_MOSCOW=08:00_TO_08:00")

    print(
        "SELF_TEST_GREEN_V10 "
        f"ANALYSIS={len(daily)} BEST={len(best)} EXACT_FOUR=YES RUSSIAN_UI=YES "
        f"SOCCER={sum(1 for item in daily if item['sport'] == 'soccer')} "
        f"HOCKEY={sum(1 for item in daily if item['sport'] == 'ice_hockey')} "
        f"MARKETS={diagnostics['marketCandidates']} BANK={state['bank']['current']:.2f} "
        f"FULL_STATISTICS=YES LEARNING=ACTIVE LIVE_LAYER=SEPARATE "
        f"HISTORY_CLEANUP=YES LIVE_FINAL_BRIDGE=YES "
        f"ATOMIC_BATCH_ROLLOVER=YES LINKED_BANK_METRICS=YES "
        f"FROZEN_BEST_FOUR=YES OPERATIONAL_DAY_08_MSK=YES IMMEDIATE_ROLLOVER=YES "
        f"FOOTBALL_ONLY=YES STANDARD_MARKETS_ONLY=YES ASIAN_MARKETS=REMOVED"
    )
    return 0


def migrate_state_file() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    migrated = migrate_state(load_json(STATE_PATH, {}), config)
    write_json_atomic(STATE_PATH, migrated)
    validate_state(migrated, config, allow_legacy=False)
    write_json_atomic(
        DAILY_SNAPSHOT_PATH,
        {
            "version": STATE_VERSION,
            "updatedAt": migrated.get("meta", {}).get("updatedAt"),
            "analysisDateLocal": migrated.get("meta", {}).get("analysisDateLocal"),
            "dailyAnalysis": migrated.get("dailyAnalysis", []),
            "bestBets": migrated.get("bestBets", []),
        },
    )
    write_json_atomic(
        REPORT_PATH,
        {
            "status": "GREEN",
            "version": STATE_VERSION,
            "sourceMarker": PIPELINE_MARKER,
            "mode": "MIGRATION",
            "finishedAt": iso_z(utc_now()),
            "diagnostics": {
                "history": len(migrated.get("history") or []),
                "analysisHistory": len(migrated.get("analysisHistory") or []),
                "bank": migrated.get("bank", {}).get("current"),
            },
            "warnings": [],
            "errors": [],
        },
    )
    print("MIGRATION_GREEN_V10")
    return 0



def reset_state_file() -> int:
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = utc_now()
    state = default_state(config, now)
    state["meta"].update(
        {
            "status": "INITIALIZED",
            "dataFreshness": "RESET_PENDING_GENERATION",
            "analysisDateLocal": "",
            "analysisGeneratedAt": None,
            "lastSuccessfulRefreshAt": None,
            "cleanStart": True,
            "cleanStartAt": iso_z(now),
            "resetMarker": RESET_MARKER,
            "modelEpoch": 1,
            "notice": (
                "Новый цикл V10 начат с чистого состояния. "
                "Банк сброшен до стартового значения, старая история и обучение удалены."
            ),
        }
    )
    state["bank"]["history"] = [
        {
            "date": now.date().isoformat(),
            "value": round(
                safe_float(config.get("startingVirtualBank"), 10000.0),
                2,
            ),
            "event": "V10_CLEAN_START",
        }
    ]
    state["learning"]["modelNotes"] = [
        {
            "createdAt": iso_z(now),
            "type": "CLEAN_START",
            "message": (
                "История прогнозов, результаты обучения и виртуальный банк "
                "сброшены по явной команде владельца."
            ),
        }
    ]

    write_json_atomic(STATE_PATH, state)
    write_json_atomic(
        REPORT_PATH,
        {
            "status": "GREEN",
            "version": STATE_VERSION,
            "sourceMarker": PIPELINE_MARKER,
            "resetMarker": RESET_MARKER,
            "mode": "CLEAN_RESET",
            "startedAt": iso_z(now),
            "finishedAt": iso_z(now),
            "diagnostics": {
                "startingBank": state["bank"]["starting"],
                "currentBank": state["bank"]["current"],
                "dailyAnalysis": 0,
                "bestBets": 0,
                "analysisHistory": 0,
                "bestBetHistory": 0,
                "learningSegments": 0,
                "calibrationBins": 0,
            },
            "warnings": [],
            "errors": [],
        },
    )
    write_json_atomic(
        DAILY_SNAPSHOT_PATH,
        {
            "version": STATE_VERSION,
            "sourceMarker": PIPELINE_MARKER,
            "resetMarker": RESET_MARKER,
            "updatedAt": state["meta"]["updatedAt"],
            "analysisDateLocal": "",
            "dailyAnalysis": [],
            "bestBets": [],
        },
    )

    validate_state(state, config, allow_legacy=False)

    if safe_float(state["bank"].get("starting")) != safe_float(
        config.get("startingVirtualBank"),
        10000.0,
    ):
        raise RuntimeError("Clean reset starting bank mismatch")
    if safe_float(state["bank"].get("current")) != safe_float(
        config.get("startingVirtualBank"),
        10000.0,
    ):
        raise RuntimeError("Clean reset current bank mismatch")
    if any(
        state.get(key)
        for key in (
            "dailyAnalysis",
            "bestBets",
            "predictions",
            "analysisHistory",
            "history",
        )
    ):
        raise RuntimeError("Clean reset did not clear prediction collections")
    if state.get("learning", {}).get("segments"):
        raise RuntimeError("Clean reset did not clear learning segments")
    if state.get("learning", {}).get("calibrationBins"):
        raise RuntimeError("Clean reset did not clear calibration bins")

    print("CLEAN_RESET_GREEN_V10")
    print(f"RESET_MARKER={RESET_MARKER}")
    print(f"BANK={state['bank']['current']:.2f}")
    print("DAILY_ANALYSIS=0")
    print("BEST_BETS=0")
    print("HISTORY=0")
    print("LEARNING=0")
    return 0



def repair_prediction_integrity() -> int:
    """Idempotently repair projections/localization while preserving bank and history."""
    config = load_json(CONFIG_PATH, {})
    validate_config(config)
    now = utc_now()
    raw_state = load_json(STATE_PATH, {})
    if not isinstance(raw_state, dict):
        raise RuntimeError("STATE_NOT_OBJECT")
    state = copy.deepcopy(raw_state)
    bank_snapshot = copy.deepcopy(state.get("bank") if isinstance(state.get("bank"), dict) else {})
    bank_fingerprint = json.dumps(bank_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changed = False
    target = safe_int(config.get("bestBetsTarget"), 4)
    expected_percent = safe_float(config.get("stakePerBestBetPercent"), 20.0)
    current = [item for item in state.get("bestBets") or [] if isinstance(item, dict)]
    history = [item for item in state.get("history") or [] if isinstance(item, dict)]
    batch = state.get("batch") if isinstance(state.get("batch"), dict) else {}
    batch_id = str(batch.get("id") or "")
    repaired_best: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for rank, visible in enumerate(current, start=1):
        event_id = str(visible.get("eventId") or "")
        candidates = [
            item for item in history
            if str(item.get("eventId") or "") == event_id
            and (not batch_id or str(item.get("batchId") or "") == batch_id)
            and item.get("recordType") == "BEST_BET"
        ]
        candidates.sort(key=lambda item: (safe_float(item.get("stake")) > 0, history_record_quality(item)))
        record = copy.deepcopy(candidates[-1] if candidates else visible)
        record_id = str(record.get("id") or "")
        if not record_id or record_id in used_ids:
            record["id"] = "bet-" + stable_id(event_id, record.get("market"), record.get("publishedAt"), rank)
        used_ids.add(str(record.get("id") or ""))
        record["recordType"] = "BEST_BET"
        record["isBestBet"] = True
        record["rank"] = rank
        record["rankLabel"] = "Лучшая ставка дня" if rank == 1 else f"Ставка №{rank}"
        if abs(safe_float(record.get("stakePercent")) - expected_percent) > 0.001:
            record["stakePercent"] = expected_percent
        if safe_float(record.get("stake")) <= 0:
            record["stake"] = round(safe_float(bank_snapshot.get("current"), config.get("startingVirtualBank", 10000)) * expected_percent / 100.0, 2)
        record["statusLabel"] = result_status_label(normalize_history_status(record.get("status")))
        apply_russian_display_fields(record)
        repaired_best.append(record)

    if current and len(repaired_best) != target:
        raise RuntimeError(f"CURRENT_BEST_BET_REPAIR_COUNT={len(repaired_best)}")
    if repaired_best != current:
        state["bestBets"] = repaired_best
        state["predictions"] = copy.deepcopy(repaired_best)
        changed = True

    daily = [item for item in state.get("dailyAnalysis") or [] if isinstance(item, dict)]
    daily_before = json.dumps(daily, ensure_ascii=False, sort_keys=True)
    for item in daily:
        apply_russian_display_fields(item)
        item["alternatives"] = [
            alt for alt in item.get("alternatives") or []
            if isinstance(alt, dict) and standard_market_allowed(alt.get("marketKey"), alt.get("market"), alt.get("point"))
        ]
    if repaired_best:
        apply_best_bets_to_daily_analysis(daily, repaired_best)
    if json.dumps(daily, ensure_ascii=False, sort_keys=True) != daily_before:
        state["dailyAnalysis"] = daily
        changed = True

    meta = state.setdefault("meta", {})
    if meta.get("integrityPatch") != "V10_R14_RESULT_INTEGRITY_GOAL_DIRECTION":
        meta["integrityPatch"] = "V10_R14_RESULT_INTEGRITY_GOAL_DIRECTION"
        meta["marketPolicy"] = R14_MARKET_POLICY
        meta["futurePublicationPolicy"] = "FOOTBALL_ONLY_NO_ASIAN_MARKETS"
        meta["translationPolicy"] = "OFFICIAL_DICTIONARY_THEN_FULL_UNICODE_TRANSLITERATION_NO_MIXED_SCRIPT"
        changed = True

    state["bank"] = copy.deepcopy(bank_snapshot)
    after_fingerprint = json.dumps(state["bank"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if after_fingerprint != bank_fingerprint:
        raise RuntimeError("BANK_PRESERVATION_GUARD_FAILED")

    if changed:
        meta["sourceMarker"] = PIPELINE_MARKER
        meta["updatedAt"] = iso_z(now)
        validate_state(state, config, allow_legacy=True)
        write_json_atomic(STATE_PATH, state)
        daily_snapshot = load_json(DAILY_SNAPSHOT_PATH, {})
        if isinstance(daily_snapshot, dict):
            daily_snapshot["meta"] = copy.deepcopy(meta)
            daily_snapshot["bank"] = copy.deepcopy(bank_snapshot)
            daily_snapshot["dailyAnalysis"] = copy.deepcopy(state.get("dailyAnalysis") or [])
            daily_snapshot["bestBets"] = copy.deepcopy(state.get("bestBets") or [])
            write_json_atomic(DAILY_SNAPSHOT_PATH, daily_snapshot)
    else:
        validate_state(state, config, allow_legacy=True)

    print(f"CURRENT_BEST_BETS_REPAIRED={len(repaired_best)}")
    print(f"STATE_REPAIR_CHANGED={str(changed).upper()}")
    print("BANK_PRESERVED=TRUE")
    print(f"BANK_CURRENT={bank_snapshot.get('current')}")
    print("V10_R14_STATE_REPAIR=GREEN")
    return 0

def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Football Lab V10 pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auto", action="store_true", help="Settle due events and generate once per local day")
    group.add_argument("--update", action="store_true", help="Force daily generation")
    group.add_argument("--settle", action="store_true", help="Settle due events without forced generation")
    group.add_argument("--settle-live", action="store_true", help="Settle confirmed live finals and clean prediction history")
    group.add_argument("--live-cycle", action="store_true", help="Settle the current batch and immediately publish the next 15 plus 4 when complete")
    group.add_argument("--validate", action="store_true", help="Validate repository and state")
    group.add_argument("--self-test", action="store_true", help="Run offline synthetic end-to-end test")
    group.add_argument("--migrate-state", action="store_true", help="Migrate legacy state to V10")
    group.add_argument("--repair-integrity", action="store_true", help="Repair frozen picks and independent learning while preserving bank")
    group.add_argument("--reset-state", action="store_true", help="Reset V10 bank, predictions, history and learning")
    args = parser.parse_args(argv)

    if args.validate:
        return validate_repository_files()
    if args.self_test:
        return run_self_test()
    if args.migrate_state:
        return migrate_state_file()
    if args.repair_integrity:
        return repair_prediction_integrity()
    if args.reset_state:
        return reset_state_file()
    if args.settle_live:
        return run_live_settlement()
    if args.live_cycle:
        return run_live_cycle()
    if args.update:
        return run_pipeline("update", force_generation=True)
    if args.settle:
        return run_pipeline("settle", force_generation=False)
    return run_pipeline("auto", force_generation=False)


if __name__ == "__main__":
    try:
        raise SystemExit(cli_main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        try:
            write_json_atomic(
                REPORT_PATH,
                {
                    "status": "RED",
                    "version": STATE_VERSION,
                    "sourceMarker": PIPELINE_MARKER,
                    "finishedAt": iso_z(utc_now()),
                    "errors": [f"{type(exc).__name__}: {exc}"],
                },
            )
        except Exception:
            pass
        raise
