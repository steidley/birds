"""eBird API 2.0 client and configuration."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.ebird.org/v2"
ROOT = Path(__file__).parent

load_dotenv(ROOT / ".env")


def get_api_key() -> str | None:
    """Return the eBird API key from env or Streamlit secrets."""
    key = os.environ.get("EBIRD_API_KEY") or os.environ.get("EBIRD_API_TOKEN")
    if key:
        return key.strip()

    try:
        import streamlit as st

        secrets = getattr(st, "secrets", None)
        if secrets is not None:
            for name in ("EBIRD_API_KEY", "EBIRD_API_TOKEN"):
                if name in secrets and secrets[name]:
                    return str(secrets[name]).strip()
    except Exception:
        pass

    return None


class EBirdClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or get_api_key()
        if not self.api_key:
            raise ValueError(
                "Missing eBird API key. Set EBIRD_API_KEY in .env or "
                ".streamlit/secrets.toml. Get a key at https://ebird.org/api/keygen"
            )
        self.session = requests.Session()
        self.session.headers.update({"X-eBirdApiToken": self.api_key})

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def taxonomy(self, species: str | None = None) -> Any:
        params: dict[str, Any] = {"fmt": "json"}
        if species:
            params["species"] = species
        return self.get("/ref/taxonomy/ebird", params=params)

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
        taxa: dict[str, dict[str, Any]] = {}
        batch_size = 50
        for start in range(0, len(codes), batch_size):
            batch = codes[start : start + batch_size]
            rows = self.taxonomy(species=",".join(batch))
            if not isinstance(rows, list):
                continue
            for row in rows:
                code = row.get("speciesCode")
                if code:
                    taxa[str(code)] = row
        for code in codes:
            if code in taxa:
                continue
            rows = self.taxonomy(species=code)
            if isinstance(rows, list) and rows:
                taxa[code] = rows[0]
        return taxa

    def recent_observations(
        self,
        region_code: str,
        species_code: str | None = None,
        *,
        back: int = 14,
        max_results: int = 10,
    ) -> Any:
        if species_code:
            path = f"/data/obs/{region_code}/recent/{species_code}"
        else:
            path = f"/data/obs/{region_code}/recent"
        return self.get(
            path,
            params={"back": back, "maxResults": max_results},
        )

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

    def checklist(self, sub_id: str) -> dict[str, Any]:
        data = self.get(f"/product/checklist/view/{sub_id}")
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
    ) -> list[dict[str, Any]]:
        rows = self.hotspots(region_code, back=back)
        rows = sorted(
            rows,
            key=lambda row: (
                int(row.get("numSpeciesAllTime") or 0),
                int(row.get("numChecklistsAllTime") or 0),
            ),
            reverse=True,
        )
        return rows[:limit]

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
