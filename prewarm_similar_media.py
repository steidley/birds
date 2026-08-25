"""Prewarm iNaturalist similar/photo/gallery caches for the default region.

Fetches similar-species lists for every historical species in the home region,
then fills photo and gallery caches for the unique birds that appear in those
lists. The JSON files are meant to be committed so a fresh deploy can render
similar birds without live API calls.

Example:
    .venv/bin/python prewarm_similar_media.py
    .venv/bin/python prewarm_similar_media.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from ebird import CONFIG_DIR, ROOT

load_dotenv(CONFIG_DIR / ".env")
load_dotenv(ROOT / ".env")  # legacy root .env if present

from ebird import (
    EBirdClient,
    load_cached_taxa,
    load_disk_region_species_birds,
    resolve_ebird_code,
)
from inaturalist import (
    CACHE_PATH as PHOTO_CACHE_PATH,
    DEFAULT_MAX_PHOTOS,
    GALLERY_CACHE_PATH,
    GALLERY_CACHE_VERSION,
    SIMILAR_CACHE_PATH,
    species_gallery,
    species_photo,
    species_similar,
)

DEFAULT_REGION = os.environ.get("EBIRD_HOME_REGION", "US-FL-099")


def _load_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _gallery_by_code(gallery_cache: dict) -> dict[str, dict]:
    by_code: dict[str, dict] = {}
    for key, value in gallery_cache.items():
        if not isinstance(value, dict) or not value:
            continue
        by_code.setdefault(str(key), value)
        nested = str(value.get("ebird_code") or "").strip()
        if nested:
            by_code.setdefault(nested, value)
    return by_code


def ensure_region_species_codes(region: str) -> list[str]:
    client = EBirdClient()
    codes = client.region_species_codes(region)
    if load_disk_region_species_birds(region) is None and codes:
        print(f"Caching named species list for {region}…", flush=True)
        client.region_species_birds(region)
    return [str(item) for item in codes if item]


def _similar_list_for_code(
    code: str,
    *,
    similar_cache: dict,
    gallery_by_code: dict[str, dict],
) -> list | None:
    for key, value in similar_cache.items():
        text = str(key)
        if text.startswith(f"code:{code}|") and isinstance(value, list):
            return value
    taxon_id = (gallery_by_code.get(code) or {}).get("taxon_id")
    if taxon_id is None:
        return None
    cached = similar_cache.get(f"taxon:{taxon_id}")
    return cached if isinstance(cached, list) else None


def region_similar_result_species(region: str) -> list[dict[str, Any]]:
    """Unique similar-species *results* for birds recorded in the region."""
    codes = ensure_region_species_codes(region)
    similar_cache = _load_json_object(SIMILAR_CACHE_PATH)
    gallery_by_code = _gallery_by_code(_load_json_object(GALLERY_CACHE_PATH))
    unique: dict[str, dict[str, Any]] = {}
    for code in codes:
        items = _similar_list_for_code(
            code, similar_cache=similar_cache, gallery_by_code=gallery_by_code
        )
        if not items:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            taxon_id = item.get("taxon_id")
            sci = str(item.get("scientific_name") or "").strip()
            common = str(item.get("common_name") or "").strip()
            key = str(taxon_id) if taxon_id is not None else sci.casefold()
            if not key or key in unique:
                continue
            unique[key] = {
                "taxon_id": taxon_id,
                "code": "",
                "name": common or sci or "Unknown",
                "sciName": sci,
            }
    for bird in unique.values():
        code = resolve_ebird_code(
            scientific_name=bird["sciName"] or None,
            common_name=bird["name"] or None,
            local_only=True,
        )
        bird["code"] = str(code or "").strip()
    return list(unique.values())


def _gallery_cache_current_keys() -> tuple[set[str], set[str], set[str]]:
    gallery_cache = _load_json_object(GALLERY_CACHE_PATH)
    codes: set[str] = set()
    taxon_ids: set[str] = set()
    sci_names: set[str] = set()
    for key, value in gallery_cache.items():
        if not isinstance(value, dict) or not value:
            continue
        if value.get("cache_version") != GALLERY_CACHE_VERSION:
            continue
        text_key = str(key).strip()
        if text_key:
            codes.add(text_key)
        nested = str(value.get("ebird_code") or "").strip()
        if nested:
            codes.add(nested)
        taxon_id = value.get("taxon_id")
        if taxon_id is not None:
            taxon_ids.add(str(taxon_id))
        sci = str(value.get("scientific_name") or "").strip().casefold()
        if sci:
            sci_names.add(sci)
    return codes, taxon_ids, sci_names


def _has_gallery(
    bird: dict,
    *,
    gallery_codes: set[str],
    gallery_taxon_ids: set[str],
    gallery_sci_names: set[str],
) -> bool:
    code = str(bird.get("code") or "").strip()
    if code and code in gallery_codes:
        return True
    taxon_id = bird.get("taxon_id")
    if taxon_id is not None and str(taxon_id) in gallery_taxon_ids:
        return True
    sci = str(bird.get("sciName") or "").strip().casefold()
    return bool(sci and sci in gallery_sci_names)


def missing_region_similar_codes(region: str) -> list[str]:
    codes = ensure_region_species_codes(region)
    similar_cache = _load_json_object(SIMILAR_CACHE_PATH)
    gallery_by_code = _gallery_by_code(_load_json_object(GALLERY_CACHE_PATH))
    missing: list[str] = []
    for code in codes:
        if _similar_list_for_code(
            code, similar_cache=similar_cache, gallery_by_code=gallery_by_code
        ) is None:
            missing.append(code)
    return missing


def missing_similar_result_lookups(
    birds: list[dict],
    *,
    kind: str,
) -> list[tuple[str, str | None]]:
    photo_cache = _load_json_object(PHOTO_CACHE_PATH)
    gallery_codes, gallery_taxon_ids, gallery_sci_names = _gallery_cache_current_keys()
    missing: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for bird in birds:
        code = str(bird.get("code") or "").strip()
        sci = str(bird.get("sciName") or "").strip() or None
        lookup = code or sci or ""
        if not lookup or lookup in seen:
            continue
        if kind == "photo":
            if code and code in photo_cache:
                continue
            if not code:
                continue
        elif kind == "gallery":
            if _has_gallery(
                bird,
                gallery_codes=gallery_codes,
                gallery_taxon_ids=gallery_taxon_ids,
                gallery_sci_names=gallery_sci_names,
            ):
                continue
        seen.add(lookup)
        missing.append((lookup, sci))
    return missing


def _format_eta(seconds: float) -> str:
    remaining = max(0, int(seconds))
    hours, rem = divmod(remaining, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _call_with_retry(worker: Callable[[], bool], *, label: str) -> bool:
    delay = 5.0
    for attempt in range(1, 6):
        try:
            return worker()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {429, 500, 502, 503, 504} or attempt == 5:
                print(f"  error {label}: {exc}", flush=True)
                return False
            print(
                f"  {status} on {label}; retry {attempt}/5 in {delay:.0f}s",
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except requests.RequestException as exc:
            if attempt == 5:
                print(f"  error {label}: {exc}", flush=True)
                return False
            print(f"  network error on {label}; retry {attempt}/5 in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return False


def warm_lookups(
    items: list[tuple[str, str | None]],
    *,
    label: str,
    worker,
) -> dict[str, int]:
    total = len(items)
    if total == 0:
        print(f"{label}: nothing missing", flush=True)
        return {"missing": 0, "attempted": 0, "found": 0}
    found = 0
    recent: list[float] = []
    for index, (lookup, sci) in enumerate(items, start=1):
        started = time.perf_counter()
        ok = _call_with_retry(
            lambda lookup=lookup, sci=sci: bool(worker(lookup, sci)),
            label=lookup,
        )
        if ok:
            found += 1
        recent.append(time.perf_counter() - started)
        if len(recent) > 10:
            recent.pop(0)
        avg = sum(recent) / len(recent)
        eta = _format_eta(avg * (total - index)) if index < total else "done"
        print(
            f"{label} {index:,}/{total:,} {lookup} "
            f"{'ok' if ok else 'miss'} · ETA {eta} · {avg:.1f}s/bird",
            flush=True,
        )
    print(f"{label}: found {found:,}/{total:,}", flush=True)
    return {"missing": total, "attempted": total, "found": found}


def print_status(region: str) -> None:
    similar_missing = missing_region_similar_codes(region)
    birds = region_similar_result_species(region)
    photo_missing = missing_similar_result_lookups(birds, kind="photo")
    gallery_missing = missing_similar_result_lookups(birds, kind="gallery")
    unresolved = sum(1 for bird in birds if not bird.get("code"))
    print(f"Region {region}")
    print(f"  historical species: {len(ensure_region_species_codes(region)):,}")
    print(f"  similar-list missing: {len(similar_missing):,}")
    print(f"  unique similar-result birds: {len(birds):,} ({unresolved:,} without eBird code)")
    print(f"  similar-result photos missing: {len(photo_missing):,}")
    print(f"  similar-result galleries missing: {len(gallery_missing):,}")


def warm_region(region: str, *, max_photos: int) -> None:
    taxa = load_cached_taxa(ensure_region_species_codes(region))

    def _sci_for(code: str) -> str | None:
        return str((taxa.get(code) or {}).get("sciName") or "").strip() or None

    similar_missing = missing_region_similar_codes(region)
    print(f"Warming similar lists for {len(similar_missing):,} {region} species…", flush=True)

    def similar_worker(code: str, sci: str | None) -> bool:
        taxon_id = None
        gallery = _gallery_by_code(_load_json_object(GALLERY_CACHE_PATH)).get(code) or {}
        raw_id = gallery.get("taxon_id")
        if raw_id is not None:
            try:
                taxon_id = int(raw_id)
            except (TypeError, ValueError):
                taxon_id = None
        similar = species_similar(
            taxon_id=taxon_id,
            ebird_code=code,
            scientific_name=sci or _sci_for(code),
            limit=12,
        )
        return bool(similar)

    warm_lookups(
        [(code, _sci_for(code)) for code in similar_missing],
        label="similar list",
        worker=similar_worker,
    )

    birds = region_similar_result_species(region)
    photo_missing = missing_similar_result_lookups(birds, kind="photo")
    print(f"Warming photos for {len(photo_missing):,} similar-result birds…", flush=True)
    warm_lookups(
        photo_missing,
        label="similar photo",
        worker=lambda code, sci: bool(species_photo(code, scientific_name=sci)),
    )

    gallery_missing = missing_similar_result_lookups(birds, kind="gallery")
    print(
        f"Warming galleries for {len(gallery_missing):,} similar-result birds "
        f"(max_photos={max_photos})…",
        flush=True,
    )
    warm_lookups(
        gallery_missing,
        label="similar gallery",
        worker=lambda code, sci: bool(
            species_gallery(code, scientific_name=sci, max_photos=max_photos)
        ),
    )
    print_status(region)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prewarm similar-bird photo and gallery caches for the home region."
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"eBird region code (default {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print coverage and exit without fetching.",
    )
    parser.add_argument(
        "--max-photos",
        type=int,
        default=DEFAULT_MAX_PHOTOS,
        help=f"Gallery photo cap to cache (default {DEFAULT_MAX_PHOTOS})",
    )
    args = parser.parse_args()
    region = str(args.region or DEFAULT_REGION).strip()
    if args.status:
        print_status(region)
        return
    warm_region(region, max_photos=max(1, int(args.max_photos)))


if __name__ == "__main__":
    main()
