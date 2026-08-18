"""Resolve eBird species codes to iNaturalist taxon photos and species info.

Uses the BirdNET taxonomy API as a cross-reference (eBird code -> scientific
name/iNaturalist ID), then retrieves photos and metadata from iNaturalist /
BirdNET.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from api_log import log_api_done, log_api_send

ROOT = Path(__file__).parent
CACHE_PATH = ROOT / "inaturalist_cache.json"
GALLERY_CACHE_PATH = ROOT / "inaturalist_gallery_cache.json"
BIRDNET_API_URL = "https://birdnet.cornell.edu/taxonomy/api/species"
INAT_API_URL = "https://api.inaturalist.org/v1/taxa"
INAT_OBS_API_URL = "https://api.inaturalist.org/v1/observations"
INAT_SIMILAR_API_URL = "https://api.inaturalist.org/v1/identifications/similar_species"
GALLERY_CACHE_VERSION = 3
DEFAULT_MAX_PHOTOS = 200
SIMILAR_CACHE_PATH = ROOT / "inaturalist_similar_cache.json"


def _log_timing(label: str, started: float, **details: Any) -> None:
    """Print API/cache timing to the console for gallery performance work."""
    elapsed_ms = (time.perf_counter() - started) * 1000
    extras = " ".join(
        f"{key}={value}" for key, value in details.items() if value is not None
    )
    suffix = f" {extras}" if extras else ""
    print(f"[timing] {label}: {elapsed_ms:.0f}ms{suffix}", flush=True)


class INaturalistClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def birdnet_species(self, ebird_code: str) -> dict[str, Any] | None:
        started = time.perf_counter()
        url = f"{BIRDNET_API_URL}/{quote(ebird_code, safe='')}"
        log_api_send(
            "birdnet",
            "species by eBird code",
            url=url,
            ebird_code=ebird_code,
        )
        response = self.session.get(url, timeout=20)
        if response.status_code == 404:
            log_api_done(
                "birdnet",
                "species by eBird code",
                started=started,
                status=404,
                ebird_code=ebird_code,
            )
            return None
        response.raise_for_status()
        data = response.json()
        log_api_done(
            "birdnet",
            "species by eBird code",
            started=started,
            status=response.status_code,
            ebird_code=ebird_code,
            scientific_name=(
                (data or {}).get("scientific_name") if isinstance(data, dict) else None
            ),
        )
        return data if isinstance(data, dict) else None

    def taxon(self, taxon_id: int) -> dict[str, Any] | None:
        started = time.perf_counter()
        url = f"{INAT_API_URL}/{taxon_id}"
        log_api_send(
            "inaturalist",
            "taxon details",
            url=url,
            taxon_id=taxon_id,
        )
        response = self.session.get(url, timeout=20)
        response.raise_for_status()
        results = response.json().get("results", [])
        taxon_photos = len((results[0] or {}).get("taxon_photos") or []) if results else 0
        log_api_done(
            "inaturalist",
            "taxon details",
            started=started,
            status=response.status_code,
            taxon_id=taxon_id,
            taxon_photos=taxon_photos,
        )
        return results[0] if results else None

    def search_taxon(self, scientific_name: str) -> dict[str, Any] | None:
        started = time.perf_counter()
        params = {"q": scientific_name, "rank": "species", "per_page": 30}
        log_api_send(
            "inaturalist",
            "search taxon",
            url=INAT_API_URL,
            params=params,
            scientific_name=scientific_name,
        )
        response = self.session.get(
            INAT_API_URL,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        exact = next(
            (
                result
                for result in results
                if result.get("name", "").casefold() == scientific_name.casefold()
            ),
            None,
        )
        match = exact or (results[0] if results else None)
        log_api_done(
            "inaturalist",
            "search taxon",
            started=started,
            status=response.status_code,
            scientific_name=scientific_name,
            hits=len(results),
            matched=bool(match),
            taxon_id=(match or {}).get("id"),
        )
        return match

    def observation_photos(
        self,
        taxon_id: int,
        *,
        max_photos: int,
        seen_urls: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch research-grade observation photos for a taxon."""
        photos: list[dict[str, Any]] = []
        seen = seen_urls if seen_urls is not None else set()
        page = 1
        per_page = 100
        max_pages = max(1, (max_photos + per_page - 1) // per_page)
        total_started = time.perf_counter()

        while len(photos) < max_photos and page <= max_pages:
            page_started = time.perf_counter()
            params = {
                "taxon_id": taxon_id,
                "photos": "true",
                "quality_grade": "research",
                "per_page": per_page,
                "page": page,
                "order_by": "votes",
            }
            log_api_send(
                "inaturalist",
                "observation photos",
                url=INAT_OBS_API_URL,
                params=params,
                max_photos=max_photos,
            )
            response = self.session.get(
                INAT_OBS_API_URL,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            results = response.json().get("results") or []
            before = len(photos)
            if not results:
                log_api_done(
                    "inaturalist",
                    "observation photos",
                    started=page_started,
                    status=response.status_code,
                    taxon_id=taxon_id,
                    page=page,
                    obs=0,
                    added=0,
                )
                break
            for obs in results:
                for photo in obs.get("photos") or []:
                    entry = _photo_entry(
                        photo if isinstance(photo, dict) else {},
                        photo_kind="observation",
                    )
                    if not entry or entry["image_url"] in seen:
                        continue
                    seen.add(entry["image_url"])
                    photos.append(entry)
                    if len(photos) >= max_photos:
                        break
                if len(photos) >= max_photos:
                    break
            log_api_done(
                "inaturalist",
                "observation photos",
                started=page_started,
                status=response.status_code,
                taxon_id=taxon_id,
                page=page,
                obs=len(results),
                added=len(photos) - before,
                photos_so_far=len(photos),
            )
            if len(photos) >= max_photos:
                break
            if len(results) < per_page:
                break
            page += 1

        _log_timing(
            "inat_observations_total",
            total_started,
            taxon_id=taxon_id,
            photos=len(photos),
            pages=page,
            max_photos=max_photos,
        )
        return photos

    def similar_species(
        self,
        taxon_id: int,
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return species often confused with this taxon (iNaturalist)."""
        started = time.perf_counter()
        params = {"taxon_id": taxon_id, "per_page": limit}
        log_api_send(
            "inaturalist",
            "similar species",
            url=INAT_SIMILAR_API_URL,
            params=params,
        )
        response = self.session.get(
            INAT_SIMILAR_API_URL,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        similar: list[dict[str, Any]] = []
        for row in results:
            taxon = row.get("taxon") or {}
            if not taxon.get("id"):
                continue
            rank = str(taxon.get("rank") or "").casefold()
            if rank and rank not in {"species", "subspecies"}:
                continue
            photo = taxon.get("default_photo") or {}
            entry = _photo_entry(photo, photo_kind="taxon")
            similar.append(
                {
                    "taxon_id": taxon.get("id"),
                    "scientific_name": taxon.get("name") or "",
                    "common_name": taxon.get("preferred_common_name")
                    or taxon.get("english_common_name")
                    or taxon.get("name")
                    or "",
                    "image_url": (entry or {}).get("image_url"),
                    "attribution": (entry or {}).get("attribution")
                    or photo.get("attribution"),
                    "license": (entry or {}).get("license") or photo.get("license_code"),
                    "taxon_url": f"https://www.inaturalist.org/taxa/{taxon.get('id')}",
                    "count": row.get("count"),
                }
            )
            if len(similar) >= limit:
                break
        log_api_done(
            "inaturalist",
            "similar species",
            started=started,
            status=response.status_code,
            taxon_id=taxon_id,
            limit=limit,
            results=len(similar),
        )
        return similar


def _load_json_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def _binomial(scientific_name: str) -> str:
    parts = scientific_name.replace(",", " ").split()
    return " ".join(parts[:2]) if len(parts) >= 2 else scientific_name


def _photo_entry(
    photo: dict[str, Any],
    *,
    photo_kind: str = "taxon",
) -> dict[str, Any] | None:
    url = (
        photo.get("large_url")
        or photo.get("medium_url")
        or photo.get("original_url")
        or photo.get("url")
    )
    if not url:
        return None
    # Prefer larger variants when only a square URL is present.
    if "/square." in url:
        url = url.replace("/square.", "/large.")
    kind_label = {
        "taxon": "iNaturalist taxon photo",
        "observation": "iNaturalist observation",
        "birdnet": "BirdNET taxonomy",
    }.get(photo_kind, "iNaturalist")
    return {
        "image_url": url,
        "attribution": photo.get("attribution"),
        "license": photo.get("license_code"),
        "author": photo.get("attribution_name"),
        "source": kind_label,
        "photo_kind": photo_kind,
    }


def _photo_result(taxon: dict[str, Any], scientific_name: str) -> dict[str, Any] | None:
    photo = taxon.get("default_photo") or {}
    entry = _photo_entry(photo)
    if not entry:
        return None
    return {
        "taxon_id": taxon.get("id"),
        "scientific_name": scientific_name,
        "common_name": taxon.get("preferred_common_name") or taxon.get("name"),
        "image_url": entry["image_url"],
        "taxon_url": f"https://www.inaturalist.org/taxa/{taxon.get('id')}",
        "attribution": entry.get("attribution"),
        "license": entry.get("license"),
    }


def _resolve_taxon(
    client: INaturalistClient,
    ebird_code: str,
    scientific_name: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    birdnet = client.birdnet_species(ebird_code)
    resolved_sci = (birdnet or {}).get("scientific_name") or scientific_name
    taxon_id = (birdnet or {}).get("inat_id")

    taxon = client.taxon(int(taxon_id)) if taxon_id else None
    if taxon is None and resolved_sci:
        taxon = client.search_taxon(_binomial(str(resolved_sci)))
    return birdnet, taxon, str(resolved_sci) if resolved_sci else None


def species_photo(
    ebird_code: str,
    *,
    scientific_name: str | None = None,
) -> dict[str, Any] | None:
    """Return cached iNaturalist photo metadata for an eBird species code.

    ``scientific_name`` is an optional fallback when BirdNET does not know the
    code (common for eBird subspecies/issf forms).
    """
    started = time.perf_counter()
    cache = _load_json_cache(CACHE_PATH)
    if ebird_code in cache:
        _log_timing("species_photo", started, code=ebird_code, cache="hit")
        return cache[ebird_code] or None

    client = INaturalistClient()
    _birdnet, taxon, resolved_sci = _resolve_taxon(client, ebird_code, scientific_name)
    result = _photo_result(taxon, str(resolved_sci or "")) if taxon else None
    cache[ebird_code] = result or {}
    _save_json_cache(CACHE_PATH, cache)
    _log_timing(
        "species_photo",
        started,
        code=ebird_code,
        cache="miss",
        found=bool(result),
    )
    return result


def species_gallery(
    ebird_code: str,
    *,
    scientific_name: str | None = None,
    max_photos: int = DEFAULT_MAX_PHOTOS,
) -> dict[str, Any] | None:
    """Return gallery photos plus species info and data-source credits.

    Uses curated iNaturalist taxon photos first, then fills from research-grade
    observation photos so galleries are not stuck at the ~12 taxon-photo cap.
    """
    started = time.perf_counter()
    cache = _load_json_cache(GALLERY_CACHE_PATH)
    cached = cache.get(ebird_code)
    if cached and cached.get("cache_version") == GALLERY_CACHE_VERSION:
        cached_max = int(cached.get("max_photos") or 0)
        if cached_max >= max_photos:
            _log_timing(
                "species_gallery",
                started,
                code=ebird_code,
                cache="hit",
                photos=len(cached.get("photos") or []),
            )
            return cached or None

    client = INaturalistClient()
    birdnet, taxon, resolved_sci = _resolve_taxon(client, ebird_code, scientific_name)
    if taxon is None and birdnet is None:
        cache[ebird_code] = {}
        _save_json_cache(GALLERY_CACHE_PATH, cache)
        _log_timing("species_gallery", started, code=ebird_code, cache="miss", found=False)
        return None

    photos: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    used_observations = False
    if taxon:
        # Curated taxon photos first, then research-grade observation photos.
        for item in taxon.get("taxon_photos") or []:
            entry = _photo_entry(item.get("photo") or {}, photo_kind="taxon")
            if not entry or entry["image_url"] in seen_urls:
                continue
            seen_urls.add(entry["image_url"])
            photos.append(entry)
            if len(photos) >= max_photos:
                break
        if not photos:
            entry = _photo_entry(taxon.get("default_photo") or {}, photo_kind="taxon")
            if entry:
                seen_urls.add(entry["image_url"])
                photos.append(entry)

        taxon_id = taxon.get("id")
        if taxon_id and len(photos) < max_photos:
            extra = client.observation_photos(
                int(taxon_id),
                max_photos=max_photos - len(photos),
                seen_urls=seen_urls,
            )
            if extra:
                used_observations = True
                photos.extend(extra)

    # Fall back to BirdNET-selected image when iNat taxon photos are empty.
    if not photos and birdnet and isinstance(birdnet.get("image"), dict):
        image = birdnet["image"]
        url = image.get("src") or image.get("medium")
        if url:
            photos.append(
                {
                    "image_url": url,
                    "attribution": image.get("author"),
                    "license": image.get("license"),
                    "author": image.get("author"),
                    "source": image.get("source") or "BirdNET taxonomy",
                    "photo_kind": "birdnet",
                }
            )

    description = ""
    description_source = None
    if birdnet:
        descriptions = birdnet.get("descriptions") or {}
        description = descriptions.get("en") or ""
        sources = birdnet.get("description_sources") or {}
        description_source = sources.get("en") or birdnet.get("description_source")

    sources: list[str] = []
    if birdnet:
        sources.append("BirdNET taxonomy (eBird code / scientific name crosswalk)")
    if taxon:
        sources.append("iNaturalist taxa API (curated taxon photos, common name)")
    if used_observations:
        sources.append("iNaturalist observations API (research-grade photos)")
    if description_source:
        sources.append(f"Description: {description_source}")
    if birdnet and birdnet.get("ebird_code"):
        sources.append("eBird species code via BirdNET")
    if birdnet and birdnet.get("image", {}).get("source"):
        sources.append(f"Primary image selection: {birdnet['image']['source']}")

    # Deduplicate while preserving order.
    unique_sources: list[str] = []
    for source in sources:
        if source not in unique_sources:
            unique_sources.append(source)

    common_name = None
    if taxon:
        common_name = taxon.get("preferred_common_name") or taxon.get("name")
    if not common_name and birdnet:
        common_name = birdnet.get("common_name")

    taxon_id = taxon.get("id") if taxon else (birdnet or {}).get("inat_id")
    result = {
        "ebird_code": (birdnet or {}).get("ebird_code") or ebird_code,
        "taxon_id": taxon_id,
        "scientific_name": resolved_sci
        or (taxon or {}).get("name")
        or scientific_name
        or "",
        "common_name": common_name or "",
        "description": description,
        "description_source": description_source,
        "taxon_url": f"https://www.inaturalist.org/taxa/{taxon_id}" if taxon_id else None,
        "ebird_url": f"https://ebird.org/species/{(birdnet or {}).get('ebird_code') or ebird_code}",
        "birdnet_url": (
            f"{BIRDNET_API_URL}/{quote((birdnet or {}).get('scientific_name') or resolved_sci or ebird_code, safe='')}"
            if birdnet or resolved_sci
            else None
        ),
        "photos": photos,
        "max_photos": max_photos,
        "cache_version": GALLERY_CACHE_VERSION,
        "sources": unique_sources,
        "wikipedia_url": ((birdnet or {}).get("wikipedia_urls") or {}).get("en"),
    }
    cache[ebird_code] = result
    _save_json_cache(GALLERY_CACHE_PATH, cache)
    _log_timing(
        "species_gallery",
        started,
        code=ebird_code,
        cache="miss",
        photos=len(photos),
        used_observations=used_observations,
        max_photos=max_photos,
    )
    return result


def species_similar(
    *,
    taxon_id: int | None = None,
    ebird_code: str | None = None,
    scientific_name: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return similar species for a taxon, with thumbnail metadata when available."""
    started = time.perf_counter()
    cache_key = (
        f"taxon:{taxon_id}"
        if taxon_id
        else f"code:{ebird_code or ''}|sci:{scientific_name or ''}"
    )
    cache = _load_json_cache(SIMILAR_CACHE_PATH)
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        _log_timing(
            "species_similar",
            started,
            cache="hit",
            key=cache_key,
            results=len(cached),
        )
        return cached[:limit]

    client = INaturalistClient()
    resolved_taxon_id = taxon_id
    if resolved_taxon_id is None:
        birdnet, taxon, _resolved = _resolve_taxon(
            client,
            ebird_code or scientific_name or "",
            scientific_name,
        )
        resolved_taxon_id = (taxon or {}).get("id") or (birdnet or {}).get("inat_id")

    if not resolved_taxon_id:
        cache[cache_key] = []
        _save_json_cache(SIMILAR_CACHE_PATH, cache)
        _log_timing("species_similar", started, cache="miss", key=cache_key, results=0)
        return []

    similar = client.similar_species(int(resolved_taxon_id), limit=limit)
    cache[cache_key] = similar
    # Also store under taxon id for reuse across code/sci lookups.
    cache[f"taxon:{resolved_taxon_id}"] = similar
    _save_json_cache(SIMILAR_CACHE_PATH, cache)
    _log_timing(
        "species_similar",
        started,
        cache="miss",
        key=cache_key,
        taxon_id=resolved_taxon_id,
        results=len(similar),
    )
    return similar
