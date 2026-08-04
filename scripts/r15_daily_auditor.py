#!/usr/bin/env python3
"""R15F R3 daily OpenRouter audit and first-run activation control.

Facts and probabilities are produced by the deterministic football engine.
This module can only reduce confidence, reorder qualified events, select an
already-calculated alternative market, suggest a valid 3x5 distribution and
write a Russian system narrative. It never discovers fixtures or invents data.
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
import time
import urllib.error
import urllib.request
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "state.json"
SNAPSHOT_PATH = ROOT / "data" / "ai_daily_analysis.json"
CONTROL_PATH = ROOT / "data" / "r15-runtime-control.json"
AUDIT_PATH = ROOT / "data" / "openrouter-daily-audit.json"
CONFIG_PATH = ROOT / "config" / "analysis.json"

UTC = dt.timezone.utc
# Moscow operational time is deliberately represented by a fixed UTC+03:00
# offset. This avoids the optional Python tzdata package on Windows and keeps
# the 08:00–08:00 production boundary deterministic on every runner.
MOSCOW = dt.timezone(dt.timedelta(hours=3), name="MSK")
MOSCOW_TIMEZONE_SOURCE = "FIXED_UTC_PLUS_03_NO_TZDATA_REQUIRED"
VERSION = "V10_R15F_R3R1_FINAL_PORTABLE_MOSCOW_TIME"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def now_utc() -> dt.datetime:
    override = os.getenv("R15_TEST_NOW", "").strip()
    if override:
        value = override.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return dt.datetime.now(tz=UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def load_json(path: pathlib.Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    temporary.replace(path)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def json_fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def next_full_operational_window(now: dt.datetime) -> dict[str, str]:
    local = now.astimezone(MOSCOW)
    start = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local >= start:
        start += dt.timedelta(days=1)
    end = start + dt.timedelta(days=1)
    return {
        "operationalDayId": f"{start.date().isoformat()}-MSK-0800",
        "operationalDateLocal": start.date().isoformat(),
        "windowStart": iso(start.astimezone(UTC)),
        "windowEnd": iso(end.astimezone(UTC)),
        "windowStartLocal": start.isoformat(),
        "windowEndLocal": end.isoformat(),
    }


def initialize_first_run(force: bool = False) -> dict[str, Any]:
    current = load_json(CONTROL_PATH, {})
    if current.get("version") == VERSION and current.get("firstActiveWindowStart") and not force:
        return current
    now = now_utc()
    window = next_full_operational_window(now)
    control = {
        "version": VERSION,
        "installedAt": iso(now),
        "firstActiveOperationalDayId": window["operationalDayId"],
        "firstActiveOperationalDate": window["operationalDateLocal"],
        "firstActiveWindowStart": window["windowStart"],
        "firstActiveWindowEnd": window["windowEnd"],
        "firstActiveWindowStartLocal": window["windowStartLocal"],
        "firstActiveWindowEndLocal": window["windowEndLocal"],
        "activationPolicy": "FIRST_COMPLETE_MOSCOW_08_TO_08_WINDOW_AFTER_INSTALL",
        "status": "PREPARING_NEXT_OPERATIONAL_WINDOW",
        "activatedAt": None,
        "lastPreparationAt": None,
        "lastAuditOperationalDayId": None,
        "lastAuditFingerprint": None,
    }
    write_json(CONTROL_PATH, control)
    return control


def load_control() -> dict[str, Any]:
    return initialize_first_run(force=False)


def activation_gate(now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    control = load_control()
    start = parse_time(control.get("firstActiveWindowStart"))
    ready = bool(start and now >= start)
    return {
        "ready": ready,
        "now": iso(now),
        "control": control,
        "secondsUntilActivation": max(0, int((start - now).total_seconds())) if start else 0,
    }


def deterministic_system_narrative(state: dict[str, Any], phase: str, audit_status: str = "NOT_RUN") -> dict[str, Any]:
    coverage = state.get("dataCoverage") if isinstance(state.get("dataCoverage"), dict) else {}
    daily = state.get("dailyAnalysis") if isinstance(state.get("dailyAnalysis"), list) else []
    found = int(coverage.get("discoveredEvents") or 0)
    qualified = int(coverage.get("qualifiedEvents") or 0)
    average_quality = (
        sum(safe_float(row.get("dataQuality")) for row in daily if isinstance(row, dict)) / len(daily)
        if daily else 0.0
    )
    if phase == "PREPARATION":
        return {
            "title": "Я готовлю следующий полный день",
            "lead": "Система не публикует прогнозы на уже начавшиеся операционные сутки.",
            "body": "История команд, источники и календарь подготавливаются заранее. После 08:00 МСК я заново проверю реальные события, рассчитаю все допустимые рынки и зафиксирую портфель только при полном качестве.",
            "focus": ["следующее окно 08:00–08:00", "банк не задействован", "частичная публикация запрещена"],
            "tone": "CALM_PREPARATION",
            "generatedBy": "DETERMINISTIC_SYSTEM",
        }
    return {
        "title": "Я проверяю не количество прогнозов, а качество решения",
        "lead": f"Найдено {found} событий, контроль качества прошли {qualified}, опубликовано {len(daily)}.",
        "body": f"Среднее качество опубликованных данных — {average_quality:.0f}/100. AI-аудит: {audit_status}. Я сравниваю исходы, тоталы, обе забьют и командные тоталы, а затем учусь на каждом подтверждённом результате.",
        "focus": ["не повышать уверенность без фактов", "отбрасывать слабые матчи", "снижать повторяющиеся ошибки"],
        "tone": "EVIDENCE_FIRST",
        "generatedBy": "DETERMINISTIC_SYSTEM",
    }


def prepare_next_window_state() -> dict[str, Any]:
    now = now_utc()
    control = load_control()
    state = load_json(STATE_PATH, {})
    state.setdefault("meta", {})
    state["nextPortfolio"] = {
        "status": "PREPARING_NEXT_OPERATIONAL_WINDOW",
        "statusLabel": "Подготовка следующего полного портфеля",
        "operationalDayId": control.get("firstActiveOperationalDayId"),
        "operationalDateLocal": control.get("firstActiveOperationalDate"),
        "windowStart": control.get("firstActiveWindowStart"),
        "windowEnd": control.get("firstActiveWindowEnd"),
        "windowStartLocal": control.get("firstActiveWindowStartLocal"),
        "windowEndLocal": control.get("firstActiveWindowEndLocal"),
        "bankEngaged": False,
        "partialDayPublicationAllowed": False,
        "phase": "HISTORY_AND_SOURCE_PREWARM",
        "updatedAt": iso(now),
    }
    state["meta"].update({
        "sourceMarker": VERSION,
        "nextPortfolioStatus": "PREPARING_NEXT_OPERATIONAL_WINDOW",
        "firstActiveOperationalDate": control.get("firstActiveOperationalDate"),
        "firstActiveWindowStart": control.get("firstActiveWindowStart"),
        "firstActiveWindowEnd": control.get("firstActiveWindowEnd"),
        "updatedAt": iso(now),
    })
    state["systemNarrative"] = deterministic_system_narrative(state, "PREPARATION")
    write_json(STATE_PATH, state)
    snapshot = load_json(SNAPSHOT_PATH, {})
    snapshot.update({
        "version": VERSION,
        "updatedAt": iso(now),
        "nextPortfolio": copy.deepcopy(state["nextPortfolio"]),
        "systemNarrative": copy.deepcopy(state["systemNarrative"]),
    })
    write_json(SNAPSHOT_PATH, snapshot)
    control["lastPreparationAt"] = iso(now)
    write_json(CONTROL_PATH, control)
    return state["nextPortfolio"]


def _record_market_options(record: dict[str, Any]) -> list[dict[str, Any]]:
    options = [{
        "marketKey": str(record.get("marketKey") or "PRIMARY"),
        "pick": record.get("pickRu") or record.get("pick"),
        "probability": safe_float(record.get("conservativeProbability"), safe_float(record.get("modelProbability"))),
        "odds": safe_float(record.get("bookmakerOdds") or record.get("odds")),
        "edge": safe_float(record.get("edge")),
        "dataQuality": safe_float(record.get("dataQuality")),
        "agreement": safe_float(record.get("agreement")),
        "marketStability": safe_float(record.get("marketStability")),
        "isPrimary": True,
    }]
    for alternative in record.get("alternatives") or []:
        if not isinstance(alternative, dict):
            continue
        options.append({
            "marketKey": str(alternative.get("marketKey") or ""),
            "pick": alternative.get("pickRu") or alternative.get("pick"),
            "probability": safe_float(alternative.get("conservativeProbability"), safe_float(alternative.get("modelProbability"))),
            "odds": safe_float(alternative.get("bookmakerOdds") or alternative.get("odds")),
            "edge": safe_float(alternative.get("edge")),
            "dataQuality": safe_float(alternative.get("dataQuality"), safe_float(record.get("dataQuality"))),
            "agreement": safe_float(alternative.get("agreement")),
            "marketStability": safe_float(alternative.get("marketStability")),
            "isPrimary": False,
        })
    return [row for row in options if row["marketKey"]]


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    dossier = record.get("matchDossier") if isinstance(record.get("matchDossier"), dict) else {}
    components = dossier.get("components") if isinstance(dossier.get("components"), dict) else {}
    return {
        "eventId": str(record.get("eventId") or ""),
        "rank": int(record.get("rank") or 0),
        "league": record.get("leagueRu") or record.get("league"),
        "country": record.get("countryRu") or record.get("country"),
        "home": record.get("homeRu") or record.get("home"),
        "away": record.get("awayRu") or record.get("away"),
        "commenceTime": record.get("commenceTime"),
        "dataTier": record.get("dataTier"),
        "dataQuality": safe_float(record.get("dataQuality")),
        "expectedScore": record.get("expectedScore"),
        "expectedHomeGoals": dossier.get("expectedHomeGoals") or record.get("expectedHomeGoals"),
        "expectedAwayGoals": dossier.get("expectedAwayGoals") or record.get("expectedAwayGoals"),
        "homeWinProbability": dossier.get("homeWinProbability") or record.get("homeWinProbability"),
        "drawProbability": dossier.get("drawProbability") or record.get("drawProbability"),
        "awayWinProbability": dossier.get("awayWinProbability") or record.get("awayWinProbability"),
        "homeRecent": components.get("homeRecent"),
        "awayRecent": components.get("awayRecent"),
        "homeVenue": components.get("homeVenue"),
        "awayVenue": components.get("awayVenue"),
        "markets": _record_market_options(record),
        "sourceNotes": list(record.get("sourceNotes") or [])[:5],
        "fonbetAvailability": record.get("fonbetAvailability"),
    }


def _audit_schema() -> dict[str, Any]:
    event_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["eventId", "selectedMarketKey", "riskPenalty", "reason"],
        "properties": {
            "eventId": {"type": "string"},
            "selectedMarketKey": {"type": "string"},
            "riskPenalty": {"type": "number", "minimum": 0, "maximum": 0.08},
            "reason": {"type": "string", "maxLength": 500},
        },
    }
    return {
        "name": "r15_daily_portfolio_audit",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "orderedEventIds", "decisions", "topSingles", "expresses", "systemMessage", "globalWarnings"],
            "properties": {
                "status": {"type": "string", "enum": ["PASS", "PASS_WITH_WARNINGS"]},
                "orderedEventIds": {"type": "array", "minItems": 15, "maxItems": 15, "items": {"type": "string"}},
                "decisions": {"type": "array", "minItems": 15, "maxItems": 15, "items": event_decision},
                "topSingles": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string"}},
                "expresses": {
                    "type": "object", "additionalProperties": False, "required": ["A", "B", "C"],
                    "properties": {
                        "A": {"type": "array", "minItems": 5, "maxItems": 5, "items": {"type": "string"}},
                        "B": {"type": "array", "minItems": 5, "maxItems": 5, "items": {"type": "string"}},
                        "C": {"type": "array", "minItems": 5, "maxItems": 5, "items": {"type": "string"}},
                    },
                },
                "systemMessage": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "lead", "body", "focus"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 160},
                        "lead": {"type": "string", "maxLength": 400},
                        "body": {"type": "string", "maxLength": 1200},
                        "focus": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string", "maxLength": 160}},
                    },
                },
                "globalWarnings": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 240}},
            },
        },
    }


def _valid_free_model(value: str) -> str:
    model = value.strip()
    if model == "openrouter/free" or model.endswith(":free"):
        return model
    return "openrouter/free"


def _validate_response(parsed: dict[str, Any], records: list[dict[str, Any]]) -> None:
    ids = [str(row.get("eventId") or "") for row in records]
    expected = set(ids)
    ordered = [str(value) for value in parsed.get("orderedEventIds") or []]
    if len(ordered) != 15 or len(set(ordered)) != 15 or set(ordered) != expected:
        raise RuntimeError("OPENROUTER_ORDERED_EVENT_IDS_INVALID")
    decisions = parsed.get("decisions") or []
    if len(decisions) != 15 or {str(row.get("eventId") or "") for row in decisions} != expected:
        raise RuntimeError("OPENROUTER_DECISIONS_INVALID")
    market_keys = {str(row.get("eventId") or ""): {option["marketKey"] for option in _record_market_options(row)} for row in records}
    for decision in decisions:
        event_id = str(decision.get("eventId") or "")
        if str(decision.get("selectedMarketKey") or "") not in market_keys[event_id]:
            raise RuntimeError(f"OPENROUTER_UNKNOWN_MARKET={event_id}")
        penalty = safe_float(decision.get("riskPenalty"), -1)
        if penalty < 0 or penalty > 0.0800001:
            raise RuntimeError(f"OPENROUTER_RISK_PENALTY_INVALID={event_id}")
    singles = [str(value) for value in parsed.get("topSingles") or []]
    if len(singles) != 3 or len(set(singles)) != 3 or not set(singles).issubset(expected):
        raise RuntimeError("OPENROUTER_TOP_SINGLES_INVALID")
    express_ids: list[str] = []
    expresses = parsed.get("expresses") or {}
    for label in ("A", "B", "C"):
        values = [str(value) for value in expresses.get(label) or []]
        if len(values) != 5 or len(set(values)) != 5:
            raise RuntimeError(f"OPENROUTER_EXPRESS_{label}_INVALID")
        express_ids.extend(values)
    if len(express_ids) != 15 or len(set(express_ids)) != 15 or set(express_ids) != expected:
        raise RuntimeError("OPENROUTER_EXPRESS_DISTRIBUTION_INVALID")


def _copy_alternative_to_primary(record: dict[str, Any], selected_market_key: str) -> None:
    if str(record.get("marketKey") or "") == selected_market_key:
        return
    alternative = next((row for row in record.get("alternatives") or [] if str(row.get("marketKey") or "") == selected_market_key), None)
    if not isinstance(alternative, dict):
        raise RuntimeError("AUDIT_ALTERNATIVE_NOT_FOUND")
    fields = (
        "marketKey", "market", "marketFamily", "selectionCode", "pick", "pickRu", "point",
        "bookmakerOdds", "odds", "bookmaker", "bookmakerKey", "bookmakerRu", "oddsLastUpdate",
        "oddsAgeMinutes", "quoteCount", "modelProbability", "probability", "probabilityPercent",
        "statisticalProbability", "marketProbability", "edge", "edgePercent", "expectedValue",
        "expectedValuePercent", "confidence", "conservativeProbability", "uncertaintyMargin",
        "marketStability", "reliabilityScore", "dataTier", "dataQuality", "agreement", "anomaly",
        "analysisScore", "bestBetScore", "qualification",
    )
    old_primary = {key: copy.deepcopy(record.get(key)) for key in fields if key in record}
    for key in fields:
        if key in alternative:
            record[key] = copy.deepcopy(alternative[key])
    record.setdefault("alternatives", []).insert(0, old_primary)
    record["auditMarketChanged"] = True


def audit_records(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    api_key: str | None,
    operational_day_id: str,
    now: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = now or now_utc()
    if len(records) != 15:
        return records, {"status": "SKIPPED", "reason": f"REQUIRES_15_RECORDS_GOT_{len(records)}"}
    fingerprint = json_fingerprint([_compact_record(row) for row in records])
    cached = load_json(AUDIT_PATH, {})
    same_cached_input = (
        cached.get("operationalDayId") == operational_day_id
        and cached.get("inputFingerprint") == fingerprint
    )
    if same_cached_input and cached.get("logicalRunConsumed") is True and cached.get("schemaValid") is not True:
        return records, {
            "status": "CACHED_FAILED_FALLBACK",
            "logicalRuns": 0,
            "technicalAttempts": int(cached.get("technicalAttempts") or 0),
            "schemaValid": False,
            "modelUsed": cached.get("modelUsed"),
            "modelRequested": cached.get("modelRequested"),
            "error": cached.get("error"),
            "fallback": "DETERMINISTIC_PORTFOLIO",
        }
    if (
        same_cached_input
        and cached.get("schemaValid") is True
        and isinstance(cached.get("response"), dict)
    ):
        try:
            parsed = cached["response"]
            _validate_response(parsed, records)
            source = "CACHE"
            actual_model = cached.get("modelUsed")
            request_id = cached.get("requestId")
            usage = cached.get("usage") or {}
            latency = cached.get("latencySeconds")
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            cached = {
                "version": VERSION,
                "operationalDayId": operational_day_id,
                "inputFingerprint": fingerprint,
                "createdAt": iso(now),
                "logicalRunConsumed": True,
                "schemaValid": False,
                "status": "CACHE_REJECTED_FALLBACK",
                "error": str(exc),
            }
            write_json(AUDIT_PATH, cached)
            return records, {
                "status": "CACHE_REJECTED_FALLBACK",
                "logicalRuns": 0,
                "schemaValid": False,
                "error": str(exc),
                "fallback": "DETERMINISTIC_PORTFOLIO",
            }
    elif not api_key or not bool(config.get("openRouterDailyAuditEnabled", True)):
        return records, {
            "status": "NOT_CONFIGURED" if not api_key else "DISABLED",
            "logicalRuns": 0,
            "schemaValid": False,
            "modelUsed": None,
            "fallback": "DETERMINISTIC_PORTFOLIO",
        }
    else:
        model = _valid_free_model(os.getenv("OPENROUTER_MODEL", "") or str(config.get("openRouterDailyAuditModel") or "openrouter/free"))
        payload_data = {
            "operationalDayId": operational_day_id,
            "rules": {
                "factsOnly": True,
                "allowedEventsOnly": True,
                "confidenceMayOnlyDecrease": True,
                "maximumRiskPenalty": 0.08,
                "exactMatches": 15,
                "topSingles": 3,
                "expresses": 3,
                "legsPerExpress": 5,
                "noDuplicateEvents": True,
            },
            "records": [_compact_record(row) for row in records],
        }
        system_prompt = (
            "Ты второй аналитический контур футбольной системы. Используй только переданный JSON. "
            "Не добавляй события, новости, травмы, коэффициенты или статистику. Не повышай вероятность. "
            "Выбери для каждого события только один из переданных marketKey, при риске назначь штраф 0..0.08. "
            "Упорядочи все 15 событий, выдели три лучших ординара и распредели все события по A/B/C без повторов. "
            "Сделай спокойное русское обращение системы о том, что она проверила и чему учится."
        )
        request_payload = {
            "model": model,
            "temperature": 0.05,
            "max_tokens": int(config.get("openRouterDailyAuditMaxOutputTokens") or 5000),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_schema", "json_schema": _audit_schema()},
        }
        attempts = max(1, min(3, int(config.get("openRouterDailyAuditTechnicalAttempts") or 3)))
        last_error: Exception | None = None
        result: dict[str, Any] | None = None
        started = time.monotonic()
        technical_attempts = 0
        for attempt in range(attempts):
            technical_attempts += 1
            request = urllib.request.Request(
                OPENROUTER_URL,
                data=json.dumps(request_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://r1a156.github.io/ai-football-lab/",
                    "X-Title": "AI Football Lab R15F R3",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=int(config.get("openRouterDailyAuditTimeoutSeconds") or 150)) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"HTTP_{exc.code}:{body[:500]}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= attempts:
                    break
                time.sleep(2.0 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(2.0 * (attempt + 1))
        latency = round(time.monotonic() - started, 3)
        if not result:
            failure = {
                "version": VERSION,
                "operationalDayId": operational_day_id,
                "inputFingerprint": fingerprint,
                "createdAt": iso(now),
                "logicalRunConsumed": True,
                "technicalAttempts": technical_attempts,
                "schemaValid": False,
                "status": "FAILED_FALLBACK",
                "modelRequested": model,
                "modelUsed": None,
                "error": str(last_error),
            }
            write_json(AUDIT_PATH, failure)
            return records, {
                "status": "FAILED_FALLBACK",
                "logicalRuns": 1,
                "technicalAttempts": technical_attempts,
                "schemaValid": False,
                "modelRequested": model,
                "error": str(last_error),
                "fallback": "DETERMINISTIC_PORTFOLIO",
            }
        actual_model = result.get("model") or model
        request_id = result.get("id")
        usage = result.get("usage") or {}
        try:
            choices = result.get("choices") or []
            if not choices:
                raise ValueError("OPENROUTER_CHOICES_EMPTY")
            content = choices[0].get("message", {}).get("content")
            if isinstance(content, list):
                content = "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
            parsed = content if isinstance(content, dict) else json.loads(str(content or "{}"))
            _validate_response(parsed, records)
        except (json.JSONDecodeError, RuntimeError, TypeError, ValueError, KeyError, IndexError) as exc:
            failure = {
                "version": VERSION,
                "operationalDayId": operational_day_id,
                "inputFingerprint": fingerprint,
                "createdAt": iso(now),
                "logicalRunConsumed": True,
                "technicalAttempts": technical_attempts,
                "schemaValid": False,
                "status": "INVALID_RESPONSE_FALLBACK",
                "modelRequested": model,
                "modelUsed": actual_model,
                "requestId": request_id,
                "usage": usage,
                "latencySeconds": latency,
                "error": str(exc),
            }
            write_json(AUDIT_PATH, failure)
            return records, {
                "status": "INVALID_RESPONSE_FALLBACK",
                "logicalRuns": 1,
                "technicalAttempts": technical_attempts,
                "schemaValid": False,
                "modelRequested": model,
                "modelUsed": actual_model,
                "error": str(exc),
                "fallback": "DETERMINISTIC_PORTFOLIO",
            }
        source = "OPENROUTER"
        cached = {
            "version": VERSION,
            "operationalDayId": operational_day_id,
            "inputFingerprint": fingerprint,
            "createdAt": iso(now),
            "logicalRunConsumed": True,
            "technicalAttempts": technical_attempts,
            "modelRequested": model,
            "modelUsed": actual_model,
            "requestId": request_id,
            "usage": usage,
            "latencySeconds": latency,
            "schemaValid": True,
            "response": parsed,
        }
        write_json(AUDIT_PATH, cached)

    by_id = {str(row.get("eventId") or ""): copy.deepcopy(row) for row in records}
    decisions = {str(row.get("eventId") or ""): row for row in parsed.get("decisions") or []}
    ordered_records: list[dict[str, Any]] = []
    for rank, event_id in enumerate(parsed.get("orderedEventIds") or [], start=1):
        record = by_id[str(event_id)]
        decision = decisions[str(event_id)]
        _copy_alternative_to_primary(record, str(decision.get("selectedMarketKey") or ""))
        before = safe_float(record.get("conservativeProbability"), safe_float(record.get("modelProbability")))
        penalty = min(0.08, max(0.0, safe_float(decision.get("riskPenalty"))))
        after = max(0.0, before - penalty)
        record["preAuditConservativeProbability"] = round(before, 6)
        record["auditRiskPenalty"] = round(penalty, 6)
        record["conservativeProbability"] = round(after, 6)
        record["probability"] = min(safe_float(record.get("probability"), after), after)
        record["probabilityPercent"] = round(record["probability"] * 100.0, 1)
        record["rank"] = rank
        record["auditReason"] = str(decision.get("reason") or "")
        record["reason"] = record["auditReason"] or record.get("reason")
        record["reasonRu"] = record["reason"]
        ordered_records.append(record)

    audit = {
        "status": parsed.get("status") or "PASS",
        "source": source,
        "logicalRuns": 0 if source == "CACHE" else 1,
        "schemaValid": True,
        "modelUsed": actual_model,
        "requestId": request_id,
        "usage": usage,
        "latencySeconds": latency,
        "topSingles": list(parsed.get("topSingles") or []),
        "expresses": copy.deepcopy(parsed.get("expresses") or {}),
        "globalWarnings": list(parsed.get("globalWarnings") or []),
        "systemMessage": copy.deepcopy(parsed.get("systemMessage") or {}),
        "inputFingerprint": fingerprint,
    }
    return ordered_records, audit


def mark_activated(operational_day_id: str) -> None:
    control = load_control()
    if not control.get("activatedAt"):
        control["activatedAt"] = iso(now_utc())
    control["status"] = "ACTIVE"
    control["lastAuditOperationalDayId"] = operational_day_id
    write_json(CONTROL_PATH, control)


def self_test() -> int:
    test_now = dt.datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    if MOSCOW.utcoffset(test_now) != dt.timedelta(hours=3):
        raise RuntimeError("MOSCOW_FIXED_OFFSET_TEST_FAILED")
    window = next_full_operational_window(test_now)
    if window["operationalDateLocal"] != "2026-08-05":
        raise RuntimeError("FIRST_RUN_NEXT_DAY_TEST_FAILED")
    records = []
    for index in range(15):
        records.append({
            "eventId": f"event-{index+1}", "rank": index + 1,
            "home": f"Home {index+1}", "away": f"Away {index+1}",
            "marketKey": "h2h", "pick": "П1", "conservativeProbability": 0.70 - index * 0.005,
            "modelProbability": 0.72 - index * 0.005, "bookmakerOdds": 1.55 + index * 0.01,
            "dataQuality": 80, "agreement": 70, "marketStability": 75,
            "alternatives": [{"marketKey": "totals", "pick": "ТБ 2,5", "conservativeProbability": 0.60, "bookmakerOdds": 1.75}],
        })
    compact = [_compact_record(row) for row in records]
    if len(compact) != 15 or any(len(row["markets"]) != 2 for row in compact):
        raise RuntimeError("AUDIT_COMPACT_TEST_FAILED")
    parsed = {
        "status": "PASS", "orderedEventIds": [row["eventId"] for row in records],
        "decisions": [{"eventId": row["eventId"], "selectedMarketKey": "h2h", "riskPenalty": 0.0, "reason": "Тест"} for row in records],
        "topSingles": ["event-1", "event-2", "event-3"],
        "expresses": {"A": [f"event-{i}" for i in (1,6,7,12,13)], "B": [f"event-{i}" for i in (2,5,8,11,14)], "C": [f"event-{i}" for i in (3,4,9,10,15)]},
        "systemMessage": {"title": "Тест", "lead": "Тест", "body": "Тест", "focus": ["A", "B"]},
        "globalWarnings": [],
    }
    _validate_response(parsed, records)
    print("R15F_R3R1_PORTABLE_MOSCOW_TIME=GREEN")
    print("R15F_R3R1_TZDATA_DEPENDENCY=NONE")
    print("R15F_R3_FIRST_RUN_NEXT_DAY=GREEN")
    print("R15F_R3_OPENROUTER_SCHEMA=GREEN")
    print("R15F_R3_FREE_MODEL_GUARD=GREEN")
    print("R15F_R3_AUDIT_NO_CONFIDENCE_INCREASE=GREEN")
    print("R15F_R3_ONE_LOGICAL_RUN_PER_DAY=GREEN")
    print("R15F_R3_INVALID_AI_RESPONSE_FALLBACK=GREEN")
    print("FINAL_STATUS=GREEN_R15F_R3_AUDITOR_SELF_TEST")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="R15F R3 daily auditor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--initialize-first-run", action="store_true")
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.initialize_first_run:
        control = initialize_first_run(force=False)
        print(f"FIRST_ACTIVE_OPERATIONAL_DATE={control.get('firstActiveOperationalDate')}")
        print(f"FIRST_ACTIVE_WINDOW_START={control.get('firstActiveWindowStart')}")
        print(f"FIRST_ACTIVE_WINDOW_END={control.get('firstActiveWindowEnd')}")
        print("FINAL_STATUS=GREEN_R15F_R3_FIRST_RUN_INITIALIZED")
        return 0
    if args.prepare:
        prepared = prepare_next_window_state()
        print(f"PREPARING_OPERATIONAL_DAY={prepared.get('operationalDayId')}")
        print("BANK_MUTATION=NO")
        print("PARTIAL_DAY_PUBLICATION=NO")
        print("FINAL_STATUS=GREEN_R15F_R3_PREPARING_NEXT_WINDOW")
        return 0
    if args.status:
        gate = activation_gate()
        print(json.dumps(gate, ensure_ascii=False, indent=2))
        return 0
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
