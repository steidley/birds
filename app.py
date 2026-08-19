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
    configured_observer_names,
    filter_regions_by_query,
    get_api_key,
    list_local_checklist_regions,
    filter_hotspots_for_region,
    load_cached_hotspots,
    load_cached_region_list,
    load_cached_taxa,
    load_disk_region_species_codes,
    load_taxonomy_cache,
    load_local_checklists_for_hotspot,
    load_own_local_checklists,
    local_own_recent_sightings_for_species,
    local_recent_sightings_for_species,
    rebuild_local_last_seen_indexes,
    region_historical_species_cache_coverage,
    parse_ebird_obs_day,
    resolve_ebird_code,
    sort_hotspots,
)
from download_checklists import (
    dedupe_downloaded_checklists,
    download_progress_path,
    download_window_slices,
    format_download_windows,
    load_download_progress,
    load_feed_cache_progress,
    missing_checklists_by_species_count,
    request_download_stop,
    request_feed_cache_stop,
    save_checklist_detail,
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
CHECKLIST_GALLERY_QUERY = "checklist_gallery"
SUMMARY_GALLERY_QUERY = "summary_gallery"
SAVED_GALLERY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d+)?$")
CHECKLIST_SUB_ID_RE = re.compile(r"^S\d+$")
DOWNLOAD_MY_DATA_URL = "https://ebird.org/downloadMyData"
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
.stApp [data-testid="stPopoverButton"] {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  padding-left: 0.45rem !important;
  padding-right: 0.45rem !important;
}
.stApp [data-testid="stPopoverButton"] > div {
  margin-right: 0 !important;
  justify-content: center !important;
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
section[data-testid="stSidebar"] {
  min-width: 16rem;
  max-width: 18rem;
}
[data-testid="stSidebarCollapseButton"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"] {
  display: none !important;
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
OPEN_GALLERY_ICON_BUTTON_CSS = """
<style>
div[class*="st-key-open_gallery_icon_"] {
  width: 2.4rem !important;
  min-width: 2.4rem !important;
  max-width: 2.4rem !important;
}
div[class*="st-key-open_gallery_icon_"] button p {
  display: none !important;
}
div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-open_gallery_icon_"]) {
  align-items: center !important;
  gap: 0.35rem !important;
}
div[class*="st-key-header_region_"] {
  display: flex !important;
  justify-content: flex-end !important;
}
div[class*="st-key-header_region_"] button {
  justify-content: flex-end !important;
}
div[class*="st-key-header_region_"] button p {
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  max-width: 22rem !important;
  text-align: right !important;
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


def request_user_agent() -> str:
    """Browser User-Agent for the current Streamlit session, if available."""
    try:
        headers = getattr(st.context, "headers", None)
        if headers is not None:
            ua = headers.get("User-Agent") or headers.get("user-agent")
            if ua:
                return str(ua)
    except Exception:
        pass
    return ""


def is_iphone_user_agent() -> bool:
    return "iphone" in request_user_agent().lower()


def apply_iphone_mobile_layout() -> None:
    """Use mobile layout on iPhone unless the user picked a display width."""
    if st.session_state.get("ui_layout_user_set"):
        return
    radio = st.session_state.get("ui_layout_mode_radio")
    if radio == "desktop":
        st.session_state.ui_layout_pref = "desktop"
        st.session_state.ui_layout_user_set = True
        return
    if radio == "mobile":
        st.session_state.ui_layout_pref = "mobile"
        return
    if not is_iphone_user_agent():
        return
    st.session_state.ui_layout_pref = "mobile"
    st.session_state.ui_layout_mode_radio = "mobile"


def current_ui_layout() -> str:
    """Return ``desktop`` or ``mobile`` from the cache-maintenance layout control."""
    radio = st.session_state.get("ui_layout_mode_radio")
    if radio in {"desktop", "mobile"}:
        return radio
    mode = str(st.session_state.get("ui_layout_pref") or "desktop")
    return mode if mode in {"desktop", "mobile"} else "desktop"


def apply_ui_layout() -> None:
    """Apply desktop (full width) or mobile (narrow) layout CSS."""
    mode = current_ui_layout()
    st.session_state.ui_layout_pref = mode
    css = UI_LAYOUT_MOBILE_CSS if mode == "mobile" else UI_LAYOUT_DESKTOP_CSS
    st.markdown(UI_HEADING_CSS + OPEN_GALLERY_ICON_BUTTON_CSS + css, unsafe_allow_html=True)


def _sync_ui_layout_pref() -> None:
    """Copy the layout radio into a key that survives leaving this screen."""
    chosen = st.session_state.get("ui_layout_mode_radio")
    if chosen in {"desktop", "mobile"}:
        st.session_state.ui_layout_pref = chosen
        st.session_state.ui_layout_user_set = True


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
        frame_bird = {
                "is_new_region": bool(
                    item.get("is_new_region") or item.get("New_region")
                ),
                "is_new_world": bool(
                    item.get("is_new_world") or item.get("New_world")
                ),
                "is_foy_region": bool(
                    item.get("is_foy_region") or item.get("FoY_region")
                ),
                "is_foy_world": bool(
                    item.get("is_foy_world") or item.get("FoY_world")
                ),
            }
        border = gallery_frame_outline_css(frame_bird)
        photo = inaturalist_photo_for_code(str(code), sci) if code else None
        if photo and photo.get("image_url"):
            src = html.escape(str(photo["image_url"]), quote=True)
            alt = html.escape(name or "species", quote=True)
            inner = (
                f'<img src="{src}" alt="{alt}" '
                f'style="width:{width}px;height:{width}px;object-fit:cover;'
                f'display:block;margin:0;padding:0;border:0;{border}"/>'
            )
        else:
            label = html.escape((name[:10] or "—"), quote=False)
            inner = (
                f'<div style="width:{width}px;height:{width}px;display:flex;'
                f'align-items:center;justify-content:center;font-size:11px;'
                f'color:#64748b;background:#f1f5f9;margin:0;padding:0;{border}">'
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


def render_checklist_species_summary_grid(items: list[dict], *, width: int = 144) -> None:
    """Clickable species-summary thumbnails that open the gallery in-session."""
    if not items:
        return
    st.markdown(
        f"""
<style>
div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-checklist_summary_open_"]) {{
  display: grid !important;
  grid-template-columns: repeat(auto-fill, {width}px) !important;
  gap: 1px !important;
  justify-content: start !important;
  align-items: start !important;
  flex-wrap: unset !important;
}}
div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-checklist_summary_open_"]) > div {{
  width: {width}px !important;
  min-width: {width}px !important;
  max-width: {width}px !important;
  flex: none !important;
  padding: 0 !important;
  position: relative !important;
  height: {width}px !important;
  overflow: hidden !important;
}}
div[data-testid="column"]:has(div[class*="st-key-checklist_summary_open_"]) [data-testid="stVerticalBlock"],
div[data-testid="stColumn"]:has(div[class*="st-key-checklist_summary_open_"]) [data-testid="stVerticalBlock"] {{
  gap: 0 !important;
  height: {width}px !important;
  position: relative !important;
}}
div[class*="st-key-checklist_summary_open_"] {{
  position: absolute !important;
  inset: 0 !important;
  margin: 0 !important;
  height: {width}px !important;
  z-index: 2;
}}
div[class*="st-key-checklist_summary_open_"] button {{
  width: {width}px !important;
  height: {width}px !important;
  min-height: {width}px !important;
  opacity: 0 !important;
  cursor: pointer !important;
  border: 0 !important;
  padding: 0 !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(max(len(items), 1), gap="small")
    for index, (col, item) in enumerate(zip(cols, items)):
        with col:
            code = item.get("code")
            sci = item.get("sciName") or None
            name = str(item.get("Species") or item.get("name") or code or "Open")
            frame_bird = {
                "is_new_region": bool(
                    item.get("is_new_region") or item.get("New_region")
                ),
                "is_new_world": bool(
                    item.get("is_new_world") or item.get("New_world")
                ),
                "is_foy_region": bool(
                    item.get("is_foy_region") or item.get("FoY_region")
                ),
                "is_foy_world": bool(
                    item.get("is_foy_world") or item.get("FoY_world")
                ),
            }
            border = gallery_frame_outline_css(frame_bird)
            photo = inaturalist_photo_for_code(str(code), sci) if code else None
            alt = html.escape(name or "species", quote=True)
            if photo and photo.get("image_url"):
                src = html.escape(str(photo["image_url"]), quote=True)
                inner = (
                    f'<img src="{src}" alt="{alt}" '
                    f'style="width:{width}px;height:{width}px;object-fit:cover;'
                    f'display:block;margin:0;padding:0;border:0;{border}"/>'
                )
            else:
                label = html.escape((name[:10] or "—"), quote=False)
                inner = (
                    f'<div style="width:{width}px;height:{width}px;display:flex;'
                    f'align-items:center;justify-content:center;font-size:11px;'
                    f'color:#64748b;background:#f1f5f9;margin:0;padding:0;{border}">'
                    f"{label}</div>"
                )
            st.markdown(inner, unsafe_allow_html=True)
            st.button(
                " ",
                key=f"checklist_summary_open_{index}",
                help=name,
                type="tertiary",
                use_container_width=True,
                on_click=queue_open_summary_gallery_at,
                args=(index,),
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
    start_day: str | None = None,
    end_day: str | None = None,
    loc_id: str | None = None,
    min_species: int = 0,
    prior_years: int = 0,
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
    if start_day:
        command.extend(["--start-day", start_day])
    if end_day:
        command.extend(["--end-day", end_day])
    if loc_id:
        command.extend(["--loc-id", loc_id])
    species_floor = max(0, int(min_species or 0))
    if species_floor > 0:
        command.extend(["--min-species", str(species_floor)])
    prior = max(0, int(prior_years or 0))
    if prior > 0:
        command.extend(["--prior-years", str(prior)])
    try:
        subprocess.Popen(
            command,
            cwd=str(Path(__file__).parent),
            start_new_session=True,
        )
    except OSError as exc:
        return str(exc)
    return None


def parse_streamlit_date_range(value: object) -> tuple[date, date] | None:
    """Normalize a Streamlit date_input value into an inclusive start/end."""
    if isinstance(value, date):
        return value, value
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
        if isinstance(start, date) and isinstance(end, date):
            if start > end:
                start, end = end, start
            return start, end
    return None


def prior_year_download_caption(start: date, end: date, prior_years: int) -> str:
    slices = download_window_slices(start, end, prior_years=max(0, prior_years))
    return f"Will download {format_download_windows(slices)}."


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
    from my_ebird_data import my_ebird_data_path

    export_path = my_ebird_data_path()
    if export_path is not None:
        try:
            export_stat = export_path.stat()
            with export_path.open(newline="", encoding="utf-8") as handle:
                export_rows = max(sum(1 for _ in handle) - 1, 0)
            rows.append(
                {
                    "Cache": export_path.name,
                    "Size": _format_bytes(export_stat.st_size),
                    "Bytes": export_stat.st_size,
                    "Entries": export_rows,
                    "Region birds": "—",
                    "Region %": "—",
                    "Modified": datetime.fromtimestamp(export_stat.st_mtime)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M"),
                    "loader": None,
                    "missing_kind": None,
                    "missing": None,
                }
            )
        except OSError:
            pass
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
    birds = EBirdClient().region_species_birds(region)
    return {"missing": 1, "attempted": 1, "found": len(birds)}


def normalize_gallery_bird(bird: dict) -> dict | None:
    """Stable gallery/compare bird record, or None if it has no identity."""
    if not isinstance(bird, dict):
        return None
    code = str(bird.get("code") or "").strip()
    name = str(bird.get("name") or bird.get("Species") or code).strip()
    if not code and not name:
        return None
    cleaned = {
        "code": code,
        "name": name.split(" (", 1)[0].strip() or name,
        "sciName": str(bird.get("sciName") or "").strip(),
    }
    if any(
        field in bird
        for field in ("is_new_region", "New_region", "is_new", "New")
    ):
        cleaned["is_new_region"] = bool(
            bird.get("is_new_region")
            if "is_new_region" in bird
            else bird.get("New_region")
            if "New_region" in bird
            else bird.get("is_new") or bird.get("New")
        )
        cleaned["is_new"] = cleaned["is_new_region"]
    if "is_new_world" in bird or "New_world" in bird:
        cleaned["is_new_world"] = bool(
            bird.get("is_new_world")
            if "is_new_world" in bird
            else bird.get("New_world")
        )
    if "is_foy_region" in bird or "FoY_region" in bird:
        cleaned["is_foy_region"] = bool(
            bird.get("is_foy_region")
            if "is_foy_region" in bird
            else bird.get("FoY_region")
        )
    if "is_foy_world" in bird or "FoY_world" in bird:
        cleaned["is_foy_world"] = bool(
            bird.get("is_foy_world")
            if "is_foy_world" in bird
            else bird.get("FoY_world")
        )
    if "is_recorded_region" in bird or "Recorded" in bird:
        cleaned["is_recorded_region"] = bool(
            bird.get("is_recorded_region")
            if "is_recorded_region" in bird
            else bird.get("Recorded")
        )
    obs_day = bird.get("obs_day") or bird.get("obsDt")
    if obs_day:
        cleaned["obs_day"] = str(obs_day)
    return cleaned


def open_gallery(
    birds: list[dict],
    *,
    title: str = "Gallery",
    saved_id: str | None = None,
    view_mode: str | None = None,
    source_title: str | None = None,
    sort: str | None = None,
    compare_by_bird: dict | None = None,
    checklist_id: str | None = None,
    start_index: int | None = None,
    notes: str | None = None,
) -> None:
    """Store a bird list in session state and open the gallery view."""
    cleaned: list[dict] = []
    seen: set[str] = set()
    for bird in birds:
        cleaned_bird = normalize_gallery_bird(bird) if isinstance(bird, dict) else None
        if not cleaned_bird:
            continue
        key = cleaned_bird["code"] or normalize_common_name(cleaned_bird["name"])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(cleaned_bird)
    if not cleaned:
        st.warning("No birds available for the gallery.")
        return
    st.session_state.gallery_birds = cleaned
    focus = 0
    if start_index is not None and 0 <= int(start_index) < len(birds):
        source = birds[int(start_index)]
        wanted = normalize_gallery_bird(source) if isinstance(source, dict) else None
        if wanted:
            want_key = wanted["code"] or normalize_common_name(wanted["name"])
            for idx, item in enumerate(cleaned):
                if (item["code"] or normalize_common_name(item["name"])) == want_key:
                    focus = idx
                    break
    st.session_state.gallery_title = title
    st.session_state.gallery_bird_index = focus
    st.session_state.gallery_image_index = 0
    st.session_state.gallery_show_info = gallery_info_visible_default()
    st.session_state.gallery_show_similar = True
    st.session_state.setdefault("gallery_hide_similar_never_seen", True)
    if view_mode in {"summary", "standard"}:
        st.session_state.gallery_view_mode = view_mode
        st.session_state.gallery_view_mode_pending = view_mode
    else:
        st.session_state.gallery_view_mode_pending = "summary"
    if sort in GALLERY_SORT_OPTIONS:
        st.session_state.gallery_sort = sort
        st.session_state.gallery_sort_pref = sort
        st.session_state.gallery_sort_pending = sort
    st.session_state.gallery_compare_by_bird = normalize_compare_by_bird(
        compare_by_bird
    )
    st.session_state.pop("gallery_compare_birds", None)
    st.session_state.pop("gallery_compare_owner_key", None)
    st.session_state.pop("gallery_compare_bird_index", None)
    st.session_state.pop("gallery_compare_image_index", None)
    st.session_state.gallery_list_image_indices = {}
    st.session_state.pop("gallery_image_cache_warmed", None)
    st.session_state.pop("gallery_summary_page", None)
    st.session_state.dashboard_pref = "gallery"
    origin = source_title or title
    if saved_id:
        st.session_state.gallery_saved_id = saved_id
        st.session_state.gallery_saved_dirty = False
        st.session_state.gallery_source_title = origin
        st.session_state.gallery_name = title
        st.session_state.gallery_title = title
        _set_saved_gallery_query(saved_id)
        st.session_state.pop("gallery_checklist_id", None)
        _clear_checklist_gallery_query()
    elif checklist_id:
        st.session_state.pop("gallery_saved_id", None)
        st.session_state.gallery_saved_dirty = False
        st.session_state.gallery_source_title = origin
        st.session_state.gallery_name = title
        st.session_state.gallery_title = title
        st.session_state.gallery_checklist_id = str(checklist_id)
        _clear_saved_gallery_query()
        _set_checklist_gallery_query(str(checklist_id))
    else:
        st.session_state.pop("gallery_saved_id", None)
        st.session_state.gallery_saved_dirty = False
        st.session_state.gallery_source_title = origin
        display_title = title if title and title != "Gallery" else default_gallery_name()
        st.session_state.gallery_name = display_title
        st.session_state.gallery_title = display_title
        st.session_state.pop("gallery_checklist_id", None)
        _clear_saved_gallery_query()
        _clear_checklist_gallery_query()
    if notes is None:
        notes = default_gallery_notes(
            title=str(st.session_state.get("gallery_title") or title),
            source_title=origin,
            species_count=len(cleaned),
            region_code=selected_region_code(),
            checklist_id=str(checklist_id or ""),
        )
    st.session_state.gallery_notes = str(notes)
    _clear_summary_gallery_query()
    st.rerun()


def default_gallery_notes(
    *,
    title: str,
    source_title: str = "",
    species_count: int = 0,
    region_code: str = "",
    checklist_id: str = "",
    extra_lines: list[str] | None = None,
    built_at: datetime | None = None,
) -> str:
    """Origin text stored with a gallery when it is first opened."""
    when = (built_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [f"Built {when}."]
    opened = str(title or "").strip()
    if opened:
        lines.append(f"Opened as: {opened}.")
    source = str(source_title or "").strip()
    if source and source != opened:
        lines.append(f"Source: {source}.")
    if species_count:
        lines.append(f"Species: {species_count}.")
    region = str(region_code or "").strip()
    if region:
        short, full = region_display_names(region, allow_api=False)
        label = full or short or region
        if label != region:
            lines.append(f"Region: {label} ({region}).")
        else:
            lines.append(f"Region: {region}.")
    sub_id = str(checklist_id or "").strip()
    if sub_id:
        lines.append(f"Checklist: {sub_id} (https://ebird.org/checklist/{sub_id}).")
    for line in extra_lines or []:
        text = str(line or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def notes_from_saved_gallery(payload: dict) -> str:
    """Stored notes, or origin text for older saved galleries."""
    saved_notes = payload.get("notes")
    if isinstance(saved_notes, str) and saved_notes.strip():
        return saved_notes
    gallery_id = str(payload.get("id") or "")
    extra = [f"Opened from saved gallery `{gallery_id}`."]
    saved_at = payload.get("saved_at")
    if saved_at:
        extra.append(f"Originally saved {saved_at}.")
    return default_gallery_notes(
        title=str(payload.get("title") or gallery_id),
        source_title=str(payload.get("source_title") or ""),
        species_count=len(payload.get("birds") or []),
        extra_lines=extra,
    )


def checklist_window_note_lines() -> list[str]:
    """How the Checklists screen was configured when a gallery was opened."""
    lines: list[str] = []
    loc_id = str(st.session_state.get("checklists_loc_id") or "").strip()
    loc_name = ""
    for row in st.session_state.get("checklists_hotspots") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("locId") or row.get("locID") or "").strip() == loc_id:
            loc_name = str(row.get("locName") or "").strip()
            break
    if loc_id:
        if loc_name and loc_name != loc_id:
            lines.append(f"Hotspot: {loc_name} ({loc_id}).")
        else:
            lines.append(f"Hotspot: {loc_id}.")
    start = st.session_state.get("checklist_start_date") or st.session_state.get(
        "checklist_start_date_input"
    )
    end = st.session_state.get("checklist_end_date") or st.session_state.get(
        "checklist_end_date_input"
    )
    if start and end:
        lines.append(f"Date window: {start} to {end}.")
    prior = st.session_state.get("checklists_prior_years")
    try:
        prior_n = int(prior)
    except (TypeError, ValueError):
        prior_n = 0
    if prior_n:
        lines.append(f"Prior years: {prior_n}.")
    rows = st.session_state.get("checklist_rows") or []
    if isinstance(rows, list) and rows:
        lines.append(f"Loaded checklists: {len(rows)}.")
    return lines


def default_gallery_name(when: datetime | None = None) -> str:
    """Date/time used when a gallery has no specific name."""
    return (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _saved_gallery_path(gallery_id: str) -> Path:
    return SAVED_GALLERIES_DIR / f"{gallery_id}.json"


def _saved_gallery_git_relpath(gallery_id: str) -> str:
    return f"saved_galleries/{gallery_id}.json"


def _run_git(args: list[str]) -> str | None:
    """Run a git command in the app repo. Returns an error message, or None."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return str(exc)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        return detail or "git command failed"
    return None


def git_repo_available() -> bool:
    error = _run_git(["rev-parse", "--is-inside-work-tree"])
    return error is None


def tracked_saved_gallery_ids() -> set[str]:
    """Gallery ids currently tracked in git (staged or committed)."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--", "saved_galleries"],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if completed.returncode != 0:
        return set()
    ids: set[str] = set()
    for raw in completed.stdout.split("\0"):
        line = raw.strip()
        if not line:
            continue
        stem = Path(line).stem
        if _valid_saved_gallery_id(stem):
            ids.add(stem)
    return ids


def add_saved_gallery_to_git_deploy(gallery_id: str) -> str | None:
    """Force-add a gitignored gallery so it is included in the next commit."""
    if not _valid_saved_gallery_id(gallery_id):
        return "Invalid gallery id."
    if not _saved_gallery_path(gallery_id).is_file():
        return "Gallery file not found."
    return _run_git(["add", "-f", "--", _saved_gallery_git_relpath(gallery_id)])


def remove_saved_gallery_from_git_deploy(gallery_id: str) -> str | None:
    """Untrack a gallery without deleting the local file."""
    if not _valid_saved_gallery_id(gallery_id):
        return "Invalid gallery id."
    return _run_git(
        ["rm", "--cached", "-f", "--", _saved_gallery_git_relpath(gallery_id)]
    )


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


def _clear_checklist_gallery_query() -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    try:
        if CHECKLIST_GALLERY_QUERY in params:
            del params[CHECKLIST_GALLERY_QUERY]
    except Exception:
        try:
            params.pop(CHECKLIST_GALLERY_QUERY, None)
        except Exception:
            pass


def _set_checklist_gallery_query(sub_id: str) -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    try:
        params[CHECKLIST_GALLERY_QUERY] = sub_id
    except Exception:
        pass


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


def saved_gallery_url(gallery_id: str, *, bird_index: int | None = None) -> str:
    query = f"{SAVED_GALLERY_QUERY}={gallery_id}"
    if bird_index is not None:
        query = f"{query}&gallery_open={int(bird_index)}"
    return f"?{query}"


def checklist_gallery_url(sub_id: str, *, bird_index: int | None = None) -> str:
    query = f"{CHECKLIST_GALLERY_QUERY}={sub_id}"
    if bird_index is not None:
        query = f"{query}&gallery_open={int(bird_index)}"
    return f"?{query}"


def summary_gallery_url(*, bird_index: int | None = None) -> str:
    query = f"{SUMMARY_GALLERY_QUERY}=1"
    if bird_index is not None:
        query = f"{query}&gallery_open={int(bird_index)}"
    # Relative so Streamlit keeps the current session instead of a full reload.
    return f"?{query}"


def _clear_summary_gallery_query() -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    try:
        if SUMMARY_GALLERY_QUERY in params:
            del params[SUMMARY_GALLERY_QUERY]
    except Exception:
        try:
            params.pop(SUMMARY_GALLERY_QUERY, None)
        except Exception:
            pass


def expander_gallery_label(header: str, href: str | None) -> str:
    """Expander title; when ``href`` is set the title is a gallery link."""
    if not href:
        return header
    safe = (
        str(header)
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    return f"[{safe}]({href})"


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
    rel = _saved_gallery_git_relpath(gallery_id)
    if gallery_id in tracked_saved_gallery_ids():
        error = _run_git(["rm", "-f", "--", rel])
        if error:
            st.warning(f"Removed locally, but git deploy was not updated: {error}")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    else:
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
    if gallery_id in tracked_saved_gallery_ids():
        add_saved_gallery_to_git_deploy(gallery_id)
    if st.session_state.get("gallery_saved_id") == gallery_id:
        st.session_state.gallery_name = cleaned
        st.session_state.gallery_title = cleaned
    return cleaned


def _on_gallery_notes_change() -> None:
    if st.session_state.get("gallery_saved_id"):
        st.session_state.gallery_saved_dirty = True


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
    if view_mode not in {"summary", "standard"}:
        view_mode = "summary"
    payload = {
        "id": new_id,
        "saved_at": now.isoformat(timespec="seconds"),
        "title": title,
        "source_title": source_title,
        "notes": str(st.session_state.get("gallery_notes") or ""),
        "view_mode": view_mode,
        "sort": current_gallery_sort(),
        "birds": birds,
        "compare_by_bird": persistable_compare_by_bird(),
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
        was_deployed = old_id in tracked_saved_gallery_ids()
        old_path = _saved_gallery_path(old_id)
        if was_deployed:
            _run_git(["rm", "-f", "--", _saved_gallery_git_relpath(old_id)])
        else:
            try:
                if old_path.is_file():
                    old_path.unlink()
            except OSError:
                pass
        if was_deployed:
            add_saved_gallery_to_git_deploy(new_id)
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
        compare_by_bird=payload.get("compare_by_bird")
        if isinstance(payload.get("compare_by_bird"), dict)
        else None,
        notes=notes_from_saved_gallery(payload),
    )


def checklist_row_for_gallery(sub_id: str) -> dict | None:
    """Find a cached/session checklist row that can open a gallery."""
    if not CHECKLIST_SUB_ID_RE.fullmatch(sub_id):
        return None
    for key in ("checklist_rows", "own_checklists_enriched"):
        rows = st.session_state.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("subId") or row.get("subID") or "") == sub_id:
                return row
    names = configured_observer_names()
    for row in load_own_local_checklists(list(names)):
        if str(row.get("subId") or row.get("subID") or "") == sub_id:
            return row
    return None


def maybe_open_checklist_gallery_from_query() -> None:
    """Open a checklist gallery when the URL contains ``?checklist_gallery=``."""
    sub_id = _query_param_raw(CHECKLIST_GALLERY_QUERY)
    if not sub_id or not CHECKLIST_SUB_ID_RE.fullmatch(sub_id):
        return
    if (
        st.session_state.get("gallery_checklist_id") == sub_id
        and st.session_state.get("gallery_birds")
    ):
        return
    row = checklist_row_for_gallery(sub_id)
    if row is None:
        st.session_state.checklist_gallery_missing = sub_id
        _clear_checklist_gallery_query()
        return
    if not (row.get("species_rows") or row.get("_detail")):
        enriched = enrich_own_checklist_page([row], 0, 1)
        if enriched:
            row = enriched[0]
    open_checklist_gallery(row, "all")


def species_summary_gallery_birds(items: list[dict]) -> list[dict]:
    """Gallery bird dicts from a species-summary table."""
    birds: list[dict] = []
    for item in items:
        name = str(item.get("Species") or item.get("name") or "").strip()
        code = str(item.get("code") or "").strip()
        if not name and not code:
            continue
        birds.append(
            {
                "code": code,
                "name": name or code,
                "sciName": item.get("sciName") or "",
                "is_new_region": bool(item.get("New_region") or item.get("is_new_region")),
                "is_new_world": bool(item.get("New_world") or item.get("is_new_world")),
                "is_foy_region": bool(item.get("FoY_region") or item.get("is_foy_region")),
                "is_foy_world": bool(item.get("FoY_world") or item.get("is_foy_world")),
                "is_recorded_region": bool(
                    item.get("Recorded") or item.get("is_recorded_region")
                ),
                "is_new": bool(item.get("New_region") or item.get("is_new_region")),
            }
        )
    return birds


def resolve_summary_gallery_birds(*, scope: str | None = None) -> list[dict]:
    """Birds for the checklists species-summary gallery, rebuilt from loaded rows if needed."""
    stored = st.session_state.get("summary_gallery_birds")
    if isinstance(stored, list) and stored:
        return stored
    rows = st.session_state.get("checklist_rows")
    if not isinstance(rows, list) or not rows:
        return []
    summary = build_species_summary(rows)
    life_scope = scope or current_life_list_scope()
    if life_scope == "all":
        filtered = summary
    else:
        filtered = [
            row for row in summary if summary_is_new_for_scope(row, life_scope)
        ]
    return species_summary_gallery_birds(filtered)


def open_species_summary_gallery(
    birds: list[dict] | None = None,
    *,
    bird_index: int | None = None,
) -> None:
    payload = birds if birds is not None else resolve_summary_gallery_birds()
    if not isinstance(payload, list) or not payload:
        st.warning("No birds available for the gallery.")
        return
    st.session_state.summary_gallery_birds = payload
    start = None
    view = "summary"
    if bird_index is not None and 0 <= int(bird_index) < len(payload):
        start = int(bird_index)
        view = "standard"
    extra = [
        "Opened from the Checklists species summary.",
        *checklist_window_note_lines(),
    ]
    open_gallery(
        payload,
        title="Species summary gallery",
        source_title="Species summary gallery",
        view_mode=view,
        sort=current_gallery_sort(),
        start_index=start,
        notes=default_gallery_notes(
            title="Species summary gallery",
            source_title="Checklists screen",
            species_count=len(payload),
            region_code=selected_region_code(),
            extra_lines=extra,
        ),
    )


def queue_open_summary_gallery() -> None:
    """Mark the checklists species-summary gallery to open on this run."""
    st.session_state["_open_summary_gallery"] = True
    st.session_state.pop("_open_summary_gallery_index", None)


def queue_open_summary_gallery_at(bird_index: int) -> None:
    """Open the species-summary gallery on a specific bird."""
    st.session_state["_open_summary_gallery"] = True
    st.session_state["_open_summary_gallery_index"] = int(bird_index)


def consume_open_summary_gallery() -> None:
    """Open the queued species-summary gallery before screen routing."""
    if not st.session_state.pop("_open_summary_gallery", False):
        return
    bird_index = st.session_state.pop("_open_summary_gallery_index", None)
    try:
        bird_index = int(bird_index) if bird_index is not None else None
    except (TypeError, ValueError):
        bird_index = None
    open_species_summary_gallery(bird_index=bird_index)


def maybe_open_summary_gallery_from_query() -> None:
    """Open the checklists species-summary gallery when ``?summary_gallery=1``.

    The query is handled before the Checklists screen renders, so birds are
    rebuilt from loaded checklist rows when ``summary_gallery_birds`` is empty.
    If rows are not in session yet, the query is left in place for
    ``render_checklists`` to open the gallery after the summary is built.
    """
    if not _query_param_raw(SUMMARY_GALLERY_QUERY):
        return
    if (
        st.session_state.get("dashboard_pref") == "gallery"
        and st.session_state.get("gallery_birds")
        and st.session_state.get("gallery_source_title") == "Species summary gallery"
    ):
        _clear_summary_gallery_query()
        return
    birds = resolve_summary_gallery_birds()
    if not birds:
        return
    open_species_summary_gallery(birds)


GALLERY_SESSION_KEYS = (
    "gallery_birds",
    "gallery_title",
    "gallery_name",
    "gallery_source_title",
    "gallery_notes",
    "gallery_saved_id",
    "gallery_saved_dirty",
    "gallery_checklist_id",
    "gallery_bird_index",
    "gallery_image_index",
    "gallery_show_info",
    "gallery_show_similar",
    "gallery_hide_similar_never_seen",
    "gallery_last_swipe_t",
    "gallery_compare_birds",
    "gallery_compare_by_bird",
    "gallery_compare_owner_key",
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
    "gallery_show_remove",
    "gallery_show_nav_buttons",
    "gallery_chrome_layout",
    "gallery_view_mode_radio",
    "gallery_filter_radio",
)


HOME_SCREEN = "saved"
DASHBOARD_SCREENS = {
    "saved": "Saved galleries",
    "mine": "My checklists",
    "checklists": "Checklists",
    "region": "Region",
    "cache": "Checklist cache",
    "maintenance": "Cache maintenance",
}
REGION_CHIP_SCREENS = frozenset(
    {"checklists", "cache", "maintenance", "gallery"}
)
OWN_CHECKLISTS_PAGE_SIZE = 8


def close_gallery() -> None:
    """Leave the gallery and return to saved galleries."""
    for key in GALLERY_SESSION_KEYS:
        st.session_state.pop(key, None)
    _clear_saved_gallery_query()
    _clear_checklist_gallery_query()
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
            if key == "region" and current != "region":
                st.session_state.dashboard_before_region = current
            go_dashboard(key)


def desktop_nav_panel_open() -> bool:
    """Whether the desktop left nav panel should be shown."""
    if current_ui_layout() != "desktop":
        return False
    return bool(st.session_state.get("nav_panel_open", True))


def render_nav_show_button(*, help: str) -> None:
    """Restore the hidden desktop nav panel."""
    if st.button(
        ":material/menu:",
        help=help,
        type="tertiary",
        key="nav_panel_show",
    ):
        st.session_state.nav_panel_open = True
        st.rerun()


def render_desktop_nav_panel(
    *,
    screen: str,
    saved_id: str = "",
    region_code: str = "",
    birds: list[dict] | None = None,
) -> None:
    """Left sidebar with screen links (and gallery controls on that screen)."""
    if not desktop_nav_panel_open():
        return
    key_prefix = "gallery_nav" if screen == "gallery" else f"dashboard_nav_{screen}"
    with st.sidebar:
        if st.button(
            "Hide menu",
            icon=":material/chevron_left:",
            help="Hide this panel",
            use_container_width=True,
            key="nav_panel_hide",
        ):
            st.session_state.nav_panel_open = False
            st.rerun()
        render_app_nav_buttons(current=screen, key_prefix=key_prefix)
        if screen == "gallery":
            st.divider()
            render_gallery_menu_controls(
                saved_id=saved_id,
                region_code=region_code,
                birds=birds,
            )
        elif screen in {"saved", "mine"}:
            st.divider()
            render_gallery_sort_controls()


def _cached_region_list_name(code: str) -> str | None:
    """Return a region’s common name from the on-disk region-list cache."""
    region = str(code or "").strip()
    if not region:
        return None
    parts = region.split("-")
    if len(parts) == 1:
        rows = load_cached_region_list("country", "world")
    elif len(parts) == 2:
        rows = load_cached_region_list("subnational1", parts[0])
    else:
        rows = load_cached_region_list("subnational2", f"{parts[0]}-{parts[1]}")
    for row in rows:
        if str(row.get("code") or "").strip() == region:
            name = str(row.get("name") or "").strip()
            if name and name != region:
                return name
            return None
    return None


def region_display_names(code: str, *, allow_api: bool = False) -> tuple[str, str]:
    """Return ``(short_name, long_name)`` for an eBird region code."""
    region = str(code or "").strip()
    if not region:
        return ("Select region", "Select region")
    cache = st.session_state.setdefault("_region_display_names", {})
    cached = cache.get(region)
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        return str(cached[0]), str(cached[1])

    leaf = _cached_region_list_name(region)
    if leaf:
        names = [leaf]
        parts = region.split("-")
        if len(parts) >= 2:
            parent = _cached_region_list_name("-".join(parts[:2]))
            if parent and parent not in names:
                names.append(parent)
        country = _cached_region_list_name(parts[0])
        if country and country not in names:
            names.append(country)
        result = (leaf, ", ".join(names))
        cache[region] = result
        return result

    if allow_api and get_api_key():
        try:
            info = EBirdClient().region_info(region)
        except (MissingEbirdApiKey, requests.RequestException, OSError, ValueError):
            info = {}
        detailed = ""
        if isinstance(info, dict):
            detailed = str(info.get("result") or info.get("name") or "").strip()
        if detailed:
            short = detailed.split(",")[0].strip() or detailed
            result = (short, detailed)
            cache[region] = result
            return result
    return (region, region)


def render_region_chip(*, screen: str) -> None:
    """Upper-right control showing the selected region name."""
    code = selected_region_code()
    short, long_name = region_display_names(code, allow_api=True)
    if code and long_name and long_name != code:
        help_text = f"{long_name} · {code}. Open the region screen to change it."
    elif code:
        help_text = f"{code}. Open the region screen to change it."
    else:
        help_text = "Open the region screen to choose an eBird region."
    if st.button(
        long_name or short or "Select region",
        type="tertiary",
        key=f"header_region_{screen}",
        help=help_text,
        use_container_width=True,
    ):
        st.session_state.dashboard_before_region = screen
        go_dashboard("region")


def render_page_header(title: str, *, screen: str) -> None:
    """Title row; desktop uses a hideable left panel, mobile uses a hamburger."""
    nav_help = (
        "Open saved galleries, my checklists, Gallery, Checklists, region, "
        "downloads, or cache maintenance"
    )
    show_chip = screen in REGION_CHIP_SCREENS
    show_menu = current_ui_layout() != "desktop" or not desktop_nav_panel_open()
    if current_ui_layout() == "desktop":
        render_desktop_nav_panel(screen=screen)

    if show_menu and show_chip:
        menu_col, title_col, region_col = st.columns(
            [1, 10, 4], vertical_alignment="center"
        )
    elif show_menu:
        menu_col, title_col = st.columns([1, 16], vertical_alignment="center")
        region_col = None
    elif show_chip:
        title_col, region_col = st.columns([4, 1.4], vertical_alignment="center")
        menu_col = None
    else:
        menu_col = None
        region_col = None
        title_col = None

    if menu_col is not None:
        with menu_col:
            if current_ui_layout() == "desktop":
                render_nav_show_button(help=nav_help)
            else:
                with st.popover(":material/menu:", help=nav_help):
                    render_app_nav_buttons(
                        current=screen, key_prefix=f"dashboard_nav_{screen}"
                    )
                    if screen in {"saved", "mine"}:
                        st.divider()
                        render_gallery_sort_controls()
    if title_col is not None:
        with title_col:
            st.title(title)
    else:
        st.title(title)
    if region_col is not None:
        with region_col:
            render_region_chip(screen=screen)


def list_cards_expand_all() -> bool:
    """Whether saved-gallery / my-checklist cards should start expanded."""
    return bool(st.session_state.get("gallery_list_expand_all"))


def apply_list_expander_state(widget_key: str, *, index: int) -> bool:
    """Set an expander's open state before the widget is instantiated."""
    force = st.session_state.get("gallery_list_expand_force")
    if force is True:
        st.session_state[widget_key] = True
    elif force is False:
        st.session_state[widget_key] = False
    elif widget_key not in st.session_state:
        st.session_state[widget_key] = list_cards_expand_all() or index == 0
    return bool(st.session_state.get(widget_key))


def clear_list_expander_force() -> None:
    st.session_state.pop("gallery_list_expand_force", None)


def render_expand_all_button(*, key: str) -> None:
    """Toggle every saved-gallery / my-checklist card open or closed."""
    expanded = list_cards_expand_all()
    label = "Collapse all" if expanded else "Expand all"
    if st.button(label, key=key, use_container_width=True):
        st.session_state.gallery_list_expand_all = not expanded
        st.session_state.gallery_list_expand_force = not expanded
        st.rerun()


def render_open_gallery_icon_button(
    *,
    key: str,
    disabled: bool = False,
    on_click=None,
) -> bool:
    """Compact photo-library control for opening a gallery beside a card title."""
    return st.button(
        "Open",
        icon=":material/photo_library:",
        type="tertiary",
        key=key,
        help="Open gallery",
        disabled=disabled,
        use_container_width=True,
        on_click=on_click,
    )


def render_list_card_expander(
    header: str,
    *,
    expander_key: str,
    index: int,
    href: str | None = None,
):
    """Expander whose title is a gallery URL when ``href`` is set."""
    expanded = apply_list_expander_state(expander_key, index=index)
    return st.expander(
        expander_gallery_label(header, href),
        expanded=expanded,
        key=expander_key,
        on_change="rerun",
    )


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
        "Open a gallery with the photo-library icon or a photo. Rename and delete inside the card. "
        "Add a gallery to git deploy to include it in the next commit and Streamlit Cloud push; "
        "other galleries stay local."
    )
    expand_col, _ = st.columns([1, 4])
    with expand_col:
        render_expand_all_button(key="saved_galleries_expand_all")
    deploy_ok = git_repo_available()
    deploy_ids = tracked_saved_gallery_ids() if deploy_ok else set()
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
        if gallery_id in deploy_ids:
            header = f"{header} · git deploy"
        expander_key = f"saved_gallery_exp_{gallery_id}"
        with st.container(border=True):
            open_col, name_col = st.columns([1, 16], vertical_alignment="center")
            with open_col:
                if birds and render_open_gallery_icon_button(
                    key=f"open_gallery_icon_saved_{gallery_id}"
                ):
                    open_gallery(
                        birds,
                        title=title,
                        saved_id=gallery_id,
                        view_mode=(
                            item.get("view_mode")
                            if isinstance(item.get("view_mode"), str)
                            else None
                        ),
                        source_title=(
                            item.get("source_title")
                            if isinstance(item.get("source_title"), str)
                            else None
                        ),
                        sort=item.get("sort") if isinstance(item.get("sort"), str) else None,
                        compare_by_bird=(
                            item.get("compare_by_bird")
                            if isinstance(item.get("compare_by_bird"), dict)
                            else None
                        ),
                        notes=notes_from_saved_gallery(item),
                    )
            with name_col:
                expander = render_list_card_expander(
                    header,
                    expander_key=expander_key,
                    index=index,
                )
            with expander:
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
                if deploy_ok:
                    in_deploy = gallery_id in deploy_ids
                    if in_deploy:
                        st.caption(
                            "This gallery is staged or committed for git deploy."
                        )
                        if st.button(
                            "Remove from git deploy",
                            key=f"undeploy_saved_gallery_{gallery_id}",
                            use_container_width=True,
                        ):
                            error = remove_saved_gallery_from_git_deploy(gallery_id)
                            if error:
                                st.error(error)
                            else:
                                st.rerun()
                    elif st.button(
                        "Add to git deploy",
                        key=f"deploy_saved_gallery_{gallery_id}",
                        use_container_width=True,
                    ):
                        error = add_saved_gallery_to_git_deploy(gallery_id)
                        if error:
                            st.error(error)
                        else:
                            st.rerun()
                thumbs = sorted_gallery_birds(birds)
                render_species_thumbnail_table(
                    thumbs,
                    columns=6,
                    width=144,
                    click_hrefs=[url] * len(thumbs) if thumbs else None,
                )
                notes = str(item.get("notes") or "").strip()
                if notes:
                    preview = notes.splitlines()[0]
                    if len(preview) > 140:
                        preview = preview[:137] + "…"
                    st.caption(preview)
    clear_list_expander_force()


def checklist_date_label(row: dict) -> str:
    """Human-readable checklist date/time from a feed or detail row."""
    return " ".join(
        part for part in [row.get("obsDt"), row.get("obsTime")] if part
    ).strip()


def checklist_gallery_birds(row: dict, life_scope: str) -> list[dict]:
    """Gallery bird dicts from an enriched checklist, honoring the life-list filter."""
    species_rows = row.get("species_rows") or []
    if life_scope != "all":
        species_rows = [
            obs for obs in species_rows if obs_is_new_for_scope(obs, life_scope)
        ]
    birds: list[dict] = []
    obs_day = parse_ebird_obs_day(
        str(row.get("isoObsDate") or row.get("obsDt") or "")
    )
    obs_day_text = obs_day.isoformat() if obs_day else ""
    for obs in species_rows:
        is_new_region = bool(
            obs.get("is_new_region") if "is_new_region" in obs else obs.get("is_new")
        )
        birds.append(
            {
                "code": obs.get("code"),
                "name": obs.get("name"),
                "sciName": obs.get("sciName"),
                "is_new_region": is_new_region,
                "is_new_world": bool(obs.get("is_new_world")),
                "is_foy_region": bool(obs.get("is_foy_region")),
                "is_foy_world": bool(obs.get("is_foy_world")),
                "is_recorded_region": bool(obs.get("is_recorded_region")),
                "is_new": is_new_region,
                "obs_day": obs_day_text,
            }
        )
    return birds


def open_checklist_gallery(row: dict, life_scope: str) -> None:
    """Open a gallery for one checklist's (optionally filtered) species."""
    birds = checklist_gallery_birds(row, life_scope)
    if not birds:
        st.warning("No birds available for the gallery.")
        return
    date_label = checklist_date_label(row) or str(row.get("subId") or "checklist")
    location = str(row.get("locName") or row.get("locId") or "").strip()
    title = f"Checklist gallery · {date_label}"
    if location:
        title = f"{title} · {location}"
    sub_id = str(row.get("subId") or row.get("subID") or "")
    extra = [
        "Opened from a single eBird checklist.",
    ]
    observer = str(row.get("userDisplayName") or "").strip()
    if observer:
        extra.append(f"Observer: {observer}.")
    loc_id = str(row.get("locId") or row.get("locID") or "").strip()
    if loc_id:
        extra.append(f"Location ID: {loc_id}.")
    species = row.get("numSpecies")
    if species not in (None, ""):
        extra.append(f"Checklist species count: {species}.")
    extra.extend(checklist_window_note_lines())
    open_gallery(
        birds,
        title=title,
        source_title=location or None,
        sort=current_gallery_sort(),
        checklist_id=sub_id if CHECKLIST_SUB_ID_RE.fullmatch(sub_id) else None,
        notes=default_gallery_notes(
            title=title,
            source_title=location or "Checklist",
            species_count=len(birds),
            region_code=str(row.get("regionCode") or selected_region_code()),
            checklist_id=sub_id if CHECKLIST_SUB_ID_RE.fullmatch(sub_id) else "",
            extra_lines=extra,
        ),
    )


def own_checklists_from_session() -> list[dict]:
    """Own checklists from downloaded JSON and the My eBird data export."""
    names = tuple(configured_observer_names())
    from my_ebird_data import my_ebird_source_signature

    signature = my_ebird_source_signature()
    if (
        st.session_state.get("own_checklists_names") == names
        and st.session_state.get("own_checklists_export_sig") == signature
    ):
        rows = st.session_state.get("own_checklists_rows")
        if isinstance(rows, list):
            return rows
    rows = load_own_local_checklists(list(names))
    st.session_state.own_checklists_rows = rows
    st.session_state.own_checklists_names = names
    st.session_state.own_checklists_export_sig = signature
    st.session_state.pop("own_checklists_enriched", None)
    st.session_state.pop("own_checklists_shown", None)
    return rows


def enrich_own_checklist_page(summaries: list[dict], start: int, end: int) -> list[dict]:
    """Attach species rows to a slice of own-checklist summaries."""
    page = summaries[start:end]
    if not page:
        return []
    by_region: dict[str, list[dict]] = {}
    for row in page:
        region = str(row.get("regionCode") or row.get("_region") or "").strip()
        by_region.setdefault(region, []).append(row)
    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    client = EBirdClient() if get_api_key() else None
    enriched: list[dict] = []
    for region, group in by_region.items():
        region_life = load_life_list(region) if region else None
        enriched.extend(
            enrich_checklists(
                client,
                group,
                region_life,
                world_life,
                allow_api=bool(client),
                region_code=region,
            )
        )
    enriched.sort(
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )
    return enriched


def own_checklists_latest_local_labels(summaries: list[dict]) -> tuple[str, str]:
    """Newest observation date and latest on-disk file time from local own checklists."""
    if not summaries:
        return "", ""
    newest = summaries[0]
    latest = checklist_date_label(newest)
    if not latest:
        day = parse_ebird_obs_day(
            str(newest.get("isoObsDate") or newest.get("obsDt") or "")
        )
        latest = day.strftime("%d %b %Y") if day else ""
    newest_mtime: float | None = None
    for row in summaries:
        path_raw = str(row.get("_path") or "").strip()
        if not path_raw:
            continue
        try:
            mtime = Path(path_raw).stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
    saved = ""
    if newest_mtime is not None:
        saved = (
            datetime.fromtimestamp(newest_mtime)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    return latest, saved


def render_own_checklists_latest_caption(summaries: list[dict]) -> None:
    """Show how current the local own-checklist files are."""
    latest, saved = own_checklists_latest_local_labels(summaries)
    if latest and saved:
        st.caption(f"Latest local checklist: {latest} · file {saved}")
    elif latest:
        st.caption(f"Latest local checklist: {latest}")
    elif saved:
        st.caption(f"Latest local file: {saved}")


def render_own_checklists() -> None:
    """Browse the user's eBird checklists from downloads and My eBird data."""
    render_page_header("My checklists", screen="mine")
    render_life_list_gallery_links()
    st.markdown(f"[Download my data]({DOWNLOAD_MY_DATA_URL}) on eBird")
    from my_ebird_data import my_ebird_data_path

    export_path = my_ebird_data_path()
    if export_path is not None:
        st.caption(
            f"Using `{export_path.name}` for life lists, last seen, and your checklists. "
            "Replace this file after downloading a newer export from eBird."
        )
    names = configured_observer_names()
    summaries: list[dict] = []
    cached = (
        st.session_state.get("own_checklists_names") == tuple(names)
        and isinstance(st.session_state.get("own_checklists_rows"), list)
    )
    if cached:
        summaries = own_checklists_from_session()
    else:
        with st.spinner("Loading your checklists…"):
            summaries = own_checklists_from_session()
    render_own_checklists_latest_caption(summaries)

    if not names and export_path is None:
        st.warning(
            "Add a `MyEBirdData.csv` export from eBird, or set "
            "`EBIRD_USER_DISPLAY_NAME` in `.env` to the public name on your checklists."
        )
        return

    refresh_col, expand_col, _ = st.columns([1, 1, 3])
    with refresh_col:
        if st.button("Refresh", use_container_width=True, key="own_checklists_refresh"):
            st.session_state.pop("own_checklists_rows", None)
            st.session_state.pop("own_checklists_names", None)
            st.session_state.pop("own_checklists_export_sig", None)
            st.session_state.pop("own_checklists_enriched", None)
            st.session_state.pop("own_checklists_shown", None)
            st.rerun()
    with expand_col:
        render_expand_all_button(key="own_checklists_expand_all")

    if not summaries:
        missing = []
        if export_path is None:
            missing.append("a My eBird data CSV in the project folder")
        if names:
            missing.append(
                "downloaded checklists matching "
                + ", ".join(f"`{name}`" for name in names)
            )
        st.info("No checklists found. Add " + " or ".join(missing) + ".")
        return

    shown = int(st.session_state.get("own_checklists_shown") or 0)
    if shown <= 0:
        shown = min(OWN_CHECKLISTS_PAGE_SIZE, len(summaries))
        st.session_state.own_checklists_shown = shown
    shown = min(shown, len(summaries))
    enriched = st.session_state.get("own_checklists_enriched")
    if not isinstance(enriched, list):
        enriched = []
    if len(enriched) < shown:
        with st.spinner("Loading species photos…"):
            enriched = list(enriched) + enrich_own_checklist_page(
                summaries, len(enriched), shown
            )
        st.session_state.own_checklists_enriched = enriched

    source_bits: list[str] = []
    if names:
        source_bits.append(
            "matching " + ", ".join(f"**{name}**" for name in names)
        )
    if export_path is not None:
        source_bits.append(f"from `{export_path.name}`")
    source_text = (" · ".join(source_bits) + " · ") if source_bits else ""
    st.caption(
        f"{len(summaries)} checklist"
        f"{'' if len(summaries) == 1 else 's'} {source_text}"
        f"showing {len(enriched)} · newest first. Open a gallery with the photo-library icon or a photo."
    )

    for index, row in enumerate(enriched):
        sub_id = str(row.get("subId") or row.get("subID") or index)
        date_label = checklist_date_label(row) or sub_id
        location = str(row.get("locName") or row.get("locId") or "").strip()
        species = row.get("numSpecies", len(row.get("species_rows") or []))
        region_new = row.get("new_count_region", row.get("new_count"))
        world_new = row.get("new_count_world")
        new_bits: list[str] = []
        if region_new:
            new_bits.append(f"{region_new} new to region")
        if world_new:
            new_bits.append(f"{world_new} new to world")
        header = f"{date_label} · {species} species"
        if location:
            header = f"{date_label} · {location} · {species} species"
        if new_bits:
            header = f"{header} · {' · '.join(new_bits)}"
        gallery_birds = checklist_gallery_birds(row, "all")
        expander_key = f"own_checklist_exp_{sub_id}"
        href = (
            checklist_gallery_url(sub_id)
            if gallery_birds and CHECKLIST_SUB_ID_RE.fullmatch(sub_id)
            else None
        )
        with st.container(border=True):
            open_col, name_col = st.columns([1, 16], vertical_alignment="center")
            with open_col:
                if gallery_birds and render_open_gallery_icon_button(
                    key=f"open_gallery_icon_own_{sub_id}"
                ):
                    open_checklist_gallery(row, "all")
            with name_col:
                expander = render_list_card_expander(
                    header,
                    expander_key=expander_key,
                    index=index,
                )
            with expander:
                if gallery_birds:
                    thumbs = sorted_gallery_birds(gallery_birds)
                    render_species_thumbnail_table(
                        thumbs,
                        columns=6,
                        width=144,
                        click_hrefs=[href] * len(thumbs) if href else None,
                    )
                else:
                    st.caption("No species on this downloaded checklist.")
                checklist_url = f"https://ebird.org/checklist/{sub_id}"
                st.markdown(f"[eBird checklist]({checklist_url})")

    clear_list_expander_force()

    if shown < len(summaries):
        more = min(OWN_CHECKLISTS_PAGE_SIZE, len(summaries) - shown)
        if st.button(f"Show more ({more} more)", key="own_checklists_more"):
            st.session_state.own_checklists_shown = shown + more
            st.rerun()


LIFE_LIST_SCOPES = frozenset(
    {"region", "world", "foy_world", "foy_region", "recorded"}
)


def _scopes_from_stored(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value and value != "all":
        return [value]
    return []


def _sync_life_list_scope_pref() -> None:
    value = st.session_state.get("life_list_scopes")
    if isinstance(value, list):
        st.session_state.life_list_scope_pref = [
            item for item in value if item in LIFE_LIST_SCOPES
        ]


def current_life_list_scopes(*, region_code: str | None = None) -> list[str]:
    """Selected gallery novelty filters; empty means show every bird."""
    options = set(life_list_filter_options(region_code=region_code))
    for key in ("life_list_scopes", "life_list_scope_pref", "life_list_scope"):
        cleaned = [
            item
            for item in _scopes_from_stored(st.session_state.get(key))
            if item in options
        ]
        if key == "life_list_scope" and not cleaned:
            continue
        if cleaned or key != "life_list_scope":
            return cleaned
    return []


def current_life_list_scope() -> str:
    """Single-scope fallback used by checklist summary gallery opens."""
    scopes = current_life_list_scopes()
    if len(scopes) == 1:
        return scopes[0]
    return "all"


def selected_region_code() -> str:
    return str(
        st.session_state.get("checklists_region")
        or os.environ.get("EBIRD_HOME_REGION", "US-FL-099")
        or ""
    ).strip()


def region_life_list_present(region_code: str | None = None) -> bool:
    code = (region_code or selected_region_code()).strip()
    return bool(code) and load_life_list(code) is not None


def life_list_filter_options(
    *,
    region_code: str | None = None,
    birds: list[dict] | None = None,
) -> list[str]:
    options: list[str] = []
    if region_life_list_present(region_code):
        options.append("region")
    if (region_code or selected_region_code()).strip():
        options.append("recorded")
    options.append("world")
    options.append("foy_world")
    if region_life_list_present(region_code):
        options.append("foy_region")
    if birds is None:
        return options
    present = set()
    for bird in birds:
        present.update(gallery_bird_filter_tags(bird))
    return [item for item in options if item in present]


def life_list_scope_label(value: str, *, region_code: str = "") -> str:
    return {
        "region": f"New to region ({region_code or 'region'})",
        "recorded": "Recorded (region this year)",
        "world": "New to world",
        "foy_world": "Missing FoY world",
        "foy_region": "Missing FoY region",
    }.get(value, value)


def coerce_life_list_scope_widget(
    *,
    region_code: str | None = None,
    birds: list[dict] | None = None,
) -> None:
    """Keep the multiselect in sync with options present in this gallery."""
    options = life_list_filter_options(region_code=region_code, birds=birds)
    current = _scopes_from_stored(st.session_state.get("life_list_scopes"))
    if not current:
        current = _scopes_from_stored(st.session_state.get("life_list_scope_pref"))
    if not current:
        current = _scopes_from_stored(st.session_state.get("life_list_scope"))
    cleaned = [item for item in current if item in options]
    st.session_state.life_list_scopes = cleaned
    st.session_state.life_list_scope_pref = cleaned


def current_gallery_view_mode() -> str:
    value = st.session_state.get("gallery_view_mode")
    if value == "list":
        return "summary"
    if value in {"summary", "standard"}:
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


def render_gallery_menu_controls(
    *,
    saved_id: str,
    region_code: str,
    birds: list[dict] | None = None,
) -> None:
    """Save, filter, view, sort, and legend controls for the gallery menu."""
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
    coerce_life_list_scope_widget(region_code=region_code, birds=birds)
    filter_options = life_list_filter_options(region_code=region_code, birds=birds)
    if filter_options:
        st.multiselect(
            "Filter",
            options=filter_options,
            format_func=lambda value: life_list_scope_label(
                value, region_code=region_code
            ),
            key="life_list_scopes",
            on_change=_sync_life_list_scope_pref,
            help="Leave empty to show every bird. Select one or more labels to keep birds that match any of them. Recorded means seen in this region this calendar year.",
        )
        st.session_state.life_list_scope_pref = list(
            st.session_state.get("life_list_scopes") or []
        )
    else:
        st.caption("No filter labels in this gallery.")
    st.divider()
    gallery_mode = current_gallery_view_mode()
    pending_mode = st.session_state.pop("gallery_view_mode_pending", None)
    if pending_mode == "list":
        pending_mode = "summary"
    if pending_mode in {"summary", "standard"}:
        st.session_state.gallery_view_mode_radio = pending_mode
        gallery_mode = pending_mode
    elif st.session_state.get("gallery_view_mode_radio") == "list":
        st.session_state.gallery_view_mode_radio = gallery_mode
    elif "gallery_view_mode_radio" not in st.session_state:
        st.session_state.gallery_view_mode_radio = gallery_mode
    st.radio(
        "Gallery view",
        options=["summary", "standard"],
        format_func=lambda value: {
            "summary": "Summary",
            "standard": "Standard",
        }[value],
        key="gallery_view_mode_radio",
        help="Summary is the thumbnail grid. Standard is one bird at a time. Tap a summary photo for Standard view.",
    )
    st.session_state.gallery_view_mode = st.session_state.gallery_view_mode_radio
    st.divider()
    render_gallery_sort_controls()
    st.divider()
    st.checkbox(
        "Show legends",
        key="gallery_show_legends",
        help="Also shows the Image 1/200 counter.",
    )
    st.checkbox(
        "Show remove from gallery",
        key="gallery_show_remove",
        help="Shows the × buttons that remove a bird from this gallery.",
    )
    st.checkbox(
        "Show bird and photo buttons",
        key="gallery_show_nav_buttons",
        help="◀ ▶ change birds and ← → change photos. When off, swipe the image instead.",
    )
    st.checkbox(
        "Show bird info",
        key="gallery_show_info_default",
        on_change=_sync_gallery_info_default,
        help="When on, Standard view opens the About this bird panel. Tap the bird name to hide or show it.",
    )


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
        elif gallery_bird_is_foy_world(bird):
            rank = 2
        elif gallery_bird_is_foy_region(bird):
            rank = 3
        elif gallery_bird_is_recorded(bird):
            rank = 4
        else:
            rank = 5
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


def gallery_chrome_visible_default() -> bool:
    """Legends and motion buttons default on for desktop, off for mobile."""
    return current_ui_layout() != "mobile"


def apply_gallery_chrome_defaults() -> None:
    """Apply layout-based defaults for legends, remove, and bird/photo buttons.

    Must run before those checkboxes are instantiated. Switching desktop/mobile
    reapplies that layout's defaults; a user toggle is kept until the layout
    changes or the gallery is closed.
    """
    st.session_state.setdefault("gallery_show_info_default", False)
    show = gallery_chrome_visible_default()
    layout = current_ui_layout()
    if st.session_state.get("gallery_chrome_layout") != layout:
        st.session_state.gallery_show_legends = show
        st.session_state.gallery_show_remove = show
        st.session_state.gallery_show_nav_buttons = show
        st.session_state.gallery_chrome_layout = layout
        return
    st.session_state.setdefault("gallery_show_legends", show)
    st.session_state.setdefault("gallery_show_remove", show)
    st.session_state.setdefault("gallery_show_nav_buttons", show)


def gallery_info_visible_default() -> bool:
    """Whether Standard view should show About this bird until the name is tapped."""
    return bool(st.session_state.get("gallery_show_info_default", False))


def _sync_gallery_info_default() -> None:
    st.session_state.gallery_show_info = gallery_info_visible_default()


def reset_gallery_info_to_default() -> None:
    st.session_state.gallery_show_info = gallery_info_visible_default()


def gallery_legends_visible() -> bool:
    """Whether photo counters and other legends are shown."""
    if "gallery_show_legends" not in st.session_state:
        return gallery_chrome_visible_default()
    return bool(st.session_state.gallery_show_legends)


def gallery_remove_visible() -> bool:
    """Whether × buttons that remove a bird from this gallery are shown."""
    if "gallery_show_remove" not in st.session_state:
        return gallery_chrome_visible_default()
    return bool(st.session_state.gallery_show_remove)


def gallery_nav_buttons_visible() -> bool:
    """Whether ◀▶ / ←→ bird and photo buttons are shown."""
    if "gallery_show_nav_buttons" not in st.session_state:
        return gallery_chrome_visible_default()
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
    st.session_state.gallery_view_mode = "standard"
    st.session_state.gallery_view_mode_pending = "standard"
    st.session_state.gallery_show_info = gallery_info_visible_default()
    st.session_state.gallery_show_view_picker = False
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
        st.session_state.gallery_show_info = gallery_info_visible_default()
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
        st.session_state.gallery_show_info = gallery_info_visible_default()
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


def gallery_bird_is_foy_region(bird: dict) -> bool:
    return bool(bird.get("is_foy_region") or bird.get("FoY_region"))


def gallery_bird_is_foy_world(bird: dict) -> bool:
    return bool(bird.get("is_foy_world") or bird.get("FoY_world"))


def gallery_bird_is_recorded(bird: dict) -> bool:
    return bool(bird.get("is_recorded_region") or bird.get("Recorded"))


def gallery_bird_filter_tags(bird: dict) -> set[str]:
    """Novelty filter tags that apply to this bird."""
    tags: set[str] = set()
    if gallery_bird_is_new_world(bird):
        tags.add("world")
    if gallery_bird_is_new_region(bird):
        tags.add("region")
    if gallery_bird_is_recorded(bird):
        tags.add("recorded")
    if gallery_bird_is_foy_world(bird):
        tags.add("foy_world")
    if gallery_bird_is_foy_region(bird):
        tags.add("foy_region")
    return tags


def gallery_bird_matches_scope(bird: dict, scope: str) -> bool:
    if scope == "world":
        return gallery_bird_is_new_world(bird)
    if scope == "region":
        return gallery_bird_is_new_region(bird)
    if scope == "foy_world":
        return gallery_bird_is_foy_world(bird)
    if scope == "foy_region":
        return gallery_bird_is_foy_region(bird)
    if scope == "recorded":
        return gallery_bird_is_recorded(bird)
    return True


def gallery_bird_matches_scopes(bird: dict, scopes: list[str]) -> bool:
    if not scopes:
        return True
    tags = gallery_bird_filter_tags(bird)
    return any(scope in tags for scope in scopes)


def gallery_filter_count_lines(
    birds: list[dict],
    *,
    region_code: str = "",
) -> list[str]:
    """Per-option counts and pairwise overlaps for the gallery."""
    options = life_list_filter_options(region_code=region_code, birds=birds)
    if not options:
        return []
    counts = {option: 0 for option in options}
    pair_counts: dict[tuple[str, str], int] = {}
    multi = 0
    for bird in birds:
        tags = [option for option in options if option in gallery_bird_filter_tags(bird)]
        for option in tags:
            counts[option] += 1
        if len(tags) >= 2:
            multi += 1
            for left_idx, left in enumerate(tags):
                for right in tags[left_idx + 1 :]:
                    key = (left, right)
                    pair_counts[key] = pair_counts.get(key, 0) + 1
    lines = [
        f"{life_list_scope_label(option, region_code=region_code)}: **{counts[option]}**"
        for option in options
        if counts[option]
    ]
    for (left, right), total in pair_counts.items():
        if total:
            lines.append(
                "Overlap "
                f"{life_list_scope_label(left, region_code=region_code)} ∩ "
                f"{life_list_scope_label(right, region_code=region_code)}: **{total}**"
            )
    if multi:
        lines.append(f"**{multi}** bird{'s' if multi != 1 else ''} match more than one")
    return lines


def gallery_legend_frame(bird: dict) -> tuple[str, str]:
    """Legend color and line style for a bird.

    Priority: new world (teal solid), new region (amber solid), missing FoY
    world (teal dashed), missing FoY region (amber dashed). When both FoY
    flags are set, world wins.
    """
    if gallery_bird_is_new_world(bird):
        return FRAME_COLOR_WORLD, "solid"
    if gallery_bird_is_new_region(bird):
        return FRAME_COLOR_REGION, "solid"
    if gallery_bird_is_foy_world(bird):
        return FRAME_COLOR_WORLD, "dashed"
    if gallery_bird_is_foy_region(bird):
        return FRAME_COLOR_REGION, "dashed"
    return FRAME_COLOR_SEEN, "solid"


def gallery_frame_color(bird: dict) -> str:
    """Border color for the gallery image based on life-list novelty."""
    return gallery_legend_frame(bird)[0]


def gallery_frame_style(bird: dict) -> str:
    """Solid border for new birds; dashed for missing-FoY-only."""
    return gallery_legend_frame(bird)[1]


def gallery_frame_outline_css(bird: dict, *, width: int = 3) -> str:
    color = gallery_frame_color(bird)
    if color == FRAME_COLOR_SEEN:
        return ""
    return (
        f"outline:{width}px {gallery_frame_style(bird)} {color};"
        f"outline-offset:-{width}px;"
    )


def _legend_swatch(color: str, style: str) -> str:
    return (
        "<span style='display:inline-block;width:0.72em;height:0.72em;"
        f"box-sizing:border-box;border:2px {style} {color};"
        "vertical-align:-0.1em;margin:0 0.12em 0 0.05em'></span>"
    )


def novelty_legend_html() -> str:
    return (
        f"{_legend_swatch(FRAME_COLOR_WORLD, 'solid')} new to world · "
        f"{_legend_swatch(FRAME_COLOR_REGION, 'solid')} new to region · "
        f"{_legend_swatch(FRAME_COLOR_WORLD, 'dashed')} missing FoY world · "
        f"{_legend_swatch(FRAME_COLOR_REGION, 'dashed')} missing FoY region · "
        f"{_legend_swatch(FRAME_COLOR_SEEN, 'solid')} already counted"
    )


def annotate_gallery_birds_with_life_lists(birds: list[dict]) -> list[dict]:
    """Fill region/world-new and missing-FoY flags from the current life lists."""
    region_code = st.session_state.get("checklists_region") or os.environ.get(
        "EBIRD_HOME_REGION", "US-FL-099"
    )
    region_life = load_life_list(region_code) if region_code else None
    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    this_year = date.today().year
    annotated: list[dict] = []
    for bird in birds:
        item = dict(bird)
        taxon = {
            "comName": item.get("name") or "",
            "sciName": item.get("sciName") or "",
            "category": "species",
        }
        region_flag = is_new_to_region_life_list(taxon, region_life, world_life)
        world_flag = is_new_to_life_list(taxon, world_life)
        item["is_new_region"] = bool(region_flag) if region_flag is not None else False
        item["is_new_world"] = bool(world_flag) if world_flag is not None else False
        item["is_new"] = bool(item.get("is_new_region"))
        obs_day = parse_ebird_obs_day(
            str(item.get("obs_day") or item.get("obsDt") or "")
        )
        item["is_foy_region"] = is_missing_first_of_year(
            taxon,
            year=this_year,
            life=region_life,
            obs_day=obs_day,
        )
        item["is_foy_world"] = is_missing_first_of_year(
            taxon,
            year=this_year,
            life=world_life,
            obs_day=obs_day,
        )
        item["is_recorded_region"] = is_recorded_in_region_this_year(
            taxon,
            year=this_year,
            region_life=region_life,
            obs_day=obs_day,
            obs_is_in_region=bool(region_code),
        )
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
    """Whether a bird is already on the current gallery bird's compare list."""
    key = gallery_bird_key(bird)
    return any(gallery_bird_key(item) == key for item in current_compare_birds())


def current_gallery_bird() -> dict | None:
    birds = st.session_state.get("gallery_birds") or []
    try:
        index = int(st.session_state.get("gallery_bird_index", 0))
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(birds) and isinstance(birds[index], dict):
        return birds[index]
    return None


def compare_by_bird_map() -> dict[str, list[dict]]:
    raw = st.session_state.get("gallery_compare_by_bird")
    if not isinstance(raw, dict):
        raw = {}
        st.session_state.gallery_compare_by_bird = raw
    return raw


def normalize_compare_by_bird(raw: object) -> dict[str, list[dict]]:
    """Clean a persisted per-bird compare map."""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[dict]] = {}
    for owner_key, birds in raw.items():
        owner = str(owner_key or "").strip()
        if not owner or not isinstance(birds, list):
            continue
        seen: set[str] = set()
        items: list[dict] = []
        for bird in birds:
            item = normalize_gallery_bird(bird) if isinstance(bird, dict) else None
            if not item:
                continue
            identity = gallery_bird_key(item)
            if identity in seen:
                continue
            seen.add(identity)
            items.append(item)
        if items:
            cleaned[owner] = items
    return cleaned


def persistable_compare_by_bird() -> dict[str, list[dict]]:
    """Compare lists keyed by gallery bird, omitting empty and orphaned entries."""
    mapping = normalize_compare_by_bird(compare_by_bird_map())
    birds = st.session_state.get("gallery_birds") or []
    allowed = {
        gallery_bird_key(bird) for bird in birds if isinstance(bird, dict)
    }
    return {key: items for key, items in mapping.items() if key in allowed}


def current_compare_birds() -> list[dict]:
    bird = current_gallery_bird()
    if not bird:
        return []
    return list(compare_by_bird_map().get(gallery_bird_key(bird)) or [])


def set_current_compare_birds(birds: list[dict], *, dirty: bool = True) -> None:
    owner = current_gallery_bird()
    if not owner:
        st.session_state.gallery_compare_birds = list(birds)
        return
    mapping = compare_by_bird_map()
    owner_key = gallery_bird_key(owner)
    cleaned: list[dict] = []
    seen: set[str] = set()
    for bird in birds:
        item = normalize_gallery_bird(bird) if isinstance(bird, dict) else None
        if not item:
            continue
        identity = gallery_bird_key(item)
        if identity in seen:
            continue
        seen.add(identity)
        cleaned.append(item)
    if cleaned:
        mapping[owner_key] = cleaned
    else:
        mapping.pop(owner_key, None)
    st.session_state.gallery_compare_by_bird = mapping
    st.session_state.gallery_compare_birds = cleaned
    st.session_state.gallery_compare_owner_key = owner_key
    if dirty and st.session_state.get("gallery_saved_id"):
        st.session_state.gallery_saved_dirty = True


def sync_compare_list_for_current_bird() -> None:
    """Load the compare list that belongs to the bird currently on screen."""
    owner = current_gallery_bird()
    owner_key = gallery_bird_key(owner) if owner else ""
    stored = current_compare_birds()
    last_owner = str(st.session_state.get("gallery_compare_owner_key") or "")
    st.session_state.gallery_compare_birds = stored
    if owner_key != last_owner:
        st.session_state.gallery_compare_owner_key = owner_key
        st.session_state.gallery_compare_bird_index = 0
        st.session_state.gallery_compare_image_index = 0


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
    if "is_foy_region" in item:
        bird["is_foy_region"] = bool(item.get("is_foy_region"))
    if "is_foy_world" in item:
        bird["is_foy_world"] = bool(item.get("is_foy_world"))
    if "is_recorded_region" in item:
        bird["is_recorded_region"] = bool(item.get("is_recorded_region"))
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
        if item.get("is_new_world"):
            counts["new_world"] += 1
        else:
            counts["on_world_life"] += 1
        if item.get("is_new_region"):
            counts["new_region"] += 1
        elif not item.get("is_new_world"):
            counts["on_region_life"] += 1
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


def cached_sighting_day_location(row: dict) -> tuple[str, str]:
    """Date and location labels from a cached checklist sighting row."""
    day = str(row.get("obsDay") or "")[:10]
    if not day:
        parsed = parse_ebird_obs_day(row.get("obsDt") or "")
        day = parsed.isoformat() if parsed else str(row.get("obsDt") or "").strip()
    loc = str(row.get("locName") or row.get("locId") or "Unknown location").strip()
    return day, loc


def render_cached_sighting_lines(
    rows: list[dict],
    *,
    highlight_region: str | None = None,
) -> None:
    """Render cached sighting rows; highlight those in ``highlight_region``."""
    wanted = str(highlight_region or "").strip()
    blocks: list[str] = []
    for row in rows:
        day, loc = cached_sighting_day_location(row)
        label = f"{day} · {loc}" if day else loc
        same_region = bool(
            wanted and str(row.get("regionCode") or "").strip() == wanted
        )
        if same_region:
            blocks.append(
                "<div style='background:#ccfbf1;border-radius:0.35rem;"
                "padding:0.2rem 0.45rem;margin:0.15rem 0;'>"
                f"{html.escape(label)}</div>"
            )
        else:
            blocks.append(
                "<div style='padding:0.2rem 0.45rem;margin:0.15rem 0;'>"
                f"{html.escape(label)}</div>"
            )
    st.markdown("".join(blocks), unsafe_allow_html=True)


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
        region_flag = is_new_to_region_life_list(taxon, region_life, world_life)
        world_flag = is_new_to_life_list(taxon, world_life)
        row["is_new_region"] = bool(region_flag) if region_flag is not None else False
        row["is_new_world"] = bool(world_flag) if world_flag is not None else False
        row["is_new"] = row["is_new_region"]
        obs_day = parse_ebird_obs_day(str((observation or {}).get("obsDt") or ""))
        row["is_foy_region"] = is_missing_first_of_year(
            taxon,
            year=date.today().year,
            life=region_life,
            obs_day=obs_day,
        )
        row["is_foy_world"] = is_missing_first_of_year(
            taxon,
            year=date.today().year,
            life=world_life,
            obs_day=obs_day,
        )
        row["is_recorded_region"] = is_recorded_in_region_this_year(
            taxon,
            year=date.today().year,
            region_life=region_life,
            obs_day=obs_day,
            obs_is_in_region=True,
        )
        labels = novelty_labels(
            is_new_world=row["is_new_world"],
            is_new_region=row["is_new_region"],
            is_foy_world=row["is_foy_world"],
            is_foy_region=row["is_foy_region"],
        )
        novelty_label = ", ".join(labels) if labels else "already counted"
        row["novelty_label"] = novelty_label
        row["frame_color"] = gallery_frame_color(row)
        row["frame_style"] = gallery_frame_style(row)

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
    """Add a bird to the current gallery bird's comparison list.

    Returns True when the bird was newly added.
    """
    compare_birds = current_compare_birds()
    key = gallery_bird_key(bird)
    if any(gallery_bird_key(item) == key for item in compare_birds):
        return False
    item = normalize_gallery_bird(bird)
    if not item:
        return False
    item["is_new_region"] = gallery_bird_is_new_region(bird)
    item["is_new_world"] = gallery_bird_is_new_world(bird)
    item["is_foy_region"] = gallery_bird_is_foy_region(bird)
    item["is_foy_world"] = gallery_bird_is_foy_world(bird)
    item["is_recorded_region"] = gallery_bird_is_recorded(bird)
    item["is_new"] = item["is_new_region"]
    compare_birds.append(item)
    set_current_compare_birds(compare_birds)
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
    """Remove a bird from the current gallery bird's comparison list."""
    key = gallery_bird_key(bird)
    compare_birds = [
        item
        for item in current_compare_birds()
        if gallery_bird_key(item) != key
    ]
    set_current_compare_birds(compare_birds)
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
    removed_key = gallery_bird_key(removed)
    mapping = compare_by_bird_map()
    mapping.pop(removed_key, None)
    for owner, items in list(mapping.items()):
        kept = [item for item in items if gallery_bird_key(item) != removed_key]
        if kept:
            mapping[owner] = kept
        else:
            mapping.pop(owner, None)
    st.session_state.gallery_compare_by_bird = mapping
    if not birds:
        close_gallery()
        return
    st.session_state.gallery_birds = birds
    current = int(st.session_state.get("gallery_bird_index", 0))
    if current == bird_index:
        st.session_state.gallery_bird_index = min(bird_index, len(birds) - 1)
        st.session_state.gallery_image_index = 0
        st.session_state.gallery_show_info = gallery_info_visible_default()
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
    """Remove every compare bird for the current gallery bird."""
    set_current_compare_birds([])
    st.session_state.pop("gallery_compare_bird_index", None)
    st.session_state.pop("gallery_compare_image_index", None)


def render_compare_gallery() -> None:
    """Render comparison birds beneath the main gallery bird."""
    sync_compare_list_for_current_bird()
    compare_birds = current_compare_birds()
    if not compare_birds:
        return

    compare_birds = annotate_gallery_birds_with_life_lists(compare_birds)
    set_current_compare_birds(compare_birds, dirty=False)

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
        compare_bird = compare_birds[compare_index]
        compare_frame = gallery_frame_color(compare_bird)
        swipe = swipe_image(
            photo["image_url"],
            height=420,
            frame_color=compare_frame,
            frame_style=gallery_frame_style(compare_bird),
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
        show_image_pos = gallery_legends_visible()
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
    coerce_life_list_scope_widget(
        region_code=str(region_code or ""),
        birds=birds,
    )

    if st.session_state.get("gallery_view_mode") == "list":
        st.session_state.gallery_view_mode = "summary"
    pending_mode = st.session_state.get("gallery_view_mode_pending")
    if pending_mode == "list":
        pending_mode = "summary"
    if pending_mode in {"summary", "standard"}:
        st.session_state.gallery_view_mode = pending_mode
    if "gallery_view_mode" not in st.session_state:
        st.session_state.gallery_view_mode = "summary"

    open_idx = consume_gallery_open_query()
    if open_idx is not None and 0 <= open_idx < len(birds):
        open_gallery_standard_for_bird(open_idx)

    apply_gallery_chrome_defaults()

    saved_id = str(st.session_state.get("gallery_saved_id") or "").strip()
    gallery_menu_help = "Navigation, save, filter, view, and legends"

    def _gallery_name_input() -> None:
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

    if current_ui_layout() == "desktop":
        render_desktop_nav_panel(
            screen="gallery",
            saved_id=saved_id,
            region_code=str(region_code or ""),
            birds=birds,
        )
        show_menu = not desktop_nav_panel_open()
        if show_menu:
            menu_col, title_col, region_col = st.columns(
                [1, 10, 4], vertical_alignment="center"
            )
            with menu_col:
                render_nav_show_button(help=gallery_menu_help)
            with title_col:
                _gallery_name_input()
            with region_col:
                render_region_chip(screen="gallery")
        else:
            title_col, region_col = st.columns([4, 1.4], vertical_alignment="center")
            with title_col:
                _gallery_name_input()
            with region_col:
                render_region_chip(screen="gallery")
    else:
        menu_col, title_col, region_col = st.columns(
            [1, 10, 4], vertical_alignment="center"
        )
        with menu_col:
            with st.popover(":material/menu:", help=gallery_menu_help):
                render_app_nav_buttons(current="gallery", key_prefix="gallery_nav")
                st.divider()
                render_gallery_menu_controls(
                    saved_id=saved_id,
                    region_code=str(region_code or ""),
                    birds=birds,
                )
        with title_col:
            _gallery_name_input()
        with region_col:
            render_region_chip(screen="gallery")
    st.session_state.gallery_title = (
        str(st.session_state.get("gallery_name") or "").strip()
        or default_gallery_name()
    )
    if "gallery_notes" not in st.session_state:
        st.session_state.gallery_notes = default_gallery_notes(
            title=str(st.session_state.gallery_title),
            source_title=str(st.session_state.get("gallery_source_title") or ""),
            species_count=len(birds),
            region_code=str(region_code or ""),
            checklist_id=str(st.session_state.get("gallery_checklist_id") or ""),
        )
    with st.expander("Notes", expanded=False):
        st.text_area(
            "Notes",
            key="gallery_notes",
            height=140,
            label_visibility="collapsed",
            help="Defaults to how this gallery was built. Saved with the gallery.",
            on_change=_on_gallery_notes_change,
        )

    gallery_scopes = current_life_list_scopes(region_code=str(region_code or ""))
    gallery_mode = current_gallery_view_mode()
    show_legends = gallery_legends_visible()

    if show_legends and gallery_mode != "summary":
        st.markdown(
            f"Frame colors · {novelty_legend_html()}",
            unsafe_allow_html=True,
        )

    visible_indices = sort_gallery_visible_indices(
        birds,
        [
            idx
            for idx, item in enumerate(birds)
            if gallery_bird_matches_scopes(item, gallery_scopes)
        ],
    )
    st.session_state.gallery_visible_indices = visible_indices
    if not visible_indices:
        st.info(
            "No birds match this filter in the current gallery."
            if gallery_scopes
            else "No birds available for the gallery."
        )
        return

    if gallery_mode == "summary":
        render_gallery_summary(
            birds,
            visible_indices,
            region_code=str(region_code or ""),
        )
        return

    render_gallery_standard(birds, visible_indices, gallery_scopes)


def render_gallery_summary(
    birds: list[dict],
    visible_indices: list[int],
    *,
    region_code: str = "",
) -> None:
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
            outline = gallery_frame_outline_css(bird)
            if photo and photo.get("image_url"):
                src = html.escape(str(photo["image_url"]), quote=True)
                inner = (
                    f'<img src="{src}" alt="{alt}" '
                    f'style="width:{width}px;height:{width}px;'
                    f'object-fit:cover;display:block;margin:0;padding:0;border:0;{outline}"/>'
                )
            else:
                label = html.escape((name[:10] or "—"), quote=False)
                inner = (
                    f'<div style="width:{width}px;height:{width}px;'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'font-size:11px;color:#64748b;background:#f1f5f9;'
                    f'margin:0;padding:0;{outline}">{label}</div>'
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
            if gallery_remove_visible() and st.button(
                "×",
                key=f"gallery_summary_remove_{bird_index}",
                help=f"Remove {name} from this gallery",
                type="tertiary",
            ):
                remove_gallery_bird(bird_index)
    shown = len(visible_indices)
    total = len(birds)
    shown_label = (
        f"{shown} of {total} species"
        if shown != total
        else f"{shown} species"
    )
    caption_bits = [shown_label]
    if gallery_legends_visible():
        caption_bits.append("tap a photo for Standard view")
    if gallery_remove_visible():
        caption_bits.append("× removes")
    st.caption(" · ".join(caption_bits))
    count_lines = gallery_filter_count_lines(birds, region_code=region_code)
    if count_lines:
        st.markdown("  \n".join(count_lines))


def render_gallery_standard(
    birds: list[dict],
    visible_indices: list[int],
    gallery_scopes: list[str],
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
                st.session_state.gallery_show_info = gallery_info_visible_default()
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
                st.session_state.gallery_show_info = gallery_info_visible_default()
                st.rerun()
    else:
        nav_name = st.container()
    with nav_name:
        label = common
        extra = novelty_labels(
            is_new_world=gallery_bird_is_new_world(bird),
            is_new_region=gallery_bird_is_new_region(bird),
            is_foy_world=gallery_bird_is_foy_world(bird),
            is_foy_region=gallery_bird_is_foy_region(bird),
        )
        if extra:
            label = f"{label} · {', '.join(extra)}"
        label = f"{label} · {visible_pos + 1}/{len(visible_indices)}"
        show_remove = gallery_remove_visible()
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
                help="Show or hide bird info",
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
            frame_style=gallery_frame_style(bird),
            key=f"gallery_swipe_{bird_index}_{image_index}_{','.join(gallery_scopes)}",
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
        show_image_pos = gallery_legends_visible()
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

        species_code = str(bird.get("code") or "").strip()
        region_label = region_display_names(str(region_code or ""), allow_api=False)[0]
        mine_col, region_col = st.columns(2, vertical_alignment="top")
        with mine_col:
            st.markdown("**My recent sightings**")
            from my_ebird_data import my_ebird_data_path

            if not configured_observer_names() and my_ebird_data_path() is None:
                st.write(
                    "Add a My eBird data CSV, or set `EBIRD_USER_DISPLAY_NAME` "
                    "to your public eBird checklist name."
                )
            elif not species_code:
                st.write("This gallery bird has no eBird species code.")
            else:
                mine = local_own_recent_sightings_for_species(species_code, limit=5)
                if not mine:
                    st.write("No personal sightings of this species in the export or cached checklists.")
                else:
                    render_cached_sighting_lines(
                        mine,
                        highlight_region=str(region_code or ""),
                    )
        with region_col:
            st.markdown("**Recent sightings in this region**")
            if not species_code:
                st.write("This gallery bird has no eBird species code.")
            else:
                sightings = local_recent_sightings_for_species(
                    str(region_code or ""),
                    species_code,
                    limit=5,
                )
                if not sightings:
                    st.write(
                        f"No cached checklists in {region_label or region_code} "
                        "include this species."
                    )
                else:
                    render_cached_sighting_lines(sightings)
        st.caption(
            "Highlighted rows in My recent sightings are in the current region. "
            "Both lists use downloaded checklists only."
        )

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
        if gallery_legends_visible():
            st.caption(
                "Species often confused with this one on iNaturalist. "
                f"Regional last-seen uses eBird data for {region_code}."
            )
            st.markdown(
                f"Highlights · {novelty_legend_html()}",
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
                            similar_bird = similar_item_to_bird(item)
                            frame_color = gallery_frame_color(similar_bird)
                            frame_style = gallery_frame_style(similar_bird)
                            image_url = str(item.get("image_url") or "").strip()
                            if image_url:
                                src = html.escape(image_url, quote=True)
                                st.markdown(
                                    f"<div style='border:4px {frame_style} {frame_color};"
                                    f"border-radius:10px;padding:4px;"
                                    f"box-sizing:border-box;line-height:0'>"
                                    f"<img src='{src}' alt='' "
                                    f"style='width:100%;display:block;"
                                    f"border-radius:6px;margin:0'/></div>",
                                    unsafe_allow_html=True,
                                )
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
      - last_by_common / last_by_sci: latest CSV Date per species
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
    last_by_common: dict[str, date] = {}
    last_by_sci: dict[str, date] = {}
    for bird in birds:
        last_day = bird.get("last_day")
        if not isinstance(last_day, date):
            continue
        name_key = normalize_common_name(str(bird.get("name") or ""))
        if name_key:
            previous = last_by_common.get(name_key)
            if previous is None or last_day > previous:
                last_by_common[name_key] = last_day
        sci_key = binomial_sci_name(str(bird.get("sciName") or ""))
        if sci_key:
            previous = last_by_sci.get(sci_key)
            if previous is None or last_day > previous:
                last_by_sci[sci_key] = last_day
    return {
        "common": common,
        "sci": sci,
        "last_by_common": last_by_common,
        "last_by_sci": last_by_sci,
    }


def _life_list_bird_key(name: str, sci_name: str) -> str:
    display = name.split(" (", 1)[0].strip() or name
    if display:
        return normalize_common_name(display)
    return binomial_sci_name(sci_name)


def _merge_life_list_bird(
    birds: list[dict],
    index_by_key: dict[str, int],
    *,
    name: str,
    sci_name: str,
    code: str = "",
    last_day: date | None = None,
    taxon_order: float | None = None,
) -> None:
    display = name.split(" (", 1)[0].strip() or name or sci_name
    key = _life_list_bird_key(display, sci_name)
    if not key:
        return
    existing_idx = index_by_key.get(key)
    if existing_idx is not None:
        existing = birds[existing_idx]
        if last_day is not None:
            previous = existing.get("last_day")
            if not isinstance(previous, date) or last_day > previous:
                existing["last_day"] = last_day
        if code and not existing.get("code"):
            existing["code"] = code
        if taxon_order is not None and existing.get("taxon_order") is None:
            existing["taxon_order"] = taxon_order
        return
    index_by_key[key] = len(birds)
    birds.append(
        {
            "name": display,
            "sciName": sci_name,
            "code": code,
            "last_day": last_day,
            "taxon_order": taxon_order,
        }
    )


def load_life_list_birds(region_code: str) -> list[dict] | None:
    """Load ordered life-list species rows for gallery browsing.

    Merges ``lifeLists/ebird_<region>_life_list.csv`` with ticks from the
    My eBird data export when that file is present.
    """
    path = life_list_path(region_code)
    birds: list[dict] = []
    index_by_key: dict[str, int] = {}
    if path.exists():
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
                _merge_life_list_bird(
                    birds,
                    index_by_key,
                    name=name,
                    sci_name=sci_name,
                    last_day=parse_ebird_obs_day(str(row.get("Date") or "")),
                )
    try:
        from my_ebird_data import my_ebird_life_list_birds
    except Exception:
        my_ebird_life_list_birds = None  # type: ignore[assignment]
    if my_ebird_life_list_birds is not None:
        for row in my_ebird_life_list_birds(region_code):
            _merge_life_list_bird(
                birds,
                index_by_key,
                name=str(row.get("name") or ""),
                sci_name=str(row.get("sciName") or ""),
                code=str(row.get("code") or ""),
                last_day=row.get("last_day") if isinstance(row.get("last_day"), date) else None,
                taxon_order=row.get("taxon_order") if isinstance(row.get("taxon_order"), (int, float)) else None,
            )
    if not birds:
        return None
    birds.sort(
        key=lambda bird: (
            bird.get("taxon_order") if isinstance(bird.get("taxon_order"), (int, float)) else float("inf"),
            str(bird.get("name") or "").casefold(),
        )
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
    extra = [f"Opened from the {region_code} life list."]
    try:
        from my_ebird_data import my_ebird_data_path

        export_path = my_ebird_data_path()
        if export_path is not None:
            extra.append(f"Includes ticks from `{export_path.name}`.")
    except Exception:
        pass
    csv_path = life_list_path(region_code)
    if csv_path.exists():
        extra.append(f"Includes `{csv_path.name}`.")
    open_gallery(
        birds,
        title=title,
        notes=default_gallery_notes(
            title=title,
            source_title="Life list",
            species_count=len(birds),
            region_code="" if region_code == WORLD_LIFE_LIST_CODE else region_code,
            extra_lines=extra,
        ),
    )


def render_life_list_gallery_links() -> None:
    """World and region life-list gallery shortcuts."""
    region_code = selected_region_code()
    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    region_life = load_life_list(region_code) if region_code else None
    world_total = life_list_total(world_life)
    region_total = life_list_total(region_life)
    _, region_label = region_display_names(region_code, allow_api=False)
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
            st.caption(f"Region life list ({region_label or region_code})")
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


def load_region_species_gallery_birds(region_code: str) -> list[dict]:
    """Resolve the full eBird regional species list into gallery bird rows.

    Uses the on-disk region species cache when present. Otherwise
    ``/product/spplist/{region}`` codes are fetched, named via taxonomy, and
    written back to ``ebird_region_species_cache.json``.
    """
    code = (region_code or "").strip()
    if not code:
        return []
    return EBirdClient().region_species_birds(code)


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
    extra = [
        "Opened from the full eBird historical species list for this region.",
        "This is every species ever reported in the region, not only your ticks.",
    ]
    open_gallery(
        birds,
        title=f"Region species gallery · {code}",
        notes=default_gallery_notes(
            title=f"Region species gallery · {code}",
            source_title="Region species list",
            species_count=len(birds),
            region_code=code,
            extra_lines=extra,
        ),
    )


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


def is_new_to_region_life_list(
    taxon: dict,
    region_life: dict[str, set[str]] | None,
    world_life: dict[str, set[str]] | None,
) -> bool | None:
    """New to the region only when the bird is already on the world life list."""
    region_new = is_new_to_life_list(taxon, region_life)
    if region_new is None:
        return None
    if is_new_to_life_list(taxon, world_life) is not False:
        return False
    return bool(region_new)


def life_list_last_day(taxon: dict, life: dict[str, set[str]] | None) -> date | None:
    """Most recent date from the My eBird data export or a life-list CSV."""
    if not life:
        return None
    common = normalize_common_name(str(taxon.get("comName") or taxon.get("name") or ""))
    last_by_common = life.get("last_by_common") or {}
    if common and isinstance(last_by_common, dict) and common in last_by_common:
        day = last_by_common[common]
        return day if isinstance(day, date) else None
    sci = binomial_sci_name(str(taxon.get("sciName") or ""))
    last_by_sci = life.get("last_by_sci") or {}
    if sci and isinstance(last_by_sci, dict) and sci in last_by_sci:
        day = last_by_sci[sci]
        return day if isinstance(day, date) else None
    return None


def is_missing_first_of_year(
    taxon: dict,
    *,
    year: int,
    life: dict[str, set[str]] | None,
    obs_day: date | None,
) -> bool:
    """True when the bird is on the life list but last seen before ``year``."""
    if life is None:
        return False
    if is_new_to_life_list(taxon, life) is not False:
        return False
    if obs_day is not None and obs_day.year == year:
        return False
    last_day = life_list_last_day(taxon, life)
    return last_day is None or last_day.year < year


def is_recorded_in_region_this_year(
    taxon: dict,
    *,
    year: int,
    region_life: dict[str, set[str]] | None,
    obs_day: date | None = None,
    obs_is_in_region: bool = False,
) -> bool:
    """True when the bird was seen in the selected region this calendar year."""
    last_day = life_list_last_day(taxon, region_life)
    if last_day is not None and last_day.year >= year:
        return True
    return bool(
        obs_is_in_region and obs_day is not None and obs_day.year == year
    )


def novelty_labels(
    *,
    is_new_world: bool,
    is_new_region: bool,
    is_foy_world: bool = False,
    is_foy_region: bool = False,
) -> list[str]:
    labels: list[str] = []
    if is_new_world:
        labels.append("new to world")
    elif is_new_region:
        labels.append("new to region")
    if is_foy_world and not is_new_world:
        labels.append("missing FoY world")
    elif is_foy_region and not is_new_region:
        labels.append("missing FoY region")
    return labels


def obs_is_new_for_scope(obs: dict, scope: str) -> bool:
    """Whether an observation is new for the selected life-list scope."""
    if scope == "world":
        return bool(obs.get("is_new_world"))
    if scope == "region":
        if "is_new_region" in obs:
            return bool(obs.get("is_new_region"))
        return bool(obs.get("is_new"))
    if scope == "foy_world":
        return bool(obs.get("is_foy_world"))
    if scope == "foy_region":
        return bool(obs.get("is_foy_region"))
    if scope == "recorded":
        return bool(obs.get("is_recorded_region"))
    return False


def summary_is_new_for_scope(item: dict, scope: str) -> bool:
    if scope == "world":
        return bool(item.get("New_world"))
    if scope == "region":
        return bool(item.get("New_region") or item.get("New"))
    if scope == "foy_world":
        return bool(item.get("FoY_world") or item.get("is_foy_world"))
    if scope == "foy_region":
        return bool(item.get("FoY_region") or item.get("is_foy_region"))
    if scope == "recorded":
        return bool(item.get("Recorded") or item.get("is_recorded_region"))
    return False


def new_bird_marker(
    is_new_region: bool,
    is_new_world: bool,
    *,
    scope: str,
    is_foy_region: bool = False,
    is_foy_world: bool = False,
) -> str:
    """Build a short marker for region/world new and missing-FoY status."""
    if scope == "region":
        bits: list[str] = []
        if is_new_region:
            bits.append("new to region")
        elif is_foy_region:
            bits.append("missing FoY region")
        if not bits:
            return ""
        return " · **" + ", ".join(bits) + "**"
    if scope == "world":
        bits = []
        if is_new_world:
            bits.append("new to world")
        elif is_foy_world:
            bits.append("missing FoY world")
        if not bits:
            return ""
        return " · **" + ", ".join(bits) + "**"
    if scope == "foy_world":
        return " · **missing FoY world**" if is_foy_world else ""
    if scope == "foy_region":
        return " · **missing FoY region**" if is_foy_region else ""
    labels = novelty_labels(
        is_new_world=is_new_world,
        is_new_region=is_new_region,
        is_foy_world=is_foy_world,
        is_foy_region=is_foy_region,
    )
    if not labels:
        return ""
    return " · **" + ", ".join(labels) + "**"


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
                    "FoY_region": False,
                    "FoY_world": False,
                    "Recorded": False,
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
            if obs.get("is_foy_region"):
                entry["FoY_region"] = True
            if obs.get("is_foy_world"):
                entry["FoY_world"] = True
            if obs.get("is_recorded_region"):
                entry["Recorded"] = True

    results: list[dict] = []
    for entry in summary.values():
        results.append(
            {
                "Species": entry["Species"],
                "Max count": entry["Max count"] if entry["Max count"] is not None else "—",
                "Checklists": len(entry["_checklist_ids"]),
                "New_region": entry["New_region"],
                "New_world": entry["New_world"],
                "FoY_region": entry["FoY_region"],
                "FoY_world": entry["FoY_world"],
                "Recorded": entry["Recorded"],
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


def render_region_code_lookup(
    *,
    session_key: str = "checklists_region",
    in_expander: bool = True,
) -> None:
    """Country → state → county picker that writes an eBird region code."""
    def _body() -> None:
        if in_expander:
            st.caption(
                "Browse eBird regions by name, then apply the code. "
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
            st.session_state[session_key] = selected_code
            st.session_state.pop("_region_display_names", None)
            apply_region_code(selected_code)
            st.rerun()

    if in_expander:
        with st.expander("Look up region code", expanded=False):
            _body()
    else:
        _body()


def render_region_code_input(
    *,
    session_key: str = "checklists_region",
    help: str,
    lookup_in_expander: bool = True,
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
    render_region_code_lookup(
        session_key=session_key,
        in_expander=lookup_in_expander,
    )
    return str(st.session_state.get(session_key) or "").strip()


def leave_region_select() -> None:
    """Save the chosen region and return to the previous screen."""
    apply_region_code(selected_region_code())
    previous = st.session_state.pop("dashboard_before_region", None)
    if previous == "gallery":
        go_dashboard("gallery")
    elif previous in DASHBOARD_SCREENS and previous != "region":
        go_dashboard(previous)
    else:
        go_dashboard("checklists")


def render_region_select() -> None:
    """Dedicated screen for choosing the eBird region."""
    render_page_header("Region", screen="region")
    st.caption(
        "This region is used by Checklists, Checklist cache, Cache maintenance, "
        "and gallery filters."
    )
    code = selected_region_code()
    short, long_name = region_display_names(code, allow_api=True)
    if code:
        st.write(f"**{long_name}**")
        if short != code:
            st.caption(code)
    else:
        st.info("No region selected yet.")
    if st.button(
        "Open full region species gallery",
        key="gallery_region_species_list",
        use_container_width=True,
        help=(
            "Loads every species ever recorded in this region from "
            "eBird /product/spplist, resolves names via taxonomy, and opens "
            "the gallery."
        ),
        disabled=not bool(code),
    ):
        if ensure_api_key():
            open_region_species_gallery(code)
    render_recent_region_buttons(code)
    render_region_code_input(
        help=(
            "eBird region, e.g. US-FL-099, US-FL, or US. "
            "Browse by name below if you do not know the code."
        ),
        lookup_in_expander=False,
    )
    if st.button(
        "Done",
        type="primary",
        use_container_width=True,
        key="region_select_done",
    ):
        leave_region_select()


def _checklist_sub_id(row: dict) -> str:
    return str(row.get("subId") or row.get("subID") or "").strip()


def merge_checklist_summaries(*groups: list[dict]) -> list[dict]:
    """Union checklist rows by id, preferring local ``_detail`` payloads."""
    found: dict[str, dict] = {}
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            sub_id = _checklist_sub_id(row)
            if not sub_id:
                continue
            current = found.get(sub_id)
            if current is None:
                found[sub_id] = dict(row)
                continue
            merged = dict(current)
            for key, value in row.items():
                if key == "_detail":
                    if value and not merged.get("_detail"):
                        merged["_detail"] = value
                    continue
                if not merged.get(key) and value not in (None, ""):
                    merged[key] = value
            found[sub_id] = merged
    return sorted(
        found.values(),
        key=lambda row: str(row.get("isoObsDate") or row.get("obsDt") or ""),
        reverse=True,
    )


def load_checklists_for_date_windows(
    region_code: str,
    loc_id: str,
    start: date,
    end: date,
    *,
    prior_years: int = 0,
    client: EBirdClient | None = None,
) -> list[dict]:
    """Load hotspot checklists for the date range and the same window in prior years.

    Local detail files and the regional daily-feed cache are used first. The
    eBird API is called only for missing feed days (and today), and those
    results are written back into the feed cache.
    """
    slices = download_window_slices(start, end, prior_years=max(0, int(prior_years)))
    groups: list[list[dict]] = []
    for _year, window_start, window_end in slices:
        groups.append(
            load_local_checklists_for_hotspot(
                region_code,
                loc_id,
                start_date=window_start,
                end_date=window_end,
            )
        )
        if client is not None:
            api_rows = client.location_checklists(
                loc_id,
                start_date=window_start,
                end_date=window_end,
                region_code=region_code,
                persist=True,
            )
            for row in api_rows:
                if isinstance(row, dict) and region_code:
                    row.setdefault("regionCode", region_code)
            groups.append(api_rows)
    return merge_checklist_summaries(*groups)


def enrich_checklists(
    client: EBirdClient | None,
    rows: list[dict],
    region_life: dict[str, set[str]] | None,
    world_life: dict[str, set[str]] | None = None,
    *,
    allow_api: bool = True,
    region_code: str | None = None,
) -> list[dict]:
    """Attach species names and life-list-new counts to checklist summaries.

    When a row already includes ``_detail`` (local cache), that payload is used.
    Otherwise details are fetched via the eBird API when ``allow_api`` is true,
    then written into the on-disk checklist cache.
    """
    details: dict[str, dict] = {}
    codes: list[str] = []
    persist_region = str(
        region_code
        or st.session_state.get("checklists_region")
        or os.environ.get("EBIRD_HOME_REGION", "")
        or ""
    ).strip()
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
            save_region = str(
                row.get("regionCode") or row.get("_region") or persist_region or ""
            ).strip()
            if save_region and detail:
                saved = save_checklist_detail(save_region, row, detail)
                if saved is not None:
                    row["_detail"] = detail
                    row["_path"] = str(saved)
        else:
            details[sub_id] = {}
        for obs in details[sub_id].get("obs") or []:
            code = obs.get("speciesCode")
            if code:
                codes.append(str(code))

    if allow_api and client is not None and codes:
        taxa_by_code = client.species_taxa(codes)
    else:
        taxa_by_code = load_cached_taxa(codes) if codes else {}
    this_year = date.today().year

    enriched: list[dict] = []
    for row in rows:
        sub_id = str(row.get("subId") or row.get("subID") or "")
        detail = details.get(sub_id, {})
        species_rows: list[dict] = []
        new_region_names: list[str] = []
        new_world_names: list[str] = []
        seen_codes: set[str] = set()
        obs_day = parse_ebird_obs_day(
            str(row.get("isoObsDate") or row.get("obsDt") or "")
        )
        for obs in detail.get("obs") or []:
            code = str(obs.get("speciesCode") or "")
            taxon = taxa_by_code.get(code, {}) if code else {}
            common = taxon.get("comName") or obs.get("comName") or code
            sci_name = taxon.get("sciName") or obs.get("sciName") or ""
            identity = (
                code
                or binomial_sci_name(str(sci_name))
                or normalize_common_name(str(common))
            )
            if not identity or identity in seen_codes:
                continue
            seen_codes.add(identity)
            count = (
                obs.get("howManyAtleast")
                or obs.get("howMany")
                or obs.get("howManyStr")
                or ""
            )
            taxon_for_match = {
                "comName": common,
                "sciName": sci_name,
                "category": taxon.get("category") or "",
            }
            is_new_region = is_new_to_region_life_list(
                taxon_for_match, region_life, world_life
            )
            is_new_world = is_new_to_life_list(taxon_for_match, world_life)
            is_foy_region = is_missing_first_of_year(
                taxon_for_match,
                year=this_year,
                life=region_life,
                obs_day=obs_day,
            )
            is_foy_world = is_missing_first_of_year(
                taxon_for_match,
                year=this_year,
                life=world_life,
                obs_day=obs_day,
            )
            row_region = str(
                row.get("regionCode") or row.get("_region") or persist_region or ""
            ).strip()
            obs_is_in_region = bool(persist_region) and (
                not row_region
                or row_region == persist_region
                or row_region.startswith(persist_region + "-")
                or persist_region.startswith(row_region + "-")
            )
            is_recorded_region = is_recorded_in_region_this_year(
                taxon_for_match,
                year=this_year,
                region_life=region_life,
                obs_day=obs_day,
                obs_is_in_region=obs_is_in_region,
            )
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
                    "is_foy_region": is_foy_region,
                    "is_foy_world": is_foy_world,
                    "is_recorded_region": is_recorded_region,
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
        "Mobile constrains the layout to a phone-sized column. "
        "iPhone browsers start in mobile; you can still switch here.",
    )
    st.caption(
        "Local API and image caches. Checklist feeds/details are reported separately "
        "under Checklist cache. Region bird coverage is based on the selected "
        "region’s historical species list."
    )

    region_code = selected_region_code()

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
        "Every eBird HTTP call goes through ``EBirdClient.get``, which spaces "
        "requests at 37.5/min (shared with this app) and pauses for eBird’s "
        "Retry-After interval on HTTP 429."
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
    if progress.get("windows"):
        scope_bits.append(str(progress["windows"]))
    elif progress.get("start_day") or progress.get("end_day"):
        scope_bits.append(
            f"{progress.get('start_day') or '…'}–{progress.get('end_day') or '…'}"
        )
    if progress.get("day") and not progress.get("windows"):
        scope_bits.append(f"day {progress['day']}")
    if progress.get("prior_years"):
        scope_bits.append(f"{int(progress['prior_years'])} prior year(s)")
    if progress.get("loc_id"):
        scope_bits.append(f"hotspot {progress['loc_id']}")
    if progress.get("min_species"):
        scope_bits.append(f"≥{int(progress['min_species'])} species")
    if progress.get("phase") == "feed":
        scope_bits.append("filling daily feed")
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
    if st.session_state.get("checklist_download_scope") == "day":
        st.session_state.checklist_download_scope = "range"
    scope = st.radio(
        "Scope",
        options=["all", "range", "hotspot"],
        format_func=lambda value: {
            "all": "All missing for year",
            "range": "Date range",
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

    selected_start: str | None = None
    selected_end: str | None = None
    selected_loc: str | None = None
    prior_years = 0
    today = date.today()
    year_start = date(int(year), 1, 1)
    year_end = date(int(year), 12, 31) if int(year) < today.year else today
    if year_start > year_end:
        year_end = year_start

    if scope == "range":
        if (
            st.session_state.get("checklist_download_range_year") != int(year)
            or "checklist_download_range" not in st.session_state
        ):
            default_start = (
                date(int(year), today.month, 1)
                if int(year) == today.year
                else year_start
            )
            if default_start < year_start:
                default_start = year_start
            if default_start > year_end:
                default_start = year_end
            st.session_state.checklist_download_range = (default_start, year_end)
            st.session_state.checklist_download_range_year = int(year)
        range_value = st.date_input(
            "Days / months to download",
            min_value=date(2002, 1, 1),
            max_value=today,
            key="checklist_download_range",
            disabled=active,
            help="Inclusive observation-date window. One day or several months.",
        )
        parsed = parse_streamlit_date_range(range_value)
        if parsed:
            selected_start, selected_end = (
                parsed[0].isoformat(),
                parsed[1].isoformat(),
            )
        include_prior = st.checkbox(
            "Also download this same period from prior years",
            key="checklist_download_prior",
            disabled=active,
            help="Repeats this month/day window in earlier years (needs daily feeds for those years; missing days are fetched first).",
        )
        if include_prior:
            prior_years = int(
                st.number_input(
                    "Prior years",
                    min_value=1,
                    max_value=15,
                    value=1,
                    step=1,
                    key="checklist_download_prior_years",
                    disabled=active,
                    help="1 = last year as well, 2 = the two previous years, and so on.",
                )
            )
            if parsed:
                st.caption(prior_year_download_caption(parsed[0], parsed[1], prior_years))
        elif parsed:
            st.caption(prior_year_download_caption(parsed[0], parsed[1], 0))
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
            or (scope == "range" and not selected_start)
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
            start_day=selected_start,
            end_day=selected_end,
            loc_id=selected_loc,
            min_species=min_species,
            prior_years=prior_years,
        )
        if error:
            st.error(f"Could not start background downloader: {error}")
        else:
            if scope == "all":
                label = "all missing in year"
            elif selected_start:
                label = f"{selected_start}–{selected_end or selected_start}"
                if prior_years:
                    label = f"{label} + {prior_years} prior year(s)"
            else:
                label = f"hotspot {selected_loc}"
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

    region_code = selected_region_code()

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
        st.info("Select a region to inspect the checklist cache.")
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
            st.info("Select a region to inspect the checklist cache.")
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


HOTSPOT_DROPDOWN_LIMIT = 100
RECENT_REGIONS_MAX = 10
RECENT_REGIONS_PATH = Path(__file__).parent / "ebird_recent_regions.json"


def load_recent_checklist_regions() -> list[str]:
    stored = st.session_state.get("recent_checklist_regions")
    if isinstance(stored, list):
        return [str(code).strip() for code in stored if str(code).strip()][:RECENT_REGIONS_MAX]
    rows: list[str] = []
    try:
        payload = json.loads(RECENT_REGIONS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            rows = [str(code).strip() for code in payload if str(code).strip()]
    except (OSError, json.JSONDecodeError):
        rows = []
    home = str(os.environ.get("EBIRD_HOME_REGION", "US-FL-099") or "").strip()
    if home and home not in rows:
        rows.append(home)
    rows = rows[:RECENT_REGIONS_MAX]
    st.session_state.recent_checklist_regions = rows
    return rows


def remember_checklist_region(code: str) -> None:
    region = str(code or "").strip()
    if not region:
        return
    items = [item for item in load_recent_checklist_regions() if item != region]
    items.insert(0, region)
    items = items[:RECENT_REGIONS_MAX]
    st.session_state.recent_checklist_regions = items
    try:
        RECENT_REGIONS_PATH.write_text(
            json.dumps(items, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def hotspot_dropdown_rows(region_code: str) -> list[dict]:
    """Top hotspots for the dropdown, without expanding a full-region cache."""
    region = str(region_code or "").strip()
    if not region:
        return []
    cached = load_cached_hotspots(region)
    if not cached:
        return []
    return sort_hotspots(filter_hotspots_for_region(cached, region))[
        :HOTSPOT_DROPDOWN_LIMIT
    ]


def apply_checklist_hotspots(
    region: str,
    rows: list[dict],
    *,
    remember: bool = True,
) -> None:
    previous = str(st.session_state.get("checklists_hotspots_region") or "")
    rows = filter_hotspots_for_region(rows, region)
    st.session_state.checklists_region = region
    st.session_state.checklists_hotspots = rows
    st.session_state.checklists_hotspots_region = region
    hotspot_ids = [row["locId"] for row in rows if row.get("locId")]
    if hotspot_ids and st.session_state.get("checklists_loc_id") not in hotspot_ids:
        st.session_state.checklists_loc_id = (
            DEFAULT_HOTSPOT_ID
            if DEFAULT_HOTSPOT_ID in hotspot_ids
            else hotspot_ids[0]
        )
    if previous != region:
        st.session_state.pop("checklist_rows", None)
        st.session_state.pop("checklist_summaries", None)
        st.session_state.pop("checklist_shown", None)
        st.session_state.pop("checklist_source", None)
    if remember:
        remember_checklist_region(region)


def apply_region_code(code: str) -> None:
    """Store the selected region and refresh hotspot rows for Checklists."""
    region = str(code or "").strip()
    if not region:
        return
    st.session_state.checklists_region = region
    names = st.session_state.get("_region_display_names")
    if isinstance(names, dict):
        names.pop(region, None)
    rows = hotspot_dropdown_rows(region)
    if not rows and get_api_key():
        try:
            rows = EBirdClient().top_hotspots(region, limit=HOTSPOT_DROPDOWN_LIMIT)
        except Exception:
            rows = []
    apply_checklist_hotspots(region, rows)


def select_checklist_region(code: str) -> None:
    """Switch region and reset the hotspot dropdown to the top 100."""
    apply_region_code(code)
    st.rerun()


def render_recent_region_buttons(current: str) -> None:
    recents = load_recent_checklist_regions()
    if not recents:
        return
    st.caption("Recent regions")
    for start in range(0, len(recents), 5):
        chunk = recents[start : start + 5]
        cols = st.columns(5)
        for col, code in zip(cols, chunk):
            with col:
                short, _ = region_display_names(code, allow_api=False)
                if st.button(
                    short,
                    key=f"recent_region_{code}",
                    use_container_width=True,
                    type="primary" if code == current else "secondary",
                    help=code if short != code else None,
                ):
                    select_checklist_region(code)


def render_checklists() -> None:
    render_page_header("Checklists", screen="checklists")
    st.caption(
        "Choose a top hotspot, then browse recent checklists. "
        "Tap the region name in the upper right to change region. "
        "New-bird counts use your My eBird data export when present, plus "
        "`lifeLists/ebird_world_life_list.csv` and "
        "`lifeLists/ebird_<region>_life_list.csv`."
    )

    region_code = selected_region_code()
    life_scope = "all"

    hotspots: list[dict] = []
    loaded_region = str(st.session_state.get("checklists_hotspots_region") or "")
    if region_code and loaded_region == region_code:
        hotspots = filter_hotspots_for_region(
            list(st.session_state.get("checklists_hotspots") or []),
            region_code,
        )
        if hotspots != list(st.session_state.get("checklists_hotspots") or []):
            apply_checklist_hotspots(region_code, hotspots, remember=False)
    elif region_code:
        hotspots = hotspot_dropdown_rows(region_code)
        apply_checklist_hotspots(region_code, hotspots, remember=bool(hotspots))

    load_label = "Load additional hotspots" if hotspots else "Load hotspots"
    if st.button(
        load_label,
        key="load_hotspots_button",
        type="primary" if not hotspots else "secondary",
        help=(
            "With an empty dropdown, fetch the top 100 hotspots. "
            "If the dropdown already has the top 100, this adds the rest of the region."
        ),
    ):
        if not region_code:
            st.warning("Enter a region code.")
        elif ensure_api_key():
            spinner = (
                f"Loading additional hotspots for {region_code}…"
                if hotspots
                else f"Loading top {HOTSPOT_DROPDOWN_LIMIT} hotspots for {region_code}…"
            )
            with st.spinner(spinner):
                try:
                    client = EBirdClient()
                    if hotspots:
                        merged, added = client.additional_hotspots(
                            region_code,
                            existing=hotspots,
                        )
                    else:
                        merged = client.top_hotspots(
                            region_code,
                            limit=HOTSPOT_DROPDOWN_LIMIT,
                        )
                        added = merged
                except MissingEbirdApiKey:
                    ensure_api_key()
                except requests.HTTPError as exc:
                    st.error(
                        f"eBird API error: {exc.response.status_code if exc.response else exc}"
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    apply_checklist_hotspots(region_code, merged)
                    hotspots = merged
                    if not merged:
                        st.warning(f"No hotspots found for {region_code}.")
                    elif added and len(merged) <= HOTSPOT_DROPDOWN_LIMIT:
                        st.success(
                            f"Loaded {len(merged):,} hotspot"
                            f"{'' if len(merged) == 1 else 's'} "
                            f"(top {HOTSPOT_DROPDOWN_LIMIT})."
                        )
                    elif added:
                        st.success(
                            f"Added {len(added):,} hotspot"
                            f"{'' if len(added) == 1 else 's'} "
                            f"({len(merged):,} total)."
                        )
                    else:
                        st.info(
                            f"No additional hotspots. {len(merged):,} already cached."
                        )

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
        help="Top 100 hotspots for this region by all-time species count. Load additional hotspots to expand the list.",
    )
    today = date.today()
    if "checklist_end_date_input" not in st.session_state:
        previous_end = st.session_state.get("checklist_end_date", today)
        if not isinstance(previous_end, date):
            try:
                previous_end = date.fromisoformat(str(previous_end)[:10])
            except ValueError:
                previous_end = today
        st.session_state.checklist_end_date_input = previous_end
    if "checklist_start_date_input" not in st.session_state:
        previous_end = st.session_state.checklist_end_date_input
        previous_days = int(st.session_state.get("checklist_days") or 7)
        st.session_state.checklist_start_date_input = previous_end - timedelta(
            days=max(1, previous_days) - 1
        )
    date_cols = st.columns(2)
    with date_cols[0]:
        start_date = st.date_input(
            "Start date",
            min_value=date(2002, 1, 1),
            max_value=date(2100, 12, 31),
            key="checklist_start_date_input",
            help="First observation day to include. Future dates are allowed.",
        )
    with date_cols[1]:
        end_date = st.date_input(
            "End date",
            min_value=date(2002, 1, 1),
            max_value=date(2100, 12, 31),
            key="checklist_end_date_input",
            help="Last observation day to include. Future dates are allowed.",
        )
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    if "checklists_prior_years" not in st.session_state:
        st.session_state.checklists_prior_years = 0
    prior_years = int(
        st.slider(
            "Prior years",
            min_value=0,
            max_value=15,
            key="checklists_prior_years",
            help=(
                "0 = only this start/end window. 1 also includes the same dates last year "
                "when showing, caching, and downloading, and so on."
            ),
        )
    )
    page_size = 50

    action_cols = st.columns(3)
    with action_cols[0]:
        show_api = st.button("Show checklists", type="primary", use_container_width=True)
    with action_cols[1]:
        show_cache = st.button(
            "Show from cache",
            use_container_width=True,
            help=(
                "Browse downloaded checklist files for the selected hotspot "
                "in the date range above, including prior years if set."
            ),
        )
    with action_cols[2]:
        show_all_cache = st.button(
            "Show all from cache",
            use_container_width=True,
            help=(
                "Browse every downloaded checklist for the selected hotspot, "
                "ignoring the start and end dates."
            ),
        )

    with st.expander("Download checklist details for this period"):
        st.caption(
            "Downloads missing full checklists for the hotspot and date range above. "
            "Missing daily-feed days are fetched first."
        )
        st.caption(prior_year_download_caption(start_date, end_date, prior_years))
        download_busy = bool(
            region_code and _checklist_download_active(region_code, end_date.year)
        )
        if st.button(
            "Download missing details",
            type="primary",
            disabled=not region_code or not loc_id or download_busy,
            use_container_width=True,
            key="checklists_download_period",
            help="Starts the same background downloader used on the cache screen.",
        ):
            if not ensure_api_key():
                pass
            else:
                error = _start_checklist_download(
                    region_code,
                    end_date.year,
                    start_day=start_date.isoformat(),
                    end_day=end_date.isoformat(),
                    loc_id=loc_id,
                    prior_years=prior_years,
                )
                if error:
                    st.error(f"Could not start background downloader: {error}")
                else:
                    label = f"{start_date.isoformat()}–{end_date.isoformat()} · {loc_id}"
                    if prior_years:
                        label = f"{label} + {prior_years} prior year(s)"
                    st.success(
                        f"Background downloader started ({label}). "
                        "Watch progress on Checklist cache."
                    )
                    time.sleep(0.25)
                    st.rerun()

    if show_api:
        if ensure_api_key():
            active_region = st.session_state.get("checklists_region", region_code)
            life_for_region = load_life_list(active_region)
            life_for_world = load_life_list(WORLD_LIFE_LIST_CODE)
            with st.spinner("Loading checklists…"):
                try:
                    client = EBirdClient()
                    summaries = load_checklists_for_date_windows(
                        active_region,
                        loc_id,
                        start_date,
                        end_date,
                        prior_years=prior_years,
                        client=client,
                    )
                    enriched = enrich_checklists(
                        client,
                        summaries,
                        life_for_region,
                        life_for_world,
                        allow_api=True,
                        region_code=active_region,
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
                    st.session_state.checklist_start_date = start_date
                    st.session_state.checklist_end_date = end_date
                    st.session_state.checklist_prior_years = prior_years
                    st.session_state.checklist_summaries = summaries
                    st.session_state.checklist_rows = enriched
                    st.session_state.checklist_shown = len(enriched)
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
                summaries = load_checklists_for_date_windows(
                    active_region,
                    loc_id,
                    start_date,
                    end_date,
                    prior_years=prior_years,
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
                summaries,
                life_for_region,
                life_for_world,
                allow_api=allow_api,
                region_code=active_region,
            )
        st.session_state.checklists_loc_id = loc_id
        st.session_state.checklist_start_date = start_date
        st.session_state.checklist_end_date = end_date
        st.session_state.checklist_prior_years = 0 if all_dates else prior_years
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
    stored_start = st.session_state.get("checklist_start_date", start_date)
    if not isinstance(stored_end, date):
        stored_end = end_date
    if not isinstance(stored_start, date):
        stored_start = start_date
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
        stored_prior = int(st.session_state.get("checklist_prior_years") or 0)
        windows = download_window_slices(
            stored_start, stored_end, prior_years=max(0, stored_prior)
        )
        range_label = f"in **{format_download_windows(windows)}**"
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
                            region_code=str(
                                st.session_state.get("checklists_region") or ""
                            ),
                        )
                    elif ensure_api_key():
                        more = enrich_checklists(
                            EBirdClient(),
                            summaries[start:end],
                            life_for_region,
                            life_for_world,
                            allow_api=True,
                            region_code=str(
                                st.session_state.get("checklists_region") or ""
                            ),
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
        if life_scope == "all":
            filtered = species_summary
        else:
            filtered = [
                row
                for row in species_summary
                if summary_is_new_for_scope(row, life_scope)
            ]
        if life_scope != "all" and not filtered:
            st.subheader("Species summary")
            st.info(
                "No birds missing FoY world in the loaded checklists."
                if life_scope == "foy_world"
                else (
                    "No birds missing FoY region in the loaded checklists."
                    if life_scope == "foy_region"
                    else (
                        "No birds new to your "
                        f"{'world' if life_scope == 'world' else 'region'} "
                        "life list in the loaded checklists."
                    )
                )
            )
        else:
            gallery_birds = species_summary_gallery_birds(filtered)
            st.session_state.summary_gallery_birds = gallery_birds
            open_col, name_col = st.columns([1, 16], vertical_alignment="center")
            with open_col:
                render_open_gallery_icon_button(
                    key="open_gallery_icon_summary",
                    on_click=queue_open_summary_gallery,
                )
            with name_col:
                st.markdown("**Species summary**")
            render_checklist_species_summary_grid(filtered, width=144)
            st.caption(
                f"{len(filtered)} species · tap a photo or the photo-library icon to open the gallery"
            )

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
        elif life_scope == "foy_world":
            foy_count = sum(
                1
                for obs in (row.get("species_rows") or [])
                if obs_is_new_for_scope(obs, "foy_world")
            )
            new_bits.append(f"**{foy_count}** missing FoY world")
        elif life_scope == "foy_region":
            foy_count = sum(
                1
                for obs in (row.get("species_rows") or [])
                if obs_is_new_for_scope(obs, "foy_region")
            )
            new_bits.append(f"**{foy_count}** missing FoY region")
        else:
            if region_new is not None:
                new_bits.append(f"**{region_new}** new to region")
            if world_new is not None:
                new_bits.append(f"**{world_new}** new to world")
        new_label = f" · {' · '.join(new_bits)}" if new_bits else ""
        location_label = f" · {location}" if location else ""

        gallery_rows = checklist_gallery_birds(row, life_scope)
        date_text = date_label or str(sub_id)
        if gallery_rows and sub_id:
            date_md = f"[{date_text}]({checklist_gallery_url(str(sub_id))})"
        elif date_text:
            date_md = date_text
        else:
            date_md = str(sub_id)
        with st.container(border=True):
            st.markdown(
                f"**{date_md}** · {species} species · "
                f"{observer}{location_label}{new_label} · "
                f"[eBird]({checklist_url})"
            )
            species_rows = row.get("species_rows") or []
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
                        is_foy_region=bool(obs.get("is_foy_region")),
                        is_foy_world=bool(obs.get("is_foy_world")),
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
apply_iphone_mobile_layout()
apply_ui_layout()
apply_gallery_chrome_defaults()
get_api_key()  # ingest ?EBIRD_API_KEY=… into session when present
if st.session_state.get("ebird_api_key_needed") and not get_api_key():
    render_api_key_form()
render_ebird_rate_limit_notices()
maybe_open_saved_gallery_from_query()
maybe_open_checklist_gallery_from_query()
maybe_open_summary_gallery_from_query()
consume_open_summary_gallery()
missing = st.session_state.pop("saved_gallery_missing", None)
if missing:
    st.warning(f"Saved gallery `{missing}` was not found.")
missing_checklist = st.session_state.pop("checklist_gallery_missing", None)
if missing_checklist:
    st.warning(f"Checklist `{missing_checklist}` was not found in the local cache.")
dashboard = current_dashboard()
if dashboard == "gallery" and st.session_state.get("gallery_birds"):
    render_gallery()
elif dashboard == "mine":
    render_own_checklists()
elif dashboard == "checklists":
    render_checklists()
elif dashboard == "region":
    render_region_select()
elif dashboard == "cache":
    render_cache_status()
elif dashboard == "maintenance":
    render_general_cache_maintenance()
else:
    render_saved_galleries()
