"""Server-side movie/show title lookup for the Rename UI."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MediaType = Literal["movie", "tv"]
SearchType = Literal["movie", "tv", "any"]


@dataclass(frozen=True)
class MediaMatch:
    provider: str
    provider_id: str
    media_type: MediaType
    title: str
    year: int | None


class LookupUnavailable(RuntimeError):
    """The selected metadata provider is not configured."""


class LookupFailed(RuntimeError):
    """The provider could not complete a lookup."""


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, str, int | None, str], tuple[float, tuple[MediaMatch, ...]]] = {}
_CACHE_TTL_SECONDS = 6 * 60 * 60
_CACHE_MAX_ENTRIES = 256
_TITLE_YEAR = re.compile(r"\s*\((19\d{2}|20\d{2})\)\s*$")


_MOCK_MATCHES = (
    MediaMatch("mock", "693134", "movie", "Dune: Part Two", 2024),
    MediaMatch("mock", "155", "movie", "The Dark Knight", 2008),
    MediaMatch("mock", "49026", "movie", "The Dark Knight Rises", 2012),
    MediaMatch("mock", "424", "movie", "Schindler's List", 1993),
    MediaMatch("mock", "9394", "movie", "They Call Me \"Trinity\"", 1970),
    MediaMatch("mock", "2316", "tv", "The Office", 2005),
    MediaMatch("mock", "2996", "tv", "The Office", 2001),
    MediaMatch("mock", "1396", "tv", "Breaking Bad", 2008),
    MediaMatch("mock", "60574", "tv", "Peaky Blinders", 2013),
    MediaMatch("mock", "series-a", "tv", "Series A", 2021),
    MediaMatch("mock", "show-name", "tv", "Show Name", 2020),
)


def provider_name() -> str:
    provider = os.environ.get("VHC_METADATA_PROVIDER", "tmdb").strip().lower()
    return "mock" if provider == "mock" else "tmdb"


def provider_configured() -> bool:
    return provider_name() == "mock" or bool(os.environ.get("TMDB_API_TOKEN", "").strip())


def _normalized(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def _query_and_year(query: str, year: int | None) -> tuple[str, int | None]:
    query = " ".join(query.strip().split())[:120]
    match = _TITLE_YEAR.search(query)
    if match:
        year = year or int(match.group(1))
        query = query[:match.start()].rstrip()
    return query, year


def _rank(matches: list[MediaMatch], query: str,
          search_type: SearchType, year: int | None) -> list[MediaMatch]:
    query_key = _normalized(query)

    def score(item: tuple[int, MediaMatch]) -> tuple[float, int]:
        index, match = item
        title_key = _normalized(match.title)
        similarity = SequenceMatcher(None, query_key, title_key).ratio()
        value = similarity * 100
        if title_key == query_key:
            value += 1000
        elif title_key.startswith(query_key):
            value += 350
        elif query_key in title_key:
            value += 175
        if year and match.year == year:
            value += 300
        if search_type != "any" and match.media_type == search_type:
            value += 100
        return value, -index

    return [
        match for _, match in sorted(
            enumerate(matches), key=score, reverse=True,
        )
    ]


def _parse_tmdb_results(payload: dict, media_type: SearchType) -> list[MediaMatch]:
    matches: list[MediaMatch] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.get("results") or []:
        item_type = item.get("media_type") or (media_type if media_type != "any" else None)
        if item_type not in ("movie", "tv"):
            continue
        title = item.get("title") if item_type == "movie" else item.get("name")
        if not isinstance(title, str) or not title.strip() or item.get("id") is None:
            continue
        date = item.get("release_date") if item_type == "movie" else item.get("first_air_date")
        year = int(date[:4]) if isinstance(date, str) and re.match(r"^\d{4}", date) else None
        dedupe_key = (item_type, str(item["id"]))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append(MediaMatch(
            provider="tmdb",
            provider_id=str(item["id"]),
            media_type=item_type,
            title=title.strip(),
            year=year,
        ))
    return matches


def _search_mock(query: str, media_type: SearchType) -> list[MediaMatch]:
    query_key = _normalized(query)
    matches = []
    for match in _MOCK_MATCHES:
        if media_type != "any" and match.media_type != media_type:
            continue
        title_key = _normalized(match.title)
        if query_key in title_key or SequenceMatcher(None, query_key, title_key).ratio() >= 0.42:
            matches.append(match)
    return matches


def _search_tmdb(query: str, media_type: SearchType,
                 year: int | None, language: str) -> list[MediaMatch]:
    token = os.environ.get("TMDB_API_TOKEN", "").strip()
    if not token:
        raise LookupUnavailable("TMDB title matching is not configured")

    endpoint = "multi" if media_type == "any" else media_type
    params: dict[str, str] = {
        "query": query,
        "language": language,
        "include_adult": "false",
        "page": "1",
    }

    request = Request(
        f"https://api.themoviedb.org/3/search/{endpoint}?{urlencode(params)}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "VideoHEVCConverter/1.0",
        },
    )
    try:
        with urlopen(request, timeout=3.5) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise LookupUnavailable("TMDB rejected the configured API token") from error
        if error.code == 429:
            raise LookupFailed("TMDB is rate limiting searches; try again shortly") from error
        raise LookupFailed(f"TMDB search failed with HTTP {error.code}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise LookupFailed("TMDB is temporarily unavailable") from error
    return _parse_tmdb_results(payload, media_type)


def search_media(query: str, media_type: SearchType = "any",
                 year: int | None = None, limit: int = 8) -> list[MediaMatch]:
    """Return ranked movie/show title matches without exposing provider secrets."""
    if media_type not in ("movie", "tv", "any"):
        media_type = "any"
    query, year = _query_and_year(query, year)
    if len(query) < 2:
        return []

    provider = provider_name()
    language = os.environ.get("TMDB_LANGUAGE", "en-US").strip() or "en-US"
    cache_key = (provider, query.casefold(), media_type, year, language)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1][:limit])

    if provider == "mock":
        matches = _search_mock(query, media_type)
    else:
        matches = _search_tmdb(query, media_type, year, language)
    ranked = tuple(_rank(matches, query, media_type, year)[:20])

    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest_key, None)
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, ranked)
    return list(ranked[:limit])