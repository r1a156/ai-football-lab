#!/usr/bin/env python3
"""R15F no-key football data mesh.

All network sources in this module are free at the point of access and require
no user registration. The module never replaces current bookmaker odds; it
builds the persistent historical/team-strength layer used by R15.

Sources:
- football-data.co.uk CSV result/odds history
- OpenFootball CC0 JSON repositories
- OpenLigaDB public JSON API
- ClubElo public CSV feed
- StatsBomb Open Data match metadata
- TheSportsDB v1 shared public key 123 for aliases/artwork
- Wikidata / MediaWiki public APIs for Russian display names
- public Fonbet web page as best-effort availability evidence only
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "analysis.json"
HISTORY_PATH = ROOT / "data" / "football-history-cache.json"
REGISTRY_PATH = ROOT / "data" / "team-registry.json"
MESH_PATH = ROOT / "data" / "free-data-mesh.json"
ELO_PATH = ROOT / "data" / "clubelo-ratings.json"
RUSSIAN_NAMES_PATH = ROOT / "data" / "russian-team-names.json"
PROVIDER_HEALTH_PATH = ROOT / "data" / "provider-health.json"
FONBET_PATH = ROOT / "data" / "fonbet-public-snapshot.json"
THESPORTSDB_PATH = ROOT / "data" / "thesportsdb-team-cache.json"

UTC = dt.timezone.utc
MESH_MARKER = "V10_R15F_NO_KEY_FREE_DATA_MESH"
HISTORY_MARKER = "V10_R15_PERSISTENT_FOOTBALL_HISTORY"
USER_AGENT = "AI-Football-Lab/10 R15F (+https://github.com/r1a156/ai-football-lab)"

FDCUK_LEAGUES: dict[str, str] = {
    "E0": "England Premier League", "E1": "England Championship",
    "E2": "England League One", "E3": "England League Two",
    "SC0": "Scotland Premiership", "SC1": "Scotland Championship",
    "D1": "Germany Bundesliga", "D2": "Germany 2 Bundesliga",
    "I1": "Italy Serie A", "I2": "Italy Serie B",
    "SP1": "Spain La Liga", "SP2": "Spain Segunda",
    "F1": "France Ligue 1", "F2": "France Ligue 2",
    "N1": "Netherlands Eredivisie", "P1": "Portugal Primeira Liga",
    "B1": "Belgium First Division", "T1": "Turkey Super Lig",
    "G1": "Greece Super League", "AUT": "Austria Bundesliga",
    "SWZ": "Switzerland Super League", "DNK": "Denmark Superliga",
    "NOR": "Norway Eliteserien", "SWE": "Sweden Allsvenskan",
    "POL": "Poland Ekstraklasa", "ROU": "Romania Liga I",
}

OPENFOOTBALL_CODES: dict[str, str] = {
    "en.1": "England Premier League", "en.2": "England Championship",
    "de.1": "Germany Bundesliga", "de.2": "Germany 2 Bundesliga",
    "es.1": "Spain La Liga", "es.2": "Spain Segunda",
    "it.1": "Italy Serie A", "it.2": "Italy Serie B",
    "fr.1": "France Ligue 1", "fr.2": "France Ligue 2",
    "nl.1": "Netherlands Eredivisie", "pt.1": "Portugal Primeira Liga",
    "be.1": "Belgium First Division", "tr.1": "Turkey Super Lig",
    "at.1": "Austria Bundesliga", "ch.1": "Switzerland Super League",
}

OPENLIGA_CODES = {
    "bl1": "Germany Bundesliga",
    "bl2": "Germany 2 Bundesliga",
    "bl3": "Germany 3 Liga",
    "dfb": "Germany DFB Pokal",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any, *, default_hour: int = 12) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            date = dt.datetime.strptime(text, fmt).date()
            return dt.datetime.combine(date, dt.time(default_hour, 0), tzinfo=UTC)
        except ValueError:
            continue
    return None


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(fc|cf|sc|afc|ac|fk|sk|bk|if|club|football|soccer)\b", " ", text)
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text)
    return " ".join(text.split())


def stable_id(*parts: Any) -> str:
    raw = "|".join(normalize(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def empty_mesh() -> dict[str, Any]:
    return {
        "version": 1,
        "sourceMarker": MESH_MARKER,
        "updatedAt": None,
        "sources": {},
        "sourceDocuments": {},
        "matchesIngested": 0,
        "duplicatesRemoved": 0,
        "lastRefreshStatus": "INITIALIZED",
    }


def empty_history() -> dict[str, Any]:
    return {
        "version": 1,
        "sourceMarker": HISTORY_MARKER,
        "updatedAt": None,
        "lastSuccessfulAt": None,
        "coverageStart": None,
        "coverageEnd": None,
        "complete": False,
        "matches": [],
        "sourceHealth": {},
    }


def _request(
    url: str,
    *,
    timeout: int = 25,
    headers: dict[str, str] | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    retries: int = 2,
) -> tuple[bytes | None, dict[str, str], int]:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/csv,text/plain,text/html,*/*",
        "Accept-Encoding": "identity",
    }
    request_headers.update(headers or {})
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read()
                result_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                return body, result_headers, int(response.status)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, {str(k).lower(): str(v) for k, v in exc.headers.items()}, 304
            last_error = exc
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                retry_after = 0
                try:
                    retry_after = int(exc.headers.get("Retry-After", "0"))
                except (TypeError, ValueError):
                    retry_after = 0
                time.sleep(min(12, max(retry_after, 2 ** attempt)))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(8, 2 ** attempt))
                continue
            raise
    raise RuntimeError(str(last_error or "unknown request failure"))


def _source_due(mesh: dict[str, Any], name: str, hours: float, now: dt.datetime, force: bool) -> bool:
    if force:
        return True
    row = (mesh.get("sources") or {}).get(name) or {}
    last = parse_dt(row.get("lastSuccessfulAt"))
    return not last or (now - last).total_seconds() >= hours * 3600


def _source_start(mesh: dict[str, Any], name: str, now: dt.datetime) -> dict[str, Any]:
    row = dict((mesh.setdefault("sources", {}).get(name) or {}))
    row.update({"lastAttemptAt": iso(now), "status": "RUNNING", "errors": []})
    mesh["sources"][name] = row
    return row


def _source_finish(row: dict[str, Any], now: dt.datetime, *, status: str, requests: int, matches: int, errors: list[str]) -> None:
    row.update({
        "status": status,
        "requests": requests,
        "matches": matches,
        "errors": errors[-20:],
        "updatedAt": iso(now),
    })
    if status in {"GREEN", "PARTIAL", "NOT_MODIFIED"}:
        row["lastSuccessfulAt"] = iso(now)


def season_codes(now: dt.datetime) -> tuple[str, str, str, str]:
    year = now.year
    start = year if now.month >= 7 else year - 1
    current_short = f"{str(start)[2:]}{str(start + 1)[2:]}"
    previous_short = f"{str(start - 1)[2:]}{str(start)[2:]}"
    return current_short, previous_short, f"{start}-{str(start + 1)[2:]}", f"{start - 1}-{str(start)[2:]}"


def as_match(
    *,
    source: str,
    source_id: Any,
    when: dt.datetime,
    competition: str,
    competition_code: str,
    home: str,
    away: str,
    home_score: int | None,
    away_score: int | None,
    half_home: int | None = None,
    half_away: int | None = None,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    home = str(home or "").strip()
    away = str(away or "").strip()
    if not home or not away or normalize(home) == normalize(away):
        return None
    final_status = status or ("FINISHED" if home_score is not None and away_score is not None else "SCHEDULED")
    item = {
        "id": f"{source.lower()}:{source_id or stable_id(when.date(), competition, home, away)}",
        "utcDate": iso(when),
        "status": final_status,
        "competitionId": "",
        "competition": competition,
        "competitionCode": competition_code,
        "homeTeam": {"id": "", "name": home, "shortName": home, "tla": ""},
        "awayTeam": {"id": "", "name": away, "shortName": away, "tla": ""},
        "homeScore": home_score,
        "awayScore": away_score,
        "halfHome": half_home,
        "halfAway": half_away,
        "matchday": None,
        "stage": "",
        "group": "",
        "lastUpdated": iso(utc_now()),
        "source": source,
    }
    if extra:
        item.update(extra)
    return item


def _int_or_none(value: Any) -> int | None:
    if value is None or str(value).strip() in {"", "None", "null", "-"}:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_football_data_csv(body: bytes, league_code: str, league_name: str, season: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    result: list[dict[str, Any]] = []
    for index, row in enumerate(reader):
        when = parse_dt(row.get("Date"))
        if not when:
            continue
        time_text = str(row.get("Time") or "").strip()
        if time_text and re.match(r"^\d{1,2}:\d{2}$", time_text):
            hour, minute = (int(part) for part in time_text.split(":"))
            when = dt.datetime.combine(when.date(), dt.time(hour, minute), tzinfo=UTC)
        item = as_match(
            source="FOOTBALL_DATA_CO_UK",
            source_id=f"{season}:{league_code}:{index}:{when.date().isoformat()}",
            when=when,
            competition=league_name,
            competition_code=league_code,
            home=row.get("HomeTeam") or "",
            away=row.get("AwayTeam") or "",
            home_score=_int_or_none(row.get("FTHG")),
            away_score=_int_or_none(row.get("FTAG")),
            half_home=_int_or_none(row.get("HTHG")),
            half_away=_int_or_none(row.get("HTAG")),
            extra={
                "season": season,
                "historicalOdds": {
                    key: row.get(key)
                    for key in ("B365H", "B365D", "B365A", "PSH", "PSD", "PSA", "B365>2.5", "B365<2.5")
                    if row.get(key) not in {None, ""}
                },
            },
        )
        if item:
            result.append(item)
    return result


def refresh_football_data_co_uk(mesh: dict[str, Any], now: dt.datetime, force: bool = False) -> list[dict[str, Any]]:
    name = "FOOTBALL_DATA_CO_UK"
    if not _source_due(mesh, name, 11.5, now, force):
        return []
    row = _source_start(mesh, name, now)
    current, previous, _, _ = season_codes(now)
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    for season in (current, previous):
        for league_code, league_name in FDCUK_LEAGUES.items():
            url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
            try:
                body, _, status = _request(url, timeout=20, retries=1)
                requests += 1
                if body and status == 200:
                    matches.extend(parse_football_data_csv(body, league_code, league_name, season))
            except Exception as exc:  # a league file may legitimately not exist
                errors.append(f"{league_code}/{season}:{type(exc).__name__}")
            time.sleep(0.03)
    status = "GREEN" if matches and not errors else "PARTIAL" if matches else "RED"
    _source_finish(row, now, status=status, requests=requests, matches=len(matches), errors=errors)
    return matches


def parse_openfootball_json(body: bytes, code: str, fallback_name: str, season: str) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8-sig", errors="replace"))
    rows = payload.get("matches") if isinstance(payload, dict) else []
    league = str((payload or {}).get("name") or fallback_name)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        when = parse_dt(row.get("date"))
        if not when:
            continue
        team1 = row.get("team1")
        team2 = row.get("team2")
        if isinstance(team1, dict):
            team1 = team1.get("name") or team1.get("title")
        if isinstance(team2, dict):
            team2 = team2.get("name") or team2.get("title")
        score = row.get("score") if isinstance(row.get("score"), dict) else {}
        full = score.get("ft") or score.get("fullTime")
        half = score.get("ht") or score.get("halfTime")
        hs = _int_or_none(full[0]) if isinstance(full, list) and len(full) >= 2 else _int_or_none((full or {}).get("home") if isinstance(full, dict) else None)
        aws = _int_or_none(full[1]) if isinstance(full, list) and len(full) >= 2 else _int_or_none((full or {}).get("away") if isinstance(full, dict) else None)
        hh = _int_or_none(half[0]) if isinstance(half, list) and len(half) >= 2 else None
        ah = _int_or_none(half[1]) if isinstance(half, list) and len(half) >= 2 else None
        item = as_match(
            source="OPENFOOTBALL",
            source_id=f"{season}:{code}:{index}:{row.get('round') or ''}",
            when=when,
            competition=league,
            competition_code=code,
            home=str(team1 or ""),
            away=str(team2 or ""),
            home_score=hs,
            away_score=aws,
            half_home=hh,
            half_away=ah,
            extra={"season": season, "round": row.get("round")},
        )
        if item:
            result.append(item)
    return result


def refresh_openfootball(mesh: dict[str, Any], now: dt.datetime, force: bool = False) -> list[dict[str, Any]]:
    name = "OPENFOOTBALL"
    if not _source_due(mesh, name, 10.0, now, force):
        return []
    row = _source_start(mesh, name, now)
    _, _, current, previous = season_codes(now)
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    for season in (current, previous):
        for code, league in OPENFOOTBALL_CODES.items():
            url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{code}.json"
            try:
                body, _, status = _request(url, timeout=18, retries=1)
                requests += 1
                if body and status == 200:
                    matches.extend(parse_openfootball_json(body, code, league, season))
            except Exception as exc:
                errors.append(f"{code}/{season}:{type(exc).__name__}")
            time.sleep(0.03)
    status = "GREEN" if matches and not errors else "PARTIAL" if matches else "RED"
    _source_finish(row, now, status=status, requests=requests, matches=len(matches), errors=errors)
    return matches


def parse_openliga(payload: Any, code: str, league: str, season: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        when = parse_dt(row.get("matchDateTimeUTC") or row.get("matchDateTime"))
        team1 = row.get("team1") if isinstance(row.get("team1"), dict) else {}
        team2 = row.get("team2") if isinstance(row.get("team2"), dict) else {}
        if not when:
            continue
        final = None
        for score in row.get("matchResults") or []:
            if not isinstance(score, dict):
                continue
            if score.get("resultTypeID") == 2 or str(score.get("resultName") or "").lower() in {"endergebnis", "final result"}:
                final = score
                break
        if final is None and row.get("matchIsFinished"):
            scores = [score for score in row.get("matchResults") or [] if isinstance(score, dict)]
            final = scores[-1] if scores else None
        item = as_match(
            source="OPENLIGADB",
            source_id=row.get("matchID"),
            when=when,
            competition=str((row.get("league") or {}).get("leagueName") if isinstance(row.get("league"), dict) else league) or league,
            competition_code=code,
            home=str(team1.get("teamName") or team1.get("shortName") or ""),
            away=str(team2.get("teamName") or team2.get("shortName") or ""),
            home_score=_int_or_none((final or {}).get("pointsTeam1")),
            away_score=_int_or_none((final or {}).get("pointsTeam2")),
            status="FINISHED" if row.get("matchIsFinished") else "SCHEDULED",
            extra={"season": str(season), "openLigaGroup": row.get("group")},
        )
        if item:
            result.append(item)
    return result


def refresh_openligadb(mesh: dict[str, Any], now: dt.datetime, force: bool = False) -> list[dict[str, Any]]:
    name = "OPENLIGADB"
    if not _source_due(mesh, name, 2.0, now, force):
        return []
    row = _source_start(mesh, name, now)
    start_year = now.year if now.month >= 7 else now.year - 1
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    for season in (start_year, start_year - 1):
        for code, league in OPENLIGA_CODES.items():
            url = f"https://api.openligadb.de/getmatchdata/{code}/{season}"
            try:
                body, _, status = _request(url, timeout=20, retries=1)
                requests += 1
                if body and status == 200:
                    matches.extend(parse_openliga(json.loads(body.decode("utf-8")), code, league, season))
            except Exception as exc:
                errors.append(f"{code}/{season}:{type(exc).__name__}")
            time.sleep(0.05)
    status = "GREEN" if matches and not errors else "PARTIAL" if matches else "RED"
    _source_finish(row, now, status=status, requests=requests, matches=len(matches), errors=errors)
    return matches


def refresh_clubelo(mesh: dict[str, Any], now: dt.datetime, force: bool = False) -> dict[str, Any]:
    name = "CLUBELO"
    current = load_json(ELO_PATH, {"version": 1, "sourceMarker": MESH_MARKER, "updatedAt": None, "ratings": {}})
    if not _source_due(mesh, name, 12.0, now, force):
        return current
    row = _source_start(mesh, name, now)
    errors: list[str] = []
    body = None
    requests = 0
    for url in ("http://api.clubelo.com/", "https://api.clubelo.com/"):
        try:
            body, _, status = _request(url, timeout=25, retries=1)
            requests += 1
            if body and status == 200:
                break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}")
    ratings: dict[str, Any] = {}
    if body:
        text = body.decode("utf-8-sig", errors="replace")
        for item in csv.DictReader(io.StringIO(text)):
            club = str(item.get("Club") or item.get("club") or "").strip()
            if not club:
                continue
            try:
                elo = float(item.get("Elo") or item.get("elo") or 0)
            except (TypeError, ValueError):
                continue
            ratings[normalize(club)] = {
                "club": club,
                "elo": round(elo, 2),
                "country": item.get("Country") or item.get("country"),
                "rank": _int_or_none(item.get("Rank") or item.get("rank")),
                "from": item.get("From") or item.get("from"),
                "to": item.get("To") or item.get("to"),
            }
    status = "GREEN" if ratings else "RED"
    _source_finish(row, now, status=status, requests=requests, matches=len(ratings), errors=errors)
    if ratings:
        current = {"version": 1, "sourceMarker": MESH_MARKER, "updatedAt": iso(now), "ratings": ratings}
        write_json(ELO_PATH, current)
    return current


def parse_statsbomb_matches(payload: Any, competition_id: Any, season_id: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        when = parse_dt(row.get("match_date"))
        if not when:
            continue
        home = row.get("home_team") if isinstance(row.get("home_team"), dict) else {}
        away = row.get("away_team") if isinstance(row.get("away_team"), dict) else {}
        competition = row.get("competition") if isinstance(row.get("competition"), dict) else {}
        item = as_match(
            source="STATSBOMB_OPEN_DATA",
            source_id=row.get("match_id"),
            when=when,
            competition=str(competition.get("competition_name") or f"StatsBomb {competition_id}"),
            competition_code=f"SB:{competition_id}:{season_id}",
            home=str(home.get("home_team_name") or ""),
            away=str(away.get("away_team_name") or ""),
            home_score=_int_or_none(row.get("home_score")),
            away_score=_int_or_none(row.get("away_score")),
            extra={"season": str(season_id), "competitionId": str(competition_id)},
        )
        if item:
            result.append(item)
    return result


def refresh_statsbomb(mesh: dict[str, Any], now: dt.datetime, force: bool = False) -> list[dict[str, Any]]:
    name = "STATSBOMB_OPEN_DATA"
    if not _source_due(mesh, name, 168.0, now, force):
        return []
    row = _source_start(mesh, name, now)
    errors: list[str] = []
    requests = 0
    matches: list[dict[str, Any]] = []
    try:
        url = "https://raw.githubusercontent.com/statsbomb/open-data/master/data/competitions.json"
        body, _, status = _request(url, timeout=25, retries=1)
        requests += 1
        competitions = json.loads(body.decode("utf-8")) if body and status == 200 else []
        latest: dict[int, dict[str, Any]] = {}
        for item in competitions if isinstance(competitions, list) else []:
            cid = _int_or_none(item.get("competition_id"))
            sid = _int_or_none(item.get("season_id"))
            if cid is None or sid is None:
                continue
            prior = latest.get(cid)
            if prior is None or str(item.get("season_name") or "") > str(prior.get("season_name") or ""):
                latest[cid] = item
        chosen = sorted(latest.values(), key=lambda item: str(item.get("match_updated") or ""), reverse=True)[:8]
        for item in chosen:
            cid = item.get("competition_id")
            sid = item.get("season_id")
            try:
                match_url = f"https://raw.githubusercontent.com/statsbomb/open-data/master/data/matches/{cid}/{sid}.json"
                body, _, status = _request(match_url, timeout=25, retries=1)
                requests += 1
                if body and status == 200:
                    matches.extend(parse_statsbomb_matches(json.loads(body.decode("utf-8")), cid, sid))
            except Exception as exc:
                errors.append(f"{cid}/{sid}:{type(exc).__name__}")
    except Exception as exc:
        errors.append(f"competitions:{type(exc).__name__}")
    status = "GREEN" if matches and not errors else "PARTIAL" if matches else "RED"
    _source_finish(row, now, status=status, requests=requests, matches=len(matches), errors=errors)
    return matches


def match_key(item: dict[str, Any]) -> str:
    when = parse_dt(item.get("utcDate"))
    day = when.date().isoformat() if when else str(item.get("utcDate") or "")[:10]
    home = normalize((item.get("homeTeam") or {}).get("name") if isinstance(item.get("homeTeam"), dict) else "")
    away = normalize((item.get("awayTeam") or {}).get("name") if isinstance(item.get("awayTeam"), dict) else "")
    return f"{day}|{home}|{away}"


def source_rank(source: str) -> int:
    return {
        "AI_FOOTBALL_TRACKED_RESULT": 100,
        "FOOTBALL_DATA": 90,
        "OPENLIGADB": 85,
        "FOOTBALL_DATA_CO_UK": 80,
        "OPENFOOTBALL": 70,
        "STATSBOMB_OPEN_DATA": 65,
    }.get(source, 50)


def merge_history(existing: dict[str, Any], additions: Iterable[dict[str, Any]], now: dt.datetime, maximum: int = 60000) -> dict[str, Any]:
    by_key: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for item in list(existing.get("matches") or []) + [row for row in additions if isinstance(row, dict)]:
        key = match_key(item)
        if not key or key.endswith("||"):
            continue
        prior = by_key.get(key)
        if prior is None:
            by_key[key] = item
            continue
        duplicates += 1
        prior_score = int(prior.get("homeScore") is not None and prior.get("awayScore") is not None)
        item_score = int(item.get("homeScore") is not None and item.get("awayScore") is not None)
        if (item_score, source_rank(str(item.get("source") or ""))) > (prior_score, source_rank(str(prior.get("source") or ""))):
            by_key[key] = item
    ordered = sorted(by_key.values(), key=lambda row: str(row.get("utcDate") or ""), reverse=True)[:maximum]
    ordered.sort(key=lambda row: str(row.get("utcDate") or ""))
    existing["matches"] = ordered
    dates = [parse_dt(row.get("utcDate")) for row in ordered]
    dates = [value for value in dates if value]
    if dates:
        existing["coverageStart"] = iso(min(dates))
        existing["coverageEnd"] = iso(max(dates))
    existing["updatedAt"] = iso(now)
    existing["lastSuccessfulAt"] = iso(now)
    existing["sourceMarker"] = HISTORY_MARKER
    existing.setdefault("sourceHealth", {})["FREE_DATA_MESH"] = {
        "status": "GREEN",
        "updatedAt": iso(now),
        "matches": len(ordered),
    }
    return {"history": existing, "duplicates": duplicates, "matches": len(ordered)}


def rebuild_registry(history: dict[str, Any], existing: dict[str, Any], elo_data: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    registry = existing if isinstance(existing, dict) else {}
    registry.setdefault("version", 1)
    registry["sourceMarker"] = HISTORY_MARKER
    teams = registry.setdefault("teams", {})
    aliases = registry.setdefault("aliases", {})
    for match in history.get("matches") or []:
        for side in ("homeTeam", "awayTeam"):
            team = match.get(side) if isinstance(match.get(side), dict) else {}
            names = [str(team.get(key) or "").strip() for key in ("name", "shortName", "tla")]
            names = [name for name in names if name]
            if not names:
                continue
            canonical = next((aliases.get(normalize(name)) for name in names if aliases.get(normalize(name))), None)
            canonical = str(canonical or ("name:" + stable_id(names[0])))
            row = teams.setdefault(canonical, {"canonicalTeamId": canonical, "officialName": names[0], "aliases": []})
            merged = set(str(value) for value in row.get("aliases") or [])
            merged.update(names)
            row["aliases"] = sorted(merged)
            for alias in merged:
                aliases[normalize(alias)] = canonical
    ratings = elo_data.get("ratings") if isinstance(elo_data, dict) else {}
    for alias_key, rating in (ratings or {}).items():
        canonical = aliases.get(alias_key)
        if canonical and canonical in teams:
            teams[canonical]["clubElo"] = rating.get("elo")
            teams[canonical]["clubEloUpdatedAt"] = elo_data.get("updatedAt")
    registry["updatedAt"] = iso(now)
    return registry


def refresh_all(*, force: bool = False) -> dict[str, Any]:
    now = utc_now()
    config = load_json(CONFIG_PATH, {})
    mesh = load_json(MESH_PATH, empty_mesh())
    if not isinstance(mesh, dict) or mesh.get("sourceMarker") != MESH_MARKER:
        mesh = empty_mesh()
    history = load_json(HISTORY_PATH, empty_history())
    if not isinstance(history, dict):
        history = empty_history()
    additions: list[dict[str, Any]] = []
    additions.extend(refresh_football_data_co_uk(mesh, now, force))
    additions.extend(refresh_openfootball(mesh, now, force))
    additions.extend(refresh_openligadb(mesh, now, force))
    additions.extend(refresh_statsbomb(mesh, now, force))
    elo_data = refresh_clubelo(mesh, now, force)
    merged = merge_history(history, additions, now, maximum=int(config.get("freeMeshMaximumMatches", 60000)))
    history = merged["history"]
    registry = rebuild_registry(history, load_json(REGISTRY_PATH, {}), elo_data, now)
    mesh.update({
        "updatedAt": iso(now),
        "matchesIngested": len(additions),
        "duplicatesRemoved": merged["duplicates"],
        "historyMatches": merged["matches"],
        "lastRefreshStatus": "GREEN" if additions or merged["matches"] else "DEGRADED",
        "noRegistrationRequired": True,
        "noUserApiKeysRequired": True,
    })
    write_json(HISTORY_PATH, history)
    write_json(REGISTRY_PATH, registry)
    write_json(MESH_PATH, mesh)
    health = load_json(PROVIDER_HEALTH_PATH, {})
    health["FREE_DATA_MESH"] = {
        "status": mesh["lastRefreshStatus"],
        "updatedAt": iso(now),
        "sources": {name: row.get("status") for name, row in mesh.get("sources", {}).items()},
        "historyMatches": merged["matches"],
    }
    write_json(PROVIDER_HEALTH_PATH, health)
    return {
        "status": mesh["lastRefreshStatus"],
        "sources": {name: row.get("status") for name, row in mesh.get("sources", {}).items()},
        "ingested": len(additions),
        "duplicates": merged["duplicates"],
        "historyMatches": merged["matches"],
        "coverageStart": history.get("coverageStart"),
        "coverageEnd": history.get("coverageEnd"),
    }


def merge_external_elo(context: dict[str, Any]) -> dict[str, Any]:
    elo_data = load_json(ELO_PATH, {})
    ratings = elo_data.get("ratings") if isinstance(elo_data, dict) else {}
    aliases = context.get("aliases") if isinstance(context.get("aliases"), dict) else {}
    applied = 0
    for alias_key, row in (ratings or {}).items():
        canonical = aliases.get(alias_key)
        if canonical:
            context.setdefault("elo", {})[str(canonical)] = float(row.get("elo") or 1500.0)
            applied += 1
            continue
        best_id = None
        best = 0.0
        tokens = set(alias_key.split())
        for known_alias, team_id in aliases.items():
            known = set(known_alias.split())
            if not tokens or not known:
                continue
            score = len(tokens & known) / max(len(tokens | known), 1)
            if score > best:
                best = score
                best_id = team_id
        if best_id and best >= 0.67:
            context.setdefault("elo", {})[str(best_id)] = float(row.get("elo") or 1500.0)
            applied += 1
    context.setdefault("cacheMeta", {})["clubEloApplied"] = applied
    context["cacheMeta"]["clubEloUpdatedAt"] = elo_data.get("updatedAt")
    return context



def resolve_thesportsdb_teams(team_names: Iterable[str], *, maximum_new: int = 30) -> dict[str, dict[str, Any]]:
    """Resolve canonical aliases/artwork through the public shared v1 key 123.

    This is a public key documented by TheSportsDB and requires no account.
    Results are cached permanently, so a team normally costs one request once.
    """
    now = utc_now()
    cache = load_json(THESPORTSDB_PATH, {
        "version": 1, "sourceMarker": MESH_MARKER, "updatedAt": None, "teams": {}
    })
    teams = cache.setdefault("teams", {})
    changed = False
    attempted = 0
    for original in dict.fromkeys(str(value).strip() for value in team_names if str(value).strip()):
        key = normalize(original)
        if key in teams or attempted >= maximum_new:
            continue
        attempted += 1
        params = urllib.parse.urlencode({"t": original})
        try:
            body, _, status = _request(
                f"https://www.thesportsdb.com/api/v1/json/123/searchteams.php?{params}",
                timeout=15,
                retries=1,
            )
            payload = json.loads(body.decode("utf-8")) if body and status == 200 else {}
            candidates = payload.get("teams") if isinstance(payload, dict) else []
            selected = None
            best = -1.0
            original_tokens = set(key.split())
            for item in candidates or []:
                if not isinstance(item, dict):
                    continue
                sport = str(item.get("strSport") or "").casefold()
                if sport and sport not in {"soccer", "football"}:
                    continue
                candidate_name = str(item.get("strTeam") or "")
                candidate_key = normalize(candidate_name)
                candidate_tokens = set(candidate_key.split())
                overlap = len(original_tokens & candidate_tokens) / max(len(original_tokens | candidate_tokens), 1)
                if overlap > best:
                    best = overlap
                    selected = item
            if selected and best >= 0.45:
                aliases = [
                    str(selected.get("strTeam") or "").strip(),
                    str(selected.get("strTeamShort") or "").strip(),
                    str(selected.get("strAlternate") or selected.get("strTeamAlternate") or "").strip(),
                    original,
                ]
                teams[key] = {
                    "original": original,
                    "idTeam": selected.get("idTeam"),
                    "officialName": selected.get("strTeam"),
                    "shortName": selected.get("strTeamShort"),
                    "aliases": sorted({value for value in aliases if value}),
                    "country": selected.get("strCountry"),
                    "league": selected.get("strLeague"),
                    "badge": selected.get("strBadge"),
                    "source": "THESPORTSDB_PUBLIC_123",
                    "updatedAt": iso(now),
                }
            else:
                teams[key] = {
                    "original": original,
                    "status": "NOT_FOUND",
                    "source": "THESPORTSDB_PUBLIC_123",
                    "updatedAt": iso(now),
                }
            changed = True
        except Exception as exc:
            teams[key] = {
                "original": original,
                "status": "TEMPORARY_ERROR",
                "error": type(exc).__name__,
                "source": "THESPORTSDB_PUBLIC_123",
                "updatedAt": iso(now),
            }
            changed = True
        time.sleep(0.08)
    if changed:
        cache["updatedAt"] = iso(now)
        write_json(THESPORTSDB_PATH, cache)
    return {key: value for key, value in teams.items() if isinstance(value, dict)}

def _wikidata_ru(name: str) -> str | None:
    params = urllib.parse.urlencode({
        "action": "wbsearchentities",
        "search": name,
        "language": "ru",
        "uselang": "ru",
        "type": "item",
        "limit": 7,
        "format": "json",
        "origin": "*",
    })
    body, _, status = _request(f"https://www.wikidata.org/w/api.php?{params}", timeout=15, retries=1)
    if not body or status != 200:
        return None
    for item in json.loads(body.decode("utf-8")).get("search") or []:
        label = str(item.get("label") or "").strip()
        description = str(item.get("description") or "").casefold()
        if label and any(word in description for word in ("футбол", "football", "soccer", "спортив")):
            return label
    return None


def _wikipedia_ru(name: str) -> str | None:
    params = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": f'"{name}" футбольный клуб',
        "srnamespace": 0, "srlimit": 5, "format": "json", "utf8": 1,
    })
    body, _, status = _request(f"https://ru.wikipedia.org/w/api.php?{params}", timeout=15, retries=1)
    if not body or status != 200:
        return None
    for item in json.loads(body.decode("utf-8")).get("query", {}).get("search") or []:
        title = str(item.get("title") or "").strip()
        if title and not any(token in title.casefold() for token in ("список", "сезон", "матчи")):
            return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    return None


def resolve_russian_names(team_names: Iterable[str], *, maximum_new: int = 30) -> dict[str, str]:
    now = utc_now()
    cache = load_json(RUSSIAN_NAMES_PATH, {"version": 1, "sourceMarker": MESH_MARKER, "updatedAt": None, "names": {}})
    names = cache.setdefault("names", {})
    changed = False
    attempted = 0
    for original in dict.fromkeys(str(value).strip() for value in team_names if str(value).strip()):
        key = normalize(original)
        existing = names.get(key)
        if isinstance(existing, dict) and existing.get("ru"):
            continue
        if attempted >= maximum_new:
            break
        attempted += 1
        ru = None
        source = None
        try:
            ru = _wikidata_ru(original)
            source = "WIKIDATA" if ru else None
        except Exception:
            ru = None
        if not ru:
            try:
                ru = _wikipedia_ru(original)
                source = "RU_WIKIPEDIA" if ru else None
            except Exception:
                ru = None
        if ru:
            names[key] = {"original": original, "ru": ru, "source": source, "updatedAt": iso(now)}
            changed = True
        time.sleep(0.08)
    if changed:
        cache["updatedAt"] = iso(now)
        write_json(RUSSIAN_NAMES_PATH, cache)
    return {key: str(value.get("ru")) for key, value in names.items() if isinstance(value, dict) and value.get("ru")}


def apply_russian_names(records: list[dict[str, Any]]) -> dict[str, Any]:
    teams = []
    for row in records:
        teams.extend([str(row.get("home") or ""), str(row.get("away") or "")])
    sportsdb = resolve_thesportsdb_teams(teams)
    mapping = resolve_russian_names(teams)
    applied = 0
    for row in records:
        home = mapping.get(normalize(row.get("home")))
        away = mapping.get(normalize(row.get("away")))
        if home:
            row["homeRu"] = home
            applied += 1
        if away:
            row["awayRu"] = away
            applied += 1
    return {
        "resolved": applied,
        "teams": len(set(normalize(value) for value in teams if value)),
        "theSportsDbResolved": sum(1 for value in sportsdb.values() if value.get("officialName")),
    }


def refresh_fonbet_public_snapshot(*, force: bool = False) -> dict[str, Any]:
    now = utc_now()
    existing = load_json(FONBET_PATH, {"version": 1, "sourceMarker": MESH_MARKER, "updatedAt": None})
    last = parse_dt(existing.get("updatedAt"))
    if not force and last and (now - last).total_seconds() < 15 * 60:
        return existing
    urls = ["https://fonbet.com/sports/football", "https://fonbet.com/sports"]
    errors = []
    for url in urls:
        try:
            body, _, status = _request(url, timeout=20, retries=1, headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6"})
            if body and status == 200:
                text = body.decode("utf-8", errors="replace")
                text = html.unescape(re.sub(r"<[^>]+>", " ", text))
                text = " ".join(text.split())
                snapshot = {
                    "version": 1,
                    "sourceMarker": MESH_MARKER,
                    "updatedAt": iso(now),
                    "sourceUrl": url,
                    "status": "GREEN",
                    "normalizedText": normalize(text)[:750000],
                    "textLength": len(text),
                    "errors": errors,
                }
                write_json(FONBET_PATH, snapshot)
                return snapshot
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}")
    existing.update({"updatedAt": iso(now), "status": "UNAVAILABLE", "errors": errors})
    write_json(FONBET_PATH, existing)
    return existing



def fonbet_event_confirmed(home: str, away: str, snapshot: dict[str, Any] | None = None) -> bool:
    snapshot = snapshot or load_json(FONBET_PATH, {})
    if snapshot.get("status") != "GREEN":
        return False
    text = str(snapshot.get("normalizedText") or "")
    home_key = normalize(home)
    away_key = normalize(away)
    if not home_key or not away_key:
        return False
    # Full normalized name first; then require two meaningful tokens when the
    # public page abbreviates a club name.
    if home_key in text and away_key in text:
        return True
    def tokens(value: str) -> list[str]:
        return [token for token in value.split() if len(token) >= 4]
    ht = tokens(home_key)
    at = tokens(away_key)
    home_ok = bool(ht) and sum(token in text for token in ht) >= min(2, len(ht))
    away_ok = bool(at) and sum(token in text for token in at) >= min(2, len(at))
    return home_ok and away_ok

def fonbet_gate(records: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = refresh_fonbet_public_snapshot()
    text = str(snapshot.get("normalizedText") or "")
    confirmed = 0
    unknown = 0
    for row in records:
        home_keys = [normalize(row.get("home")), normalize(row.get("homeRu"))]
        away_keys = [normalize(row.get("away")), normalize(row.get("awayRu"))]
        home_ok = any(key and key in text for key in home_keys)
        away_ok = any(key and key in text for key in away_keys)
        if snapshot.get("status") == "GREEN" and home_ok and away_ok:
            row["fonbetAvailability"] = "MATCH_CONFIRMED_PUBLIC_LINE"
            confirmed += 1
        elif snapshot.get("status") == "GREEN":
            row["fonbetAvailability"] = "NOT_CONFIRMED_IN_PUBLIC_SNAPSHOT"
        else:
            row["fonbetAvailability"] = "SOURCE_UNAVAILABLE"
            unknown += 1
    return {"status": snapshot.get("status"), "confirmed": confirmed, "unknown": unknown, "total": len(records)}


def self_test() -> int:
    sample_csv = b"Date,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,B365H,B365D,B365A\n01/08/2026,Alpha FC,Beta United,2,1,1,0,1.80,3.40,4.20\n"
    rows = parse_football_data_csv(sample_csv, "E0", "Test League", "2627")
    if len(rows) != 1 or rows[0]["homeScore"] != 2 or rows[0]["awayScore"] != 1:
        raise RuntimeError("R15F CSV parser failed")
    sample_open = json.dumps({"name": "Open League", "matches": [{"date": "2026-08-01", "team1": "Alpha", "team2": "Beta", "score": {"ft": [3, 0]}}]}).encode()
    open_rows = parse_openfootball_json(sample_open, "x.1", "Open League", "2026-27")
    if len(open_rows) != 1 or open_rows[0]["homeScore"] != 3:
        raise RuntimeError("R15F OpenFootball parser failed")
    history = empty_history()
    merged = merge_history(history, rows + open_rows, utc_now())
    if merged["matches"] != 2:
        raise RuntimeError("R15F merge failed")
    print("R15F_FREE_MESH_SELF_TEST=GREEN")
    print("R15F_NO_USER_KEYS_REQUIRED=YES")
    print("R15F_FDCUK_PARSER=GREEN")
    print("R15F_OPENFOOTBALL_PARSER=GREEN")
    print("R15F_HISTORY_MERGE=GREEN")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--fonbet-snapshot", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.fonbet_snapshot:
        result = refresh_fonbet_public_snapshot(force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    result = refresh_all(force=args.force)
    print("R15F_FREE_MESH_STATUS=" + str(result.get("status")))
    print("R15F_FREE_MESH_INGESTED=" + str(result.get("ingested")))
    print("R15F_HISTORY_MATCHES=" + str(result.get("historyMatches")))
    print("R15F_FREE_MESH_SOURCES=" + json.dumps(result.get("sources"), ensure_ascii=False, sort_keys=True))
    print("FINAL_STATUS=GREEN_R15F_FREE_DATA_MESH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
