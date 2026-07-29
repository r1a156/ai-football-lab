#!/usr/bin/env python3
# V10_R6_LIVE_MATCH_INTELLIGENCE
# V10_R7_HISTORY_LIVE_CLEANUP
# V10_R8_ATOMIC_BATCH_ROLLOVER
"""Safe live-score and live-calibration layer for AI Football Lab V10.

This module writes live score and calibration only. The core R8 live-cycle command
settles bank/history and publishes the next batch atomically after all current
records are terminal.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
import pathlib
import re
import tempfile
import urllib.parse
from collections import defaultdict
from typing import Any

import update_predictions as core

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "analysis.json"
STATE_PATH = ROOT / "data" / "state.json"
LIVE_STATE_PATH = ROOT / "data" / "live-state.json"
LIVE_LEARNING_PATH = ROOT / "data" / "live-learning.json"
LIVE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "live-update.yml"

LIVE_MARKER = "V10_R6_LIVE_MATCH_INTELLIGENCE"
LIVE_WORKFLOW_MARKER = "V10_R6_LIVE_AUTO_REFRESH"
LIVE_VERSION = "1.2.0"
UTC = dt.timezone.utc


def write_json_atomic(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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


def default_live_state(config: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    return {
        "version": LIVE_VERSION,
        "sourceMarker": LIVE_MARKER,
        "status": "IDLE",
        "updatedAt": core.iso_z(now),
        "refreshMinutes": core.safe_int(config.get("liveRefreshMinutes"), 10),
        "events": [],
        "activeEventIds": [],
        "providerHealth": {},
        "notice": (
            "Счёт обновляется автоматически. Исходный прогноз, коэффициент и "
            "виртуальная ставка после начала матча не изменяются."
        ),
    }


def default_live_learning(now: dt.datetime) -> dict[str, Any]:
    return {
        "version": 1,
        "sourceMarker": LIVE_MARKER,
        "updatedAt": core.iso_z(now),
        "sessions": {},
        "calibration": {},
        "processedCompletedSessions": [],
        "statistics": {
            "completedSessions": 0,
            "snapshotCount": 0,
            "calibratedBuckets": 0,
        },
    }


def normalize_live_learning(source: Any, now: dt.datetime) -> dict[str, Any]:
    value = default_live_learning(now)
    if isinstance(source, dict):
        value.update(source)
    if not isinstance(value.get("sessions"), dict):
        value["sessions"] = {}
    if not isinstance(value.get("calibration"), dict):
        value["calibration"] = {}
    if not isinstance(value.get("processedCompletedSessions"), list):
        value["processedCompletedSessions"] = []
    if not isinstance(value.get("statistics"), dict):
        value["statistics"] = {}
    value["version"] = 1
    value["sourceMarker"] = LIVE_MARKER
    return value


def unique_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    best_ids = {
        str(item.get("eventId") or "")
        for item in state.get("bestBets") or []
        if isinstance(item, dict)
    }
    for collection_name in ("bestBets", "dailyAnalysis"):
        for source in state.get(collection_name) or []:
            if not isinstance(source, dict):
                continue
            event_id = str(source.get("eventId") or "")
            if not event_id or event_id in seen:
                continue
            record = copy.deepcopy(source)
            record["isBestBet"] = event_id in best_ids or bool(record.get("isBestBet"))
            result.append(record)
            seen.add(event_id)
    return result


def record_relevant(
    record: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any],
) -> bool:
    commence = core.parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    if not commence:
        return False
    status = str(record.get("status") or "pending").lower()
    if status in {"won", "lost", "push", "void", "cancelled"}:
        return False
    lookahead = dt.timedelta(minutes=core.safe_int(config.get("liveLookaheadMinutes"), 90))
    past_hours = core.safe_int(
        config.get(
            "livePastHoursHockey" if record.get("sport") == "ice_hockey" else "livePastHoursSoccer"
        ),
        6 if record.get("sport") == "ice_hockey" else 5,
    )
    return commence - lookahead <= now <= commence + dt.timedelta(hours=past_hours)


def priority_key(record: dict[str, Any], now: dt.datetime) -> tuple[int, float]:
    commence = core.parse_datetime(record.get("commenceTime") or record.get("utcDate")) or now
    live = commence <= now
    return (
        0 if record.get("isBestBet") and live else
        1 if live else
        2 if record.get("isBestBet") else
        3,
        abs((commence - now).total_seconds()),
    )


def score_map_from_rows(rows: Any) -> dict[str, int]:
    return {
        str(item.get("name") or ""): core.safe_int(item.get("score"))
        for item in rows or []
        if isinstance(item, dict) and str(item.get("name") or "")
    }


def odds_score_query_relevant(
    record: dict[str, Any],
    now: dt.datetime,
    config: dict[str, Any],
) -> bool:
    commence = core.parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    if not commence:
        return False
    before_minutes = max(0, core.safe_int(config.get("liveOddsQueryBeforeStartMinutes"), 10))
    past_hours = core.safe_int(
        config.get(
            "livePastHoursHockey" if record.get("sport") == "ice_hockey" else "livePastHoursSoccer"
        ),
        6 if record.get("sport") == "ice_hockey" else 5,
    )
    return commence - dt.timedelta(minutes=before_minutes) <= now <= commence + dt.timedelta(hours=past_hours)


def fetch_odds_scores(
    client: core.ApiClient,
    records: list[dict[str, Any]],
    api_key: str | None,
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not api_key:
        return [], ["Ключ глобального источника счёта не настроен"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not odds_score_query_relevant(record, now, config):
            continue
        sport_key = str(record.get("sportKey") or record.get("oddsSportKey") or "")
        if sport_key:
            grouped[sport_key].append(record)
    maximum_calls = max(0, core.safe_int(config.get("liveMaximumOddsScoreCallsPerRun"), 3))
    ordered_groups = sorted(
        grouped.items(),
        key=lambda row: min(priority_key(item, now) for item in row[1]),
    )[:maximum_calls]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for sport_key, sport_records in ordered_groups:
        event_ids = ",".join(str(item.get("eventId")) for item in sport_records if item.get("eventId"))
        params = {
            "apiKey": api_key,
            "dateFormat": "iso",
            "eventIds": event_ids,
        }
        url = (
            f"{core.ODDS_API_BASE}/sports/{urllib.parse.quote(sport_key)}/scores?"
            + urllib.parse.urlencode(params)
        )
        try:
            payload = client.request_json(url, label=f"LIVE_SCORES:{sport_key}")
        except Exception as exc:
            errors.append(f"{sport_key}: {exc}")
            continue
        for event in payload if isinstance(payload, list) else []:
            if not isinstance(event, dict):
                continue
            home = str(event.get("home_team") or "")
            away = str(event.get("away_team") or "")
            scores = score_map_from_rows(event.get("scores"))
            has_score = home in scores and away in scores
            completed = bool(event.get("completed"))
            results.append(
                {
                    "eventId": str(event.get("id") or ""),
                    "sportKey": str(event.get("sport_key") or sport_key),
                    "home": home,
                    "away": away,
                    "homeScore": scores.get(home) if has_score else None,
                    "awayScore": scores.get(away) if has_score else None,
                    "completed": completed,
                    "status": "FINISHED" if completed else "LIVE" if has_score else "SCHEDULED",
                    "minute": None,
                    "period": None,
                    "clock": "",
                    "commenceTime": event.get("commence_time"),
                    "updatedAt": core.iso_z(core.utc_now()),
                    "source": "THE_ODDS_API_SCORES",
                    "sourcePriority": 70,
                }
            )
        remaining_raw = client.odds_quota.get("requestsRemaining")
        if remaining_raw is not None:
            remaining = core.safe_int(remaining_raw, -1)
            reserve = max(0, core.safe_int(config.get("liveOddsQuotaReserve"), 50))
            if remaining >= 0 and remaining <= reserve:
                errors.append(
                    f"Глобальный источник: сохранён резерв квоты {remaining} запросов"
                )
                break
    return results, errors


def football_score_value(score: dict[str, Any], side: str) -> int | None:
    for section in ("fullTime", "regularTime", "halfTime"):
        block = score.get(section)
        if isinstance(block, dict) and block.get(side) is not None:
            return core.safe_int(block.get(side))
    return None


def fetch_football_live(
    client: core.ApiClient,
    records: list[dict[str, Any]],
    api_key: str | None,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not api_key or not any(item.get("sport") == "soccer" for item in records):
        return [], []
    dates = {
        (core.parse_datetime(item.get("commenceTime") or item.get("utcDate")) or now).date()
        for item in records
        if item.get("sport") == "soccer"
    }
    if not dates:
        dates = {now.date()}
    date_from = min(dates).isoformat()
    date_to = max(dates).isoformat()
    url = f"{core.FOOTBALL_DATA_BASE}/matches?" + urllib.parse.urlencode(
        {"dateFrom": date_from, "dateTo": date_to}
    )
    try:
        payload = client.request_json(
            url,
            headers={"X-Auth-Token": api_key},
            label="LIVE_FOOTBALL_DATA",
        )
    except Exception as exc:
        return [], [f"football-data.org: {exc}"]
    results: list[dict[str, Any]] = []
    for match in payload.get("matches") or [] if isinstance(payload, dict) else []:
        if not isinstance(match, dict):
            continue
        status = str(match.get("status") or "").upper()
        home_obj = match.get("homeTeam") if isinstance(match.get("homeTeam"), dict) else {}
        away_obj = match.get("awayTeam") if isinstance(match.get("awayTeam"), dict) else {}
        score = match.get("score") if isinstance(match.get("score"), dict) else {}
        home_score = football_score_value(score, "home")
        away_score = football_score_value(score, "away")
        completed = status in {"FINISHED", "AWARDED"}
        results.append(
            {
                "eventId": "",
                "providerEventId": str(match.get("id") or ""),
                "home": str(home_obj.get("name") or home_obj.get("shortName") or ""),
                "away": str(away_obj.get("name") or away_obj.get("shortName") or ""),
                "homeScore": home_score,
                "awayScore": away_score,
                "completed": completed,
                "status": status or "UNKNOWN",
                "minute": match.get("minute"),
                "period": None,
                "clock": "",
                "commenceTime": match.get("utcDate"),
                "updatedAt": core.iso_z(now),
                "source": "FOOTBALL_DATA_LIVE",
                "sourcePriority": 90,
            }
        )
    return results, []


def nhl_team_name(team: Any) -> str:
    if not isinstance(team, dict):
        return ""
    for key in ("name", "commonName", "placeName", "teamName"):
        value = team.get(key)
        if isinstance(value, dict):
            value = value.get("default") or next(iter(value.values()), "")
        if value:
            return str(value)
    return str(team.get("abbrev") or "")


def fetch_nhl_live(
    client: core.ApiClient,
    records: list[dict[str, Any]],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    nhl_records = [
        item for item in records
        if item.get("sport") == "ice_hockey"
        and "nhl" in str(item.get("sportKey") or item.get("league") or "").lower()
    ]
    if not nhl_records:
        return [], []
    dates = sorted({
        (core.parse_datetime(item.get("commenceTime") or item.get("utcDate")) or now).date()
        for item in nhl_records
    })
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for day in dates[:2]:
        try:
            payload = client.request_json(
                f"{core.NHL_API_BASE}/score/{day.isoformat()}",
                label=f"LIVE_NHL:{day.isoformat()}",
            )
        except Exception as exc:
            errors.append(f"NHL {day.isoformat()}: {exc}")
            continue
        for game in payload.get("games") or [] if isinstance(payload, dict) else []:
            if not isinstance(game, dict):
                continue
            state = str(game.get("gameState") or game.get("gameScheduleState") or "").upper()
            home_obj = game.get("homeTeam") if isinstance(game.get("homeTeam"), dict) else {}
            away_obj = game.get("awayTeam") if isinstance(game.get("awayTeam"), dict) else {}
            clock_obj = game.get("clock") if isinstance(game.get("clock"), dict) else {}
            period_obj = game.get("periodDescriptor") if isinstance(game.get("periodDescriptor"), dict) else {}
            completed = state in {"FINAL", "OFF"}
            results.append(
                {
                    "eventId": "",
                    "providerEventId": str(game.get("id") or ""),
                    "home": nhl_team_name(home_obj),
                    "away": nhl_team_name(away_obj),
                    "homeScore": home_obj.get("score"),
                    "awayScore": away_obj.get("score"),
                    "completed": completed,
                    "status": "FINISHED" if completed else "LIVE" if state in {"LIVE", "CRIT"} else "SCHEDULED",
                    "minute": None,
                    "period": period_obj.get("number") or game.get("period"),
                    "periodType": period_obj.get("periodType"),
                    "clock": str(clock_obj.get("timeRemaining") or ""),
                    "commenceTime": game.get("startTimeUTC"),
                    "updatedAt": core.iso_z(now),
                    "source": "NHL_PUBLIC_LIVE",
                    "sourcePriority": 100,
                }
            )
    return results, errors


def result_similarity(record: dict[str, Any], result: dict[str, Any]) -> float:
    if str(result.get("eventId") or "") == str(record.get("eventId") or ""):
        return 2.0
    home_score = core.token_similarity(record.get("home"), result.get("home"))
    away_score = core.token_similarity(record.get("away"), result.get("away"))
    reverse_score = (
        core.token_similarity(record.get("home"), result.get("away"))
        + core.token_similarity(record.get("away"), result.get("home"))
    ) / 2
    direct = (home_score + away_score) / 2
    if reverse_score > direct:
        return 0.0
    record_time = core.parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    result_time = core.parse_datetime(result.get("commenceTime"))
    if record_time and result_time and abs((record_time - result_time).total_seconds()) > 8 * 3600:
        return 0.0
    return direct


def best_result(record: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = [
        (result_similarity(record, result), core.safe_int(result.get("sourcePriority")), result)
        for result in results
    ]
    ranked = [row for row in ranked if row[0] >= 0.72 or row[0] >= 1.9]
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return ranked[0][2]


def parse_expected_goals(record: dict[str, Any]) -> tuple[float, float]:
    home = core.safe_float(record.get("expectedHomeGoals"), -1)
    away = core.safe_float(record.get("expectedAwayGoals"), -1)
    if home >= 0 and away >= 0:
        return home, away
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*[:—-]\s*([0-9]+(?:[.,][0-9]+)?)", str(record.get("expectedScore") or ""))
    if match:
        return float(match.group(1).replace(",", ".")), float(match.group(2).replace(",", "."))
    return (1.45, 1.15) if record.get("sport") == "soccer" else (3.0, 2.8)


def phase_key(record: dict[str, Any], elapsed_fraction: float) -> str:
    phase = "EARLY" if elapsed_fraction < 0.34 else "MIDDLE" if elapsed_fraction < 0.72 else "LATE"
    family = str(record.get("marketFamily") or core.market_family(str(record.get("market") or "")))
    return f"{record.get('sport') or 'soccer'}|{family}|{phase}"


def live_calibration_adjustment(
    learning: dict[str, Any],
    key: str,
    config: dict[str, Any],
) -> tuple[float, dict[str, Any] | None]:
    bucket = (learning.get("calibration") or {}).get(key)
    if not isinstance(bucket, dict):
        return 0.0, None
    count = core.safe_int(bucket.get("count"))
    minimum = core.safe_int(config.get("liveLearningMinimumSamples"), 15)
    if count < minimum:
        return 0.0, bucket
    predicted = core.safe_float(bucket.get("predictedSum")) / max(1, count)
    actual = core.safe_float(bucket.get("actualSum")) / max(1, count)
    maximum = core.safe_float(config.get("liveMaximumProbabilityAdjustment"), 0.06)
    weight = core.clamp(count / max(minimum, core.safe_int(config.get("liveLearningFullWeightSamples"), 80)), 0.15, 1.0)
    return core.clamp((actual - predicted) * weight, -maximum, maximum), bucket


def live_probability(
    record: dict[str, Any],
    home_score: int,
    away_score: int,
    elapsed_fraction: float,
    learning: dict[str, Any],
    config: dict[str, Any],
) -> tuple[float, str, dict[str, Any] | None]:
    duration = 90.0 if record.get("sport") == "soccer" else 60.0
    remaining_fraction = core.clamp(1.0 - elapsed_fraction, 0.0, 1.0)
    expected_home, expected_away = parse_expected_goals(record)
    remaining_home = max(0.02, expected_home * remaining_fraction)
    remaining_away = max(0.02, expected_away * remaining_fraction)
    max_goals = 7 if record.get("sport") == "soccer" else 10
    probability = 0.0
    push_probability = 0.0
    for add_home in range(max_goals + 1):
        p_home = core.poisson_probability(remaining_home, add_home)
        for add_away in range(max_goals + 1):
            weight = p_home * core.poisson_probability(remaining_away, add_away)
            status = core.settle_market(record, home_score + add_home, away_score + add_away)
            if status == "won":
                probability += weight
            elif status == "push":
                push_probability += weight
    base_probability = core.clamp(probability + push_probability * 0.5, 0.001, 0.999)
    key = phase_key(record, elapsed_fraction)
    adjustment, evidence = live_calibration_adjustment(learning, key, config)
    adjusted = core.clamp(base_probability + adjustment, 0.001, 0.999)
    reason = (
        f"Пересчёт по текущему счёту и оставшемуся времени: "
        f"{int(round(remaining_fraction * duration))} мин. до расчётного завершения."
    )
    return adjusted, reason, evidence


def elapsed_information(
    record: dict[str, Any],
    result: dict[str, Any] | None,
    now: dt.datetime,
) -> tuple[float, str, int | None, int | None]:
    sport = str(record.get("sport") or "soccer")
    duration = 60 if sport == "ice_hockey" else 90
    minute = core.safe_int((result or {}).get("minute"), -1)
    period = core.safe_int((result or {}).get("period"), -1)
    clock = str((result or {}).get("clock") or "")
    if sport == "ice_hockey" and period > 0:
        elapsed = max(0, (period - 1) * 20)
        clock_match = re.match(r"(\d{1,2}):(\d{2})", clock)
        if clock_match:
            remaining = int(clock_match.group(1)) + int(clock_match.group(2)) / 60
            elapsed += max(0, 20 - remaining)
        fraction = core.clamp(elapsed / duration, 0.0, 1.0)
        label = f"{period}-й период" + (f" · {clock}" if clock else "")
        return fraction, label, None, period
    if minute >= 0:
        fraction = core.clamp(minute / duration, 0.0, 1.0)
        return fraction, f"{minute}-я минута", minute, None
    commence = core.parse_datetime(record.get("commenceTime") or record.get("utcDate"))
    elapsed_minutes = max(0, int((now - commence).total_seconds() // 60)) if commence else 0
    elapsed_minutes = min(duration, elapsed_minutes)
    fraction = core.clamp(elapsed_minutes / duration, 0.0, 1.0)
    if sport == "ice_hockey":
        inferred_period = min(3, elapsed_minutes // 20 + 1) if elapsed_minutes < 60 else 3
        return fraction, f"{inferred_period}-й период", None, inferred_period
    return fraction, f"ориентировочно {elapsed_minutes}-я минута", elapsed_minutes, None


def public_status(
    result: dict[str, Any] | None,
    record: dict[str, Any],
    now: dt.datetime,
    previous: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    source_status = str((result or {}).get("status") or "").upper()

    if (
        source_status in {"FINISHED", "AWARDED", "FINAL", "OFF"}
        or bool((result or {}).get("completed"))
    ):
        return "FINISHED"
    if source_status in {"LIVE", "IN_PLAY", "PAUSED", "CRIT"}:
        return "LIVE"
    if source_status in {"POSTPONED", "SUSPENDED"}:
        return "POSTPONED"
    if source_status in {"CANCELLED", "CANCELED"}:
        return "CANCELLED"

    commence = core.parse_datetime(
        record.get("commenceTime")
        or record.get("utcDate")
    )
    if commence and now < commence:
        return "SCHEDULED"

    # Never declare a match live from the clock alone. A provider must have
    # confirmed LIVE/IN_PLAY. A recent provider-confirmed live event may be
    # retained briefly while one polling response is temporarily missing.
    if previous and str(previous.get("status") or "") == "LIVE":
        previous_updated = core.parse_datetime(previous.get("updatedAt"))
        grace_minutes = max(
            0,
            core.safe_int(
                (config or {}).get("liveProviderGraceMinutes"),
                20,
            ),
        )
        if (
            previous_updated
            and now - previous_updated
            <= dt.timedelta(minutes=grace_minutes)
        ):
            return "LIVE"

    return "UNCONFIRMED"


def partition_public_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    active = [
        item
        for item in events
        if str(item.get("status") or "") == "LIVE"
    ]
    scheduled = [
        item
        for item in events
        if str(item.get("status") or "") == "SCHEDULED"
    ]
    hidden = [
        item
        for item in events
        if str(item.get("status") or "")
        not in {"LIVE", "SCHEDULED"}
    ]
    return active, scheduled, hidden

def make_live_event(
    record: dict[str, Any],
    result: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    learning: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    status = public_status(result, record, now, previous, config)
    home_score = (result or {}).get("homeScore")
    away_score = (result or {}).get("awayScore")
    keep_previous_score = (
        result is None
        and status == "LIVE"
        and previous
        and str(previous.get("status") or "") == "LIVE"
    )
    if home_score is None and keep_previous_score:
        home_score = previous.get("homeScore")
    if away_score is None and keep_previous_score:
        away_score = previous.get("awayScore")
    has_score = home_score is not None and away_score is not None
    fraction, clock_label, minute, period = elapsed_information(record, result, now)
    pre_probability = core.clamp(
        core.safe_float(record.get("modelProbability") or record.get("probability"), 0.5),
        0.001,
        0.999,
    )
    current_probability = pre_probability
    live_reason = "Матч ещё не начался. Используется предматчевая вероятность."
    calibration_evidence = None
    if has_score and status in {"LIVE", "FINISHED"}:
        if status == "FINISHED":
            settled = core.settle_market(record, core.safe_int(home_score), core.safe_int(away_score))
            current_probability = 1.0 if settled == "won" else 0.5 if settled == "push" else 0.0
            live_reason = "Матч завершён; показан фактический результат прогноза."
        else:
            current_probability, live_reason, calibration_evidence = live_probability(
                record,
                core.safe_int(home_score),
                core.safe_int(away_score),
                fraction,
                learning,
                config,
            )
    updated_at = str((result or {}).get("updatedAt") or core.iso_z(now))
    event = {
        "eventId": str(record.get("eventId") or ""),
        "analysisId": str(record.get("id") or ""),
        "isBestBet": bool(record.get("isBestBet")),
        "sport": record.get("sport"),
        "sportLabel": record.get("sportLabel") or core.sport_label(str(record.get("sport") or "soccer")),
        "sportKey": record.get("sportKey") or record.get("oddsSportKey"),
        "league": record.get("league"),
        "leagueRu": record.get("leagueRu") or core.russian_display_text(record.get("league")),
        "country": record.get("country"),
        "countryRu": record.get("countryRu") or core.russian_display_text(record.get("country")),
        "home": record.get("home"),
        "away": record.get("away"),
        "homeRu": record.get("homeRu") or core.russian_display_text(record.get("home")),
        "awayRu": record.get("awayRu") or core.russian_display_text(record.get("away")),
        "commenceTime": record.get("commenceTime") or record.get("utcDate"),
        "status": status,
        "statusRu": {
            "SCHEDULED": "Ожидается",
            "LIVE": "Матч идёт",
            "FINISHED": "Матч завершён",
            "POSTPONED": "Матч перенесён",
            "CANCELLED": "Матч отменён",
            "UNCONFIRMED": "Ожидается подтверждение источника",
        }.get(status, "Статус уточняется"),
        "homeScore": core.safe_int(home_score) if home_score is not None else None,
        "awayScore": core.safe_int(away_score) if away_score is not None else None,
        "score": f"{core.safe_int(home_score)}:{core.safe_int(away_score)}" if has_score else "",
        "clockLabel": clock_label if status == "LIVE" else "",
        "minute": minute,
        "period": period,
        "elapsedFraction": round(fraction, 4),
        "pick": record.get("pick"),
        "pickRu": record.get("pickRu") or core.russian_display_text(record.get("pick")),
        "market": record.get("market"),
        "marketFamily": record.get("marketFamily") or core.market_family(str(record.get("market") or "")),
        "point": record.get("point"),
        "preMatchProbability": round(pre_probability, 6),
        "liveProbability": round(current_probability, 6),
        "liveProbabilityPercent": round(current_probability * 100, 1),
        "liveReason": live_reason,
        "calibrationEvidence": calibration_evidence,
        "provider": (result or {}).get("source") or (previous or {}).get("provider") or "Ожидание источника",
        "providerEventId": (result or {}).get("providerEventId"),
        "updatedAt": updated_at,
        "freshnessSeconds": max(0, int((now - (core.parse_datetime(updated_at) or now)).total_seconds())),
        "originalPredictionImmutable": True,
    }
    return event


def append_snapshot(
    learning: dict[str, Any],
    event: dict[str, Any],
    config: dict[str, Any],
    now: dt.datetime,
) -> None:
    sessions = learning.setdefault("sessions", {})
    event_id = str(event.get("eventId") or "")
    if not event_id:
        return
    session = sessions.setdefault(
        event_id,
        {
            "eventId": event_id,
            "sport": event.get("sport"),
            "marketFamily": event.get("marketFamily"),
            "pickRu": event.get("pickRu"),
            "homeRu": event.get("homeRu"),
            "awayRu": event.get("awayRu"),
            "preMatchProbability": event.get("preMatchProbability"),
            "startedAt": event.get("commenceTime"),
            "snapshots": [],
            "completed": False,
        },
    )
    snapshots = session.setdefault("snapshots", [])
    current = {
        "at": core.iso_z(now),
        "status": event.get("status"),
        "score": event.get("score"),
        "clockLabel": event.get("clockLabel"),
        "elapsedFraction": event.get("elapsedFraction"),
        "liveProbability": event.get("liveProbability"),
    }
    last = snapshots[-1] if snapshots else None
    changed = not last or any(
        last.get(key) != current.get(key)
        for key in ("status", "score", "clockLabel", "liveProbability")
    )
    if changed:
        snapshots.append(current)
        limit = max(12, core.safe_int(config.get("liveSnapshotsPerEventLimit"), 72))
        session["snapshots"] = snapshots[-limit:]
    if event.get("status") == "FINISHED":
        session["completed"] = True
        session["completedAt"] = core.iso_z(now)
        session["finalScore"] = event.get("score")
        final_status = core.settle_market(
            {
                "market": event.get("market"),
                "marketFamily": event.get("marketFamily"),
                "selectionCode": None,
                "point": event.get("point"),
                "pick": event.get("pick"),
            },
            core.safe_int(event.get("homeScore")),
            core.safe_int(event.get("awayScore")),
        )
        # The complete record is not stored in the live event. The final status
        # is therefore read from the probability when the market cannot be
        # reconstructed exactly. Main settlement remains the source of truth.
        if event.get("liveProbability") == 1.0:
            final_status = "won"
        elif event.get("liveProbability") == 0.5:
            final_status = "push"
        elif event.get("liveProbability") == 0.0:
            final_status = "lost"
        session["finalStatus"] = final_status


def update_completed_calibration(
    learning: dict[str, Any],
    config: dict[str, Any],
) -> None:
    processed = set(str(value) for value in learning.get("processedCompletedSessions") or [])
    calibration = learning.setdefault("calibration", {})
    for event_id, session in list((learning.get("sessions") or {}).items()):
        if not isinstance(session, dict) or not session.get("completed") or event_id in processed:
            continue
        actual = 1.0 if session.get("finalStatus") == "won" else 0.5 if session.get("finalStatus") == "push" else 0.0
        for snapshot in session.get("snapshots") or []:
            if not isinstance(snapshot, dict) or snapshot.get("status") != "LIVE":
                continue
            fraction = core.safe_float(snapshot.get("elapsedFraction"))
            phase = "EARLY" if fraction < 0.34 else "MIDDLE" if fraction < 0.72 else "LATE"
            key = f"{session.get('sport') or 'soccer'}|{session.get('marketFamily') or 'OTHER'}|{phase}"
            bucket = calibration.setdefault(
                key,
                {
                    "count": 0,
                    "predictedSum": 0.0,
                    "actualSum": 0.0,
                    "brierSum": 0.0,
                },
            )
            probability = core.clamp(core.safe_float(snapshot.get("liveProbability"), 0.5), 0.001, 0.999)
            bucket["count"] = core.safe_int(bucket.get("count")) + 1
            bucket["predictedSum"] = core.safe_float(bucket.get("predictedSum")) + probability
            bucket["actualSum"] = core.safe_float(bucket.get("actualSum")) + actual
            bucket["brierSum"] = core.safe_float(bucket.get("brierSum")) + (probability - actual) ** 2
            count = bucket["count"]
            bucket["averagePredicted"] = round(bucket["predictedSum"] / count, 4)
            bucket["actualRate"] = round(bucket["actualSum"] / count, 4)
            bucket["probabilityBias"] = round(bucket["actualRate"] - bucket["averagePredicted"], 6)
            bucket["brierScore"] = round(bucket["brierSum"] / count, 6)
        processed.add(event_id)
    learning["processedCompletedSessions"] = list(processed)[-1000:]
    sessions = learning.get("sessions") or {}
    maximum_sessions = max(100, core.safe_int(config.get("liveLearningSessionLimit"), 500))
    if len(sessions) > maximum_sessions:
        ordered = sorted(
            sessions.items(),
            key=lambda row: str(row[1].get("completedAt") or row[1].get("startedAt") or ""),
            reverse=True,
        )[:maximum_sessions]
        learning["sessions"] = dict(ordered)
    learning["statistics"] = {
        "completedSessions": sum(1 for value in learning.get("sessions", {}).values() if isinstance(value, dict) and value.get("completed")),
        "snapshotCount": sum(len(value.get("snapshots") or []) for value in learning.get("sessions", {}).values() if isinstance(value, dict)),
        "calibratedBuckets": sum(1 for value in calibration.values() if isinstance(value, dict) and core.safe_int(value.get("count")) >= core.safe_int(config.get("liveLearningMinimumSamples"), 15)),
    }


def canonical_content(value: dict[str, Any]) -> str:
    copy_value = copy.deepcopy(value)
    copy_value.pop("updatedAt", None)
    for event in copy_value.get("events") or []:
        if isinstance(event, dict):
            event.pop("updatedAt", None)
            event.pop("freshnessSeconds", None)
    return json.dumps(copy_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_learning_content(value: dict[str, Any]) -> str:
    copy_value = copy.deepcopy(value)
    copy_value.pop("updatedAt", None)
    return json.dumps(
        copy_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def run_update() -> int:
    config = core.load_json(CONFIG_PATH, {})
    core.validate_config(config)
    now = core.utc_now()
    state = core.migrate_state(core.load_json(STATE_PATH, {}), config, now)
    previous_live = core.load_json(
        LIVE_STATE_PATH,
        default_live_state(config, now),
    )
    previous_learning = core.load_json(
        LIVE_LEARNING_PATH,
        {},
    )
    learning = normalize_live_learning(
        previous_learning,
        now,
    )
    records = sorted(
        [item for item in unique_records(state) if record_relevant(item, now, config)],
        key=lambda item: priority_key(item, now),
    )
    if not records:
        idle = default_live_state(config, now)
        old = previous_live if isinstance(previous_live, dict) else {}
        if str(old.get("status")) != "IDLE" or old.get("events"):
            write_json_atomic(LIVE_STATE_PATH, idle)
        print("LIVE_UPDATE=IDLE_NO_TRACKED_EVENTS")
        return 0

    client = core.ApiClient()
    football_results, football_errors = fetch_football_live(
        client,
        records,
        os.getenv("FOOTBALL_DATA_API_KEY", "").strip() or None,
        now,
    )
    nhl_results, nhl_errors = fetch_nhl_live(client, records, now)
    free_results = nhl_results + football_results
    unresolved_records = [
        record
        for record in records
        if best_result(record, free_results) is None
    ]
    odds_results, odds_errors = fetch_odds_scores(
        client,
        unresolved_records,
        os.getenv("ODDS_API_KEY", "").strip() or None,
        config,
        now,
    )
    all_results = free_results + odds_results
    previous_by_event = {
        str(item.get("eventId") or ""): item
        for item in previous_live.get("events") or []
        if isinstance(item, dict)
    }
    observed_events: list[dict[str, Any]] = []
    for record in records:
        event_id = str(record.get("eventId") or "")
        result = best_result(record, all_results)
        event = make_live_event(
            record,
            result,
            previous_by_event.get(event_id),
            learning,
            config,
            now,
        )
        observed_events.append(event)

        # Finished events are recorded for calibration and immediate core
        # settlement, but are not kept in the public "Матчи сейчас" list.
        append_snapshot(learning, event, config, now)

    update_completed_calibration(learning, config)
    learning["updatedAt"] = core.iso_z(now)

    active, scheduled, hidden = partition_public_events(observed_events)
    events = active + scheduled
    live_state = {
        "version": LIVE_VERSION,
        "sourceMarker": LIVE_MARKER,
        "status": "LIVE" if active else "TRACKING" if scheduled else "IDLE",
        "updatedAt": core.iso_z(now),
        "refreshMinutes": core.safe_int(config.get("liveRefreshMinutes"), 10),
        "events": events,
        "activeEventIds": [str(item.get("eventId")) for item in active],
        "providerHealth": {
            "status": "GREEN" if all_results else "DEGRADED",
            "calls": len(client.calls),
            "results": len(all_results),
            "oddsQuota": client.odds_quota,
            "errors": odds_errors + football_errors + nhl_errors,
        },
        "cleanup": {
            "finishedRemoved": sum(
                1
                for item in hidden
                if item.get("status") == "FINISHED"
            ),
            "cancelledRemoved": sum(
                1
                for item in hidden
                if item.get("status") == "CANCELLED"
            ),
            "postponedRemoved": sum(
                1
                for item in hidden
                if item.get("status") == "POSTPONED"
            ),
            "unconfirmedRemoved": sum(
                1
                for item in hidden
                if item.get("status") == "UNCONFIRMED"
            ),
        },
        "notice": (
            "Исходный прогноз, коэффициент и размер виртуальной ставки неизменяемы. "
            "Завершённые матчи автоматически удаляются из раздела текущих матчей."
        ),
    }

    if canonical_content(live_state) != canonical_content(previous_live if isinstance(previous_live, dict) else {}):
        write_json_atomic(LIVE_STATE_PATH, live_state)
        print("LIVE_STATE_CHANGED=YES")
    else:
        print("LIVE_STATE_CHANGED=NO")
    if canonical_learning_content(learning) != canonical_learning_content(
        previous_learning if isinstance(previous_learning, dict) else {}
    ):
        write_json_atomic(LIVE_LEARNING_PATH, learning)
        print("LIVE_LEARNING_CHANGED=YES")
    else:
        print("LIVE_LEARNING_CHANGED=NO")
    print(f"LIVE_TRACKED={len(events)}")
    print(f"LIVE_ACTIVE={len(active)}")
    print(
        "LIVE_FINISHED_REMOVED="
        f"{live_state.get('cleanup', {}).get('finishedRemoved', 0)}"
    )
    print(
        "LIVE_UNCONFIRMED_REMOVED="
        f"{live_state.get('cleanup', {}).get('unconfirmedRemoved', 0)}"
    )
    print(f"LIVE_PROVIDER_RESULTS={len(all_results)}")
    print(f"LIVE_PROVIDER_ERRORS={len(odds_errors + football_errors + nhl_errors)}")
    print("LIVE_BANK_MUTATION=NO")
    print("LIVE_PUBLISHED_PREDICTION_MUTATION=NO")
    print("FINAL_STATUS=GREEN_V10_R6_LIVE_REFRESH")
    return 0


def validate_files() -> int:
    config = core.load_json(CONFIG_PATH, {})
    core.validate_config(config)
    for path in (STATE_PATH, LIVE_STATE_PATH, LIVE_LEARNING_PATH, LIVE_WORKFLOW_PATH):
        if not path.exists():
            raise RuntimeError(f"Required live file missing: {path.relative_to(ROOT)}")
    if LIVE_MARKER not in pathlib.Path(__file__).read_text(encoding="utf-8"):
        raise RuntimeError("Live source marker missing")
    if LIVE_WORKFLOW_MARKER not in LIVE_WORKFLOW_PATH.read_text(encoding="utf-8"):
        raise RuntimeError("Live workflow marker missing")
    live_state = core.load_json(LIVE_STATE_PATH, {})
    live_learning = core.load_json(LIVE_LEARNING_PATH, {})
    if live_state.get("sourceMarker") != LIVE_MARKER:
        raise RuntimeError("Live state marker mismatch")
    if live_learning.get("sourceMarker") != LIVE_MARKER:
        raise RuntimeError("Live learning marker mismatch")
    print("LIVE_VALIDATION_GREEN_V10_R6")
    return 0


def run_self_test() -> int:
    config = core.load_json(CONFIG_PATH, {})
    core.validate_config(config)
    now = dt.datetime(2026, 8, 2, 18, 45, tzinfo=UTC)
    record = {
        "id": "analysis-live-test",
        "eventId": "event-live-test",
        "sport": "soccer",
        "sportLabel": "Футбол",
        "sportKey": "soccer_test",
        "league": "Test League",
        "home": "Home Club",
        "away": "Away Club",
        "homeRu": "Хоум Клаб",
        "awayRu": "Эвей Клаб",
        "commenceTime": core.iso_z(now - dt.timedelta(minutes=60)),
        "market": "TOTAL_OVER",
        "marketFamily": "TOTAL",
        "selectionCode": "OVER",
        "point": 2.5,
        "pick": "Тотал больше 2,5",
        "pickRu": "Тотал больше 2,5",
        "expectedScore": "1.8 : 1.1",
        "modelProbability": 0.59,
        "status": "pending",
        "isBestBet": True,
    }
    learning = default_live_learning(now)
    event = make_live_event(
        record,
        {
            "homeScore": 2,
            "awayScore": 1,
            "status": "LIVE",
            "minute": 60,
            "source": "SELF_TEST",
            "updatedAt": core.iso_z(now),
        },
        None,
        learning,
        config,
        now,
    )
    if event.get("score") != "2:1":
        raise RuntimeError("Live self-test score mismatch")
    if core.safe_float(event.get("liveProbability")) < 0.99:
        raise RuntimeError("Live self-test settled over probability mismatch")
    append_snapshot(learning, event, config, now)
    finished = copy.deepcopy(event)
    finished.update({"status": "FINISHED", "liveProbability": 1.0})
    append_snapshot(learning, finished, config, now + dt.timedelta(minutes=35))
    update_completed_calibration(learning, config)
    if not learning.get("sessions"):
        raise RuntimeError("Live self-test session missing")
    if learning.get("sourceMarker") != LIVE_MARKER:
        raise RuntimeError("Live self-test marker mismatch")

    unconfirmed = public_status(
        None,
        record,
        now,
        None,
        config,
    )
    if unconfirmed != "UNCONFIRMED":
        raise RuntimeError(
            "Live self-test clock-only event was falsely marked LIVE"
        )

    fresh_previous = copy.deepcopy(event)
    fresh_previous["updatedAt"] = core.iso_z(
        now - dt.timedelta(minutes=5)
    )
    if public_status(
        None,
        record,
        now,
        fresh_previous,
        config,
    ) != "LIVE":
        raise RuntimeError(
            "Live self-test provider grace window failed"
        )

    stale_previous = copy.deepcopy(event)
    stale_previous["updatedAt"] = core.iso_z(
        now - dt.timedelta(minutes=90)
    )
    if public_status(
        None,
        record,
        now,
        stale_previous,
        config,
    ) != "UNCONFIRMED":
        raise RuntimeError(
            "Live self-test stale event did not expire"
        )

    active, scheduled, hidden = partition_public_events(
        [
            event,
            finished,
            {
                **record,
                "status": "SCHEDULED",
            },
        ]
    )
    if len(active) != 1 or len(scheduled) != 1:
        raise RuntimeError(
            "Live self-test public partition mismatch"
        )
    if not hidden or hidden[0].get("status") != "FINISHED":
        raise RuntimeError(
            "Live self-test finished event was not removed"
        )

    print("SELF_TEST_GREEN_V10_R7_LIVE SCORE=2:1 LIVE_PROBABILITY=100.0 SNAPSHOTS=2")
    print("FINISHED_PUBLIC_EVENTS=0")
    print("CLOCK_ONLY_FALSE_LIVE=0")
    print("BANK_MUTATION=NO")
    print("PREDICTION_MUTATION=NO")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V10 R7 live score updater")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--update", action="store_true")
    group.add_argument("--validate", action="store_true")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.validate:
        return validate_files()
    if args.self_test:
        return run_self_test()
    return run_update()


if __name__ == "__main__":
    raise SystemExit(main())
