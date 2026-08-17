"""eBird API 2.0 client and configuration."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.ebird.org/v2"
ROOT = Path(__file__).parent
CHECKLIST_CACHE_VERSION = 1
REGION_SPECIES_CACHE_PATH = ROOT / "ebird_region_species_cache.json"
LAST_SEEN_CACHE_PATH = ROOT / "ebird_last_seen_cache.json"
BIRDNET_CODE_CACHE_PATH = ROOT / "birdnet_code_cache.json"
REGION_LIST_CACHE_PATH = ROOT / "ebird_region_list_cache.json"
CHECKLISTS_DIR = ROOT / "ebird_checklists"
HOTSPOT_CACHE_VERSION = 1
REGION_LIST_CACHE_VERSION = 1
MAX_RATE_LIMIT_RETRIES = 8
MIN_RATE_LIMIT_WAIT_SECONDS = 1.0

load_dotenv(ROOT / ".env")


def get_api_key() -> str | None:
    """Return the eBird API key from env, secrets, URL param, or session input."""
    for candidate in (
        os.environ.get("EBIRD_API_KEY"),
        os.environ.get("EBIRD_API_TOKEN"),
    ):
        key = _clean_api_key(candidate)
        if key:
            return key

    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is not None:
            for name in ("EBIRD_API_KEY", "EBIRD_API_TOKEN"):
                if name in secrets:
                    key = _clean_api_key(secrets[name])
                    if key:
                        return key

        _ingest_api_key_from_query()

        session_key = st.session_state.get("ebird_api_key")
        key = _clean_api_key(session_key)
        if key:
            return key
    except Exception:
        pass

    return None


def _query_param_value(params: object, name: str) -> object:
    try:
        if name not in params:
            return None
        value = params.get(name) if hasattr(params, "get") else params[name]
    except Exception:
        return None
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _ingest_api_key_from_query() -> None:
    """Copy an API key from URL query params into session state, then drop it from the URL."""
    try:
        import streamlit as st
    except Exception:
        return

    params = getattr(st, "query_params", None)
    if params is None:
        return

    for name in ("EBIRD_API_KEY", "ebird_api_key", "api_key"):
        key = _clean_api_key(_query_param_value(params, name))
        if not key:
            continue
        st.session_state.ebird_api_key = key
        try:
            del params[name]
        except Exception:
            try:
                params.pop(name)
            except Exception:
                pass
        break


def _clean_api_key(value: object) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key or key == "your_ebird_api_key_here":
        return None
    return key


def region_year_checklist_cache_path(region_code: str, year: int) -> Path:
    """Location of the on-disk regional daily-checklist cache."""
    safe_region = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in region_code.strip()
    )
    return ROOT / f"ebird_{safe_region}_checklists_{year}.json"


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_region_component(region_code: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in region_code.strip()
    )


def local_checklist_index_path(region_code: str) -> Path:
    return ROOT / f"ebird_{_safe_region_component(region_code)}_local_last_seen.json"


def hotspots_cache_path(region_code: str) -> Path:
    return ROOT / f"ebird_{_safe_region_component(region_code)}_hotspots.json"


def load_cached_region_list(region_type: str, parent_region_code: str) -> list[dict[str, str]]:
    """Return a cached eBird region list, or []."""
    cache = _load_json_file(REGION_LIST_CACHE_PATH)
    if cache.get("cache_version") != REGION_LIST_CACHE_VERSION:
        return []
    key = f"{region_type}:{parent_region_code}"
    rows = (cache.get("lists") or {}).get(key)
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        if code:
            cleaned.append({"code": code, "name": name or code})
    return cleaned


def save_cached_region_list(
    region_type: str,
    parent_region_code: str,
    rows: list[dict[str, str]],
) -> None:
    """Persist an eBird region list for reuse."""
    cache = _load_json_file(REGION_LIST_CACHE_PATH)
    if cache.get("cache_version") != REGION_LIST_CACHE_VERSION:
        cache = {"cache_version": REGION_LIST_CACHE_VERSION, "lists": {}}
    lists = cache.get("lists")
    if not isinstance(lists, dict):
        lists = {}
    key = f"{region_type}:{parent_region_code}"
    lists[key] = rows
    cache["lists"] = lists
    cache["updated_at"] = datetime.now().astimezone().isoformat()
    _save_json_file(REGION_LIST_CACHE_PATH, cache)


def filter_regions_by_query(
    rows: list[dict[str, str]],
    query: str,
) -> list[dict[str, str]]:
    """Filter region rows by code or name substring."""
    needle = (query or "").strip().casefold()
    if not needle:
        return list(rows)
    matches: list[dict[str, str]] = []
    for row in rows:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        if needle in code.casefold() or needle in name.casefold():
            matches.append(row)
    return matches


def load_cached_hotspots(region_code: str) -> list[dict[str, Any]]:
    """Return disk-cached top hotspots for a region, or []."""
    code = (region_code or "").strip()
    if not code:
        return []
    data = _load_json_file(hotspots_cache_path(code))
    if data.get("cache_version") != HOTSPOT_CACHE_VERSION:
        return []
    if data.get("region_code") != code:
        return []
    rows = data.get("hotspots")
    return rows if isinstance(rows, list) else []


def save_cached_hotspots(region_code: str, hotspots: list[dict[str, Any]]) -> Path:
    """Persist top hotspots for a region to disk."""
    code = (region_code or "").strip()
    path = hotspots_cache_path(code)
    _save_json_file(
        path,
        {
            "cache_version": HOTSPOT_CACHE_VERSION,
            "region_code": code,
            "updated_at": datetime.now().astimezone().isoformat(),
            "hotspots": hotspots,
        },
    )
    return path


def _parse_checklist_day(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Prefer ISO prefix: 2026-08-03 or 2026-08-03 14:06
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return None


CHECKLIST_CACHE_STATUS_VERSION = 1


def checklist_cache_status_path(region_code: str, year: int) -> Path:
    """Location of the derived checklist download status index."""
    return (
        ROOT
        / f"ebird_{_safe_region_component(region_code)}_checklist_cache_status_{year}.json"
    )


def build_checklist_cache_status(
    region_code: str,
    year: int,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Summarize downloaded checklist coverage by day and hotspot.

    Compares on-disk checklist detail files under ``ebird_checklists/{region}``
    against the regional daily feed cache when present. Results are cached on
    disk and rebuilt when the file set or feed cache changes.
    """
    region = (region_code or "").strip()
    if not region:
        return {
            "region_code": "",
            "year": year,
            "days": [],
            "hotspots": [],
            "downloaded_total": 0,
            "expected_total": 0,
            "days_with_downloads": 0,
            "hotspot_count": 0,
            "truncated_dates": [],
            "feed_cache_exists": False,
        }

    root = CHECKLISTS_DIR / region
    feed_path = region_year_checklist_cache_path(region, year)
    files = sorted(root.rglob("S*.json")) if root.exists() else []
    signature = {
        "version": CHECKLIST_CACHE_STATUS_VERSION,
        "file_count": len(files),
        "newest_mtime": max((path.stat().st_mtime for path in files), default=0.0),
        "feed_mtime": feed_path.stat().st_mtime if feed_path.exists() else 0.0,
        "feed_path": str(feed_path),
    }
    status_path = checklist_cache_status_path(region, year)
    if not force_refresh:
        existing = _load_json_file(status_path)
        if (
            existing.get("signature") == signature
            and existing.get("cache_version") == CHECKLIST_CACHE_STATUS_VERSION
            and isinstance(existing.get("days"), list)
            and isinstance(existing.get("hotspots"), list)
        ):
            return existing

    year_prefix = f"{year}-"
    expected_by_day: dict[str, int] = {}
    truncated_dates: list[str] = []
    expected_ids: set[str] = set()
    feed_cache_exists = feed_path.exists()
    if feed_cache_exists:
        feed = _load_json_file(feed_path)
        truncated_dates = [
            str(day)
            for day in (feed.get("truncated_dates") or [])
            if str(day).startswith(year_prefix)
        ]
        for day_key, entry in (feed.get("daily") or {}).items():
            day = str(day_key)
            if not day.startswith(year_prefix) or not isinstance(entry, dict):
                continue
            rows = entry.get("checklists") or []
            day_ids: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                checklist_id = str(
                    row.get("subId") or row.get("subID") or ""
                ).strip()
                if checklist_id:
                    day_ids.add(checklist_id)
                    expected_ids.add(checklist_id)
            expected_by_day[day] = len(day_ids)

    downloaded_by_day: dict[str, int] = {}
    downloaded_ids: set[str] = set()
    hotspot_stats: dict[str, dict[str, Any]] = {}

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        checklist = payload.get("checklist") if isinstance(payload.get("checklist"), dict) else {}
        feed = (
            payload.get("feed_summary")
            if isinstance(payload.get("feed_summary"), dict)
            else {}
        )
        obs_day = _parse_checklist_day(
            checklist.get("obsDt") or feed.get("isoObsDate") or feed.get("obsDt")
        )
        if obs_day is None or obs_day.year != year:
            continue
        day_key = obs_day.isoformat()
        sub_id = str(
            checklist.get("subId") or feed.get("subId") or feed.get("subID") or path.stem
        ).strip()
        if sub_id:
            downloaded_ids.add(sub_id)
        downloaded_by_day[day_key] = downloaded_by_day.get(day_key, 0) + 1

        loc_obj = feed.get("loc") if isinstance(feed.get("loc"), dict) else {}
        loc_id = str(
            checklist.get("locId")
            or feed.get("locId")
            or feed.get("locID")
            or loc_obj.get("locId")
            or loc_obj.get("locID")
            or ""
        ).strip()
        if not loc_id:
            parent = path.parent.name
            loc_id = parent.split("__", 1)[0] if parent else "unknown"
        loc_name = str(
            feed.get("locName")
            or loc_obj.get("locName")
            or loc_obj.get("name")
            or ""
        ).strip()
        if not loc_name and "__" in path.parent.name:
            loc_name = path.parent.name.split("__", 1)[1].replace("_", " ").strip()

        hotspot = hotspot_stats.get(loc_id)
        if hotspot is None:
            hotspot_stats[loc_id] = {
                "locId": loc_id,
                "locName": loc_name,
                "checklists": 1,
                "first_day": day_key,
                "last_day": day_key,
            }
        else:
            hotspot["checklists"] = int(hotspot["checklists"]) + 1
            if loc_name and not hotspot.get("locName"):
                hotspot["locName"] = loc_name
            if day_key < str(hotspot["first_day"]):
                hotspot["first_day"] = day_key
            if day_key > str(hotspot["last_day"]):
                hotspot["last_day"] = day_key

    all_days = sorted(set(expected_by_day) | set(downloaded_by_day))
    truncated_set = set(truncated_dates)
    days = [
        {
            "day": day,
            "expected": int(expected_by_day.get(day, 0)),
            "downloaded": int(downloaded_by_day.get(day, 0)),
            "truncated": day in truncated_set,
        }
        for day in all_days
    ]
    hotspots = sorted(
        hotspot_stats.values(),
        key=lambda row: (-int(row["checklists"]), str(row.get("locName") or ""), str(row["locId"])),
    )
    result = {
        "cache_version": CHECKLIST_CACHE_STATUS_VERSION,
        "signature": signature,
        "region_code": region,
        "year": year,
        "updated_at": datetime.now().astimezone().isoformat(),
        "feed_cache_exists": feed_cache_exists,
        "feed_cache_path": str(feed_path),
        "checklists_dir": str(root),
        "expected_total": len(expected_ids) if expected_ids else sum(expected_by_day.values()),
        "downloaded_total": len(downloaded_ids) if downloaded_ids else sum(downloaded_by_day.values()),
        "days_in_feed": len(expected_by_day),
        "days_with_downloads": sum(1 for count in downloaded_by_day.values() if count > 0),
        "hotspot_count": len(hotspots),
        "truncated_dates": truncated_dates,
        "days": days,
        "hotspots": hotspots,
    }
    _save_json_file(status_path, result)
    return result


def load_local_checklists_for_hotspot(
    region_code: str,
    loc_id: str,
    *,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    """Load downloaded checklist files for a hotspot within a date window."""
    return load_local_checklists(
        region_code,
        loc_id=loc_id,
        start_date=start_date,
        end_date=end_date,
    )


def load_local_checklists(
    region_code: str,
    *,
    start_date: date,
    end_date: date,
    loc_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load downloaded checklist files within a date window.

    When ``loc_id`` is set, only that hotspot is included; otherwise every
    downloaded checklist in the region for the window is returned.

    Returns feed-summary-shaped rows with ``_detail`` set to the full checklist
    payload so callers can enrich without another API call.
    """
    region = (region_code or "").strip()
    location = (loc_id or "").strip() or None
    if not region:
        return []
    root = CHECKLISTS_DIR / region
    if not root.exists():
        return []

    found: dict[str, dict[str, Any]] = {}
    for path in root.rglob("S*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        checklist = payload.get("checklist") or {}
        feed = payload.get("feed_summary") or {}
        if not isinstance(checklist, dict):
            continue
        loc_obj = feed.get("loc") if isinstance(feed.get("loc"), dict) else {}
        file_loc = str(
            checklist.get("locId")
            or feed.get("locId")
            or feed.get("locID")
            or loc_obj.get("locId")
            or loc_obj.get("locID")
            or ""
        ).strip()
        if not file_loc:
            parent = path.parent.name
            file_loc = parent.split("__", 1)[0] if parent else ""
        if location is not None and file_loc != location:
            continue
        obs_day = _parse_checklist_day(
            checklist.get("obsDt") or feed.get("isoObsDate") or feed.get("obsDt")
        )
        if obs_day is None or obs_day < start_date or obs_day > end_date:
            continue
        sub_id = str(
            checklist.get("subId") or feed.get("subId") or feed.get("subID") or path.stem
        ).strip()
        if not sub_id:
            continue
        loc_name = str(
            feed.get("locName")
            or loc_obj.get("locName")
            or loc_obj.get("name")
            or ""
        ).strip()
        if not loc_name and "__" in path.parent.name:
            loc_name = path.parent.name.split("__", 1)[1].replace("_", " ").strip()
        summary = {
            "subId": sub_id,
            "subID": sub_id,
            "locId": file_loc,
            "locID": file_loc,
            "locName": loc_name,
            "obsDt": str(checklist.get("obsDt") or feed.get("obsDt") or ""),
            "isoObsDate": str(
                feed.get("isoObsDate") or checklist.get("obsDt") or feed.get("obsDt") or ""
            ),
            "numSpecies": checklist.get("numSpecies") or feed.get("numSpecies"),
            "userDisplayName": checklist.get("userDisplayName")
            or feed.get("userDisplayName")
            or "",
            "_detail": checklist,
            "_source": "local_cache",
            "_path": str(path),
        }
        previous = found.get(sub_id)
        if previous is None or str(summary["isoObsDate"]) >= str(
            previous.get("isoObsDate") or ""
        ):
            found[sub_id] = summary

    return sorted(
        found.values(),
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )


def _parse_obs_count(obs: dict[str, Any]) -> int | None:
    for key in ("howMany", "howManyAtleast", "howManyStr"):
        raw = obs.get(key)
        if raw in (None, "", "X"):
            continue
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            continue
    return None


def build_local_last_seen_index(region_code: str) -> dict[str, dict[str, Any]]:
    """Index species last-seen info from downloaded checklist detail files."""
    region = (region_code or "").strip()
    if not region:
        return {}
    root = CHECKLISTS_DIR / region
    index_path = local_checklist_index_path(region)
    files = sorted(root.rglob("S*.json")) if root.exists() else []
    signature = {
        "file_count": len(files),
        "newest_mtime": max((path.stat().st_mtime for path in files), default=0.0),
    }
    existing = _load_json_file(index_path)
    if (
        existing.get("signature") == signature
        and isinstance(existing.get("by_code"), dict)
    ):
        return {
            str(code): value
            for code, value in existing["by_code"].items()
            if isinstance(value, dict)
        }

    by_code: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        checklist = payload.get("checklist") or {}
        feed = payload.get("feed_summary") or {}
        if not isinstance(checklist, dict):
            continue
        obs_dt = str(
            checklist.get("obsDt")
            or feed.get("isoObsDate")
            or feed.get("obsDt")
            or ""
        ).strip()
        if not obs_dt:
            continue
        loc_id = str(
            checklist.get("locId") or feed.get("locId") or feed.get("locID") or ""
        ).strip()
        loc_obj = feed.get("loc") if isinstance(feed.get("loc"), dict) else {}
        loc_name = str(
            feed.get("locName")
            or loc_obj.get("locName")
            or loc_obj.get("name")
            or ""
        ).strip()
        if not loc_name:
            parent_name = path.parent.name
            if "__" in parent_name:
                loc_name = parent_name.split("__", 1)[1].replace("_", " ").strip()
        if not loc_id:
            loc_id = str(loc_obj.get("locId") or loc_obj.get("locID") or "").strip()
        sub_id = str(
            checklist.get("subId") or feed.get("subId") or feed.get("subID") or path.stem
        ).strip()
        for obs in checklist.get("obs") or []:
            if not isinstance(obs, dict):
                continue
            code = str(obs.get("speciesCode") or "").strip()
            if not code:
                continue
            current = by_code.get(code)
            if current and str(current.get("obsDt") or "") >= obs_dt:
                continue
            by_code[code] = {
                "speciesCode": code,
                "obsDt": obs_dt,
                "locName": loc_name,
                "locId": loc_id,
                "howMany": _parse_obs_count(obs),
                "subId": sub_id,
                "source": "local_checklist",
            }

    _save_json_file(
        index_path,
        {
            "region_code": region,
            "signature": signature,
            "updated_at": datetime.now().astimezone().isoformat(),
            "by_code": by_code,
        },
    )
    return by_code


def note_rate_limit(seconds: float, *, path: str) -> None:
    """Record a rate-limit wait for UI surfaces (Streamlit session when available)."""
    event = {
        "seconds": float(seconds),
        "path": path,
        "at": datetime.now().astimezone().isoformat(),
    }
    print(
        f"[rate-limit] waiting {seconds:.0f}s for {path}",
        flush=True,
    )
    try:
        import streamlit as st

        events = list(st.session_state.get("ebird_rate_limit_events") or [])
        events.append(event)
        st.session_state.ebird_rate_limit_events = events
        st.session_state.ebird_rate_limit_active = event
        if hasattr(st, "toast"):
            st.toast(f"eBird rate limit — waiting {seconds:.0f}s…")
    except Exception:
        pass


def resolve_ebird_code(
    *,
    scientific_name: str | None = None,
    common_name: str | None = None,
) -> str | None:
    """Resolve an eBird species code via BirdNET taxonomy (disk-cached)."""
    sci = (scientific_name or "").strip()
    common = (common_name or "").strip()
    lookup = sci or common
    if not lookup:
        return None
    cache = _load_json_file(BIRDNET_CODE_CACHE_PATH)
    cache_key = f"sci:{sci.casefold()}" if sci else f"common:{common.casefold()}"
    if cache_key in cache:
        value = cache[cache_key]
        return str(value) if value else None
    try:
        response = requests.get(
            f"https://birdnet.cornell.edu/taxonomy/api/species/{quote(lookup, safe='')}",
            timeout=20,
        )
        if response.status_code == 404:
            cache[cache_key] = ""
            _save_json_file(BIRDNET_CODE_CACHE_PATH, cache)
            return None
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return None
    code = str((payload or {}).get("ebird_code") or "").strip()
    cache[cache_key] = code
    if sci and common:
        cache[f"common:{common.casefold()}"] = code
    _save_json_file(BIRDNET_CODE_CACHE_PATH, cache)
    return code or None


class EBirdClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        min_rate_limit_wait_seconds: float = MIN_RATE_LIMIT_WAIT_SECONDS,
    ) -> None:
        self.api_key = api_key or get_api_key()
        if not self.api_key:
            raise ValueError(
                "Missing eBird API key. Set EBIRD_API_KEY in .env or "
                ".streamlit/secrets.toml. Get a key at https://ebird.org/api/keygen"
            )
        self.session = requests.Session()
        self.session.headers.update({"X-eBirdApiToken": self.api_key})
        self.rate_limit_events: list[dict[str, Any]] = []
        self.min_rate_limit_wait_seconds = max(
            float(min_rate_limit_wait_seconds),
            MIN_RATE_LIMIT_WAIT_SECONDS,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        started = time.perf_counter()
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            response = self.session.get(
                f"{BASE_URL}{path}",
                params=params,
                timeout=30,
            )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else 60.0
                except ValueError:
                    wait_seconds = 60.0
                wait_seconds = max(
                    wait_seconds,
                    self.min_rate_limit_wait_seconds,
                )
                event = {
                    "seconds": wait_seconds,
                    "path": path,
                    "attempt": attempt + 1,
                }
                self.rate_limit_events.append(event)
                note_rate_limit(wait_seconds, path=path)
                time.sleep(wait_seconds)
                continue
            response.raise_for_status()
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(
                f"[timing] ebird_get: {elapsed_ms:.0f}ms path={path} "
                f"status={response.status_code}",
                flush=True,
            )
            return response.json()
        raise requests.HTTPError(
            f"eBird rate limit retries exhausted for {path} "
            f"after {MAX_RATE_LIMIT_RETRIES} attempts"
        )

    def taxonomy(self, species: str | None = None) -> Any:
        params: dict[str, Any] = {"fmt": "json"}
        if species:
            params["species"] = species
        return self.get("/ref/taxonomy/ebird", params=params)

    def species_names(self, species_codes: list[str]) -> dict[str, str]:
        """Map species codes to common names via the taxonomy endpoint."""
        return {
            code: row["comName"]
            for code, row in self.species_taxa(species_codes).items()
            if row.get("comName")
        }

    def species_taxa(self, species_codes: list[str]) -> dict[str, dict[str, Any]]:
        """Map species codes to taxonomy rows (common name, sci name, category)."""
        codes = sorted({code for code in species_codes if code})
        taxa: dict[str, dict[str, Any]] = {}
        batch_size = 50
        for start in range(0, len(codes), batch_size):
            batch = codes[start : start + batch_size]
            rows = self.taxonomy(species=",".join(batch))
            if not isinstance(rows, list):
                continue
            for row in rows:
                code = row.get("speciesCode")
                if code:
                    taxa[str(code)] = row
        for code in codes:
            if code in taxa:
                continue
            rows = self.taxonomy(species=code)
            if isinstance(rows, list) and rows:
                taxa[code] = rows[0]
        return taxa

    def recent_observations(
        self,
        region_code: str,
        species_code: str | None = None,
        *,
        back: int = 14,
        max_results: int | None = 10,
    ) -> Any:
        if species_code:
            path = f"/data/obs/{region_code}/recent/{species_code}"
            params: dict[str, Any] = {"back": back}
            # Species-specific recent feeds reject maxResults on some deployments.
        else:
            path = f"/data/obs/{region_code}/recent"
            params = {"back": back}
            if max_results is not None:
                params["maxResults"] = max_results
        return self.get(path, params=params)

    def cached_region_species_codes(self, region_code: str) -> set[str]:
        """Return species codes ever recorded in a region (disk-cached)."""
        code = (region_code or "").strip()
        if not code:
            return set()
        cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
        cached = cache.get(code)
        if isinstance(cached, list):
            return {str(item) for item in cached if item}
        codes = self.region_species_codes(code)
        cache[code] = codes
        _save_json_file(REGION_SPECIES_CACHE_PATH, cache)
        return set(codes)

    def last_seen_in_region(
        self,
        region_code: str,
        species_code: str,
        *,
        back: int = 30,
        allow_api: bool = True,
    ) -> dict[str, Any] | None:
        """Most recent observation of a species in a region.

        Prefers downloaded checklist detail files. Falls back to the eBird
        recent-observations API only when local data is missing and
        ``allow_api`` is true.
        """
        region = (region_code or "").strip()
        code = (species_code or "").strip()
        if not region or not code:
            return None

        local_index = build_local_last_seen_index(region)
        local = local_index.get(code)
        if local:
            return dict(local)

        if not allow_api:
            return None

        cache = _load_json_file(LAST_SEEN_CACHE_PATH)
        cache_key = f"{region}|{code}|{back}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "fetched_at" in cached:
            return cached.get("observation") or None

        observation: dict[str, Any] | None = None
        try:
            rows = self.recent_observations(
                region,
                code,
                back=back,
                max_results=None,
            )
            if isinstance(rows, list) and rows:
                row = rows[0]
                observation = {
                    "speciesCode": row.get("speciesCode") or code,
                    "comName": row.get("comName") or "",
                    "sciName": row.get("sciName") or "",
                    "obsDt": row.get("obsDt") or "",
                    "locName": row.get("locName") or "",
                    "locId": row.get("locId") or "",
                    "howMany": row.get("howMany"),
                    "subId": row.get("subId") or "",
                    "source": "ebird_api",
                }
        except requests.HTTPError as exc:
            # 400/404 typically mean no usable recent result for this taxon.
            status = exc.response.status_code if exc.response is not None else None
            if status not in {400, 404}:
                raise

        cache[cache_key] = {
            "fetched_at": datetime.now().astimezone().isoformat(),
            "observation": observation,
        }
        _save_json_file(LAST_SEEN_CACHE_PATH, cache)
        return observation

    def recent_checklists(self, region_code: str, *, max_results: int = 100) -> list[dict[str, Any]]:
        rows = self.get(
            f"/product/lists/{region_code}",
            params={"maxResults": max_results},
        )
        return rows if isinstance(rows, list) else []

    def checklists_on_date(
        self,
        region_code: str,
        year: int,
        month: int,
        day: int,
        *,
        max_results: int = 200,
    ) -> list[dict[str, Any]]:
        rows = self.get(
            f"/product/lists/{region_code}/{year}/{month}/{day}",
            params={"maxResults": max_results},
        )
        return rows if isinstance(rows, list) else []

    def cache_region_year_checklists(
        self,
        region_code: str,
        year: int,
        *,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Persist daily checklist feeds for a region through today.

        eBird returns at most ``max_results`` entries per date. Dates that hit
        that limit are retained but marked as truncated so consumers do not
        mistake the cache for a complete daily record.
        """
        code = (region_code or "").strip()
        if not code:
            raise ValueError("A region code is required.")
        if year < 2002 or year > date.today().year:
            raise ValueError("Year must be between 2002 and the current year.")

        path = region_year_checklist_cache_path(code, year)
        cache: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(loaded, dict)
                    and loaded.get("cache_version") == CHECKLIST_CACHE_VERSION
                    and loaded.get("region_code") == code
                    and loaded.get("year") == year
                ):
                    cache = loaded
            except (json.JSONDecodeError, OSError):
                cache = {}

        daily = cache.get("daily")
        if not isinstance(daily, dict):
            daily = {}

        today = date.today()
        last_day = min(date(year, 12, 31), today)
        first_day = date(year, 1, 1)
        if first_day > last_day:
            raise ValueError("The requested year has not started yet.")

        day = first_day
        while day <= last_day:
            day_key = day.isoformat()
            # Historical dates do not change; always refresh today.
            if day_key not in daily or day == today:
                rows = self.checklists_on_date(
                    code,
                    day.year,
                    day.month,
                    day.day,
                    max_results=max_results,
                )
                daily[day_key] = {
                    "checklists": rows,
                    "truncated": len(rows) >= max_results,
                }
                cache.update(
                    {
                        "cache_version": CHECKLIST_CACHE_VERSION,
                        "region_code": code,
                        "year": year,
                        "max_results_per_day": max_results,
                        "daily": daily,
                        "updated_at": datetime.now().astimezone().isoformat(),
                    }
                )
                path.write_text(
                    json.dumps(cache, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            day = date.fromordinal(day.toordinal() + 1)

        checklist_ids: set[str] = set()
        for entry in daily.values():
            if not isinstance(entry, dict):
                continue
            for row in entry.get("checklists") or []:
                checklist_id = str(row.get("subId") or row.get("subID") or "")
                if checklist_id:
                    checklist_ids.add(checklist_id)
        cache["unique_checklist_count"] = len(checklist_ids)
        cache["truncated_dates"] = sorted(
            day_key
            for day_key, entry in daily.items()
            if isinstance(entry, dict) and entry.get("truncated")
        )
        cache["updated_at"] = datetime.now().astimezone().isoformat()
        path.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return cache

    def checklist(self, sub_id: str) -> dict[str, Any]:
        data = self.get(f"/product/checklist/view/{sub_id}")
        return data if isinstance(data, dict) else {}

    def region_species_codes(self, region_code: str) -> list[str]:
        """Species codes ever recorded in a region, in eBird taxonomic order.

        Calls ``GET /product/spplist/{regionCode}``, which returns only an
        ordered JSON array of species-level eBird codes — no names or counts.
        """
        code = (region_code or "").strip()
        if not code:
            return []
        rows = self.get(f"/product/spplist/{code}")
        if not isinstance(rows, list):
            return []
        return [str(item).strip() for item in rows if str(item).strip()]

    def list_regions(
        self,
        region_type: str,
        parent_region_code: str = "world",
        *,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[dict[str, str]]:
        """List child regions via ``GET /ref/region/list/{type}/{parent}``.

        ``region_type`` is ``country``, ``subnational1``, or ``subnational2``.
        """
        kind = (region_type or "").strip().lower()
        parent = (parent_region_code or "world").strip() or "world"
        if kind not in {"country", "subnational1", "subnational2"}:
            raise ValueError(
                "region_type must be country, subnational1, or subnational2"
            )
        if use_cache and not refresh:
            cached = load_cached_region_list(kind, parent)
            if cached:
                return cached
        rows = self.get(f"/ref/region/list/{kind}/{quote(parent, safe='')}")
        if not isinstance(rows, list):
            return []
        cleaned: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            name = str(row.get("name") or "").strip()
            if code:
                cleaned.append({"code": code, "name": name or code})
        cleaned.sort(key=lambda item: (item["name"].casefold(), item["code"]))
        save_cached_region_list(kind, parent, cleaned)
        return cleaned

    def region_info(self, region_code: str) -> dict[str, Any]:
        """Return ``GET /ref/region/info/{regionCode}`` metadata."""
        code = (region_code or "").strip()
        if not code:
            return {}
        data = self.get(
            f"/ref/region/info/{quote(code, safe='')}",
            params={"regionNameFormat": "detailed"},
        )
        return data if isinstance(data, dict) else {}

    def hotspots(self, region_code: str, *, back: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fmt": "json"}
        if back is not None:
            params["back"] = back
        rows = self.get(f"/ref/hotspot/{region_code}", params=params)
        return rows if isinstance(rows, list) else []

    def top_hotspots(
        self,
        region_code: str,
        *,
        limit: int = 100,
        back: int | None = None,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        code = (region_code or "").strip()
        if use_cache and not refresh:
            cached = load_cached_hotspots(code)
            if cached:
                return cached[:limit]
        rows = self.hotspots(code, back=back)
        rows = sorted(
            rows,
            key=lambda row: (
                int(row.get("numSpeciesAllTime") or 0),
                int(row.get("numChecklistsAllTime") or 0),
            ),
            reverse=True,
        )
        rows = rows[:limit]
        if use_cache and rows:
            save_cached_hotspots(code, rows)
        return rows

    def location_checklists(
        self,
        loc_id: str,
        *,
        days_back: int = 7,
        end_date: date | None = None,
        max_results: int = 200,
    ) -> list[dict[str, Any]]:
        """Checklists submitted at a hotspot/location over a date window.

        The window ends on ``end_date`` (default: today) and includes
        ``days_back`` days ending on that date.
        """
        from datetime import date, timedelta

        end = end_date or date.today()
        start = end - timedelta(days=days_back - 1)
        found: dict[str, dict[str, Any]] = {}

        def keep(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                sub_id = row.get("subId") or row.get("subID")
                if sub_id:
                    found[str(sub_id)] = row

        # Recent feed is only useful when the window includes today.
        if end >= date.today():
            keep(self.recent_checklists(loc_id, max_results=max_results))

        for offset in range(days_back):
            day = end - timedelta(days=offset)
            keep(
                self.checklists_on_date(
                    loc_id,
                    day.year,
                    day.month,
                    day.day,
                    max_results=max_results,
                )
            )

        def within_window(row: dict[str, Any]) -> bool:
            iso = str(row.get("isoObsDate") or "")
            if not iso:
                return True
            try:
                obs_day = date.fromisoformat(iso[:10])
            except ValueError:
                return True
            return start <= obs_day <= end

        filtered = [row for row in found.values() if within_window(row)]
        return sorted(
            filtered,
            key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
            reverse=True,
        )

    def verify(self) -> dict[str, str]:
        """Hit an authenticated endpoint to confirm the API key works."""
        rows = self.recent_observations("US", back=1, max_results=1)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected eBird response while verifying API key")
        if not rows:
            return {"status": "ok", "detail": "authenticated (no recent US observations)"}
        row = rows[0]
        return {
            "status": "ok",
            "comName": row.get("comName", ""),
            "locName": row.get("locName", ""),
            "obsDt": row.get("obsDt", ""),
        }
