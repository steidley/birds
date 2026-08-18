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

from api_log import log_api_done, log_api_send

BASE_URL = "https://api.ebird.org/v2"
ROOT = Path(__file__).parent
CHECKLIST_CACHE_VERSION = 1
REGION_SPECIES_CACHE_PATH = ROOT / "ebird_region_species_cache.json"
LAST_SEEN_CACHE_PATH = ROOT / "ebird_last_seen_cache.json"
BIRDNET_CODE_CACHE_PATH = ROOT / "birdnet_code_cache.json"
REGION_LIST_CACHE_PATH = ROOT / "ebird_region_list_cache.json"
TAXONOMY_CACHE_PATH = ROOT / "ebird_taxonomy_cache.json"
CHECKLISTS_DIR = ROOT / "ebird_checklists"
HOTSPOT_CACHE_VERSION = 1
REGION_LIST_CACHE_VERSION = 1
TAXONOMY_CACHE_VERSION = 1
MAX_RATE_LIMIT_RETRIES = 8
MIN_RATE_LIMIT_WAIT_SECONDS = 1.0

load_dotenv(ROOT / ".env")


def _streamlit_runtime_active() -> bool:
    """True only when called inside a live Streamlit script run."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx(suppress_warning=True) is not None
    except TypeError:
        # Older Streamlit builds may not accept suppress_warning.
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx

            return get_script_run_ctx() is not None
        except Exception:
            return False
    except Exception:
        return False


def get_api_key() -> str | None:
    """Return the eBird API key from env, secrets, URL param, or session input."""
    for candidate in (
        os.environ.get("EBIRD_API_KEY"),
        os.environ.get("EBIRD_API_TOKEN"),
    ):
        key = _clean_api_key(candidate)
        if key:
            return key

    if not _streamlit_runtime_active():
        return None

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
    if not _streamlit_runtime_active():
        return
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


class MissingEbirdApiKey(ValueError):
    """Raised when an eBird HTTP call is attempted without an API key."""

    def __init__(self) -> None:
        super().__init__(
            "Missing eBird API key. Set EBIRD_API_KEY in .env or "
            ".streamlit/secrets.toml, or enter it when prompted. "
            "Get a key at https://ebird.org/api/keygen"
        )


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
    """Persist hotspots for a region to disk."""
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


def sort_hotspots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order hotspots by all-time species, then checklist count."""
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("numSpeciesAllTime") or 0),
            int(row.get("numChecklistsAllTime") or 0),
            str(row.get("locName") or ""),
        ),
        reverse=True,
    )


def merge_hotspot_lists(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge hotspot rows by locId. Incoming updates existing rows.

    Returns ``(merged, newly_added)``.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for row in existing:
        loc_id = str(row.get("locId") or "").strip()
        if loc_id:
            by_id[loc_id] = row
    added: list[dict[str, Any]] = []
    for row in incoming:
        loc_id = str(row.get("locId") or "").strip()
        if not loc_id:
            continue
        if loc_id not in by_id:
            added.append(row)
        by_id[loc_id] = row
    return sort_hotspots(list(by_id.values())), added


def _empty_taxonomy_cache() -> dict[str, Any]:
    return {
        "cache_version": TAXONOMY_CACHE_VERSION,
        "complete": False,
        "taxa": {},
    }


def load_taxonomy_cache() -> dict[str, Any]:
    """Load the on-disk eBird taxonomy cache."""
    cache = _load_json_file(TAXONOMY_CACHE_PATH)
    if cache.get("cache_version") != TAXONOMY_CACHE_VERSION:
        return _empty_taxonomy_cache()
    taxa = cache.get("taxa")
    if not isinstance(taxa, dict):
        cache["taxa"] = {}
    return cache


def load_cached_taxa(species_codes: list[str]) -> dict[str, dict[str, Any]]:
    """Return cached taxonomy rows for the requested species codes."""
    taxa = load_taxonomy_cache().get("taxa") or {}
    found: dict[str, dict[str, Any]] = {}
    for code in species_codes:
        row = taxa.get(code)
        if isinstance(row, dict) and row:
            found[code] = row
    return found


def load_complete_taxonomy_rows() -> list[dict[str, Any]]:
    """Return every cached taxonomy row when the full dump has been stored."""
    cache = load_taxonomy_cache()
    if not cache.get("complete"):
        return []
    taxa = cache.get("taxa") or {}
    if not isinstance(taxa, dict) or not taxa:
        return []
    return [row for row in taxa.values() if isinstance(row, dict)]


def save_cached_taxa(
    rows: dict[str, dict[str, Any]],
    *,
    complete: bool | None = None,
) -> None:
    """Merge taxonomy rows into the on-disk cache."""
    if not rows and complete is not True:
        return
    cache = load_taxonomy_cache()
    taxa = cache.get("taxa")
    if not isinstance(taxa, dict):
        taxa = {}
    for code, row in rows.items():
        if code and isinstance(row, dict):
            taxa[str(code)] = row
    cache["cache_version"] = TAXONOMY_CACHE_VERSION
    cache["taxa"] = taxa
    cache["count"] = len(taxa)
    cache["updated_at"] = datetime.now().astimezone().isoformat()
    if complete is True:
        cache["complete"] = True
    elif complete is False:
        cache["complete"] = False
    try:
        TAXONOMY_CACHE_PATH.write_text(
            json.dumps(cache, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _taxonomy_rows_by_code(rows: object) -> dict[str, dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return by_code
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("speciesCode") or "").strip()
        if code:
            by_code[code] = row
    return by_code


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


CHECKLIST_CACHE_STATUS_VERSION = 3


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
    expected_by_loc: dict[str, set[str]] = {}
    loc_names_from_feed: dict[str, str] = {}
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
                loc_obj = row.get("loc") if isinstance(row.get("loc"), dict) else {}
                loc_id = str(
                    row.get("locId")
                    or row.get("locID")
                    or loc_obj.get("locId")
                    or loc_obj.get("locID")
                    or ""
                ).strip()
                if not loc_id or not checklist_id:
                    continue
                expected_by_loc.setdefault(loc_id, set()).add(checklist_id)
                loc_name = str(
                    row.get("locName")
                    or loc_obj.get("locName")
                    or loc_obj.get("name")
                    or ""
                ).strip()
                if loc_name and loc_id not in loc_names_from_feed:
                    loc_names_from_feed[loc_id] = loc_name
            expected_by_day[day] = len(day_ids)

    downloaded_by_day: dict[str, set[str]] = {}
    downloaded_ids: set[str] = set()
    downloaded_by_loc: dict[str, set[str]] = {}
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
            downloaded_by_day.setdefault(day_key, set()).add(sub_id)
        else:
            downloaded_by_day.setdefault(day_key, set()).add(path.stem)

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
        if sub_id:
            downloaded_by_loc.setdefault(loc_id, set()).add(sub_id)

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

    for loc_id, checklist_ids in expected_by_loc.items():
        hotspot = hotspot_stats.get(loc_id)
        if hotspot is None:
            hotspot_stats[loc_id] = {
                "locId": loc_id,
                "locName": loc_names_from_feed.get(loc_id, ""),
                "checklists": 0,
                "first_day": "",
                "last_day": "",
            }
            hotspot = hotspot_stats[loc_id]
        elif not hotspot.get("locName") and loc_names_from_feed.get(loc_id):
            hotspot["locName"] = loc_names_from_feed[loc_id]
        expected = len(checklist_ids)
        downloaded = len(downloaded_by_loc.get(loc_id, set()) & checklist_ids)
        if not checklist_ids:
            downloaded = len(downloaded_by_loc.get(loc_id, set()))
        hotspot["expected"] = expected
        hotspot["downloaded"] = downloaded
        hotspot["missing"] = max(0, expected - downloaded)
        # Keep checklists as the on-disk count for backwards-compatible sorting.
        hotspot["checklists"] = len(downloaded_by_loc.get(loc_id, set()))

    for loc_id, hotspot in hotspot_stats.items():
        if "expected" in hotspot:
            continue
        downloaded = len(downloaded_by_loc.get(loc_id, set()))
        hotspot["expected"] = 0
        hotspot["downloaded"] = downloaded
        hotspot["missing"] = 0
        hotspot["checklists"] = downloaded

    all_days = sorted(set(expected_by_day) | set(downloaded_by_day))
    truncated_set = set(truncated_dates)
    days = [
        {
            "day": day,
            "expected": int(expected_by_day.get(day, 0)),
            "downloaded": len(downloaded_by_day.get(day, set())),
            "missing": max(
                0,
                int(expected_by_day.get(day, 0))
                - len(downloaded_by_day.get(day, set())),
            ),
            "truncated": day in truncated_set,
        }
        for day in all_days
    ]
    hotspots = sorted(
        hotspot_stats.values(),
        key=lambda row: (
            -int(row.get("missing") or 0),
            -int(row.get("checklists") or 0),
            str(row.get("locName") or ""),
            str(row["locId"]),
        ),
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
        "downloaded_total": len(downloaded_ids) if downloaded_ids else sum(
            len(ids) for ids in downloaded_by_day.values()
        ),
        "days_in_feed": len(expected_by_day),
        "days_with_downloads": sum(1 for ids in downloaded_by_day.values() if ids),
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
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Load downloaded checklist files for a hotspot.

    When ``start_date``/``end_date`` are omitted, every cached checklist for
    the hotspot is returned.
    """
    return load_local_checklists(
        region_code,
        loc_id=loc_id,
        start_date=start_date,
        end_date=end_date,
    )


def load_local_checklists(
    region_code: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    loc_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load downloaded checklist files, optionally filtered by date and hotspot.

    When ``loc_id`` is set, only that hotspot is included; otherwise every
    downloaded checklist in the region for the window is returned. When
    ``start_date`` or ``end_date`` is ``None``, that bound is open.

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
        if obs_day is None:
            continue
        if start_date is not None and obs_day < start_date:
            continue
        if end_date is not None and obs_day > end_date:
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
            "_obs_day": obs_day.isoformat(),
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


def build_local_last_seen_index(
    region_code: str,
    *,
    rebuild: bool = False,
) -> dict[str, dict[str, Any]]:
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
        not rebuild
        and existing.get("signature") == signature
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

    previous_files = int((existing.get("signature") or {}).get("file_count") or 0)
    previous_by_code = existing.get("by_code")
    if previous_files > len(files) and isinstance(previous_by_code, dict):
        # Deploy/subset trees have fewer checklist files than the index was
        # built from; keep last-seen rows that this disk scan cannot see.
        for code, row in previous_by_code.items():
            if not isinstance(row, dict):
                continue
            key = str(code)
            current = by_code.get(key)
            if current is None:
                by_code[key] = row
            elif str(row.get("obsDt") or "") > str(current.get("obsDt") or ""):
                by_code[key] = row

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


def list_local_checklist_regions() -> list[str]:
    """Region codes that have downloaded checklist files on disk."""
    if not CHECKLISTS_DIR.exists():
        return []
    regions: list[str] = []
    for path in sorted(CHECKLISTS_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if any(path.rglob("S*.json")):
            regions.append(path.name)
    return regions


def rebuild_local_last_seen_indexes(
    region_codes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild per-region last-seen indexes from on-disk checklist details."""
    codes = [code.strip() for code in (region_codes or list_local_checklist_regions()) if code.strip()]
    results: list[dict[str, Any]] = []
    for code in codes:
        by_code = build_local_last_seen_index(code, rebuild=True)
        path = local_checklist_index_path(code)
        results.append(
            {
                "region_code": code,
                "species": len(by_code),
                "path": str(path.name),
            }
        )
    return results


def _retry_after_seconds(response: requests.Response) -> float:
    """Seconds to wait from a 429 Retry-After header (seconds or HTTP-date)."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return 60.0
    try:
        return max(float(raw), MIN_RATE_LIMIT_WAIT_SECONDS)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.now().astimezone().tzinfo)
        delay = (when - datetime.now(when.tzinfo)).total_seconds()
        return max(delay, MIN_RATE_LIMIT_WAIT_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return 60.0


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
    if not _streamlit_runtime_active():
        return
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


_TAXONOMY_NAME_INDEX: dict[str, str] | None = None


def _taxonomy_code_for_name(
    *,
    scientific_name: str = "",
    common_name: str = "",
) -> str | None:
    """Map a scientific or common name to an eBird code from the local taxonomy dump."""
    global _TAXONOMY_NAME_INDEX
    if _TAXONOMY_NAME_INDEX is None:
        index: dict[str, str] = {}
        for row in load_complete_taxonomy_rows():
            code = str(row.get("speciesCode") or "").strip()
            if not code:
                continue
            sci_name = str(row.get("sciName") or "").strip().casefold()
            com_name = str(row.get("comName") or "").strip().casefold()
            if sci_name:
                index.setdefault(f"sci:{sci_name}", code)
            if com_name:
                index.setdefault(f"common:{com_name}", code)
        _TAXONOMY_NAME_INDEX = index
    sci = scientific_name.strip().casefold()
    common = common_name.strip().casefold()
    if sci:
        code = _TAXONOMY_NAME_INDEX.get(f"sci:{sci}")
        if code:
            return code
    if common:
        return _TAXONOMY_NAME_INDEX.get(f"common:{common}")
    return None


def resolve_ebird_code(
    *,
    scientific_name: str | None = None,
    common_name: str | None = None,
    local_only: bool = False,
) -> str | None:
    """Resolve an eBird species code from local caches, then BirdNET if needed."""
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
    local_code = _taxonomy_code_for_name(scientific_name=sci, common_name=common)
    if local_code:
        return local_code
    if local_only:
        return None
    url = f"https://birdnet.cornell.edu/taxonomy/api/species/{quote(lookup, safe='')}"
    started = time.perf_counter()
    log_api_send(
        "birdnet",
        "resolve eBird code",
        url=url,
        scientific_name=sci or None,
        common_name=common or None,
        lookup=lookup,
    )
    try:
        response = requests.get(url, timeout=20)
        if response.status_code == 404:
            cache[cache_key] = ""
            _save_json_file(BIRDNET_CODE_CACHE_PATH, cache)
            log_api_done(
                "birdnet",
                "resolve eBird code",
                started=started,
                status=404,
                lookup=lookup,
            )
            return None
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        log_api_done(
            "birdnet",
            "resolve eBird code",
            started=started,
            status=None,
            lookup=lookup,
            error=exc.__class__.__name__,
        )
        return None
    code = str((payload or {}).get("ebird_code") or "").strip()
    cache[cache_key] = code
    if sci and common:
        cache[f"common:{common.casefold()}"] = code
    _save_json_file(BIRDNET_CODE_CACHE_PATH, cache)
    log_api_done(
        "birdnet",
        "resolve eBird code",
        started=started,
        status=response.status_code,
        lookup=lookup,
        output=payload,
        ebird_code=code or None,
    )
    return code or None


def _ebird_request_summary(path: str, params: dict[str, Any] | None) -> str:
    """Short human-readable label for an eBird API path."""
    text = path or ""
    if text.startswith("/product/checklist/view/"):
        return f"checklist detail subId={text.rsplit('/', 1)[-1]}"
    if text.startswith("/product/spplist/"):
        return f"region species list region={text.rsplit('/', 1)[-1]}"
    if text.startswith("/ref/hotspot/"):
        return f"hotspots region={text.rsplit('/', 1)[-1]}"
    if text.startswith("/ref/region/list/"):
        parts = text.strip("/").split("/")
        kind = parts[3] if len(parts) > 3 else "?"
        parent = parts[4] if len(parts) > 4 else "?"
        return f"region list type={kind} parent={parent}"
    if text.startswith("/ref/region/info/"):
        return f"region info code={text.rsplit('/', 1)[-1]}"
    if text.startswith("/ref/taxonomy/ebird"):
        species = (params or {}).get("species")
        if species:
            return f"taxonomy species={species}"
        return "taxonomy"
    if "/data/obs/" in text and "/recent" in text:
        return f"recent observations {text}"
    if text.startswith("/product/lists/"):
        parts = text.strip("/").split("/")
        # /product/lists/{loc} or /product/lists/{loc}/{y}/{m}/{d}
        loc = parts[2] if len(parts) > 2 else "?"
        if len(parts) >= 6:
            return f"checklists loc={loc} date={parts[3]}-{parts[4].zfill(2)}-{parts[5].zfill(2)}"
        return f"recent checklists loc={loc}"
    return text


def load_disk_region_species_codes(region_code: str) -> list[str] | None:
    """Return cached regional species codes without calling the eBird API.

    ``None`` means this region has no disk entry yet.
    """
    region = (region_code or "").strip()
    if not region:
        return None
    cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
    cached = cache.get(region)
    if not isinstance(cached, list):
        return None
    return [str(item) for item in cached if item]


def region_historical_species_cache_coverage(region_code: str) -> dict[str, Any]:
    """Coverage of a region's historical species list against local caches.

    Historical list = eBird ``/product/spplist`` codes stored in
    ``ebird_region_species_cache.json``.
    """
    from inaturalist import GALLERY_CACHE_VERSION

    region = (region_code or "").strip()
    historical_list = load_disk_region_species_codes(region)
    historical_set = set(historical_list or [])
    local_codes = set(build_local_last_seen_index(region)) if region else set()
    in_checklists = historical_set & local_codes

    photo_cache = _load_json_file(ROOT / "inaturalist_cache.json")
    in_photos = {
        code
        for code in historical_set
        if isinstance(photo_cache.get(code), dict) and photo_cache.get(code)
    }

    gallery_cache = _load_json_file(ROOT / "inaturalist_gallery_cache.json")
    gallery_by_code: dict[str, dict[str, Any]] = {}
    for key, value in gallery_cache.items():
        if not isinstance(value, dict) or not value:
            continue
        if key in historical_set:
            gallery_by_code.setdefault(str(key), value)
        nested_code = str(value.get("ebird_code") or "").strip()
        if nested_code and nested_code in historical_set:
            gallery_by_code.setdefault(nested_code, value)
    in_gallery = {
        code
        for code, entry in gallery_by_code.items()
        if entry.get("cache_version") == GALLERY_CACHE_VERSION
    }

    similar_cache = _load_json_file(ROOT / "inaturalist_similar_cache.json")
    similar_codes: set[str] = set()
    similar_taxon_ids: set[str] = set()
    for key in similar_cache:
        text = str(key)
        if text.startswith("code:"):
            similar_codes.add(text[5:].split("|", 1)[0])
        elif text.startswith("taxon:"):
            similar_taxon_ids.add(text[6:])
    in_similar: set[str] = set()
    for code in historical_set:
        if code in similar_codes:
            in_similar.add(code)
            continue
        entry = gallery_by_code.get(code) or {}
        taxon_id = entry.get("taxon_id")
        if taxon_id is not None and str(taxon_id) in similar_taxon_ids:
            in_similar.add(code)

    total = len(historical_set)
    checklist_count = len(in_checklists)
    photo_count = len(in_photos)
    gallery_count = len(in_gallery)
    similar_count = len(in_similar)
    return {
        "region_code": region,
        "historical_total": total,
        "has_historical_list": historical_list is not None,
        "in_checklist_cache": checklist_count,
        "checklist_pct": (100.0 * checklist_count / total) if total else None,
        "in_photo_cache": photo_count,
        "photo_pct": (100.0 * photo_count / total) if total else None,
        "in_gallery_cache": gallery_count,
        "gallery_pct": (100.0 * gallery_count / total) if total else None,
        "in_similar_cache": similar_count,
        "similar_pct": (100.0 * similar_count / total) if total else None,
    }


class EBirdClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        min_rate_limit_wait_seconds: float = MIN_RATE_LIMIT_WAIT_SECONDS,
    ) -> None:
        self.api_key = _clean_api_key(api_key) or get_api_key()
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"X-eBirdApiToken": self.api_key})
        self.rate_limit_events: list[dict[str, Any]] = []
        self.http_429_count = 0
        self.wait_count = 0
        self.wait_seconds_total = 0.0
        self.min_rate_limit_wait_seconds = max(
            float(min_rate_limit_wait_seconds),
            MIN_RATE_LIMIT_WAIT_SECONDS,
        )
        # Extra pause before the next request after recovering from a 429.
        self._post_rate_limit_cooldown_seconds = 0.0

    def _require_api_key(self) -> str:
        """Return a usable API key, refreshing from env/session if needed."""
        key = _clean_api_key(self.api_key) or get_api_key()
        if not key:
            raise MissingEbirdApiKey()
        if key != self.api_key:
            self.api_key = key
            self.session.headers.update({"X-eBirdApiToken": key})
        return key

    def _rate_limit_wait(self, seconds: float, *, path: str) -> None:
        wait_seconds = max(float(seconds), 0.0)
        if wait_seconds <= 0:
            return
        self.wait_count += 1
        self.wait_seconds_total += wait_seconds
        note_rate_limit(wait_seconds, path=path)
        time.sleep(wait_seconds)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._require_api_key()
        started = time.perf_counter()
        url = f"{BASE_URL}{path}"
        summary = _ebird_request_summary(path, params)
        cooldown = float(self._post_rate_limit_cooldown_seconds or 0.0)
        if cooldown > 0:
            print(
                f"[rate-limit] post-429 cooldown {cooldown:.0f}s before {path}",
                flush=True,
            )
            self._rate_limit_wait(cooldown, path=f"{path} (post-429 cooldown)")
            self._post_rate_limit_cooldown_seconds = 0.0
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            log_api_send(
                "ebird",
                summary,
                url=url,
                params=params,
                attempt=attempt + 1 if attempt else None,
            )
            response = self.session.get(
                url,
                params=params,
                timeout=30,
            )
            if response.status_code == 429:
                header_wait = _retry_after_seconds(response)
                # Honor Retry-After; only apply the client floor when the header
                # is missing/unusable (header helper already returns >= 1s).
                raw_retry = (response.headers.get("Retry-After") or "").strip()
                base_wait = (
                    max(header_wait, self.min_rate_limit_wait_seconds)
                    if not raw_retry
                    else header_wait
                )
                # Wait twice the recommended interval, then apply the same
                # doubled pause before the next request after this one succeeds.
                wait_seconds = base_wait * 2.0
                event = {
                    "seconds": wait_seconds,
                    "path": path,
                    "attempt": attempt + 1,
                    "retry_after": raw_retry or None,
                    "retry_after_seconds": base_wait,
                    "multiplier": 2.0,
                }
                self.rate_limit_events.append(event)
                self.http_429_count += 1
                self._post_rate_limit_cooldown_seconds = wait_seconds
                log_api_done(
                    "ebird",
                    summary,
                    started=started,
                    status=429,
                    attempt=attempt + 1,
                    retry_after_s=f"{wait_seconds:.0f}",
                    output=response.text,
                )
                self._rate_limit_wait(wait_seconds, path=path)
                continue
            response.raise_for_status()
            data = response.json()
            log_api_done(
                "ebird",
                summary,
                started=started,
                status=response.status_code,
                output=data,
                **(params or {}),
            )
            return data
        raise requests.HTTPError(
            f"eBird rate limit retries exhausted for {path} "
            f"after {MAX_RATE_LIMIT_RETRIES} attempts"
        )

    def taxonomy(self, species: str | None = None, *, use_cache: bool = True) -> Any:
        params: dict[str, Any] = {"fmt": "json"}
        if species:
            params["species"] = species
        elif use_cache:
            cached = load_complete_taxonomy_rows()
            if cached:
                return cached
        rows = self.get("/ref/taxonomy/ebird", params=params)
        if isinstance(rows, list):
            save_cached_taxa(
                _taxonomy_rows_by_code(rows),
                complete=True if not species else None,
            )
        return rows

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
        taxa = load_cached_taxa(codes)
        missing = [code for code in codes if code not in taxa]
        if not missing:
            return taxa
        batch_size = 50
        fetched: dict[str, dict[str, Any]] = {}
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            rows = self.taxonomy(species=",".join(batch), use_cache=False)
            fetched.update(_taxonomy_rows_by_code(rows))
        still_missing = [code for code in missing if code not in fetched]
        for code in still_missing:
            rows = self.taxonomy(species=code, use_cache=False)
            fetched.update(_taxonomy_rows_by_code(rows))
        if fetched:
            save_cached_taxa(fetched)
            taxa.update(fetched)
        return {code: taxa[code] for code in codes if code in taxa}

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
        cached = load_disk_region_species_codes(code)
        if cached is not None:
            return set(cached)
        codes = self.region_species_codes(code)
        cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
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

        cache = _load_json_file(LAST_SEEN_CACHE_PATH)
        cache_key = f"{region}|{code}|{back}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "fetched_at" in cached:
            return cached.get("observation") or None

        if not allow_api:
            return None

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
        delay_seconds: float = 0.0,
        on_progress: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        """Persist daily checklist feeds for a region through today.

        eBird returns at most ``max_results`` entries per date. Dates that hit
        that limit are retained but marked as truncated so consumers do not
        mistake the cache for a complete daily record.

        Missing days (including whole prior years) are fetched. Already-cached
        historical days are skipped; today is always refreshed.
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

        pending_days: list[date] = []
        day = first_day
        while day <= last_day:
            day_key = day.isoformat()
            # Historical dates do not change; always refresh today.
            if day_key not in daily or day == today:
                pending_days.append(day)
            day = date.fromordinal(day.toordinal() + 1)

        total = len(pending_days)
        stopped = False
        for index, day in enumerate(pending_days, start=1):
            if callable(should_stop) and should_stop():
                stopped = True
                break
            day_key = day.isoformat()
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
            if callable(on_progress):
                on_progress(
                    {
                        "processed": index,
                        "total": total,
                        "remaining": total - index,
                        "last_day": day_key,
                        "days_in_feed": len(daily),
                    }
                )
            if delay_seconds > 0 and index < total:
                time.sleep(delay_seconds)

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
        cache["status"] = "stopped" if stopped else "complete"
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
                return cached[:limit] if limit else cached
        rows = sort_hotspots(self.hotspots(code, back=back))
        if limit:
            rows = rows[:limit]
        if use_cache and rows:
            save_cached_hotspots(code, rows)
        return rows

    def additional_hotspots(
        self,
        region_code: str,
        existing: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch the region's hotspots and add any that are not already cached.

        Returns ``(merged_list, newly_added)``.
        """
        code = (region_code or "").strip()
        current = (
            list(existing)
            if existing is not None
            else load_cached_hotspots(code)
        )
        incoming = self.hotspots(code)
        merged, added = merge_hotspot_lists(current, incoming)
        if merged:
            save_cached_hotspots(code, merged)
        return merged, added

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
