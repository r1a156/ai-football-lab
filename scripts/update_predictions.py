#!/usr/bin/env python3
"""
AI Football Lab
Автоматическое получение матчей, подготовка статистики,
анализ через OpenRouter и обновление публичного состояния сайта.

Используются только модули стандартной библиотеки Python.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "analysis.json"
STATE_PATH = ROOT / "data" / "state.json"
REPORT_PATH = ROOT / "data" / "last-update-report.json"

FOOTBALL_API_BASE = "https://api.football-data.org/v4"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
PUBLIC_SITE_URL = "https://r1a156.github.io/ai-football-lab/"

HTTP_TIMEOUT_SECONDS = 60
REQUEST_RETRIES = 3


MARKET_LABELS = {
    "HOME_WIN": "Победа хозяев",
    "AWAY_WIN": "Победа гостей",
    "HOME_OR_DRAW": "Хозяева не проиграют",
    "AWAY_OR_DRAW": "Гости не проиграют",
    "OVER_1_5": "Тотал больше 1,5",
    "UNDER_3_5": "Тотал меньше 3,5",
    "HOME_OVER_0_5": "Хозяева забьют",
    "AWAY_OVER_0_5": "Гости забьют",
    "BOTH_SCORE": "Обе команды забьют",
}


COUNTRY_TRANSLATIONS = {
    "England": "Англия",
    "Spain": "Испания",
    "Germany": "Германия",
    "Italy": "Италия",
    "France": "Франция",
    "Netherlands": "Нидерланды",
    "Portugal": "Португалия",
    "Brazil": "Бразилия",
}


def log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp} UTC] {message}", flush=True)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Файл не найден: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(f"Корень JSON должен быть объектом: {path}")

    return data


def write_json_atomic(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(path.suffix + ".tmp")

    serialized = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )

    temporary_path.write_text(
        serialized + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)


def require_environment_variable(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Не задан обязательный секрет или параметр окружения: {name}"
        )

    return value


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "AI-Football-Lab/2.0",
    }

    if headers:
        request_headers.update(headers)

    body: bytes | None = None

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    last_error: Exception | None = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=request_headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw)

                if not isinstance(parsed, dict):
                    raise RuntimeError(
                        f"API вернул неожиданный тип данных: {url}"
                    )

                return parsed

        except urllib.error.HTTPError as error:
            response_body = ""

            try:
                response_body = error.read().decode(
                    "utf-8",
                    errors="replace",
                )
            except Exception:
                response_body = ""

            last_error = RuntimeError(
                f"HTTP {error.code} для {url}: "
                f"{response_body[:700]}"
            )

            if error.code not in {429, 500, 502, 503, 504}:
                raise last_error

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as error:
            last_error = error

        if attempt < REQUEST_RETRIES:
            delay = attempt * 5
            log(
                f"Повтор запроса через {delay} секунд. "
                f"Попытка {attempt}/{REQUEST_RETRIES}"
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Запрос завершился ошибкой после "
        f"{REQUEST_RETRIES} попыток: {last_error}"
    )


def iso_date(value: dt.date) -> str:
    return value.isoformat()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc_datetime(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)

    return parsed.astimezone(dt.timezone.utc)


def fetch_matches(
    api_key: str,
    *,
    date_from: dt.date,
    date_to: dt.date,
    competitions: list[str],
) -> list[dict[str, Any]]:
    parameters = {
        "dateFrom": iso_date(date_from),
        "dateTo": iso_date(date_to),
        "competitions": ",".join(competitions),
    }

    url = (
        f"{FOOTBALL_API_BASE}/matches?"
        f"{urllib.parse.urlencode(parameters)}"
    )

    log(
        "Получение матчей "
        f"{date_from.isoformat()} — {date_to.isoformat()}"
    )

    response = request_json(
        url,
        headers={
            "X-Auth-Token": api_key,
        },
    )

    matches = response.get("matches", [])

    if not isinstance(matches, list):
        raise RuntimeError(
            "Football-Data вернул некорректный список matches"
        )

    return [
        match
        for match in matches
        if isinstance(match, dict)
    ]


def competition_is_allowed(
    match: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    competition = match.get("competition") or {}

    competition_type = str(
        competition.get("type") or ""
    ).upper()

    if competition_type and competition_type != "LEAGUE":
        return False

    competition_name = str(
        competition.get("name") or ""
    ).lower()

    excluded_words = config.get(
        "excludedCompetitionWords",
        [],
    )

    for word in excluded_words:
        if str(word).lower() in competition_name:
            return False

    return True


def get_team_id(match: dict[str, Any], side: str) -> int | None:
    team = match.get(side) or {}
    value = team.get("id")

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_team_name(match: dict[str, Any], side: str) -> str:
    team = match.get(side) or {}

    return str(
        team.get("shortName")
        or team.get("name")
        or team.get("tla")
        or "Неизвестная команда"
    )


def final_score(match: dict[str, Any]) -> tuple[int, int] | None:
    if str(match.get("status") or "").upper() != "FINISHED":
        return None

    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}

    home = full_time.get("home")
    away = full_time.get("away")

    if home is None or away is None:
        return None

    try:
        return int(home), int(away)
    except (TypeError, ValueError):
        return None


def build_team_form(
    finished_matches: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    form: dict[int, dict[str, Any]] = {}

    sorted_matches = sorted(
        finished_matches,
        key=lambda item: str(item.get("utcDate") or ""),
    )

    for match in sorted_matches:
        result = final_score(match)

        if result is None:
            continue

        home_goals, away_goals = result
        home_id = get_team_id(match, "homeTeam")
        away_id = get_team_id(match, "awayTeam")

        if home_id is None or away_id is None:
            continue

        home_points = (
            3 if home_goals > away_goals
            else 1 if home_goals == away_goals
            else 0
        )

        away_points = (
            3 if away_goals > home_goals
            else 1 if away_goals == home_goals
            else 0
        )

        home_entry = form.setdefault(
            home_id,
            {
                "matches": [],
            },
        )

        away_entry = form.setdefault(
            away_id,
            {
                "matches": [],
            },
        )

        home_entry["matches"].append(
            {
                "points": home_points,
                "goalsFor": home_goals,
                "goalsAgainst": away_goals,
                "venue": "HOME",
                "date": match.get("utcDate"),
            }
        )

        away_entry["matches"].append(
            {
                "points": away_points,
                "goalsFor": away_goals,
                "goalsAgainst": home_goals,
                "venue": "AWAY",
                "date": match.get("utcDate"),
            }
        )

    summarized: dict[int, dict[str, Any]] = {}

    for team_id, entry in form.items():
        recent = entry["matches"][-6:]

        games = len(recent)
        points = sum(item["points"] for item in recent)
        goals_for = sum(item["goalsFor"] for item in recent)
        goals_against = sum(
            item["goalsAgainst"]
            for item in recent
        )

        scored_games = sum(
            1 for item in recent
            if item["goalsFor"] > 0
        )

        conceded_games = sum(
            1 for item in recent
            if item["goalsAgainst"] > 0
        )

        over_15_games = sum(
            1 for item in recent
            if (
                item["goalsFor"]
                + item["goalsAgainst"]
            ) >= 2
        )

        both_score_games = sum(
            1 for item in recent
            if (
                item["goalsFor"] > 0
                and item["goalsAgainst"] > 0
            )
        )

        summarized[team_id] = {
            "games": games,
            "points": points,
            "pointsPerGame": round(
                points / games,
                2,
            ) if games else 0,
            "goalsFor": goals_for,
            "goalsAgainst": goals_against,
            "goalsForPerGame": round(
                goals_for / games,
                2,
            ) if games else 0,
            "goalsAgainstPerGame": round(
                goals_against / games,
                2,
            ) if games else 0,
            "scoredRate": round(
                scored_games / games,
                2,
            ) if games else 0,
            "concededRate": round(
                conceded_games / games,
                2,
            ) if games else 0,
            "over15Rate": round(
                over_15_games / games,
                2,
            ) if games else 0,
            "bothScoreRate": round(
                both_score_games / games,
                2,
            ) if games else 0,
        }

    return summarized


def candidate_quality_score(
    home_form: dict[str, Any],
    away_form: dict[str, Any],
) -> float:
    home_games = int(home_form.get("games") or 0)
    away_games = int(away_form.get("games") or 0)

    data_coverage = min(
        home_games + away_games,
        12,
    ) / 12

    home_ppg = float(
        home_form.get("pointsPerGame") or 0
    )

    away_ppg = float(
        away_form.get("pointsPerGame") or 0
    )

    form_difference = min(
        abs(home_ppg - away_ppg) / 3,
        1,
    )

    home_over = float(
        home_form.get("over15Rate") or 0
    )

    away_over = float(
        away_form.get("over15Rate") or 0
    )

    goal_signal = (
        home_over + away_over
    ) / 2

    return round(
        data_coverage * 55
        + form_difference * 20
        + goal_signal * 25,
        2,
    )


def build_candidates(
    scheduled_matches: list[dict[str, Any]],
    team_form: dict[int, dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    minimum_team_matches = max(
        1,
        int(config.get("minimumTeamMatches") or 1),
    )

    for match in scheduled_matches:
        if not competition_is_allowed(match, config):
            continue

        match_id = match.get("id")
        home_id = get_team_id(match, "homeTeam")
        away_id = get_team_id(match, "awayTeam")

        if match_id is None or home_id is None or away_id is None:
            continue

        home_form = team_form.get(
            home_id,
            {
                "games": 0,
            },
        )

        away_form = team_form.get(
            away_id,
            {
                "games": 0,
            },
        )

        # Для честного анализа нужны хотя бы некоторые
        # недавние данные по обеим командам.
        if (
            int(home_form.get("games") or 0) < minimum_team_matches
            or int(away_form.get("games") or 0) < minimum_team_matches
        ):
            continue

        competition = match.get("competition") or {}
        area = match.get("area") or {}

        candidate = {
            "matchId": int(match_id),
            "utcDate": str(match.get("utcDate") or ""),
            "competition": {
                "code": str(
                    competition.get("code") or ""
                ),
                "name": str(
                    competition.get("name")
                    or "Неизвестная лига"
                ),
            },
            "country": str(
                area.get("name") or ""
            ),
            "homeTeam": {
                "id": home_id,
                "name": get_team_name(
                    match,
                    "homeTeam",
                ),
                "form": home_form,
            },
            "awayTeam": {
                "id": away_id,
                "name": get_team_name(
                    match,
                    "awayTeam",
                ),
                "form": away_form,
            },
        }

        candidate["dataQuality"] = candidate_quality_score(
            home_form,
            away_form,
        )

        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -float(item.get("dataQuality") or 0),
            str(item.get("utcDate") or ""),
        )
    )

    maximum_candidates = int(
        config.get("maximumCandidates") or 12
    )

    return candidates[:maximum_candidates]


def build_analysis_prompt(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    allowed_markets = config.get(
        "allowedMarkets",
        [],
    )

    minimum_confidence = int(
        config.get("minimumConfidence") or 68
    )

    maximum_predictions = int(
        config.get("maximumPredictions") or 5
    )

    candidate_json = json.dumps(
        candidates,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"""
Ты являешься строгим аналитическим модулем футбольных матчей.

Работай только с переданными числовыми данными.
Не используй внешние знания, слухи, составы или коэффициенты.
Не выдумывай отсутствующие факты.

ЗАДАЧА:
Отбери от 0 до {maximum_predictions} наиболее обоснованных прогнозов.

Если ни один матч не имеет достаточного статистического основания,
верни пустой массив predictions.

Минимальная допустимая уверенность:
{minimum_confidence} из 100.

Разрешённые значения market:
{json.dumps(allowed_markets, ensure_ascii=False)}

ОГРАНИЧЕНИЯ:
1. Один прогноз на один matchId.
2. Не выбирай матч только ради заполнения количества.
3. Учитывай малый объём выборки как фактор риска.
4. confidence — целое число от 0 до 100.
5. probability — число от 0.50 до 0.90.
6. risk должен быть только LOW или MEDIUM.
7. reason — краткое объяснение на русском языке,
   основанное только на переданных показателях.
8. Не возвращай markdown.
9. Не возвращай комментарии вне JSON.
10. Не придумывай рыночные букмекерские коэффициенты.

Верни строго JSON следующей структуры:

{{
  "predictions": [
    {{
      "matchId": 123,
      "market": "OVER_1_5",
      "confidence": 74,
      "probability": 0.72,
      "risk": "LOW",
      "reason": "Краткое статистическое основание."
    }}
  ]
}}

МАТЧИ И СТАТИСТИКА:
{candidate_json}
""".strip()


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned,
        )

    try:
        parsed = json.loads(cleaned)

        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start < 0 or end <= start:
        raise RuntimeError(
            "OpenRouter не вернул JSON-объект"
        )

    fragment = cleaned[start:end + 1]
    parsed = json.loads(fragment)

    if not isinstance(parsed, dict):
        raise RuntimeError(
            "Ответ модели не является JSON-объектом"
        )

    return parsed


def call_openrouter(
    api_key: str,
    prompt: str,
) -> tuple[dict[str, Any], str]:
    model = os.getenv(
        "OPENROUTER_MODEL",
        "",
    ).strip()

    payload: dict[str, Any] = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты строгий статистический аналитический "
                    "модуль. Возвращай только корректный JSON."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.15,
        "max_tokens": 3000,
    }

    # Если модель не задана, OpenRouter использует
    # модель по умолчанию, настроенную в аккаунте.
    if model:
        payload["model"] = model

    response = request_json(
        OPENROUTER_API_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": PUBLIC_SITE_URL,
            "X-OpenRouter-Title": "AI Football Lab",
        },
        payload=payload,
    )

    choices = response.get("choices") or []

    if not choices:
        raise RuntimeError(
            "OpenRouter не вернул choices"
        )

    first_choice = choices[0] or {}
    message = first_choice.get("message") or {}
    content = message.get("content")

    if isinstance(content, list):
        text_parts: list[str] = []

        for item in content:
            if isinstance(item, dict):
                value = item.get("text")

                if value:
                    text_parts.append(str(value))

        content = "\n".join(text_parts)

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            "OpenRouter вернул пустой ответ"
        )

    returned_model = str(
        response.get("model")
        or model
        or "default"
    )

    return extract_json_object(content), returned_model


def normalize_model_predictions(
    model_result: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_predictions = model_result.get(
        "predictions",
        [],
    )

    if not isinstance(raw_predictions, list):
        raise RuntimeError(
            "Поле predictions должно быть массивом"
        )

    candidates_by_id = {
        int(candidate["matchId"]): candidate
        for candidate in candidates
    }

    allowed_markets = {
        str(value)
        for value in config.get(
            "allowedMarkets",
            [],
        )
    }

    minimum_confidence = int(
        config.get("minimumConfidence") or 68
    )

    maximum_predictions = int(
        config.get("maximumPredictions") or 5
    )

    normalized: list[dict[str, Any]] = []
    used_match_ids: set[int] = set()

    for raw in raw_predictions:
        if not isinstance(raw, dict):
            continue

        try:
            match_id = int(raw.get("matchId"))
            confidence = int(raw.get("confidence"))
            probability = float(raw.get("probability"))
        except (TypeError, ValueError):
            continue

        market = str(
            raw.get("market") or ""
        ).upper()

        risk = str(
            raw.get("risk") or ""
        ).upper()

        reason = str(
            raw.get("reason") or ""
        ).strip()

        if match_id not in candidates_by_id:
            continue

        if match_id in used_match_ids:
            continue

        if market not in allowed_markets:
            continue

        if confidence < minimum_confidence:
            continue

        confidence = max(
            0,
            min(confidence, 100),
        )

        probability = max(
            0.50,
            min(probability, 0.90),
        )

        if risk not in {"LOW", "MEDIUM"}:
            risk = (
                "LOW"
                if confidence >= 76
                else "MEDIUM"
            )

        if not reason:
            continue

        # Расчётный справедливый коэффициент.
        # Это не коэффициент букмекерской конторы.
        fair_odds = round(
            1 / probability,
            2,
        )

        normalized.append(
            {
                "matchId": match_id,
                "market": market,
                "confidence": confidence,
                "probability": round(
                    probability,
                    4,
                ),
                "fairOdds": fair_odds,
                "risk": risk,
                "reason": reason[:500],
            }
        )

        used_match_ids.add(match_id)

    normalized.sort(
        key=lambda item: (
            -int(item["confidence"]),
            -float(item["probability"]),
        )
    )

    return normalized[:maximum_predictions]


def evaluate_market(
    market: str,
    home_goals: int,
    away_goals: int,
) -> bool:
    if market == "HOME_WIN":
        return home_goals > away_goals

    if market == "AWAY_WIN":
        return away_goals > home_goals

    if market == "HOME_OR_DRAW":
        return home_goals >= away_goals

    if market == "AWAY_OR_DRAW":
        return away_goals >= home_goals

    if market == "OVER_1_5":
        return home_goals + away_goals >= 2

    if market == "UNDER_3_5":
        return home_goals + away_goals <= 3

    if market == "HOME_OVER_0_5":
        return home_goals >= 1

    if market == "AWAY_OVER_0_5":
        return away_goals >= 1

    if market == "BOTH_SCORE":
        return home_goals >= 1 and away_goals >= 1

    raise RuntimeError(
        f"Неизвестный рынок: {market}"
    )


def ensure_real_state(
    old_state: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    current_mode = str(
        (old_state.get("meta") or {}).get("mode")
        or ""
    )

    if current_mode == "real":
        state = copy.deepcopy(old_state)

        state.setdefault("predictions", [])
        state.setdefault("history", [])
        state.setdefault("statistics", {})
        state.setdefault("bank", {})

        return state

    starting_bank = float(
        config.get("startingVirtualBank")
        or 10000
    )

    log(
        "Обнаружен демонстрационный режим. "
        "Создаётся чистая реальная статистика."
    )

    return {
        "meta": {
            "version": "2.0.0",
            "mode": "real",
            "updatedAt": None,
            "analyzedMatches": 0,
            "source": "football-data.org",
            "analysisProvider": "OpenRouter",
            "notice": (
                "Расчётные коэффициенты не являются "
                "коэффициентами букмекерских контор."
            ),
        },
        "bank": {
            "starting": starting_bank,
            "current": starting_bank,
            "stakePercent": int(
                config.get(
                    "maximumTotalStakePercent",
                )
                or 20
            ),
            "roi": 0,
            "maxDrawdown": 0,
            "history": [
                {
                    "date": utc_now().date().isoformat(),
                    "value": starting_bank,
                    "event": "REAL_MODE_START",
                }
            ],
        },
        "statistics": {
            "averageOdds": 0,
            "currentStreak": "Нет завершённых прогнозов",
            "bestSegment": "Недостаточно данных",
        },
        "predictions": [],
        "history": [],
    }


def resolve_existing_history(
    state: dict[str, Any],
    matches_by_id: dict[int, dict[str, Any]],
) -> int:
    history = state.get("history") or []
    bank = state.get("bank") or {}

    current_bank = float(
        bank.get("current")
        or bank.get("starting")
        or 10000
    )

    settled_count = 0

    for entry in history:
        if not isinstance(entry, dict):
            continue

        if entry.get("status") != "pending":
            continue

        match_id = entry.get("sourceMatchId")

        try:
            match_id = int(match_id)
        except (TypeError, ValueError):
            continue

        match = matches_by_id.get(match_id)

        if not match:
            continue

        result = final_score(match)

        if result is None:
            continue

        home_goals, away_goals = result

        market = str(
            entry.get("market") or ""
        )

        won = evaluate_market(
            market,
            home_goals,
            away_goals,
        )

        stake = float(
            entry.get("stake") or 0
        )

        fair_odds = float(
            entry.get("fairOdds")
            or entry.get("odds")
            or 1
        )

        if won:
            profit = stake * (
                fair_odds - 1
            )
            current_bank += profit
            entry["status"] = "won"
            entry["profit"] = round(
                profit,
                2,
            )
        else:
            current_bank -= stake
            entry["status"] = "lost"
            entry["profit"] = round(
                -stake,
                2,
            )

        entry["score"] = (
            f"{home_goals}:{away_goals}"
        )

        entry["settledAt"] = (
            utc_now().isoformat()
        )

        bank.setdefault(
            "history",
            [],
        ).append(
            {
                "date": utc_now().date().isoformat(),
                "value": round(
                    current_bank,
                    2,
                ),
                "event": (
                    "PREDICTION_WON"
                    if won
                    else "PREDICTION_LOST"
                ),
                "matchId": match_id,
            }
        )

        settled_count += 1

    bank["current"] = round(
        current_bank,
        2,
    )

    state["bank"] = bank

    return settled_count


def prediction_to_public_records(
    prediction: dict[str, Any],
    candidate: dict[str, Any],
    *,
    stake: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kickoff = parse_utc_datetime(
        candidate["utcDate"]
    )

    competition = candidate["competition"]
    home_team = candidate["homeTeam"]["name"]
    away_team = candidate["awayTeam"]["name"]

    market = prediction["market"]
    market_label = MARKET_LABELS[market]

    country_value = candidate.get(
        "country",
        "",
    )

    country = COUNTRY_TRANSLATIONS.get(
        country_value,
        country_value or "Международный турнир",
    )

    risk_label = (
        "Низкий"
        if prediction["risk"] == "LOW"
        else "Средний"
    )

    public_prediction = {
        "id": (
            f"real-{candidate['matchId']}-"
            f"{market.lower()}"
        ),
        "sourceMatchId": candidate["matchId"],
        "league": competition["name"],
        "country": country,
        "date": kickoff.date().isoformat(),
        "time": kickoff.strftime("%H:%M"),
        "utcDate": candidate["utcDate"],
        "home": home_team,
        "away": away_team,
        "market": market,
        "pick": market_label,
        "odds": prediction["fairOdds"],
        "fairOdds": prediction["fairOdds"],
        "probability": prediction["probability"],
        "confidence": prediction["confidence"],
        "risk": risk_label,
        "reason": prediction["reason"],
        "coefficientType": "MODEL_FAIR",
    }

    history_record = {
        "id": public_prediction["id"],
        "sourceMatchId": candidate["matchId"],
        "date": kickoff.date().isoformat(),
        "utcDate": candidate["utcDate"],
        "league": competition["name"],
        "home": home_team,
        "away": away_team,
        "market": market,
        "pick": market_label,
        "odds": prediction["fairOdds"],
        "fairOdds": prediction["fairOdds"],
        "probability": prediction["probability"],
        "confidence": prediction["confidence"],
        "stake": round(stake, 2),
        "score": "",
        "status": "pending",
        "publishedAt": utc_now().isoformat(),
        "coefficientType": "MODEL_FAIR",
    }

    return public_prediction, history_record


def calculate_streak(
    completed_history: list[dict[str, Any]],
) -> str:
    if not completed_history:
        return "Нет завершённых прогнозов"

    latest_status = completed_history[-1].get(
        "status"
    )

    count = 0

    for entry in reversed(completed_history):
        if entry.get("status") != latest_status:
            break

        count += 1

    if latest_status == "won":
        return f"{count} успешных подряд"

    return f"{count} неуспешных подряд"


def calculate_best_segment(
    completed_history: list[dict[str, Any]],
) -> str:
    markets: dict[str, dict[str, int]] = {}

    for entry in completed_history:
        market = str(
            entry.get("market") or ""
        )

        if not market:
            continue

        bucket = markets.setdefault(
            market,
            {
                "won": 0,
                "total": 0,
            },
        )

        bucket["total"] += 1

        if entry.get("status") == "won":
            bucket["won"] += 1

    eligible = [
        (
            market,
            values["won"] / values["total"],
            values["total"],
        )
        for market, values in markets.items()
        if values["total"] >= 3
    ]

    if not eligible:
        return "Недостаточно данных"

    eligible.sort(
        key=lambda item: (
            -item[1],
            -item[2],
        )
    )

    best_market = eligible[0][0]

    return MARKET_LABELS.get(
        best_market,
        best_market,
    )


def update_statistics(
    state: dict[str, Any],
) -> None:
    history = [
        item
        for item in state.get("history", [])
        if isinstance(item, dict)
    ]

    completed = [
        item
        for item in history
        if item.get("status") in {
            "won",
            "lost",
        }
    ]

    odds_values = [
        float(item.get("fairOdds") or 0)
        for item in history
        if float(item.get("fairOdds") or 0) > 0
    ]

    average_odds = (
        sum(odds_values) / len(odds_values)
        if odds_values
        else 0
    )

    statistics = state.setdefault(
        "statistics",
        {},
    )

    statistics["averageOdds"] = round(
        average_odds,
        2,
    )

    statistics["currentStreak"] = calculate_streak(
        completed
    )

    statistics["bestSegment"] = calculate_best_segment(
        completed
    )

    bank = state.setdefault(
        "bank",
        {},
    )

    starting = float(
        bank.get("starting") or 10000
    )

    current = float(
        bank.get("current") or starting
    )

    roi = (
        ((current - starting) / starting) * 100
        if starting
        else 0
    )

    bank["roi"] = round(
        roi,
        2,
    )

    values = [
        float(item.get("value") or 0)
        for item in bank.get("history", [])
        if isinstance(item, dict)
    ]

    if not values:
        values = [starting, current]

    peak = values[0]
    maximum_drawdown = 0.0

    for value in values:
        peak = max(peak, value)

        if peak > 0:
            drawdown = (
                (peak - value) / peak
            ) * 100

            maximum_drawdown = max(
                maximum_drawdown,
                drawdown,
            )

    bank["maxDrawdown"] = round(
        maximum_drawdown,
        2,
    )


def remove_duplicate_pending_predictions(
    history: list[dict[str, Any]],
) -> set[int]:
    return {
        int(item["sourceMatchId"])
        for item in history
        if (
            isinstance(item, dict)
            and item.get("status") == "pending"
            and item.get("sourceMatchId") is not None
        )
    }


def create_report(
    *,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "timestamp": utc_now().isoformat(),
        "details": details or {},
    }


def main() -> int:
    log("Запуск AI Football Lab Data Pipeline")

    config = load_json(CONFIG_PATH)
    old_state = load_json(STATE_PATH)

    football_api_key = require_environment_variable(
        "FOOTBALL_DATA_API_KEY"
    )

    openrouter_api_key = require_environment_variable(
        "OPENROUTER_API_KEY"
    )

    now = utc_now()
    today = now.date()

    lookback_days = int(
        config.get("lookbackDays") or 10
    )

    lookahead_days = int(
        config.get("lookaheadDays") or 3
    )

    competitions = [
        str(item)
        for item in config.get(
            "competitions",
            [],
        )
    ]

    if not competitions:
        raise RuntimeError(
            "В конфигурации отсутствуют соревнования"
        )

    recent_matches = fetch_matches(
        football_api_key,
        date_from=today - dt.timedelta(
            days=lookback_days
        ),
        date_to=today,
        competitions=competitions,
    )

    # Небольшая пауза сохраняет запас по лимитам API.
    time.sleep(7)

    upcoming_matches = fetch_matches(
        football_api_key,
        date_from=today,
        date_to=today + dt.timedelta(
            days=lookahead_days
        ),
        competitions=competitions,
    )

    all_matches_by_id: dict[int, dict[str, Any]] = {}

    for match in recent_matches + upcoming_matches:
        try:
            all_matches_by_id[int(match["id"])] = match
        except (KeyError, TypeError, ValueError):
            continue

    finished_matches = [
        match
        for match in recent_matches
        if final_score(match) is not None
    ]

    scheduled_matches = [
        match
        for match in upcoming_matches
        if str(
            match.get("status") or ""
        ).upper() in {
            "SCHEDULED",
            "TIMED",
        }
    ]

    state = ensure_real_state(
        old_state,
        config,
    )

    settled_count = resolve_existing_history(
        state,
        all_matches_by_id,
    )

    log(
        f"Завершено ранее ожидавших прогнозов: "
        f"{settled_count}"
    )

    team_form = build_team_form(
        finished_matches
    )

    candidates = build_candidates(
        scheduled_matches,
        team_form,
        config,
    )

    log(
        f"Недавних матчей получено: "
        f"{len(recent_matches)}"
    )

    log(
        f"Завершённых матчей для формы: "
        f"{len(finished_matches)}"
    )

    log(
        f"Предстоящих матчей получено: "
        f"{len(upcoming_matches)}"
    )

    log(
        f"Предстоящих матчей после статуса: "
        f"{len(scheduled_matches)}"
    )

    log(
        f"Команд с рассчитанной формой: "
        f"{len(team_form)}"
    )

    log(
        f"Подготовлено кандидатов для анализа: "
        f"{len(candidates)}"
    )

    if not candidates:
        state["predictions"] = []

        state.setdefault(
            "meta",
            {},
        ).update(
            {
                "version": "2.0.0",
                "mode": "real",
                "updatedAt": now.isoformat(),
                "analyzedMatches": len(
                    scheduled_matches
                ),
                "candidateMatches": 0,
                "selectedPredictions": 0,
                "source": "football-data.org",
                "analysisProvider": "OpenRouter",
                "analysisStatus": "NO_SUITABLE_DATA",
                "notice": (
                    "Система не публикует прогнозы "
                    "без достаточного объёма данных."
                ),
            }
        )

        update_statistics(state)
        write_json_atomic(STATE_PATH, state)

        report = create_report(
            status="GREEN_NO_PREDICTIONS",
            message=(
                "Нет матчей с достаточным объёмом "
                "статистических данных."
            ),
            details={
                "recentMatches": len(
                    recent_matches
                ),
                "finishedMatches": len(
                    finished_matches
                ),
                "upcomingMatches": len(
                    upcoming_matches
                ),
                "scheduledMatches": len(
                    scheduled_matches
                ),
                "teamsWithForm": len(
                    team_form
                ),
                "candidates": 0,
                "minimumTeamMatches": int(
                    config.get("minimumTeamMatches") or 1
                ),
                "settledPredictions": settled_count,
            },
        )

        write_json_atomic(
            REPORT_PATH,
            report,
        )

        log("Обновление завершено без публикации прогнозов")
        return 0

    prompt = build_analysis_prompt(
        candidates,
        config,
    )

    model_result, model_name = call_openrouter(
        openrouter_api_key,
        prompt,
    )

    selected = normalize_model_predictions(
        model_result,
        candidates,
        config,
    )

    candidates_by_id = {
        int(candidate["matchId"]): candidate
        for candidate in candidates
    }

    history = [
        item
        for item in state.get("history", [])
        if isinstance(item, dict)
    ]

    already_pending = remove_duplicate_pending_predictions(
        history
    )

    selected = [
        item
        for item in selected
        if int(item["matchId"]) not in already_pending
    ]

    current_bank = float(
        state.get("bank", {}).get("current")
        or config.get("startingVirtualBank")
        or 10000
    )

    total_stake_percent = float(
        config.get("maximumTotalStakePercent")
        or 20
    )

    total_stake = current_bank * (
        total_stake_percent / 100
    )

    stake_per_prediction = (
        total_stake / len(selected)
        if selected
        else 0
    )

    public_predictions: list[dict[str, Any]] = []
    new_history_records: list[dict[str, Any]] = []

    for prediction in selected:
        candidate = candidates_by_id[
            int(prediction["matchId"])
        ]

        public_record, history_record = (
            prediction_to_public_records(
                prediction,
                candidate,
                stake=stake_per_prediction,
            )
        )

        public_predictions.append(
            public_record
        )

        new_history_records.append(
            history_record
        )

    history.extend(new_history_records)

    # Публичная выдача содержит только актуальные
    # прогнозы текущего запуска.
    state["predictions"] = public_predictions
    state["history"] = history

    state.setdefault(
        "meta",
        {},
    ).update(
        {
            "version": "2.0.0",
            "mode": "real",
            "updatedAt": now.isoformat(),
            "analyzedMatches": len(
                scheduled_matches
            ),
            "candidateMatches": len(
                candidates
            ),
            "selectedPredictions": len(
                public_predictions
            ),
            "source": "football-data.org",
            "analysisProvider": "OpenRouter",
            "analysisModel": model_name,
            "analysisStatus": (
                "PREDICTIONS_SELECTED"
                if public_predictions
                else "NO_CONFIDENT_PREDICTIONS"
            ),
            "notice": (
                "Расчётные коэффициенты основаны "
                "на вероятности модели и не являются "
                "коэффициентами букмекерских контор."
            ),
        }
    )

    update_statistics(state)

    write_json_atomic(
        STATE_PATH,
        state,
    )

    report = create_report(
        status="GREEN",
        message="Данные успешно обновлены.",
        details={
            "recentMatches": len(
                recent_matches
            ),
            "scheduledMatches": len(
                scheduled_matches
            ),
            "candidateMatches": len(
                candidates
            ),
            "selectedPredictions": len(
                public_predictions
            ),
            "settledPredictions": settled_count,
            "analysisModel": model_name,
        },
    )

    write_json_atomic(
        REPORT_PATH,
        report,
    )

    log(
        "Обновление завершено. "
        f"Опубликовано прогнозов: "
        f"{len(public_predictions)}"
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except Exception as error:
        log(f"КРИТИЧЕСКАЯ ОШИБКА: {error}")

        try:
            write_json_atomic(
                REPORT_PATH,
                create_report(
                    status="FAILED",
                    message=str(error),
                ),
            )
        except Exception:
            pass

        raise