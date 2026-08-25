"""Download cached eBird regional checklist feeds into full checklist files.

Example:
    .venv/bin/python download_checklists.py --region US-FL-099 --year 2026
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ebird import (
    CACHE_DIR,
    EBirdClient,
    MAX_CALLS_PER_MINUTE,
    region_checklists_dir,
    region_year_checklist_cache_path,
)

PROGRESS_VERSION = 1


def _suppress_streamlit_bare_mode_warnings() -> None:
    """Hide ScriptRunContext warnings when this worker runs outside Streamlit."""

    class _ScriptRunContextFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            message = record.getMessage()
            return "missing ScriptRunContext" not in message

    for name in (
        "streamlit.runtime.scriptrunner_utils.script_run_context",
        "streamlit.runtime.scriptrunner.script_run_context",
        "streamlit",
    ):
        logging.getLogger(name).addFilter(_ScriptRunContextFilter())


_suppress_streamlit_bare_mode_warnings()

def safe_component(value: object, fallback: str) -> str:
    """Return a filesystem-safe directory or filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or fallback


def _region_cache_dir(region_code: str) -> Path:
    """``cache/<region>/`` for progress and related region-scoped files."""
    from ebird import cache_region_dir

    return cache_region_dir(region_code)


def checklist_destination(
    region_code: str,
    *,
    output_root: Path | None = None,
) -> Path:
    """Directory holding year/hotspot checklist detail JSON for one region."""
    if output_root is None:
        return region_checklists_dir(region_code)
    return Path(output_root) / safe_component(region_code, "region")


def download_progress_path(region_code: str, year: int) -> Path:
    """Location of a resumable background checklist-download progress record."""
    return _region_cache_dir(region_code) / f"checklist_download_progress_{year}.json"


def feed_cache_progress_path(region_code: str, year: int) -> Path:
    """Location of resumable daily-feed cache progress."""
    return _region_cache_dir(region_code) / f"feed_cache_progress_{year}.json"


def load_feed_cache_progress(region_code: str, year: int) -> dict[str, Any]:
    """Load persisted daily-feed worker state, or {}."""
    path = feed_cache_progress_path(region_code, year)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_download_progress(region_code: str, year: int) -> dict[str, Any]:
    """Load persisted background-worker state, or {}."""
    path = download_progress_path(region_code, year)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_is_running(pid: object) -> bool:
    """True when ``pid`` refers to a live process we can signal."""
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


_PROGRESS_FILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("checklist", re.compile(r"^checklist_download_progress_(\d+)\.json$")),
    ("feed", re.compile(r"^feed_cache_progress_(\d+)\.json$")),
)

# Keep stopped / crashed workers visible for resume this long.
STOPPED_WORKER_VISIBLE_SECONDS = 14 * 24 * 3600


def _parse_progress_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return when


def list_background_workers(*, include_stopped: bool = True) -> list[dict[str, Any]]:
    """Checklist-download / feed-cache workers across every cache region.

    Each item: ``kind`` (``checklist``|``feed``), ``region_code``, ``year``,
    ``state`` (``running``|``stopped``), ``progress``, ``path``.

    Stopped includes interrupted (progress says running but pid is dead) and
    explicit stops, while they remain recent enough to resume.
    """
    if not CACHE_DIR.exists():
        return []
    workers: list[dict[str, Any]] = []
    now = datetime.now().astimezone()
    for region_dir in sorted(CACHE_DIR.iterdir()):
        if (
            not region_dir.is_dir()
            or region_dir.name.startswith(".")
            or region_dir.name == "shared"
        ):
            continue
        region = region_dir.name
        for path in sorted(region_dir.iterdir()):
            if not path.is_file():
                continue
            kind: str | None = None
            year: int | None = None
            for kind_name, pattern in _PROGRESS_FILE_PATTERNS:
                match = pattern.match(path.name)
                if match:
                    kind = kind_name
                    year = int(match.group(1))
                    break
            if kind is None or year is None:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "").strip().lower()
            if status in {"complete", "done", "idle", ""}:
                continue
            pid_alive = _pid_is_running(data.get("pid"))
            if status == "running" and pid_alive:
                state = "running"
            elif include_stopped and (
                status in {"stopped", "interrupted", "error"}
                or (status == "running" and not pid_alive)
            ):
                state = "stopped"
                stamp = (
                    _parse_progress_iso(data.get("finished_at"))
                    or _parse_progress_iso(data.get("updated_at"))
                    or _parse_progress_iso(data.get("started_at"))
                )
                if stamp is None:
                    continue
                age = (now - stamp).total_seconds()
                if age > STOPPED_WORKER_VISIBLE_SECONDS:
                    continue
            else:
                continue
            workers.append(
                {
                    "kind": kind,
                    "region_code": region,
                    "year": int(year),
                    "state": state,
                    "progress": data,
                    "path": path,
                }
            )
    # Running first, then stopped; stable by region/year within each group.
    workers.sort(
        key=lambda row: (
            0 if row.get("state") == "running" else 1,
            str(row.get("region_code") or ""),
            int(row.get("year") or 0),
            str(row.get("kind") or ""),
        )
    )
    return workers


def list_active_background_workers() -> list[dict[str, Any]]:
    """Running checklist-download / feed-cache workers across every cache region."""
    return [
        worker
        for worker in list_background_workers(include_stopped=False)
        if worker.get("state") == "running"
    ]


def dismiss_background_worker(
    kind: str,
    region_code: str,
    year: int,
) -> dict[str, Any]:
    """Remove a stopped worker's progress file so it no longer appears in the UI.

    Does not delete downloaded checklist or feed cache data. Refuses if the
    worker process is still running.
    """
    region = str(region_code or "").strip()
    worker_kind = str(kind or "checklist").strip().lower()
    if not region or int(year) < 2002:
        return {"removed": False, "reason": "invalid"}
    if worker_kind == "feed":
        path = feed_cache_progress_path(region, int(year))
        progress = load_feed_cache_progress(region, int(year))
    else:
        path = download_progress_path(region, int(year))
        progress = load_download_progress(region, int(year))
    if not progress and not path.exists():
        return {"removed": False, "reason": "not_found"}
    if (
        str(progress.get("status") or "") == "running"
        and _pid_is_running(progress.get("pid"))
    ):
        return {"removed": False, "reason": "still_running", "pid": progress.get("pid")}
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"removed": False, "reason": "unlink_failed", "error": str(exc)}
    return {
        "removed": True,
        "kind": worker_kind,
        "region_code": region,
        "year": int(year),
        "path": str(path),
    }


LIVE_FETCH_STALE_SECONDS = 180


def live_fetch_progress_path(region_code: str) -> Path:
    """Progress file for in-app checklist detail fetches (Checklists / Hotspots)."""
    return _region_cache_dir(region_code) / "checklist_live_fetch_progress.json"


def load_live_fetch_progress(region_code: str) -> dict[str, Any]:
    """Load in-app checklist fetch progress, or {}."""
    path = live_fetch_progress_path(region_code)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_live_fetch_progress(region_code: str, progress: dict[str, Any]) -> None:
    progress = dict(progress)
    progress["updated_at"] = datetime.now().astimezone().isoformat()
    path = live_fetch_progress_path(region_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, progress)


def begin_live_checklist_fetch(
    region_code: str,
    *,
    total: int,
    label: str = "",
    loc_id: str | None = None,
    source: str = "",
) -> dict[str, Any]:
    """Mark an in-app checklist detail batch as running (visible on Cache screen)."""
    region = str(region_code or "").strip()
    if not region:
        return {}
    now = datetime.now().astimezone().isoformat()
    progress = {
        "version": PROGRESS_VERSION,
        "status": "running",
        "source": str(source or "").strip(),
        "label": str(label or "").strip(),
        "loc_id": str(loc_id or "").strip(),
        "total": max(0, int(total)),
        "processed": 0,
        "downloaded": 0,
        "failed": 0,
        "message": "Starting…",
        "started_at": now,
        "finished_at": "",
        "pid": os.getpid(),
    }
    _write_live_fetch_progress(region, progress)
    return progress


def update_live_checklist_fetch(
    region_code: str,
    *,
    processed: int | None = None,
    downloaded: int | None = None,
    failed: int | None = None,
    total: int | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Update in-app checklist fetch counters while a batch is running."""
    region = str(region_code or "").strip()
    if not region:
        return {}
    progress = load_live_fetch_progress(region)
    if not progress:
        progress = {
            "version": PROGRESS_VERSION,
            "status": "running",
            "started_at": datetime.now().astimezone().isoformat(),
            "pid": os.getpid(),
        }
    if total is not None:
        progress["total"] = max(0, int(total))
    if processed is not None:
        progress["processed"] = max(0, int(processed))
    if downloaded is not None:
        progress["downloaded"] = max(0, int(downloaded))
    if failed is not None:
        progress["failed"] = max(0, int(failed))
    if message is not None:
        progress["message"] = str(message)
    progress["status"] = "running"
    _write_live_fetch_progress(region, progress)
    return progress


def finish_live_checklist_fetch(
    region_code: str,
    *,
    status: str = "done",
    message: str | None = None,
) -> dict[str, Any]:
    """Mark an in-app checklist fetch finished (done / error / cancelled)."""
    region = str(region_code or "").strip()
    if not region:
        return {}
    progress = load_live_fetch_progress(region)
    if not progress:
        return {}
    now = datetime.now().astimezone().isoformat()
    progress["status"] = str(status or "done")
    progress["finished_at"] = now
    if message is not None:
        progress["message"] = str(message)
    _write_live_fetch_progress(region, progress)
    return progress


def live_checklist_fetch_is_active(progress: dict[str, Any] | None) -> bool:
    """True when a live fetch looks still in progress (not stale)."""
    if not isinstance(progress, dict):
        return False
    if str(progress.get("status") or "") != "running":
        return False
    updated = str(progress.get("updated_at") or progress.get("started_at") or "")
    if not updated:
        return False
    try:
        when = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = (datetime.now().astimezone() - when).total_seconds()
    return age <= LIVE_FETCH_STALE_SECONDS


def request_download_stop(region_code: str, year: int) -> dict[str, Any]:
    """Force-stop the background checklist download worker immediately.

    Sets the progress file to stopped and sends SIGTERM (then SIGKILL if
    needed) to the worker process / process group.
    """
    return _stop_background_worker(
        load_download_progress(region_code, year),
        download_progress_path(region_code, year),
    )


def request_feed_cache_stop(region_code: str, year: int) -> dict[str, Any]:
    """Force-stop the background daily-feed cache worker."""
    return _stop_background_worker(
        load_feed_cache_progress(region_code, year),
        feed_cache_progress_path(region_code, year),
    )


def _stop_background_worker(
    progress: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    """Mark a worker stopped and terminate its process group."""
    if not progress:
        return {"stopped": False, "killed": False, "reason": "no_progress"}

    pid_raw = progress.get("pid")
    now = datetime.now().astimezone().isoformat()
    progress["stop_requested"] = True
    progress["status"] = "stopped"
    progress["finished_at"] = now
    progress["updated_at"] = now
    _write_json(path, progress)

    try:
        process_id = int(pid_raw)
    except (TypeError, ValueError):
        return {
            "stopped": True,
            "killed": False,
            "reason": "invalid_pid",
            "pid": pid_raw,
        }
    if process_id <= 0:
        return {
            "stopped": True,
            "killed": False,
            "reason": "invalid_pid",
            "pid": process_id,
        }

    def _alive() -> bool:
        try:
            os.kill(process_id, 0)
            return True
        except OSError:
            return False

    if not _alive():
        return {
            "stopped": True,
            "killed": False,
            "reason": "not_running",
            "pid": process_id,
        }

    def _signal(sig: int) -> None:
        # Worker is started with start_new_session=True, so pid == pgid.
        try:
            os.killpg(process_id, sig)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(process_id, sig)

    try:
        _signal(signal.SIGTERM)
    except OSError as exc:
        return {
            "stopped": True,
            "killed": False,
            "reason": "signal_failed",
            "error": str(exc),
            "pid": process_id,
        }

    deadline = time.time() + 1.5
    while time.time() < deadline and _alive():
        time.sleep(0.05)
    if _alive():
        try:
            _signal(signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.05)

    return {
        "stopped": True,
        "killed": True,
        "pid": process_id,
        "still_running": _alive(),
    }


def _update_progress(
    region_code: str,
    year: int,
    progress: dict[str, Any],
) -> None:
    progress["updated_at"] = datetime.now().astimezone().isoformat()
    _write_json(download_progress_path(region_code, year), progress)


def _parse_iso_day(value: object) -> date | None:
    raw = str(value or "").strip()
    if len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _shift_calendar_date(day: date, years_back: int) -> date:
    """Same month/day in an earlier year; Feb 29 becomes Feb 28."""
    year = day.year - years_back
    try:
        return day.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def download_window_slices(
    start: date,
    end: date,
    *,
    prior_years: int = 0,
) -> list[tuple[int, date, date]]:
    """Per-year slices for a date range plus the same calendar window in prior years."""
    if start > end:
        start, end = end, start
    slices: list[tuple[int, date, date]] = []
    seen: set[tuple[int, str, str]] = set()
    for back in range(0, max(0, int(prior_years)) + 1):
        window_start = _shift_calendar_date(start, back)
        window_end = _shift_calendar_date(end, back)
        if window_start > window_end:
            window_start, window_end = window_end, window_start
        year = window_start.year
        while year <= window_end.year:
            slice_start = (
                window_start if year == window_start.year else date(year, 1, 1)
            )
            slice_end = (
                window_end if year == window_end.year else date(year, 12, 31)
            )
            key = (year, slice_start.isoformat(), slice_end.isoformat())
            if key not in seen:
                seen.add(key)
                slices.append((year, slice_start, slice_end))
            year += 1
    return slices


def format_download_windows(slices: list[tuple[int, date, date]]) -> str:
    """Human-readable list of download windows."""
    return ", ".join(
        f"{start.isoformat()}–{end.isoformat()}" if start != end else start.isoformat()
        for _year, start, end in slices
    )


def load_cached_summaries(
    region_code: str,
    year: int,
    month: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read unique summaries from the local regional date-feed cache.

    ``month=None`` includes every available day in the requested year. Dates
    marked truncated are reported, but their available summaries are retained.
    """
    cache_path = region_year_checklist_cache_path(region_code, year)
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Missing {cache_path.name}. Create the regional checklist cache first."
        )
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    prefix = f"{year}-{month:02d}-" if month is not None else f"{year}-"
    truncated = [
        day for day in cache.get("truncated_dates") or [] if str(day).startswith(prefix)
    ]

    summaries: dict[str, dict[str, Any]] = {}
    for day, entry in (cache.get("daily") or {}).items():
        if not str(day).startswith(prefix) or not isinstance(entry, dict):
            continue
        for row in entry.get("checklists") or []:
            checklist_id = str(row.get("subId") or row.get("subID") or "").strip()
            if checklist_id:
                summaries[checklist_id] = row
    return _sort_summaries_for_download(list(summaries.values())), truncated


def _checklist_obs_day(row: dict[str, Any]) -> str:
    """ISO day (YYYY-MM-DD) from a feed summary, or ''."""
    obs = str(row.get("isoObsDate") or row.get("obsDt") or "").strip()
    return obs[:10] if len(obs) >= 10 else ""


def save_checklist_detail(
    region_code: str,
    summary: dict[str, Any],
    detail: dict[str, Any],
    *,
    output_root: Path | None = None,
) -> Path | None:
    """Write a checklist detail file in the same layout as the downloader."""
    region = (region_code or "").strip()
    checklist_id = str(summary.get("subId") or summary.get("subID") or "").strip()
    if not region or not checklist_id or not isinstance(detail, dict) or not detail:
        return None
    region_destination = checklist_destination(region, output_root=output_root)
    filename = f"{safe_component(checklist_id, 'unknown')}.json"
    existing = (
        next(region_destination.rglob(filename), None)
        if region_destination.exists()
        else None
    )
    if existing is not None:
        checklist_path = existing
    else:
        location_id = str(summary.get("locId") or summary.get("locID") or "").strip()
        location_name = str(summary.get("locName") or "").strip()
        hotspot = safe_component(location_id, "unknown-location")
        if location_name:
            hotspot = f"{hotspot}__{safe_component(location_name, 'unnamed')}"
        obs_day = _checklist_obs_day(summary)
        period = obs_day[:4] if len(obs_day) >= 4 else "unknown-year"
        checklist_path = region_destination / period / hotspot / filename
    feed_summary = {
        key: value
        for key, value in summary.items()
        if not str(key).startswith("_")
    }
    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    checklist_path.write_text(
        json.dumps(
            {"feed_summary": feed_summary, "checklist": detail},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return checklist_path


def _sort_summaries_for_download(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Order checklists by observation date descending, then hotspot."""
    ordered = list(rows)

    def hotspot_key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("locId") or row.get("locID") or "").strip(),
            str(row.get("locName") or "").strip().casefold(),
            str(row.get("subId") or row.get("subID") or "").strip(),
        )

    # Stable sorts: hotspot ascending, then date descending.
    ordered.sort(key=hotspot_key)
    ordered.sort(key=_checklist_obs_day, reverse=True)
    return ordered


def missing_cached_summaries(
    region_code: str,
    year: int,
    *,
    month: int | None = None,
    day: str | None = None,
    start_day: str | None = None,
    end_day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    output_root: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return feed summaries whose full detail files are not on disk."""
    summaries, truncated_dates = load_cached_summaries(region_code, year, month)
    day_key = (day or "").strip()
    start_key = (start_day or "").strip() or day_key
    end_key = (end_day or "").strip() or day_key
    location = (loc_id or "").strip()
    species_floor = max(0, int(min_species or 0))
    if start_key or end_key:
        lo = start_key or "0000-01-01"
        hi = end_key or "9999-12-31"
        if lo > hi:
            lo, hi = hi, lo
        summaries = [
            row
            for row in summaries
            if lo <= _checklist_obs_day(row) <= hi
        ]
    if location:
        summaries = [
            row
            for row in summaries
            if str(row.get("locId") or row.get("locID") or "").strip() == location
        ]
    if species_floor > 0:
        summaries = [
            row
            for row in summaries
            if int(row.get("numSpecies") or 0) >= species_floor
        ]
    region_destination = checklist_destination(region_code, output_root=output_root)
    existing_ids = (
        {path.stem for path in region_destination.rglob("S*.json")}
        if region_destination.exists()
        else set()
    )
    missing = [
        row
        for row in summaries
        if str(row.get("subId") or row.get("subID") or "").strip()
        not in existing_ids
    ]
    return _sort_summaries_for_download(missing), truncated_dates


def missing_checklists_by_species_count(
    region_code: str,
    year: int,
    *,
    month: int | None = None,
    day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    output_root: Path | None = None,
) -> list[dict[str, int]]:
    """Histogram of missing checklist details keyed by feed ``numSpecies``."""
    summaries, _truncated = missing_cached_summaries(
        region_code,
        year,
        month=month,
        day=day,
        loc_id=loc_id,
        min_species=min_species,
        output_root=output_root,
    )
    counts: Counter[int] = Counter(
        int(row.get("numSpecies") or 0) for row in summaries
    )
    return [
        {"Species count": species, "Remaining": remaining}
        for species, remaining in sorted(counts.items())
    ]


def _checklist_id_from_path(path: Path) -> str:
    """Best-effort checklist id from filename or payload."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return path.stem
    if not isinstance(payload, dict):
        return path.stem
    checklist = payload.get("checklist") if isinstance(payload.get("checklist"), dict) else {}
    feed = (
        payload.get("feed_summary")
        if isinstance(payload.get("feed_summary"), dict)
        else {}
    )
    return str(
        checklist.get("subId")
        or feed.get("subId")
        or feed.get("subID")
        or path.stem
    ).strip() or path.stem


def _dedupe_keep_score(path: Path) -> tuple:
    """Higher score wins when choosing which duplicate checklist file to keep."""
    size = 0
    mtime = 0.0
    valid = 0
    has_detail = 0
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
    except OSError:
        pass
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            valid = 1
            if isinstance(payload.get("checklist"), dict) and payload.get("checklist"):
                has_detail = 1
    except (json.JSONDecodeError, OSError):
        pass
    named_hotspot = 1 if "__" in path.parent.name else 0
    return (valid, has_detail, named_hotspot, size, mtime, str(path))


def dedupe_downloaded_checklists(
    region_code: str | None = None,
    *,
    output_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete duplicate on-disk checklist detail files (same subId).

    Keeps the best copy per checklist id (valid detail JSON, named hotspot
    folder, largest, newest) and removes the rest.
    """
    roots: list[Path] = []
    if region_code:
        roots = [checklist_destination(region_code, output_root=output_root)]
    elif output_root is not None:
        roots = [Path(output_root)]
    elif CACHE_DIR.exists():
        for path in sorted(CACHE_DIR.iterdir()):
            if path.is_dir() and path.name != "shared":
                checklists = path / "checklists"
                if checklists.is_dir():
                    roots.append(checklists)

    by_id: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        if root.exists():
            for path in root.rglob("S*.json"):
                by_id[_checklist_id_from_path(path)].append(path)

    removed: list[str] = []
    kept = 0
    for checklist_id, paths in sorted(by_id.items()):
        if len(paths) < 2:
            kept += 1
            continue
        ranked = sorted(paths, key=_dedupe_keep_score, reverse=True)
        keep = ranked[0]
        kept += 1
        for path in ranked[1:]:
            removed.append(str(path))
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    removed.pop()

    # Drop empty hotspot directories left behind.
    empty_dirs = 0
    if not dry_run:
        for root in roots:
            if not root.exists():
                continue
            for directory in sorted(
                (p for p in root.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                try:
                    next(directory.iterdir())
                except StopIteration:
                    try:
                        directory.rmdir()
                        empty_dirs += 1
                    except OSError:
                        pass
                except OSError:
                    pass

    return {
        "region_code": region_code,
        "unique_checklists": kept,
        "duplicate_ids": sum(1 for paths in by_id.values() if len(paths) > 1),
        "removed_files": len(removed),
        "removed_paths": removed,
        "empty_dirs_removed": empty_dirs,
        "dry_run": dry_run,
    }


def cache_year_feed(region_code: str, year: int) -> dict[str, Any]:
    """Fetch/update the regional daily-feed cache for a year (including prior years)."""
    path = feed_cache_progress_path(region_code, year)
    progress: dict[str, Any] = {
        "cache_version": PROGRESS_VERSION,
        "kind": "feed",
        "region_code": region_code,
        "year": year,
        "status": "running",
        "pid": os.getpid(),
        "processed": 0,
        "total": 0,
        "remaining": 0,
        "stop_requested": False,
        "started_at": datetime.now().astimezone().isoformat(),
    }
    _write_json(path, progress)

    def _should_stop() -> bool:
        latest = load_feed_cache_progress(region_code, year)
        return bool(latest.get("stop_requested"))

    def _on_progress(update: dict[str, Any]) -> None:
        latest = load_feed_cache_progress(region_code, year)
        if latest.get("stop_requested"):
            progress["stop_requested"] = True
        progress.update(
            {
                "status": "running",
                "processed": int(update.get("processed") or 0),
                "total": int(update.get("total") or 0),
                "remaining": int(update.get("remaining") or 0),
                "last_day": update.get("last_day"),
                "days_in_feed": int(update.get("days_in_feed") or 0),
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )
        _write_json(path, progress)

    client = EBirdClient()
    result = client.cache_region_year_checklists(
        region_code,
        year,
        on_progress=_on_progress,
        should_stop=_should_stop,
    )
    stopped = str(result.get("status") or "") == "stopped" or progress.get(
        "stop_requested"
    )
    progress.update(
        {
            "status": "stopped" if stopped else "complete",
            "unique_checklists": int(result.get("unique_checklist_count") or 0),
            "truncated_dates": result.get("truncated_dates") or [],
            "days_in_feed": len(result.get("daily") or {}),
            "finished_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
    )
    _write_json(path, progress)
    return result


def download_cached_checklists(
    region_code: str,
    year: int,
    *,
    month: int | None = None,
    day: str | None = None,
    start_day: str | None = None,
    end_day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    prior_years: int = 0,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """Download missing cached checklist details, grouped by hotspot.

    Progress is durable, so this can run in a separate process and be stopped
    safely from the cache-maintenance UI. When ``start_day``/``end_day`` (or
    ``day`` / ``month``) is set, ``prior_years`` also fetches the same calendar
    window in earlier years.
    """
    species_floor = max(0, int(min_species or 0))
    prior = max(0, int(prior_years or 0))
    start = _parse_iso_day(start_day) or _parse_iso_day(day)
    end = _parse_iso_day(end_day) or _parse_iso_day(day)
    if start is None and end is None and month is not None:
        start, end = _month_bounds(year, month)
    windowed = start is not None and end is not None
    if not windowed:
        prior = 0
    slices = (
        download_window_slices(start, end, prior_years=prior)
        if windowed
        else []
    )

    region_destination = checklist_destination(region_code, output_root=output_root)
    region_destination.mkdir(parents=True, exist_ok=True)
    dedupe_downloaded_checklists(region_code, output_root=output_root)
    existing_ids = {
        path.stem
        for path in region_destination.rglob("S*.json")
    }

    client = EBirdClient()
    downloaded = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    retried_loads = 0
    hotspots: dict[str, int] = defaultdict(int)
    recent_durations: list[float] = []
    progress: dict[str, Any] = {
        "cache_version": PROGRESS_VERSION,
        "region_code": region_code,
        "year": year,
        "month": month,
        "day": (day or "").strip() or None,
        "start_day": start.isoformat() if start else None,
        "end_day": end.isoformat() if end else None,
        "prior_years": prior or None,
        "windows": format_download_windows(slices) if slices else None,
        "loc_id": (loc_id or "").strip() or None,
        "min_species": species_floor or None,
        "status": "running",
        "phase": "starting",
        "pid": os.getpid(),
        "total_missing": 0,
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "remaining": 0,
        "http_429_count": 0,
        "retried_loads": 0,
        "wait_count": 0,
        "wait_seconds_total": 0.0,
        "recent_durations_seconds": [],
        "rate_limit_events": [],
        "stop_requested": False,
        "started_at": datetime.now().astimezone().isoformat(),
    }
    _update_progress(region_code, year, progress)

    def _stop_requested() -> bool:
        return bool(load_download_progress(region_code, year).get("stop_requested"))

    summaries: list[dict[str, Any]] = []
    truncated_dates: list[str] = []
    if windowed:
        seen_ids: set[str] = set()
        for slice_year, slice_start, slice_end in slices:
            if _stop_requested():
                progress["status"] = "stopped"
                progress["finished_at"] = datetime.now().astimezone().isoformat()
                _update_progress(region_code, year, progress)
                break
            progress["phase"] = "feed"
            progress["window"] = (
                f"{slice_start.isoformat()}–{slice_end.isoformat()}"
            )
            _update_progress(region_code, year, progress)
            print(
                f"filling daily feed {region_code} {slice_year} "
                f"{slice_start.isoformat()}–{slice_end.isoformat()}",
                flush=True,
            )
            try:
                client.cache_region_year_checklists(
                    region_code,
                    slice_year,
                    first_day=slice_start,
                    last_day=slice_end,
                    should_stop=_stop_requested,
                )
            except Exception as exc:
                print(f"feed fill failed {slice_year}: {exc}", flush=True)
            try:
                more, trunc = missing_cached_summaries(
                    region_code,
                    slice_year,
                    start_day=slice_start.isoformat(),
                    end_day=slice_end.isoformat(),
                    loc_id=loc_id,
                    min_species=species_floor,
                    output_root=output_root,
                )
            except FileNotFoundError:
                continue
            truncated_dates.extend(trunc)
            for row in more:
                checklist_id = str(
                    row.get("subId") or row.get("subID") or ""
                ).strip()
                if not checklist_id or checklist_id in seen_ids:
                    continue
                seen_ids.add(checklist_id)
                summaries.append(row)
        summaries = _sort_summaries_for_download(summaries)
    else:
        summaries, truncated_dates = missing_cached_summaries(
            region_code,
            year,
            month=month,
            day=day,
            loc_id=loc_id,
            min_species=species_floor,
            output_root=output_root,
        )

    progress.update(
        {
            "phase": "details",
            "total_missing": len(summaries),
            "remaining": len(summaries),
        }
    )
    _update_progress(region_code, year, progress)

    remaining_by_day: Counter[str] = Counter()
    remaining_by_day_hotspot: Counter[tuple[str, str]] = Counter()
    for row in summaries:
        row_day = _checklist_obs_day(row) or "unknown-date"
        row_loc = str(row.get("locId") or row.get("locID") or "").strip() or "?"
        remaining_by_day[row_day] += 1
        remaining_by_day_hotspot[(row_day, row_loc)] += 1

    for index, summary in enumerate(summaries, start=1):
        latest_progress = load_download_progress(region_code, year)
        if latest_progress.get("stop_requested"):
            progress.update(
                {
                    "status": "stopped",
                    "processed": index - 1,
                    "downloaded": downloaded,
                    "skipped": skipped,
                    "failed": len(failures),
                    "remaining": len(summaries) - index + 1,
                    "recent_durations_seconds": recent_durations,
                    "http_429_count": int(client.http_429_count),
                    "retried_loads": retried_loads,
                    "wait_count": int(client.wait_count),
                    "wait_seconds_total": float(client.wait_seconds_total),
                    "rate_limit_events": client.rate_limit_events,
                    "finished_at": datetime.now().astimezone().isoformat(),
                }
            )
            _update_progress(region_code, year, progress)
            break
        checklist_id = str(summary.get("subId") or summary.get("subID") or "").strip()
        location_id = str(summary.get("locId") or summary.get("locID") or "").strip()
        location_name = str(summary.get("locName") or "").strip()
        obs_day = _checklist_obs_day(summary) or "unknown-date"
        loc_key = location_id or "?"
        hotspot_label = location_name or loc_key
        if location_name and location_id:
            hotspot_label = f"{location_name} ({location_id})"
        hotspot = safe_component(location_id, "unknown-location")
        if location_name:
            hotspot = f"{hotspot}__{safe_component(location_name, 'unnamed')}"
        if month is not None and len(obs_day) >= 7:
            period = obs_day[:7]
        elif len(obs_day) >= 4:
            period = obs_day[:4]
        else:
            period = str(year)
        checklist_path = (
            region_destination
            / period
            / hotspot
            / f"{safe_component(checklist_id, 'unknown')}.json"
        )
        hotspots[hotspot] += 1
        day_remaining = int(remaining_by_day[obs_day])
        day_hotspot_remaining = int(remaining_by_day_hotspot[(obs_day, loc_key)])

        item_started = time.perf_counter()
        if checklist_id in existing_ids or checklist_path.exists():
            skipped += 1
            existing_ids.add(checklist_id)
        else:
            print(
                f"loading checklist {checklist_id} date={obs_day} "
                f"hotspot={hotspot_label} "
                f"remaining_day={day_remaining} "
                f"remaining_day_hotspot={day_hotspot_remaining} "
                f"({index}/{len(summaries)})",
                flush=True,
            )
            rate_limits_before = int(client.http_429_count)
            try:
                # EBirdClient pauses for the 429 Retry-After interval.
                detail = client.checklist(checklist_id)
                save_checklist_detail(
                    region_code,
                    summary,
                    detail,
                    output_root=output_root,
                )
                downloaded += 1
                existing_ids.add(checklist_id)
            except requests.RequestException as exc:
                failures.append({"checklist_id": checklist_id, "error": str(exc)})
            if int(client.http_429_count) > rate_limits_before:
                retried_loads += 1

        remaining_by_day[obs_day] = max(0, day_remaining - 1)
        remaining_by_day_hotspot[(obs_day, loc_key)] = max(0, day_hotspot_remaining - 1)
        recent_durations.append(time.perf_counter() - item_started)
        if len(recent_durations) > 10:
            recent_durations.pop(0)
        avg = sum(recent_durations) / len(recent_durations)
        remaining = len(summaries) - index
        progress.update(
            {
                "status": "running",
                "processed": index,
                "downloaded": downloaded,
                "skipped": skipped,
                "failed": len(failures),
                "remaining": remaining,
                "recent_durations_seconds": recent_durations,
                "seconds_per_item": avg,
                "eta_seconds": avg * remaining,
                "http_429_count": int(client.http_429_count),
                "retried_loads": retried_loads,
                "wait_count": int(client.wait_count),
                "wait_seconds_total": float(client.wait_seconds_total),
                "rate_limit_events": client.rate_limit_events,
                "last_checklist_id": checklist_id,
                "last_checklist_day": obs_day,
            }
        )
        _update_progress(region_code, year, progress)
        if index % 50 == 0:
            print(
                f"processed={index}/{len(summaries)} downloaded={downloaded} "
                f"skipped={skipped} failures={len(failures)} "
                f"http_429s={client.http_429_count} retried_loads={retried_loads} "
                f"waits={client.wait_count} wait_s={client.wait_seconds_total:.0f} "
                f"eta_s={avg * remaining:.0f}",
                flush=True,
            )

    else:
        progress.update(
            {
                "status": "complete",
                "processed": len(summaries),
                "downloaded": downloaded,
                "skipped": skipped,
                "failed": len(failures),
                "remaining": 0,
                "recent_durations_seconds": recent_durations,
                "seconds_per_item": (
                    sum(recent_durations) / len(recent_durations)
                    if recent_durations
                    else None
                ),
                "eta_seconds": 0,
                "http_429_count": int(client.http_429_count),
                "retried_loads": retried_loads,
                "wait_count": int(client.wait_count),
                "wait_seconds_total": float(client.wait_seconds_total),
                "rate_limit_events": client.rate_limit_events,
                "finished_at": datetime.now().astimezone().isoformat(),
            }
        )
        _update_progress(region_code, year, progress)

    manifest = {
        "region_code": region_code,
        "year": year,
        "month": month,
        "day": (day or "").strip() or None,
        "start_day": start.isoformat() if start else None,
        "end_day": end.isoformat() if end else None,
        "prior_years": prior or None,
        "windows": format_download_windows(slices) if slices else None,
        "loc_id": (loc_id or "").strip() or None,
        "min_species": species_floor or None,
        "source": "locally cached daily checklist feeds",
        "max_calls_per_minute": MAX_CALLS_PER_MINUTE,
        "rate_limit_policy": (
            f"{MAX_CALLS_PER_MINUTE:g} calls/min via EBirdClient.get; "
            "on HTTP 429 wait 2x Retry-After, then 2x again before the next request"
        ),
        "truncated_dates": truncated_dates,
        "expected_checklists": len(summaries),
        "downloaded_this_run": downloaded,
        "skipped_existing": skipped,
        "http_429_count": int(client.http_429_count),
        "retried_loads": retried_loads,
        "wait_count": int(client.wait_count),
        "wait_seconds_total": float(client.wait_seconds_total),
        "failures": failures,
        "hotspots": dict(sorted(hotspots.items())),
    }
    manifest_dir = region_destination / str(year)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download full eBird checklists from a local regional feed cache."
    )
    parser.add_argument("--region", required=True, help="eBird region code")
    parser.add_argument(
        "--year",
        type=int,
        help="Four-digit year (required unless --dedupe-only)",
    )
    parser.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        help="Optional month; omit to download all cached dates in the year.",
    )
    parser.add_argument(
        "--day",
        help="Optional ISO day (YYYY-MM-DD) to download missing checklists for one date only.",
    )
    parser.add_argument(
        "--start-day",
        help="Inclusive start of a date window (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-day",
        help="Inclusive end of a date window (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--prior-years",
        type=int,
        default=0,
        help="Also download the same calendar window from this many previous years.",
    )
    parser.add_argument(
        "--loc-id",
        help="Optional eBird location id to download missing checklists for one hotspot only.",
    )
    parser.add_argument(
        "--min-species",
        type=int,
        default=0,
        help=(
            "Only download checklists whose feed summary numSpecies is at least "
            "this many (0 = no minimum)."
        ),
    )
    parser.add_argument(
        "--cache-feed",
        action="store_true",
        help="Fetch/update the regional daily checklist feed for the year, then exit.",
    )
    parser.add_argument(
        "--dedupe-only",
        action="store_true",
        help="Only remove duplicate on-disk checklist files, then exit.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    from cache_ship import ensure_shipped_caches_extracted

    ensure_shipped_caches_extracted()
    args = parse_args()
    if args.dedupe_only:
        result = dedupe_downloaded_checklists(args.region)
        print(
            f"dedupe unique={result['unique_checklists']} "
            f"duplicate_ids={result['duplicate_ids']} "
            f"removed={result['removed_files']} "
            f"empty_dirs={result['empty_dirs_removed']}"
        )
    elif args.cache_feed:
        if args.year is None:
            raise SystemExit("--year is required unless --dedupe-only is set")
        result = cache_year_feed(args.region, args.year)
        print(
            f"feed_cache status={result.get('status')} "
            f"days={len(result.get('daily') or {})} "
            f"unique={result.get('unique_checklist_count')} "
            f"truncated={len(result.get('truncated_dates') or [])}"
        )
    else:
        if args.year is None:
            raise SystemExit("--year is required unless --dedupe-only is set")
        result = download_cached_checklists(
            args.region,
            args.year,
            month=args.month,
            day=args.day,
            start_day=args.start_day,
            end_day=args.end_day,
            loc_id=args.loc_id,
            min_species=args.min_species,
            prior_years=args.prior_years,
        )
        print(
            f"complete expected={result['expected_checklists']} "
            f"downloaded={result['downloaded_this_run']} "
            f"skipped={result['skipped_existing']} "
            f"failures={len(result['failures'])}"
        )
