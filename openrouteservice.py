"""OpenRouteService client (directions / matrix) for travel times to hotspots."""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from api_log import log_api_done, log_api_send
from ebird import CACHE_SHARED_DIR, CONFIG_DIR, ROOT

load_dotenv(CONFIG_DIR / ".env")
load_dotenv(ROOT / ".env")

# HeiGIT migrated ORS off api.openrouteservice.org.
ORS_BASE_URL = "https://api.heigit.org/openrouteservice/v2"
PELIAS_BASE_URL = "https://api.heigit.org/pelias/v1"
DRIVE_METRICS_CACHE_PATH = CACHE_SHARED_DIR / "ors_drive_matrix_cache.json"
DRIVE_METRICS_CACHE_VERSION = 1
RECENT_LOCATIONS_PATH = CONFIG_DIR / "recent_locations.json"
RECENT_LOCATIONS_LIMIT = 20
RECENT_LOCATION_COORD_DECIMALS = 5
ORS_PROFILES = (
    "driving-car",
    "driving-hgv",
    "cycling-regular",
    "cycling-road",
    "foot-walking",
)


def get_ors_api_key() -> str | None:
    """Return the OpenRouteService API key from env or Streamlit secrets."""
    for name in (
        "OPENROUTESERVICE_API_KEY",
        "ORS_API_KEY",
        "OPENROUTE_API_KEY",
    ):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is not None:
            for name in (
                "OPENROUTESERVICE_API_KEY",
                "ORS_API_KEY",
                "OPENROUTE_API_KEY",
            ):
                if name in secrets:
                    value = str(secrets[name] or "").strip()
                    if value:
                        return value
    except Exception:
        pass
    return None


def configured_home_coordinates() -> tuple[float, float] | None:
    """Home lat/lng used as the travel-time origin (env ``ORS_HOME_*``)."""
    try:
        lat = float(str(os.environ.get("ORS_HOME_LAT") or "").strip())
        lng = float(str(os.environ.get("ORS_HOME_LNG") or "").strip())
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    return (lat, lng)


def configured_home_address() -> str | None:
    """Optional human-readable home address (env ``ORS_HOME_ADDRESS``)."""
    value = str(os.environ.get("ORS_HOME_ADDRESS") or "").strip()
    return value or None


def _env_escape(value: str) -> str:
    """Quote an env value when it contains spaces or special characters."""
    text = str(value)
    if text == "":
        return ""
    if any(character in text for character in ' \t\n\r"\'#\\='):
        return json.dumps(text, ensure_ascii=False)
    return text


def _update_env_values(updates: dict[str, str]) -> None:
    """Merge keys into ``config/.env`` and ``os.environ``."""
    for key, value in updates.items():
        os.environ[key] = value
    env_path = CONFIG_DIR / ".env"
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rewritten.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            rewritten.append(f"{key}={_env_escape(updates[key])}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={_env_escape(value)}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(rewritten).rstrip() + "\n"
    env_path.write_text(text, encoding="utf-8")


def persist_home_coordinates(lat: float, lng: float) -> tuple[float, float]:
    """Update the travel-time origin in-process and in ``config/.env``."""
    return persist_home_location(lat, lng)[:2]


def persist_home_location(
    lat: float,
    lng: float,
    *,
    address: str | None = None,
) -> tuple[float, float, str | None]:
    """Update home lat/lng (and optional address) in-process and in ``config/.env``."""
    lat_f = float(lat)
    lng_f = float(lng)
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        raise ValueError("Latitude/longitude out of range.")
    lat_s = f"{lat_f:.7f}".rstrip("0").rstrip(".")
    lng_s = f"{lng_f:.7f}".rstrip("0").rstrip(".")
    updates = {
        "ORS_HOME_LAT": lat_s,
        "ORS_HOME_LNG": lng_s,
    }
    address_value: str | None = None
    if address is not None:
        address_value = str(address).strip() or None
        updates["ORS_HOME_ADDRESS"] = address_value or ""
    _update_env_values(updates)
    remember_recent_location(
        lat_f,
        lng_f,
        address=address_value if address is not None else None,
    )
    return (lat_f, lng_f, configured_home_address())


def recent_location_coord_key(lat: float, lng: float) -> str:
    """Stable key for distinct locations (≈1 m at the equator)."""
    return (
        f"{float(lat):.{RECENT_LOCATION_COORD_DECIMALS}f},"
        f"{float(lng):.{RECENT_LOCATION_COORD_DECIMALS}f}"
    )


def recent_location_display_name(entry: dict[str, Any]) -> str:
    """Preferred label: saved name, then address, then coordinates."""
    name = str(entry.get("name") or "").strip()
    if name:
        return name
    address = str(entry.get("address") or "").strip()
    if address:
        return address
    try:
        return format_home_coordinates(
            (float(entry["lat"]), float(entry["lng"]))
        )
    except (KeyError, TypeError, ValueError):
        return "Saved location"


def load_recent_locations() -> list[dict[str, Any]]:
    """Load distinct recently used home locations (most recent first)."""
    try:
        raw = json.loads(RECENT_LOCATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("locations") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            lat = float(row["lat"])
            lng = float(row["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            continue
        key = recent_location_coord_key(lat, lng)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": key,
                "lat": lat,
                "lng": lng,
                "name": str(row.get("name") or "").strip(),
                "address": str(row.get("address") or "").strip(),
                "used_at": str(row.get("used_at") or "").strip(),
            }
        )
        if len(out) >= RECENT_LOCATIONS_LIMIT:
            break
    return out


def save_recent_locations(locations: list[dict[str, Any]]) -> None:
    """Write the recent-locations list to ``config/recent_locations.json``."""
    payload = {
        "locations": [
            {
                "id": str(row.get("id") or recent_location_coord_key(row["lat"], row["lng"])),
                "lat": float(row["lat"]),
                "lng": float(row["lng"]),
                "name": str(row.get("name") or "").strip(),
                "address": str(row.get("address") or "").strip(),
                "used_at": str(row.get("used_at") or "").strip(),
            }
            for row in locations[:RECENT_LOCATIONS_LIMIT]
        ]
    }
    RECENT_LOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECENT_LOCATIONS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def remember_recent_location(
    lat: float,
    lng: float,
    *,
    address: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Upsert a location at the front of the recent list (distinct by coords)."""
    lat_f = float(lat)
    lng_f = float(lng)
    key = recent_location_coord_key(lat_f, lng_f)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    existing = load_recent_locations()
    prior = next((row for row in existing if row.get("id") == key), None)
    if name is not None:
        saved_name = str(name).strip()
    elif prior:
        saved_name = str(prior.get("name") or "").strip()
    else:
        saved_name = ""
    if address is not None:
        saved_address = str(address).strip()
    elif prior:
        saved_address = str(prior.get("address") or "").strip()
    else:
        saved_address = ""
    entry = {
        "id": key,
        "lat": lat_f,
        "lng": lng_f,
        "name": saved_name,
        "address": saved_address,
        "used_at": now,
    }
    merged = [entry] + [row for row in existing if row.get("id") != key]
    save_recent_locations(merged[:RECENT_LOCATIONS_LIMIT])
    return merged[:RECENT_LOCATIONS_LIMIT]


def rename_recent_location(location_id: str, name: str) -> dict[str, Any] | None:
    """Set or clear the saved display name for a recent location."""
    key = str(location_id or "").strip()
    if not key:
        return None
    label = str(name or "").strip()
    rows = load_recent_locations()
    updated: dict[str, Any] | None = None
    for row in rows:
        if row.get("id") != key:
            continue
        row["name"] = label
        updated = row
        break
    if updated is None:
        return None
    save_recent_locations(rows)
    home = configured_home_coordinates()
    if home is not None and recent_location_coord_key(*home) == key:
        # Keep current origin caption in sync with the list label.
        display = label or str(updated.get("address") or "").strip()
        _update_env_values({"ORS_HOME_ADDRESS": display})
    return updated


def delete_recent_location(location_id: str) -> bool:
    """Remove a location from the recent list. Does not clear the current home."""
    key = str(location_id or "").strip()
    if not key:
        return False
    rows = load_recent_locations()
    kept = [row for row in rows if row.get("id") != key]
    if len(kept) == len(rows):
        return False
    save_recent_locations(kept)
    return True


def format_home_coordinates(coords: tuple[float, float] | None) -> str:
    if not coords:
        return "not set"
    lat, lng = coords
    return f"{lat:.5f}, {lng:.5f}"


def _coords_lon_lat(lat: float, lng: float) -> list[float]:
    # ORS expects [longitude, latitude].
    return [float(lng), float(lat)]


def _feature_label(properties: dict[str, Any]) -> str:
    for key in ("label", "name"):
        value = str(properties.get(key) or "").strip()
        if value:
            return value
    parts = [
        str(properties.get(part) or "").strip()
        for part in (
            "housenumber",
            "street",
            "locality",
            "region",
            "postalcode",
            "country",
        )
    ]
    return ", ".join(part for part in parts if part) or "Matched location"


_HOUSE_NUMBER_RE = re.compile(
    r"^\s*(\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\b",
)
_POSTAL_CA_RE = re.compile(
    r"\b([A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d)\b",
)
_STREET_ABBREV = (
    (re.compile(r"\bBlvd\.?", re.I), "Boulevard"),
    (re.compile(r"\bAve\.?", re.I), "Avenue"),
    (re.compile(r"\bRd\.?", re.I), "Road"),
    (re.compile(r"\bSt\.?(?=\s|,|$)", re.I), "Street"),
    (re.compile(r"\bDr\.?", re.I), "Drive"),
    (re.compile(r"\bLn\.?", re.I), "Lane"),
    (re.compile(r"\bHwy\.?", re.I), "Highway"),
)


def normalize_geocode_query(text: str) -> str:
    """Expand common street abbreviations for Pelias matching."""
    query = str(text or "").strip()
    if not query:
        return ""
    for pattern, replacement in _STREET_ABBREV:
        query = pattern.sub(replacement, query)
    return re.sub(r"\s+", " ", query).strip()


def parse_address_housenumber(text: str) -> int | None:
    """Leading house number from an address string, if present."""
    match = _HOUSE_NUMBER_RE.match(str(text or ""))
    if not match:
        return None
    raw = match.group(1)
    digits = re.match(r"(\d+)", raw)
    if not digits:
        return None
    try:
        return int(digits.group(1))
    except ValueError:
        return None


def parse_canadian_postal_code(text: str) -> str | None:
    match = _POSTAL_CA_RE.search(str(text or ""))
    if not match:
        return None
    raw = match.group(1).upper().replace(" ", "")
    if len(raw) != 6:
        return None
    return f"{raw[:3]} {raw[3:]}"


def _housenumber_int(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.match(r"(\d+)", raw)
    if not digits:
        return None
    try:
        return int(digits.group(1))
    except ValueError:
        return None


def _street_tail_from_query(query: str, housenumber: int | None) -> str:
    text = str(query or "").strip()
    if housenumber is None:
        return text
    return _HOUSE_NUMBER_RE.sub("", text, count=1).strip(" ,")


def _label_looks_like_street(label: str, street_hint: str) -> bool:
    """Loose check that a geocode label is on the requested street."""
    hay = str(label or "").casefold()
    hint = str(street_hint or "").casefold()
    if not hay or not hint:
        return False
    # Compare the first street-ish token chunk before city separators.
    hint_core = re.split(r"[,]", hint, maxsplit=1)[0]
    hint_core = re.sub(
        r"\b(boulevard|avenue|street|road|drive|lane|ouest|est|west|east|north|south|n|s|e|w|o)\b",
        " ",
        hint_core,
    )
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", hint_core) if len(tok) >= 4]
    if not tokens:
        return "maisonneuve" in hay or hint_core.strip()[:12] in hay
    return any(tok in hay for tok in tokens[:3])


def _parse_geocode_features(payload: Any) -> list[dict[str, Any]]:
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return []
    results: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            continue
        coords = geometry.get("coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            lng = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            continue
        housenumber = str(properties.get("housenumber") or "").strip() or None
        results.append(
            {
                "lat": lat,
                "lng": lng,
                "label": _feature_label(properties),
                "confidence": properties.get("confidence"),
                "layer": str(properties.get("layer") or "").strip().lower() or None,
                "match_type": (
                    str(properties.get("match_type") or "").strip().lower() or None
                ),
                "accuracy": (
                    str(properties.get("accuracy") or "").strip().lower() or None
                ),
                "housenumber": housenumber,
                "raw": feature,
            }
        )
    return results


def geocode_match_is_street_level(match: dict[str, Any]) -> bool:
    """True when a Pelias hit is a house/venue, not a city or street-centroid fallback."""
    if not isinstance(match, dict):
        return False
    layer = str(match.get("layer") or "").strip().lower()
    match_type = str(match.get("match_type") or "").strip().lower()
    accuracy = str(match.get("accuracy") or "").strip().lower()
    # House-number / POI hits.
    if layer in {"address", "venue", "building"}:
        return True
    if match.get("housenumber"):
        return True
    # A named street with a real point (not the whole-road centroid fallback).
    if layer == "street" and match_type != "fallback" and accuracy in {
        "point",
        "rooftop",
        "interpolated",
        "parcel",
    }:
        return True
    if accuracy in {"point", "rooftop", "interpolated", "parcel"} and match_type in {
        "exact",
        "interpolated",
    }:
        return True
    return False


class OpenRouteServiceClient:
    """Thin wrapper around OpenRouteService directions, matrix, and geocode."""

    def __init__(self, api_key: str | None = None) -> None:
        key = (api_key or get_ors_api_key() or "").strip()
        if not key:
            raise ValueError(
                "Missing OpenRouteService API key. Set OPENROUTESERVICE_API_KEY "
                "in config/.env or Streamlit secrets."
            )
        self.api_key = key
        self.session = requests.Session()
        # Ignore HTTP(S)_PROXY — ORS often fails through corporate/local proxies.
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Authorization": self.api_key,
                "Accept": "application/json, application/geo+json",
                "Content-Type": "application/json; charset=utf-8",
            }
        )

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{ORS_BASE_URL}{path}"
        started = time.perf_counter()
        log_api_send("openrouteservice", path, url=url, body=payload)
        response = self.session.post(url, json=payload, timeout=60)
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        log_api_done(
            "openrouteservice",
            path,
            started=started,
            status=response.status_code,
            output=data if response.ok else {"error": data},
        )
        if not response.ok:
            message = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    message = str(err.get("message") or "")
                elif err:
                    message = str(err)
            raise requests.HTTPError(
                f"OpenRouteService {response.status_code}: {message or response.text[:200]}",
                response=response,
            )
        return data

    def _get_pelias(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{PELIAS_BASE_URL}{path}"
        started = time.perf_counter()
        log_api_send("openrouteservice", f"pelias {path}", url=url, params=params)
        response = self.session.get(url, params=params, timeout=30)
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        log_api_done(
            "openrouteservice",
            f"pelias {path}",
            started=started,
            status=response.status_code,
            output=data if response.ok else {"error": data},
        )
        if not response.ok:
            message = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    message = str(err.get("message") or err.get("msg") or "")
                elif err:
                    message = str(err)
            raise requests.HTTPError(
                f"Geocoder {response.status_code}: {message or response.text[:200]}",
                response=response,
            )
        return data

    def geocode_search(
        self,
        text: str,
        *,
        size: int = 5,
        layers: str | None = None,
        focus: tuple[float, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Forward-geocode a street address / place name via Pelias."""
        query = str(text or "").strip()
        if not query:
            return []
        params: dict[str, Any] = {
            "text": query,
            "size": max(1, min(20, int(size))),
        }
        if layers:
            params["layers"] = str(layers).strip()
        if focus is not None:
            params["focus.point.lat"] = float(focus[0])
            params["focus.point.lon"] = float(focus[1])
        data = self._get_pelias("/search", params)
        return _parse_geocode_features(data)

    def geocode_address(self, text: str, *, size: int = 5) -> list[dict[str, Any]]:
        """Geocode an address, preferring house numbers and closest-number fallback.

        Pelias often lacks an exact house number (``match_type=fallback`` to a
        street centroid). When that happens, probe nearby numbers on the same
        street and return the closest available address point.
        """
        raw = str(text or "").strip()
        if not raw:
            return []
        query = normalize_geocode_query(raw)
        wanted = parse_address_housenumber(query)
        street_tail = _street_tail_from_query(query, wanted)
        focus: tuple[float, float] | None = None
        postal = parse_canadian_postal_code(query)
        if postal:
            postal_hits = self.geocode_search(
                f"{postal}, Canada",
                size=1,
                layers="postalcode",
            )
            if postal_hits:
                focus = (float(postal_hits[0]["lat"]), float(postal_hits[0]["lng"]))

        primary = self.geocode_search(query, size=max(size, 8), focus=focus)
        if wanted is not None:
            exact = [
                match
                for match in primary
                if _housenumber_int(match.get("housenumber")) == wanted
                and _label_looks_like_street(
                    str(match.get("label") or ""), street_tail or query
                )
            ]
            if exact:
                for match in exact:
                    match["match_quality"] = "exact_housenumber"
                return exact[:size]
            # Address-layer search sometimes finds the number when free-text does not.
            address_hits = self.geocode_search(
                query,
                size=max(size, 8),
                layers="address",
                focus=focus,
            )
            exact = [
                match
                for match in address_hits
                if _housenumber_int(match.get("housenumber")) == wanted
                and _label_looks_like_street(
                    str(match.get("label") or ""), street_tail or query
                )
            ]
            if exact:
                for match in exact:
                    match["match_quality"] = "exact_housenumber"
                return exact[:size]

            closest = self._closest_housenumber_on_street(
                street_tail=street_tail or query,
                wanted=wanted,
                focus=focus,
                limit=size,
            )
            if closest:
                return closest

        # No house number (or probing failed): prefer any true street-level hits.
        good = [match for match in primary if geocode_match_is_street_level(match)]
        if good:
            return good[:size]
        return primary[:size]

    def _closest_housenumber_on_street(
        self,
        *,
        street_tail: str,
        wanted: int,
        focus: tuple[float, float] | None,
        limit: int = 5,
        max_delta: int = 40,
    ) -> list[dict[str, Any]]:
        """Probe nearby house numbers on the same street; return closest hits."""
        street = str(street_tail or "").strip()
        if not street or wanted <= 0:
            return []
        found: dict[int, dict[str, Any]] = {}
        # Check nearest offsets first so we can stop early once we have options.
        offsets = [0]
        for delta in range(1, max_delta + 1):
            offsets.extend((delta, -delta))
        for offset in offsets:
            number = wanted + offset
            if number <= 0 or number in found:
                continue
            probe = f"{number} {street}"
            hits = self.geocode_search(
                probe,
                size=3,
                layers="address",
                focus=focus,
            )
            for match in hits:
                hn = _housenumber_int(match.get("housenumber"))
                if hn is None:
                    continue
                if not _label_looks_like_street(str(match.get("label") or ""), street):
                    continue
                if hn in found:
                    continue
                annotated = dict(match)
                annotated["requested_housenumber"] = str(wanted)
                annotated["housenumber_delta"] = abs(hn - wanted)
                annotated["closest_housenumber"] = hn != wanted
                annotated["match_quality"] = (
                    "exact_housenumber" if hn == wanted else "closest_housenumber"
                )
                if hn != wanted:
                    base = str(annotated.get("label") or "").strip()
                    annotated["label"] = (
                        f"{base} (closest to {wanted}; geocoder has {hn})"
                    )
                found[hn] = annotated
            # Once we have a few nearby numbers, stop probing farther away.
            if found and min(abs(n - wanted) for n in found) <= 5 and len(found) >= 3:
                break
            if found and min(abs(n - wanted) for n in found) == 0:
                break
        ranked = sorted(
            found.values(),
            key=lambda row: (
                int(row.get("housenumber_delta") or 10**9),
                _housenumber_int(row.get("housenumber")) or 10**9,
            ),
        )
        return ranked[: max(1, limit)]

    def reverse_geocode(
        self,
        lat: float,
        lng: float,
        *,
        size: int = 1,
    ) -> list[dict[str, Any]]:
        """Reverse-geocode coordinates to a nearby address label."""
        data = self._get_pelias(
            "/reverse",
            {
                "point.lat": float(lat),
                "point.lon": float(lng),
                "size": max(1, min(5, int(size))),
            },
        )
        return _parse_geocode_features(data)

    def directions(
        self,
        *,
        start: tuple[float, float],
        end: tuple[float, float],
        profile: str = "driving-car",
    ) -> dict[str, Any]:
        """One-shot driving (or other profile) route summary between two points."""
        if profile not in ORS_PROFILES:
            raise ValueError(f"Unsupported ORS profile: {profile}")
        start_lat, start_lng = start
        end_lat, end_lng = end
        payload = {
            "coordinates": [
                _coords_lon_lat(start_lat, start_lng),
                _coords_lon_lat(end_lat, end_lng),
            ],
            "instructions": False,
            "elevation": False,
        }
        data = self._post(f"/directions/{profile}", payload)
        return _summarize_directions(data)

    def matrix(
        self,
        *,
        origins: list[tuple[float, float]],
        destinations: list[tuple[float, float]],
        profile: str = "driving-car",
        metrics: tuple[str, ...] = ("duration", "distance"),
    ) -> dict[str, Any]:
        """Travel-time / distance matrix. Coordinates are (lat, lng)."""
        if profile not in ORS_PROFILES:
            raise ValueError(f"Unsupported ORS profile: {profile}")
        if not origins or not destinations:
            return {"durations": [], "distances": []}
        locations = [_coords_lon_lat(lat, lng) for lat, lng in origins] + [
            _coords_lon_lat(lat, lng) for lat, lng in destinations
        ]
        source_indices = list(range(len(origins)))
        dest_indices = list(range(len(origins), len(origins) + len(destinations)))
        payload = {
            "locations": locations,
            "sources": source_indices,
            "destinations": dest_indices,
            "metrics": list(metrics),
            "units": "m",
        }
        data = self._post(f"/matrix/{profile}", payload)
        return data if isinstance(data, dict) else {}


def _summarize_directions(payload: dict[str, Any]) -> dict[str, Any]:
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list) or not routes:
        return {"duration_s": None, "distance_m": None, "raw": payload}
    summary = routes[0].get("summary") if isinstance(routes[0], dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "duration_s": summary.get("duration"),
        "distance_m": summary.get("distance"),
        "raw": payload,
    }


def _drive_cache_coord_key(lat: float, lng: float) -> str:
    return f"{float(lat):.5f},{float(lng):.5f}"


def _drive_metrics_cache_key(
    home: tuple[float, float],
    destination: tuple[float, float],
    *,
    profile: str,
) -> str:
    return "|".join(
        [
            str(profile or "driving-car"),
            _drive_cache_coord_key(*home),
            _drive_cache_coord_key(*destination),
        ]
    )


def load_drive_metrics_cache() -> dict[str, Any]:
    """Load the on-disk ORS drive-time/distance cache."""
    if not DRIVE_METRICS_CACHE_PATH.is_file():
        return {
            "cache_version": DRIVE_METRICS_CACHE_VERSION,
            "routes": {},
            "updated_at": None,
        }
    try:
        data = json.loads(DRIVE_METRICS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "cache_version": DRIVE_METRICS_CACHE_VERSION,
            "routes": {},
            "updated_at": None,
        }
    if not isinstance(data, dict) or data.get("cache_version") != DRIVE_METRICS_CACHE_VERSION:
        return {
            "cache_version": DRIVE_METRICS_CACHE_VERSION,
            "routes": {},
            "updated_at": None,
        }
    routes = data.get("routes")
    if not isinstance(routes, dict):
        data["routes"] = {}
    return data


def save_drive_metrics_cache(cache: dict[str, Any]) -> Path:
    """Persist the ORS drive-time/distance cache."""
    payload = {
        "cache_version": DRIVE_METRICS_CACHE_VERSION,
        "updated_at": datetime.now().astimezone().isoformat(),
        "routes": cache.get("routes") if isinstance(cache.get("routes"), dict) else {},
    }
    DRIVE_METRICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRIVE_METRICS_CACHE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return DRIVE_METRICS_CACHE_PATH


def drive_metrics_cache_stats() -> dict[str, Any]:
    """Summary for Cache maintenance (entries + file size)."""
    path = DRIVE_METRICS_CACHE_PATH
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    cache = load_drive_metrics_cache() if exists else {"routes": {}}
    routes = cache.get("routes") if isinstance(cache.get("routes"), dict) else {}
    return {
        "path": path,
        "exists": exists,
        "bytes": size,
        "entries": len(routes),
        "updated_at": cache.get("updated_at"),
    }


def clear_drive_metrics_cache() -> int:
    """Delete the drive-time cache file. Returns how many routes were stored."""
    cache = load_drive_metrics_cache()
    routes = cache.get("routes") if isinstance(cache.get("routes"), dict) else {}
    count = len(routes)
    try:
        if DRIVE_METRICS_CACHE_PATH.is_file():
            DRIVE_METRICS_CACHE_PATH.unlink()
    except OSError:
        pass
    return count


def drive_metrics_from_home(
    destinations: list[tuple[float, float]],
    *,
    home: tuple[float, float] | None = None,
    profile: str = "driving-car",
    use_cache: bool = True,
) -> list[dict[str, float | None]]:
    """Return per-destination ``{duration_s, distance_m}`` from home via matrix.

    Successful lookups are stored in ``cache/shared/ors_drive_matrix_cache.json``
    keyed by home + destination coordinates (5 decimal places) and profile.
    """
    origin = home or configured_home_coordinates()
    if origin is None:
        raise ValueError(
            "Set ORS_HOME_LAT and ORS_HOME_LNG in config/.env for travel times."
        )
    if not destinations:
        return []

    cache = load_drive_metrics_cache() if use_cache else {"routes": {}}
    routes = cache.setdefault("routes", {})
    if not isinstance(routes, dict):
        routes = {}
        cache["routes"] = routes

    results: list[dict[str, float | None]] = [
        {"duration_s": None, "distance_m": None} for _ in destinations
    ]
    missing_indices: list[int] = []
    missing_destinations: list[tuple[float, float]] = []
    for index, destination in enumerate(destinations):
        key = _drive_metrics_cache_key(origin, destination, profile=profile)
        entry = routes.get(key)
        if (
            use_cache
            and isinstance(entry, dict)
            and entry.get("duration_s") is not None
            and entry.get("distance_m") is not None
        ):
            results[index] = {
                "duration_s": float(entry["duration_s"]),
                "distance_m": float(entry["distance_m"]),
            }
            continue
        missing_indices.append(index)
        missing_destinations.append(destination)

    if not missing_destinations:
        return results

    client = OpenRouteServiceClient()
    chunk_size = 40
    dirty = False
    for start in range(0, len(missing_destinations), chunk_size):
        chunk = missing_destinations[start : start + chunk_size]
        chunk_indices = missing_indices[start : start + chunk_size]
        payload = client.matrix(
            origins=[origin],
            destinations=chunk,
            profile=profile,
        )
        durations = payload.get("durations") or []
        distances = payload.get("distances") or []
        row_dur = durations[0] if durations else []
        row_dist = distances[0] if distances else []
        fetched_at = datetime.now().astimezone().isoformat()
        for offset, destination in enumerate(chunk):
            dur = row_dur[offset] if offset < len(row_dur) else None
            dist = row_dist[offset] if offset < len(row_dist) else None
            metric = {
                "duration_s": float(dur) if dur is not None else None,
                "distance_m": float(dist) if dist is not None else None,
            }
            results[chunk_indices[offset]] = metric
            if metric["duration_s"] is None or metric["distance_m"] is None:
                continue
            key = _drive_metrics_cache_key(origin, destination, profile=profile)
            routes[key] = {
                "duration_s": metric["duration_s"],
                "distance_m": metric["distance_m"],
                "fetched_at": fetched_at,
                "home": _drive_cache_coord_key(*origin),
                "destination": _drive_cache_coord_key(*destination),
                "profile": profile,
            }
            dirty = True

    if use_cache and dirty:
        try:
            save_drive_metrics_cache(cache)
        except OSError:
            pass
    return results


def format_duration_minutes(duration_s: float | None) -> str:
    if duration_s is None:
        return "—"
    minutes = int(round(float(duration_s) / 60.0))
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem:02d}m"


def haversine_meters(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> float:
    """Great-circle distance in meters between two ``(lat, lng)`` points."""
    lat1, lon1 = float(origin[0]), float(origin[1])
    lat2, lon2 = float(destination[0]), float(destination[1])
    radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * radius_m * math.asin(min(1.0, math.sqrt(a)))


def format_distance_miles(distance_m: float | None) -> str:
    if distance_m is None:
        return "—"
    miles = float(distance_m) / 1609.344
    if miles < 10:
        return f"{miles:.1f} mi"
    return f"{miles:.0f} mi"
