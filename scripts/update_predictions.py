#!/usr/bin/env python3
"""
AI Football Lab
Автоматическое получение матчей, подготовка статистики,
анализ через OpenRouter и обновление публичного состояния сайта.

Используются только модули стандартной библиотеки Python.
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
    timeout_seconds: int | float | None = None,
    retries: int | None = None,
) -> dict[str, Any]:
    """Выполняет JSON-запрос с ограниченными повторными попытками."""

    request_headers = {
        "Accept": "application/json",
        "User-Agent": "AI-Football-Lab/3.0",
    }

    if headers:
        request_headers.update(headers)

    body: bytes | None = None

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request_timeout = max(
        1,
        int(timeout_seconds or HTTP_TIMEOUT_SECONDS),
    )

    maximum_attempts = max(
        1,
        int(retries or REQUEST_RETRIES),
    )

    last_error: Exception | None = None

    for attempt in range(1, maximum_attempts + 1):
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=request_headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=request_timeout,
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

            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise last_error

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as error:
            last_error = error

        if attempt < maximum_attempts:
            delay = min(15, attempt * 5)
            log(
                f"Повтор запроса через {delay} секунд. "
                f"Попытка {attempt}/{maximum_attempts}"
            )
            time.sleep(delay)

    raise RuntimeError(
        "Запрос завершился ошибкой после "
        f"{maximum_attempts} попыток: {last_error}"
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
    parameters: dict[str, str] = {
        "dateFrom": iso_date(date_from),
        "dateTo": iso_date(date_to),
    }

    normalized_competitions = sorted({
        str(item).strip()
        for item in competitions
        if str(item).strip()
    })

    # Empty list means: request every competition accessible to the
    # authenticated football-data.org account. No fixed country list is
    # imposed by the application.
    if normalized_competitions:
        parameters["competitions"] = ",".join(normalized_competitions)

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
    """Строит общую и домашнюю/выездную форму команд."""

    rows_by_team: dict[int, list[dict[str, Any]]] = {}

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

        rows_by_team.setdefault(home_id, []).append(
            {
                "points": home_points,
                "goalsFor": home_goals,
                "goalsAgainst": away_goals,
                "venue": "HOME",
                "date": match.get("utcDate"),
            }
        )

        rows_by_team.setdefault(away_id, []).append(
            {
                "points": away_points,
                "goalsFor": away_goals,
                "goalsAgainst": home_goals,
                "venue": "AWAY",
                "date": match.get("utcDate"),
            }
        )

    def summarize(
        rows: list[dict[str, Any]],
        limit: int,
    ) -> dict[str, Any]:
        recent = rows[-limit:]
        games = len(recent)

        if not games:
            return {
                "games": 0,
                "points": 0,
                "pointsPerGame": 0.0,
                "goalsFor": 0,
                "goalsAgainst": 0,
                "goalsForPerGame": 0.0,
                "goalsAgainstPerGame": 0.0,
                "winRate": 0.0,
                "drawRate": 0.0,
                "lossRate": 0.0,
                "nonLossRate": 0.0,
                "scoredRate": 0.0,
                "concededRate": 0.0,
                "cleanSheetRate": 0.0,
                "over15Rate": 0.0,
                "under35Rate": 0.0,
                "bothScoreRate": 0.0,
            }

        points = sum(int(item["points"]) for item in recent)
        goals_for = sum(int(item["goalsFor"]) for item in recent)
        goals_against = sum(int(item["goalsAgainst"]) for item in recent)

        wins = sum(1 for item in recent if int(item["points"]) == 3)
        draws = sum(1 for item in recent if int(item["points"]) == 1)
        losses = games - wins - draws
        scored_games = sum(1 for item in recent if int(item["goalsFor"]) > 0)
        conceded_games = sum(1 for item in recent if int(item["goalsAgainst"]) > 0)
        clean_sheets = sum(1 for item in recent if int(item["goalsAgainst"]) == 0)
        over_15_games = sum(
            1
            for item in recent
            if int(item["goalsFor"]) + int(item["goalsAgainst"]) >= 2
        )
        under_35_games = sum(
            1
            for item in recent
            if int(item["goalsFor"]) + int(item["goalsAgainst"]) <= 3
        )
        both_score_games = sum(
            1
            for item in recent
            if int(item["goalsFor"]) > 0
            and int(item["goalsAgainst"]) > 0
        )

        return {
            "games": games,
            "points": points,
            "pointsPerGame": round(points / games, 3),
            "goalsFor": goals_for,
            "goalsAgainst": goals_against,
            "goalsForPerGame": round(goals_for / games, 3),
            "goalsAgainstPerGame": round(goals_against / games, 3),
            "winRate": round(wins / games, 4),
            "drawRate": round(draws / games, 4),
            "lossRate": round(losses / games, 4),
            "nonLossRate": round((wins + draws) / games, 4),
            "scoredRate": round(scored_games / games, 4),
            "concededRate": round(conceded_games / games, 4),
            "cleanSheetRate": round(clean_sheets / games, 4),
            "over15Rate": round(over_15_games / games, 4),
            "under35Rate": round(under_35_games / games, 4),
            "bothScoreRate": round(both_score_games / games, 4),
        }

    result: dict[int, dict[str, Any]] = {}

    for team_id, rows in rows_by_team.items():
        overall = summarize(rows, 8)
        home_rows = [item for item in rows if item.get("venue") == "HOME"]
        away_rows = [item for item in rows if item.get("venue") == "AWAY"]

        overall["homeVenue"] = summarize(home_rows, 5)
        overall["awayVenue"] = summarize(away_rows, 5)
        result[team_id] = overall

    return result



def candidate_quality_score(
    home_form: dict[str, Any],
    away_form: dict[str, Any],
) -> float:
    home_games = int(home_form.get("games") or 0)
    away_games = int(away_form.get("games") or 0)

    home_venue_games = int(
        (home_form.get("homeVenue") or {}).get("games") or 0
    )
    away_venue_games = int(
        (away_form.get("awayVenue") or {}).get("games") or 0
    )

    overall_coverage = (
        min(home_games, 8) + min(away_games, 8)
    ) / 16

    venue_coverage = (
        min(home_venue_games, 5) + min(away_venue_games, 5)
    ) / 10

    maximum_games = max(home_games, away_games, 1)
    sample_balance = 1 - abs(home_games - away_games) / maximum_games

    home_over = float(home_form.get("over15Rate") or 0)
    away_over = float(away_form.get("over15Rate") or 0)
    observable_signal = (home_over + away_over) / 2

    score = (
        overall_coverage * 55
        + venue_coverage * 20
        + sample_balance * 10
        + observable_signal * 15
    )

    return round(clamp_number(score, 0, 100), 2)



def select_diverse_candidates(
    candidates: list[dict[str, Any]],
    maximum_candidates: int,
) -> list[dict[str, Any]]:
    """Round-robin candidate pool across available competitions.

    The strongest candidate of every available competition is considered
    before the second candidate of any competition. If only one competition
    is available, its candidates are still returned normally.
    """

    maximum_candidates = max(0, int(maximum_candidates))

    if maximum_candidates == 0 or not candidates:
        return []

    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("dataQuality") or 0),
            str(item.get("utcDate") or ""),
            int(item.get("matchId") or 0),
        ),
    )

    buckets: dict[str, list[dict[str, Any]]] = {}

    for candidate in ranked:
        competition = candidate.get("competition") or {}
        country = str(candidate.get("country") or "UNKNOWN")
        competition_code = str(competition.get("code") or "").strip()
        competition_name = str(competition.get("name") or "").strip()
        bucket_key = "|".join(
            value
            for value in (
                country,
                competition_code,
                competition_name,
            )
            if value
        ) or "UNKNOWN"
        buckets.setdefault(bucket_key, []).append(candidate)

    bucket_keys = sorted(
        buckets,
        key=lambda key: (
            -float(buckets[key][0].get("dataQuality") or 0),
            str(buckets[key][0].get("utcDate") or ""),
            key,
        ),
    )

    selected: list[dict[str, Any]] = []
    round_index = 0

    while len(selected) < maximum_candidates:
        progress = False

        for key in bucket_keys:
            bucket = buckets[key]

            if round_index >= len(bucket):
                continue

            selected.append(bucket[round_index])
            progress = True

            if len(selected) >= maximum_candidates:
                break

        if not progress:
            break

        round_index += 1

    return selected


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

    maximum_candidates = max(
        1,
        int(config.get("maximumCandidates") or 24),
    )

    return select_diverse_candidates(
        candidates,
        maximum_candidates,
    )


def build_analysis_prompt(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    allowed_markets = [
        str(value)
        for value in config.get("allowedMarkets", [])
    ]

    minimum_confidence = int(
        config.get("minimumConfidence") or 70
    )

    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 5),
    )

    minimum_probability = float(
        config.get("minimumProbability") or 0.52
    )

    maximum_probability = float(
        config.get("maximumProbability") or 0.69
    )

    minimum_model_odds = float(
        config.get("minimumModelOdds") or 1.45
    )

    if minimum_model_odds > 1:
        maximum_probability = min(
            maximum_probability,
            1 / minimum_model_odds,
        )

    candidate_json = json.dumps(
        candidates,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"""
Ты — строгий аналитический модуль футбольных матчей.

Используй только переданные числовые показатели. Не используй внешние
знания, составы, новости, слухи и букмекерские коэффициенты. Не выдумывай
отсутствующие данные.

Отбери не более {maximum_predictions} прогнозов. Пустой массив допустим,
если статистическое основание недостаточно. Не создавай прогноз ради
заполнения карточек.

Важно различать два показателя:
- confidence — надёжность самого анализа и полнота данных;
- probability — оценка вероятности наступления выбранного события.

Требования:
1. confidence должен быть не ниже {minimum_confidence}.
2. probability должен быть от {minimum_probability:.2f} до
   {maximum_probability:.4f}.
3. Один прогноз на один matchId.
4. market — только одно из значений:
   {json.dumps(allowed_markets, ensure_ascii=False)}
5. risk — только LOW или MEDIUM.
6. reason — краткое статистическое объяснение на русском языке.
7. Не возвращай fairOdds или букмекерские коэффициенты: backend рассчитает
   математический коэффициент как 1 / probability.
8. Верни только корректный JSON без markdown и текста вокруг него.

Формат ответа:
{{
  "predictions": [
    {{
      "matchId": 123,
      "market": "OVER_1_5",
      "confidence": 74,
      "probability": 0.64,
      "risk": "MEDIUM",
      "reason": "Краткое статистическое основание."
    }}
  ]
}}

КАНДИДАТЫ И СТАТИСТИКА:
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
    """Вызывает OpenRouter без вложенного каскада повторов."""

    configured_model = str(
        os.getenv("OPENROUTER_MODEL")
        or "google/gemini-2.5-flash-lite"
    ).strip()

    if not configured_model:
        configured_model = "openrouter/auto"

    candidate_models = [configured_model]

    if configured_model != "openrouter/auto":
        candidate_models.append("openrouter/auto")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": PUBLIC_SITE_URL,
        "X-Title": "AI Football Lab",
    }

    last_error: Exception | None = None

    for model_index, model_name in enumerate(candidate_models):
        attempts = 2 if model_index == 0 else 1

        for attempt in range(1, attempts + 1):
            payload = {
                "model": model_name,
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
                },
            }

            log(
                "Запрос OpenRouter: "
                f"модель={model_name}; попытка={attempt}/{attempts}"
            )

            try:
                response = request_json(
                    OPENROUTER_API_URL,
                    method="POST",
                    headers=headers,
                    payload=payload,
                    timeout_seconds=180,
                    retries=1,
                )

                choices = response.get("choices") or []

                if not choices:
                    raise RuntimeError("OpenRouter не вернул choices")

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
                    raise RuntimeError("OpenRouter вернул пустой content")

                parsed = extract_json_object(content)
                returned_model = str(
                    response.get("model") or model_name
                )

                log(
                    "OpenRouter успешно ответил; "
                    f"модель={returned_model}"
                )

                return parsed, returned_model

            except Exception as error:
                last_error = error
                log(
                    "Предупреждение OpenRouter: "
                    f"{type(error).__name__}: {error}"
                )

                if attempt < attempts:
                    time.sleep(10 * attempt)

    raise RuntimeError(
        "OpenRouter недоступен после ограниченного числа попыток: "
        f"{last_error}"
    )



def normalize_model_predictions(
    model_result: dict[str, Any],
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_predictions = model_result.get("predictions", [])

    if not isinstance(raw_predictions, list):
        raise RuntimeError("Поле predictions должно быть массивом")

    candidates_by_id = {
        int(candidate["matchId"]): candidate
        for candidate in candidates
    }

    allowed_markets = {
        str(value)
        for value in config.get("allowedMarkets", [])
    }

    minimum_confidence = int(
        config.get("minimumConfidence") or 70
    )
    win_market_minimum_confidence = int(
        config.get("winMarketMinimumConfidence")
        or max(minimum_confidence + 3, 74)
    )
    minimum_probability = float(
        config.get("minimumProbability") or 0.52
    )
    maximum_probability = float(
        config.get("maximumProbability") or 0.69
    )
    minimum_model_odds = float(
        config.get("minimumModelOdds") or 1.45
    )
    maximum_model_odds = float(
        config.get("maximumModelOdds") or 2.10
    )
    minimum_data_quality = float(
        config.get("minimumDataQuality") or 42
    )
    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 5),
    )

    if minimum_model_odds > 1:
        maximum_probability = min(
            maximum_probability,
            1 / minimum_model_odds,
        )

    if maximum_model_odds > 1:
        minimum_probability = max(
            minimum_probability,
            1 / maximum_model_odds,
        )

    win_markets = {"HOME_WIN", "AWAY_WIN"}
    normalized: list[dict[str, Any]] = []
    used_match_ids: set[int] = set()

    for index, raw in enumerate(raw_predictions):
        if not isinstance(raw, dict):
            log(f"AI prediction #{index + 1} отклонён: не объект")
            continue

        try:
            match_id = int(raw.get("matchId"))
            confidence = int(round(float(raw.get("confidence"))))
            probability = float(raw.get("probability"))
        except (TypeError, ValueError):
            log(f"AI prediction #{index + 1} отклонён: неверные числа")
            continue

        if 1 < probability <= 100:
            probability /= 100

        market = str(raw.get("market") or "").upper().strip()
        risk = str(raw.get("risk") or "").upper().strip()
        reason = str(raw.get("reason") or "").strip()

        candidate = candidates_by_id.get(match_id)

        if candidate is None:
            log(f"AI prediction отклонён: неизвестный matchId={match_id}")
            continue

        if match_id in used_match_ids:
            log(f"AI prediction отклонён: дубль matchId={match_id}")
            continue

        if market not in allowed_markets:
            log(
                "AI prediction отклонён: "
                f"market={market}; matchId={match_id}"
            )
            continue

        data_quality = float(candidate.get("dataQuality") or 0)

        if data_quality < minimum_data_quality:
            log(
                "AI prediction отклонён по качеству данных: "
                f"matchId={match_id}; quality={data_quality:.1f}"
            )
            continue

        required_confidence = (
            win_market_minimum_confidence
            if market in win_markets
            else minimum_confidence
        )

        maximum_supported_confidence = int(
            round(clamp_number(62 + data_quality * 0.28, 68, 88))
        )
        confidence = min(
            max(0, min(confidence, 100)),
            maximum_supported_confidence,
        )

        if confidence < required_confidence:
            log(
                "AI prediction отклонён по уверенности: "
                f"matchId={match_id}; confidence={confidence}; "
                f"required={required_confidence}"
            )
            continue

        if not minimum_probability <= probability <= maximum_probability:
            log(
                "AI prediction отклонён по вероятности: "
                f"matchId={match_id}; probability={probability:.4f}; "
                f"range={minimum_probability:.4f}-{maximum_probability:.4f}"
            )
            continue

        if not reason:
            log(f"AI prediction отклонён без reason: matchId={match_id}")
            continue

        fair_odds = round(1 / probability, 2)

        if not minimum_model_odds <= fair_odds <= maximum_model_odds:
            log(
                "AI prediction отклонён по расчётному коэффициенту: "
                f"matchId={match_id}; fairOdds={fair_odds:.2f}"
            )
            continue

        if risk not in {"LOW", "MEDIUM"}:
            risk = "LOW" if confidence >= 78 else "MEDIUM"

        ranking_score = round(
            confidence * 0.50
            + probability * 100 * 0.25
            + data_quality * 0.25,
            4,
        )

        normalized.append(
            {
                "matchId": match_id,
                "market": market,
                "confidence": confidence,
                "probability": round(probability, 4),
                "fairOdds": fair_odds,
                "risk": risk,
                "reason": reason[:500],
                "analysisMode": "AI",
                "rankingScore": ranking_score,
                "dataQuality": round(data_quality, 2),
            }
        )
        used_match_ids.add(match_id)

    normalized.sort(
        key=lambda item: (
            -float(item.get("rankingScore") or 0),
            -int(item.get("confidence") or 0),
            str(item.get("matchId") or ""),
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
        (old_state.get("meta") or {}).get("mode") or ""
    )

    if current_mode == "real":
        state = copy.deepcopy(old_state)
        state.setdefault("meta", {})
        state.setdefault("predictions", [])
        state.setdefault("history", [])
        state.setdefault("statistics", {})
        state.setdefault("bank", {})
        return state

    starting_bank = float(
        config.get("startingVirtualBank") or 10000
    )

    log(
        "Обнаружен демонстрационный или пустой режим. "
        "Создаётся чистое реальное состояние."
    )

    return {
        "meta": {
            "version": "3.0.0",
            "mode": "real",
            "updatedAt": None,
            "analyzedMatches": 0,
            "candidateMatches": 0,
            "selectedPredictions": 0,
            "source": "football-data.org",
            "analysisProvider": "Не запускался",
            "timezone": str(
                config.get("timezone") or "Europe/Moscow"
            ),
            "minimumLeadHours": float(
                config.get("minimumLeadHours") or 3
            ),
            "notice": (
                "Расчётные коэффициенты модели не являются "
                "букмекерской линией."
            ),
        },
        "bank": {
            "starting": starting_bank,
            "current": starting_bank,
            "stakePercent": int(
                config.get("maximumTotalStakePercent") or 20
            ),
            "roi": 0.0,
            "maxDrawdown": 0.0,
            "history": [
                {
                    "date": utc_now().date().isoformat(),
                    "value": starting_bank,
                    "event": "REAL_MODE_START",
                }
            ],
        },
        "statistics": {
            "averageOdds": 0.0,
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

        try:
            match_id = int(entry.get("sourceMatchId"))
        except (TypeError, ValueError):
            continue

        match = matches_by_id.get(match_id)

        if not match:
            continue

        match_status = str(match.get("status") or "").upper()

        if match.get("utcDate"):
            entry["utcDate"] = str(match.get("utcDate"))

        entry.update(extract_public_match_status(match))

        if match_status == "CANCELLED":
            entry["status"] = "void"
            entry["profit"] = 0.0
            entry["settledAt"] = utc_now().isoformat()
            entry["settlementReason"] = "MATCH_CANCELLED"
            settled_count += 1
            continue

        result = final_score(match)

        if result is None:
            continue

        home_goals, away_goals = result
        market = str(entry.get("market") or "")

        try:
            won = evaluate_market(
                market,
                home_goals,
                away_goals,
            )
        except RuntimeError:
            entry["status"] = "void"
            entry["profit"] = 0.0
            entry["settledAt"] = utc_now().isoformat()
            entry["settlementReason"] = "UNKNOWN_MARKET"
            settled_count += 1
            continue

        stake = float(entry.get("stake") or 0)
        fair_odds = float(
            entry.get("fairOdds")
            or entry.get("odds")
            or 1
        )

        if won:
            profit = stake * max(0.0, fair_odds - 1)
            current_bank += profit
            entry["status"] = "won"
            entry["profit"] = round(profit, 2)
        else:
            current_bank -= stake
            entry["status"] = "lost"
            entry["profit"] = round(-stake, 2)

        entry["score"] = f"{home_goals}:{away_goals}"
        entry["settledAt"] = utc_now().isoformat()
        entry["settlementOddsType"] = "MODEL_FAIR_SIMULATION"

        bank.setdefault("history", []).append(
            {
                "date": utc_now().date().isoformat(),
                "value": round(current_bank, 2),
                "event": (
                    "PREDICTION_WON"
                    if won
                    else "PREDICTION_LOST"
                ),
                "matchId": match_id,
            }
        )

        settled_count += 1

    bank["current"] = round(current_bank, 2)
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


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _standard_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0

    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _preferred_venue_form(
    form: dict[str, Any],
    venue_key: str,
) -> dict[str, Any]:
    venue = form.get(venue_key) or {}

    if int(venue.get("games") or 0) >= 2:
        return venue

    return form


def _poisson_over_15(expected_total: float) -> float:
    expected_total = clamp_number(expected_total, 0.1, 5.0)
    return 1 - math.exp(-expected_total) * (1 + expected_total)


def _poisson_under_35(expected_total: float) -> float:
    expected_total = clamp_number(expected_total, 0.1, 5.0)
    cumulative = sum(
        math.exp(-expected_total)
        * expected_total ** goals
        / math.factorial(goals)
        for goals in range(4)
    )
    return clamp_number(cumulative, 0, 1)


def build_deterministic_predictions(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Формирует честный статистический пул без искусственных заглушек."""

    if not candidates:
        return []

    allowed_markets = {
        str(value)
        for value in config.get("allowedMarkets", [])
    }

    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 5),
    )
    minimum_confidence = int(
        config.get("minimumConfidence") or 70
    )
    win_market_minimum_confidence = int(
        config.get("winMarketMinimumConfidence")
        or max(minimum_confidence + 3, 74)
    )
    minimum_probability = float(
        config.get("minimumProbability") or 0.52
    )
    maximum_probability = float(
        config.get("maximumProbability") or 0.69
    )
    minimum_model_odds = float(
        config.get("minimumModelOdds") or 1.45
    )
    maximum_model_odds = float(
        config.get("maximumModelOdds") or 2.10
    )
    minimum_data_quality = float(
        config.get("minimumDataQuality") or 42
    )

    if minimum_model_odds > 1:
        maximum_probability = min(
            maximum_probability,
            1 / minimum_model_odds,
        )

    if maximum_model_odds > 1:
        minimum_probability = max(
            minimum_probability,
            1 / maximum_model_odds,
        )

    win_markets = {"HOME_WIN", "AWAY_WIN"}
    ranked: list[dict[str, Any]] = []

    for candidate in candidates:
        home_form = candidate.get("homeTeam", {}).get("form", {})
        away_form = candidate.get("awayTeam", {}).get("form", {})
        home_venue = _preferred_venue_form(home_form, "homeVenue")
        away_venue = _preferred_venue_form(away_form, "awayVenue")

        data_quality = float(candidate.get("dataQuality") or 0)

        if data_quality < minimum_data_quality:
            continue

        home_ppg = float(home_venue.get("pointsPerGame") or 0)
        away_ppg = float(away_venue.get("pointsPerGame") or 0)
        home_gf = float(home_venue.get("goalsForPerGame") or 0)
        home_ga = float(home_venue.get("goalsAgainstPerGame") or 0)
        away_gf = float(away_venue.get("goalsForPerGame") or 0)
        away_ga = float(away_venue.get("goalsAgainstPerGame") or 0)

        expected_home = clamp_number((home_gf + away_ga) / 2, 0.1, 3.0)
        expected_away = clamp_number((away_gf + home_ga) / 2, 0.1, 3.0)
        expected_total = expected_home + expected_away

        form_edge = home_ppg - away_ppg
        goal_edge = expected_home - expected_away

        market_components: dict[str, list[float]] = {
            "OVER_1_5": [
                _poisson_over_15(expected_total),
                float(home_form.get("over15Rate") or 0),
                float(away_form.get("over15Rate") or 0),
            ],
            "UNDER_3_5": [
                _poisson_under_35(expected_total),
                float(home_form.get("under35Rate") or 0),
                float(away_form.get("under35Rate") or 0),
            ],
            "HOME_OVER_0_5": [
                float(home_venue.get("scoredRate") or 0),
                float(away_venue.get("concededRate") or 0),
                clamp_number(expected_home / 1.5, 0, 1),
            ],
            "AWAY_OVER_0_5": [
                float(away_venue.get("scoredRate") or 0),
                float(home_venue.get("concededRate") or 0),
                clamp_number(expected_away / 1.5, 0, 1),
            ],
            "BOTH_SCORE": [
                float(home_form.get("bothScoreRate") or 0),
                float(away_form.get("bothScoreRate") or 0),
                float(home_venue.get("scoredRate") or 0)
                * float(away_venue.get("concededRate") or 0),
                float(away_venue.get("scoredRate") or 0)
                * float(home_venue.get("concededRate") or 0),
            ],
            "HOME_OR_DRAW": [
                float(home_venue.get("nonLossRate") or 0),
                1 - float(away_venue.get("winRate") or 0),
                clamp_number(0.58 + form_edge * 0.08, 0.35, 0.80),
            ],
            "AWAY_OR_DRAW": [
                float(away_venue.get("nonLossRate") or 0),
                1 - float(home_venue.get("winRate") or 0),
                clamp_number(0.58 - form_edge * 0.08, 0.35, 0.80),
            ],
            "HOME_WIN": [
                float(home_venue.get("winRate") or 0),
                float(away_venue.get("lossRate") or 0),
                clamp_number(0.46 + form_edge * 0.09 + goal_edge * 0.07, 0.20, 0.75),
            ],
            "AWAY_WIN": [
                float(away_venue.get("winRate") or 0),
                float(home_venue.get("lossRate") or 0),
                clamp_number(0.46 - form_edge * 0.09 - goal_edge * 0.07, 0.20, 0.75),
            ],
        }

        for market, components in market_components.items():
            if market not in allowed_markets:
                continue

            cleaned_components = [
                clamp_number(float(value), 0, 1)
                for value in components
            ]
            probability = _mean(cleaned_components)

            if not minimum_probability <= probability <= maximum_probability:
                continue

            fair_odds = round(1 / probability, 2)

            if not minimum_model_odds <= fair_odds <= maximum_model_odds:
                continue

            agreement = clamp_number(
                100 - _standard_deviation(cleaned_components) * 220,
                0,
                100,
            )
            signal_strength = clamp_number(
                (probability - 0.50) / 0.20 * 100,
                0,
                100,
            )
            confidence = int(
                round(
                    0.55 * data_quality
                    + 0.25 * agreement
                    + 0.20 * signal_strength
                )
            )
            confidence = int(clamp_number(confidence, 0, 88))

            required_confidence = (
                win_market_minimum_confidence
                if market in win_markets
                else minimum_confidence
            )

            if confidence < required_confidence:
                continue

            reason_map = {
                "OVER_1_5": (
                    "Модель формы оценивает вероятность минимум двух голов "
                    f"в {probability * 100:.0f}%; средний ожидаемый тотал "
                    f"{expected_total:.2f}."
                ),
                "UNDER_3_5": (
                    "Модель формы оценивает вероятность не более трёх голов "
                    f"в {probability * 100:.0f}%; средний ожидаемый тотал "
                    f"{expected_total:.2f}."
                ),
                "HOME_OVER_0_5": (
                    "Хозяева регулярно забивают, а гости допускают голы; "
                    f"оценка события {probability * 100:.0f}%."
                ),
                "AWAY_OVER_0_5": (
                    "Гости регулярно забивают, а хозяева допускают голы; "
                    f"оценка события {probability * 100:.0f}%."
                ),
                "BOTH_SCORE": (
                    "Показатели результативности и пропущенных голов обеих "
                    f"команд дают оценку {probability * 100:.0f}%."
                ),
                "HOME_OR_DRAW": (
                    "Домашняя форма хозяев и выездная форма гостей дают "
                    f"оценку непоражения хозяев {probability * 100:.0f}%."
                ),
                "AWAY_OR_DRAW": (
                    "Выездная форма гостей и домашняя форма хозяев дают "
                    f"оценку непоражения гостей {probability * 100:.0f}%."
                ),
                "HOME_WIN": (
                    "Преимущество хозяев по форме и ожидаемым голам даёт "
                    f"оценку победы {probability * 100:.0f}%."
                ),
                "AWAY_WIN": (
                    "Преимущество гостей по форме и ожидаемым голам даёт "
                    f"оценку победы {probability * 100:.0f}%."
                ),
            }

            ranking_score = round(
                confidence * 0.55
                + probability * 100 * 0.25
                + data_quality * 0.20,
                4,
            )

            ranked.append(
                {
                    "matchId": int(candidate["matchId"]),
                    "market": market,
                    "confidence": confidence,
                    "probability": round(probability, 4),
                    "fairOdds": fair_odds,
                    "risk": "LOW" if confidence >= 78 else "MEDIUM",
                    "reason": reason_map[market],
                    "analysisMode": "DETERMINISTIC_STATISTICAL",
                    "rankingScore": ranking_score,
                    "dataQuality": round(data_quality, 2),
                }
            )

    ranked.sort(
        key=lambda item: (
            -float(item.get("rankingScore") or 0),
            -int(item.get("confidence") or 0),
            str(item.get("matchId") or ""),
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
    kickoff_utc = parse_utc_datetime(candidate["utcDate"])
    timezone = configured_timezone(config)
    kickoff = kickoff_utc.astimezone(timezone)
    timezone_name = str(
        config.get("timezone") or "Europe/Moscow"
    )

    competition = candidate["competition"]
    home_team = candidate["homeTeam"]["name"]
    away_team = candidate["awayTeam"]["name"]
    market = str(prediction["market"])
    market_label = MARKET_LABELS[market]

    country_value = candidate.get("country", "")
    country = COUNTRY_TRANSLATIONS.get(
        country_value,
        country_value or "Международный турнир",
    )

    risk_code = str(prediction.get("risk") or "MEDIUM").upper()
    risk_label = "Низкий" if risk_code == "LOW" else "Средний"
    fair_odds = round(float(prediction["fairOdds"]), 2)
    probability = round(float(prediction["probability"]), 4)
    confidence = int(prediction["confidence"])

    common = {
        "id": f"real-{candidate['matchId']}-{market.lower()}",
        "sourceMatchId": int(candidate["matchId"]),
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
        "odds": fair_odds,
        "fairOdds": fair_odds,
        "oddsLabel": "Расчётный коэффициент модели",
        "probability": probability,
        "probabilityPercent": round(probability * 100, 1),
        "confidence": confidence,
        "risk": risk_label,
        "riskCode": risk_code,
        "reason": str(prediction.get("reason") or "")[:500],
        "analysisMode": str(
            prediction.get("analysisMode") or "AI"
        ),
        "rankingScore": round(
            float(prediction.get("rankingScore") or 0),
            4,
        ),
        "dataQuality": round(
            float(
                prediction.get("dataQuality")
                or candidate.get("dataQuality")
                or 0
            ),
            2,
        ),
        "coefficientType": "MODEL_FAIR",
        "marketOddsAvailable": False,
        "expectedValueAvailable": False,
    }

    public_prediction = dict(common)

    history_record = dict(common)
    history_record.update(
        {
            "stake": round(stake, 2),
            "score": "",
            "status": "pending",
            "publishedAt": utc_now().isoformat(),
            "settlementOddsType": "MODEL_FAIR_SIMULATION",
        }
    )

    return public_prediction, history_record


def pending_history_to_public_record(
    entry: dict[str, Any],
    match: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        match_id = int(entry.get("sourceMatchId"))
        probability = float(entry.get("probability") or 0)
        fair_odds = float(
            entry.get("fairOdds") or entry.get("odds") or 0
        )
        confidence = int(entry.get("confidence") or 0)
    except (TypeError, ValueError):
        return None

    if probability <= 0 and fair_odds > 1:
        probability = 1 / fair_odds

    risk_code = str(entry.get("riskCode") or "").upper()

    if risk_code not in {"LOW", "MEDIUM"}:
        risk_code = "LOW" if confidence >= 78 else "MEDIUM"

    result = {
        "id": str(entry.get("id") or f"real-{match_id}"),
        "sourceMatchId": match_id,
        "league": str(entry.get("league") or "Неизвестная лига"),
        "country": str(entry.get("country") or ""),
        "date": str(entry.get("date") or ""),
        "time": str(entry.get("time") or ""),
        "utcDate": str(
            (match or {}).get("utcDate")
            or entry.get("utcDate")
            or ""
        ),
        "timezone": str(
            entry.get("timezone")
            or config.get("timezone")
            or "Europe/Moscow"
        ),
        "home": str(entry.get("home") or ""),
        "away": str(entry.get("away") or ""),
        "market": str(entry.get("market") or ""),
        "pick": str(entry.get("pick") or ""),
        "odds": round(fair_odds, 2),
        "fairOdds": round(fair_odds, 2),
        "oddsLabel": "Расчётный коэффициент модели",
        "probability": round(probability, 4),
        "probabilityPercent": round(probability * 100, 1),
        "confidence": confidence,
        "risk": "Низкий" if risk_code == "LOW" else "Средний",
        "riskCode": risk_code,
        "reason": str(
            entry.get("reason")
            or "Ранее опубликованный прогноз остаётся активным до начала матча."
        ),
        "analysisMode": str(
            entry.get("analysisMode") or "EXISTING_PENDING"
        ),
        "rankingScore": float(
            entry.get("rankingScore") or confidence
        ),
        "dataQuality": float(entry.get("dataQuality") or 0),
        "coefficientType": "MODEL_FAIR",
        "marketOddsAvailable": False,
        "expectedValueAvailable": False,
        "isExistingPending": True,
    }

    result.update(extract_public_match_status(match))
    return result



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


def select_diverse_public_predictions(
    predictions: list[dict[str, Any]],
    maximum_predictions: int,
) -> list[dict[str, Any]]:
    """Preserve ranking while preventing one competition from monopolising output.

    Selection is round-robin across country/competition buckets. This is a
    diversity rule, not a fake quota: every item has already passed the same
    quality guard. When only one bucket exists, all slots may come from it.
    """

    maximum_predictions = max(0, int(maximum_predictions))

    if maximum_predictions == 0 or not predictions:
        return []

    buckets: dict[str, list[dict[str, Any]]] = {}

    for prediction in predictions:
        country = str(prediction.get("country") or "UNKNOWN")
        league = str(
            prediction.get("leagueOriginal")
            or prediction.get("league")
            or "UNKNOWN"
        )
        buckets.setdefault(f"{country}|{league}", []).append(prediction)

    bucket_keys = sorted(
        buckets,
        key=lambda key: (
            -float(buckets[key][0].get("rankingScore") or 0),
            -int(buckets[key][0].get("confidence") or 0),
            str(buckets[key][0].get("utcDate") or ""),
            key,
        ),
    )

    selected: list[dict[str, Any]] = []
    round_index = 0

    while len(selected) < maximum_predictions:
        progress = False

        for key in bucket_keys:
            bucket = buckets[key]

            if round_index >= len(bucket):
                continue

            selected.append(bucket[round_index])
            progress = True

            if len(selected) >= maximum_predictions:
                break

        if not progress:
            break

        round_index += 1

    return selected


def finalize_public_selection(
    public_predictions: list[dict[str, Any]],
    history_records: list[dict[str, Any]],
    matches_by_id: dict[int, dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Единый финальный guard для новых и активных прогнозов."""

    minimum_confidence = int(
        config.get("minimumConfidence") or 70
    )
    win_market_minimum_confidence = int(
        config.get("winMarketMinimumConfidence")
        or max(minimum_confidence + 3, 74)
    )
    minimum_model_odds = float(
        config.get("minimumModelOdds") or 1.45
    )
    maximum_model_odds = float(
        config.get("maximumModelOdds") or 2.10
    )
    window_hours = max(
        1.0,
        float(config.get("selectionWindowHours") or 72),
    )
    maximum_predictions = max(
        0,
        int(config.get("maximumPredictions") or 5),
    )
    minimum_lead_hours = max(
        0.0,
        float(config.get("minimumLeadHours") or 3),
    )

    window_start = now + dt.timedelta(hours=minimum_lead_hours)
    window_end = window_start + dt.timedelta(hours=window_hours)
    win_markets = {"HOME_WIN", "AWAY_WIN"}

    history_by_id = {
        str(item.get("id") or ""): item
        for item in history_records
        if isinstance(item, dict)
    }

    accepted_by_match: dict[int, dict[str, Any]] = {}

    for prediction in public_predictions:
        if not isinstance(prediction, dict):
            continue

        try:
            kickoff = parse_utc_datetime(
                str(prediction.get("utcDate") or "")
            )
            confidence = int(prediction.get("confidence") or 0)
            model_odds = float(
                prediction.get("fairOdds")
                or prediction.get("odds")
                or 0
            )
            probability = float(prediction.get("probability") or 0)
            match_id = int(prediction.get("sourceMatchId"))
        except (TypeError, ValueError):
            continue

        market = str(prediction.get("market") or "").upper()
        required_confidence = (
            win_market_minimum_confidence
            if market in win_markets
            else minimum_confidence
        )

        if kickoff < window_start or kickoff > window_end:
            log(
                "Финальный guard: прогноз вне окна; "
                f"matchId={match_id}; kickoff={kickoff.isoformat()}"
            )
            continue

        if confidence < required_confidence:
            log(
                "Финальный guard: низкая уверенность; "
                f"matchId={match_id}; confidence={confidence}"
            )
            continue

        if not minimum_model_odds <= model_odds <= maximum_model_odds:
            log(
                "Финальный guard: коэффициент вне диапазона; "
                f"matchId={match_id}; fairOdds={model_odds:.2f}"
            )
            continue

        if probability <= 0:
            probability = 1 / model_odds
            prediction["probability"] = round(probability, 4)

        source_home = str(prediction.get("home") or "")
        source_away = str(prediction.get("away") or "")
        source_league = str(prediction.get("league") or "")

        localized_home = localize_team_name(source_home)
        localized_away = localize_team_name(source_away)
        localized_league = localize_competition_name(source_league)

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
        prediction["maximumModelOdds"] = maximum_model_odds
        prediction["marketOddsAvailable"] = False
        prediction["expectedValueAvailable"] = False
        prediction["coefficientType"] = "MODEL_FAIR"
        prediction["oddsLabel"] = "Расчётный коэффициент модели"
        prediction["probabilityPercent"] = round(probability * 100, 1)
        prediction.update(
            extract_public_match_status(matches_by_id.get(match_id))
        )

        current = accepted_by_match.get(match_id)

        if current is None or float(
            prediction.get("rankingScore") or 0
        ) > float(current.get("rankingScore") or 0):
            accepted_by_match[match_id] = prediction

    accepted = list(accepted_by_match.values())
    accepted.sort(
        key=lambda item: (
            -float(item.get("rankingScore") or 0),
            -int(item.get("confidence") or 0),
            str(item.get("utcDate") or ""),
        )
    )
    accepted = select_diverse_public_predictions(
        accepted,
        maximum_predictions,
    )

    accepted_ids = {
        str(item.get("id") or "")
        for item in accepted
    }

    accepted_history: list[dict[str, Any]] = []

    for prediction in accepted:
        history_record = history_by_id.get(
            str(prediction.get("id") or "")
        )

        if history_record is None:
            continue

        history_record["home"] = prediction.get("home")
        history_record["away"] = prediction.get("away")
        history_record["league"] = prediction.get("league")
        history_record["reason"] = prediction.get("reason")
        history_record["rank"] = prediction.get("rank")
        history_record["marketOddsAvailable"] = False
        history_record["expectedValueAvailable"] = False
        history_record.update(
            extract_public_match_status(
                matches_by_id.get(
                    int(prediction.get("sourceMatchId") or 0)
                )
            )
        )
        accepted_history.append(history_record)

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
    """Возвращает только действительно активные pending matchId."""

    pending_match_ids: set[int] = set()

    for item in history:
        if not isinstance(item, dict):
            continue

        if str(item.get("status") or "").lower() != "pending":
            continue

        try:
            pending_match_ids.add(int(item.get("sourceMatchId")))
        except (TypeError, ValueError):
            continue

    return pending_match_ids


def build_active_pending_records(
    history: list[dict[str, Any]],
    matches_by_id: dict[int, dict[str, Any]],
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    source_history: list[dict[str, Any]] = []

    minimum_lead_hours = max(
        0.0,
        float(config.get("minimumLeadHours") or 3),
    )
    window_hours = max(
        1.0,
        float(config.get("selectionWindowHours") or 72),
    )
    window_start = now + dt.timedelta(hours=minimum_lead_hours)
    window_end = window_start + dt.timedelta(hours=window_hours)

    for entry in history:
        if not isinstance(entry, dict):
            continue

        if str(entry.get("status") or "").lower() != "pending":
            continue

        try:
            match_id = int(entry.get("sourceMatchId"))
        except (TypeError, ValueError):
            continue

        match = matches_by_id.get(match_id)
        utc_value = str(
            (match or {}).get("utcDate")
            or entry.get("utcDate")
            or ""
        )

        try:
            kickoff = parse_utc_datetime(utc_value)
        except Exception:
            continue

        if kickoff < window_start or kickoff > window_end:
            continue

        record = pending_history_to_public_record(
            entry,
            match,
            config,
        )

        if record is None:
            continue

        records.append(record)
        source_history.append(entry)

    return records, source_history



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


def merge_prediction_sources(
    ai_predictions: list[dict[str, Any]],
    statistical_predictions: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Объединяет AI и статистику, сохраняя один прогноз на матч."""

    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 5),
    )

    by_key: dict[tuple[int, str], dict[str, Any]] = {}

    for item in statistical_predictions:
        key = (int(item["matchId"]), str(item["market"]))
        by_key[key] = dict(item)

    for ai_item in ai_predictions:
        key = (int(ai_item["matchId"]), str(ai_item["market"]))
        statistical_item = by_key.get(key)

        if statistical_item is None:
            enriched = dict(ai_item)
            enriched["rankingScore"] = round(
                float(enriched.get("rankingScore") or 0) + 2,
                4,
            )
            by_key[key] = enriched
            continue

        probability = round(
            float(ai_item["probability"]) * 0.60
            + float(statistical_item["probability"]) * 0.40,
            4,
        )
        confidence = int(
            round(
                float(ai_item["confidence"]) * 0.60
                + float(statistical_item["confidence"]) * 0.40
                + 2
            )
        )
        confidence = int(clamp_number(confidence, 0, 88))
        fair_odds = round(1 / probability, 2)

        consensus = dict(ai_item)
        consensus.update(
            {
                "probability": probability,
                "fairOdds": fair_odds,
                "confidence": confidence,
                "risk": "LOW" if confidence >= 78 else "MEDIUM",
                "analysisMode": "AI_STAT_CONSENSUS",
                "rankingScore": round(
                    max(
                        float(ai_item.get("rankingScore") or 0),
                        float(
                            statistical_item.get("rankingScore") or 0
                        ),
                    ) + 5,
                    4,
                ),
                "dataQuality": max(
                    float(ai_item.get("dataQuality") or 0),
                    float(
                        statistical_item.get("dataQuality") or 0
                    ),
                ),
                "reason": (
                    str(ai_item.get("reason") or "")
                    + " Статистический модуль подтверждает выбранный рынок."
                )[:500],
            }
        )
        by_key[key] = consensus

    ranked = list(by_key.values())
    ranked.sort(
        key=lambda item: (
            -float(item.get("rankingScore") or 0),
            -int(item.get("confidence") or 0),
            str(item.get("matchId") or ""),
        )
    )

    selected: list[dict[str, Any]] = []
    used_match_ids: set[int] = set()

    for item in ranked:
        match_id = int(item["matchId"])

        if match_id in used_match_ids:
            continue

        selected.append(item)
        used_match_ids.add(match_id)

        if len(selected) >= maximum_predictions:
            break

    return selected


def _load_optional_dotenv(path: pathlib.Path) -> None:
    """Поддерживает локальный .env, не задавая вопросов в консоли."""

    if not path.exists():
        return

    for raw_line in path.read_text(
        encoding="utf-8-sig",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if name and name not in os.environ:
            os.environ[name] = value


def main() -> int:
    log("Запуск AI Football Lab Data Pipeline v3")

    config = load_json(CONFIG_PATH)
    old_state = load_json(STATE_PATH)
    _load_optional_dotenv(ROOT / ".env")

    football_api_key = os.getenv(
        "FOOTBALL_DATA_API_KEY",
        "",
    ).strip()

    if not football_api_key:
        raise RuntimeError(
            "Не задан FOOTBALL_DATA_API_KEY. Скрипт больше не запрашивает "
            "ключ интерактивно. Для production используйте GitHub Secret, "
            "для локального запуска — переменную окружения или файл .env."
        )

    openrouter_api_key = os.getenv(
        "OPENROUTER_API_KEY",
        "",
    ).strip()

    now = utc_now()
    today = now.date()
    lookback_days = max(
        10,
        int(config.get("lookbackDays") or 60),
    )
    lookahead_days = max(
        1,
        int(config.get("lookaheadDays") or 3),
    )
    competitions = [
        str(item).strip()
        for item in config.get("competitions", [])
        if str(item).strip()
    ]
    competition_scope = str(
        config.get("competitionScope") or "ALL_ACCESSIBLE"
    ).strip().upper()

    if competition_scope == "ALL_ACCESSIBLE":
        competitions = []
        log(
            "Охват соревнований: все турниры, доступные текущему "
            "football-data.org аккаунту."
        )
    elif not competitions:
        raise RuntimeError(
            "competitionScope=CONFIGURED, но config.competitions пуст."
        )
    else:
        log(
            "Охват соревнований ограничен конфигурацией: "
            + ", ".join(competitions)
        )

    recent_matches = fetch_matches_chunked(
        football_api_key,
        date_from=today - dt.timedelta(days=lookback_days),
        date_to=today,
        competitions=competitions,
        maximum_days_per_request=10,
        pause_seconds=7,
    )

    time.sleep(7)

    upcoming_matches = fetch_matches(
        football_api_key,
        date_from=today,
        date_to=today + dt.timedelta(days=lookahead_days + 1),
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
        if str(match.get("status") or "").upper()
        in {"SCHEDULED", "TIMED"}
    ]

    state = ensure_real_state(old_state, config)
    localize_existing_history(state)
    settled_count = resolve_existing_history(
        state,
        all_matches_by_id,
    )

    team_form = build_team_form(finished_matches)
    candidates = build_candidates(
        scheduled_matches,
        team_form,
        config,
    )

    log(f"Недавних матчей: {len(recent_matches)}")
    log(f"Завершённых матчей для формы: {len(finished_matches)}")
    available_countries = sorted({
        str((match.get("area") or {}).get("name") or "").strip()
        for match in scheduled_matches
        if str((match.get("area") or {}).get("name") or "").strip()
    })
    available_competitions = sorted({
        str((match.get("competition") or {}).get("name") or "").strip()
        for match in scheduled_matches
        if str((match.get("competition") or {}).get("name") or "").strip()
    })
    candidate_countries = sorted({
        str(candidate.get("country") or "").strip()
        for candidate in candidates
        if str(candidate.get("country") or "").strip()
    })
    candidate_competitions = sorted({
        str((candidate.get("competition") or {}).get("name") or "").strip()
        for candidate in candidates
        if str((candidate.get("competition") or {}).get("name") or "").strip()
    })

    log(f"Предстоящих матчей: {len(scheduled_matches)}")
    log(f"Доступных стран: {len(available_countries)}")
    log(f"Доступных соревнований: {len(available_competitions)}")
    log(f"Команд с формой: {len(team_form)}")
    log(f"Кандидатов радара: {len(candidates)}")
    log(f"Стран в радаре: {len(candidate_countries)}")
    log(f"Соревнований в радаре: {len(candidate_competitions)}")
    log(f"Завершено pending-прогнозов: {settled_count}")

    history = [
        item
        for item in state.get("history", [])
        if isinstance(item, dict)
    ]

    active_public, active_history = build_active_pending_records(
        history,
        all_matches_by_id,
        config,
        now,
    )

    # Старые pending-прогнозы сначала проходят те же актуальные
    # требования качества. Отклонённые записи не занимают новые слоты.
    active_public, active_history = finalize_public_selection(
        active_public,
        active_history,
        all_matches_by_id,
        config,
        now,
    )

    active_match_ids = {
        int(item["sourceMatchId"])
        for item in active_public
    }

    statistical_predictions = build_deterministic_predictions(
        candidates,
        config,
    )
    ai_predictions: list[dict[str, Any]] = []
    model_name = "not-used"
    openrouter_error_text = ""
    ai_attempted = False

    if candidates and openrouter_api_key:
        ai_attempted = True
        prompt = build_analysis_prompt(candidates, config)

        try:
            model_result, model_name = call_openrouter(
                openrouter_api_key,
                prompt,
            )
            ai_predictions = normalize_model_predictions(
                model_result,
                candidates,
                config,
            )
        except Exception as openrouter_error:
            openrouter_error_text = str(openrouter_error)
            log(
                "OpenRouter недоступен или ответ не прошёл проверку. "
                "Используется статистический модуль: "
                f"{openrouter_error_text}"
            )
    elif candidates:
        openrouter_error_text = (
            "OPENROUTER_API_KEY отсутствует; использован статистический модуль."
        )
        log(openrouter_error_text)

    selected = merge_prediction_sources(
        ai_predictions,
        statistical_predictions,
        config,
    )
    selected = [
        item
        for item in selected
        if int(item["matchId"]) not in active_match_ids
    ]

    maximum_predictions = max(
        1,
        int(config.get("maximumPredictions") or 5),
    )
    available_slots = max(
        0,
        maximum_predictions - len(active_public),
    )
    selected = selected[:available_slots]

    candidates_by_id = {
        int(candidate["matchId"]): candidate
        for candidate in candidates
    }

    new_public: list[dict[str, Any]] = []
    new_history: list[dict[str, Any]] = []

    for prediction in selected:
        candidate = candidates_by_id.get(
            int(prediction["matchId"])
        )

        if candidate is None:
            continue

        public_record, history_record = prediction_to_public_records(
            prediction,
            candidate,
            stake=0.0,
            config=config,
        )
        new_public.append(public_record)
        new_history.append(history_record)

    combined_public = active_public + new_public
    combined_history_for_guard = active_history + new_history

    (
        public_predictions,
        accepted_history_for_guard,
    ) = finalize_public_selection(
        combined_public,
        combined_history_for_guard,
        all_matches_by_id,
        config,
        now,
    )

    accepted_ids = {
        str(item.get("id") or "")
        for item in public_predictions
    }
    accepted_new_history = [
        item
        for item in new_history
        if str(item.get("id") or "") in accepted_ids
    ]

    current_bank = float(
        state.get("bank", {}).get("current")
        or config.get("startingVirtualBank")
        or 10000
    )
    maximum_total_stake_percent = float(
        config.get("maximumTotalStakePercent") or 20
    )
    maximum_exposure = current_bank * (
        maximum_total_stake_percent / 100
    )
    existing_pending_exposure = sum(
        float(item.get("stake") or 0)
        for item in history
        if str(item.get("status") or "").lower() == "pending"
    )
    available_exposure = max(
        0.0,
        maximum_exposure - existing_pending_exposure,
    )
    stake_per_new = (
        available_exposure / len(accepted_new_history)
        if accepted_new_history
        else 0.0
    )

    for item in accepted_new_history:
        item["stake"] = round(stake_per_new, 2)

    history_ids = {
        str(item.get("id") or "")
        for item in history
    }

    for item in accepted_new_history:
        item_id = str(item.get("id") or "")

        if item_id and item_id not in history_ids:
            history.append(item)
            history_ids.add(item_id)

    history_by_id = {
        str(item.get("id") or ""): item
        for item in history
    }

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
        mode = str(prediction.get("analysisMode") or "")
        prediction["analysisSourceLabel"] = (
            "ИИ и статистический консенсус"
            if mode == "AI_STAT_CONSENSUS"
            else "ИИ-анализ"
            if mode == "AI"
            else "Статистический расчёт"
        )

        history_item = history_by_id.get(
            str(prediction.get("id") or "")
        )

        if history_item:
            history_item["rank"] = index
            history_item["rankLabel"] = prediction["rankLabel"]
            history_item["analysisSourceLabel"] = prediction[
                "analysisSourceLabel"
            ]

    state["predictions"] = public_predictions
    state["history"] = history

    modes = {
        str(item.get("analysisMode") or "")
        for item in public_predictions
    }

    if public_predictions:
        if modes & {"AI", "AI_STAT_CONSENSUS"}:
            analysis_status = "PREDICTIONS_SELECTED"
        else:
            analysis_status = "STATISTICAL_PREDICTIONS_SELECTED"
    elif candidates:
        analysis_status = "NO_CONFIDENT_PREDICTIONS"
    else:
        analysis_status = "NO_SUITABLE_DATA"

    if ai_predictions:
        analysis_provider = "OpenRouter + встроенный статистический модуль"
        analysis_mode = "AI_STAT_FUSION"
    elif statistical_predictions:
        analysis_provider = "Встроенный статистический модуль"
        analysis_mode = "DETERMINISTIC_STATISTICAL"
    else:
        analysis_provider = "Нет допустимых прогнозов"
        analysis_mode = "NO_SELECTION"

    state.setdefault("meta", {}).update(
        {
            "version": "3.0.0",
            "mode": "real",
            "updatedAt": now.isoformat(),
            "analyzedMatches": len(scheduled_matches),
            "candidateMatches": len(candidates),
            "selectedPredictions": len(public_predictions),
            "activePendingPredictions": len(active_public),
            "newPredictions": len(accepted_new_history),
            "source": "football-data.org",
            "competitionScope": competition_scope,
            "configuredCompetitions": competitions,
            "availableCountries": available_countries,
            "availableCompetitions": available_competitions,
            "candidateCountries": candidate_countries,
            "candidateCompetitions": candidate_competitions,
            "candidateDiversityMode": "ROUND_ROBIN_BY_COMPETITION",
            "predictionDiversityMode": "ROUND_ROBIN_BY_COUNTRY_AND_COMPETITION",
            "analysisProvider": analysis_provider,
            "analysisModel": model_name,
            "analysisMode": analysis_mode,
            "analysisStatus": analysis_status,
            "analysisError": openrouter_error_text,
            "aiAttempted": ai_attempted,
            "aiNormalizedPredictions": len(ai_predictions),
            "statisticalPredictions": len(statistical_predictions),
            "timezone": str(
                config.get("timezone") or "Europe/Moscow"
            ),
            "minimumLeadHours": float(
                config.get("minimumLeadHours") or 3
            ),
            "selectionWindowHours": float(
                config.get("selectionWindowHours") or 72
            ),
            "maximumPredictions": maximum_predictions,
            "minimumConfidence": int(
                config.get("minimumConfidence") or 70
            ),
            "minimumModelOdds": float(
                config.get("minimumModelOdds") or 1.45
            ),
            "maximumModelOdds": float(
                config.get("maximumModelOdds") or 2.10
            ),
            "marketOddsAvailable": False,
            "expectedValueAvailable": False,
            "notice": (
                "Показывается расчётный коэффициент модели (1 / вероятность), "
                "а не коэффициент букмекерской конторы. Рыночный EV нельзя "
                "рассчитать без отдельного источника реальных котировок."
            ),
        }
    )

    update_statistics(state)
    write_json_atomic(STATE_PATH, state)

    report_status = (
        "GREEN"
        if public_predictions
        else "GREEN_NO_PREDICTIONS"
    )
    report_message = (
        "Данные обновлены, прогнозы опубликованы."
        if public_predictions
        else "Данные обновлены, но допустимых прогнозов нет."
    )

    report = create_report(
        status=report_status,
        message=report_message,
        details={
            "lookbackDays": lookback_days,
            "recentMatches": len(recent_matches),
            "finishedMatches": len(finished_matches),
            "scheduledMatches": len(scheduled_matches),
            "candidateMatches": len(candidates),
            "aiPredictions": len(ai_predictions),
            "statisticalPredictions": len(statistical_predictions),
            "activePendingPredictions": len(active_public),
            "selectedPredictions": len(public_predictions),
            "newPredictions": len(accepted_new_history),
            "settledPredictions": settled_count,
            "analysisModel": model_name,
            "analysisStatus": analysis_status,
            "competitionScope": competition_scope,
            "availableCountries": available_countries,
            "availableCompetitions": available_competitions,
            "candidateCountries": candidate_countries,
            "candidateCompetitions": candidate_competitions,
        },
    )
    write_json_atomic(REPORT_PATH, report)

    log(
        "Обновление завершено. "
        f"RADAR={len(candidates)}; "
        f"AI={len(ai_predictions)}; "
        f"STAT={len(statistical_predictions)}; "
        f"PRED={len(public_predictions)}"
    )

    return 0


def run_daily_ai_mode() -> int:
    """Создаёт дневной JSON из уже опубликованного state без API-ключей."""

    state = load_json(STATE_PATH)
    predictions = [
        item
        for item in state.get("predictions", [])
        if isinstance(item, dict)
    ]
    output_path = ROOT / "data" / "ai_daily_analysis.json"
    payload = {
        "status": "READY",
        "model": str(
            (state.get("meta") or {}).get("analysisModel")
            or "not-used"
        ),
        "generatedAt": utc_now().isoformat(),
        "matchesAnalyzed": int(
            (state.get("meta") or {}).get("candidateMatches") or 0
        ),
        "recommendations": predictions,
    }
    write_json_atomic(output_path, payload)
    print("DAILY_AI_EXPORT_READY")
    return 0


def validate_repository_files() -> int:
    config = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH)

    if not isinstance(config.get("competitions"), list):
        raise RuntimeError("config.competitions должен быть массивом")

    competition_scope = str(
        config.get("competitionScope") or "ALL_ACCESSIBLE"
    ).strip().upper()

    if competition_scope not in {"ALL_ACCESSIBLE", "CONFIGURED"}:
        raise RuntimeError(
            "config.competitionScope должен быть ALL_ACCESSIBLE или CONFIGURED"
        )

    if competition_scope == "CONFIGURED" and not config.get("competitions"):
        raise RuntimeError(
            "Для competitionScope=CONFIGURED нужен непустой competitions"
        )

    if not isinstance(config.get("allowedMarkets"), list):
        raise RuntimeError("config.allowedMarkets должен быть массивом")

    if not isinstance(state.get("predictions", []), list):
        raise RuntimeError("state.predictions должен быть массивом")

    if not isinstance(state.get("history", []), list):
        raise RuntimeError("state.history должен быть массивом")

    print("VALIDATION_GREEN")
    return 0


def run_self_test() -> int:
    config = {
        "allowedMarkets": list(MARKET_LABELS),
        "maximumPredictions": 5,
        "minimumConfidence": 68,
        "winMarketMinimumConfidence": 72,
        "minimumProbability": 0.52,
        "maximumProbability": 0.69,
        "minimumModelOdds": 1.45,
        "maximumModelOdds": 2.10,
        "minimumDataQuality": 40,
    }

    strong_form = {
        "games": 8,
        "pointsPerGame": 1.75,
        "goalsForPerGame": 1.5,
        "goalsAgainstPerGame": 1.1,
        "winRate": 0.5,
        "drawRate": 0.25,
        "lossRate": 0.25,
        "nonLossRate": 0.75,
        "scoredRate": 0.75,
        "concededRate": 0.625,
        "cleanSheetRate": 0.375,
        "over15Rate": 0.75,
        "under35Rate": 0.75,
        "bothScoreRate": 0.5,
        "homeVenue": {
            "games": 5,
            "pointsPerGame": 1.8,
            "goalsForPerGame": 1.6,
            "goalsAgainstPerGame": 1.0,
            "winRate": 0.6,
            "lossRate": 0.2,
            "nonLossRate": 0.8,
            "scoredRate": 0.8,
            "concededRate": 0.6,
        },
        "awayVenue": {
            "games": 5,
            "pointsPerGame": 1.4,
            "goalsForPerGame": 1.3,
            "goalsAgainstPerGame": 1.2,
            "winRate": 0.4,
            "lossRate": 0.4,
            "nonLossRate": 0.6,
            "scoredRate": 0.8,
            "concededRate": 0.6,
        },
    }

    candidates = []

    for index in range(1, 7):
        candidates.append(
            {
                "matchId": index,
                "utcDate": (
                    utc_now() + dt.timedelta(hours=8 + index)
                ).isoformat(),
                "competition": {
                    "code": "TEST",
                    "name": "Test League",
                },
                "country": "England",
                "homeTeam": {
                    "id": index * 2,
                    "name": f"Home {index}",
                    "form": copy.deepcopy(strong_form),
                },
                "awayTeam": {
                    "id": index * 2 + 1,
                    "name": f"Away {index}",
                    "form": copy.deepcopy(strong_form),
                },
                "dataQuality": 80.0,
            }
        )

    predictions = build_deterministic_predictions(
        candidates,
        config,
    )

    if not predictions:
        raise RuntimeError("SELF_TEST: статистический модуль вернул пусто")

    if len({item["matchId"] for item in predictions}) != len(predictions):
        raise RuntimeError("SELF_TEST: обнаружены дубли matchId")

    for item in predictions:
        fair_odds = float(item["fairOdds"])

        if not 1.45 <= fair_odds <= 2.10:
            raise RuntimeError(
                f"SELF_TEST: fairOdds вне диапазона: {fair_odds}"
            )

    diversity_input = [
        {
            "id": f"diverse-{index}",
            "sourceMatchId": 1000 + index,
            "country": country,
            "league": league,
            "leagueOriginal": league,
            "rankingScore": 100 - index,
            "confidence": 80,
            "utcDate": (
                utc_now() + dt.timedelta(hours=8 + index)
            ).isoformat(),
        }
        for index, (country, league) in enumerate(
            [
                ("Brazil", "Serie A"),
                ("Brazil", "Serie A"),
                ("England", "Premier League"),
                ("Spain", "La Liga"),
                ("Brazil", "Serie A"),
            ],
            start=1,
        )
    ]
    diversity_result = select_diverse_public_predictions(
        diversity_input,
        3,
    )
    diversity_countries = {
        str(item.get("country") or "")
        for item in diversity_result
    }

    if len(diversity_countries) < 3:
        raise RuntimeError(
            "SELF_TEST: финальный отбор не обеспечил межстрановую диверсификацию"
        )

    print(
        "SELF_TEST_GREEN "
        f"PRED={len(predictions)} "
        f"DIVERSE_COUNTRIES={len(diversity_countries)}"
    )
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI Football Lab data pipeline"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--daily-ai",
        action="store_true",
        help="Сформировать дневной JSON из текущего state без API",
    )
    mode.add_argument(
        "--validate",
        action="store_true",
        help="Проверить конфигурацию и state без API",
    )
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Запустить встроенный офлайн-тест",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help="Запустить полное обновление (режим по умолчанию)",
    )
    arguments = parser.parse_args(argv)

    if arguments.daily_ai:
        return run_daily_ai_mode()

    if arguments.validate:
        return validate_repository_files()

    if arguments.self_test:
        return run_self_test()

    return main()


if __name__ == "__main__":
    try:
        sys.exit(cli_main())
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
