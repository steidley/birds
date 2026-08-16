"""Resolve eBird species codes to iNaturalist taxon photos and species info.

Uses the BirdNET taxonomy API as a cross-reference (eBird code -> scientific
name/iNaturalist ID), then retrieves photos and metadata from iNaturalist /
BirdNET.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

ROOT = Path(__file__).parent
CACHE_PATH = ROOT / "inaturalist_cache.json"
GALLERY_CACHE_PATH = ROOT / "inaturalist_gallery_cache.json"
BIRDNET_API_URL = "https://birdnet.cornell.edu/taxonomy/api/species"
INAT_API_URL = "https://api.inaturalist.org/v1/taxa"


class INaturalistClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def birdnet_species(self, ebird_code: str) -> dict[str, Any] | None:
        response = self.session.get(
            f"{BIRDNET_API_URL}/{quote(ebird_code, safe='')}",
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None

    def taxon(self, taxon_id: int) -> dict[str, Any] | None:
        response = self.session.get(f"{INAT_API_URL}/{taxon_id}", timeout=20)
        response.raise_for_status()
        results = response.json().get("results", [])
        return results[0] if results else None

    def search_taxon(self, scientific_name: str) -> dict[str, Any] | None:
        response = self.session.get(
            INAT_API_URL,
            params={"q": scientific_name, "rank": "species", "per_page": 30},
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
        return exact or (results[0] if results else None)


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


def _photo_entry(photo: dict[str, Any]) -> dict[str, Any] | None:
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
    return {
        "image_url": url,
        "attribution": photo.get("attribution"),
        "license": photo.get("license_code"),
        "author": photo.get("attribution_name"),
        "source": "iNaturalist",
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
    cache = _load_json_cache(CACHE_PATH)
    if ebird_code in cache:
        return cache[ebird_code] or None

    client = INaturalistClient()
    _birdnet, taxon, resolved_sci = _resolve_taxon(client, ebird_code, scientific_name)
    result = _photo_result(taxon, str(resolved_sci or "")) if taxon else None
    cache[ebird_code] = result or {}
    _save_json_cache(CACHE_PATH, cache)
    return result


def species_gallery(
    ebird_code: str,
    *,
    scientific_name: str | None = None,
    max_photos: int = 99,
) -> dict[str, Any] | None:
    """Return gallery photos plus species info and data-source credits."""
    cache = _load_json_cache(GALLERY_CACHE_PATH)
    cached = cache.get(ebird_code)
    if cached:
        cached_max = int(cached.get("max_photos") or 0)
        # Re-fetch when a higher photo limit is requested than what we stored.
        if cached_max >= max_photos or not cached.get("photos"):
            return cached or None

    client = INaturalistClient()
    birdnet, taxon, resolved_sci = _resolve_taxon(client, ebird_code, scientific_name)
    if taxon is None and birdnet is None:
        cache[ebird_code] = {}
        _save_json_cache(GALLERY_CACHE_PATH, cache)
        return None

    photos: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    if taxon:
        for item in taxon.get("taxon_photos") or []:
            entry = _photo_entry(item.get("photo") or {})
            if not entry or entry["image_url"] in seen_urls:
                continue
            seen_urls.add(entry["image_url"])
            photos.append(entry)
            if len(photos) >= max_photos:
                break
        if not photos:
            entry = _photo_entry(taxon.get("default_photo") or {})
            if entry:
                photos.append(entry)

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
        sources.append("iNaturalist taxa API (photos, common name)")
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
        "sources": unique_sources,
        "wikipedia_url": ((birdnet or {}).get("wikipedia_urls") or {}).get("en"),
    }
    cache[ebird_code] = result
    _save_json_cache(GALLERY_CACHE_PATH, cache)
    return result
