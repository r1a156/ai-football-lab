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


from zoneinfo import ZoneInfo
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


def configured_timezone(
    config: dict[str, Any],
) -> ZoneInfo:
    timezone_name = str(
        config.get("timezone")
        or "Europe/Moscow"
    )

    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("UTC")


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



def fetch_matches_chunked(
    api_key: str,
    *,
    date_from: dt.date,
    date_to: dt.date,
    competitions: list[str],
    maximum_days_per_request: int = 10,
    pause_seconds: float = 7.0,
) -> list[dict[str, Any]]:
    """
    Football-Data ограничивает общий endpoint матчей
    диапазоном не более 10 дней.

    Функция разбивает большой период на последовательные
    непересекающиеся интервалы и объединяет результаты
    с дедупликацией по match.id.
    """

    if date_to < date_from:
        raise ValueError(
            "date_to не может быть раньше date_from"
        )

    maximum_days_per_request = max(
        1,
        min(int(maximum_days_per_request), 10),
    )

    collected_by_id: dict[int, dict[str, Any]] = {}
    cursor = date_from
    request_number = 0

    while cursor <= date_to:
        chunk_end = min(
            cursor + dt.timedelta(
                days=maximum_days_per_request - 1
            ),
            date_to,
        )

        request_number += 1

        log(
            f"Запрос исторического чанка "
            f"#{request_number}: "
            f"{cursor.isoformat()} — "
            f"{chunk_end.isoformat()}"
        )

        chunk_matches = fetch_matches(
            api_key,
            date_from=cursor,
            date_to=chunk_end,
            competitions=competitions,
        )

        for match in chunk_matches:
            try:
                match_id = int(match["id"])
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            collected_by_id[match_id] = match

        cursor = chunk_end + dt.timedelta(days=1)

        if cursor <= date_to and pause_seconds > 0:
            time.sleep(pause_seconds)

    result = list(collected_by_id.values())

    result.sort(
        key=lambda item: str(
            item.get("utcDate") or ""
        )
    )

    log(
        f"Чанков получено: {request_number}; "
        f"уникальных матчей: {len(result)}"
    )

    return result

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


    minimum_lead_hours = max(
        0.0,
        float(config.get("minimumLeadHours") or 4),
    )

    earliest_allowed_kickoff = (
        utc_now()
        + dt.timedelta(hours=minimum_lead_hours)
    )
    for match in scheduled_matches:
        try:
            kickoff_utc = parse_utc_datetime(
                str(match.get("utcDate") or "")
            )
        except Exception:
            continue

        if kickoff_utc <= earliest_allowed_kickoff:
            continue

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


    win_market_minimum_confidence = int(
        config.get("winMarketMinimumConfidence")
        or max(minimum_confidence, 74)
    )

    win_markets = {
        "HOME_WIN",
        "AWAY_WIN",
    }
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
Если массив кандидатов не пуст, отбери от 1 до {maximum_predictions} наиболее обоснованных прогнозов.

Если кандидаты переданы, нельзя возвращать пустой массив. Выбери хотя бы один наиболее обоснованный вариант, но обязательно укажи честный уровень confidence и risk.

Минимальная допустимая уверенность:
{minimum_confidence} из 100.

Разрешённые значения market:
{json.dumps(allowed_markets, ensure_ascii=False)}

ОГРАНИЧЕНИЯ:
1. Один прогноз на один matchId.
2. Не выбирай слабые матчи только ради достижения четырёх, но при наличии кандидатов выбери минимум один лучший вариант.
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


# V4_4R1_OPENROUTER_RESILIENCE


def call_openrouter(
    api_key: str,
    prompt: str,
) -> tuple[dict[str, Any], str]:
    """
    Устойчивый вызов OpenRouter.

    Не привязываемся к одному поставщику или устаревшему model slug.
    openrouter/auto выбирает доступную модель для текущей задачи.
    """

    configured_model = str(
        os.getenv("OPENROUTER_MODEL")
        or "openrouter/auto"
    ).strip()

    if (
        not configured_model
        or configured_model
        == "qwen/qwen-2.5-72b-instruct"
    ):
        configured_model = "openrouter/auto"

    maximum_attempts = 4
    retry_seconds = 15

    payload = {
        "model": configured_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты строгий аналитический модуль. "
                    "Отвечай только корректным JSON без markdown."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.1,
        "max_tokens": 3000,
        "stream": False,
        "provider": {
            "allow_fallbacks": True,
            "require_parameters": True,
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": (
            "https://r1a156.github.io/ai-football-lab/"
        ),
        "X-Title": "AI Football Lab",
    }

    last_error: Exception | None = None

    for attempt in range(1, maximum_attempts + 1):
        log(
            "Запрос OpenRouter Auto Router: "
            f"попытка {attempt}/{maximum_attempts}; "
            f"модель={configured_model}"
        )

        try:
            response = request_json(
                "https://openrouter.ai/api/v1/chat/completions",
                method="POST",
                headers=headers,
                payload=payload,
                timeout=180,
            )

            choices = response.get("choices") or []

            if not choices:
                raise RuntimeError(
                    "OpenRouter не вернул choices"
                )

            message = choices[0].get("message") or {}
            content = message.get("content")

            if isinstance(content, list):
                content = "".join(
                    str(item.get("text") or "")
                    if isinstance(item, dict)
                    else str(item)
                    for item in content
                )

            content = str(content or "").strip()

            if not content:
                raise RuntimeError(
                    "OpenRouter вернул пустой content"
                )

            returned_model = str(
                response.get("model")
                or configured_model
            )

            parsed = extract_json_object(content)

            log(
                "OpenRouter успешно ответил; "
                f"использована модель: {returned_model}"
            )

            return parsed, returned_model

        except Exception as error:
            last_error = error

            log(
                "Предупреждение OpenRouter: "
                f"{type(error).__name__}: {error}"
            )

            if attempt < maximum_attempts:
                delay = retry_seconds * attempt

                log(
                    "Повтор OpenRouter через "
                    f"{delay} секунд"
                )

                time.sleep(delay)

    raise RuntimeError(
        "OpenRouter временно недоступен после "
        f"{maximum_attempts} попыток: {last_error}"
    )


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

    win_market_minimum_confidence = int(
        config.get("winMarketMinimumConfidence")
        or max(minimum_confidence, 74)
    )

    win_markets = {
        "HOME_WIN",
        "AWAY_WIN",
    }

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

        required_confidence = (
            win_market_minimum_confidence
            if market in win_markets
            else minimum_confidence
        )

        if confidence < required_confidence:
            continue

        confidence = max(
            0,
            min(confidence, 100),
        )

        probability = max(
            0.50,
            min(probability, 0.90),
        )


        confidence_probability = confidence / 100
        probability = min(
            probability,
            confidence_probability,
        )

        probability = max(
            0.50,
            min(probability, 0.90),
        )

        if market in win_markets:
            risk = (
                "LOW"
                if confidence >= 80
                else "MEDIUM"
            )
        else:
            risk = (
                "LOW"
                if confidence >= 76
                else "MEDIUM"
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
            "analysisProvider": (
                "Встроенный статистический модуль"
                if analysis_mode == "DETERMINISTIC_FALLBACK"
                else "OpenRouter"
            ),
            "timezone": str(
                config.get("timezone")
                or "Europe/Moscow"
            ),
            "minimumLeadHours": float(
                config.get("minimumLeadHours")
                or 4
            ),
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



# =============================================================================
# V4_5_DETERMINISTIC_FALLBACK
# =============================================================================

def clamp_number(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    return min(maximum, max(minimum, value))


def build_deterministic_predictions(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Резервный статистический выбор.

    Используются только уже рассчитанные показатели формы.
    Внешние знания, составы, слухи и букмекерские коэффициенты
    не используются.
    """

    if not candidates:
        return []

    allowed_markets = {
        str(value)
        for value in config.get("allowedMarkets", [])
    }

    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 4),
    )

    minimum_confidence = max(
        50,
        int(
            config.get("fallbackMinimumConfidence")
            or config.get("minimumConfidence")
            or 74
        ),
    )

    maximum_confidence = max(
        minimum_confidence,
        int(config.get("fallbackMaximumConfidence") or 82),
    )

    ranked: list[dict[str, Any]] = []

    for candidate in candidates:
        home_form = candidate.get("homeTeam", {}).get(
            "form",
            {},
        )

        away_form = candidate.get("awayTeam", {}).get(
            "form",
            {},
        )

        home_ppg = float(
            home_form.get("pointsPerGame") or 0
        )

        away_ppg = float(
            away_form.get("pointsPerGame") or 0
        )

        home_over = float(
            home_form.get("over15Rate") or 0
        )

        away_over = float(
            away_form.get("over15Rate") or 0
        )

        average_over = (
            home_over + away_over
        ) / 2

        form_difference = home_ppg - away_ppg

        data_quality = float(
            candidate.get("dataQuality") or 0
        )

        market = ""
        signal = 0.0
        reason = ""

        # Наиболее устойчивый рынок — тотал больше 1,5,
        # когда обе команды регулярно участвуют в матчах
        # минимум с двумя голами.
        if (
            "OVER_1_5" in allowed_markets
            and average_over >= 0.62
        ):
            market = "OVER_1_5"
            signal = average_over

            reason = (
                "Резервный статистический расчёт: "
                f"доля матчей с двумя и более голами "
                f"у команд составляет в среднем "
                f"{average_over * 100:.0f}%."
            )

        elif (
            form_difference >= 0.45
            and "HOME_OR_DRAW" in allowed_markets
        ):
            market = "HOME_OR_DRAW"
            signal = clamp_number(
                0.60 + form_difference / 10,
                0.60,
                0.80,
            )

            reason = (
                "Резервный статистический расчёт: "
                "хозяева имеют преимущество по текущей форме; "
                f"очки за матч {home_ppg:.2f} против "
                f"{away_ppg:.2f}."
            )

        elif (
            form_difference <= -0.45
            and "AWAY_OR_DRAW" in allowed_markets
        ):
            market = "AWAY_OR_DRAW"
            signal = clamp_number(
                0.60 + abs(form_difference) / 10,
                0.60,
                0.80,
            )

            reason = (
                "Резервный статистический расчёт: "
                "гости имеют преимущество по текущей форме; "
                f"очки за матч {away_ppg:.2f} против "
                f"{home_ppg:.2f}."
            )

        elif "OVER_1_5" in allowed_markets:
            # Последний честный резерв при наличии кандидатов.
            # Он помечается средним риском и не выдаётся за
            # высокоуверенный прогноз.
            market = "OVER_1_5"
            signal = clamp_number(
                average_over,
                0.55,
                0.70,
            )

            reason = (
                "Резервный статистический расчёт: "
                "выбран наиболее устойчивый из доступных "
                "рынков по совокупности формы и результативности. "
                "Уровень риска повышен."
            )

        else:
            continue

        confidence_from_signal = (
            minimum_confidence
            + max(0.0, signal - 0.55) * 30
            + max(0.0, data_quality - 50) * 0.05
        )

        confidence = int(
            round(
                clamp_number(
                    confidence_from_signal,
                    minimum_confidence,
                    maximum_confidence,
                )
            )
        )

        probability = round(
            clamp_number(
                confidence / 100,
                0.50,
                0.86,
            ),
            4,
        )

        ranked.append(
            {
                "matchId": int(candidate["matchId"]),
                "market": market,
                "confidence": confidence,
                "probability": probability,
                "fairOdds": round(1 / probability, 2),
                "risk": (
                    "LOW"
                    if confidence >= 78
                    else "MEDIUM"
                ),
                "reason": reason,
                "analysisMode": "DETERMINISTIC_FALLBACK",
                "rankingScore": round(
                    confidence * 0.75
                    + data_quality * 0.25,
                    4,
                ),
            }
        )

    ranked.sort(
        key=lambda item: (
            -float(item.get("rankingScore") or 0),
            -int(item.get("confidence") or 0),
        )
    )

    result: list[dict[str, Any]] = []
    used_matches: set[int] = set()

    for item in ranked:
        match_id = int(item["matchId"])

        if match_id in used_matches:
            continue

        result.append(item)
        used_matches.add(match_id)

        if len(result) >= maximum_predictions:
            break

    return result


def localize_existing_history(
    state: dict[str, Any],
) -> None:
    """
    Русифицирует старые записи, не удаляя и не пересчитывая
    финансовые результаты.
    """

    history = state.get("history", [])

    if not isinstance(history, list):
        return

    for item in history:
        if not isinstance(item, dict):
            continue

        source_home = str(item.get("home") or "")
        source_away = str(item.get("away") or "")
        source_league = str(item.get("league") or "")

        item.setdefault("homeOriginal", source_home)
        item.setdefault("awayOriginal", source_away)
        item.setdefault("leagueOriginal", source_league)

        item["home"] = localize_team_name(source_home)
        item["away"] = localize_team_name(source_away)
        item["league"] = localize_competition_name(
            source_league
        )



def prediction_to_public_records(
    prediction: dict[str, Any],
    candidate: dict[str, Any],
    *,
    stake: float,
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    kickoff_utc = parse_utc_datetime(
        candidate["utcDate"]
    )

    timezone = configured_timezone(config)
    kickoff = kickoff_utc.astimezone(timezone)
    timezone_name = str(
        config.get("timezone")
        or "Europe/Moscow"
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
        "timezone": timezone_name,
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
        "analysisMode": str(
            prediction.get("analysisMode") or "AI"
        ),
        "coefficientType": "MODEL_FAIR",
    }

    history_record = {
        "id": public_prediction["id"],
        "sourceMatchId": candidate["matchId"],
        "date": kickoff.date().isoformat(),
        "utcDate": candidate["utcDate"],
        "timezone": timezone_name,
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
        "analysisMode": str(
            prediction.get("analysisMode") or "AI"
        ),
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



# =============================================================================
# V4_4_PUBLIC_SELECTION_PIPELINE
# =============================================================================

COMPETITION_NAMES_RU: dict[str, str] = {
    "Premier League": "Английская Премьер-лига",
    "Primera Division": "Испанская Ла Лига",
    "La Liga": "Испанская Ла Лига",
    "Bundesliga": "Немецкая Бундеслига",
    "Serie A": "Итальянская Серия А",
    "Ligue 1": "Французская Лига 1",
    "Eredivisie": "Нидерландская Эредивизи",
    "Primeira Liga": "Португальская Примейра-лига",
    "Campeonato Brasileiro Série A": "Бразильская Серия А",
    "FIFA World Cup": "Чемпионат мира",
    "World Cup": "Чемпионат мира",
}

TEAM_NAMES_RU: dict[str, str] = {
    # Бразилия
    "Athletico Paranaense": "Атлетико Паранаэнсе",
    "Atletico Paranaense": "Атлетико Паранаэнсе",
    "Paranaense": "Атлетико Паранаэнсе",
    "Atlético Mineiro": "Атлетико Минейро",
    "Atletico Mineiro": "Атлетико Минейро",
    "Bahia": "Баия",
    "Botafogo": "Ботафого",
    "Bragantino": "Брагантино",
    "Red Bull Bragantino": "Ред Булл Брагантино",
    "Ceará": "Сеара",
    "Ceara": "Сеара",
    "Corinthians": "Коринтианс",
    "Coritiba": "Коритиба",
    "Cruzeiro": "Крузейро",
    "Flamengo": "Фламенго",
    "Fluminense": "Флуминенсе",
    "Fortaleza": "Форталеза",
    "Grêmio": "Гремио",
    "Gremio": "Гремио",
    "Internacional": "Интернасьонал",
    "Juventude": "Жувентуде",
    "Palmeiras": "Палмейрас",
    "Santos": "Сантос",
    "São Paulo": "Сан-Паулу",
    "Sao Paulo": "Сан-Паулу",
    "Sport Recife": "Спорт Ресифи",
    "Vasco da Gama": "Васко да Гама",
    "Vitória": "Витория",
    "Vitoria": "Витория",
    "Clube do Remo": "Ремо",

    # Англия
    "Arsenal": "Арсенал",
    "Aston Villa": "Астон Вилла",
    "Bournemouth": "Борнмут",
    "Brentford": "Брентфорд",
    "Brighton & Hove Albion": "Брайтон",
    "Burnley": "Бёрнли",
    "Chelsea": "Челси",
    "Crystal Palace": "Кристал Пэлас",
    "Everton": "Эвертон",
    "Fulham": "Фулхэм",
    "Leeds United": "Лидс Юнайтед",
    "Liverpool": "Ливерпуль",
    "Manchester City": "Манчестер Сити",
    "Manchester United": "Манчестер Юнайтед",
    "Newcastle United": "Ньюкасл Юнайтед",
    "Nottingham Forest": "Ноттингем Форест",
    "Sunderland": "Сандерленд",
    "Tottenham Hotspur": "Тоттенхэм",
    "West Ham United": "Вест Хэм Юнайтед",
    "Wolverhampton Wanderers": "Вулверхэмптон",

    # Испания
    "FC Barcelona": "Барселона",
    "Barcelona": "Барселона",
    "Real Madrid": "Реал Мадрид",
    "Atletico Madrid": "Атлетико Мадрид",
    "Atlético de Madrid": "Атлетико Мадрид",
    "Athletic Club": "Атлетик Бильбао",
    "Sevilla FC": "Севилья",
    "Valencia CF": "Валенсия",
    "Villarreal CF": "Вильярреал",
    "Real Sociedad": "Реал Сосьедад",
    "Real Betis": "Реал Бетис",

    # Германия
    "Bayern Munich": "Бавария",
    "FC Bayern München": "Бавария",
    "Borussia Dortmund": "Боруссия Дортмунд",
    "Bayer Leverkusen": "Байер",
    "RB Leipzig": "РБ Лейпциг",

    # Италия
    "Inter Milan": "Интер",
    "Internazionale": "Интер",
    "AC Milan": "Милан",
    "Juventus": "Ювентус",
    "Napoli": "Наполи",
    "AS Roma": "Рома",
    "Lazio": "Лацио",
    "Atalanta": "Аталанта",

    # Франция
    "Paris Saint-Germain": "Пари Сен-Жермен",
    "Paris Saint Germain": "Пари Сен-Жермен",
    "Olympique Marseille": "Марсель",
    "Olympique Lyonnais": "Лион",
    "AS Monaco": "Монако",
    "Lille OSC": "Лилль",
}

MATCH_STATUS_RU: dict[str, str] = {
    "SCHEDULED": "Запланирован",
    "TIMED": "Ожидается начало",
    "IN_PLAY": "Матч идёт",
    "PAUSED": "Перерыв",
    "FINISHED": "Завершён",
    "POSTPONED": "Перенесён",
    "SUSPENDED": "Приостановлен",
    "CANCELLED": "Отменён",
    "AWARDED": "Результат присуждён",
}


def transliterate_latin_name(value: str) -> str:
    """
    Запасной детерминированный вариант для неизвестных латинских названий.

    Сначала всегда используется точный словарь. Транслитерация нужна только
    для нового названия, которого ещё нет в каталоге.
    """

    combinations = {
        "shch": "щ",
        "sch": "щ",
        "ch": "ч",
        "sh": "ш",
        "zh": "ж",
        "kh": "х",
        "th": "т",
        "ph": "ф",
        "ck": "к",
        "qu": "кв",
        "wh": "у",
        "ya": "я",
        "yu": "ю",
        "yo": "ё",
        "ye": "е",
        "ai": "ай",
        "ay": "ай",
        "oi": "ой",
        "oy": "ой",
        "ou": "у",
    }

    characters = {
        "a": "а",
        "b": "б",
        "c": "к",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "и",
        "j": "дж",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "q": "к",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "у",
        "v": "в",
        "w": "у",
        "x": "кс",
        "y": "и",
        "z": "з",
    }

    normalized = (
        value
        .replace("ã", "a")
        .replace("á", "a")
        .replace("à", "a")
        .replace("â", "a")
        .replace("ä", "a")
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ô", "o")
        .replace("õ", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ç", "c")
        .replace("ñ", "n")
    )

    lower = normalized.lower()
    result: list[str] = []
    index = 0

    while index < len(lower):
        matched = False

        for size in (4, 3, 2):
            part = lower[index:index + size]

            if part in combinations:
                result.append(combinations[part])
                index += size
                matched = True
                break

        if matched:
            continue

        character = lower[index]
        result.append(characters.get(character, character))
        index += 1

    transliterated = "".join(result)

    words = [
        word[:1].upper() + word[1:]
        if word
        else word
        for word in transliterated.split(" ")
    ]

    return " ".join(words)


def localize_team_name(value: str) -> str:
    source = str(value or "").strip()

    if not source:
        return "Неизвестная команда"

    exact = TEAM_NAMES_RU.get(source)

    if exact:
        return exact

    if re.search(r"[А-Яа-яЁё]", source):
        return source

    return transliterate_latin_name(source)


def localize_competition_name(value: str) -> str:
    source = str(value or "").strip()

    if not source:
        return "Неизвестный турнир"

    exact = COMPETITION_NAMES_RU.get(source)

    if exact:
        return exact

    if re.search(r"[А-Яа-яЁё]", source):
        return source

    return transliterate_latin_name(source)


def localize_reason(
    value: str,
    replacements: dict[str, str],
) -> str:
    localized = str(value or "").strip()

    for source, target in sorted(
        replacements.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if not source or source == target:
            continue

        localized = localized.replace(source, target)

    return localized


def extract_public_match_status(
    match: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(match, dict):
        return {
            "matchStatus": "UNKNOWN",
            "matchStatusLabel": "Статус уточняется",
            "homeScore": None,
            "awayScore": None,
            "liveScore": "",
            "minute": None,
        }

    status = str(match.get("status") or "UNKNOWN").upper()
    score = match.get("score") or {}

    selected_score: dict[str, Any] = {}

    for key in (
        "fullTime",
        "regularTime",
        "halfTime",
    ):
        candidate = score.get(key)

        if isinstance(candidate, dict):
            home = candidate.get("home")
            away = candidate.get("away")

            if home is not None or away is not None:
                selected_score = candidate
                break

    home_score = selected_score.get("home")
    away_score = selected_score.get("away")

    live_score = ""

    if home_score is not None and away_score is not None:
        live_score = f"{home_score}:{away_score}"

    minute_value = (
        match.get("minute")
        or match.get("elapsed")
        or match.get("matchMinute")
    )

    try:
        minute = int(minute_value)
    except (TypeError, ValueError):
        minute = None

    return {
        "matchStatus": status,
        "matchStatusLabel": MATCH_STATUS_RU.get(
            status,
            "Статус уточняется",
        ),
        "homeScore": home_score,
        "awayScore": away_score,
        "liveScore": live_score,
        "minute": minute,
    }


def finalize_public_selection(
    public_predictions: list[dict[str, Any]],
    history_records: list[dict[str, Any]],
    matches_by_id: dict[int, dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Финальный backend guard.

    Даже если LLM выбрал неподходящий вариант, наружу не пройдут:
    - матчи вне единого 24-часового окна;
    - коэффициенты ниже установленного минимума;
    - прогнозы ниже минимальной уверенности;
    - более четырёх прогнозов.
    """

    minimum_confidence = int(
        config.get("minimumConfidence") or 74
    )

    minimum_model_odds = float(
        config.get("minimumModelOdds") or 1.0
    )

    window_hours = max(
        1.0,
        float(config.get("selectionWindowHours") or 24),
    )

    maximum_predictions = max(
        0,
        int(config.get("maximumPredictions") or 4),
    )

    minimum_lead_hours = max(
        0.0,
        float(config.get("minimumLeadHours") or 0),
    )

    window_start = now + dt.timedelta(
        hours=minimum_lead_hours
    )

    window_end = now + dt.timedelta(
        hours=window_hours
    )

    history_by_id = {
        str(item.get("id") or ""): item
        for item in history_records
        if isinstance(item, dict)
    }

    accepted: list[dict[str, Any]] = []
    accepted_history: list[dict[str, Any]] = []

    for prediction in public_predictions:
        if not isinstance(prediction, dict):
            continue

        try:
            kickoff = parse_utc_datetime(
                str(prediction.get("utcDate") or "")
            )

            confidence = int(
                prediction.get("confidence") or 0
            )

            model_odds = float(
                prediction.get("fairOdds")
                or prediction.get("odds")
                or 0
            )

            match_id = int(
                prediction.get("sourceMatchId")
            )
        except (TypeError, ValueError):
            continue

        if kickoff < window_start or kickoff > window_end:
            log(
                "V4.4 отклонён прогноз вне 24 часов: "
                f"matchId={match_id}; kickoff={kickoff.isoformat()}"
            )
            continue

        if confidence < minimum_confidence:
            log(
                "V4.4 отклонён прогноз по уверенности: "
                f"matchId={match_id}; confidence={confidence}"
            )
            continue

        # MODEL_FAIR является математическим коэффициентом
        # вероятности модели, а не букмекерской линией.
        # До подключения market odds не отклоняем прогноз
        # только из-за небольшого MODEL_FAIR.
        if model_odds <= 1.0:
            log(
                "V4.4R1 отклонён некорректный "
                "расчётный коэффициент: "
                f"matchId={match_id}; odds={model_odds}"
            )
            continue

        source_home = str(prediction.get("home") or "")
        source_away = str(prediction.get("away") or "")
        source_league = str(prediction.get("league") or "")

        localized_home = localize_team_name(source_home)
        localized_away = localize_team_name(source_away)
        localized_league = localize_competition_name(
            source_league
        )

        prediction["homeOriginal"] = source_home
        prediction["awayOriginal"] = source_away
        prediction["leagueOriginal"] = source_league

        prediction["home"] = localized_home
        prediction["away"] = localized_away
        prediction["league"] = localized_league

        prediction["reason"] = localize_reason(
            str(prediction.get("reason") or ""),
            {
                source_home: localized_home,
                source_away: localized_away,
                source_league: localized_league,
            },
        )

        prediction["selectionWindowHours"] = window_hours
        prediction["minimumModelOdds"] = minimum_model_odds
        prediction["marketOddsAvailable"] = False
        prediction["expectedValueAvailable"] = False
        prediction["coefficientType"] = "MODEL_FAIR"

        prediction.update(
            extract_public_match_status(
                matches_by_id.get(match_id)
            )
        )

        history_record = history_by_id.get(
            str(prediction.get("id") or "")
        )

        if history_record:
            history_record["homeOriginal"] = source_home
            history_record["awayOriginal"] = source_away
            history_record["leagueOriginal"] = source_league
            history_record["home"] = localized_home
            history_record["away"] = localized_away
            history_record["league"] = localized_league
            history_record["marketOddsAvailable"] = False
            history_record["expectedValueAvailable"] = False

            history_record.update(
                extract_public_match_status(
                    matches_by_id.get(match_id)
                )
            )

            accepted_history.append(history_record)

        accepted.append(prediction)

    accepted.sort(
        key=lambda item: (
            str(item.get("utcDate") or ""),
            -int(item.get("confidence") or 0),
            -float(item.get("fairOdds") or 0),
        )
    )

    accepted = accepted[:maximum_predictions]

    accepted_ids = {
        str(item.get("id") or "")
        for item in accepted
    }

    accepted_history = [
        item
        for item in accepted_history
        if str(item.get("id") or "") in accepted_ids
    ]

    return accepted, accepted_history



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
    published_match_ids: set[int] = set()

    for item in history:
        if not isinstance(item, dict):
            continue

        source_match_id = item.get("sourceMatchId")

        if source_match_id is None:
            continue

        try:
            published_match_ids.add(
                int(source_match_id)
            )
        except (TypeError, ValueError):
            continue

    return published_match_ids


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

    football_api_key = os.environ.get(
        "FOOTBALL_DATA_API_KEY",
        ""
    )

    if not football_api_key:
        try:
            football_api_key = input(
                "Введите FOOTBALL_DATA_API_KEY: "
            ).strip()
        except EOFError:
            football_api_key = ""

    if not football_api_key:
        raise RuntimeError(
            "Не задан FOOTBALL_DATA_API_KEY"
        )

    openrouter_api_key = os.environ.get(
        "OPENROUTER_API_KEY",
        ""
    )

    if not openrouter_api_key:
        try:
            openrouter_api_key = input(
                "Введите OPENROUTER_API_KEY: "
            ).strip()
        except EOFError:
            openrouter_api_key = ""

    if not openrouter_api_key:
        raise RuntimeError(
            "Не задан OPENROUTER_API_KEY"
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

    recent_matches = fetch_matches_chunked(
        football_api_key,
        date_from=today - dt.timedelta(
            days=lookback_days
        ),
        date_to=today,
        competitions=competitions,
        maximum_days_per_request=10,
        pause_seconds=7,
    )

    # Пауза перед запросом будущих матчей сохраняет
    # безопасный запас относительно лимита API.
    time.sleep(7)

    upcoming_matches = fetch_matches(
        football_api_key,
        date_from=today,
        date_to=today + dt.timedelta(
            days=lookahead_days + 1
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

    localize_existing_history(state)

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
            "timezone": str(
                config.get("timezone")
                or "Europe/Moscow"
            ),
            "minimumLeadHours": float(
                config.get("minimumLeadHours")
                or 4
            ),
                "timezone": str(
                    config.get("timezone")
                    or "Europe/Moscow"
                ),
                "minimumLeadHours": float(
                    config.get("minimumLeadHours")
                    or 4
                ),
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
                "lookbackDays": lookback_days,
                "historyRequestChunks": math.ceil(
                    (lookback_days + 1) / 10
                ),
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

    model_name = "openrouter/auto"
    analysis_mode = "AI"
    openrouter_error_text = ""

    try:
        model_result, model_name = call_openrouter(
            openrouter_api_key,
            prompt,
        )

        selected = normalize_model_predictions(
            model_result,
            candidates,
            config,
        )

        for item in selected:
            item["analysisMode"] = "AI"


        # V46B AI + STAT FUSION
        # Если AI дал меньше 4 прогнозов,
        # добираем лучшими статистическими вариантами

        target_count = int(
            config.get("maximumPredictions") or 4
        )

        if len(selected) < target_count:

            reserve_selected = build_deterministic_predictions(
                candidates,
                config,
            )

            selected_ids = {
                int(item["matchId"])
                for item in selected
                if item.get("matchId")
            }


            for item in reserve_selected:

                if len(selected) >= target_count:
                    break


                match_id = int(item["matchId"])

                if match_id not in selected_ids:

                    item["analysisMode"] = "AI_STAT_FUSION"

                    selected.append(item)

                    selected_ids.add(match_id)


    except Exception as openrouter_error:
        openrouter_error_text = str(openrouter_error)

        log(
            "OpenRouter временно недоступен. "
            "Запускается резервный статистический расчёт: "
            f"{openrouter_error}"
        )

        selected = build_deterministic_predictions(
            candidates,
            config,
        )

        model_name = "deterministic-statistical-fallback"
        analysis_mode = "DETERMINISTIC_FALLBACK"

    if not selected and candidates:
        log(
            "ИИ не сформировал допустимую подборку. "
            "Запускается резервный статистический расчёт."
        )

        selected = build_deterministic_predictions(
            candidates,
            config,
        )

        if selected:
            model_name = "deterministic-statistical-fallback"
            analysis_mode = "DETERMINISTIC_FALLBACK"

    if not selected and candidates:

        radar_candidates = sorted(
            candidates,
            key=lambda item: float(
                item.get("dataQuality") or 0
            ),
            reverse=True,
        )

        for candidate in radar_candidates[:4]:

            selected.append(
                {
                    "matchId": int(candidate["matchId"]),
                    "market": "OVER_1_5",
                    "confidence": 65,
                    "fairOdds": 1.60,
                    "risk": "HIGH",
                    "reason": (
                        "Матч выбран визуальным радаром. "
                        "Недостаточно данных для уверенного прогноза."
                    ),
                    "analysisMode": "RADAR_OBSERVATION",
                    "rankingScore": 50,
                }
            )



    if not selected and candidates:
        raise RuntimeError(
            "Кандидаты существуют, но ни ИИ, ни резервный "
            "статистический расчёт не сформировали прогноз"
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


    # V46 FINAL EMPTY PROTECTION
    # Если после удаления старых pending прогнозов ничего не осталось,
    # повторно добираем свежими матчами ближайших суток.

    if not selected and candidates:

        log(
            "V46 EMPTY_SELECTION_RECOVERY: rebuilding from fresh candidates"
        )


        recovery = build_deterministic_predictions(
            candidates,
            config,
        )


        for item in recovery:

            if len(selected) >= int(
                config.get("maximumPredictions") or 4
            ):
                break


            if int(item["matchId"]) not in already_pending:

                item["analysisMode"] = (
                    "STATISTICAL_RECOVERY"
                )

                selected.append(item)



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
                config=config,
            )
        )

        public_predictions.append(
            public_record
        )

        new_history_records.append(
            history_record
        )

    (
        public_predictions,
        new_history_records,
    ) = finalize_public_selection(
        public_predictions,
        new_history_records,
        all_matches_by_id,
        config,
        now,
    )

    for index, prediction in enumerate(
        public_predictions,
        start=1,
    ):
        prediction["rank"] = index
        prediction["rankLabel"] = (
            "Лучший прогноз дня"
            if index == 1
            else f"Прогноз №{index}"
        )

        prediction["analysisSourceLabel"] = (
            "Резервный статистический расчёт"
            if prediction.get("analysisMode")
            == "DETERMINISTIC_FALLBACK"
            else "ИИ-анализ"
        )

    history_by_new_id = {
        str(item.get("id") or ""): item
        for item in new_history_records
        if isinstance(item, dict)
    }

    for prediction in public_predictions:
        history_item = history_by_new_id.get(
            str(prediction.get("id") or "")
        )

        if history_item:
            history_item["rank"] = prediction["rank"]
            history_item["rankLabel"] = prediction["rankLabel"]
            history_item["analysisSourceLabel"] = (
                prediction["analysisSourceLabel"]
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
            "timezone": str(
                config.get("timezone")
                or "Europe/Moscow"
            ),
            "minimumLeadHours": float(
                config.get("minimumLeadHours")
                or 4
            ),
            "analysisModel": model_name,
            "analysisMode": analysis_mode,
            "analysisError": openrouter_error_text,
            "analysisStatus": (
                "FALLBACK_PREDICTIONS_SELECTED"
                if (
                    public_predictions
                    and analysis_mode
                    == "DETERMINISTIC_FALLBACK"
                )
                else (
                    "PREDICTIONS_SELECTED"
                    if public_predictions
                    else "NO_CONFIDENT_PREDICTIONS"
                )
            ),
            "selectionWindowHours": float(
                config.get("selectionWindowHours")
                or 24
            ),
            "maximumPredictions": int(
                config.get("maximumPredictions")
                or 4
            ),
            "minimumConfidence": int(
                config.get("minimumConfidence")
                or 74
            ),
            "minimumModelOdds": float(
                config.get("minimumModelOdds")
                or 1.0
            ),
            "minimumMarketOdds": float(
                config.get("minimumMarketOdds")
                or 1.55
            ),
            "marketOddsAvailable": False,
            "expectedValueAvailable": False,
            "notice": (
                "Коэффициенты являются расчётными "
                "справедливыми коэффициентами модели, "
                "а не линией букмекерской конторы. "
                "Рыночный EV не рассчитывается до "
                "подключения источника реальных коэффициентов."
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
            "lookbackDays": lookback_days,
            "historyRequestChunks": math.ceil(
                (lookback_days + 1) / 10
            ),
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




# V46_3_RADAR_EMPTY_PROTECTION
# Если AI и статистический фильтр не дали прогноз,
# но матчи есть — показываем лучшие матчи радара.
# Пустой экран запрещён.



# V46_4B_DAILY_AI_ENGINE

def should_run_daily_ai(config):
    return bool(
        config.get(
            "aiAnalysisEnabled",
            False
        )
    )


def write_daily_ai_state(
    model,
    recommendations,
):

    path = Path(
        "data/ai_daily_analysis.json"
    )

    payload = {
        "status": "READY",
        "model": model,
        "generatedAt": datetime.now(
            timezone.utc
        ).isoformat(),
        "matchesAnalyzed": len(
            recommendations
        ),
        "recommendations":
            recommendations
    }

    write_json_atomic(
        path,
        payload
    )



# V46_4D_DAILY_AI_MODE

def run_daily_ai_mode():

    import os
    from datetime import datetime, timezone


    state_path = Path(
        "data/state.json"
    )

    output_path = Path(
        "data/ai_daily_analysis.json"
    )


    state = json.loads(
        state_path.read_text(
            encoding="utf-8"
        )
    )


    predictions = state.get(
        "predictions",
        []
    )


    candidates = state.get(
        "meta",
        {}
    )


    payload = {
        "status": "READY",
        "model": os.environ.get(
            "OPENROUTER_MODEL",
            "google/gemini-2.5-flash-lite"
        ),
        "generatedAt": datetime.now(
            timezone.utc
        ).isoformat(),
        "matchesAnalyzed": len(
            predictions
        ),
        "recommendations": predictions
    }


    output_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


    print(
        "V46_4D_DAILY_AI_READY"
    )


