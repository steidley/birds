"""eBird API 2.0 client and configuration."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from api_log import log_api_done, log_api_send

BASE_URL = "https://api.ebird.org/v2"
ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"
REQUIRED_DATA_DIR = ROOT / "requiredData"
CACHE_DIR = ROOT / "cache"
CACHE_SHARED_DIR = CACHE_DIR / "shared"
CHECKLIST_CACHE_VERSION = 1
REGION_SPECIES_CACHE_PATH = CACHE_SHARED_DIR / "ebird_region_species_cache.json"
LAST_SEEN_CACHE_PATH = CACHE_SHARED_DIR / "ebird_last_seen_cache.json"
OWN_RECENT_SIGHTINGS_PATH = CACHE_SHARED_DIR / "ebird_own_recent_sightings.json"
BIRDNET_CODE_CACHE_PATH = CACHE_SHARED_DIR / "birdnet_code_cache.json"
REGION_LIST_CACHE_PATH = CACHE_SHARED_DIR / "ebird_region_list_cache.json"
TAXONOMY_CACHE_PATH = CACHE_SHARED_DIR / "ebird_taxonomy_cache.json"
RECENT_REGIONS_PATH = CACHE_SHARED_DIR / "ebird_recent_regions.json"
RECENT_OBS_CACHE_DIR = CACHE_SHARED_DIR / "ebird_recent_obs"
HOTSPOT_CACHE_VERSION = 1
REGION_LIST_CACHE_VERSION = 1
TAXONOMY_CACHE_VERSION = 1
RECENT_OBS_CACHE_VERSION = 1
# Recent-obs feeds change slowly enough that a multi-hour TTL saves many
# hotspot-finder / last-seen API calls without feeling badly stale.
RECENT_OBS_CACHE_TTL_SECONDS = 3 * 60 * 60
MAX_RATE_LIMIT_RETRIES = 8
MIN_RATE_LIMIT_WAIT_SECONDS = 1.0
# Stay under eBird’s practical ~60/min ceiling, including Show checklists.
MAX_CALLS_PER_MINUTE = 37.5
MIN_REQUEST_INTERVAL_SECONDS = 60.0 / MAX_CALLS_PER_MINUTE
EBIRD_THROTTLE_PATH = CACHE_SHARED_DIR / ".ebird_api_throttle"

_ebird_throttle_lock = threading.Lock()

load_dotenv(CONFIG_DIR / ".env")
load_dotenv(ROOT / ".env")  # legacy root .env if present


def _safe_region_component(region_code: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in (region_code or "").strip()
    )


def cache_region_dir(region_code: str) -> Path:
    """``cache/<region>/`` for region-scoped cache files."""
    code = _safe_region_component(region_code)
    if not code or code.casefold() in {"shared", "_shared"}:
        raise ValueError(f"Invalid cache region code: {region_code!r}")
    return CACHE_DIR / code


def region_checklists_dir(region_code: str) -> Path:
    """Downloaded checklist detail files for one region."""
    return cache_region_dir(region_code) / "checklists"


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


def _config_text(name: str) -> str:
    """Read a non-secret config string from env, then Streamlit secrets."""
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    if not _streamlit_runtime_active():
        return ""
    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is not None and name in secrets:
            return str(secrets[name] or "").strip()
    except Exception:
        pass
    return ""


def configured_observer_names() -> list[str]:
    """Display names that identify the user's own eBird checklists.

    ``EBIRD_USER_DISPLAY_NAME`` is a comma-separated list. Use the public
    name shown on checklists (not the profile handle), e.g.
    ``Adam and Anastasiya Steidley``.
    """
    raw = _config_text("EBIRD_USER_DISPLAY_NAME")
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        folded = name.casefold()
        if not name or folded in seen:
            continue
        seen.add(folded)
        names.append(name)
    return names


def observer_name_matches(display_name: str, names: list[str] | None = None) -> bool:
    """True when ``display_name`` matches a configured observer name."""
    needle = (display_name or "").strip().casefold()
    if not needle:
        return False
    configured = names if names is not None else configured_observer_names()
    return any(needle == name.strip().casefold() for name in configured)


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
            "Missing eBird API key. Set EBIRD_API_KEY in config/.env or "
            "config/streamlit/secrets.toml (also via .streamlit symlink), "
            "or enter it when prompted. "
            "Get a key at https://ebird.org/api/keygen"
        )


def region_year_checklist_cache_path(region_code: str, year: int) -> Path:
    """Location of the on-disk regional daily-checklist cache."""
    return cache_region_dir(region_code) / f"checklists_{int(year)}.json"


def _feed_row_loc_id(row: dict[str, Any]) -> str:
    return str(row.get("locId") or row.get("locID") or "").strip()


def load_region_year_feed_cache(region_code: str, year: int) -> dict[str, Any]:
    """Load the regional daily-feed JSON, or an empty cache dict."""
    code = (region_code or "").strip()
    path = region_year_checklist_cache_path(code, year)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if (
        isinstance(loaded, dict)
        and loaded.get("cache_version") == CHECKLIST_CACHE_VERSION
        and loaded.get("region_code") == code
        and loaded.get("year") == year
    ):
        return loaded
    return {}


def persist_region_feed_day(
    region_code: str,
    day: date,
    rows: list[Any],
    *,
    max_results: int = 200,
) -> None:
    """Write one day's regional checklist feed into the year cache file."""
    code = (region_code or "").strip()
    if not code:
        return
    year = day.year
    path = region_year_checklist_cache_path(code, year)
    cache = load_region_year_feed_cache(code, year)
    daily = cache.get("daily")
    if not isinstance(daily, dict):
        daily = {}
    row_list = [row for row in rows if isinstance(row, dict)]
    daily[day.isoformat()] = {
        "checklists": row_list,
        "truncated": len(row_list) >= max_results,
    }
    checklist_ids: set[str] = set()
    for entry in daily.values():
        if not isinstance(entry, dict):
            continue
        for row in entry.get("checklists") or []:
            if not isinstance(row, dict):
                continue
            checklist_id = str(row.get("subId") or row.get("subID") or "").strip()
            if checklist_id:
                checklist_ids.add(checklist_id)
    cache.update(
        {
            "cache_version": CHECKLIST_CACHE_VERSION,
            "region_code": code,
            "year": year,
            "max_results_per_day": max_results,
            "daily": daily,
            "unique_checklist_count": len(checklist_ids),
            "truncated_dates": sorted(
                day_key
                for day_key, entry in daily.items()
                if isinstance(entry, dict) and entry.get("truncated")
            ),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
    )
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def local_checklist_index_path(region_code: str) -> Path:
    return cache_region_dir(region_code) / "local_last_seen.json"


def local_year_first_index_path(region_code: str, year: int) -> Path:
    return cache_region_dir(region_code) / f"year_first_{int(year)}.json"


def hotspots_cache_path(region_code: str) -> Path:
    return cache_region_dir(region_code) / "hotspots.json"


def checklist_cache_status_path(region_code: str, year: int) -> Path:
    return cache_region_dir(region_code) / f"checklist_cache_status_{int(year)}.json"


def _recent_obs_cache_key(
    region_code: str,
    species_code: str | None,
    *,
    back: int,
    max_results: int | None,
    hotspot: bool,
) -> str:
    return "|".join(
        [
            str(region_code or "").strip(),
            str(species_code or "").strip(),
            str(int(back)),
            "" if max_results is None else str(int(max_results)),
            "1" if hotspot else "0",
        ]
    )


def recent_obs_cache_path(
    region_code: str,
    species_code: str | None = None,
    *,
    back: int = 14,
    max_results: int | None = 10,
    hotspot: bool = False,
) -> Path:
    """On-disk path for one recent-observations query."""
    key = _recent_obs_cache_key(
        region_code,
        species_code,
        back=back,
        max_results=max_results,
        hotspot=hotspot,
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    label = _safe_region_component(region_code) or "unknown"
    species = _safe_region_component(species_code or "")
    name = f"{label}_{species}_{digest}.json" if species else f"{label}_{digest}.json"
    return RECENT_OBS_CACHE_DIR / name


def load_cached_recent_observations(
    region_code: str,
    species_code: str | None = None,
    *,
    back: int = 14,
    max_results: int | None = 10,
    hotspot: bool = False,
    ttl_seconds: int = RECENT_OBS_CACHE_TTL_SECONDS,
) -> Any | None:
    """Return cached recent-obs payload if present and fresh, else ``None``."""
    path = recent_obs_cache_path(
        region_code,
        species_code,
        back=back,
        max_results=max_results,
        hotspot=hotspot,
    )
    data = _load_json_file(path)
    if data.get("cache_version") != RECENT_OBS_CACHE_VERSION:
        return None
    if data.get("cache_key") != _recent_obs_cache_key(
        region_code,
        species_code,
        back=back,
        max_results=max_results,
        hotspot=hotspot,
    ):
        return None
    fetched_raw = str(data.get("fetched_at") or "").strip()
    if not fetched_raw:
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.astimezone()
    age = (datetime.now().astimezone() - fetched_at).total_seconds()
    if age < 0 or age > max(0, int(ttl_seconds)):
        return None
    return data.get("observations")


def save_cached_recent_observations(
    region_code: str,
    observations: Any,
    species_code: str | None = None,
    *,
    back: int = 14,
    max_results: int | None = 10,
    hotspot: bool = False,
) -> Path:
    """Persist a recent-observations API response for later reuse."""
    path = recent_obs_cache_path(
        region_code,
        species_code,
        back=back,
        max_results=max_results,
        hotspot=hotspot,
    )
    payload = {
        "cache_version": RECENT_OBS_CACHE_VERSION,
        "cache_key": _recent_obs_cache_key(
            region_code,
            species_code,
            back=back,
            max_results=max_results,
            hotspot=hotspot,
        ),
        "region_code": str(region_code or "").strip(),
        "species_code": str(species_code or "").strip() or None,
        "back": int(back),
        "max_results": None if max_results is None else int(max_results),
        "hotspot": bool(hotspot),
        "fetched_at": datetime.now().astimezone().isoformat(),
        "observations": observations,
    }
    _save_json_file(path, payload)
    return path


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


def hotspot_in_region(row: dict[str, Any], region_code: str) -> bool:
    """True when a hotspot belongs to ``region_code`` (country, state, or county)."""
    wanted = (region_code or "").strip()
    if not wanted or wanted.casefold() == "world":
        return True
    codes = [
        str(row.get(key) or "").strip()
        for key in ("subnational2Code", "subnational1Code", "countryCode")
    ]
    codes = [code for code in codes if code]
    if not codes:
        return True
    return any(code == wanted or code.startswith(wanted + "-") for code in codes)


def filter_hotspots_for_region(
    rows: list[dict[str, Any]],
    region_code: str,
) -> list[dict[str, Any]]:
    """Keep only hotspots in ``region_code``."""
    return [
        row
        for row in rows
        if isinstance(row, dict) and hotspot_in_region(row, region_code)
    ]


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
    if not isinstance(rows, list):
        return []
    return filter_hotspots_for_region(rows, code)


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
            "hotspots": filter_hotspots_for_region(hotspots, code),
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


def build_checklist_cache_status(
    region_code: str,
    year: int,
    *,
    force_refresh: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Summarize downloaded checklist coverage by day and hotspot.

    Compares on-disk checklist detail files under ``cache/<region>/checklists``
    against the regional daily feed cache when present. Results are cached on
    disk for full-year scans and rebuilt when the file set or feed cache changes.

    Optional ``start_date`` / ``end_date`` limit the summary to that inclusive
    window (still within ``year``). Windowed results are not written to the
    year status file.
    """
    region = (region_code or "").strip()
    year_start = date(int(year), 1, 1)
    year_end = date(int(year), 12, 31)
    window_start = start_date if start_date is not None else year_start
    window_end = end_date if end_date is not None else year_end
    if window_start > window_end:
        window_start, window_end = window_end, window_start
    window_start = max(window_start, year_start)
    window_end = min(window_end, year_end)
    windowed = start_date is not None or end_date is not None

    if not region:
        return {
            "region_code": "",
            "year": year,
            "start_date": window_start.isoformat(),
            "end_date": window_end.isoformat(),
            "days": [],
            "hotspots": [],
            "downloaded_total": 0,
            "expected_total": 0,
            "days_with_downloads": 0,
            "hotspot_count": 0,
            "truncated_dates": [],
            "feed_cache_exists": False,
        }

    root = region_checklists_dir(region)
    feed_path = region_year_checklist_cache_path(region, year)
    files = sorted(root.rglob("S*.json")) if root.exists() else []
    signature = {
        "version": CHECKLIST_CACHE_STATUS_VERSION,
        "file_count": len(files),
        "newest_mtime": max((path.stat().st_mtime for path in files), default=0.0),
        "feed_mtime": feed_path.stat().st_mtime if feed_path.exists() else 0.0,
        "feed_path": str(feed_path),
        "start_date": window_start.isoformat() if windowed else "",
        "end_date": window_end.isoformat() if windowed else "",
    }
    status_path = checklist_cache_status_path(region, year)
    if not force_refresh and not windowed:
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
            and _day_in_window(str(day), window_start, window_end)
        ]
        for day_key, entry in (feed.get("daily") or {}).items():
            day = str(day_key)
            if not day.startswith(year_prefix) or not isinstance(entry, dict):
                continue
            if not _day_in_window(day, window_start, window_end):
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
        if obs_day < window_start or obs_day > window_end:
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
        "start_date": window_start.isoformat(),
        "end_date": window_end.isoformat(),
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
    if not windowed:
        _save_json_file(status_path, result)
    return result


def _day_in_window(day_iso: str, start: date, end: date) -> bool:
    try:
        day = date.fromisoformat(str(day_iso)[:10])
    except ValueError:
        return False
    return start <= day <= end


def build_checklist_cache_status_window(
    region_code: str,
    start: date,
    end: date,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Coverage summary for an inclusive date window (may span years)."""
    if start > end:
        start, end = end, start
    years = list(range(int(start.year), int(end.year) + 1))
    if len(years) == 1:
        return build_checklist_cache_status(
            region_code,
            years[0],
            force_refresh=force_refresh,
            start_date=start,
            end_date=end,
        )

    merged_days: list[dict[str, Any]] = []
    hotspot_by_id: dict[str, dict[str, Any]] = {}
    truncated: list[str] = []
    feed_exists = False
    feed_paths: list[str] = []
    updated_at = ""
    for year in years:
        year_start = max(start, date(year, 1, 1))
        year_end = min(end, date(year, 12, 31))
        part = build_checklist_cache_status(
            region_code,
            year,
            force_refresh=force_refresh,
            start_date=year_start,
            end_date=year_end,
        )
        merged_days.extend(part.get("days") or [])
        truncated.extend(part.get("truncated_dates") or [])
        feed_exists = feed_exists or bool(part.get("feed_cache_exists"))
        path = str(part.get("feed_cache_path") or "").strip()
        if path:
            feed_paths.append(path)
        updated_at = str(part.get("updated_at") or updated_at)
        for row in part.get("hotspots") or []:
            loc_id = str(row.get("locId") or "").strip()
            if not loc_id:
                continue
            existing = hotspot_by_id.get(loc_id)
            if existing is None:
                hotspot_by_id[loc_id] = dict(row)
                continue
            existing["expected"] = int(existing.get("expected") or 0) + int(
                row.get("expected") or 0
            )
            existing["downloaded"] = int(existing.get("downloaded") or 0) + int(
                row.get("downloaded") or 0
            )
            existing["missing"] = int(existing.get("missing") or 0) + int(
                row.get("missing") or 0
            )
            existing["checklists"] = int(existing.get("checklists") or 0) + int(
                row.get("checklists") or 0
            )
            if row.get("locName") and not existing.get("locName"):
                existing["locName"] = row.get("locName")
            first = str(row.get("first_day") or "")
            last = str(row.get("last_day") or "")
            if first and (
                not existing.get("first_day") or first < str(existing.get("first_day"))
            ):
                existing["first_day"] = first
            if last and (
                not existing.get("last_day") or last > str(existing.get("last_day"))
            ):
                existing["last_day"] = last

    hotspots = sorted(
        hotspot_by_id.values(),
        key=lambda row: (
            -int(row.get("missing") or 0),
            -int(row.get("checklists") or 0),
            str(row.get("locName") or ""),
            str(row.get("locId") or ""),
        ),
    )
    expected_total = sum(int(row.get("expected") or 0) for row in merged_days)
    downloaded_total = sum(int(row.get("downloaded") or 0) for row in merged_days)
    return {
        "cache_version": CHECKLIST_CACHE_STATUS_VERSION,
        "region_code": str(region_code or "").strip(),
        "year": int(end.year),
        "years": years,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "updated_at": updated_at,
        "feed_cache_exists": feed_exists,
        "feed_cache_path": feed_paths[-1] if feed_paths else "",
        "feed_cache_paths": feed_paths,
        "expected_total": expected_total,
        "downloaded_total": downloaded_total,
        "days_in_feed": sum(1 for row in merged_days if int(row.get("expected") or 0)),
        "days_with_downloads": sum(
            1 for row in merged_days if int(row.get("downloaded") or 0)
        ),
        "hotspot_count": len(hotspots),
        "truncated_dates": truncated,
        "days": sorted(merged_days, key=lambda row: str(row.get("day") or "")),
        "hotspots": hotspots,
    }


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
    disk_only: bool = False,
) -> list[dict[str, Any]]:
    """Load downloaded checklist files, optionally filtered by date and hotspot.

    When ``loc_id`` is set, only that hotspot is included; otherwise every
    downloaded checklist in the region for the window is returned. When
    ``start_date`` or ``end_date`` is ``None``, that bound is open.

    When ``disk_only`` is True, only JSON files under the region checklist
    cache are returned (My eBird CSV summaries are omitted).

    Returns feed-summary-shaped rows with ``_detail`` set to the full checklist
    payload so callers can enrich without another API call.
    """
    region = (region_code or "").strip()
    location = (loc_id or "").strip() or None
    if not region:
        return []
    root = region_checklists_dir(region)
    found: dict[str, dict[str, Any]] = {}
    paths = sorted(root.rglob("S*.json")) if root.exists() else []
    for path in paths:
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
            "regionCode": region,
            "_detail": checklist,
            "_source": "local_cache",
            "_path": str(path),
            "_obs_day": obs_day.isoformat(),
            "_region": region,
        }
        previous = found.get(sub_id)
        if previous is None or str(summary["isoObsDate"]) >= str(
            previous.get("isoObsDate") or ""
        ):
            found[sub_id] = summary

    if not disk_only:
        from my_ebird_data import my_ebird_checklist_summaries_for

        for extra in my_ebird_checklist_summaries_for(
            region,
            loc_id=location,
            start_date=start_date,
            end_date=end_date,
        ):
            extra_id = str(extra.get("subId") or extra.get("subID") or "").strip()
            if not extra_id:
                continue
            current = found.get(extra_id)
            if current is None:
                found[extra_id] = extra
                continue
            if not current.get("_detail") and extra.get("_detail"):
                current["_detail"] = extra["_detail"]
            if not current.get("numSpecies") and extra.get("numSpecies"):
                current["numSpecies"] = extra["numSpecies"]

    return sorted(
        found.values(),
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )


def load_own_local_checklists(
    observer_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Own checklists from downloaded JSON and the My eBird data export."""
    names = observer_names if observer_names is not None else configured_observer_names()
    folded = {name.strip().casefold() for name in names if str(name).strip()}
    found: dict[str, dict[str, Any]] = {}
    if folded:
        for region in list_local_checklist_regions():
            for row in load_local_checklists(region):
                display = str(row.get("userDisplayName") or "").strip()
                source = str(row.get("_source") or "")
                if (
                    display.casefold() not in folded
                    and source != "my_ebird_data"
                ):
                    continue
                sub_id = str(row.get("subId") or row.get("subID") or "").strip()
                if not sub_id:
                    continue
                tagged = dict(row)
                tagged["regionCode"] = tagged.get("regionCode") or region
                previous = found.get(sub_id)
                if previous is None or str(tagged.get("isoObsDate") or "") >= str(
                    previous.get("isoObsDate") or ""
                ):
                    found[sub_id] = tagged
    from my_ebird_data import my_ebird_checklist_summaries

    for extra in my_ebird_checklist_summaries():
        extra_id = str(extra.get("subId") or extra.get("subID") or "").strip()
        if not extra_id:
            continue
        current = found.get(extra_id)
        if current is None:
            found[extra_id] = extra
            continue
        if not current.get("_detail") and extra.get("_detail"):
            current["_detail"] = extra["_detail"]
        if not current.get("numSpecies") and extra.get("numSpecies"):
            current["numSpecies"] = extra["numSpecies"]
    return sorted(
        found.values(),
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )


def _checklist_sighting_count(checklist: dict[str, Any], feed: dict[str, Any]) -> int:
    """How many species a cached checklist reported."""
    if not isinstance(checklist, dict):
        checklist = {}
    if not isinstance(feed, dict):
        feed = {}
    for raw in (checklist.get("numSpecies"), feed.get("numSpecies")):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    obs = checklist.get("obs")
    return len(obs) if isinstance(obs, list) else 0


def _recent_sighting_rank(row: dict[str, Any]) -> tuple[int, int, str, str]:
    """Prefer the checklist with more species, then a higher count of this bird."""
    try:
        species = int(row.get("numSpecies") or 0)
    except (TypeError, ValueError):
        species = 0
    raw_count = row.get("howMany")
    try:
        how_many = int(raw_count) if raw_count not in (None, "") else -1
    except (TypeError, ValueError):
        how_many = -1
    return (
        species,
        how_many,
        str(row.get("obsDt") or ""),
        str(row.get("subId") or ""),
    )


def _reduce_recent_sightings(
    rows: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """One checklist per hotspot+date, then the latest ``limit`` days."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("obsDay") or "").strip()[:10]
        if not day:
            parsed = parse_ebird_obs_day(row.get("obsDt") or "")
            day = parsed.isoformat() if parsed else ""
        if not day:
            continue
        loc_id = str(row.get("locId") or "").strip()
        loc_name = str(row.get("locName") or "").strip()
        hotspot = loc_id or loc_name.casefold() or str(row.get("subId") or "").strip()
        if not hotspot:
            continue
        key = (hotspot, day)
        current = best.get(key)
        if current is None or _recent_sighting_rank(row) > _recent_sighting_rank(current):
            stored = dict(row)
            stored["obsDay"] = day
            best[key] = stored
    ordered = sorted(
        best.values(),
        key=lambda item: str(item.get("obsDt") or ""),
        reverse=True,
    )
    return ordered[:limit]


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
    root = region_checklists_dir(region)
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
        and isinstance(existing.get("recent_by_code"), dict)
    ):
        return {
            str(code): value
            for code, value in existing["by_code"].items()
            if isinstance(value, dict)
        }

    by_code: dict[str, dict[str, Any]] = {}
    recent_rows: dict[str, list[dict[str, Any]]] = {}
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
        sighting_count = _checklist_sighting_count(checklist, feed if isinstance(feed, dict) else {})
        obs_day = parse_ebird_obs_day(obs_dt)
        day_text = obs_day.isoformat() if obs_day else ""
        for obs in checklist.get("obs") or []:
            if not isinstance(obs, dict):
                continue
            code = str(obs.get("speciesCode") or "").strip()
            if not code:
                continue
            current = by_code.get(code)
            if current and str(current.get("obsDt") or "") >= obs_dt:
                pass
            else:
                by_code[code] = {
                    "speciesCode": code,
                    "obsDt": obs_dt,
                    "locName": loc_name,
                    "locId": loc_id,
                    "howMany": _parse_obs_count(obs),
                    "subId": sub_id,
                    "source": "local_checklist",
                }
            if day_text:
                recent_rows.setdefault(code, []).append(
                    {
                        "obsDt": obs_dt,
                        "obsDay": day_text,
                        "locName": loc_name,
                        "locId": loc_id,
                        "subId": sub_id,
                        "numSpecies": sighting_count,
                        "howMany": _parse_obs_count(obs),
                    }
                )

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
    previous_recent = existing.get("recent_by_code")
    if previous_files > len(files) and isinstance(previous_recent, dict):
        for code, rows in previous_recent.items():
            if isinstance(rows, list):
                recent_rows.setdefault(str(code), []).extend(rows)

    recent_by_code = {
        code: _reduce_recent_sightings(rows)
        for code, rows in recent_rows.items()
    }

    _save_json_file(
        index_path,
        {
            "region_code": region,
            "signature": signature,
            "updated_at": datetime.now().astimezone().isoformat(),
            "by_code": by_code,
            "recent_by_code": recent_by_code,
        },
    )
    return by_code


def local_recent_sightings_for_species(
    region_code: str,
    species_code: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Latest cached checklist sightings for a species in a region.

    Uses only downloaded checklist files. At most one checklist per hotspot
    and date; when several exist, the list with the most species wins.
    """
    region = (region_code or "").strip()
    code = (species_code or "").strip()
    if not region or not code:
        return []
    build_local_last_seen_index(region)
    payload = _load_json_file(local_checklist_index_path(region))
    rows = payload.get("recent_by_code")
    if not isinstance(rows, dict):
        return []
    items = rows.get(code)
    if not isinstance(items, list):
        return []
    cleaned = [item for item in items if isinstance(item, dict)]
    return cleaned[: max(0, int(limit))]


def _checklist_disk_signature() -> dict[str, Any]:
    files: list[Path] = []
    if CACHE_DIR.exists():
        for region_dir in CACHE_DIR.iterdir():
            if not region_dir.is_dir() or region_dir.name in {"shared", "."}:
                continue
            checklists = region_dir / "checklists"
            if checklists.is_dir():
                files.extend(checklists.rglob("S*.json"))
    files = sorted(files)
    from my_ebird_data import my_ebird_source_signature

    export_path, export_mtime = my_ebird_source_signature()
    return {
        "file_count": len(files),
        "newest_mtime": max((path.stat().st_mtime for path in files), default=0.0),
        "observers": [name.strip().casefold() for name in configured_observer_names()],
        "my_ebird_path": export_path,
        "my_ebird_mtime": export_mtime,
    }


def build_own_recent_sightings_index(
    *,
    rebuild: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Index the configured observer’s cached species sightings by eBird code."""
    names = configured_observer_names()
    folded = {name.strip().casefold() for name in names if str(name).strip()}
    signature = _checklist_disk_signature()
    from my_ebird_data import my_ebird_data_path, my_ebird_own_recent_rows

    if not folded and my_ebird_data_path() is None:
        return {}
    existing = _load_json_file(OWN_RECENT_SIGHTINGS_PATH)
    cached = existing.get("by_code")
    if (
        not rebuild
        and existing.get("signature") == signature
        and isinstance(cached, dict)
    ):
        cleaned: dict[str, list[dict[str, Any]]] = {}
        for code, rows in cached.items():
            if isinstance(rows, list):
                cleaned[str(code)] = [row for row in rows if isinstance(row, dict)]
        return cleaned

    recent_rows: dict[str, list[dict[str, Any]]] = {}
    for region in list_local_checklist_regions():
        root = region_checklists_dir(region)
        if not root.exists():
            continue
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
            display = str(
                checklist.get("userDisplayName")
                or feed.get("userDisplayName")
                or ""
            ).strip()
            if display.casefold() not in folded:
                continue
            obs_dt = str(
                checklist.get("obsDt")
                or feed.get("isoObsDate")
                or feed.get("obsDt")
                or ""
            ).strip()
            if not obs_dt:
                continue
            loc_obj = feed.get("loc") if isinstance(feed.get("loc"), dict) else {}
            loc_id = str(
                checklist.get("locId")
                or feed.get("locId")
                or feed.get("locID")
                or loc_obj.get("locId")
                or loc_obj.get("locID")
                or ""
            ).strip()
            loc_name = str(
                feed.get("locName")
                or loc_obj.get("locName")
                or loc_obj.get("name")
                or ""
            ).strip()
            if not loc_name and "__" in path.parent.name:
                loc_name = path.parent.name.split("__", 1)[1].replace("_", " ").strip()
            sub_id = str(
                checklist.get("subId")
                or feed.get("subId")
                or feed.get("subID")
                or path.stem
            ).strip()
            sighting_count = _checklist_sighting_count(
                checklist, feed if isinstance(feed, dict) else {}
            )
            obs_day = parse_ebird_obs_day(obs_dt)
            day_text = obs_day.isoformat() if obs_day else ""
            if not day_text:
                continue
            for obs in checklist.get("obs") or []:
                if not isinstance(obs, dict):
                    continue
                code = str(obs.get("speciesCode") or "").strip()
                if not code:
                    continue
                recent_rows.setdefault(code, []).append(
                    {
                        "obsDt": obs_dt,
                        "obsDay": day_text,
                        "locName": loc_name,
                        "locId": loc_id,
                        "subId": sub_id,
                        "numSpecies": sighting_count,
                        "howMany": _parse_obs_count(obs),
                        "regionCode": region,
                    }
                )

    for code, rows in my_ebird_own_recent_rows().items():
        recent_rows.setdefault(code, []).extend(rows)

    by_code = {
        code: _reduce_recent_sightings(rows)
        for code, rows in recent_rows.items()
    }
    _save_json_file(
        OWN_RECENT_SIGHTINGS_PATH,
        {
            "signature": signature,
            "updated_at": datetime.now().astimezone().isoformat(),
            "by_code": by_code,
        },
    )
    return by_code


def local_own_recent_sightings_for_species(
    species_code: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """The observer’s latest cached sightings of a species, any region."""
    code = (species_code or "").strip()
    if not code:
        return []
    by_code = build_own_recent_sightings_index()
    items = by_code.get(code)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][: max(0, int(limit))]


def parse_ebird_obs_day(text: str) -> date | None:
    """Parse an eBird observation day from ISO or life-list date text."""
    raw = str(text or "").strip()
    if not raw:
        return None
    iso = raw[:10]
    if len(iso) == 10 and iso[4] == "-" and iso[7] == "-":
        try:
            return date.fromisoformat(iso)
        except ValueError:
            pass
    for fmt, size in (("%d %b %Y", 11), ("%d %B %Y", 15)):
        snippet = raw[:size].strip()
        try:
            return datetime.strptime(snippet, fmt).date()
        except ValueError:
            continue
    return None


def build_local_year_first_index(
    region_code: str,
    year: int,
    *,
    rebuild: bool = False,
) -> dict[str, date]:
    """Earliest downloaded observation date in ``year`` per species code."""
    region = (region_code or "").strip()
    if not region or year < 2002:
        return {}
    root = region_checklists_dir(region)
    path = local_year_first_index_path(region, year)
    files = sorted(root.rglob("S*.json")) if root.exists() else []
    signature = {
        "file_count": len(files),
        "newest_mtime": max((item.stat().st_mtime for item in files), default=0.0),
        "year": int(year),
    }
    existing = _load_json_file(path)
    if (
        not rebuild
        and existing.get("signature") == signature
        and isinstance(existing.get("by_code"), dict)
    ):
        found: dict[str, date] = {}
        for code, value in existing["by_code"].items():
            day = parse_ebird_obs_day(str(value or ""))
            if day is not None:
                found[str(code)] = day
        return found

    by_code: dict[str, date] = {}
    for file_path in files:
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        checklist = payload.get("checklist") or {}
        feed = payload.get("feed_summary") or {}
        if not isinstance(checklist, dict):
            continue
        obs_day = parse_ebird_obs_day(
            str(
                checklist.get("obsDt")
                or feed.get("isoObsDate")
                or feed.get("obsDt")
                or ""
            )
        )
        if obs_day is None or obs_day.year != year:
            continue
        for obs in checklist.get("obs") or []:
            if not isinstance(obs, dict):
                continue
            code = str(obs.get("speciesCode") or "").strip()
            if not code:
                continue
            current = by_code.get(code)
            if current is None or obs_day < current:
                by_code[code] = obs_day

    _save_json_file(
        path,
        {
            "region_code": region,
            "year": int(year),
            "signature": signature,
            "updated_at": datetime.now().astimezone().isoformat(),
            "by_code": {code: day.isoformat() for code, day in by_code.items()},
        },
    )
    return by_code


def build_world_year_first_index(year: int) -> dict[str, date]:
    """Earliest downloaded observation this year across all cached regions."""
    merged: dict[str, date] = {}
    for region in list_local_checklist_regions():
        for code, day in build_local_year_first_index(region, year).items():
            previous = merged.get(code)
            if previous is None or day < previous:
                merged[code] = day
    return merged


def list_local_checklist_regions() -> list[str]:
    """Region codes that have downloaded checklist files on disk."""
    if not CACHE_DIR.exists():
        return []
    regions: list[str] = []
    for path in sorted(CACHE_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith(".") or path.name == "shared":
            continue
        checklists = path / "checklists"
        if checklists.is_dir() and any(checklists.rglob("S*.json")):
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


def _read_ebird_not_before(handle) -> float:
    handle.seek(0)
    raw = handle.read().strip()
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _write_ebird_not_before(handle, when: float) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(f"{when:.6f}\n")
    handle.flush()


def _with_ebird_throttle_file(write_fn) -> None:
    """Run ``write_fn(handle)`` with an exclusive lock on the throttle file."""
    EBIRD_THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EBIRD_THROTTLE_PATH.touch(exist_ok=True)
    with open(EBIRD_THROTTLE_PATH, "a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            write_fn(handle)
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


def wait_for_ebird_request_slot() -> float:
    """Block until the next eBird request is allowed. Returns seconds waited.

    Shared by every ``EBirdClient`` in this process and by the download worker
    via ``.ebird_api_throttle``, so Show checklists and background jobs cannot
    burst past ``MAX_CALLS_PER_MINUTE``.
    """
    waited = 0.0

    def _reserve(handle) -> None:
        nonlocal waited
        now = time.time()
        not_before = _read_ebird_not_before(handle)
        wait = not_before - now
        if wait > 0:
            time.sleep(wait)
            waited = wait
            now = time.time()
        _write_ebird_not_before(handle, now + MIN_REQUEST_INTERVAL_SECONDS)

    with _ebird_throttle_lock:
        _with_ebird_throttle_file(_reserve)
    return waited


def defer_ebird_requests(seconds: float) -> None:
    """Push the shared next-allowed time out (used after HTTP 429)."""
    extra = max(float(seconds), 0.0)
    if extra <= 0:
        return

    def _defer(handle) -> None:
        now = time.time()
        not_before = max(_read_ebird_not_before(handle), now + extra)
        _write_ebird_not_before(handle, not_before)

    with _ebird_throttle_lock:
        _with_ebird_throttle_file(_defer)


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
    entry = _load_region_species_entry(region_code)
    if entry is None:
        return None
    return _codes_from_region_species_entry(entry)


def load_disk_region_species_birds(region_code: str) -> list[dict[str, str]] | None:
    """Return the cached named regional species list, or ``None`` if missing."""
    entry = _load_region_species_entry(region_code)
    if not isinstance(entry, dict):
        return None
    birds = entry.get("birds")
    if not isinstance(birds, list):
        return None
    cleaned: list[dict[str, str]] = []
    for bird in birds:
        if not isinstance(bird, dict):
            continue
        code = str(bird.get("code") or "").strip()
        if not code:
            continue
        name = str(bird.get("name") or code).strip()
        cleaned.append(
            {
                "code": code,
                "name": name.split(" (", 1)[0].strip() or name,
                "sciName": str(bird.get("sciName") or "").strip(),
            }
        )
    return cleaned


def _load_region_species_entry(region_code: str) -> list | dict | None:
    region = (region_code or "").strip()
    if not region:
        return None
    cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
    cached = cache.get(region)
    if isinstance(cached, (list, dict)):
        return cached
    return None


def _codes_from_region_species_entry(entry: list | dict) -> list[str] | None:
    if isinstance(entry, list):
        return [str(item).strip() for item in entry if str(item).strip()]
    codes = entry.get("codes")
    if isinstance(codes, list):
        return [str(item).strip() for item in codes if str(item).strip()]
    birds = entry.get("birds")
    if isinstance(birds, list):
        out: list[str] = []
        for bird in birds:
            if not isinstance(bird, dict):
                continue
            code = str(bird.get("code") or "").strip()
            if code:
                out.append(code)
        return out
    return None


def save_region_species_cache(
    region_code: str,
    *,
    codes: list[str] | None = None,
    birds: list[dict] | None = None,
) -> None:
    """Persist a region’s historical species codes and/or named bird rows."""
    region = (region_code or "").strip()
    if not region:
        return
    cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
    existing = cache.get(region)
    entry: dict[str, Any] = {}
    if isinstance(existing, dict):
        entry = dict(existing)
    elif isinstance(existing, list):
        entry["codes"] = [str(item).strip() for item in existing if str(item).strip()]
    if codes is not None:
        entry["codes"] = [str(item).strip() for item in codes if str(item).strip()]
    if birds is not None:
        cleaned: list[dict[str, str]] = []
        for bird in birds:
            if not isinstance(bird, dict):
                continue
            code = str(bird.get("code") or "").strip()
            if not code:
                continue
            name = str(bird.get("name") or code).strip()
            cleaned.append(
                {
                    "code": code,
                    "name": name.split(" (", 1)[0].strip() or name,
                    "sciName": str(bird.get("sciName") or "").strip(),
                }
            )
        entry["birds"] = cleaned
        if "codes" not in entry:
            entry["codes"] = [item["code"] for item in cleaned]
    entry["updated_at"] = datetime.now().astimezone().isoformat()
    cache[region] = entry
    _save_json_file(REGION_SPECIES_CACHE_PATH, cache)


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

    photo_cache = _load_json_file(CACHE_SHARED_DIR / "inaturalist_cache.json")
    in_photos = {
        code
        for code in historical_set
        if isinstance(photo_cache.get(code), dict) and photo_cache.get(code)
    }

    gallery_cache = _load_json_file(CACHE_SHARED_DIR / "inaturalist_gallery_cache.json")
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

    similar_cache = _load_json_file(CACHE_SHARED_DIR / "inaturalist_similar_cache.json")
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
        last_429_wait = 0.0
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            slot_wait = wait_for_ebird_request_slot()
            if slot_wait > 5.0:
                self.wait_count += 1
                self.wait_seconds_total += slot_wait
                note_rate_limit(slot_wait, path=f"{path} (shared throttle)")
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
                raw_retry = (response.headers.get("Retry-After") or "").strip()
                base_wait = (
                    max(header_wait, self.min_rate_limit_wait_seconds)
                    if not raw_retry
                    else header_wait
                )
                wait_seconds = base_wait * 2.0
                last_429_wait = wait_seconds
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
                defer_ebird_requests(wait_seconds)
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
            if last_429_wait > 0:
                defer_ebird_requests(last_429_wait)
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
        hotspot: bool = False,
        use_cache: bool = True,
        refresh: bool = False,
        cache_ttl_seconds: int = RECENT_OBS_CACHE_TTL_SECONDS,
    ) -> Any:
        """Recent observations for a region or hotspot location.

        Results are cached on disk under ``cache/shared/ebird_recent_obs/``
        for ``cache_ttl_seconds`` (default 3 hours) unless ``use_cache`` is
        False or ``refresh`` is True.
        """
        region = str(region_code or "").strip()
        species = str(species_code or "").strip() or None
        days_back = max(1, int(back))
        if use_cache and not refresh and region:
            cached = load_cached_recent_observations(
                region,
                species,
                back=days_back,
                max_results=max_results,
                hotspot=hotspot,
                ttl_seconds=cache_ttl_seconds,
            )
            if cached is not None:
                return cached

        if species:
            path = f"/data/obs/{region}/recent/{species}"
            params: dict[str, Any] = {"back": days_back}
            # Species-specific recent feeds reject maxResults on some deployments.
        else:
            path = f"/data/obs/{region}/recent"
            params = {"back": days_back}
            if max_results is not None:
                params["maxResults"] = max_results
        if hotspot:
            params["hotspot"] = "true"
        rows = self.get(path, params=params)
        if use_cache and region:
            try:
                save_cached_recent_observations(
                    region,
                    rows,
                    species,
                    back=days_back,
                    max_results=max_results,
                    hotspot=hotspot,
                )
            except OSError:
                pass
        return rows

    def cached_region_species_codes(self, region_code: str) -> set[str]:
        """Return species codes ever recorded in a region (disk-cached)."""
        return set(self.region_species_codes(region_code))

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
        first_day: date | None = None,
        last_day: date | None = None,
    ) -> dict[str, Any]:
        """Persist daily checklist feeds for a region through today.

        eBird returns at most ``max_results`` entries per date. Dates that hit
        that limit are retained but marked as truncated so consumers do not
        mistake the cache for a complete daily record.

        Missing days (including whole prior years) are fetched. Already-cached
        historical days are skipped; today is always refreshed. Pass
        ``first_day`` / ``last_day`` to fill only a window inside the year.
        Extra ``delay_seconds`` between days is optional; ``EBirdClient.get``
        already enforces ``MAX_CALLS_PER_MINUTE``.
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
        year_first = date(year, 1, 1)
        year_last = min(date(year, 12, 31), today)
        if year_first > year_last:
            raise ValueError("The requested year has not started yet.")
        first_day = year_first if first_day is None else max(first_day, year_first)
        last_day = year_last if last_day is None else min(last_day, year_last)
        if first_day > last_day:
            raise ValueError("The requested date window is empty for this year.")

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

    def region_species_codes(
        self,
        region_code: str,
        *,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[str]:
        """Species codes ever recorded in a region, in eBird taxonomic order.

        Calls ``GET /product/spplist/{regionCode}`` when the region is not
        already on disk. The ordered JSON array is persisted for reuse.
        """
        code = (region_code or "").strip()
        if not code:
            return []
        if use_cache and not refresh:
            cached = load_disk_region_species_codes(code)
            if cached is not None:
                return cached
        rows = self.get(f"/product/spplist/{code}")
        if not isinstance(rows, list):
            return []
        codes = [str(item).strip() for item in rows if str(item).strip()]
        save_region_species_cache(code, codes=codes)
        return codes

    def region_species_birds(
        self,
        region_code: str,
        *,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[dict[str, str]]:
        """Named species ever recorded in a region, in eBird taxonomic order.

        Prefers the on-disk named list. Otherwise uses cached species codes,
        resolves names from taxonomy, and writes the full list to disk.
        Non-species taxonomy categories are omitted.
        """
        code = (region_code or "").strip()
        if not code:
            return []
        if use_cache and not refresh:
            cached_birds = load_disk_region_species_birds(code)
            if cached_birds is not None:
                return cached_birds
        species_codes = self.region_species_codes(
            code, use_cache=use_cache, refresh=refresh
        )
        if not species_codes:
            save_region_species_cache(code, codes=[], birds=[])
            return []
        taxa = self.species_taxa(species_codes)
        birds: list[dict[str, str]] = []
        for species_code in species_codes:
            taxon = taxa.get(species_code) or {}
            category = str(taxon.get("category") or "species").strip().casefold()
            if category and category != "species":
                continue
            name = str(taxon.get("comName") or species_code).strip()
            birds.append(
                {
                    "code": species_code,
                    "name": name.split(" (", 1)[0].strip() or name,
                    "sciName": str(taxon.get("sciName") or "").strip(),
                }
            )
        save_region_species_cache(code, codes=species_codes, birds=birds)
        return birds

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
        if not isinstance(rows, list):
            return []
        return filter_hotspots_for_region(
            [row for row in rows if isinstance(row, dict)],
            region_code,
        )

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
        current = filter_hotspots_for_region(
            list(existing) if existing is not None else load_cached_hotspots(code),
            code,
        )
        incoming = self.hotspots(code)
        merged, added = merge_hotspot_lists(current, incoming)
        if merged:
            save_cached_hotspots(code, merged)
        return merged, added

    def _hotspot_rows_from_feed(
        self, rows: list[Any], loc_id: str
    ) -> list[dict[str, Any]]:
        wanted = (loc_id or "").strip()
        matched: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict) and _feed_row_loc_id(row) == wanted:
                matched.append(row)
        return matched

    def _hotspot_checklists_for_day(
        self,
        loc_id: str,
        day: date,
        *,
        region_code: str,
        persist: bool,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """Prefer the regional daily-feed cache; fetch and store missing days."""
        today = date.today()
        code = (region_code or "").strip()
        found: dict[str, dict[str, Any]] = {}

        def keep(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                sub_id = str(row.get("subId") or row.get("subID") or "").strip()
                if sub_id:
                    found[sub_id] = row

        if day > today:
            cache = load_region_year_feed_cache(code, day.year)
            daily = cache.get("daily") if isinstance(cache.get("daily"), dict) else {}
            entry = daily.get(day.isoformat()) if isinstance(daily, dict) else None
            if isinstance(entry, dict):
                keep(self._hotspot_rows_from_feed(entry.get("checklists") or [], loc_id))
            return list(found.values())

        cache = load_region_year_feed_cache(code, day.year)
        daily = cache.get("daily") if isinstance(cache.get("daily"), dict) else {}
        entry = daily.get(day.isoformat()) if isinstance(daily, dict) else None
        use_cache = isinstance(entry, dict) and day < today
        truncated = False
        if use_cache:
            keep(self._hotspot_rows_from_feed(entry.get("checklists") or [], loc_id))
            truncated = bool(entry.get("truncated"))
        else:
            rows = self.checklists_on_date(
                code,
                day.year,
                day.month,
                day.day,
                max_results=max_results,
            )
            if persist:
                persist_region_feed_day(code, day, rows, max_results=max_results)
            keep(self._hotspot_rows_from_feed(rows, loc_id))
            truncated = len(rows) >= max_results

        # Region feeds cap at max_results per day. Fill this hotspot from the
        # location endpoint without overwriting the regional cache.
        if truncated:
            keep(
                self.checklists_on_date(
                    loc_id,
                    day.year,
                    day.month,
                    day.day,
                    max_results=max_results,
                )
            )
        return list(found.values())

    def location_checklists(
        self,
        loc_id: str,
        *,
        days_back: int = 7,
        start_date: date | None = None,
        end_date: date | None = None,
        max_results: int = 200,
        region_code: str | None = None,
        persist: bool = True,
    ) -> list[dict[str, Any]]:
        """Checklists submitted at a hotspot/location over a date window.

        Uses ``start_date``–``end_date`` when both are provided. Otherwise the
        window ends on ``end_date`` (default: today) and includes ``days_back``
        days ending on that date.

        When ``region_code`` is set, historical days are read from the regional
        daily-feed cache first. Missing days (and today) are fetched from eBird
        and written back into that cache.
        """
        end = end_date or date.today()
        if start_date is not None:
            start = start_date
            if start > end:
                start, end = end, start
            days_back = (end - start).days + 1
        else:
            start = end - timedelta(days=max(1, int(days_back)) - 1)
        found: dict[str, dict[str, Any]] = {}

        def keep(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                sub_id = row.get("subId") or row.get("subID")
                if sub_id:
                    found[str(sub_id)] = row

        region = (region_code or "").strip()
        if region:
            day = start
            while day <= end:
                keep(
                    self._hotspot_checklists_for_day(
                        loc_id,
                        day,
                        region_code=region,
                        persist=persist,
                        max_results=max_results,
                    )
                )
                day += timedelta(days=1)
        else:
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
