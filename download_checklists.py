"""Download cached eBird regional checklist feeds into full checklist files.

Example:
    .venv/bin/python download_checklists.py --region US-FL-099 --year 2026
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

from ebird import EBirdClient, ROOT, region_year_checklist_cache_path

MAX_CALLS_PER_MINUTE = 37.5
REQUEST_DELAY_SECONDS = 60 / MAX_CALLS_PER_MINUTE
RATE_LIMIT_PAUSE_SECONDS = 10 * 60


def safe_component(value: object, fallback: str) -> str:
    """Return a filesystem-safe directory or filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or fallback


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
    return [summaries[key] for key in sorted(summaries)], truncated


def download_cached_checklists(
    region_code: str,
    year: int,
    *,
    month: int | None = None,
    output_root: Path = ROOT / "ebird_checklists",
) -> dict[str, Any]:
    """Download available cached checklist details, grouped by hotspot."""
    summaries, truncated_dates = load_cached_summaries(region_code, year, month)
    period = f"{year}-{month:02d}" if month is not None else str(year)
    region_destination = output_root / safe_component(region_code, "region")
    destination = region_destination / period
    destination.mkdir(parents=True, exist_ok=True)
    # Earlier runs used a month directory; never download an existing ID again.
    existing_ids = {path.stem for path in region_destination.rglob("S*.json")}

    client = EBirdClient(min_rate_limit_wait_seconds=RATE_LIMIT_PAUSE_SECONDS)
    downloaded = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    hotspots: dict[str, int] = defaultdict(int)

    for index, summary in enumerate(summaries, start=1):
        checklist_id = str(summary.get("subId") or summary.get("subID") or "").strip()
        location_id = str(summary.get("locId") or summary.get("locID") or "").strip()
        location_name = str(summary.get("locName") or "").strip()
        hotspot = safe_component(location_id, "unknown-location")
        if location_name:
            hotspot = f"{hotspot}__{safe_component(location_name, 'unnamed')}"
        checklist_path = destination / hotspot / f"{safe_component(checklist_id, 'unknown')}.json"
        hotspots[hotspot] += 1

        if checklist_id in existing_ids or checklist_path.exists():
            skipped += 1
            continue
        try:
            # EBirdClient pauses for at least ten minutes on HTTP 429.
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

        time.sleep(REQUEST_DELAY_SECONDS)
        if index % 50 == 0:
            print(
                f"processed={index}/{len(summaries)} downloaded={downloaded} "
                f"skipped={skipped} failures={len(failures)} "
                f"rate_limits={len(client.rate_limit_events)}",
                flush=True,
            )

    manifest = {
        "region_code": region_code,
        "year": year,
        "month": month,
        "source": "locally cached daily checklist feeds",
        "max_calls_per_minute": MAX_CALLS_PER_MINUTE,
        "rate_limit_pause_seconds": RATE_LIMIT_PAUSE_SECONDS,
        "truncated_dates": truncated_dates,
        "expected_checklists": len(summaries),
        "downloaded_this_run": downloaded,
        "skipped_existing": skipped,
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
    parser.add_argument("--year", required=True, type=int, help="Four-digit year")
    parser.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        help="Optional month; omit to download all cached dates in the year.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = download_cached_checklists(args.region, args.year, month=args.month)
    print(
        f"complete expected={result['expected_checklists']} "
        f"downloaded={result['downloaded_this_run']} "
        f"skipped={result['skipped_existing']} "
        f"failures={len(result['failures'])}"
    )
