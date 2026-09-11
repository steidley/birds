"""Parse a personal eBird observations CSV (``requiredData/MyBirdData.csv``)."""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ebird import (
    REGION_LIST_CACHE_PATH,
    REQUIRED_DATA_DIR,
    ROOT,
    configured_observer_names,
    parse_ebird_obs_day,
    resolve_ebird_code,
)

# Preferred name first; keep eBird’s default export names as fallbacks.
_MY_EBIRD_GLOBS = (
    "MyBirdData*.csv",
    "MyEBirdData*.csv",
    "MyEbirdData*.csv",
)
_SEARCH_DIRS = (REQUIRED_DATA_DIR, ROOT)
_UPLOAD_CSV_NAME = "MyBirdData.csv"
_REQUIRED_UPLOAD_COLUMNS = ("Submission ID", "Common Name", "Scientific Name", "Date")


def my_ebird_data_destination() -> Path:
    """Where an uploaded My eBird CSV should be written."""
    override = (os.environ.get("MY_EBIRD_DATA_PATH") or "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path
    return REQUIRED_DATA_DIR / _UPLOAD_CSV_NAME


def my_ebird_data_path() -> Path | None:
    """Personal observations CSV under ``requiredData/``, or ``MY_EBIRD_DATA_PATH``."""
    override = (os.environ.get("MY_EBIRD_DATA_PATH") or "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path if path.is_file() else None
    found: list[Path] = []
    for directory in _SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for pattern in _MY_EBIRD_GLOBS:
            found.extend(path for path in directory.glob(pattern) if path.is_file())
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def my_ebird_source_signature() -> tuple[str, float]:
    path = my_ebird_data_path()
    if path is None:
        return ("", 0.0)
    try:
        return (str(path), path.stat().st_mtime)
    except OSError:
        return (str(path), 0.0)


def invalidate_my_ebird_dataset_cache() -> None:
    """Drop the parsed-export LRU cache after the on-disk file changes."""
    load_my_ebird_dataset.cache_clear()


def _preferred_csv_member(names: list[str]) -> str | None:
    csv_names = [
        name
        for name in names
        if name.lower().endswith(".csv")
        and not Path(name).name.startswith(".")
        and "__macosx/" not in name.casefold()
    ]
    if not csv_names:
        return None
    for pattern in _MY_EBIRD_GLOBS:
        prefix = pattern.replace("*", "").casefold().removesuffix(".csv")
        for name in csv_names:
            stem = Path(name).name.casefold().removesuffix(".csv")
            if stem.startswith(prefix.rstrip(".")) or prefix.rstrip(".") in stem:
                return name
    return csv_names[0]


def extract_my_ebird_csv_bytes(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Return CSV bytes from a raw upload (``.csv`` or a zip containing one)."""
    name = (filename or "").strip() or "upload.csv"
    lowered = name.casefold()
    if lowered.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                member = _preferred_csv_member(archive.namelist())
                if member is None:
                    raise ValueError("Zip archive does not contain a CSV file.")
                return archive.read(member), Path(member).name
        except zipfile.BadZipFile as exc:
            raise ValueError("Upload is not a valid zip archive.") from exc
    if not lowered.endswith(".csv"):
        raise ValueError("Upload must be a .csv file or a .zip containing one.")
    return raw, Path(name).name


def validate_my_ebird_csv_bytes(raw: bytes) -> None:
    """Ensure the CSV looks like an eBird My Data export."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(io.StringIO(text))
    fields = {str(name or "").strip() for name in (reader.fieldnames or [])}
    missing = [col for col in _REQUIRED_UPLOAD_COLUMNS if col not in fields]
    if missing:
        raise ValueError(
            "Not an eBird My Data export — missing column"
            f"{'' if len(missing) == 1 else 's'}: {', '.join(missing)}."
        )


def save_uploaded_my_ebird_data(raw: bytes, *, filename: str = "") -> Path:
    """Validate an upload and write it to the configured MyBirdData path."""
    csv_bytes, _inner_name = extract_my_ebird_csv_bytes(raw, filename)
    validate_my_ebird_csv_bytes(csv_bytes)
    destination = my_ebird_data_destination()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Normalize to UTF-8 without BOM for consistent reloads.
    text = csv_bytes.decode("utf-8-sig")
    destination.write_text(text, encoding="utf-8", newline="")
    invalidate_my_ebird_dataset_cache()
    return destination


def observation_in_region(region_codes: list[str], target: str) -> bool:
    """True when an observation belongs to ``target`` (world, country, state, county)."""
    wanted = (target or "").strip()
    if not wanted or wanted.casefold() == "world":
        return True
    for code in region_codes:
        if code == wanted or code.startswith(wanted + "-"):
            return True
    return False


@lru_cache(maxsize=1)
def _county_code_index(cache_mtime: float) -> dict[tuple[str, str], str]:
    del cache_mtime
    index: dict[tuple[str, str], str] = {}
    try:
        payload = json.loads(REGION_LIST_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return index
    lists = payload.get("lists") if isinstance(payload, dict) else None
    if not isinstance(lists, dict):
        return index
    for key, rows in lists.items():
        if not str(key).startswith("subnational2:") or not isinstance(rows, list):
            continue
        parent = str(key).split(":", 1)[1].strip().casefold()
        if not parent:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            name = str(row.get("name") or "").strip()
            if code and name:
                index[(parent, name.casefold())] = code
    return index


def _region_codes_for_place(state: str, county: str) -> list[str]:
    codes: list[str] = []
    parent = (state or "").strip()
    if parent:
        codes.append(parent)
    place = (county or "").strip()
    if parent and place:
        mtime = 0.0
        try:
            mtime = REGION_LIST_CACHE_PATH.stat().st_mtime
        except OSError:
            pass
        mapped = _county_code_index(mtime).get((parent.casefold(), place.casefold()))
        if mapped:
            codes.insert(0, mapped)
    return codes


def _is_life_list_tick(common: str, sci: str, category: str) -> bool:
    kind = (category or "").strip().casefold()
    if kind in {"spuh", "slash", "hybrid", "intergrade"}:
        return False
    folded = common.casefold()
    if folded.endswith(" sp.") or folded.endswith(" sp") or " spuh" in folded:
        return False
    if "/" in sci:
        return False
    if "/" in common and "(" not in common:
        return False
    return True


def _species_code(common: str, sci: str) -> str:
    code = resolve_ebird_code(
        scientific_name=sci,
        common_name=common,
        local_only=True,
    )
    if code:
        return code
    parts = sci.replace(",", " ").split()
    if len(parts) >= 2:
        binomial = f"{parts[0]} {parts[1]}"
        if binomial.casefold() != sci.casefold():
            code = resolve_ebird_code(
                scientific_name=binomial,
                common_name=common.split(" (", 1)[0].strip(),
                local_only=True,
            )
            if code:
                return code
    return ""


def _combine_obs_dt(day_text: str, time_text: str) -> str:
    day = (day_text or "").strip()
    clock = (time_text or "").strip()
    if not day:
        return ""
    if not clock:
        return day
    for fmt in ("%I:%M %p", "%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(clock, fmt).time()
            return f"{day} {parsed.strftime('%H:%M')}"
        except ValueError:
            continue
    return f"{day} {clock}"


def _parse_count(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text or text.casefold() == "x":
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


@lru_cache(maxsize=1)
def load_my_ebird_dataset(signature: tuple[str, float]) -> dict[str, Any]:
    """Parse the export into observations and checklist summaries."""
    path_text, _mtime = signature
    if not path_text:
        return {"path": None, "observations": [], "checklists": {}}
    path = Path(path_text)
    observations: list[dict[str, Any]] = []
    checklists: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sub_id = str(row.get("Submission ID") or "").strip()
            if not sub_id:
                continue
            common = str(row.get("Common Name") or "").strip()
            sci = str(row.get("Scientific Name") or "").strip()
            if not common and not sci:
                continue
            day = parse_ebird_obs_day(str(row.get("Date") or ""))
            if day is None:
                continue
            state = str(row.get("State/Province") or "").strip()
            county = str(row.get("County") or "").strip()
            region_codes = _region_codes_for_place(state, county)
            loc_id = str(row.get("Location ID") or "").strip()
            loc_name = str(row.get("Location") or "").strip()
            obs_dt = _combine_obs_dt(str(row.get("Date") or ""), str(row.get("Time") or ""))
            count = _parse_count(str(row.get("Count") or ""))
            code = _species_code(common, sci)
            how_many_str = str(row.get("Count") or "").strip()
            obs = {
                "speciesCode": code,
                "comName": common,
                "sciName": sci,
                "howMany": count,
                "howManyStr": how_many_str or None,
                "howManyAtleast": count,
            }
            observations.append(
                {
                    "subId": sub_id,
                    "common": common,
                    "sciName": sci,
                    "code": code,
                    "day": day,
                    "obsDt": obs_dt,
                    "locId": loc_id,
                    "locName": loc_name,
                    "region_codes": region_codes,
                    "count": count,
                    "taxonOrder": str(row.get("Taxonomic Order") or ""),
                    "obs": obs,
                }
            )
            entry = checklists.get(sub_id)
            if entry is None:
                entry = {
                    "subId": sub_id,
                    "subID": sub_id,
                    "locId": loc_id,
                    "locID": loc_id,
                    "locName": loc_name,
                    "obsDt": obs_dt,
                    "isoObsDate": obs_dt,
                    "regionCode": region_codes[0] if region_codes else "",
                    "region_codes": list(region_codes),
                    "latitude": str(row.get("Latitude") or "").strip(),
                    "longitude": str(row.get("Longitude") or "").strip(),
                    "_obs_day": day.isoformat(),
                    "_source": "my_ebird_data",
                    "_detail": {
                        "subId": sub_id,
                        "locId": loc_id,
                        "obsDt": obs_dt,
                        "obs": [],
                    },
                    "_seen_taxa": set(),
                }
                checklists[sub_id] = entry
            detail = entry["_detail"]
            seen = entry["_seen_taxa"]
            taxon_key = code or sci.casefold() or common.casefold()
            if taxon_key and taxon_key not in seen:
                seen.add(taxon_key)
                detail.setdefault("obs", []).append(obs)
            if loc_id and not entry.get("locId"):
                entry["locId"] = loc_id
                entry["locID"] = loc_id
                detail["locId"] = loc_id
            if loc_name and not entry.get("locName"):
                entry["locName"] = loc_name
            for code_value in region_codes:
                if code_value not in entry["region_codes"]:
                    entry["region_codes"].append(code_value)

    for entry in checklists.values():
        seen = entry.pop("_seen_taxa", set())
        entry["numSpecies"] = len(seen)
        entry["_detail"]["numSpecies"] = len(seen)
        names = configured_observer_names()
        if names:
            entry["userDisplayName"] = names[0]
            entry["_detail"]["userDisplayName"] = names[0]
        else:
            entry["userDisplayName"] = "Me"
            entry["_detail"]["userDisplayName"] = "Me"

    return {
        "path": path,
        "observations": observations,
        "checklists": checklists,
    }


def my_ebird_dataset() -> dict[str, Any]:
    return load_my_ebird_dataset(my_ebird_source_signature())


def my_ebird_checklist_summaries() -> list[dict[str, Any]]:
    checklists = my_ebird_dataset().get("checklists") or {}
    rows = [dict(row) for row in checklists.values() if isinstance(row, dict)]
    return sorted(
        rows,
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )


def my_ebird_checklist_summaries_for(
    region_code: str = "",
    *,
    loc_id: str | None = None,
    start_date=None,
    end_date=None,
) -> list[dict[str, Any]]:
    region = (region_code or "").strip()
    location = (loc_id or "").strip() or None
    found: list[dict[str, Any]] = []
    for row in my_ebird_checklist_summaries():
        codes = [str(code) for code in (row.get("region_codes") or []) if code]
        if region and not observation_in_region(codes, region):
            continue
        file_loc = str(row.get("locId") or "").strip()
        if location is not None and file_loc != location:
            continue
        day = parse_ebird_obs_day(str(row.get("_obs_day") or row.get("isoObsDate") or ""))
        if day is None:
            continue
        if start_date is not None and day < start_date:
            continue
        if end_date is not None and day > end_date:
            continue
        tagged = dict(row)
        if region:
            tagged.setdefault("regionCode", region)
            tagged.setdefault("_region", region)
        found.append(tagged)
    return found


def my_ebird_life_list_birds(region_code: str) -> list[dict[str, Any]]:
    """Species ticks from the export, with last-seen (and first-seen) dates."""
    target = (region_code or "").strip() or "world"
    birds: dict[str, dict[str, Any]] = {}
    for row in my_ebird_dataset().get("observations") or []:
        if not observation_in_region(list(row.get("region_codes") or []), target):
            continue
        common = str(row.get("common") or "").strip()
        sci = str(row.get("sciName") or "").strip()
        if not _is_life_list_tick(common, sci, ""):
            continue
        display = common.split(" (", 1)[0].strip() or common or sci
        key = display.casefold() if display else sci.casefold()
        day = row.get("day")
        existing = birds.get(key)
        if existing is None:
            order_raw = str(row.get("taxonOrder") or "").strip()
            try:
                taxon_order = float(order_raw) if order_raw else float("inf")
            except ValueError:
                taxon_order = float("inf")
            birds[key] = {
                "name": display,
                "sciName": sci,
                "code": str(row.get("code") or ""),
                "last_day": day,
                "first_day": day,
                "first_by_year": {day.year: day} if day is not None else {},
                "taxon_order": taxon_order,
            }
            continue
        if day is not None:
            previous_last = existing.get("last_day")
            if previous_last is None or day > previous_last:
                existing["last_day"] = day
            previous_first = existing.get("first_day")
            if previous_first is None or day < previous_first:
                existing["first_day"] = day
            years = existing.setdefault("first_by_year", {})
            previous_year = years.get(day.year)
            if previous_year is None or day < previous_year:
                years[day.year] = day
        if not existing.get("code") and row.get("code"):
            existing["code"] = row["code"]
    return sorted(
        birds.values(),
        key=lambda bird: (
            bird.get("taxon_order") if bird.get("taxon_order") is not None else float("inf"),
            str(bird.get("name") or "").casefold(),
        ),
    )


def my_ebird_own_recent_rows() -> dict[str, list[dict[str, Any]]]:
    """Personal sightings from the export, keyed by eBird species code."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    checklists = my_ebird_dataset().get("checklists") or {}
    for row in my_ebird_dataset().get("observations") or []:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        sub_id = str(row.get("subId") or "")
        checklist = checklists.get(sub_id) or {}
        day = row.get("day")
        grouped.setdefault(code, []).append(
            {
                "obsDt": str(row.get("obsDt") or ""),
                "obsDay": day.isoformat() if day is not None else "",
                "locName": str(row.get("locName") or ""),
                "locId": str(row.get("locId") or ""),
                "subId": sub_id,
                "numSpecies": int(checklist.get("numSpecies") or 0),
                "howMany": row.get("count"),
                "regionCode": (row.get("region_codes") or [""])[0],
                "source": "my_ebird_data",
            }
        )
    return grouped
