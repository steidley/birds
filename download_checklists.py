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
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ebird import EBirdClient, ROOT, region_year_checklist_cache_path

MAX_CALLS_PER_MINUTE = 37.5
REQUEST_DELAY_SECONDS = 60 / MAX_CALLS_PER_MINUTE
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


def download_progress_path(region_code: str, year: int) -> Path:
    """Location of a resumable background checklist-download progress record."""
    region = safe_component(region_code, "region")
    return ROOT / f"ebird_{region}_checklist_download_progress_{year}.json"


def feed_cache_progress_path(region_code: str, year: int) -> Path:
    """Location of resumable daily-feed cache progress."""
    region = safe_component(region_code, "region")
    return ROOT / f"ebird_{region}_feed_cache_progress_{year}.json"


def load_feed_cache_progress(region_code: str, year: int) -> dict[str, Any]:
    """Load persisted daily-feed worker state, or {}."""
    path = feed_cache_progress_path(region_code, year)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
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
    loc_id: str | None = None,
    min_species: int = 0,
    output_root: Path = ROOT / "ebird_checklists",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return feed summaries whose full detail files are not on disk."""
    summaries, truncated_dates = load_cached_summaries(region_code, year, month)
    day_key = (day or "").strip()
    location = (loc_id or "").strip()
    species_floor = max(0, int(min_species or 0))
    if day_key:
        filtered: list[dict[str, Any]] = []
        for row in summaries:
            if _checklist_obs_day(row) == day_key:
                filtered.append(row)
        summaries = filtered
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
    region_destination = output_root / safe_component(region_code, "region")
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
    output_root: Path = ROOT / "ebird_checklists",
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
    output_root: Path = ROOT / "ebird_checklists",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete duplicate on-disk checklist detail files (same subId).

    Keeps the best copy per checklist id (valid detail JSON, named hotspot
    folder, largest, newest) and removes the rest.
    """
    root = output_root
    if region_code:
        root = output_root / safe_component(region_code, "region")
    by_id: dict[str, list[Path]] = defaultdict(list)
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
    if root.exists() and not dry_run:
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
        delay_seconds=REQUEST_DELAY_SECONDS,
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
    loc_id: str | None = None,
    min_species: int = 0,
    output_root: Path = ROOT / "ebird_checklists",
) -> dict[str, Any]:
    """Download missing cached checklist details, grouped by hotspot.

    Progress is durable, so this can run in a separate process and be stopped
    safely from the cache-maintenance UI.
    """
    species_floor = max(0, int(min_species or 0))
    summaries, truncated_dates = missing_cached_summaries(
        region_code,
        year,
        month=month,
        day=day,
        loc_id=loc_id,
        min_species=species_floor,
        output_root=output_root,
    )
    period = f"{year}-{month:02d}" if month is not None else str(year)
    region_destination = output_root / safe_component(region_code, "region")
    destination = region_destination / period
    destination.mkdir(parents=True, exist_ok=True)
    # Dedupe any legacy double-writes before downloading more.
    dedupe_downloaded_checklists(region_code, output_root=output_root)
    existing_paths = {
        path.stem: path
        for path in region_destination.rglob("S*.json")
    }
    existing_ids = set(existing_paths)

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
        "loc_id": (loc_id or "").strip() or None,
        "min_species": species_floor or None,
        "status": "running",
        "pid": os.getpid(),
        "total_missing": len(summaries),
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "remaining": len(summaries),
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
        checklist_path = destination / hotspot / f"{safe_component(checklist_id, 'unknown')}.json"
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
                checklist_path.parent.mkdir(parents=True, exist_ok=True)
                checklist_path.write_text(
                    json.dumps(
                        {"feed_summary": summary, "checklist": detail},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                downloaded += 1
                existing_ids.add(checklist_id)
            except requests.RequestException as exc:
                failures.append({"checklist_id": checklist_id, "error": str(exc)})
            if int(client.http_429_count) > rate_limits_before:
                retried_loads += 1

            # Keep under 60 calls/minute. This is intentionally 37.5 RPM.
            time.sleep(REQUEST_DELAY_SECONDS)

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
        "loc_id": (loc_id or "").strip() or None,
        "min_species": species_floor or None,
        "source": "locally cached daily checklist feeds",
        "max_calls_per_minute": MAX_CALLS_PER_MINUTE,
        "rate_limit_policy": "on HTTP 429 wait 2x Retry-After, then 2x again before next request",
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
    (destination / "manifest.json").write_text(
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
            loc_id=args.loc_id,
            min_species=args.min_species,
        )
        print(
            f"complete expected={result['expected_checklists']} "
            f"downloaded={result['downloaded_this_run']} "
            f"skipped={result['skipped_existing']} "
            f"failures={len(result['failures'])}"
        )
