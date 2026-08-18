from pathlib import Path
import csv
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, urlunparse

import requests
import streamlit as st
from dotenv import load_dotenv

from components.swipe_image import swipe_image
from ebird import (
    EBirdClient,
    MissingEbirdApiKey,
    build_checklist_cache_status,
    build_local_last_seen_index,
    filter_regions_by_query,
    get_api_key,
    list_local_checklist_regions,
    load_cached_hotspots,
    load_disk_region_species_codes,
    load_taxonomy_cache,
    load_local_checklists_for_hotspot,
    rebuild_local_last_seen_indexes,
    region_historical_species_cache_coverage,
    resolve_ebird_code,
)
from download_checklists import (
    dedupe_downloaded_checklists,
    download_progress_path,
    load_download_progress,
    load_feed_cache_progress,
    missing_checklists_by_species_count,
    request_download_stop,
    request_feed_cache_stop,
)
from inaturalist import (
    CACHE_PATH as INAT_PHOTO_CACHE_PATH,
    DEFAULT_MAX_PHOTOS,
    GALLERY_CACHE_PATH as INAT_GALLERY_CACHE_PATH,
    GALLERY_CACHE_VERSION,
    SIMILAR_CACHE_PATH as INAT_SIMILAR_CACHE_PATH,
    species_gallery,
    species_photo,
    species_similar,
)

load_dotenv(Path(__file__).parent / ".env")

LIFE_LISTS_DIR = Path(__file__).parent / "lifeLists"
SAVED_GALLERIES_DIR = Path(__file__).parent / "saved_galleries"
SAVED_GALLERY_QUERY = "saved_gallery"
SAVED_GALLERY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d+)?$")
DEFAULT_HOTSPOT_ID = os.environ.get("EBIRD_DEFAULT_HOTSPOT", "L364884")
WORLD_LIFE_LIST_CODE = "world"
BUSY_CURSOR_CSS = """
<style>
html, body,
[data-testid="stAppViewContainer"], [data-testid="stApp"],
html *, body *,
[data-testid="stAppViewContainer"] *, [data-testid="stApp"] * {
  cursor: wait !important;
}
</style>
"""
UI_HEADING_CSS = """
<style>
.stApp [data-testid="stHeading"] h1,
.stApp h1 {
  font-size: 1.2rem !important;
  font-weight: 650 !important;
  line-height: 1.25 !important;
  margin: 0 0 0.35rem 0 !important;
  padding: 0 !important;
}
.stApp [data-testid="stTextInput"] input[aria-label="Gallery name"] {
  font-size: 1.2rem !important;
  font-weight: 650 !important;
  line-height: 1.25 !important;
}
.stApp [data-testid="stHeading"] h2,
.stApp h2 {
  font-size: 1.05rem !important;
  font-weight: 600 !important;
  line-height: 1.3 !important;
  margin: 0.75rem 0 0.25rem 0 !important;
  padding: 0 !important;
}
.stApp [data-testid="stHeading"] h3,
.stApp h3 {
  font-size: 0.98rem !important;
  font-weight: 600 !important;
  line-height: 1.3 !important;
  margin: 0.6rem 0 0.2rem 0 !important;
  padding: 0 !important;
}
.stApp [data-testid="stMainBlockContainer"],
.stApp .block-container {
  padding-top: 1.1rem !important;
}
#MainMenu, footer,
.stApp [data-testid="stHeader"],
.stApp [data-testid="stToolbar"],
.stApp [data-testid="stDecoration"],
.stApp [data-testid="stStatusWidget"],
.stApp [data-testid="stAppToolbar"],
.stApp .stAppToolbar,
.stApp header {
  display: none !important;
  visibility: hidden !important;
  height: 0 !important;
}
</style>
"""
UI_LAYOUT_DESKTOP_CSS = """
<style>
.stApp [data-testid="stMainBlockContainer"],
.stApp .block-container {
  max-width: 100% !important;
  width: 100% !important;
  padding-left: 2rem;
  padding-right: 2rem;
}
</style>
"""
UI_LAYOUT_MOBILE_CSS = """
<style>
.stApp [data-testid="stMainBlockContainer"],
.stApp .block-container {
  max-width: 430px !important;
  width: 100% !important;
  margin-left: auto;
  margin-right: auto;
  padding-left: 1rem;
  padding-right: 1rem;
}
</style>
"""


def project_git_commit_stamp() -> str | None:
    """Return the latest project commit time, or None if git is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=Path(__file__).parent,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (completed.stdout or "").strip()
    if completed.returncode != 0 or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if when.tzinfo is None:
        when = when.astimezone()
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def set_busy_cursor(enabled: bool = True) -> None:
    """Show a page-wide busy cursor while waiting on rate-limited work.

    Clearing is a no-op: Streamlit rebuilds the page each run, so wait styles
    only persist if this function injects them again.
    """
    if enabled:
        st.markdown(BUSY_CURSOR_CSS, unsafe_allow_html=True)


def apply_ui_layout() -> None:
    """Apply desktop (full width) or mobile (narrow) layout CSS."""
    mode = str(st.session_state.get("ui_layout_pref") or "desktop")
    if mode not in {"desktop", "mobile"}:
        mode = "desktop"
    st.session_state.ui_layout_pref = mode
    css = UI_LAYOUT_MOBILE_CSS if mode == "mobile" else UI_LAYOUT_DESKTOP_CSS
    st.markdown(UI_HEADING_CSS + css, unsafe_allow_html=True)


def _sync_ui_layout_pref() -> None:
    """Copy the layout radio into a key that survives leaving this screen."""
    chosen = st.session_state.get("ui_layout_mode_radio")
    if chosen in {"desktop", "mobile"}:
        st.session_state.ui_layout_pref = chosen


def render_ebird_rate_limit_notices() -> None:
    """Show any eBird 429 waits that occurred during this session/run."""
    events = list(st.session_state.get("ebird_rate_limit_events") or [])
    active = st.session_state.get("ebird_rate_limit_active")
    if active:
        set_busy_cursor(True)
        seconds = float(active.get("seconds") or 0)
        st.warning(
            f"eBird API rate limit hit. Waiting at least {seconds:.0f}s "
            f"(Retry-After) before retrying…"
        )
    if events:
        total = sum(float(event.get("seconds") or 0) for event in events)
        st.warning(
            f"eBird rate-limited {len(events)} time(s) this session "
            f"(~{total:.0f}s waited; Retry-After honored)."
        )
        # Keep history brief so the banner does not grow forever.
        if len(events) > 12:
            st.session_state.ebird_rate_limit_events = events[-12:]


@st.cache_data(show_spinner=False)
def inaturalist_photo_for_code(
    ebird_code: str,
    scientific_name: str | None = None,
) -> dict | None:
    """Resolve an eBird species code to cached iNaturalist photo metadata."""
    if not ebird_code:
        return None
    try:
        return species_photo(ebird_code, scientific_name=scientific_name or None)
    except requests.RequestException:
        return None


@st.cache_data(show_spinner=False)
def similar_species_for_taxon(
    taxon_id: int | None,
    ebird_code: str | None = None,
    scientific_name: str | None = None,
    limit: int = 12,
) -> list[dict]:
    """Resolve similar species thumbnails for the gallery."""
    try:
        return species_similar(
            taxon_id=taxon_id,
            ebird_code=ebird_code or None,
            scientific_name=scientific_name or None,
            limit=limit,
        )
    except requests.RequestException:
        return []


@st.cache_data(show_spinner=False)
def gallery_payload_for_code(
    ebird_code: str,
    scientific_name: str | None = None,
    max_photos: int = 200,
    cache_version: int = GALLERY_CACHE_VERSION,
) -> dict | None:
    """Resolve gallery photos and species info for an eBird code."""
    lookup = (ebird_code or "").strip() or (scientific_name or "").strip()
    if not lookup:
        return None
    started = time.perf_counter()
    try:
        result = species_gallery(
            lookup,
            scientific_name=scientific_name or None,
            max_photos=max_photos,
        )
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(
            f"[timing] gallery_payload_for_code: {elapsed_ms:.0f}ms "
            f"code={lookup!r} error={exc.__class__.__name__}",
            flush=True,
        )
        return None
    elapsed_ms = (time.perf_counter() - started) * 1000
    photos = len((result or {}).get("photos") or [])
    print(
        f"[timing] gallery_payload_for_code: {elapsed_ms:.0f}ms "
        f"code={lookup!r} photos={photos} "
        f"(includes disk cache; Streamlit cache may skip this on later runs)",
        flush=True,
    )
    return result


def render_species_photo(
    ebird_code: str | None,
    *,
    scientific_name: str | None = None,
    width: int = 72,
) -> None:
    """Render a small iNaturalist thumbnail when available."""
    if not ebird_code:
        return
    photo = inaturalist_photo_for_code(ebird_code, scientific_name)
    if not photo:
        return
    st.image(photo["image_url"], width=width)


def render_species_thumbnail_table(
    items: list[dict],
    *,
    columns: int = 6,
    width: int = 144,
    click_hrefs: list[str | None] | None = None,
) -> None:
    """Render species as a thumbnail-only grid with 1px gaps."""
    if not items:
        return
    cells: list[str] = []
    for index, item in enumerate(items):
        code = item.get("code")
        sci = item.get("sciName") or None
        name = str(item.get("Species") or item.get("name") or code or "")
        photo = inaturalist_photo_for_code(str(code), sci) if code else None
        if photo and photo.get("image_url"):
            src = html.escape(str(photo["image_url"]), quote=True)
            alt = html.escape(name or "species", quote=True)
            inner = (
                f'<img src="{src}" alt="{alt}" '
                f'style="width:{width}px;height:{width}px;object-fit:cover;'
                f'display:block;margin:0;padding:0;border:0"/>'
            )
        else:
            label = html.escape((name[:10] or "—"), quote=False)
            inner = (
                f'<div style="width:{width}px;height:{width}px;display:flex;'
                f'align-items:center;justify-content:center;font-size:11px;'
                f'color:#64748b;background:#f1f5f9;margin:0;padding:0">'
                f"{label}</div>"
            )
        href = None
        if click_hrefs is not None and index < len(click_hrefs):
            href = click_hrefs[index]
        if href:
            safe_href = html.escape(str(href), quote=True)
            alt_title = html.escape(name or "species", quote=True)
            inner = (
                f'<a href="{safe_href}" title="{alt_title}" '
                f'style="display:block;line-height:0;text-decoration:none">'
                f"{inner}</a>"
            )
        cells.append(inner)
    grid = "".join(
        f'<div style="margin:0;padding:0;line-height:0">{cell}</div>'
        for cell in cells
    )
    st.markdown(
        f'<div style="display:grid;grid-template-columns:repeat(auto-fill,{width}px);'
        f'gap:1px;padding:0;margin:0;width:100%;justify-content:start;'
        f'line-height:0">{grid}</div>',
        unsafe_allow_html=True,
    )

def _format_eta(seconds: float) -> str:
    """Format a remaining-time estimate as hours, minutes, and seconds."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or hours:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return ", ".join(parts)


def _format_eta_compact(seconds: float) -> str:
    """Compact Hh Mm Ss form for tight UI slots."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def _parse_progress_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp from a download progress record."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _is_process_running(pid: object) -> bool:
    """Return whether a locally launched background worker is still alive."""
    try:
        process_id = int(pid)
        if process_id <= 0:
            return False
        os.kill(process_id, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _checklist_download_active(region_code: str, year: int) -> bool:
    """True when a background checklist download worker is still running."""
    progress = load_download_progress(region_code, year)
    return (
        str(progress.get("status") or "") == "running"
        and _is_process_running(progress.get("pid"))
    )


def _start_checklist_download(
    region_code: str,
    year: int,
    *,
    day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
) -> str | None:
    """Launch the checklist detail worker. Returns an error message, or None."""
    script = Path(__file__).parent / "download_checklists.py"
    command = [
        sys.executable,
        str(script),
        "--region",
        region_code,
        "--year",
        str(year),
    ]
    if day:
        command.extend(["--day", day])
    if loc_id:
        command.extend(["--loc-id", loc_id])
    species_floor = max(0, int(min_species or 0))
    if species_floor > 0:
        command.extend(["--min-species", str(species_floor)])
    try:
        subprocess.Popen(
            command,
            cwd=str(Path(__file__).parent),
            start_new_session=True,
        )
    except OSError as exc:
        return str(exc)
    return None


def _feed_cache_active(region_code: str, year: int) -> bool:
    """True when a background daily-feed cache worker is still running."""
    progress = load_feed_cache_progress(region_code, year)
    return (
        str(progress.get("status") or "") == "running"
        and _is_process_running(progress.get("pid"))
    )


def _start_feed_cache(region_code: str, year: int) -> str | None:
    """Launch the daily-feed cache worker. Returns an error message, or None."""
    script = Path(__file__).parent / "download_checklists.py"
    command = [
        sys.executable,
        str(script),
        "--region",
        region_code,
        "--year",
        str(year),
        "--cache-feed",
    ]
    try:
        subprocess.Popen(
            command,
            cwd=str(Path(__file__).parent),
            start_new_session=True,
        )
    except OSError as exc:
        return str(exc)
    return None


def _format_bytes(size: int) -> str:
    """Compact display value for cache sizes."""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _dataframe_height(row_count: int, *, min_rows: int = 50, row_height: int = 35) -> int:
    """Pixel height so a table viewport shows up to ``min_rows`` rows."""
    visible = min(max(int(row_count), 1), min_rows)
    # Header padding is handled by the caller; this is body height only.
    return visible * row_height + 8


def _queue_checklist_download(
    region_code: str,
    year: int,
    *,
    day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    label: str,
) -> None:
    """Store a download request from a button ``on_click`` callback."""
    st.session_state["cache_download_request"] = {
        "region_code": region_code,
        "year": int(year),
        "day": day,
        "loc_id": loc_id,
        "min_species": max(0, int(min_species or 0)),
        "label": label,
    }


def _consume_checklist_download_request() -> None:
    """Start any download queued by a table-row icon click."""
    request = st.session_state.pop("cache_download_request", None)
    if not isinstance(request, dict):
        return
    _launch_row_checklist_download(
        str(request.get("region_code") or ""),
        int(request.get("year") or 0),
        day=(str(request["day"]) if request.get("day") else None),
        loc_id=(str(request["loc_id"]) if request.get("loc_id") else None),
        min_species=int(request.get("min_species") or 0),
        label=str(request.get("label") or "selection"),
    )


def _launch_row_checklist_download(
    region_code: str,
    year: int,
    *,
    day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    label: str,
) -> None:
    """Start a scoped background download and refresh the page."""
    if not region_code or year <= 0:
        st.error("Missing region/year for background download.")
        return
    error = _start_checklist_download(
        region_code,
        year,
        day=day,
        loc_id=loc_id,
        min_species=min_species,
    )
    if error:
        st.error(f"Could not start background downloader: {error}")
        return
    st.success(
        f"Background downloader started for {label}. "
        "Refresh status to update progress."
    )
    time.sleep(0.25)
    st.rerun()


def _nowrap_cell(text: object, *, title: str | None = None) -> None:
    """Render a single-line table cell with ellipsis when the label is long."""
    value = "" if text is None else str(text)
    tip = title if title is not None else value
    st.markdown(
        (
            '<div title="'
            + html.escape(tip, quote=True)
            + '" style="white-space:nowrap;overflow:hidden;'
            + 'text-overflow:ellipsis;line-height:2.1rem;">'
            + html.escape(value)
            + "</div>"
        ),
        unsafe_allow_html=True,
    )


def _sort_cache_rows(
    rows: list[dict],
    *,
    column: str,
    direction: str,
) -> list[dict]:
    """Sort cache table rows by column; blanks sort last."""
    reverse = direction == "desc"

    def sort_key(row: dict):
        value = row.get(column)
        if value is None or value == "":
            return (1, "")
        if isinstance(value, (int, float)):
            return (0, value)
        return (0, str(value).casefold())

    return sorted(rows, key=sort_key, reverse=reverse)


def _render_cache_sort_headers(
    columns: list[tuple[str, str]],
    widths: list[float],
    *,
    state_key: str,
    default_column: str,
    default_direction: str = "asc",
) -> tuple[str, str]:
    """Render clickable sort headers; return the active ``(column, direction)``."""
    st.markdown(
        """
        <style>
        div[data-testid="stHorizontalBlock"] button[kind="tertiary"] p,
        div[data-testid="stHorizontalBlock"] button[kind="tertiary"] span {
          white-space: nowrap !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    state = st.session_state.setdefault(
        state_key,
        {"column": default_column, "direction": default_direction},
    )
    active_column = str(state.get("column") or default_column)
    active_direction = str(state.get("direction") or default_direction)
    header_cols = st.columns(widths)
    for index, (column_id, label) in enumerate(columns):
        with header_cols[index]:
            if not column_id:
                continue
            marker = ""
            if column_id == active_column:
                marker = " ↑" if active_direction == "asc" else " ↓"
            if st.button(
                f"{label}{marker}",
                key=f"{state_key}_sort_{column_id}",
                type="tertiary",
                use_container_width=True,
            ):
                if column_id == active_column:
                    state["direction"] = (
                        "desc" if active_direction == "asc" else "asc"
                    )
                else:
                    state["column"] = column_id
                    state["direction"] = "asc"
                st.rerun()
    return active_column, active_direction


def _cache_table_window(
    total_rows: int,
    *,
    key: str,
    page_size: int = 50,
) -> tuple[int, int]:
    """Return a 50-row window into a large cache table."""
    if total_rows <= 0:
        return 0, 0
    page_count = max(1, (total_rows + page_size - 1) // page_size)
    page = int(st.session_state.get(key, 1) or 1)
    page = max(1, min(page, page_count))
    st.session_state[key] = page
    if page_count > 1:
        nav = st.columns([1, 2.4, 1])
        with nav[0]:
            if st.button(
                "Previous",
                key=f"{key}_prev",
                disabled=page <= 1,
                use_container_width=True,
            ):
                st.session_state[key] = page - 1
                st.rerun()
        with nav[1]:
            start_label = (page - 1) * page_size + 1
            end_label = min(page * page_size, total_rows)
            st.caption(
                f"Rows {start_label:,}–{end_label:,} of {total_rows:,} "
                "(50 per page so download buttons stay responsive)"
            )
        with nav[2]:
            if st.button(
                "Next",
                key=f"{key}_next",
                disabled=page >= page_count,
                use_container_width=True,
            ):
                st.session_state[key] = page + 1
                st.rerun()
    start = (page - 1) * page_size
    return start, min(start + page_size, total_rows)


def _render_cache_action_table(
    rows: list[dict],
    *,
    columns: list[tuple[str, str]],
    widths: list[float],
    formatters: dict,
    state_key: str,
    default_sort_column: str,
    default_sort_direction: str = "asc",
    region_code: str,
    year: int,
    can_download: bool,
    download_active: bool,
    row_download_key,
    row_download_label,
    row_download_day=None,
    row_download_loc_id=None,
    row_can_download,
) -> None:
    """Scrollable cache table with sortable headers and per-row download icons."""
    st.markdown(
        """
        <style>
        /* Icon-only cache download buttons: center glyph in the control. */
        div[data-testid="stHorizontalBlock"] button[kind="secondary"]:has(
          [data-testid="stIconMaterial"]
        ) {
          display: inline-flex !important;
          align-items: center !important;
          justify-content: center !important;
          gap: 0 !important;
          min-width: 2.35rem !important;
          padding-left: 0.55rem !important;
          padding-right: 0.55rem !important;
        }
        div[data-testid="stHorizontalBlock"] button[kind="secondary"]:has(
          [data-testid="stIconMaterial"]
        ) p {
          display: none !important;
        }
        div[data-testid="stHorizontalBlock"] button[kind="secondary"]:has(
          [data-testid="stIconMaterial"]
        ) [data-testid="stIconMaterial"] {
          margin: 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    sort_column, sort_direction = _render_cache_sort_headers(
        columns,
        widths,
        state_key=state_key,
        default_column=default_sort_column,
        default_direction=default_sort_direction,
    )
    sorted_rows = _sort_cache_rows(
        rows,
        column=sort_column,
        direction=sort_direction,
    )
    start, end = _cache_table_window(
        len(sorted_rows),
        key=f"{state_key}_page",
    )
    page_rows = sorted_rows[start:end]
    # Keep a stable viewport height once the page is full, so dense tables scroll
    # inside the page instead of stretching the whole screen.
    body_height = (
        _dataframe_height(50, row_height=42) if len(page_rows) > 50 else None
    )
    body = (
        st.container(height=body_height)
        if body_height is not None
        else st.container()
    )
    with body:
        for row in page_rows:
            cols = st.columns(widths)
            for index, (column_id, _label) in enumerate(columns):
                with cols[index]:
                    if not column_id:
                        if can_download and row_can_download(row):
                            day = row_download_day(row) if row_download_day else None
                            loc_id = (
                                row_download_loc_id(row)
                                if row_download_loc_id
                                else None
                            )
                            label = row_download_label(row)
                            missing = int(row.get("Missing") or 0)
                            min_species = int(
                                st.session_state.get(
                                    "checklist_download_min_species", 0
                                )
                                or 0
                            )
                            st.button(
                                "",
                                icon=":material/download:",
                                key=row_download_key(row),
                                disabled=download_active,
                                help=(
                                    f"Download {missing:,} missing checklist(s) "
                                    f"for {label} in the background"
                                    + (
                                        f" (≥{min_species} species)"
                                        if min_species > 0
                                        else ""
                                    )
                                    + "."
                                ),
                                width="content",
                                on_click=_queue_checklist_download,
                                kwargs={
                                    "region_code": region_code,
                                    "year": year,
                                    "day": day,
                                    "loc_id": loc_id,
                                    "min_species": min_species,
                                    "label": label,
                                },
                            )
                        elif can_download:
                            _nowrap_cell("✓")
                        continue
                    formatter = formatters.get(column_id)
                    value = formatter(row) if formatter else row.get(column_id)
                    _nowrap_cell(value)


def general_cache_inventory(
    region_code: str | None = None,
    *,
    coverage: dict | None = None,
) -> list[dict]:
    """List non-checklist JSON caches available in the project root."""
    root = Path(__file__).parent
    region = (region_code or "").strip()
    historical_total = int((coverage or {}).get("historical_total") or 0)
    has_historical_list = bool((coverage or {}).get("has_historical_list"))
    missing_by_loader: dict[str, int] = {}
    if region:
        missing_by_loader = {
            "photo": (
                len(missing_region_photo_cache_codes(region))
                if has_historical_list
                else 0
            ),
            "gallery": (
                len(missing_region_gallery_cache_codes(region))
                if has_historical_list
                else 0
            ),
            "similar": (
                len(missing_region_similar_cache_codes(region))
                if has_historical_list
                else 0
            ),
            "local_last_seen": (
                len(missing_region_local_last_seen_codes(region))
                if has_historical_list
                else 0
            ),
            "region_species": 0 if has_historical_list else 1,
        }
    file_coverage: dict[str, dict] = {
        "inaturalist_cache.json": {
            "covered": int((coverage or {}).get("in_photo_cache") or 0),
            "pct": (coverage or {}).get("photo_pct"),
            "loader": "photo",
            "missing_kind": "photo",
        },
        "inaturalist_gallery_cache.json": {
            "covered": int((coverage or {}).get("in_gallery_cache") or 0),
            "pct": (coverage or {}).get("gallery_pct"),
            "loader": "gallery",
            "missing_kind": "gallery",
        },
        "inaturalist_similar_cache.json": {
            "covered": int((coverage or {}).get("in_similar_cache") or 0),
            "pct": (coverage or {}).get("similar_pct"),
            "loader": "similar",
            "missing_kind": "similar",
        },
    }
    if region:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in region
        )
        file_coverage[f"ebird_{safe}_local_last_seen.json"] = {
            "covered": int((coverage or {}).get("in_checklist_cache") or 0),
            "pct": (coverage or {}).get("checklist_pct"),
            "loader": None,
            "missing_kind": "local_last_seen",
        }
        file_coverage["ebird_region_species_cache.json"] = {
            "covered": (
                historical_total if (coverage or {}).get("has_historical_list") else 0
            ),
            "pct": (
                100.0
                if (coverage or {}).get("has_historical_list") and historical_total
                else (0.0 if historical_total else None)
            ),
            "loader": "region_species",
            "missing_kind": None,
        }

    rows: list[dict] = []
    for path in sorted(root.glob("*.json")):
        name = path.name
        if (
            name.startswith("ebird_")
            and ("checklists_" in name or "checklist_" in name)
        ):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry_count: int | None = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if name == "ebird_taxonomy_cache.json" and isinstance(payload, dict):
                taxa = payload.get("taxa")
                entry_count = len(taxa) if isinstance(taxa, dict) else 0
            elif name == "ebird_region_list_cache.json" and isinstance(payload, dict):
                lists = payload.get("lists")
                entry_count = len(lists) if isinstance(lists, dict) else 0
            elif name.endswith("_local_last_seen.json") and isinstance(payload, dict):
                by_code = payload.get("by_code")
                entry_count = len(by_code) if isinstance(by_code, dict) else 0
            elif isinstance(payload, dict) and isinstance(payload.get("hotspots"), list):
                entry_count = len(payload["hotspots"])
            elif isinstance(payload, (dict, list)):
                entry_count = len(payload)
        except (OSError, ValueError):
            pass
        cover = file_coverage.get(name)
        region_birds = "—"
        region_pct = "—"
        missing_count = None
        loader = None
        missing_kind = None
        if cover is not None:
            loader = cover.get("loader")
            missing_kind = cover.get("missing_kind")
            if historical_total:
                covered = int(cover.get("covered") or 0)
                pct = cover.get("pct")
                region_birds = f"{covered:,}/{historical_total:,}"
                region_pct = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "—"
            elif region:
                region_birds = "0/0"
            kind_for_missing = missing_kind or loader
            if kind_for_missing in missing_by_loader:
                missing_count = int(missing_by_loader.get(kind_for_missing, 0))
        rows.append(
            {
                "Cache": name,
                "Size": _format_bytes(size),
                "Bytes": size,
                "Entries": entry_count if entry_count is not None else "—",
                "Region birds": region_birds,
                "Region %": region_pct,
                "Modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "loader": loader,
                "missing_kind": missing_kind,
                "missing": missing_count,
            }
        )
    if region and all(row["Cache"] != "ebird_region_species_cache.json" for row in rows):
        rows.append(
            {
                "Cache": "ebird_region_species_cache.json",
                "Size": "0 B",
                "Bytes": 0,
                "Entries": 0,
                "Region birds": "0/0",
                "Region %": "0.0%",
                "Modified": "—",
                "loader": "region_species",
                "missing_kind": None,
                "missing": 1,
            }
        )
    return sorted(rows, key=lambda row: int(row["Bytes"]), reverse=True)


def ensure_gallery_image_cache(
    birds: list[dict],
    *,
    max_photos: int = 24,
) -> None:
    """Warm gallery photo caches with a count + ETA progress indicator."""
    warmed: set[str] = st.session_state.setdefault(
        "gallery_image_cache_warmed", set()
    )
    pending: list[tuple[str, str | None, str]] = []
    seen: set[str] = set()
    for bird in birds:
        code = str(bird.get("code") or "").strip()
        sci_raw = bird.get("sciName")
        sci = str(sci_raw).strip() if sci_raw else None
        lookup = code or (sci or "")
        if not lookup:
            continue
        warm_key = f"{lookup}|{max_photos}|{GALLERY_CACHE_VERSION}"
        if warm_key in warmed or warm_key in seen:
            continue
        seen.add(warm_key)
        pending.append((code, sci, warm_key))

    total = len(pending)
    if total == 0:
        return

    progress = st.progress(
        0.0,
        text=f"Loading image cache 0/{total} · estimating…",
    )
    recent_durations: list[float] = []
    for i, (code, sci, warm_key) in enumerate(pending, start=1):
        item_started = time.perf_counter()
        gallery_payload_for_code(code, sci, max_photos=max_photos)
        recent_durations.append(time.perf_counter() - item_started)
        if len(recent_durations) > 10:
            recent_durations.pop(0)
        warmed.add(warm_key)
        avg = sum(recent_durations) / len(recent_durations)
        remaining = avg * (total - i) if i < total else 0.0
        eta_label = _format_eta(remaining) if i < total else "done"
        progress.progress(
            i / total,
            text=(
                f"Loading image cache {i}/{total} · "
                f"ETA {eta_label} · {avg:.1f}s/bird"
            ),
        )
    progress.empty()


def missing_region_photo_cache_codes(region_code: str) -> list[str]:
    """Historical region species codes with no iNaturalist photo-cache entry yet."""
    codes = load_disk_region_species_codes(region_code) or []
    if not codes:
        return []
    try:
        photo_cache = json.loads(INAT_PHOTO_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        photo_cache = {}
    if not isinstance(photo_cache, dict):
        photo_cache = {}
    return [code for code in codes if code not in photo_cache]


def missing_region_gallery_cache_codes(region_code: str) -> list[str]:
    """Historical codes missing a current-version gallery cache entry."""
    codes = load_disk_region_species_codes(region_code) or []
    if not codes:
        return []
    code_set = set(codes)
    try:
        gallery_cache = json.loads(INAT_GALLERY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        gallery_cache = {}
    if not isinstance(gallery_cache, dict):
        gallery_cache = {}
    gallery_by_code: dict[str, dict] = {}
    for key, value in gallery_cache.items():
        if not isinstance(value, dict) or not value:
            continue
        if key in code_set:
            gallery_by_code.setdefault(str(key), value)
        nested = str(value.get("ebird_code") or "").strip()
        if nested and nested in code_set:
            gallery_by_code.setdefault(nested, value)
    return [
        code
        for code in codes
        if (gallery_by_code.get(code) or {}).get("cache_version") != GALLERY_CACHE_VERSION
    ]


def missing_region_similar_cache_codes(region_code: str) -> list[str]:
    """Historical codes missing a similar-species cache entry."""
    codes = load_disk_region_species_codes(region_code) or []
    if not codes:
        return []
    code_set = set(codes)
    try:
        gallery_cache = json.loads(INAT_GALLERY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        gallery_cache = {}
    if not isinstance(gallery_cache, dict):
        gallery_cache = {}
    gallery_by_code: dict[str, dict] = {}
    for key, value in gallery_cache.items():
        if not isinstance(value, dict) or not value:
            continue
        if key in code_set:
            gallery_by_code.setdefault(str(key), value)
        nested = str(value.get("ebird_code") or "").strip()
        if nested and nested in code_set:
            gallery_by_code.setdefault(nested, value)
    try:
        similar_cache = json.loads(INAT_SIMILAR_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        similar_cache = {}
    if not isinstance(similar_cache, dict):
        similar_cache = {}
    similar_codes: set[str] = set()
    similar_taxon_ids: set[str] = set()
    for key in similar_cache:
        text = str(key)
        if text.startswith("code:"):
            similar_codes.add(text[5:].split("|", 1)[0])
        elif text.startswith("taxon:"):
            similar_taxon_ids.add(text[6:])
    missing: list[str] = []
    for code in codes:
        if code in similar_codes:
            continue
        taxon_id = (gallery_by_code.get(code) or {}).get("taxon_id")
        if taxon_id is not None and str(taxon_id) in similar_taxon_ids:
            continue
        missing.append(code)
    return missing


def missing_region_local_last_seen_codes(region_code: str) -> list[str]:
    """Historical codes with no entry in the local last-seen checklist index."""
    codes = load_disk_region_species_codes(region_code) or []
    if not codes:
        return []
    local = set(build_local_last_seen_index(region_code))
    return [code for code in codes if code not in local]


def missing_codes_for_cache_kind(region_code: str, kind: str | None) -> list[str]:
    """Return missing historical species codes for a cache maintenance kind."""
    region = (region_code or "").strip()
    if not region or not kind:
        return []
    if kind == "photo":
        return missing_region_photo_cache_codes(region)
    if kind == "gallery":
        return missing_region_gallery_cache_codes(region)
    if kind == "similar":
        return missing_region_similar_cache_codes(region)
    if kind == "local_last_seen":
        return missing_region_local_last_seen_codes(region)
    return []


def missing_species_display_rows(codes: list[str]) -> list[dict]:
    """Resolve missing eBird codes into display rows with common/sci names."""
    cleaned = [str(code).strip() for code in codes if str(code).strip()]
    if not cleaned:
        return []
    taxa: dict[str, dict] = {}
    try:
        taxa = EBirdClient().species_taxa(cleaned)
    except MissingEbirdApiKey:
        ensure_api_key()
        taxa = {}
    except Exception:
        taxa = {}
    rows: list[dict] = []
    for code in cleaned:
        taxon = taxa.get(code) or {}
        common = str(taxon.get("comName") or "").strip()
        sci = str(taxon.get("sciName") or "").strip()
        rows.append(
            {
                "Code": code,
                "Common name": common.split(" (", 1)[0].strip() or "—",
                "Scientific name": sci or "—",
            }
        )
    return rows


def _warm_codes_with_progress(
    codes: list[str],
    *,
    label: str,
    worker,
) -> dict[str, int]:
    """Run a per-code cache warmer with a Streamlit progress/ETA bar."""
    total = len(codes)
    if total == 0:
        return {"missing": 0, "attempted": 0, "found": 0}
    taxa: dict[str, dict] = {}
    try:
        taxa = EBirdClient().species_taxa(codes)
    except MissingEbirdApiKey:
        ensure_api_key()
        taxa = {}
    except Exception:
        taxa = {}
    progress = st.progress(0.0, text=f"Loading {label} 0/{total:,} · estimating…")
    recent_durations: list[float] = []
    found = 0
    for index, code in enumerate(codes, start=1):
        item_started = time.perf_counter()
        sci = str((taxa.get(code) or {}).get("sciName") or "").strip() or None
        try:
            if worker(code, sci):
                found += 1
        except requests.RequestException:
            pass
        recent_durations.append(time.perf_counter() - item_started)
        if len(recent_durations) > 10:
            recent_durations.pop(0)
        avg = sum(recent_durations) / len(recent_durations)
        remaining = avg * (total - index) if index < total else 0.0
        eta_label = _format_eta(remaining) if index < total else "done"
        progress.progress(
            index / total,
            text=(
                f"Loading {label} {index:,}/{total:,} · "
                f"ETA {eta_label} · {avg:.1f}s/bird"
            ),
        )
    progress.empty()
    return {"missing": total, "attempted": total, "found": found}


def warm_missing_region_photo_cache(region_code: str) -> dict[str, int]:
    """Fetch iNaturalist photo metadata for historical species missing from cache."""
    def _worker(code: str, sci: str | None) -> bool:
        return bool(species_photo(code, scientific_name=sci))

    result = _warm_codes_with_progress(
        missing_region_photo_cache_codes(region_code),
        label="photo cache",
        worker=_worker,
    )
    try:
        inaturalist_photo_for_code.clear()
    except Exception:
        pass
    return result


def warm_missing_region_gallery_cache(region_code: str) -> dict[str, int]:
    """Fetch gallery payloads for historical species missing from gallery cache."""
    def _worker(code: str, sci: str | None) -> bool:
        payload = gallery_payload_for_code(code, sci, max_photos=24)
        return bool(payload and (payload.get("photos") or payload.get("common_name")))

    result = _warm_codes_with_progress(
        missing_region_gallery_cache_codes(region_code),
        label="gallery cache",
        worker=_worker,
    )
    try:
        gallery_payload_for_code.clear()
    except Exception:
        pass
    return result


def warm_missing_region_similar_cache(region_code: str) -> dict[str, int]:
    """Fetch similar-species lists for historical species missing from similar cache."""
    def _worker(code: str, sci: str | None) -> bool:
        taxon_id = None
        try:
            gallery = gallery_payload_for_code(code, sci, max_photos=24)
        except Exception:
            gallery = None
        if isinstance(gallery, dict):
            raw_id = gallery.get("taxon_id")
            if raw_id is not None:
                try:
                    taxon_id = int(raw_id)
                except (TypeError, ValueError):
                    taxon_id = None
        similar = species_similar(
            taxon_id=taxon_id,
            ebird_code=code,
            scientific_name=sci,
            limit=12,
        )
        return bool(similar)

    result = _warm_codes_with_progress(
        missing_region_similar_cache_codes(region_code),
        label="similar cache",
        worker=_worker,
    )
    try:
        gallery_payload_for_code.clear()
        similar_species_for_taxon.clear()
    except Exception:
        pass
    return result


def _load_json_object(path: Path) -> dict:
    """Load a JSON object from disk, or {} if missing/invalid."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def similar_cache_result_species() -> list[dict]:
    """Unique species that appear as similar-species *results* (not just sources)."""
    similar_cache = _load_json_object(INAT_SIMILAR_CACHE_PATH)
    unique: dict[str, dict] = {}
    for value in similar_cache.values():
        if not isinstance(value, list):
            continue
        for item in value:
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
    """Current-version gallery entries indexed by code, taxon id, and sci name."""
    gallery_cache = _load_json_object(INAT_GALLERY_CACHE_PATH)
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


def _similar_result_has_gallery(
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


def similar_cache_media_coverage() -> dict:
    """How many similar-species result birds have photo and gallery caches."""
    birds = similar_cache_result_species()
    photo_cache = _load_json_object(INAT_PHOTO_CACHE_PATH)
    gallery_codes, gallery_taxon_ids, gallery_sci_names = _gallery_cache_current_keys()
    photo_covered = 0
    gallery_covered = 0
    unresolved = 0
    for bird in birds:
        code = str(bird.get("code") or "").strip()
        if not code:
            unresolved += 1
        elif code in photo_cache:
            photo_covered += 1
        if _similar_result_has_gallery(
            bird,
            gallery_codes=gallery_codes,
            gallery_taxon_ids=gallery_taxon_ids,
            gallery_sci_names=gallery_sci_names,
        ):
            gallery_covered += 1
    total = len(birds)
    return {
        "total": total,
        "unresolved": unresolved,
        "photo_covered": photo_covered,
        "photo_missing": max(0, total - photo_covered),
        "gallery_covered": gallery_covered,
        "gallery_missing": max(0, total - gallery_covered),
    }


def missing_similar_result_photo_codes() -> list[str]:
    """eBird codes for similar-species results missing from the photo cache."""
    photo_cache = _load_json_object(INAT_PHOTO_CACHE_PATH)
    missing: list[str] = []
    seen: set[str] = set()
    for bird in similar_cache_result_species():
        code = str(bird.get("code") or "").strip()
        if not code or code in seen or code in photo_cache:
            continue
        seen.add(code)
        missing.append(code)
    return missing


def missing_similar_result_gallery_codes() -> list[str]:
    """eBird codes (or sci names) for similar-species results missing gallery cache."""
    gallery_codes, gallery_taxon_ids, gallery_sci_names = _gallery_cache_current_keys()
    missing: list[str] = []
    seen: set[str] = set()
    for bird in similar_cache_result_species():
        if _similar_result_has_gallery(
            bird,
            gallery_codes=gallery_codes,
            gallery_taxon_ids=gallery_taxon_ids,
            gallery_sci_names=gallery_sci_names,
        ):
            continue
        lookup = str(bird.get("code") or "").strip() or str(
            bird.get("sciName") or ""
        ).strip()
        if not lookup or lookup in seen:
            continue
        seen.add(lookup)
        missing.append(lookup)
    return missing


def missing_similar_result_display_rows(kind: str) -> list[dict]:
    """Display rows for similar-species results missing photo or gallery cache."""
    photo_cache = _load_json_object(INAT_PHOTO_CACHE_PATH)
    gallery_codes, gallery_taxon_ids, gallery_sci_names = _gallery_cache_current_keys()
    rows: list[dict] = []
    for bird in similar_cache_result_species():
        if kind == "photo":
            code = str(bird.get("code") or "").strip()
            if code and code in photo_cache:
                continue
        elif kind == "gallery":
            if _similar_result_has_gallery(
                bird,
                gallery_codes=gallery_codes,
                gallery_taxon_ids=gallery_taxon_ids,
                gallery_sci_names=gallery_sci_names,
            ):
                continue
        else:
            continue
        rows.append(
            {
                "Code": str(bird.get("code") or "").strip() or "—",
                "Common name": str(bird.get("name") or "—"),
                "Scientific name": str(bird.get("sciName") or "—"),
            }
        )
    return rows


def warm_missing_similar_result_photo_cache() -> dict[str, int]:
    """Fetch iNaturalist photo metadata for similar-species results missing it."""
    def _worker(code: str, sci: str | None) -> bool:
        return bool(species_photo(code, scientific_name=sci))

    result = _warm_codes_with_progress(
        missing_similar_result_photo_codes(),
        label="similar-bird photo cache",
        worker=_worker,
    )
    try:
        inaturalist_photo_for_code.clear()
    except Exception:
        pass
    return result


def warm_missing_similar_result_gallery_cache() -> dict[str, int]:
    """Fetch gallery payloads for similar-species results missing a current cache."""
    def _worker(code: str, sci: str | None) -> bool:
        payload = gallery_payload_for_code(
            code, sci, max_photos=DEFAULT_MAX_PHOTOS
        )
        return bool(payload and (payload.get("photos") or payload.get("common_name")))

    result = _warm_codes_with_progress(
        missing_similar_result_gallery_codes(),
        label="similar-bird gallery cache",
        worker=_worker,
    )
    try:
        gallery_payload_for_code.clear()
    except Exception:
        pass
    return result


def warm_missing_region_species_list(region_code: str) -> dict[str, int]:
    """Fetch and persist the historical species list for a region."""
    region = (region_code or "").strip()
    if not region:
        return {"missing": 0, "attempted": 0, "found": 0}
    from ebird import REGION_SPECIES_CACHE_PATH, _load_json_file, _save_json_file

    codes = EBirdClient().region_species_codes(region)
    cache = _load_json_file(REGION_SPECIES_CACHE_PATH)
    cache[region] = codes
    _save_json_file(REGION_SPECIES_CACHE_PATH, cache)
    return {"missing": 1, "attempted": 1, "found": len(codes)}


def open_gallery(
    birds: list[dict],
    *,
    title: str = "Gallery",
    saved_id: str | None = None,
    view_mode: str | None = None,
    source_title: str | None = None,
    sort: str | None = None,
) -> None:
    """Store a bird list in session state and open the gallery view."""
    cleaned: list[dict] = []
    seen: set[str] = set()
    for bird in birds:
        code = str(bird.get("code") or "").strip()
        name = str(bird.get("name") or bird.get("Species") or code).strip()
        if not code and not name:
            continue
        key = code or normalize_common_name(name)
        if key in seen:
            continue
        seen.add(key)
        cleaned_bird = {
            "code": code,
            "name": name.split(" (", 1)[0].strip() or name,
            "sciName": bird.get("sciName") or "",
        }
        if any(
            key in bird
            for key in ("is_new_region", "New_region", "is_new", "New")
        ):
            cleaned_bird["is_new_region"] = bool(
                bird.get("is_new_region")
                if "is_new_region" in bird
                else bird.get("New_region")
                if "New_region" in bird
                else bird.get("is_new") or bird.get("New")
            )
            cleaned_bird["is_new"] = cleaned_bird["is_new_region"]
        if "is_new_world" in bird or "New_world" in bird:
            cleaned_bird["is_new_world"] = bool(
                bird.get("is_new_world")
                if "is_new_world" in bird
                else bird.get("New_world")
            )
        cleaned.append(cleaned_bird)
    if not cleaned:
        st.warning("No birds available for the gallery.")
        return
    st.session_state.gallery_birds = cleaned
    st.session_state.gallery_title = title
    st.session_state.gallery_bird_index = 0
    st.session_state.gallery_image_index = 0
    st.session_state.gallery_show_info = False
    st.session_state.gallery_show_similar = True
    st.session_state.setdefault("gallery_hide_similar_never_seen", True)
    if view_mode in {"summary", "list", "standard"}:
        st.session_state.gallery_view_mode = view_mode
        st.session_state.gallery_view_mode_pending = view_mode
    else:
        st.session_state.gallery_view_mode_pending = "summary"
    if sort in GALLERY_SORT_OPTIONS:
        st.session_state.gallery_sort = sort
        st.session_state.gallery_sort_pref = sort
        st.session_state.gallery_sort_pending = sort
    st.session_state.gallery_list_image_indices = {}
    st.session_state.pop("gallery_image_cache_warmed", None)
    st.session_state.pop("gallery_summary_page", None)
    origin = source_title or title
    if saved_id:
        st.session_state.gallery_saved_id = saved_id
        st.session_state.gallery_saved_dirty = False
        st.session_state.gallery_source_title = origin
        st.session_state.gallery_name = title
        st.session_state.gallery_title = title
        _set_saved_gallery_query(saved_id)
    else:
        st.session_state.pop("gallery_saved_id", None)
        st.session_state.gallery_saved_dirty = False
        st.session_state.gallery_source_title = origin
        st.session_state.gallery_name = default_gallery_name()
        st.session_state.gallery_title = st.session_state.gallery_name
        _clear_saved_gallery_query()
    st.session_state.dashboard_pref = "gallery"
    st.rerun()


def default_gallery_name(when: datetime | None = None) -> str:
    """Date/time used when a gallery has no specific name."""
    return (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _saved_gallery_path(gallery_id: str) -> Path:
    return SAVED_GALLERIES_DIR / f"{gallery_id}.json"


def _valid_saved_gallery_id(gallery_id: str) -> bool:
    return bool(SAVED_GALLERY_ID_RE.fullmatch((gallery_id or "").strip()))


def _new_saved_gallery_id(when: datetime | None = None) -> str:
    when = when or datetime.now()
    base = when.strftime("%Y-%m-%d_%H%M%S")
    if not _saved_gallery_path(base).exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}_{suffix}"
        if not _saved_gallery_path(candidate).exists():
            return candidate
    return f"{base}_{os.getpid()}"


def _query_param_raw(name: str) -> str | None:
    params = getattr(st, "query_params", None)
    if params is None:
        return None
    try:
        if name not in params:
            return None
        raw = params.get(name)
    except Exception:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    value = str(raw or "").strip()
    return value or None


def _clear_saved_gallery_query() -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    try:
        if SAVED_GALLERY_QUERY in params:
            del params[SAVED_GALLERY_QUERY]
    except Exception:
        try:
            params.pop(SAVED_GALLERY_QUERY, None)
        except Exception:
            pass


def _set_saved_gallery_query(gallery_id: str) -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    try:
        params[SAVED_GALLERY_QUERY] = gallery_id
    except Exception:
        pass


def app_base_url() -> str:
    """Current app origin+path with query string removed."""
    raw = ""
    try:
        raw = str(getattr(st.context, "url", "") or "")
    except Exception:
        raw = ""
    if not raw:
        return ""
    parts = urlparse(raw)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))


def saved_gallery_url(gallery_id: str) -> str:
    base = app_base_url()
    if not base:
        return f"?{SAVED_GALLERY_QUERY}={gallery_id}"
    return f"{base}?{SAVED_GALLERY_QUERY}={gallery_id}"


def load_saved_gallery(gallery_id: str) -> dict | None:
    if not _valid_saved_gallery_id(gallery_id):
        return None
    path = _saved_gallery_path(gallery_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    birds = payload.get("birds")
    if not isinstance(birds, list) or not birds:
        return None
    payload["id"] = str(payload.get("id") or gallery_id)
    payload["birds"] = birds
    return payload


def list_saved_galleries() -> list[dict]:
    if not SAVED_GALLERIES_DIR.is_dir():
        return []
    listed: list[dict] = []
    for path in SAVED_GALLERIES_DIR.glob("*.json"):
        payload = load_saved_gallery(path.stem)
        if payload:
            listed.append(payload)
    listed.sort(key=lambda item: str(item.get("saved_at") or item.get("id") or ""), reverse=True)
    return listed


def delete_saved_gallery(gallery_id: str) -> None:
    """Remove a saved gallery file from disk."""
    if not _valid_saved_gallery_id(gallery_id):
        return
    path = _saved_gallery_path(gallery_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if st.session_state.get("gallery_saved_id") == gallery_id:
        st.session_state.pop("gallery_saved_id", None)
        st.session_state.pop("gallery_saved_dirty", None)
        _clear_saved_gallery_query()


def rename_saved_gallery(gallery_id: str, name: str) -> str | None:
    """Update the display name of an existing saved gallery."""
    payload = load_saved_gallery(gallery_id)
    if not payload:
        return None
    cleaned = str(name or "").strip() or default_gallery_name()
    payload["title"] = cleaned
    try:
        _saved_gallery_path(gallery_id).write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        st.error(f"Could not rename gallery: {exc}")
        return None
    if st.session_state.get("gallery_saved_id") == gallery_id:
        st.session_state.gallery_name = cleaned
        st.session_state.gallery_title = cleaned
    return cleaned


def _on_gallery_name_change() -> None:
    if st.session_state.get("gallery_saved_id"):
        st.session_state.gallery_saved_dirty = True


def save_current_gallery() -> str | None:
    """Persist the current gallery under its name and return its id."""
    birds = list(st.session_state.get("gallery_birds") or [])
    if not birds:
        st.warning("Nothing to save.")
        return None
    now = datetime.now()
    old_id = str(st.session_state.get("gallery_saved_id") or "").strip()
    new_id = _new_saved_gallery_id(now)
    title = str(st.session_state.get("gallery_name") or "").strip() or default_gallery_name(now)
    source_title = (
        str(st.session_state.get("gallery_source_title") or "").strip() or "Gallery"
    )
    view_mode = st.session_state.get("gallery_view_mode")
    if view_mode not in {"summary", "list", "standard"}:
        view_mode = "summary"
    payload = {
        "id": new_id,
        "saved_at": now.isoformat(timespec="seconds"),
        "title": title,
        "source_title": source_title,
        "view_mode": view_mode,
        "sort": current_gallery_sort(),
        "birds": birds,
    }
    try:
        SAVED_GALLERIES_DIR.mkdir(parents=True, exist_ok=True)
        _saved_gallery_path(new_id).write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        st.error(f"Could not save gallery: {exc}")
        return None
    if old_id and old_id != new_id:
        old_path = _saved_gallery_path(old_id)
        try:
            if old_path.is_file():
                old_path.unlink()
        except OSError:
            pass
    st.session_state.gallery_saved_id = new_id
    st.session_state.gallery_name = title
    st.session_state.gallery_title = title
    st.session_state.gallery_source_title = source_title
    st.session_state.gallery_saved_dirty = False
    _set_saved_gallery_query(new_id)
    return new_id


def maybe_open_saved_gallery_from_query() -> None:
    """Open a saved gallery when the URL contains ``?saved_gallery=``."""
    gallery_id = _query_param_raw(SAVED_GALLERY_QUERY)
    if not gallery_id:
        return
    if (
        st.session_state.get("gallery_saved_id") == gallery_id
        and st.session_state.get("gallery_birds")
    ):
        return
    payload = load_saved_gallery(gallery_id)
    if not payload:
        st.session_state.saved_gallery_missing = gallery_id
        _clear_saved_gallery_query()
        return
    open_gallery(
        payload.get("birds") or [],
        title=str(payload.get("title") or gallery_id),
        saved_id=str(payload.get("id") or gallery_id),
        view_mode=payload.get("view_mode") if isinstance(payload.get("view_mode"), str) else None,
        source_title=payload.get("source_title") if isinstance(payload.get("source_title"), str) else None,
        sort=payload.get("sort") if isinstance(payload.get("sort"), str) else None,
    )


GALLERY_SESSION_KEYS = (
    "gallery_birds",
    "gallery_title",
    "gallery_name",
    "gallery_source_title",
    "gallery_saved_id",
    "gallery_saved_dirty",
    "gallery_bird_index",
    "gallery_image_index",
    "gallery_show_info",
    "gallery_show_similar",
    "gallery_hide_similar_never_seen",
    "gallery_last_swipe_t",
    "gallery_compare_birds",
    "gallery_compare_bird_index",
    "gallery_compare_image_index",
    "gallery_compare_last_swipe_t",
    "gallery_visible_indices",
    "gallery_view_mode",
    "gallery_view_mode_pending",
    "gallery_sort",
    "gallery_sort_radio",
    "gallery_sort_pending",
    "gallery_list_image_indices",
    "gallery_list_last_swipe_t",
    "gallery_image_cache_warmed",
    "gallery_summary_last_click_t",
    "gallery_summary_page",
    "gallery_show_filter",
    "gallery_show_view_picker",
    "gallery_show_legends",
    "gallery_show_nav_buttons",
    "gallery_view_mode_radio",
    "gallery_filter_radio",
)


HOME_SCREEN = "saved"
DASHBOARD_SCREENS = {
    "saved": "Saved galleries",
    "checklists": "Checklists",
    "cache": "Checklist cache",
    "maintenance": "Cache maintenance",
}


def close_gallery() -> None:
    """Leave the gallery and return to saved galleries."""
    for key in GALLERY_SESSION_KEYS:
        st.session_state.pop(key, None)
    _clear_saved_gallery_query()
    st.session_state.dashboard_pref = HOME_SCREEN
    st.rerun()


def current_dashboard() -> str:
    """Which home-section screen to show (not a widget key — survives navigation)."""
    value = st.session_state.get("dashboard_pref")
    if value == "gallery" and st.session_state.get("gallery_birds"):
        return "gallery"
    if value not in DASHBOARD_SCREENS:
        legacy = st.session_state.get("dashboard_screen")
        value = legacy if legacy in DASHBOARD_SCREENS else HOME_SCREEN
        st.session_state.dashboard_pref = value
    return value


def go_dashboard(screen: str) -> None:
    if screen == "gallery":
        if st.session_state.get("gallery_birds"):
            st.session_state.dashboard_pref = "gallery"
            st.rerun()
        screen = HOME_SCREEN
    if screen not in DASHBOARD_SCREENS:
        screen = HOME_SCREEN
    st.session_state.dashboard_pref = screen
    st.rerun()


def render_app_nav_buttons(*, current: str, key_prefix: str) -> None:
    """Screen links for the hamburger menu."""
    if st.session_state.get("gallery_birds"):
        is_gallery = current == "gallery"
        if st.button(
            "Gallery",
            use_container_width=True,
            type="primary" if is_gallery else "tertiary",
            disabled=is_gallery,
            key=f"{key_prefix}_gallery",
        ):
            go_dashboard("gallery")
    for key, label in DASHBOARD_SCREENS.items():
        if st.button(
            label,
            use_container_width=True,
            type="primary" if key == current else "tertiary",
            disabled=key == current,
            key=f"{key_prefix}_{key}",
        ):
            go_dashboard(key)


def render_page_header(title: str, *, screen: str) -> None:
    """Title row with a hamburger menu for Checklists / cache / maintenance."""
    menu_col, title_col = st.columns([1, 16], vertical_alignment="center")
    with menu_col:
        with st.popover(
            " ",
            icon=":material/menu:",
            help="Open saved galleries, Gallery, Checklists, downloads, or cache maintenance",
        ):
            render_app_nav_buttons(current=screen, key_prefix=f"dashboard_nav_{screen}")
            if screen == "saved":
                st.divider()
                render_gallery_sort_controls()
    with title_col:
        st.title(title)


def render_saved_galleries() -> None:
    """Browse previously saved galleries and reopen or delete them."""
    render_page_header("Saved galleries", screen="saved")
    missing = st.session_state.pop("saved_gallery_missing", None)
    if missing:
        st.warning(f"Saved gallery `{missing}` was not found.")
    galleries = list_saved_galleries()
    if not galleries:
        st.info("No saved galleries yet. Open a gallery and tap the save icon.")
        return
    st.caption(
        f"{len(galleries)} saved · defaults to the date and time; you can give each gallery a name. "
        "Open, rename, or delete from each gallery below."
    )
    for index, item in enumerate(galleries):
        gallery_id = str(item.get("id") or "")
        title = str(item.get("title") or gallery_id)
        source = str(item.get("source_title") or "").strip()
        birds = item.get("birds") if isinstance(item.get("birds"), list) else []
        count = len(birds)
        url = saved_gallery_url(gallery_id)
        header = f"{title} · {count} species"
        if source and source != title:
            header = f"{title} · {source} · {count} species"
        with st.expander(header, expanded=index == 0):
            name_key = f"saved_gallery_name_{gallery_id}"
            if name_key not in st.session_state:
                st.session_state[name_key] = title
            name_col, rename_col, delete_col = st.columns(
                [4, 1, 1], vertical_alignment="bottom"
            )
            with name_col:
                st.text_input("Name", key=name_key)
            with rename_col:
                if st.button(
                    "Rename",
                    key=f"rename_saved_gallery_{gallery_id}",
                    use_container_width=True,
                ):
                    renamed = rename_saved_gallery(
                        gallery_id, str(st.session_state.get(name_key) or "")
                    )
                    if renamed:
                        st.rerun()
            with delete_col:
                confirm_key = f"confirm_delete_saved_{gallery_id}"
                if st.session_state.get(confirm_key):
                    if st.button(
                        "Confirm delete",
                        key=f"confirm_delete_saved_gallery_{gallery_id}",
                        use_container_width=True,
                        type="primary",
                    ):
                        st.session_state.pop(confirm_key, None)
                        delete_saved_gallery(gallery_id)
                        st.rerun()
                elif st.button(
                    "Delete",
                    key=f"delete_saved_gallery_{gallery_id}",
                    use_container_width=True,
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            render_species_thumbnail_table(
                sorted_gallery_birds(birds), columns=6, width=144
            )
            st.markdown(f"[Direct link]({url})")
            if st.button(
                "Open gallery",
                key=f"open_saved_gallery_{gallery_id}",
                use_container_width=True,
                type="primary",
            ):
                open_gallery(
                    birds,
                    title=title,
                    saved_id=gallery_id,
                    view_mode=item.get("view_mode")
                    if isinstance(item.get("view_mode"), str)
                    else None,
                    source_title=source or None,
                    sort=current_gallery_sort(),
                )


def _sync_life_list_scope_pref() -> None:
    value = st.session_state.get("life_list_scope")
    if value in {"all", "region", "world"}:
        st.session_state.life_list_scope_pref = value


def current_life_list_scope() -> str:
    """Life-list filter that survives gallery toolbar panels closing."""
    for key in ("life_list_scope", "life_list_scope_pref"):
        value = st.session_state.get(key)
        if value in {"all", "region", "world"}:
            return value
    return "all"


def current_gallery_view_mode() -> str:
    value = st.session_state.get("gallery_view_mode")
    if value in {"summary", "list", "standard"}:
        return value
    return "summary"


GALLERY_SORT_OPTIONS = {
    "taxonomic": "Taxonomic",
    "alpha": "Alphabetical",
    "original": "Original",
    "new_first": "New birds first",
    "scientific": "Scientific name",
}
DEFAULT_GALLERY_SORT = "taxonomic"

_TAXON_ORDER_BY_CODE: dict[str, float] | None = None
_TAXON_ORDER_BY_SCI: dict[str, float] | None = None


def current_gallery_sort() -> str:
    for key in ("gallery_sort_radio", "gallery_sort", "gallery_sort_pref"):
        value = st.session_state.get(key)
        if value in GALLERY_SORT_OPTIONS:
            return value
    return DEFAULT_GALLERY_SORT


def _on_gallery_sort_change() -> None:
    value = st.session_state.get("gallery_sort_radio")
    if value in GALLERY_SORT_OPTIONS:
        st.session_state.gallery_sort = value
        st.session_state.gallery_sort_pref = value
    st.session_state.pop("gallery_summary_page", None)


def render_gallery_sort_controls() -> None:
    """Shared sort radio for gallery and saved-galleries hamburger menus."""
    pending = st.session_state.pop("gallery_sort_pending", None)
    if pending in GALLERY_SORT_OPTIONS:
        st.session_state.gallery_sort_radio = pending
    elif "gallery_sort_radio" not in st.session_state:
        st.session_state.gallery_sort_radio = current_gallery_sort()
    st.radio(
        "Sort birds",
        options=list(GALLERY_SORT_OPTIONS),
        format_func=lambda value: GALLERY_SORT_OPTIONS[value],
        key="gallery_sort_radio",
        on_change=_on_gallery_sort_change,
        help="Original keeps the order this gallery was built with. Taxonomic follows the eBird / Clements sequence.",
    )
    st.session_state.gallery_sort = st.session_state.gallery_sort_radio
    st.session_state.gallery_sort_pref = st.session_state.gallery_sort_radio


def ebird_taxon_order_maps() -> tuple[dict[str, float], dict[str, float]]:
    """speciesCode / scientific name → eBird taxonOrder."""
    global _TAXON_ORDER_BY_CODE, _TAXON_ORDER_BY_SCI
    if _TAXON_ORDER_BY_CODE is None:
        by_code: dict[str, float] = {}
        by_sci: dict[str, float] = {}
        taxa = load_taxonomy_cache().get("taxa") or {}
        if isinstance(taxa, dict):
            for code, row in taxa.items():
                if not isinstance(row, dict):
                    continue
                raw_order = row.get("taxonOrder")
                if raw_order is None:
                    continue
                try:
                    order = float(raw_order)
                except (TypeError, ValueError):
                    continue
                by_code[str(code)] = order
                sci = str(row.get("sciName") or "").strip()
                if sci:
                    by_sci[sci.casefold()] = order
                    by_sci[binomial_sci_name(sci)] = order
        _TAXON_ORDER_BY_CODE = by_code
        _TAXON_ORDER_BY_SCI = by_sci
    return _TAXON_ORDER_BY_CODE, _TAXON_ORDER_BY_SCI


def gallery_taxon_order(bird: dict) -> float:
    """eBird taxonomic sequence; missing taxa sort last."""
    by_code, by_sci = ebird_taxon_order_maps()
    code = str(bird.get("code") or "").strip()
    if code and code in by_code:
        return by_code[code]
    sci = str(bird.get("sciName") or "").strip()
    if sci:
        order = by_sci.get(sci.casefold())
        if order is not None:
            return order
        order = by_sci.get(binomial_sci_name(sci))
        if order is not None:
            return order
    return float("inf")


def gallery_sort_key(bird: dict, index: int, mode: str) -> tuple:
    name = str(bird.get("name") or bird.get("Species") or "").casefold()
    sci = str(bird.get("sciName") or "").casefold()
    if mode == "alpha":
        return (name, sci, index)
    if mode == "scientific":
        return (sci or name, name, index)
    if mode == "taxonomic":
        return (gallery_taxon_order(bird), name, index)
    if mode == "new_first":
        if gallery_bird_is_new_world(bird):
            rank = 0
        elif gallery_bird_is_new_region(bird):
            rank = 1
        else:
            rank = 2
        return (rank, name, index)
    return (index,)


def sort_gallery_visible_indices(birds: list[dict], visible_indices: list[int]) -> list[int]:
    mode = current_gallery_sort()
    if mode == "original" or len(visible_indices) < 2:
        return list(visible_indices)
    return sorted(
        visible_indices,
        key=lambda idx: gallery_sort_key(birds[idx], idx, mode),
    )


def sorted_gallery_birds(birds: list[dict]) -> list[dict]:
    """Return a gallery bird list in the current sort order."""
    if not birds:
        return []
    annotated = annotate_gallery_birds_with_life_lists(list(birds))
    indices = sort_gallery_visible_indices(annotated, list(range(len(annotated))))
    return [annotated[i] for i in indices]


def gallery_nav_buttons_visible() -> bool:
    """Whether ◀▶ / ←→ bird and photo buttons are shown."""
    if "gallery_show_nav_buttons" not in st.session_state:
        return True
    return bool(st.session_state.gallery_show_nav_buttons)


def gallery_list_image_index(bird_index: int) -> int:
    indices = st.session_state.setdefault("gallery_list_image_indices", {})
    return int(indices.get(str(bird_index), 0))


def set_gallery_list_image_index(bird_index: int, image_index: int) -> None:
    indices = st.session_state.setdefault("gallery_list_image_indices", {})
    indices[str(bird_index)] = int(image_index)


def open_gallery_standard_for_bird(bird_index: int) -> None:
    """Switch into standard view for a specific bird."""
    st.session_state.gallery_bird_index = bird_index
    st.session_state.gallery_image_index = gallery_list_image_index(bird_index)
    # Widget keys can't be written after instantiation; apply on the next run.
    st.session_state.gallery_view_mode = "standard"
    st.session_state.gallery_view_mode_pending = "standard"
    st.session_state.gallery_show_info = False
    st.session_state.gallery_show_view_picker = False
    if "gallery_view_mode_radio" in st.session_state:
        st.session_state.gallery_view_mode_radio = "standard"
    st.rerun()


def consume_gallery_open_query() -> int | None:
    """Read and strip ``?gallery_open=<index>`` used by summary-view thumbnails."""
    params = getattr(st, "query_params", None)
    if params is None:
        return None
    try:
        if "gallery_open" not in params:
            return None
        raw = params.get("gallery_open")
    except Exception:
        return None
    try:
        del params["gallery_open"]
    except Exception:
        try:
            params.pop("gallery_open")
        except Exception:
            pass
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def apply_gallery_swipe(action: str, *, bird_count: int, image_count: int) -> bool:
    """Apply a swipe action to gallery session state. Returns True if state changed."""
    bird_index = int(st.session_state.get("gallery_bird_index", 0))
    image_index = int(st.session_state.get("gallery_image_index", 0))
    visible = st.session_state.get("gallery_visible_indices")

    if action == "image_next" and image_index < image_count - 1:
        st.session_state.gallery_image_index = image_index + 1
        return True
    if action == "image_prev" and image_index > 0:
        st.session_state.gallery_image_index = image_index - 1
        return True
    if action == "bird_next":
        if visible:
            try:
                pos = visible.index(bird_index)
            except ValueError:
                return False
            if pos >= len(visible) - 1:
                return False
            st.session_state.gallery_bird_index = visible[pos + 1]
        elif bird_index >= bird_count - 1:
            return False
        else:
            st.session_state.gallery_bird_index = bird_index + 1
        st.session_state.gallery_image_index = 0
        st.session_state.gallery_show_info = False
        return True
    if action == "bird_prev":
        if visible:
            try:
                pos = visible.index(bird_index)
            except ValueError:
                return False
            if pos <= 0:
                return False
            st.session_state.gallery_bird_index = visible[pos - 1]
        elif bird_index <= 0:
            return False
        else:
            st.session_state.gallery_bird_index = bird_index - 1
        st.session_state.gallery_image_index = 0
        st.session_state.gallery_show_info = False
        return True
    return False


def apply_compare_swipe(action: str, *, bird_count: int, image_count: int) -> bool:
    """Apply a swipe on the compare image. Returns True if state changed."""
    bird_index = int(st.session_state.get("gallery_compare_bird_index", 0))
    image_index = int(st.session_state.get("gallery_compare_image_index", 0))

    if action == "image_next" and image_index < image_count - 1:
        st.session_state.gallery_compare_image_index = image_index + 1
        return True
    if action == "image_prev" and image_index > 0:
        st.session_state.gallery_compare_image_index = image_index - 1
        return True
    if action == "bird_next":
        if bird_index >= bird_count - 1:
            return False
        st.session_state.gallery_compare_bird_index = bird_index + 1
        st.session_state.gallery_compare_image_index = 0
        return True
    if action == "bird_prev":
        if bird_index <= 0:
            return False
        st.session_state.gallery_compare_bird_index = bird_index - 1
        st.session_state.gallery_compare_image_index = 0
        return True
    return False


FRAME_COLOR_WORLD = "#0d9488"
FRAME_COLOR_REGION = "#d97706"
FRAME_COLOR_SEEN = "#94a3b8"


def gallery_bird_is_new_region(bird: dict) -> bool:
    if "is_new_region" in bird:
        return bool(bird.get("is_new_region"))
    return bool(bird.get("is_new") or bird.get("New") or bird.get("New_region"))


def gallery_bird_is_new_world(bird: dict) -> bool:
    if "is_new_world" in bird:
        return bool(bird.get("is_new_world"))
    return bool(bird.get("New_world"))


def gallery_bird_matches_scope(bird: dict, scope: str) -> bool:
    if scope == "world":
        return gallery_bird_is_new_world(bird)
    if scope == "region":
        return gallery_bird_is_new_region(bird)
    return True


def gallery_frame_color(bird: dict) -> str:
    """Border color for the gallery image based on life-list novelty."""
    if gallery_bird_is_new_world(bird):
        return FRAME_COLOR_WORLD
    if gallery_bird_is_new_region(bird):
        return FRAME_COLOR_REGION
    return FRAME_COLOR_SEEN


def annotate_gallery_birds_with_life_lists(birds: list[dict]) -> list[dict]:
    """Fill missing region/world-new flags from the current life lists."""
    region_code = st.session_state.get("checklists_region") or os.environ.get(
        "EBIRD_HOME_REGION", "US-FL-099"
    )
    region_life = load_life_list(region_code) if region_code else None
    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    annotated: list[dict] = []
    for bird in birds:
        item = dict(bird)
        taxon = {
            "comName": item.get("name") or "",
            "sciName": item.get("sciName") or "",
            "category": "species",
        }
        if "is_new_region" not in item:
            flag = is_new_to_life_list(taxon, region_life)
            item["is_new_region"] = bool(flag) if flag is not None else False
        if "is_new_world" not in item:
            flag = is_new_to_life_list(taxon, world_life)
            item["is_new_world"] = bool(flag) if flag is not None else False
        item["is_new"] = bool(item.get("is_new_region"))
        annotated.append(item)
    return annotated


def gallery_bird_key(bird: dict) -> str:
    """Return a stable identity for a gallery bird."""
    code = str(bird.get("code") or "").strip()
    if code:
        return f"code:{code}"
    sci_name = str(bird.get("sciName") or "").strip()
    if sci_name:
        return f"sci:{binomial_sci_name(sci_name)}"
    return f"name:{normalize_common_name(str(bird.get('name') or ''))}"


def is_in_compare_list(bird: dict) -> bool:
    """Whether a bird is already on the compare list."""
    key = gallery_bird_key(bird)
    return any(
        gallery_bird_key(item) == key
        for item in st.session_state.get("gallery_compare_birds") or []
    )


def similar_item_to_bird(item: dict) -> dict:
    """Map an iNaturalist similar-species row into gallery bird shape."""
    name = item.get("common_name") or item.get("scientific_name") or "Unknown"
    sci_name = item.get("scientific_name") or ""
    bird = {
        "code": str(item.get("ebird_code") or "").strip(),
        "name": str(name).strip(),
        "sciName": str(sci_name).strip(),
    }
    if "is_new_region" in item:
        bird["is_new_region"] = bool(item.get("is_new_region"))
        bird["is_new"] = bird["is_new_region"]
    if "is_new_world" in item:
        bird["is_new_world"] = bool(item.get("is_new_world"))
    return bird


def summarize_similar_species_counts(similar: list[dict]) -> dict[str, int]:
    """Aggregate similar-species coverage for checklist / region / life lists."""
    counts = {
        "total": len(similar),
        "in_checklists": 0,
        "in_region": 0,
        "on_region_life": 0,
        "on_world_life": 0,
        "new_region": 0,
        "new_world": 0,
    }
    for item in similar:
        history = item.get("region_history") or {}
        if history.get("in_local_checklist"):
            counts["in_checklists"] += 1
        if history.get("ever_seen"):
            counts["in_region"] += 1
        if item.get("is_new_region"):
            counts["new_region"] += 1
        else:
            counts["on_region_life"] += 1
        if item.get("is_new_world"):
            counts["new_world"] += 1
        else:
            counts["on_world_life"] += 1
    return counts


def format_region_last_seen_summary(info: dict, region_code: str) -> str:
    """Human-readable regional last-seen line for similar-species cards."""
    if not info.get("ever_seen"):
        return f"Never recorded in {region_code}"
    observation = info.get("observation") or {}
    obs_dt = str(observation.get("obsDt") or "").strip()
    loc_name = str(observation.get("locName") or "").strip()
    how_many = observation.get("howMany")
    source = str(observation.get("source") or "").strip()
    if not obs_dt:
        if info.get("local_miss"):
            return (
                f"Recorded in {region_code} · not in local checklist cache"
            )
        return f"Recorded in {region_code} · not seen in last 30 days"
    bits = [f"Last seen {obs_dt}"]
    if loc_name:
        bits.append(f"at {loc_name}")
    if how_many not in (None, ""):
        bits.append(f"· {how_many}")
    if source == "local_checklist":
        bits.append("(local cache)")
    return " ".join(bits)


def enrich_similar_with_region_history(
    similar: list[dict],
    region_code: str,
) -> list[dict]:
    """Attach regional last-seen + life-list novelty; sort never-seen to the end."""
    region = (region_code or "").strip()
    if not similar:
        return []
    if not region:
        return [dict(item) for item in similar]

    client = EBirdClient()
    local_index = build_local_last_seen_index(region)
    # Prefer disk-cached species list; fall back to local checklist species.
    try:
        region_codes = client.cached_region_species_codes(region)
    except MissingEbirdApiKey:
        ensure_api_key()
        client = None
        region_codes = set(load_disk_region_species_codes(region) or [])
    except requests.RequestException:
        region_codes = set(local_index)
    region_codes = set(region_codes) | set(local_index)
    region_life = load_life_list(region) if region else None
    world_life = load_life_list(WORLD_LIFE_LIST_CODE)

    enriched: list[dict] = []
    for index, item in enumerate(similar):
        row = dict(item)
        sci_name = str(row.get("scientific_name") or "").strip()
        common_name = str(row.get("common_name") or "").strip()
        code = resolve_ebird_code(
            scientific_name=sci_name or None,
            common_name=common_name or None,
        )
        if code:
            row["ebird_code"] = code
        ever_seen = bool(code and code in region_codes)
        observation = None
        local_miss = False
        if code and code in local_index:
            observation = dict(local_index[code])
        elif ever_seen and code and client is not None:
            # Historical region bird, but not in downloaded checklists.
            # Use local last-seen / prior disk cache only — do not hit live eBird.
            try:
                observation = client.last_seen_in_region(
                    region,
                    code,
                    back=30,
                    allow_api=False,
                )
            except MissingEbirdApiKey:
                ensure_api_key()
                observation = None
            except requests.RequestException:
                observation = None
            local_miss = observation is None or (
                observation.get("source") != "local_checklist"
                and not observation.get("obsDt")
            )
            if observation is None:
                local_miss = True

        taxon = {
            "comName": common_name,
            "sciName": sci_name,
            "category": "species",
        }
        region_flag = is_new_to_life_list(taxon, region_life)
        world_flag = is_new_to_life_list(taxon, world_life)
        row["is_new_region"] = bool(region_flag) if region_flag is not None else False
        row["is_new_world"] = bool(world_flag) if world_flag is not None else False
        row["is_new"] = row["is_new_region"]
        if row["is_new_world"]:
            novelty_label = "new to world"
            frame_color = FRAME_COLOR_WORLD
        elif row["is_new_region"]:
            novelty_label = "new to region"
            frame_color = FRAME_COLOR_REGION
        else:
            novelty_label = "already counted"
            frame_color = FRAME_COLOR_SEEN
        row["novelty_label"] = novelty_label
        row["frame_color"] = frame_color

        row["region_history"] = {
            "ever_seen": ever_seen,
            "in_local_checklist": bool(
                (code and code in local_index)
                or (
                    observation
                    and observation.get("source") == "local_checklist"
                    and observation.get("obsDt")
                )
            ),
            "observation": observation,
            "local_miss": local_miss and ever_seen and not (
                observation and observation.get("obsDt")
            ),
            "summary": format_region_last_seen_summary(
                {
                    "ever_seen": ever_seen,
                    "observation": observation,
                    "local_miss": local_miss and ever_seen and not (
                        observation and observation.get("obsDt")
                    ),
                },
                region,
            ),
        }
        row["_sort_index"] = index
        enriched.append(row)

    recent: list[dict] = []
    recorded: list[dict] = []
    never_seen: list[dict] = []
    for item in enriched:
        history = item.get("region_history") or {}
        if history.get("ever_seen") and (history.get("observation") or {}).get("obsDt"):
            recent.append(item)
        elif history.get("ever_seen"):
            recorded.append(item)
        else:
            never_seen.append(item)
    recent.sort(
        key=lambda item: str(
            ((item.get("region_history") or {}).get("observation") or {}).get("obsDt")
            or ""
        ),
        reverse=True,
    )
    ordered = recent + recorded + never_seen
    for item in ordered:
        item.pop("_sort_index", None)
    return ordered


def add_compare_bird(bird: dict) -> bool:
    """Add a gallery bird to the session-scoped comparison list.

    Returns True when the bird was newly added.
    """
    compare_birds = list(st.session_state.get("gallery_compare_birds") or [])
    key = gallery_bird_key(bird)
    if any(gallery_bird_key(item) == key for item in compare_birds):
        return False
    compare_birds.append(
        {
            "code": str(bird.get("code") or "").strip(),
            "name": str(bird.get("name") or "").strip(),
            "sciName": str(bird.get("sciName") or "").strip(),
            "is_new_region": gallery_bird_is_new_region(bird),
            "is_new_world": gallery_bird_is_new_world(bird),
            "is_new": gallery_bird_is_new_region(bird),
        }
    )
    st.session_state.gallery_compare_birds = compare_birds
    st.session_state.gallery_compare_bird_index = len(compare_birds) - 1
    st.session_state.gallery_compare_image_index = 0
    return True


def add_compare_birds(birds: list[dict]) -> int:
    """Add many birds to the compare list. Returns how many were newly added."""
    added = 0
    for bird in birds:
        if add_compare_bird(bird):
            added += 1
    return added


def remove_compare_bird(bird: dict) -> None:
    """Remove a bird from the session-scoped comparison list."""
    key = gallery_bird_key(bird)
    compare_birds = [
        item
        for item in st.session_state.get("gallery_compare_birds") or []
        if gallery_bird_key(item) != key
    ]
    st.session_state.gallery_compare_birds = compare_birds
    if compare_birds:
        current = int(st.session_state.get("gallery_compare_bird_index", 0))
        st.session_state.gallery_compare_bird_index = min(
            max(0, current), len(compare_birds) - 1
        )
    else:
        st.session_state.pop("gallery_compare_bird_index", None)
        st.session_state.pop("gallery_compare_image_index", None)


def remove_gallery_bird(bird_index: int) -> None:
    """Remove one bird from the current gallery working list."""
    birds = list(st.session_state.get("gallery_birds") or [])
    if not (0 <= bird_index < len(birds)):
        return
    removed = birds.pop(bird_index)
    remove_compare_bird(removed)
    if not birds:
        close_gallery()
        return
    st.session_state.gallery_birds = birds
    current = int(st.session_state.get("gallery_bird_index", 0))
    if current == bird_index:
        st.session_state.gallery_bird_index = min(bird_index, len(birds) - 1)
        st.session_state.gallery_image_index = 0
        st.session_state.gallery_show_info = False
    elif current > bird_index:
        st.session_state.gallery_bird_index = current - 1
    indices = st.session_state.get("gallery_list_image_indices") or {}
    shifted: dict[str, int] = {}
    for key, value in indices.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index == bird_index:
            continue
        shifted[str(index - 1 if index > bird_index else index)] = int(value)
    st.session_state.gallery_list_image_indices = shifted
    if st.session_state.get("gallery_saved_id"):
        st.session_state.gallery_saved_dirty = True
    st.rerun()


def clear_compare_list() -> None:
    """Remove every bird from the compare list."""
    st.session_state.gallery_compare_birds = []
    st.session_state.pop("gallery_compare_bird_index", None)
    st.session_state.pop("gallery_compare_image_index", None)


def render_compare_gallery() -> None:
    """Render comparison birds beneath the main gallery bird."""
    compare_birds = st.session_state.get("gallery_compare_birds") or []
    if not compare_birds:
        return

    compare_birds = annotate_gallery_birds_with_life_lists(compare_birds)
    st.session_state.gallery_compare_birds = compare_birds

    compare_index = int(st.session_state.get("gallery_compare_bird_index", 0))
    compare_index = max(0, min(compare_index, len(compare_birds) - 1))
    compare_bird = compare_birds[compare_index]
    payload = gallery_payload_for_code(
        compare_bird.get("code") or "",
        compare_bird.get("sciName") or None,
    )
    photos = (payload or {}).get("photos") or []
    common = (
        (payload or {}).get("common_name")
        or compare_bird.get("name")
        or "Unknown"
    )

    st.subheader("Compare birds")
    show_nav = gallery_nav_buttons_visible()
    if show_nav:
        prev_col, name_col, next_col = st.columns(
            [1, 4, 1], vertical_alignment="center"
        )
        with prev_col:
            if st.button(
                "◀",
                disabled=compare_index == 0,
                use_container_width=True,
                key="compare_prev_bird",
                help="Previous comparison bird",
            ):
                st.session_state.gallery_compare_bird_index = compare_index - 1
                st.session_state.gallery_compare_image_index = 0
                st.rerun()
        with next_col:
            if st.button(
                "▶",
                disabled=compare_index >= len(compare_birds) - 1,
                use_container_width=True,
                key="compare_next_bird",
                help="Next comparison bird",
            ):
                st.session_state.gallery_compare_bird_index = compare_index + 1
                st.session_state.gallery_compare_image_index = 0
                st.rerun()
    else:
        name_col = st.container()
    with name_col:
        count_label = f"{compare_index + 1}/{len(compare_birds)}"
        st.markdown(
            f"<div style='text-align:center; font-weight:600; padding-top:0.35rem'>"
            f"{html.escape(common)} · {count_label}</div>",
            unsafe_allow_html=True,
        )

    if not photos:
        st.info("No iNaturalist photos found for this comparison bird.")
    else:
        image_index = int(st.session_state.get("gallery_compare_image_index", 0))
        image_index = max(0, min(image_index, len(photos) - 1))
        photo = photos[image_index]
        compare_frame = gallery_frame_color(compare_bird)
        swipe = swipe_image(
            photo["image_url"],
            height=420,
            frame_color=compare_frame,
            key=f"compare_swipe_{compare_index}_{image_index}_{gallery_bird_key(compare_bird)}",
        )
        if isinstance(swipe, dict):
            action = str(swipe.get("action") or "")
            swipe_t = swipe.get("t")
            if action and swipe_t != st.session_state.get("gallery_compare_last_swipe_t"):
                st.session_state.gallery_compare_last_swipe_t = swipe_t
                if apply_compare_swipe(
                    action,
                    bird_count=len(compare_birds),
                    image_count=len(photos),
                ):
                    st.rerun()

        show_nav = gallery_nav_buttons_visible()
        show_image_pos = bool(st.session_state.get("gallery_show_legends", True))
        image_pos_html = (
            f"<div style='text-align:center'>Image {image_index + 1}/{len(photos)}</div>"
        )
        if show_nav:
            if show_image_pos:
                prev_col, pos_col, next_col = st.columns([1, 2, 1])
            else:
                prev_col, next_col = st.columns(2)
                pos_col = None
            with prev_col:
                if st.button(
                    "←",
                    disabled=image_index == 0,
                    use_container_width=True,
                    key="compare_prev_image",
                    help="Previous comparison photo",
                ):
                    st.session_state.gallery_compare_image_index = image_index - 1
                    st.rerun()
            with next_col:
                if st.button(
                    "→",
                    disabled=image_index >= len(photos) - 1,
                    use_container_width=True,
                    key="compare_next_image",
                    help="Next comparison photo",
                ):
                    st.session_state.gallery_compare_image_index = image_index + 1
                    st.rerun()
            if pos_col is not None:
                with pos_col:
                    st.markdown(image_pos_html, unsafe_allow_html=True)
        elif show_image_pos:
            st.markdown(image_pos_html, unsafe_allow_html=True)

    clear_col, remove_col = st.columns([1, 2])
    with clear_col:
        if st.button(
            "Remove all",
            key="compare_remove_all",
            use_container_width=True,
            help="Clear the entire compare list",
        ):
            clear_compare_list()
            st.rerun()
    with remove_col:
        if st.button(
            "Remove from compare list",
            key=f"compare_remove_{gallery_bird_key(compare_bird)}",
            use_container_width=True,
        ):
            remove_compare_bird(compare_bird)
            st.rerun()


def render_gallery() -> None:
    birds = st.session_state.get("gallery_birds") or []
    if not birds:
        st.session_state.pop("gallery_birds", None)
        st.session_state.dashboard_pref = HOME_SCREEN
        render_saved_galleries()
        return

    birds = annotate_gallery_birds_with_life_lists(birds)
    st.session_state.gallery_birds = birds

    region_code = st.session_state.get("checklists_region") or os.environ.get(
        "EBIRD_HOME_REGION", "US-FL-099"
    )

    pending_mode = st.session_state.pop("gallery_view_mode_pending", None)
    if pending_mode in {"summary", "list", "standard"}:
        st.session_state.gallery_view_mode = pending_mode
        if "gallery_view_mode_radio" in st.session_state:
            st.session_state.gallery_view_mode_radio = pending_mode
    if "gallery_view_mode" not in st.session_state:
        st.session_state.gallery_view_mode = "summary"

    open_idx = consume_gallery_open_query()
    if open_idx is not None and 0 <= open_idx < len(birds):
        open_gallery_standard_for_bird(open_idx)

    if "gallery_show_legends" not in st.session_state:
        st.session_state.gallery_show_legends = True
    if "gallery_show_nav_buttons" not in st.session_state:
        st.session_state.gallery_show_nav_buttons = True

    saved_id = str(st.session_state.get("gallery_saved_id") or "").strip()
    menu_col, title_col = st.columns([1, 16], vertical_alignment="center")
    with menu_col:
        with st.popover(
            " ",
            icon=":material/menu:",
            help="Navigation, save, filter, view, and legends",
        ):
            render_app_nav_buttons(current="gallery", key_prefix="gallery_nav")
            st.divider()
            if st.button(
                "Save gallery",
                icon=":material/save:",
                help="Save this gallery and get a link",
                type="primary" if st.session_state.get("gallery_saved_dirty") else "secondary",
                use_container_width=True,
                key="gallery_save",
            ):
                if save_current_gallery():
                    st.rerun()
            if saved_id:
                confirm_key = "confirm_delete_open_gallery"
                if st.session_state.get(confirm_key) == saved_id:
                    if st.button(
                        "Confirm delete",
                        icon=":material/delete:",
                        help="Tap to permanently delete this saved gallery",
                        type="primary",
                        use_container_width=True,
                        key="gallery_delete_confirm",
                    ):
                        st.session_state.pop(confirm_key, None)
                        delete_saved_gallery(saved_id)
                        close_gallery()
                elif st.button(
                    "Delete saved gallery",
                    icon=":material/delete:",
                    help="Delete this saved gallery",
                    use_container_width=True,
                    key="gallery_delete",
                ):
                    st.session_state[confirm_key] = saved_id
                    st.rerun()
            st.divider()
            gallery_scope = current_life_list_scope()
            if "life_list_scope" not in st.session_state:
                st.session_state.life_list_scope = gallery_scope
            st.radio(
                "Filter new birds by",
                options=["all", "region", "world"],
                format_func=lambda value: {
                    "all": "All birds",
                    "region": f"New to region ({region_code or 'region'})",
                    "world": "New to world",
                }[value],
                key="life_list_scope",
                on_change=_sync_life_list_scope_pref,
                help="Same filter as the checklists screen. Highlights: teal = new to world, amber = new to region, gray = already on both lists.",
            )
            st.session_state.life_list_scope_pref = current_life_list_scope()
            st.divider()
            gallery_mode = current_gallery_view_mode()
            if "gallery_view_mode_radio" not in st.session_state:
                st.session_state.gallery_view_mode_radio = gallery_mode
            st.radio(
                "Gallery view",
                options=["summary", "list", "standard"],
                format_func=lambda value: {
                    "summary": "Summary",
                    "list": "List",
                    "standard": "Standard",
                }[value],
                key="gallery_view_mode_radio",
                help="Summary matches the checklist thumbnail grid. List shows swipeable photos. Standard is one bird at a time. Tap a summary photo for Standard view.",
            )
            st.session_state.gallery_view_mode = st.session_state.gallery_view_mode_radio
            st.divider()
            render_gallery_sort_controls()
            st.divider()
            st.checkbox(
                "Show legends",
                key="gallery_show_legends",
                help="Also shows the Image 1/200 counter and the × buttons that remove a bird from this gallery.",
            )
            st.checkbox(
                "Show bird and photo buttons",
                key="gallery_show_nav_buttons",
                help="◀ ▶ change birds and ← → change photos. When off, swipe the image instead.",
            )
    with title_col:
        if "gallery_name" not in st.session_state:
            st.session_state.gallery_name = str(
                st.session_state.get("gallery_title") or default_gallery_name()
            )
        st.text_input(
            "Gallery name",
            key="gallery_name",
            label_visibility="collapsed",
            placeholder="Name (defaults to date and time)",
            help="Defaults to the date and time. Edit to give this gallery a specific name.",
            on_change=_on_gallery_name_change,
        )
    st.session_state.gallery_title = (
        str(st.session_state.get("gallery_name") or "").strip()
        or default_gallery_name()
    )

    gallery_scope = current_life_list_scope()
    gallery_mode = current_gallery_view_mode()
    show_legends = bool(st.session_state.get("gallery_show_legends", True))

    if show_legends and gallery_mode != "summary":
        st.markdown(
            f"Frame colors · "
            f"<span style='color:{FRAME_COLOR_WORLD}'>■</span> new to world · "
            f"<span style='color:{FRAME_COLOR_REGION}'>■</span> new to region · "
            f"<span style='color:{FRAME_COLOR_SEEN}'>■</span> already counted",
            unsafe_allow_html=True,
        )

    visible_indices = sort_gallery_visible_indices(
        birds,
        [
            idx
            for idx, item in enumerate(birds)
            if gallery_bird_matches_scope(item, gallery_scope)
        ],
    )
    st.session_state.gallery_visible_indices = visible_indices
    if not visible_indices:
        st.info(
            "No birds match this filter in the current gallery."
            if gallery_scope != "all"
            else "No birds available for the gallery."
        )
        return

    if gallery_mode == "summary":
        render_gallery_summary(birds, visible_indices)
        return
    if gallery_mode == "list":
        render_gallery_list(birds, visible_indices, gallery_scope)
        return

    render_gallery_standard(birds, visible_indices, gallery_scope)


def render_gallery_summary(birds: list[dict], visible_indices: list[int]) -> None:
    """Thumbnail grid; tap a photo to open Standard view without leaving the session."""
    width = 144
    st.markdown(
        f"""
<style>
div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-gallery_summary_open_"]) {{
  display: grid !important;
  grid-template-columns: repeat(auto-fill, {width}px) !important;
  gap: 1px !important;
  justify-content: start !important;
  align-items: start !important;
  flex-wrap: unset !important;
}}
div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-gallery_summary_open_"]) > div {{
  width: {width}px !important;
  min-width: {width}px !important;
  max-width: {width}px !important;
  flex: none !important;
  padding: 0 !important;
  position: relative !important;
  height: {width}px !important;
  overflow: hidden !important;
}}
div[data-testid="column"]:has(div[class*="st-key-gallery_summary_open_"]) [data-testid="stVerticalBlock"],
div[data-testid="stColumn"]:has(div[class*="st-key-gallery_summary_open_"]) [data-testid="stVerticalBlock"] {{
  gap: 0 !important;
  height: {width}px !important;
  position: relative !important;
}}
div[class*="st-key-gallery_summary_open_"] {{
  position: absolute !important;
  inset: 0 !important;
  margin: 0 !important;
  height: {width}px !important;
  z-index: 2;
}}
div[class*="st-key-gallery_summary_open_"] button {{
  width: {width}px !important;
  height: {width}px !important;
  min-height: {width}px !important;
  opacity: 0 !important;
  cursor: pointer !important;
  border: 0 !important;
  padding: 0 !important;
}}
div[class*="st-key-gallery_summary_remove_"] {{
  position: absolute !important;
  top: 0 !important;
  right: 0 !important;
  margin: 0 !important;
  width: 32px !important;
  height: 32px !important;
  z-index: 4;
}}
div[class*="st-key-gallery_summary_remove_"] button {{
  width: 32px !important;
  height: 32px !important;
  min-height: 32px !important;
  padding: 0 !important;
  border: 0 !important;
  border-radius: 4px !important;
  background: rgba(15, 23, 42, 0.62) !important;
  color: #fff !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(len(visible_indices), gap="small")
    for col, bird_index in zip(cols, visible_indices):
        with col:
            bird = birds[bird_index]
            code = bird.get("code")
            sci = bird.get("sciName") or None
            name = str(bird.get("name") or code or "Open")
            photo = inaturalist_photo_for_code(str(code), sci) if code else None
            alt = html.escape(name or "species", quote=True)
            if photo and photo.get("image_url"):
                src = html.escape(str(photo["image_url"]), quote=True)
                inner = (
                    f'<img src="{src}" alt="{alt}" '
                    f'style="width:{width}px;height:{width}px;'
                    f'object-fit:cover;display:block;margin:0;padding:0;border:0"/>'
                )
            else:
                label = html.escape((name[:10] or "—"), quote=False)
                inner = (
                    f'<div style="width:{width}px;height:{width}px;'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'font-size:11px;color:#64748b;background:#f1f5f9;'
                    f'margin:0;padding:0">{label}</div>'
                )
            st.markdown(inner, unsafe_allow_html=True)
            if st.button(
                " ",
                key=f"gallery_summary_open_{bird_index}",
                help=name,
                type="tertiary",
                use_container_width=True,
            ):
                open_gallery_standard_for_bird(bird_index)
            if st.session_state.get("gallery_show_legends", True) and st.button(
                "×",
                key=f"gallery_summary_remove_{bird_index}",
                help=f"Remove {name} from this gallery",
                type="tertiary",
            ):
                remove_gallery_bird(bird_index)
    if st.session_state.get("gallery_show_legends", True):
        st.caption(f"{len(visible_indices)} species · tap a photo for Standard view · × removes")
    else:
        st.caption(f"{len(visible_indices)} species")


def render_gallery_list(
    birds: list[dict],
    visible_indices: list[int],
    gallery_scope: str,
) -> None:
    """Browse all matching birds with swipeable main photos."""
    if st.session_state.get("gallery_show_legends", True):
        st.caption(f"{len(visible_indices)} birds · tap a name for Standard view · × removes")
    visible_birds = [birds[idx] for idx in visible_indices]
    ensure_gallery_image_cache(visible_birds, max_photos=24)
    last_swipe = st.session_state.setdefault("gallery_list_last_swipe_t", {})

    for start in range(0, len(visible_indices), 2):
        cols = st.columns(2)
        for col, bird_index in zip(cols, visible_indices[start : start + 2]):
            with col:
                bird = birds[bird_index]
                frame_color = gallery_frame_color(bird)
                payload = gallery_payload_for_code(
                    bird.get("code") or "",
                    bird.get("sciName") or None,
                    max_photos=24,
                )
                photos = (payload or {}).get("photos") or []
                common = (
                    (payload or {}).get("common_name")
                    or bird.get("name")
                    or "Unknown"
                )
                label = common
                if gallery_bird_is_new_world(bird):
                    label = f"{label} · new to world"
                elif gallery_bird_is_new_region(bird):
                    label = f"{label} · new to region"

                show_remove = st.session_state.get("gallery_show_legends", True)
                if show_remove:
                    open_col, remove_col = st.columns(
                        [6, 1], vertical_alignment="center"
                    )
                else:
                    open_col = st.container()
                    remove_col = None
                with open_col:
                    if st.button(
                        label,
                        use_container_width=True,
                        key=f"gallery_list_open_{bird_index}",
                        help="Open Standard view for this bird",
                    ):
                        open_gallery_standard_for_bird(bird_index)
                if remove_col is not None:
                    with remove_col:
                        if st.button(
                            "×",
                            use_container_width=True,
                            key=f"gallery_list_remove_{bird_index}",
                            help=f"Remove {common} from this gallery",
                            type="tertiary",
                        ):
                            remove_gallery_bird(bird_index)

                if not photos:
                    st.info("No photos found.")
                    continue

                image_index = gallery_list_image_index(bird_index)
                image_index = max(0, min(image_index, len(photos) - 1))
                if image_index != gallery_list_image_index(bird_index):
                    set_gallery_list_image_index(bird_index, image_index)
                photo = photos[image_index]

                swipe = swipe_image(
                    photo["image_url"],
                    height=220,
                    image_only=True,
                    frame_color=frame_color,
                    key=f"gallery_list_swipe_{bird_index}_{image_index}_{gallery_scope}",
                )
                if isinstance(swipe, dict):
                    action = str(swipe.get("action") or "")
                    swipe_t = swipe.get("t")
                    swipe_key = str(bird_index)
                    if (
                        action in {"image_next", "image_prev"}
                        and swipe_t != last_swipe.get(swipe_key)
                    ):
                        last_swipe[swipe_key] = swipe_t
                        if action == "image_next" and image_index < len(photos) - 1:
                            set_gallery_list_image_index(bird_index, image_index + 1)
                            st.rerun()
                        elif action == "image_prev" and image_index > 0:
                            set_gallery_list_image_index(bird_index, image_index - 1)
                            st.rerun()

                if st.session_state.get("gallery_show_legends", True):
                    st.caption(f"Photo {image_index + 1} of {len(photos)}")


def render_gallery_standard(
    birds: list[dict],
    visible_indices: list[int],
    gallery_scope: str,
) -> None:
    """One-bird-at-a-time gallery with compare and similar species."""
    region_code = st.session_state.get("checklists_region") or os.environ.get(
        "EBIRD_HOME_REGION", "US-FL-099"
    )
    bird_index = int(st.session_state.get("gallery_bird_index", 0))
    if bird_index not in visible_indices:
        st.session_state.gallery_bird_index = visible_indices[0]
        st.session_state.gallery_image_index = 0
        st.rerun()
    visible_pos = visible_indices.index(bird_index)
    bird = birds[bird_index]
    frame_color = gallery_frame_color(bird)

    payload = gallery_payload_for_code(
        bird.get("code") or "",
        bird.get("sciName") or None,
    )
    photos = (payload or {}).get("photos") or []
    common = (payload or {}).get("common_name") or bird.get("name") or "Unknown"

    show_nav = gallery_nav_buttons_visible()
    if show_nav:
        nav_prev, nav_name, nav_next = st.columns(
            [1, 4, 1], vertical_alignment="center"
        )
        with nav_prev:
            if st.button(
                "◀",
                use_container_width=True,
                disabled=visible_pos == 0,
                key="gallery_prev_bird",
                help="Previous bird (↑)",
                shortcut="Up",
            ):
                st.session_state.gallery_bird_index = visible_indices[visible_pos - 1]
                st.session_state.gallery_image_index = 0
                st.session_state.gallery_show_info = False
                st.rerun()
        with nav_next:
            if st.button(
                "▶",
                use_container_width=True,
                disabled=visible_pos >= len(visible_indices) - 1,
                key="gallery_next_bird",
                help="Next bird (↓)",
                shortcut="Down",
            ):
                st.session_state.gallery_bird_index = visible_indices[visible_pos + 1]
                st.session_state.gallery_image_index = 0
                st.session_state.gallery_show_info = False
                st.rerun()
    else:
        nav_name = st.container()
    with nav_name:
        label = common
        if gallery_bird_is_new_world(bird):
            label = f"{label} · new to world"
        elif gallery_bird_is_new_region(bird):
            label = f"{label} · new to region"
        label = f"{label} · {visible_pos + 1}/{len(visible_indices)}"
        show_remove = st.session_state.get("gallery_show_legends", True)
        if show_remove:
            name_col, remove_col = st.columns(
                [6, 1], vertical_alignment="center"
            )
        else:
            name_col = st.container()
            remove_col = None
        with name_col:
            if st.button(
                label,
                use_container_width=True,
                key="gallery_open_info",
            ):
                st.session_state.gallery_show_info = not st.session_state.get(
                    "gallery_show_info", False
                )
                st.rerun()
        if remove_col is not None:
            with remove_col:
                if st.button(
                    "×",
                    use_container_width=True,
                    key=f"gallery_standard_remove_{bird_index}",
                    help="Remove this bird from the gallery",
                    type="tertiary",
                ):
                    remove_gallery_bird(bird_index)

    if not photos:
        st.info("No iNaturalist photos found for this species.")
    else:
        image_index = int(st.session_state.get("gallery_image_index", 0))
        image_index = max(0, min(image_index, len(photos) - 1))
        photo = photos[image_index]

        swipe = swipe_image(
            photo["image_url"],
            height=420,
            frame_color=frame_color,
            key=f"gallery_swipe_{bird_index}_{image_index}_{gallery_scope}",
        )
        if isinstance(swipe, dict):
            action = str(swipe.get("action") or "")
            swipe_t = swipe.get("t")
            if action and swipe_t != st.session_state.get("gallery_last_swipe_t"):
                st.session_state.gallery_last_swipe_t = swipe_t
                if apply_gallery_swipe(
                    action,
                    bird_count=len(birds),
                    image_count=len(photos),
                ):
                    st.rerun()

        show_nav = gallery_nav_buttons_visible()
        show_image_pos = bool(st.session_state.get("gallery_show_legends", True))
        image_pos_html = (
            f"<div style='text-align:center'>Image {image_index + 1}/{len(photos)}</div>"
        )
        if show_nav:
            if show_image_pos:
                img_prev, img_pos, img_next = st.columns(
                    [1, 2, 1], vertical_alignment="center"
                )
            else:
                img_prev, img_next = st.columns(2, vertical_alignment="center")
                img_pos = None
            with img_prev:
                if st.button(
                    "←",
                    use_container_width=True,
                    disabled=image_index == 0,
                    key="gallery_prev_image",
                    help="Previous photo (←)",
                    shortcut="Left",
                ):
                    st.session_state.gallery_image_index = image_index - 1
                    st.rerun()
            if img_pos is not None:
                with img_pos:
                    st.markdown(image_pos_html, unsafe_allow_html=True)
            with img_next:
                if st.button(
                    "→",
                    use_container_width=True,
                    disabled=image_index >= len(photos) - 1,
                    key="gallery_next_image",
                    help="Next photo (→)",
                    shortcut="Right",
                ):
                    st.session_state.gallery_image_index = image_index + 1
                    st.rerun()
        elif show_image_pos:
            st.markdown(image_pos_html, unsafe_allow_html=True)

    render_compare_gallery()

    if st.session_state.get("gallery_show_info"):
        st.subheader("About this bird")
        if payload and payload.get("description"):
            st.write(payload["description"])
        else:
            st.write("No description available yet.")

        links: list[str] = []
        if payload and payload.get("ebird_url"):
            links.append(f"[eBird]({payload['ebird_url']})")
        if payload and payload.get("taxon_url"):
            links.append(f"[iNaturalist]({payload['taxon_url']})")
        if payload and payload.get("wikipedia_url"):
            links.append(f"[Wikipedia]({payload['wikipedia_url']})")
        if links:
            st.markdown(" · ".join(links))

        st.markdown("**Data sources**")
        sources = (payload or {}).get("sources") or [
            "No source metadata available for this species."
        ]
        for source in sources:
            st.write(f"- {source}")

    show_similar = st.session_state.get("gallery_show_similar", True)
    if "gallery_show_similar" not in st.session_state:
        st.session_state.gallery_show_similar = True
        show_similar = True
    similar_label = "Hide similar birds" if show_similar else "Show similar birds"
    if st.button(similar_label, key=f"gallery_toggle_similar_{bird_index}"):
        st.session_state.gallery_show_similar = not show_similar
        st.rerun()

    if st.session_state.get("gallery_show_similar", True):
        title_col, compare_col = st.columns([3, 2], vertical_alignment="bottom")
        with title_col:
            st.subheader("Similar birds")
        with compare_col:
            if is_in_compare_list(bird):
                if st.button(
                    "Remove from compare list",
                    key=f"main_compare_remove_{gallery_bird_key(bird)}",
                    use_container_width=True,
                    help=f"Remove {common} from the compare list",
                ):
                    remove_compare_bird(bird)
                    st.rerun()
            elif st.button(
                "Add to compare list",
                key=f"main_compare_add_{gallery_bird_key(bird)}",
                use_container_width=True,
                help=f"Add {common} to the compare list",
            ):
                add_compare_bird(bird)
                st.rerun()
        if st.session_state.get("gallery_show_legends", True):
            st.caption(
                "Species often confused with this one on iNaturalist. "
                f"Regional last-seen uses eBird data for {region_code}."
            )
            st.markdown(
                f"Highlights · "
                f"<span style='color:{FRAME_COLOR_WORLD}'>■</span> new to world · "
                f"<span style='color:{FRAME_COLOR_REGION}'>■</span> new to region · "
                f"<span style='color:{FRAME_COLOR_SEEN}'>■</span> already counted",
                unsafe_allow_html=True,
            )
        if "gallery_hide_similar_never_seen" not in st.session_state:
            st.session_state.gallery_hide_similar_never_seen = True
        hide_never_seen = st.checkbox(
            f"Hide species never recorded in {region_code}",
            value=True,
            key="gallery_hide_similar_never_seen",
            help="When checked, similar species with no regional eBird records are omitted.",
        )
        taxon_id = (payload or {}).get("taxon_id")
        similar = similar_species_for_taxon(
            int(taxon_id) if taxon_id else None,
            bird.get("code") or None,
            (payload or {}).get("scientific_name") or bird.get("sciName") or None,
            limit=12,
        )
        if not similar:
            st.info("No similar species found.")
        else:
            with st.spinner("Loading regional last-seen info…"):
                similar = enrich_similar_with_region_history(similar, region_code)
                st.session_state.pop("ebird_rate_limit_active", None)
            render_ebird_rate_limit_notices()
            if hide_never_seen:
                similar = [
                    item
                    for item in similar
                    if (item.get("region_history") or {}).get("ever_seen")
                ]
            if not similar:
                st.info(
                    f"No similar species recorded in {region_code}."
                    if hide_never_seen
                    else "No similar species found."
                )
            else:
                similar_birds = [similar_item_to_bird(item) for item in similar]
                counts = summarize_similar_species_counts(similar)
                st.markdown(
                    f"**{counts['total']}** similar · "
                    f"**{counts['in_checklists']}** in local checklists · "
                    f"**{counts['in_region']}** in region · "
                    f"region life list **{counts['on_region_life']}** "
                    f"(**{counts['new_region']}** new) · "
                    f"world life list **{counts['on_world_life']}** "
                    f"(**{counts['new_world']}** new)"
                )
                if st.button(
                    "Add all to compare list",
                    key=f"similar_add_all_{bird_index}",
                ):
                    added = add_compare_birds(similar_birds)
                    if added:
                        st.rerun()
                    else:
                        st.info("All similar birds are already on the compare list.")
                for start in range(0, len(similar), 3):
                    cols = st.columns(3)
                    for col, item in zip(cols, similar[start : start + 3]):
                        with col:
                            frame_color = (
                                item.get("frame_color")
                                or gallery_frame_color(similar_item_to_bird(item))
                            )
                            image_url = str(item.get("image_url") or "").strip()
                            if image_url:
                                src = html.escape(image_url, quote=True)
                                st.markdown(
                                    f"<div style='border:4px solid {frame_color};"
                                    f"border-radius:10px;padding:4px;"
                                    f"box-sizing:border-box;line-height:0'>"
                                    f"<img src='{src}' alt='' "
                                    f"style='width:100%;display:block;"
                                    f"border-radius:6px;margin:0'/></div>",
                                    unsafe_allow_html=True,
                                )
                            similar_bird = similar_item_to_bird(item)
                            name = similar_bird["name"]
                            novelty = item.get("novelty_label") or ""
                            title = f"**{name}**"
                            if novelty:
                                title = (
                                    f"{title} · "
                                    f"<span style='color:{frame_color}'>{novelty}</span>"
                                )
                            st.markdown(title, unsafe_allow_html=True)
                            history = item.get("region_history") or {}
                            summary = history.get("summary") or ""
                            if summary:
                                st.caption(summary)
                            taxon_key = (
                                item.get("taxon_id") or gallery_bird_key(similar_bird)
                            )
                            if is_in_compare_list(similar_bird):
                                if st.button(
                                    "Remove from compare",
                                    key=f"similar_compare_remove_{bird_index}_{taxon_key}",
                                    use_container_width=True,
                                ):
                                    remove_compare_bird(similar_bird)
                                    st.rerun()
                            elif st.button(
                                "Add to compare list",
                                key=f"similar_compare_add_{bird_index}_{taxon_key}",
                                use_container_width=True,
                            ):
                                add_compare_bird(similar_bird)
                                st.rerun()

def life_list_path(region_code: str) -> Path:
    return LIFE_LISTS_DIR / f"ebird_{region_code}_life_list.csv"


def normalize_common_name(name: str) -> str:
    """Strip subspecies/group parentheticals for life-list matching."""
    base = name.split(" (", 1)[0].strip()
    return base.casefold()


def binomial_sci_name(sci_name: str) -> str:
    """Use genus + species for matching subspecies scientific names."""
    parts = sci_name.replace(",", " ").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}".casefold()
    return sci_name.strip().casefold()


def load_life_list(region_code: str) -> dict[str, set[str]] | None:
    """Load life-list match keys, or None if the file is missing.

    Returns dict with:
      - common: normalized common names
      - sci: binomial scientific names
    """
    birds = load_life_list_birds(region_code)
    if birds is None:
        return None
    common = {normalize_common_name(bird["name"]) for bird in birds if bird.get("name")}
    sci = {
        binomial_sci_name(bird["sciName"])
        for bird in birds
        if bird.get("sciName")
    }
    return {"common": common, "sci": sci}


def load_life_list_birds(region_code: str) -> list[dict] | None:
    """Load ordered life-list species rows for gallery browsing."""
    path = life_list_path(region_code)
    if not path.exists():
        return None

    birds: list[dict] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            category = (row.get("Category") or "").strip().casefold()
            if category and category != "species":
                continue
            countable = (row.get("Countable") or "1").strip()
            if countable == "0":
                continue
            name = (row.get("Common Name") or "").strip()
            sci_name = (row.get("Scientific Name") or "").strip()
            if not name and not sci_name:
                continue
            display = name.split(" (", 1)[0].strip() or name or sci_name
            key = normalize_common_name(display) if name else binomial_sci_name(sci_name)
            if key in seen:
                continue
            seen.add(key)
            birds.append(
                {
                    "name": display,
                    "sciName": sci_name,
                    "code": "",
                }
            )
    return birds


def life_list_total(life: dict[str, set[str]] | None) -> int | None:
    """Species count for a loaded life list."""
    if life is None:
        return None
    return len(life["common"])


def open_life_list_gallery(region_code: str, *, title: str) -> None:
    """Open the gallery for every species on a saved life list."""
    birds = load_life_list_birds(region_code)
    if not birds:
        st.warning(f"No species found for life list `{region_code}`.")
        return
    open_gallery(birds, title=title)


def load_region_species_gallery_birds(region_code: str) -> list[dict]:
    """Resolve the full eBird regional species list into gallery bird rows.

    ``/product/spplist/{region}`` returns only species codes. Taxonomy batches
    supply common/scientific names; non-species categories are skipped.
    """
    code = (region_code or "").strip()
    if not code:
        return []
    client = EBirdClient()
    species_codes = client.region_species_codes(code)
    if not species_codes:
        return []
    taxa = client.species_taxa(species_codes)
    birds: list[dict] = []
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
    return birds


def open_region_species_gallery(region_code: str) -> None:
    """Load every species ever recorded in a region and open the gallery."""
    code = (region_code or "").strip()
    if not code:
        st.warning("Enter a region code.")
        return
    with st.spinner(f"Loading full species list for {code}…"):
        try:
            birds = load_region_species_gallery_birds(code)
        except MissingEbirdApiKey:
            ensure_api_key()
            return
        except requests.HTTPError as exc:
            st.error(
                f"eBird API error: {exc.response.status_code if exc.response else exc}"
            )
            return
        except Exception as exc:
            st.error(str(exc))
            return
    if not birds:
        st.warning(f"No species found for region `{code}`.")
        return
    open_gallery(birds, title=f"Region species gallery · {code}")


def load_life_list_names(region_code: str) -> set[str] | None:
    """Backward-compatible helper returning normalized common names only."""
    life = load_life_list(region_code)
    return None if life is None else life["common"]


def is_new_to_life_list(taxon: dict, life: dict[str, set[str]] | None) -> bool | None:
    """Return whether a checklist taxon is new, or None if not comparable."""
    if life is None:
        return None

    category = (taxon.get("category") or "species").strip().casefold()
    # Spuhs/slashes/hybrids aren't species life-list ticks.
    if category in {"spuh", "slash", "hybrid", "intergrade"}:
        return False

    common = (taxon.get("comName") or "").strip()
    sci = (taxon.get("sciName") or "").strip()
    if normalize_common_name(common) in life["common"]:
        return False
    if sci and binomial_sci_name(sci) in life["sci"]:
        return False
    return True


def obs_is_new_for_scope(obs: dict, scope: str) -> bool:
    """Whether an observation is new for the selected life-list scope."""
    if scope == "world":
        return bool(obs.get("is_new_world"))
    if scope == "region":
        if "is_new_region" in obs:
            return bool(obs.get("is_new_region"))
        return bool(obs.get("is_new"))
    return False


def summary_is_new_for_scope(item: dict, scope: str) -> bool:
    if scope == "world":
        return bool(item.get("New_world"))
    if scope == "region":
        return bool(item.get("New_region") or item.get("New"))
    return False


def new_bird_marker(is_new_region: bool, is_new_world: bool, *, scope: str) -> str:
    """Build a short marker for region/world new status."""
    if scope == "region":
        return " · **new to region**" if is_new_region else ""
    if scope == "world":
        return " · **new to world**" if is_new_world else ""
    bits: list[str] = []
    if is_new_world:
        bits.append("new to world")
    elif is_new_region:
        bits.append("new to region")
    if not bits:
        return ""
    return " · **" + ", ".join(bits) + "**"


def parse_obs_count(value: object) -> int | None:
    if value in ("", None):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_species_summary(rows: list[dict]) -> list[dict]:
    """Aggregate loaded checklists into per-species max count and frequency."""
    summary: dict[str, dict] = {}
    for row in rows:
        sub_id = str(row.get("subId") or row.get("subID") or "")
        seen_keys: set[str] = set()
        for obs in row.get("species_rows") or []:
            raw_name = str(obs.get("name") or obs.get("code") or "").strip()
            if not raw_name:
                continue
            key = normalize_common_name(raw_name)
            display = raw_name.split(" (", 1)[0].strip() or raw_name
            entry = summary.setdefault(
                key,
                {
                    "Species": display,
                    "Max count": None,
                    "Checklists": 0,
                    "New_region": False,
                    "New_world": False,
                    "code": obs.get("code") or "",
                    "sciName": obs.get("sciName") or "",
                    "_checklist_ids": set(),
                },
            )
            if len(display) < len(str(entry["Species"])):
                entry["Species"] = display
            if not entry.get("code") and obs.get("code"):
                entry["code"] = obs.get("code")
            if not entry.get("sciName") and obs.get("sciName"):
                entry["sciName"] = obs.get("sciName")
            count = parse_obs_count(obs.get("count"))
            if count is not None:
                current = entry["Max count"]
                entry["Max count"] = count if current is None else max(current, count)
            if key not in seen_keys and sub_id:
                entry["_checklist_ids"].add(sub_id)
                seen_keys.add(key)
            if obs.get("is_new_region") or (
                "is_new_region" not in obs and obs.get("is_new")
            ):
                entry["New_region"] = True
            if obs.get("is_new_world"):
                entry["New_world"] = True

    results: list[dict] = []
    for entry in summary.values():
        results.append(
            {
                "Species": entry["Species"],
                "Max count": entry["Max count"] if entry["Max count"] is not None else "—",
                "Checklists": len(entry["_checklist_ids"]),
                "New_region": entry["New_region"],
                "New_world": entry["New_world"],
                # Backward-compatible alias used by gallery open_gallery.
                "New": entry["New_region"],
                "code": entry.get("code") or "",
                "sciName": entry.get("sciName") or "",
            }
        )
    results.sort(key=lambda item: (-int(item["Checklists"]), str(item["Species"])))
    return results


def hotspot_label(hotspot: dict) -> str:
    name = hotspot.get("locName") or hotspot.get("locId") or "Unknown hotspot"
    species = hotspot.get("numSpeciesAllTime")
    if species is not None:
        return f"{name} ({species} spp)"
    return str(name)


def _region_option_label(row: dict) -> str:
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    if name and name != code:
        return f"{name} · {code}"
    return code or name


def _filter_regions_or_all(
    rows: list[dict],
    query: str,
) -> list[dict]:
    """Filter by query; if nothing matches, keep the full list for browsing."""
    if not (query or "").strip():
        return list(rows)
    matches = filter_regions_by_query(rows, query)
    return matches if matches else list(rows)


def render_region_code_lookup(*, session_key: str = "checklists_region") -> None:
    """Country → state → county picker that writes an eBird region code."""
    with st.expander("Look up region code", expanded=False):
        st.caption(
            "Browse eBird regions by name, then apply the code to the field above. "
            "Pick a country and state first, then filter for a county name."
        )
        query = st.text_input(
            "Filter by name or code",
            key=f"{session_key}_region_lookup_query",
            placeholder="e.g. Palm Beach, Florida, US-FL",
        )
        client = EBirdClient()

        try:
            with st.spinner("Loading countries…"):
                countries = client.list_regions("country", "world")
        except MissingEbirdApiKey:
            ensure_api_key()
            return
        except requests.RequestException as exc:
            st.error(f"Could not load countries: {exc}")
            return

        country_rows = _filter_regions_or_all(countries, query)
        current = str(st.session_state.get(session_key) or "").strip()
        current_country = current.split("-", 1)[0] if current else ""
        if current_country and all(r.get("code") != current_country for r in country_rows):
            for row in countries:
                if row.get("code") == current_country:
                    country_rows = [row, *country_rows]
                    break

        if not country_rows:
            st.info("No countries available.")
            return

        country_codes = [row["code"] for row in country_rows]
        country_labels = {row["code"]: _region_option_label(row) for row in country_rows}
        default_country = (
            current_country
            if current_country in country_codes
            else ("US" if "US" in country_codes else country_codes[0])
        )
        country = st.selectbox(
            "Country",
            options=country_codes,
            index=country_codes.index(default_country),
            format_func=lambda code: country_labels.get(code, code),
            key=f"{session_key}_region_lookup_country",
        )

        selected_code = country
        selected_label = country_labels.get(country, country)

        try:
            with st.spinner(f"Loading states/provinces for {country}…"):
                states = client.list_regions("subnational1", country)
        except MissingEbirdApiKey:
            ensure_api_key()
            states = []
        except requests.RequestException as exc:
            st.error(f"Could not load subnational regions: {exc}")
            states = []

        state_rows = _filter_regions_or_all(states, query)
        current_state = ""
        parts = current.split("-")
        if len(parts) >= 2 and parts[0] == country:
            current_state = f"{parts[0]}-{parts[1]}"
        if current_state and all(r.get("code") != current_state for r in state_rows):
            for row in states:
                if row.get("code") == current_state:
                    state_rows = [row, *state_rows]
                    break

        if state_rows:
            state_codes = [row["code"] for row in state_rows]
            state_labels = {row["code"]: _region_option_label(row) for row in state_rows}
            state_options = ["(country only)", *state_codes]
            default_state = (
                current_state
                if current_state in state_codes
                else state_options[0]
            )
            state_choice = st.selectbox(
                "State / province",
                options=state_options,
                index=state_options.index(default_state),
                format_func=lambda code: (
                    code if code == "(country only)" else state_labels.get(code, code)
                ),
                key=f"{session_key}_region_lookup_state",
            )
            if state_choice != "(country only)":
                state = state_choice
                selected_code = state
                selected_label = state_labels.get(state, state)

                try:
                    with st.spinner(f"Loading counties for {state}…"):
                        counties = client.list_regions("subnational2", state)
                except MissingEbirdApiKey:
                    ensure_api_key()
                    counties = []
                except requests.RequestException as exc:
                    st.error(f"Could not load counties: {exc}")
                    counties = []

                # At county level, empty filter results should stay empty so the
                # user can tell the name did not match.
                county_rows = (
                    filter_regions_by_query(counties, query)
                    if query.strip()
                    else counties
                )
                if current and all(r.get("code") != current for r in county_rows):
                    for row in counties:
                        if row.get("code") == current:
                            county_rows = [row, *county_rows]
                            break
                if county_rows:
                    county_codes = [row["code"] for row in county_rows]
                    county_labels = {
                        row["code"]: _region_option_label(row) for row in county_rows
                    }
                    county_options = ["(state only)", *county_codes]
                    default_county = (
                        current if current in county_codes else county_options[0]
                    )
                    county_choice = st.selectbox(
                        "County / district",
                        options=county_options,
                        index=county_options.index(default_county),
                        format_func=lambda code: (
                            code
                            if code == "(state only)"
                            else county_labels.get(code, code)
                        ),
                        key=f"{session_key}_region_lookup_county",
                    )
                    if county_choice != "(state only)":
                        selected_code = county_choice
                        selected_label = county_labels.get(county_choice, county_choice)
                elif query.strip():
                    st.info("No counties match that filter in the selected state.")
                elif not counties:
                    st.caption("This state/province has no county-level regions.")
        elif not states:
            st.caption("This country has no state/province-level regions.")

        st.write(f"Selected: **{selected_label}**")
        if st.button(
            f"Use {selected_code}",
            key=f"{session_key}_region_lookup_apply",
            type="primary",
            use_container_width=True,
        ):
            # Only update the logical region here. The Region code text input
            # already exists this run, so its widget key is synced on rerun.
            st.session_state[session_key] = selected_code
            st.rerun()


def render_region_code_input(
    *,
    session_key: str = "checklists_region",
    help: str,
) -> str:
    """Region-code text field plus lookup, without mutating the widget after create."""
    default_region = os.environ.get("EBIRD_HOME_REGION", "US-FL-099")
    if session_key not in st.session_state:
        st.session_state[session_key] = default_region
    desired = str(st.session_state.get(session_key) or default_region).strip()
    if st.session_state.get("region_code_field") != desired:
        st.session_state.region_code_field = desired
    region_code = st.text_input(
        "Region code",
        key="region_code_field",
        help=help,
    ).strip()
    if region_code:
        st.session_state[session_key] = region_code
    render_region_code_lookup(session_key=session_key)
    return str(st.session_state.get(session_key) or "").strip()


def enrich_checklists(
    client: EBirdClient | None,
    rows: list[dict],
    region_life: dict[str, set[str]] | None,
    world_life: dict[str, set[str]] | None = None,
    *,
    allow_api: bool = True,
) -> list[dict]:
    """Attach species names and life-list-new counts to checklist summaries.

    When a row already includes ``_detail`` (local cache), that payload is used.
    Otherwise details are fetched via the eBird API when ``allow_api`` is true.
    """
    details: dict[str, dict] = {}
    codes: list[str] = []
    for row in rows:
        sub_id = str(row.get("subId") or row.get("subID") or "")
        if not sub_id:
            continue
        detail = row.get("_detail")
        if isinstance(detail, dict) and detail:
            details[sub_id] = detail
        elif allow_api and client is not None:
            detail = client.checklist(sub_id)
            details[sub_id] = detail
        else:
            details[sub_id] = {}
        for obs in details[sub_id].get("obs") or []:
            code = obs.get("speciesCode")
            if code:
                codes.append(str(code))

    if allow_api and client is not None and codes:
        taxa_by_code = client.species_taxa(codes)
    else:
        taxa_by_code = {}
    enriched: list[dict] = []
    for row in rows:
        sub_id = str(row.get("subId") or row.get("subID") or "")
        detail = details.get(sub_id, {})
        species_rows: list[dict] = []
        new_region_names: list[str] = []
        new_world_names: list[str] = []
        seen_codes: set[str] = set()
        for obs in detail.get("obs") or []:
            code = str(obs.get("speciesCode") or "")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            taxon = taxa_by_code.get(code, {})
            common = taxon.get("comName") or code
            sci_name = taxon.get("sciName") or ""
            count = (
                obs.get("howManyAtleast")
                or obs.get("howMany")
                or obs.get("howManyStr")
                or ""
            )
            taxon_for_match = taxon or {"comName": common, "sciName": sci_name}
            is_new_region = is_new_to_life_list(taxon_for_match, region_life)
            is_new_world = is_new_to_life_list(taxon_for_match, world_life)
            if is_new_region:
                new_region_names.append(common)
            if is_new_world:
                new_world_names.append(common)
            species_rows.append(
                {
                    "code": code,
                    "name": common,
                    "sciName": sci_name,
                    "count": count,
                    "is_new_region": bool(is_new_region),
                    "is_new_world": bool(is_new_world),
                    # Prefer region for legacy consumers / default gallery mark.
                    "is_new": bool(is_new_region),
                    "category": taxon.get("category") or "",
                }
            )
        cleaned_row = {
            key: value
            for key, value in row.items()
            if not str(key).startswith("_")
        }
        enriched.append(
            {
                **cleaned_row,
                "species_rows": species_rows,
                "new_count_region": (
                    len(new_region_names) if region_life is not None else None
                ),
                "new_count_world": (
                    len(new_world_names) if world_life is not None else None
                ),
                "new_count": (
                    len(new_region_names) if region_life is not None else None
                ),
                "new_names": new_region_names,
                "new_names_world": new_world_names,
            }
        )
    return enriched


def render_api_key_form() -> None:
    """Password field for entering an eBird API key for this session."""
    st.info(
        "An eBird API key is required for this request. "
        "Get a free key at [ebird.org/api/keygen](https://ebird.org/api/keygen), "
        "or open this app with `?EBIRD_API_KEY=your_key`."
    )
    entered = st.text_input(
        "eBird API key",
        type="password",
        key="ebird_api_key_input",
        help="Stored only for this browser session. You can also pass ?EBIRD_API_KEY=… in the URL.",
    )
    if st.button("Use API key", type="primary", key="save_ebird_api_key"):
        cleaned = (entered or "").strip()
        if not cleaned or cleaned == "your_ebird_api_key_here":
            st.warning("Paste a valid eBird API key to continue.")
            return
        st.session_state.ebird_api_key = cleaned
        st.session_state.pop("ebird_api_key_needed", None)
        st.rerun()


def ensure_api_key() -> bool:
    """Return True when a key is available; otherwise prompt and return False.

    The form is shown at the top of the app on the next run, so cached screens
    stay usable until an eBird HTTP call is actually needed.
    """
    if get_api_key():
        st.session_state.pop("ebird_api_key_needed", None)
        return True
    if not st.session_state.get("ebird_api_key_needed"):
        st.session_state.ebird_api_key_needed = True
        st.rerun()
    return False


def render_general_cache_maintenance() -> None:
    """Show the size and freshness of all non-checklist local caches."""
    render_page_header("Cache maintenance", screen="maintenance")
    commit_stamp = project_git_commit_stamp()
    if commit_stamp:
        st.caption(f"Last git commit: **{commit_stamp}**")
    if "ui_layout_mode_radio" not in st.session_state:
        st.session_state.ui_layout_mode_radio = st.session_state.get(
            "ui_layout_pref", "desktop"
        )
    st.radio(
        "Display width",
        options=["desktop", "mobile"],
        format_func=lambda value: {
            "desktop": "Desktop (full window)",
            "mobile": "Mobile (narrow)",
        }[value],
        horizontal=True,
        key="ui_layout_mode_radio",
        on_change=_sync_ui_layout_pref,
        help="Applies to every screen. Desktop uses the full browser width. "
        "Mobile constrains the layout to a phone-sized column.",
    )
    st.caption(
        "Local API and image caches. Checklist feeds/details are reported separately "
        "under Checklist cache. Region bird coverage is based on the selected "
        "region’s historical species list."
    )

    region_code = render_region_code_input(
        help="Uses the same region selection as Checklists / Checklist cache.",
    )

    coverage: dict = {}
    if region_code:
        coverage = region_historical_species_cache_coverage(region_code)
        if not coverage.get("has_historical_list"):
            st.info(
                f"No cached historical species list for `{region_code}` yet."
            )
            if st.button(
                "Load historical species list",
                key="load_region_species_list_banner",
                type="primary",
                use_container_width=True,
            ):
                if ensure_api_key():
                    try:
                        result = warm_missing_region_species_list(region_code)
                        found = int(result.get("found") or 0)
                        st.success(
                            f"Cached {found:,} historical species for `{region_code}`."
                        )
                    except MissingEbirdApiKey:
                        ensure_api_key()
                    except requests.HTTPError as exc:
                        st.error(
                            f"API error: "
                            f"{exc.response.status_code if exc.response else exc}"
                        )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
        else:
            st.caption(
                f"Historical species list for `{region_code}`: "
                f"**{int(coverage.get('historical_total') or 0):,}** birds."
            )

    rows = general_cache_inventory(region_code or None, coverage=coverage or None)
    total_bytes = sum(int(row["Bytes"]) for row in rows)
    metrics = st.columns(2)
    with metrics[0]:
        st.metric("Cache files", len(rows))
    with metrics[1]:
        st.metric("Total size", _format_bytes(total_bytes))

    if not rows:
        st.info("No non-checklist JSON caches found.")
        return

    header = st.columns([2.4, 0.8, 0.7, 1.0, 0.7, 1.0, 1.6, 1.0])
    header[0].markdown("**Cache**")
    header[1].markdown("**Size**")
    header[2].markdown("**Entries**")
    header[3].markdown("**Region birds**")
    header[4].markdown("**Region %**")
    header[5].markdown("**Modified**")
    header[6].markdown("**Load**")
    header[7].markdown("**Missing**")

    loaders = {
        "photo": warm_missing_region_photo_cache,
        "gallery": warm_missing_region_gallery_cache,
        "similar": warm_missing_region_similar_cache,
        "region_species": warm_missing_region_species_list,
    }
    show_key = str(st.session_state.get("cache_maintenance_show_missing") or "")
    for row in rows:
        cols = st.columns([2.4, 0.8, 0.7, 1.0, 0.7, 1.0, 1.6, 1.0])
        cols[0].write(row["Cache"])
        cols[1].write(row["Size"])
        cols[2].write(row["Entries"])
        cols[3].write(row["Region birds"])
        cols[4].write(row["Region %"])
        cols[5].write(row["Modified"])
        loader = row.get("loader")
        missing_kind = row.get("missing_kind")
        missing = row.get("missing")
        if loader and region_code:
            if loader == "region_species" and missing is None:
                missing = 0 if coverage.get("has_historical_list") else 1
            disabled = missing is None or int(missing) <= 0
            label = (
                "Up to date"
                if disabled
                else (
                    "Load list"
                    if loader == "region_species"
                    else f"Load ({int(missing):,})"
                )
            )
            if cols[6].button(
                label,
                key=f"load_cache_{loader}_{row['Cache']}",
                disabled=disabled,
                use_container_width=True,
            ):
                warmer = loaders.get(str(loader))
                if warmer is None:
                    st.error(f"No loader configured for {loader}.")
                elif ensure_api_key():
                    try:
                        result = warmer(region_code)
                    except MissingEbirdApiKey:
                        ensure_api_key()
                        continue
                    except requests.HTTPError as exc:
                        st.error(
                            f"API error: "
                            f"{exc.response.status_code if exc.response else exc}"
                        )
                        continue
                    except Exception as exc:
                        st.error(str(exc))
                        continue
                    attempted = int(result.get("attempted") or 0)
                    found = int(result.get("found") or 0)
                    if loader == "region_species":
                        st.success(
                            f"Cached {found:,} historical species for `{region_code}`."
                        )
                    elif attempted == 0:
                        st.info(f"`{row['Cache']}` already covers this region list.")
                    else:
                        st.success(
                            f"Loaded {attempted:,} "
                            f"entr{'y' if attempted == 1 else 'ies'} into "
                            f"`{row['Cache']}` ({found:,} with data)."
                        )
                    st.session_state.pop("cache_maintenance_show_missing", None)
                    st.rerun()
        else:
            cols[6].write("—")

        can_show_missing = bool(region_code and missing_kind and missing is not None)
        if can_show_missing:
            viewing = show_key == row["Cache"]
            show_label = "Hide" if viewing else f"Show ({int(missing):,})"
            if cols[7].button(
                show_label,
                key=f"show_missing_{missing_kind}_{row['Cache']}",
                disabled=int(missing or 0) <= 0 and not viewing,
                use_container_width=True,
            ):
                if viewing:
                    st.session_state.pop("cache_maintenance_show_missing", None)
                else:
                    st.session_state.cache_maintenance_show_missing = row["Cache"]
                st.rerun()
        else:
            cols[7].write("—")

    if region_code and show_key:
        selected = next((row for row in rows if row["Cache"] == show_key), None)
        if selected and selected.get("missing_kind"):
            kind = str(selected.get("missing_kind"))
            with st.spinner(f"Resolving missing species for `{show_key}`…"):
                missing_codes = missing_codes_for_cache_kind(region_code, kind)
                missing_rows = missing_species_display_rows(missing_codes)
            st.subheader(f"Missing from `{show_key}`")
            st.caption(
                f"{len(missing_rows):,} historical species for `{region_code}` "
                f"not yet represented in this cache."
            )
            if missing_rows:
                st.dataframe(missing_rows, use_container_width=True, hide_index=True)
            else:
                st.info("Nothing missing for this cache and region list.")

    render_similar_cache_media_maintenance()


def _format_coverage_pct(covered: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{covered / total:.1%}"


def render_similar_cache_media_maintenance() -> None:
    """Ensure similar-species *results* have photo and gallery caches."""
    st.subheader("Similar-species photo & gallery cache")
    st.caption(
        "Unique birds that appear in similar-species lists, including species that "
        "are not on this region’s historical list. Photo = `inaturalist_cache.json`. "
        "Gallery = current `inaturalist_gallery_cache.json`."
    )
    coverage = similar_cache_media_coverage()
    total = int(coverage.get("total") or 0)
    photo_covered = int(coverage.get("photo_covered") or 0)
    photo_missing = int(coverage.get("photo_missing") or 0)
    gallery_covered = int(coverage.get("gallery_covered") or 0)
    gallery_missing = int(coverage.get("gallery_missing") or 0)
    unresolved = int(coverage.get("unresolved") or 0)
    metrics = st.columns(4)
    with metrics[0]:
        st.metric("Similar birds", f"{total:,}")
    with metrics[1]:
        st.metric(
            "Photo cache",
            f"{photo_covered:,}/{total:,}",
            delta=_format_coverage_pct(photo_covered, total),
            delta_color="off",
        )
    with metrics[2]:
        st.metric(
            "Gallery cache",
            f"{gallery_covered:,}/{total:,}",
            delta=_format_coverage_pct(gallery_covered, total),
            delta_color="off",
        )
    with metrics[3]:
        st.metric("No eBird code", f"{unresolved:,}")

    show_kind = str(st.session_state.get("similar_media_show_missing") or "")
    photo_col, gallery_col = st.columns(2)
    with photo_col:
        photo_disabled = photo_missing <= 0
        photo_label = (
            "Photo cache up to date"
            if photo_disabled
            else f"Load missing photos ({photo_missing:,})"
        )
        if st.button(
            photo_label,
            key="load_similar_result_photos",
            disabled=photo_disabled,
            use_container_width=True,
        ):
            try:
                result = warm_missing_similar_result_photo_cache()
            except requests.RequestException as exc:
                st.error(str(exc))
            else:
                attempted = int(result.get("attempted") or 0)
                found = int(result.get("found") or 0)
                st.success(
                    f"Loaded {attempted:,} similar-bird photo "
                    f"entr{'y' if attempted == 1 else 'ies'} "
                    f"({found:,} with data)."
                )
                st.session_state.pop("similar_media_show_missing", None)
                st.rerun()
        viewing_photo = show_kind == "photo"
        show_photo_label = (
            "Hide missing photos"
            if viewing_photo
            else f"Show missing photos ({photo_missing:,})"
        )
        if st.button(
            show_photo_label,
            key="show_similar_result_photos",
            disabled=photo_missing <= 0 and not viewing_photo,
            use_container_width=True,
        ):
            if viewing_photo:
                st.session_state.pop("similar_media_show_missing", None)
            else:
                st.session_state.similar_media_show_missing = "photo"
            st.rerun()
    with gallery_col:
        gallery_disabled = gallery_missing <= 0
        gallery_label = (
            "Gallery cache up to date"
            if gallery_disabled
            else f"Load missing galleries ({gallery_missing:,})"
        )
        if st.button(
            gallery_label,
            key="load_similar_result_galleries",
            disabled=gallery_disabled,
            use_container_width=True,
        ):
            try:
                result = warm_missing_similar_result_gallery_cache()
            except requests.RequestException as exc:
                st.error(str(exc))
            else:
                attempted = int(result.get("attempted") or 0)
                found = int(result.get("found") or 0)
                st.success(
                    f"Loaded {attempted:,} similar-bird gallery "
                    f"entr{'y' if attempted == 1 else 'ies'} "
                    f"({found:,} with data)."
                )
                st.session_state.pop("similar_media_show_missing", None)
                st.rerun()
        viewing_gallery = show_kind == "gallery"
        show_gallery_label = (
            "Hide missing galleries"
            if viewing_gallery
            else f"Show missing galleries ({gallery_missing:,})"
        )
        if st.button(
            show_gallery_label,
            key="show_similar_result_galleries",
            disabled=gallery_missing <= 0 and not viewing_gallery,
            use_container_width=True,
        ):
            if viewing_gallery:
                st.session_state.pop("similar_media_show_missing", None)
            else:
                st.session_state.similar_media_show_missing = "gallery"
            st.rerun()

    if show_kind in {"photo", "gallery"}:
        label = "photo" if show_kind == "photo" else "gallery"
        rows = missing_similar_result_display_rows(show_kind)
        st.caption(
            f"{len(rows):,} similar-species birds missing a current {label} cache."
        )
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info(f"Nothing missing from the {label} cache.")


def render_checklist_download_maintenance(
    region_code: str,
    year: int,
    *,
    days: list[dict] | None = None,
    hotspots: list[dict] | None = None,
) -> None:
    """Render background downloader controls and rolling ETA from its progress file."""
    progress = load_download_progress(region_code, year)
    status = str(progress.get("status") or "idle")
    pid_running = _is_process_running(progress.get("pid"))
    active = status == "running" and pid_running
    if status == "running" and not pid_running:
        status = "interrupted"
    # Allow stop whenever the worker process is still alive (e.g. mid-429 sleep).
    can_stop = pid_running

    st.subheader("Download missing checklist details")
    st.caption(
        "Runs in a separate process. The worker stays below 60 calls/minute "
        "(37.5/min) and pauses for eBird’s Retry-After interval on HTTP 429."
    )

    if st.button(
        "Remove duplicate checklist files",
        key="dedupe_checklist_files",
        disabled=active,
        help="Keep one file per checklist id and delete extra copies on disk.",
    ):
        result = dedupe_downloaded_checklists(region_code)
        removed = int(result.get("removed_files") or 0)
        if removed:
            st.success(
                f"Removed {removed:,} duplicate file(s) "
                f"({int(result.get('duplicate_ids') or 0):,} checklist ids)."
            )
        else:
            st.info("No duplicate checklist files found.")
        st.rerun()

    total = int(progress.get("total_missing") or 0)
    processed = int(progress.get("processed") or 0)
    downloaded = int(progress.get("downloaded") or 0)
    failed = int(progress.get("failed") or 0)
    remaining = int(progress.get("remaining") or max(0, total - processed))
    http_429_count = int(
        progress.get("http_429_count")
        if progress.get("http_429_count") is not None
        else len(progress.get("rate_limit_events") or [])
    )
    retried_loads = int(progress.get("retried_loads") or 0)
    wait_count = int(progress.get("wait_count") or 0)
    wait_seconds_total = float(progress.get("wait_seconds_total") or 0.0)
    rate_limit_line = (
        f"**HTTP 429s:** {http_429_count:,} · "
        f"**Loads retried:** {retried_loads:,} · "
        f"**Waits:** {wait_count:,} · "
        f"**Wait time:** {_format_eta(wait_seconds_total)} "
        f"(`{_format_eta_compact(wait_seconds_total)}`)"
    )
    rate_limit_caption = (
        f"HTTP 429s: {http_429_count:,} · Loads retried: {retried_loads:,} · "
        f"Waits: {wait_count:,} · Wait time: {_format_eta_compact(wait_seconds_total)}"
    )
    has_rate_limit_stats = bool(
        http_429_count or retried_loads or wait_count or wait_seconds_total
    )
    durations = [
        float(value)
        for value in (progress.get("recent_durations_seconds") or [])
        if isinstance(value, (int, float)) and value >= 0
    ][-10:]
    average = sum(durations) / len(durations) if durations else None
    if average is None and isinstance(progress.get("seconds_per_item"), (int, float)):
        average = float(progress["seconds_per_item"])
    eta = None
    if isinstance(progress.get("eta_seconds"), (int, float)) and remaining:
        eta = float(progress["eta_seconds"])
    elif average is not None and remaining:
        eta = average * remaining

    started_at = _parse_progress_timestamp(progress.get("started_at"))
    finished_at = _parse_progress_timestamp(progress.get("finished_at"))
    updated_at = _parse_progress_timestamp(progress.get("updated_at"))
    now = datetime.now().astimezone()
    elapsed_seconds: float | None = None
    if started_at is not None:
        if finished_at is not None:
            elapsed_seconds = max(0.0, (finished_at - started_at).total_seconds())
        elif active:
            elapsed_seconds = max(0.0, (now - started_at).total_seconds())
        elif updated_at is not None:
            elapsed_seconds = max(0.0, (updated_at - started_at).total_seconds())
        else:
            elapsed_seconds = max(0.0, (now - started_at).total_seconds())
    finish_at: datetime | None = None
    if eta is not None and remaining > 0:
        finish_at = now + timedelta(seconds=eta)
    elif finished_at is not None:
        finish_at = finished_at

    scope_bits: list[str] = []
    if progress.get("day"):
        scope_bits.append(f"day {progress['day']}")
    if progress.get("loc_id"):
        scope_bits.append(f"hotspot {progress['loc_id']}")
    if progress.get("min_species"):
        scope_bits.append(f"≥{int(progress['min_species'])} species")
    scope_label = " · ".join(scope_bits) if scope_bits else "all missing in year"

    if total:
        st.progress(
            min(1.0, processed / total),
            text=f"{status.title()} · {processed:,}/{total:,} · {scope_label}",
        )
    else:
        st.info(
            "No worker progress yet. Start a download to calculate the current "
            "missing count from the regional daily-feed cache."
        )

    stats = st.columns(3)
    with stats[0]:
        st.metric("Downloaded this run", f"{downloaded:,}")
    with stats[1]:
        st.metric("Remaining", f"{remaining:,}" if total else "—")
    with stats[2]:
        st.metric("Failures", f"{failed:,}")

    if eta is not None and average is not None:
        timing_bits = [
            f"**Estimated time remaining:** {_format_eta(eta)}",
            f"`{_format_eta_compact(eta)}` · pace {average:.1f}s/item "
            f"(last {max(len(durations), 1)} loads)",
        ]
        if elapsed_seconds is not None:
            timing_bits.append(
                f"**Time spent so far:** {_format_eta(elapsed_seconds)} "
                f"(`{_format_eta_compact(elapsed_seconds)}`)"
            )
        if finish_at is not None and remaining > 0:
            timing_bits.append(
                f"**Should finish around:** {finish_at.strftime('%-I:%M:%S %p')} "
                f"({finish_at.strftime('%Y-%m-%d')})"
            )
        elif finished_at is not None:
            timing_bits.append(
                f"**Finished at:** {finished_at.strftime('%-I:%M:%S %p')} "
                f"({finished_at.strftime('%Y-%m-%d')})"
            )
        timing_bits.append(rate_limit_line)
        st.markdown("  \n".join(timing_bits))
    elif total and remaining and not durations:
        st.warning("ETC appears after the first checklist downloads complete.")
        if elapsed_seconds is not None:
            st.caption(
                f"Time spent so far: {_format_eta(elapsed_seconds)} "
                f"(`{_format_eta_compact(elapsed_seconds)}`)"
            )
        if has_rate_limit_stats:
            st.caption(rate_limit_caption)
    elif elapsed_seconds is not None and total:
        finish_line = ""
        if finished_at is not None:
            finish_line = (
                f" · finished {finished_at.strftime('%-I:%M:%S %p')} "
                f"({finished_at.strftime('%Y-%m-%d')})"
            )
        st.markdown(
            f"**Time spent:** {_format_eta(elapsed_seconds)} "
            f"(`{_format_eta_compact(elapsed_seconds)}`){finish_line}  \n"
            f"{rate_limit_line}"
        )
    elif total and has_rate_limit_stats:
        st.markdown(rate_limit_line)
    if active:
        st.caption(
            "Worker running. Click **Refresh status** above to update progress and ETC."
        )

    st.markdown("**Download scope**")
    scope = st.radio(
        "Scope",
        options=["all", "day", "hotspot"],
        format_func=lambda value: {
            "all": "All missing for year",
            "day": "One day only",
            "hotspot": "One hotspot only",
        }[value],
        horizontal=True,
        key="checklist_download_scope",
        disabled=active,
        label_visibility="collapsed",
    )

    min_species = int(
        st.number_input(
            "Minimum species",
            min_value=0,
            value=0,
            step=1,
            key="checklist_download_min_species",
            disabled=active,
            help=(
                "Only download missing checklists whose feed summary reports at least "
                "this many species (0 = no minimum)."
            ),
        )
    )

    selected_day: str | None = None
    selected_loc: str | None = None
    day_options = [
        str(row.get("day") or "")
        for row in (days or [])
        if str(row.get("day") or "")
        and int(row.get("expected") or 0) > int(row.get("downloaded") or 0)
    ]
    if not day_options:
        day_options = [str(row.get("day") or "") for row in (days or []) if row.get("day")]

    if scope == "day":
        if day_options:
            selected_day = st.selectbox(
                "Day",
                options=day_options,
                key="checklist_download_day",
                disabled=active,
                help="Download only missing checklist details for this observation day.",
            )
        else:
            st.warning("No days available in the feed cache status.")
    elif scope == "hotspot":
        hotspot_rows = [
            row
            for row in (hotspots or [])
            if str(row.get("locId") or "").strip()
        ]
        incomplete = [
            row
            for row in hotspot_rows
            if int(row.get("missing") or 0) > 0
        ]
        choices = incomplete or hotspot_rows
        if choices:
            labels = {
                str(row["locId"]): (
                    f"{row.get('locName') or row['locId']} · {row['locId']} · "
                    f"{int(row.get('missing') or 0):,} missing "
                    f"({int(row.get('downloaded') if row.get('downloaded') is not None else row.get('checklists') or 0):,}/"
                    f"{int(row.get('expected') or 0):,})"
                    if int(row.get("expected") or 0) or int(row.get("missing") or 0)
                    else (
                        f"{row.get('locName') or row['locId']} · {row['locId']} "
                        f"({int(row.get('checklists') or 0)} cached)"
                    )
                )
                for row in choices
            }
            options = [str(row["locId"]) for row in choices]
            selected_loc = st.selectbox(
                "Hotspot",
                options=options,
                format_func=lambda lid: labels.get(lid, lid),
                key="checklist_download_hotspot",
                disabled=active,
                help="Download only missing checklist details for this hotspot.",
            )
        else:
            selected_loc = st.text_input(
                "Hotspot locId",
                key="checklist_download_hotspot_text",
                disabled=active,
                help="eBird location id, e.g. L246929",
            ).strip() or None

    controls = st.columns(2)
    with controls[0]:
        start = st.button(
            "Resume / start download",
            type="primary",
            disabled=active
            or (scope == "day" and not selected_day)
            or (scope == "hotspot" and not selected_loc),
            use_container_width=True,
            help="Starts a separate, resumable worker for missing checklist details.",
        )
    with controls[1]:
        stop = st.button(
            "Stop now",
            disabled=not can_stop,
            use_container_width=True,
            help="Interrupt the download worker immediately.",
        )

    if stop:
        result = request_download_stop(region_code, year)
        if result.get("still_running"):
            st.error(
                f"Stop signaled for pid {result.get('pid')}, "
                "but the process is still running."
            )
        elif result.get("killed"):
            st.success("Download worker interrupted and stopped.")
        elif result.get("reason") == "not_running":
            st.info("Download worker was already stopped.")
        else:
            st.warning(
                f"Marked download stopped"
                + (
                    f" ({result.get('reason')})"
                    if result.get("reason")
                    else ""
                )
                + "."
            )
        time.sleep(0.15)
        st.rerun()

    if start:
        if not ensure_api_key():
            return
        error = _start_checklist_download(
            region_code,
            year,
            day=selected_day,
            loc_id=selected_loc,
            min_species=min_species,
        )
        if error:
            st.error(f"Could not start background downloader: {error}")
        else:
            label = (
                "all missing in year"
                if scope == "all"
                else (
                    f"day {selected_day}"
                    if selected_day
                    else f"hotspot {selected_loc}"
                )
            )
            if min_species > 0:
                label = f"{label} · ≥{min_species} species"
            st.success(
                f"Background downloader started ({label}). "
                "Refresh status to update progress."
            )
            time.sleep(0.25)
            st.rerun()


def render_cache_status() -> None:
    """Show downloaded checklist cache coverage by day and hotspot."""
    render_page_header("Checklist cache", screen="cache")
    st.caption(
        "Coverage of downloaded checklist detail files versus the regional "
        "daily feed cache. Days and hotspots are derived from on-disk files."
    )

    region_code = render_region_code_input(
        help="eBird region, e.g. US-FL-099. Use Look up region code if you only know the name.",
    )

    current_year = date.today().year
    stored_year = int(st.session_state.get("cache_status_year", current_year))
    stored_year = min(max(stored_year, 2002), current_year)
    if "cache_status_year_input" in st.session_state:
        st.session_state.cache_status_year_input = min(
            max(int(st.session_state.cache_status_year_input), 2002),
            current_year,
        )
    year = st.number_input(
        "Year",
        min_value=2002,
        max_value=current_year,
        value=stored_year,
        step=1,
        key="cache_status_year_input",
        help="Daily feed and checklist details can be cached for any year from 2002 through the current year.",
    )
    st.session_state.cache_status_year = int(year)

    local_regions = list_local_checklist_regions()
    last_seen_cols = st.columns([2.4, 1.6], vertical_alignment="bottom")
    with last_seen_cols[0]:
        st.caption(
            "Last-seen index files (`ebird_<region>_local_last_seen.json`) are "
            "built from downloaded checklist details on disk — no eBird API calls."
        )
    with last_seen_cols[1]:
        rebuild_last_seen = st.button(
            "Rebuild last-seen from checklists",
            use_container_width=True,
            disabled=not bool(local_regions),
            help=(
                "Rescan downloaded checklist JSON for every region on disk and "
                "rewrite the per-region last-seen cache files."
            ),
        )
    if rebuild_last_seen:
        with st.spinner("Rebuilding last-seen caches from checklist files…"):
            rebuilt = rebuild_local_last_seen_indexes()
        if not rebuilt:
            st.warning("No downloaded checklists found under `ebird_checklists/`.")
        else:
            summary = ", ".join(
                f"{row['region_code']} ({row['species']:,} spp)"
                for row in rebuilt
            )
            st.success(f"Rebuilt last-seen cache for {len(rebuilt)} region(s): {summary}.")

    refresh_cols = st.columns([1.15, 2.4, 1.7], vertical_alignment="bottom")
    with refresh_cols[0]:
        st.markdown(
            '<div style="padding-bottom:0.55rem;white-space:nowrap;">'
            "<strong>Auto-refresh</strong></div>",
            unsafe_allow_html=True,
        )
    with refresh_cols[1]:
        auto_refresh = st.selectbox(
            "Auto-refresh",
            options=["never", "5", "15", "60"],
            format_func=lambda value: {
                "never": "Never",
                "5": "Every 5 seconds",
                "15": "Every 15 seconds",
                "60": "Every 1 minute",
            }[value],
            key="cache_status_auto_refresh",
            help="Automatically reload download progress and cache status.",
            label_visibility="collapsed",
        )
    with refresh_cols[2]:
        refresh = st.button("Refresh status", use_container_width=True)
    if refresh:
        st.session_state["cache_status_force_refresh"] = True

    if not region_code:
        st.info("Enter a region code to inspect the checklist cache.")
        return

    interval_seconds = None if auto_refresh == "never" else int(auto_refresh)
    run_every = (
        timedelta(seconds=interval_seconds) if interval_seconds else None
    )

    @st.fragment(run_every=run_every)
    def _cache_status_live() -> None:
        _consume_checklist_download_request()
        force_refresh = bool(
            st.session_state.pop("cache_status_force_refresh", False)
        )
        live_region = str(st.session_state.get("checklists_region") or "").strip()
        live_year = int(
            st.session_state.get("cache_status_year", date.today().year)
        )
        if not live_region:
            st.info("Enter a region code to inspect the checklist cache.")
            return
        if interval_seconds:
            label = {
                5: "every 5 seconds",
                15: "every 15 seconds",
                60: "every 1 minute",
            }.get(interval_seconds, f"every {interval_seconds}s")
            st.caption(
                f"Auto-refresh {label} · "
                f"updated {datetime.now().astimezone().strftime('%H:%M:%S')}"
            )
        _render_cache_status_body(
            live_region,
            live_year,
            force_refresh=force_refresh,
        )

    _cache_status_live()


def render_feed_cache_controls(
    region_code: str,
    year: int,
    status: dict,
) -> None:
    """Load or update the regional daily-feed cache for the selected year."""
    feed_progress = load_feed_cache_progress(region_code, year)
    feed_status = str(feed_progress.get("status") or "idle")
    feed_running = feed_status == "running" and _is_process_running(
        feed_progress.get("pid")
    )
    if feed_status == "running" and not feed_running:
        feed_status = "interrupted"

    exists = bool(status.get("feed_cache_exists"))
    action_label = (
        f"Update daily feed for {year}"
        if exists
        else f"Load daily feed for {year}"
    )
    st.subheader("Regional daily feed")
    st.caption(
        "One eBird request per calendar day. Missing days (including prior years) "
        "are fetched; already-cached historical days are skipped. Today is "
        "always refreshed for the current year."
    )
    feed_cols = st.columns([2.2, 1.1])
    with feed_cols[0]:
        start_feed = st.button(
            action_label,
            key="start_feed_cache",
            disabled=feed_running or _checklist_download_active(region_code, year),
            use_container_width=True,
            help="Builds ebird_<region>_checklists_<year>.json used to know which checklist details are missing.",
        )
    with feed_cols[1]:
        stop_feed = st.button(
            "Stop feed",
            key="stop_feed_cache",
            disabled=not feed_running,
            use_container_width=True,
        )
    if stop_feed:
        request_feed_cache_stop(region_code, year)
        time.sleep(0.15)
        st.rerun()
    if start_feed:
        if not ensure_api_key():
            return
        error = _start_feed_cache(region_code, year)
        if error:
            st.error(f"Could not start daily-feed cache: {error}")
        else:
            st.success(
                f"Daily-feed cache started for `{region_code}` {year}. "
                "Refresh status to watch progress."
            )
            time.sleep(0.25)
            st.rerun()

    total = int(feed_progress.get("total") or 0)
    processed = int(feed_progress.get("processed") or 0)
    remaining = int(feed_progress.get("remaining") or 0)
    if total:
        st.progress(
            min(1.0, processed / total),
            text=(
                f"{feed_status.title()} · {processed:,}/{total:,} days "
                f"· last {feed_progress.get('last_day') or '—'}"
            ),
        )
    elif feed_running:
        st.info("Daily-feed worker is starting…")
    if remaining and feed_running:
        st.caption(f"{remaining:,} days remaining to fetch.")


def _render_cache_status_body(
    region_code: str,
    year: int,
    *,
    force_refresh: bool = False,
) -> None:
    """Render checklist cache metrics, download controls, and day/hotspot tables."""
    if force_refresh:
        with st.spinner("Scanning checklist cache…"):
            status = build_checklist_cache_status(
                region_code,
                int(year),
                force_refresh=True,
            )
    else:
        status = build_checklist_cache_status(
            region_code,
            int(year),
            force_refresh=False,
        )

    expected = int(status.get("expected_total") or 0)
    downloaded = int(status.get("downloaded_total") or 0)
    coverage = (100.0 * downloaded / expected) if expected else None
    metrics = st.columns(4)
    with metrics[0]:
        st.metric("Downloaded", f"{downloaded:,}")
    with metrics[1]:
        st.metric(
            "Expected (feed)",
            f"{expected:,}" if status.get("feed_cache_exists") else "—",
        )
    with metrics[2]:
        st.metric(
            "Coverage",
            f"{coverage:.1f}%" if coverage is not None else "—",
        )
    with metrics[3]:
        st.metric("Hotspots", f"{int(status.get('hotspot_count') or 0):,}")

    days_cols = st.columns(3)
    with days_cols[0]:
        st.metric("Days in feed", f"{int(status.get('days_in_feed') or 0):,}")
    with days_cols[1]:
        st.metric(
            "Days downloaded",
            f"{int(status.get('days_with_downloads') or 0):,}",
        )
    with days_cols[2]:
        truncated = status.get("truncated_dates") or []
        st.metric("Truncated feed days", f"{len(truncated):,}")

    if not status.get("feed_cache_exists"):
        st.warning(
            f"No regional feed cache at `{status.get('feed_cache_path')}`. "
            f"Use **Load daily feed for {int(year)}** below. Downloaded files "
            "are still summarized."
        )
    elif truncated:
        with st.expander(f"Truncated feed days ({len(truncated)})"):
            st.write(", ".join(truncated))

    updated = status.get("updated_at")
    if updated:
        st.caption(f"Status index updated {updated}")

    render_feed_cache_controls(region_code, int(year), status)

    if status.get("feed_cache_exists"):
        render_checklist_download_maintenance(
            region_code,
            int(year),
            days=status.get("days") or [],
            hotspots=status.get("hotspots") or [],
        )
        min_species_filter = int(
            st.session_state.get("checklist_download_min_species", 0) or 0
        )
        try:
            species_remaining = missing_checklists_by_species_count(
                region_code,
                int(year),
                min_species=min_species_filter,
            )
        except FileNotFoundError:
            species_remaining = []
        if species_remaining:
            total_remaining = sum(int(row["Remaining"]) for row in species_remaining)
            st.subheader("Remaining to load by species count")
            caption = (
                f"{total_remaining:,} missing checklist detail"
                f"{'s' if total_remaining != 1 else ''} grouped by feed numSpecies"
            )
            if min_species_filter > 0:
                caption += f" (filter: ≥{min_species_filter} species)"
            st.caption(caption)
            st.bar_chart(
                species_remaining,
                x="Species count",
                y="Remaining",
                height=280,
            )
    else:
        st.info(
            "Checklist-detail download controls appear after the daily feed "
            f"for {int(year)} is loaded."
        )

    view = st.radio(
        "View",
        options=["by_day", "by_hotspot"],
        format_func=lambda value: {
            "by_day": "By day",
            "by_hotspot": "By hotspot",
        }[value],
        horizontal=True,
        key="cache_status_view",
    )

    days = status.get("days") or []
    hotspots = status.get("hotspots") or []

    download_active = (
        bool(status.get("feed_cache_exists"))
        and _checklist_download_active(region_code, int(year))
    )

    if view == "by_day":
        if not days:
            st.info("No downloaded or feed days found for this region/year.")
            return
        day_rows = [
            {
                "Day": row["day"],
                "Downloaded": int(row.get("downloaded") or 0),
                "Expected": int(row.get("expected") or 0),
                "Missing": int(
                    row.get("missing")
                    if row.get("missing") is not None
                    else max(
                        0,
                        int(row.get("expected") or 0) - int(row.get("downloaded") or 0),
                    )
                ),
                "Coverage %": (
                    round(
                        100.0
                        * int(row.get("downloaded") or 0)
                        / int(row["expected"]),
                        1,
                    )
                    if int(row.get("expected") or 0)
                    else None
                ),
                "Truncated": "yes" if row.get("truncated") else "",
            }
            for row in days
        ]
        chart_rows = [
            {
                "Day": row["Day"],
                "Downloaded": row["Downloaded"],
                "Expected": row["Expected"],
            }
            for row in day_rows
        ]
        coverage_chart_rows = [
            {
                "Day": row["Day"],
                "Coverage %": (
                    float(row["Coverage %"])
                    if row["Coverage %"] is not None
                    else 0.0
                ),
            }
            for row in day_rows
        ]
        st.subheader("Checklists per day")
        st.bar_chart(
            chart_rows,
            x="Day",
            y=["Downloaded", "Expected"],
            height=280,
        )
        st.subheader("Coverage % per day")
        st.caption("Downloaded ÷ expected from the regional daily feed (0% when expected is unknown).")
        st.bar_chart(
            coverage_chart_rows,
            x="Day",
            y="Coverage %",
            height=280,
        )

        can_download = bool(status.get("feed_cache_exists"))
        if can_download and not any(int(row["Missing"]) > 0 for row in day_rows):
            st.caption("All feed days for this year are fully downloaded.")
        elif not can_download:
            st.caption("Download icons appear after a regional daily-feed cache exists.")

        _render_cache_action_table(
            day_rows,
            columns=[
                ("Day", "Day"),
                ("Downloaded", "Downloaded"),
                ("Expected", "Expected"),
                ("Missing", "Missing"),
                ("Coverage %", "Coverage %"),
                ("Truncated", "Truncated"),
                ("", ""),
            ],
            widths=[1.2, 1.0, 1.0, 0.9, 1.0, 0.9, 0.45],
            formatters={
                "Downloaded": lambda row: f"{row['Downloaded']:,}",
                "Expected": lambda row: f"{row['Expected']:,}",
                "Missing": lambda row: f"{row['Missing']:,}",
                "Coverage %": lambda row: (
                    "—"
                    if row["Coverage %"] is None
                    else f"{row['Coverage %']:.1f}"
                ),
                "Truncated": lambda row: row["Truncated"] or "—",
            },
            state_key="cache_status_day_sort",
            default_sort_column="Day",
            default_sort_direction="asc",
            region_code=region_code,
            year=int(year),
            can_download=can_download,
            download_active=download_active,
            row_download_key=lambda row: f"cache_day_load_{row['Day']}",
            row_download_label=lambda row: row["Day"],
            row_download_day=lambda row: row["Day"],
            row_can_download=lambda row: int(row["Missing"]) > 0,
        )
        return

    if not hotspots:
        st.info("No hotspot checklist files found for this region/year.")
        return
    hotspot_rows = [
        {
            "Hotspot": row.get("locName") or row.get("locId") or "Unknown",
            "locId": row.get("locId") or "",
            "Downloaded": int(
                row.get("downloaded")
                if row.get("downloaded") is not None
                else row.get("checklists")
                or 0
            ),
            "Expected": int(row.get("expected") or 0),
            "Missing": int(row.get("missing") or 0),
            "First day": row.get("first_day") or "",
            "Last day": row.get("last_day") or "",
        }
        for row in hotspots
    ]
    st.subheader("By hotspot")

    filter_cols = st.columns([2.2, 3.8], vertical_alignment="bottom")
    with filter_cols[0]:
        min_checklists = int(
            st.number_input(
                "Min checklists",
                min_value=0,
                value=0,
                step=1,
                key="cache_hotspot_min_checklists",
                help=(
                    "Show only hotspots with more than this many expected "
                    "checklists in the regional feed."
                ),
            )
        )
    filtered_hotspot_rows = [
        row
        for row in hotspot_rows
        if int(row.get("Expected") or 0) > min_checklists
    ]
    with filter_cols[1]:
        if min_checklists > 0:
            st.caption(
                f"Showing {len(filtered_hotspot_rows):,} of {len(hotspot_rows):,} "
                f"locations with more than {min_checklists:,} checklist"
                f"{'s' if min_checklists != 1 else ''}"
            )
        else:
            st.caption(f"{len(hotspot_rows):,} locations in feed or downloaded cache")

    if not filtered_hotspot_rows:
        st.info(
            f"No hotspots with more than {min_checklists:,} expected checklist"
            f"{'s' if min_checklists != 1 else ''}."
        )
        return

    can_download = bool(status.get("feed_cache_exists"))
    if can_download and not any(int(row["Missing"]) > 0 for row in filtered_hotspot_rows):
        st.caption("All listed hotspots are fully downloaded.")
    elif not can_download:
        st.caption("Download icons appear after a regional daily-feed cache exists.")

    _render_cache_action_table(
        filtered_hotspot_rows,
        columns=[
            ("Hotspot", "Hotspot"),
            ("locId", "locId"),
            ("Downloaded", "Downloaded"),
            ("Expected", "Expected"),
            ("Missing", "Missing"),
            ("First day", "First day"),
            ("Last day", "Last day"),
            ("", ""),
        ],
        widths=[2.4, 1.0, 0.9, 0.9, 0.85, 1.0, 1.0, 0.45],
        formatters={
            "Downloaded": lambda row: f"{row['Downloaded']:,}",
            "Expected": lambda row: f"{row['Expected']:,}",
            "Missing": lambda row: f"{row['Missing']:,}",
            "First day": lambda row: row["First day"] or "—",
            "Last day": lambda row: row["Last day"] or "—",
            "locId": lambda row: row["locId"] or "—",
        },
        state_key="cache_status_hotspot_sort",
        default_sort_column="Missing",
        default_sort_direction="desc",
        region_code=region_code,
        year=int(year),
        can_download=can_download,
        download_active=download_active,
        row_download_key=lambda row: f"cache_hotspot_load_{row['locId']}",
        row_download_label=lambda row: f"{row['Hotspot']} ({row['locId']})",
        row_download_loc_id=lambda row: str(row["locId"] or "").strip() or None,
        row_can_download=lambda row: (
            int(row["Missing"]) > 0 and bool(str(row.get("locId") or "").strip())
        ),
    )


def render_checklists() -> None:
    render_page_header("Checklists", screen="checklists")
    st.caption(
        "Pick a region, choose a top hotspot, then browse recent checklists. "
        "New-bird counts use `lifeLists/ebird_world_life_list.csv` and "
        "`lifeLists/ebird_<region>_life_list.csv`."
    )

    region_code = render_region_code_input(
        help=(
            "eBird region, e.g. US-FL-099, US-FL, or US. "
            "Use Look up region code if you only know the place name."
        ),
    )

    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    region_life = load_life_list(region_code) if region_code else None
    world_total = life_list_total(world_life)
    region_total = life_list_total(region_life)

    if st.button(
        "Open full region species gallery",
        key="gallery_region_species_list",
        use_container_width=True,
        help=(
            "Loads every species ever recorded in this region from "
            "eBird /product/spplist, resolves names via taxonomy, and opens "
            "the gallery."
        ),
        disabled=not bool(region_code),
    ):
        if ensure_api_key():
            open_region_species_gallery(region_code)

    total_cols = st.columns(2)
    with total_cols[0]:
        if world_total is None:
            st.warning(
                f"No world life list at `{life_list_path(WORLD_LIFE_LIST_CODE)}`."
            )
        else:
            st.caption("World life list")
            if st.button(
                f"{world_total} species",
                key="gallery_world_life_list",
                type="tertiary",
                help="Open world life list gallery",
            ):
                open_life_list_gallery(
                    WORLD_LIFE_LIST_CODE,
                    title="World life list gallery",
                )
    with total_cols[1]:
        if region_code and region_total is None:
            st.warning(
                f"No region life list at `{life_list_path(region_code)}`."
            )
        elif region_total is not None:
            st.caption(f"Region life list ({region_code})")
            if st.button(
                f"{region_total} species",
                key="gallery_region_life_list",
                type="tertiary",
                help=f"Open region life list gallery for {region_code}",
            ):
                open_life_list_gallery(
                    region_code,
                    title=f"Region life list gallery · {region_code}",
                )
        else:
            st.caption("Region life list")
            st.write("—")

    if "life_list_scope" not in st.session_state:
        st.session_state.life_list_scope = current_life_list_scope()
    life_scope = st.radio(
        "Filter new birds by",
        options=["all", "region", "world"],
        format_func=lambda value: {
            "all": "All birds",
            "region": f"New to region ({region_code or 'region'})",
            "world": "New to world",
        }[value],
        horizontal=True,
        key="life_list_scope",
        on_change=_sync_life_list_scope_pref,
        help="Controls which birds count as “new” in the summary, checklists, and gallery.",
    )
    st.session_state.life_list_scope_pref = life_scope

    def apply_hotspots(region: str, rows: list[dict]) -> None:
        st.session_state.checklists_region = region
        st.session_state.checklists_hotspots = rows
        hotspot_ids = [h["locId"] for h in rows if h.get("locId")]
        if hotspot_ids and st.session_state.get("checklists_loc_id") not in hotspot_ids:
            st.session_state.checklists_loc_id = (
                DEFAULT_HOTSPOT_ID
                if DEFAULT_HOTSPOT_ID in hotspot_ids
                else hotspot_ids[0]
            )

    hotspots: list[dict] = []
    if region_code:
        if st.session_state.get("checklists_region") == region_code:
            hotspots = list(st.session_state.get("checklists_hotspots") or [])
        if not hotspots:
            hotspots = load_cached_hotspots(region_code)
            if hotspots:
                apply_hotspots(region_code, hotspots)

    load_label = "Load additional hotspots" if hotspots else "Load hotspots"
    if st.button(
        load_label,
        key="load_hotspots_button",
        type="primary" if not hotspots else "secondary",
        help=(
            "Fetch hotspots for this region that are not already in the "
            "dropdown / on-disk cache."
        ),
    ):
        if not region_code:
            st.warning("Enter a region code.")
        elif ensure_api_key():
            with st.spinner(f"Loading additional hotspots for {region_code}…"):
                try:
                    merged, added = EBirdClient().additional_hotspots(
                        region_code,
                        existing=hotspots,
                    )
                except MissingEbirdApiKey:
                    ensure_api_key()
                except requests.HTTPError as exc:
                    st.error(
                        f"eBird API error: {exc.response.status_code if exc.response else exc}"
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    apply_hotspots(region_code, merged)
                    hotspots = merged
                    if added:
                        st.success(
                            f"Added {len(added):,} hotspot{'s' if len(added) != 1 else ''} "
                            f"({len(merged):,} total)."
                        )
                    elif merged:
                        st.info(f"No additional hotspots. {len(merged):,} already cached.")
                    else:
                        st.warning(f"No hotspots found for {region_code}.")

    if not hotspots:
        st.info("Enter a region and click Load hotspots.")
        return

    loc_ids = [h["locId"] for h in hotspots if h.get("locId")]
    labels = {h["locId"]: hotspot_label(h) for h in hotspots if h.get("locId")}
    current_loc = st.session_state.get("checklists_loc_id")
    if current_loc not in loc_ids:
        current_loc = DEFAULT_HOTSPOT_ID if DEFAULT_HOTSPOT_ID in loc_ids else loc_ids[0]
        st.session_state.checklists_loc_id = current_loc
    index = loc_ids.index(current_loc)

    loc_id = st.selectbox(
        f"Hotspot ({len(loc_ids)})",
        options=loc_ids,
        index=index,
        format_func=lambda lid: labels.get(lid, lid),
        help="Cached hotspots for this region, ordered by all-time species count.",
    )
    end_date = st.date_input(
        "Last observation day",
        value=st.session_state.get("checklist_end_date", date.today()),
        max_value=date.today(),
        help="Include checklists on this day and the prior days in the range.",
    )
    days_back = st.slider("Days to include", min_value=1, max_value=30, value=7)
    page_size = 50
    stored_start = end_date - timedelta(days=days_back - 1)

    action_cols = st.columns(3)
    with action_cols[0]:
        show_api = st.button("Show checklists", type="primary", use_container_width=True)
    with action_cols[1]:
        show_cache = st.button(
            "Show from cache",
            use_container_width=True,
            help=(
                "Browse downloaded checklist files for the selected hotspot "
                "in the date range above — no eBird checklist API calls."
            ),
        )
    with action_cols[2]:
        show_all_cache = st.button(
            "Show all from cache",
            use_container_width=True,
            help=(
                "Browse every downloaded checklist for the selected hotspot, "
                "ignoring the days-to-include limit."
            ),
        )

    if show_api:
        if ensure_api_key():
            active_region = st.session_state.get("checklists_region", region_code)
            life_for_region = load_life_list(active_region)
            life_for_world = load_life_list(WORLD_LIFE_LIST_CODE)
            with st.spinner("Loading checklists…"):
                try:
                    client = EBirdClient()
                    summaries = client.location_checklists(
                        loc_id,
                        days_back=days_back,
                        end_date=end_date,
                    )
                    first_page = enrich_checklists(
                        client,
                        summaries[:page_size],
                        life_for_region,
                        life_for_world,
                        allow_api=True,
                    )
                except MissingEbirdApiKey:
                    ensure_api_key()
                except requests.HTTPError as exc:
                    st.error(
                        f"eBird API error: {exc.response.status_code if exc.response else exc}"
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.session_state.checklists_loc_id = loc_id
                    st.session_state.checklist_days = days_back
                    st.session_state.checklist_end_date = end_date
                    st.session_state.checklist_summaries = summaries
                    st.session_state.checklist_rows = first_page
                    st.session_state.checklist_shown = len(first_page)
                    st.session_state.checklist_life = life_for_region
                    st.session_state.checklist_world_life = life_for_world
                    st.session_state.checklist_hotspot_name = labels.get(loc_id, loc_id)
                    st.session_state.checklist_source = "api"
                    st.session_state.checklist_cache_all_dates = False

    if show_cache or show_all_cache:
        active_region = st.session_state.get("checklists_region", region_code)
        life_for_region = load_life_list(active_region)
        life_for_world = load_life_list(WORLD_LIFE_LIST_CODE)
        all_dates = bool(show_all_cache)
        spinner_label = (
            "Loading all cached checklists for this hotspot…"
            if all_dates
            else "Loading checklists from local cache…"
        )
        with st.spinner(spinner_label):
            if all_dates:
                summaries = load_local_checklists_for_hotspot(
                    active_region,
                    loc_id,
                )
            else:
                summaries = load_local_checklists_for_hotspot(
                    active_region,
                    loc_id,
                    start_date=stored_start,
                    end_date=end_date,
                )
            # Resolve names via taxonomy when an API key is available; otherwise
            # keep species codes from the cached checklist payload.
            client = None
            allow_api = False
            if get_api_key():
                try:
                    client = EBirdClient()
                    allow_api = True
                except Exception:
                    client = None
                    allow_api = False
            first_page = enrich_checklists(
                client,
                summaries[:page_size],
                life_for_region,
                life_for_world,
                allow_api=allow_api,
            )
        st.session_state.checklists_loc_id = loc_id
        st.session_state.checklist_days = days_back
        st.session_state.checklist_end_date = end_date
        st.session_state.checklist_summaries = summaries
        st.session_state.checklist_rows = first_page
        st.session_state.checklist_shown = len(first_page)
        st.session_state.checklist_life = life_for_region
        st.session_state.checklist_world_life = life_for_world
        st.session_state.checklist_hotspot_name = labels.get(loc_id, loc_id)
        st.session_state.checklist_source = "cache"
        st.session_state.checklist_cache_all_dates = all_dates

    rows = st.session_state.get("checklist_rows")
    if rows is None:
        return

    summaries = st.session_state.get("checklist_summaries") or []
    shown = st.session_state.get("checklist_shown", len(rows))
    total = len(summaries)
    source = st.session_state.get("checklist_source", "api")
    hotspot_name = st.session_state.get(
        "checklist_hotspot_name", labels.get(loc_id, loc_id)
    )
    stored_end = st.session_state.get("checklist_end_date", end_date)
    stored_days = st.session_state.get("checklist_days", days_back)
    stored_start = stored_end - timedelta(days=stored_days - 1)
    source_label = "local cache" if source == "cache" else "eBird API"
    if source == "cache" and st.session_state.get("checklist_cache_all_dates"):
        obs_days = [
            str(row.get("_obs_day") or "")[:10]
            for row in summaries
            if row.get("_obs_day") or row.get("isoObsDate") or row.get("obsDt")
        ]
        if not obs_days:
            obs_days = [
                str(row.get("isoObsDate") or row.get("obsDt") or "")[:10]
                for row in summaries
            ]
        obs_days = [day for day in obs_days if len(day) >= 10]
        if obs_days:
            range_label = (
                f"from **{min(obs_days)}** to **{max(obs_days)}** (all cached dates)"
            )
        else:
            range_label = "(all cached dates)"
    else:
        range_label = (
            f"from **{stored_start.isoformat()}** to **{stored_end.isoformat()}**"
        )
    st.write(
        f"Showing **{len(rows)}** of **{total}** checklist(s) at **{hotspot_name}** "
        f"{range_label} ({source_label})."
    )

    if not rows and total == 0:
        if source == "cache":
            st.warning(
                "No downloaded checklists for this selection in that date range."
            )
        else:
            st.warning("No checklists found for this hotspot in that date range.")
        return

    if shown < total:
        if st.button(f"Load more ({min(page_size, total - shown)} more)"):
            life_for_region = st.session_state.get("checklist_life")
            life_for_world = st.session_state.get("checklist_world_life")
            if life_for_world is None:
                life_for_world = load_life_list(WORLD_LIFE_LIST_CODE)
            start = shown
            end = min(shown + page_size, total)
            with st.spinner("Loading more checklists…"):
                more = None
                try:
                    if source == "cache":
                        client = EBirdClient() if get_api_key() else None
                        more = enrich_checklists(
                            client,
                            summaries[start:end],
                            life_for_region,
                            life_for_world,
                            allow_api=bool(client),
                        )
                    elif ensure_api_key():
                        more = enrich_checklists(
                            EBirdClient(),
                            summaries[start:end],
                            life_for_region,
                            life_for_world,
                            allow_api=True,
                        )
                except MissingEbirdApiKey:
                    ensure_api_key()
                except requests.HTTPError as exc:
                    st.error(
                        f"eBird API error: "
                        f"{exc.response.status_code if exc.response else exc}"
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    if more is not None:
                        st.session_state.checklist_rows = rows + more
                        st.session_state.checklist_shown = end
                        st.rerun()

    loaded_rows = st.session_state.get("checklist_rows", [])
    species_summary = build_species_summary(loaded_rows)
    if species_summary:
        st.subheader("Species summary")
        if life_scope == "all":
            filtered = species_summary
        else:
            filtered = [
                row
                for row in species_summary
                if summary_is_new_for_scope(row, life_scope)
            ]
        if life_scope != "all" and not filtered:
            label = "world" if life_scope == "world" else "region"
            st.info(f"No birds new to your {label} life list in the loaded checklists.")
        else:
            if st.button(
                "Open gallery from summary",
                type="primary",
                key="gallery_from_summary",
            ):
                open_gallery(
                    [
                        {
                            "code": item.get("code"),
                            "name": item.get("Species"),
                            "sciName": item.get("sciName"),
                            "is_new_region": bool(item.get("New_region")),
                            "is_new_world": bool(item.get("New_world")),
                            "is_new": bool(item.get("New_region")),
                            "New": bool(item.get("New_region")),
                        }
                        for item in filtered
                    ],
                    title="Species summary gallery",
                )
            render_species_thumbnail_table(filtered, columns=6, width=144)
            st.caption(f"{len(filtered)} species")

    st.subheader("Checklists")
    for row in loaded_rows:
        sub_id = row.get("subId") or row.get("subID")
        date_label = " ".join(
            part for part in [row.get("obsDt"), row.get("obsTime")] if part
        )
        species = row.get("numSpecies", "?")
        observer = row.get("userDisplayName") or "Unknown observer"
        location = str(row.get("locName") or row.get("locId") or "").strip()
        checklist_url = f"https://ebird.org/checklist/{sub_id}"
        region_new = row.get("new_count_region", row.get("new_count"))
        world_new = row.get("new_count_world")
        new_bits: list[str] = []
        if life_scope == "region":
            if region_new is not None:
                new_bits.append(f"**{region_new} new** to region")
        elif life_scope == "world":
            if world_new is not None:
                new_bits.append(f"**{world_new} new** to world")
        else:
            if region_new is not None:
                new_bits.append(f"**{region_new}** new to region")
            if world_new is not None:
                new_bits.append(f"**{world_new}** new to world")
        new_label = f" · {' · '.join(new_bits)}" if new_bits else ""
        location_label = f" · {location}" if location else ""

        with st.container(border=True):
            st.markdown(
                f"**[{date_label}]({checklist_url})** · {species} species · "
                f"{observer}{location_label}{new_label}"
            )
            species_rows = row.get("species_rows") or []
            gallery_rows = species_rows
            if life_scope != "all":
                gallery_rows = [
                    obs
                    for obs in species_rows
                    if obs_is_new_for_scope(obs, life_scope)
                ]
            if gallery_rows and st.button(
                "Open gallery",
                key=f"gallery_checklist_{sub_id}",
            ):
                open_gallery(
                    [
                        {
                            "code": obs.get("code"),
                            "name": obs.get("name"),
                            "sciName": obs.get("sciName"),
                            "is_new_region": bool(
                                obs.get("is_new_region")
                                if "is_new_region" in obs
                                else obs.get("is_new")
                            ),
                            "is_new_world": bool(obs.get("is_new_world")),
                            "is_new": bool(
                                obs.get("is_new_region")
                                if "is_new_region" in obs
                                else obs.get("is_new")
                            ),
                        }
                        for obs in gallery_rows
                    ],
                    title=f"Checklist gallery · {date_label}",
                )
            with st.expander("Species"):
                if not species_rows:
                    # Fallback for older session rows: resolve codes to common names now.
                    try:
                        client = EBirdClient()
                        detail = client.checklist(str(sub_id))
                        codes = [
                            str(obs.get("speciesCode") or "")
                            for obs in detail.get("obs") or []
                            if obs.get("speciesCode")
                        ]
                        name_by_code = client.species_names(codes)
                        species_rows = []
                        for obs in detail.get("obs") or []:
                            code = str(obs.get("speciesCode") or "")
                            if not code:
                                continue
                            species_rows.append(
                                {
                                    "code": code,
                                    "name": name_by_code.get(code, code),
                                    "count": obs.get("howManyAtleast")
                                    or obs.get("howMany")
                                    or obs.get("howManyStr")
                                    or "",
                                    "is_new": False,
                                    "is_new_region": False,
                                    "is_new_world": False,
                                }
                            )
                    except MissingEbirdApiKey:
                        ensure_api_key()
                        continue
                    except Exception as exc:
                        st.write(f"Could not load checklist detail: {exc}")
                        continue
                display_rows = species_rows
                if life_scope != "all":
                    display_rows = [
                        obs
                        for obs in species_rows
                        if obs_is_new_for_scope(obs, life_scope)
                    ]
                if not display_rows:
                    if life_scope != "all":
                        st.write("No species match this life-list filter.")
                    else:
                        st.write("No species listed.")
                    continue
                for obs in display_rows:
                    bird_name = obs.get("name") or obs.get("code") or "Unknown"
                    suffix = (
                        f" — {obs['count']}"
                        if obs.get("count") not in ("", None)
                        else ""
                    )
                    marker = new_bird_marker(
                        bool(
                            obs.get("is_new_region")
                            if "is_new_region" in obs
                            else obs.get("is_new")
                        ),
                        bool(obs.get("is_new_world")),
                        scope=life_scope,
                    )
                    # new_bird_marker returns markdown; strip ** for plain write
                    plain_marker = (
                        marker.replace("**", "") if marker else ""
                    )
                    if life_scope != "all":
                        plain_marker = ""
                    photo_col, text_col = st.columns([1, 5], vertical_alignment="center")
                    with photo_col:
                        render_species_photo(
                            obs.get("code"),
                            scientific_name=obs.get("sciName") or None,
                            width=56,
                        )
                    with text_col:
                        st.write(f"{bird_name}{suffix}{plain_marker}")


st.set_page_config(page_title="Birds", page_icon="🪶", layout="wide")
apply_ui_layout()
get_api_key()  # ingest ?EBIRD_API_KEY=… into session when present
if st.session_state.get("ebird_api_key_needed") and not get_api_key():
    render_api_key_form()
render_ebird_rate_limit_notices()
maybe_open_saved_gallery_from_query()
missing = st.session_state.pop("saved_gallery_missing", None)
if missing:
    st.warning(f"Saved gallery `{missing}` was not found.")
dashboard = current_dashboard()
if dashboard == "gallery" and st.session_state.get("gallery_birds"):
    render_gallery()
elif dashboard == "checklists":
    render_checklists()
elif dashboard == "cache":
    render_cache_status()
elif dashboard == "maintenance":
    render_general_cache_maintenance()
else:
    render_saved_galleries()
