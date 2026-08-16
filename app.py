from pathlib import Path
import csv
import os
import time
from datetime import date, timedelta

import requests
import streamlit as st
from dotenv import load_dotenv

from components.swipe_image import swipe_image
from ebird import (
    EBirdClient,
    build_local_last_seen_index,
    get_api_key,
    resolve_ebird_code,
)
from inaturalist import GALLERY_CACHE_VERSION, species_gallery, species_photo, species_similar

load_dotenv(Path(__file__).parent / ".env")

LIFE_LISTS_DIR = Path(__file__).parent / "lifeLists"
DEFAULT_HOTSPOT_ID = os.environ.get("EBIRD_DEFAULT_HOTSPOT", "L364884")
WORLD_LIFE_LIST_CODE = "world"
BUSY_CURSOR_CSS = """
<style>
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
  cursor: wait !important;
}
html *, body *, [data-testid="stAppViewContainer"] *, [data-testid="stApp"] * {
  cursor: wait !important;
}
</style>
"""


def set_busy_cursor(enabled: bool = True) -> None:
    """Toggle a page-wide busy cursor while waiting on rate-limited work."""
    if enabled:
        st.markdown(BUSY_CURSOR_CSS, unsafe_allow_html=True)
    else:
        st.markdown(
            "<style>html, body, [data-testid='stAppViewContainer'], "
            "[data-testid='stApp'], html *, body * { cursor: auto !important; }</style>",
            unsafe_allow_html=True,
        )


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


def open_gallery(birds: list[dict], *, title: str = "Gallery") -> None:
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
    st.session_state.gallery_view_mode_pending = "list"
    st.session_state.gallery_list_image_indices = {}
    st.rerun()


def gallery_list_image_index(bird_index: int) -> int:
    indices = st.session_state.setdefault("gallery_list_image_indices", {})
    return int(indices.get(str(bird_index), 0))


def set_gallery_list_image_index(bird_index: int, image_index: int) -> None:
    indices = st.session_state.setdefault("gallery_list_image_indices", {})
    indices[str(bird_index)] = int(image_index)


def open_gallery_standard_for_bird(bird_index: int) -> None:
    """Switch from list mode into standard view for a specific bird."""
    st.session_state.gallery_bird_index = bird_index
    st.session_state.gallery_image_index = gallery_list_image_index(bird_index)
    # Widget keys can't be written after instantiation; apply on the next run.
    st.session_state.gallery_view_mode_pending = "standard"
    st.session_state.gallery_show_info = False
    st.rerun()


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
    return {
        "code": str(item.get("ebird_code") or "").strip(),
        "name": str(name).strip(),
        "sciName": str(sci_name).strip(),
    }


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
    """Attach regional last-seen info and sort never-seen species to the end."""
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
    except requests.RequestException:
        region_codes = set(local_index)
    region_codes = set(region_codes) | set(local_index)

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
        elif ever_seen and code:
            # Local checklist cache missed; only then ask the API.
            try:
                observation = client.last_seen_in_region(
                    region,
                    code,
                    back=30,
                    allow_api=True,
                )
            except requests.RequestException:
                observation = None
            local_miss = observation is None or (
                observation.get("source") != "local_checklist"
                and not observation.get("obsDt")
            )
            if observation is None:
                local_miss = True
        row["region_history"] = {
            "ever_seen": ever_seen,
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
    header_cap, header_clear = st.columns([3, 1], vertical_alignment="center")
    with header_cap:
        st.caption(f"Bird {compare_index + 1} of {len(compare_birds)}")
    with header_clear:
        if st.button(
            "Remove all",
            key="compare_remove_all",
            use_container_width=True,
            help="Clear the entire compare list",
        ):
            clear_compare_list()
            st.rerun()
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
    with name_col:
        st.markdown(
            f"<div style='text-align:center; font-weight:600; padding-top:0.35rem'>{common}</div>",
            unsafe_allow_html=True,
        )
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

    if not photos:
        st.info("No iNaturalist photos found for this comparison bird.")
    else:
        image_index = int(st.session_state.get("gallery_compare_image_index", 0))
        image_index = max(0, min(image_index, len(photos) - 1))
        photo = photos[image_index]
        compare_frame = gallery_frame_color(compare_bird)
        st.markdown(
            f"<div style='border:4px solid {compare_frame}; border-radius:10px; "
            f"padding:4px; box-sizing:border-box'>",
            unsafe_allow_html=True,
        )
        st.image(photo["image_url"], width="stretch")
        st.markdown("</div>", unsafe_allow_html=True)

        prev_col, pos_col, next_col = st.columns([1, 2, 1])
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
        with pos_col:
            st.markdown(
                f"<div style='text-align:center'>Image {image_index + 1}/{len(photos)}</div>",
                unsafe_allow_html=True,
            )
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

    if st.button(
        "Remove from compare list",
        key=f"compare_remove_{gallery_bird_key(compare_bird)}",
    ):
        remove_compare_bird(compare_bird)
        st.rerun()


def render_gallery() -> None:
    birds = st.session_state.get("gallery_birds") or []
    if not birds:
        st.session_state.pop("gallery_birds", None)
        render_checklists()
        return

    birds = annotate_gallery_birds_with_life_lists(birds)
    st.session_state.gallery_birds = birds

    title = st.session_state.get("gallery_title", "Gallery")
    region_code = st.session_state.get("checklists_region") or os.environ.get(
        "EBIRD_HOME_REGION", "US-FL-099"
    )

    pending_mode = st.session_state.pop("gallery_view_mode_pending", None)
    if pending_mode in {"list", "standard"}:
        st.session_state.gallery_view_mode = pending_mode

    if st.button("← Back to checklists"):
        for key in (
            "gallery_birds",
            "gallery_title",
            "gallery_bird_index",
            "gallery_image_index",
            "gallery_show_info",
            "gallery_show_similar",
            "gallery_hide_similar_never_seen",
            "gallery_last_swipe_t",
            "gallery_compare_birds",
            "gallery_compare_bird_index",
            "gallery_compare_image_index",
            "gallery_visible_indices",
            "gallery_view_mode",
            "gallery_view_mode_pending",
            "gallery_list_image_indices",
            "gallery_list_last_swipe_t",
        ):
            st.session_state.pop(key, None)
        st.rerun()

    st.title(title)
    gallery_scope = st.radio(
        "Filter new birds by",
        options=["all", "region", "world"],
        format_func=lambda value: {
            "all": "All birds",
            "region": f"New to region ({region_code or 'region'})",
            "world": "New to world",
        }[value],
        horizontal=True,
        key="life_list_scope",
        help="Same filter as the checklists screen. Highlights: teal = new to world, amber = new to region, gray = already on both lists.",
    )
    st.markdown(
        f"Frame colors · "
        f"<span style='color:{FRAME_COLOR_WORLD}'>■</span> new to world · "
        f"<span style='color:{FRAME_COLOR_REGION}'>■</span> new to region · "
        f"<span style='color:{FRAME_COLOR_SEEN}'>■</span> already counted",
        unsafe_allow_html=True,
    )
    gallery_mode = st.radio(
        "Gallery view",
        options=["list", "standard"],
        format_func=lambda value: {
            "list": "List",
            "standard": "Standard",
        }[value],
        horizontal=True,
        key="gallery_view_mode",
        help="List shows every bird with a swipeable photo. Tap a name to open Standard view for that bird.",
    )

    visible_indices = [
        idx
        for idx, item in enumerate(birds)
        if gallery_bird_matches_scope(item, gallery_scope)
    ]
    st.session_state.gallery_visible_indices = visible_indices
    if not visible_indices:
        st.info(
            "No birds match this filter in the current gallery."
            if gallery_scope != "all"
            else "No birds available for the gallery."
        )
        return

    if gallery_mode == "list":
        render_gallery_list(birds, visible_indices, gallery_scope)
        return

    render_gallery_standard(birds, visible_indices, gallery_scope)


def render_gallery_list(
    birds: list[dict],
    visible_indices: list[int],
    gallery_scope: str,
) -> None:
    """Browse all matching birds with swipeable main photos."""
    st.caption(f"{len(visible_indices)} birds · tap a name for Standard view")
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

                if st.button(
                    label,
                    use_container_width=True,
                    key=f"gallery_list_open_{bird_index}",
                    help="Open Standard view for this bird",
                ):
                    open_gallery_standard_for_bird(bird_index)

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

    st.caption(f"Bird {visible_pos + 1} of {len(visible_indices)}")

    payload = gallery_payload_for_code(
        bird.get("code") or "",
        bird.get("sciName") or None,
    )
    photos = (payload or {}).get("photos") or []
    common = (payload or {}).get("common_name") or bird.get("name") or "Unknown"

    nav_prev, nav_name, nav_next = st.columns([1, 4, 1], vertical_alignment="center")
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
    with nav_name:
        label = common
        if gallery_bird_is_new_world(bird):
            label = f"{label} · new to world"
        elif gallery_bird_is_new_region(bird):
            label = f"{label} · new to region"
        name_col, compare_col = st.columns(
            [6, 1], vertical_alignment="center"
        )
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
        with compare_col:
            main_bird_in_compare = is_in_compare_list(bird)
            if main_bird_in_compare:
                if st.button(
                    "−",
                    use_container_width=True,
                    key=f"main_compare_remove_{gallery_bird_key(bird)}",
                    help="Remove from compare list",
                    type="tertiary",
                ):
                    remove_compare_bird(bird)
                    st.rerun()
            elif st.button(
                "+",
                use_container_width=True,
                key=f"main_compare_add_{gallery_bird_key(bird)}",
                help="Add to compare list",
                type="tertiary",
            ):
                add_compare_bird(bird)
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

        img_prev, img_pos, img_next = st.columns([1, 2, 1], vertical_alignment="center")
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
        with img_pos:
            st.markdown(
                f"<div style='text-align:center'>Image {image_index + 1}/{len(photos)}</div>",
                unsafe_allow_html=True,
            )
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
        st.subheader("Similar birds")
        st.caption(
            "Species often confused with this one on iNaturalist. "
            f"Regional last-seen uses eBird data for {region_code}."
        )
        if "gallery_hide_similar_never_seen" not in st.session_state:
            st.session_state.gallery_hide_similar_never_seen = True
        hide_never_seen = st.checkbox(
            f"Hide species never recorded in {region_code}",
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
                set_busy_cursor(True)
                similar = enrich_similar_with_region_history(similar, region_code)
                st.session_state.pop("ebird_rate_limit_active", None)
                set_busy_cursor(False)
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
                            if item.get("image_url"):
                                st.image(item["image_url"], width="stretch")
                            similar_bird = similar_item_to_bird(item)
                            name = similar_bird["name"]
                            sci_name = similar_bird["sciName"]
                            st.markdown(f"**{name}**")
                            if sci_name and sci_name != name:
                                st.caption(sci_name)
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
                            if st.button(
                                "Open in gallery",
                                key=f"similar_open_{bird_index}_{taxon_key}",
                                use_container_width=True,
                            ):
                                open_gallery(
                                    [similar_bird],
                                    title=f"Similar bird · {name}",
                                )

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


def enrich_checklists(
    client: EBirdClient,
    rows: list[dict],
    region_life: dict[str, set[str]] | None,
    world_life: dict[str, set[str]] | None = None,
) -> list[dict]:
    """Attach species names and life-list-new counts to checklist summaries."""
    details: dict[str, dict] = {}
    codes: list[str] = []
    for row in rows:
        sub_id = str(row.get("subId") or row.get("subID") or "")
        if not sub_id:
            continue
        detail = client.checklist(sub_id)
        details[sub_id] = detail
        for obs in detail.get("obs") or []:
            code = obs.get("speciesCode")
            if code:
                codes.append(str(code))

    taxa_by_code = client.species_taxa(codes) if codes else {}
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
            taxon_for_match = taxon or {"comName": common}
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
        enriched.append(
            {
                **row,
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


def ensure_api_key() -> bool:
    """Return True when an API key is available; otherwise prompt for one."""
    if get_api_key():
        return True

    st.info(
        "An eBird API key is required. "
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
            return False
        st.session_state.ebird_api_key = cleaned
        st.rerun()
    return False


def render_checklists() -> None:
    st.title("Checklists")
    st.caption(
        "Pick a region, choose a top hotspot, then browse recent checklists. "
        "New-bird counts use `lifeLists/ebird_world_life_list.csv` and "
        "`lifeLists/ebird_<region>_life_list.csv`."
    )

    if not ensure_api_key():
        return

    default_region = os.environ.get("EBIRD_HOME_REGION", "US-FL-099")
    region_code = st.text_input(
        "Region code",
        value=st.session_state.get("checklists_region", default_region),
        help="eBird region, e.g. US-FL-099, US-FL, or US",
    ).strip()

    world_life = load_life_list(WORLD_LIFE_LIST_CODE)
    region_life = load_life_list(region_code) if region_code else None
    world_total = life_list_total(world_life)
    region_total = life_list_total(region_life)

    total_cols = st.columns(2)
    with total_cols[0]:
        if world_total is None:
            st.warning(
                f"No world life list at `{life_list_path(WORLD_LIFE_LIST_CODE)}`."
            )
        else:
            st.metric("World life list", f"{world_total} species")
            if st.button(
                "Open gallery",
                key="gallery_world_life_list",
                use_container_width=True,
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
            st.metric(f"Region life list ({region_code})", f"{region_total} species")
            if st.button(
                "Open gallery",
                key="gallery_region_life_list",
                use_container_width=True,
            ):
                open_life_list_gallery(
                    region_code,
                    title=f"Region life list gallery · {region_code}",
                )
        else:
            st.metric("Region life list", "—")

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
        open_region_species_gallery(region_code)

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
        help="Controls which birds count as “new” in the summary, checklists, and gallery.",
    )

    if st.button("Load hotspots", type="primary"):
        if not region_code:
            st.warning("Enter a region code.")
            return
        with st.spinner(f"Loading top hotspots for {region_code}…"):
            try:
                hotspots = EBirdClient().top_hotspots(region_code, limit=100)
            except requests.HTTPError as exc:
                st.error(
                    f"eBird API error: {exc.response.status_code if exc.response else exc}"
                )
                return
            except Exception as exc:
                st.error(str(exc))
                return
        st.session_state.checklists_region = region_code
        st.session_state.checklists_hotspots = hotspots
        st.session_state.pop("checklist_rows", None)
        st.session_state.pop("checklist_summaries", None)
        st.session_state.pop("checklist_shown", None)
        hotspot_ids = [h["locId"] for h in hotspots if h.get("locId")]
        st.session_state.checklists_loc_id = (
            DEFAULT_HOTSPOT_ID if DEFAULT_HOTSPOT_ID in hotspot_ids else hotspot_ids[0]
            if hotspot_ids
            else None
        )

    hotspots = st.session_state.get("checklists_hotspots")
    if not hotspots:
        st.info("Enter a region and click Load hotspots.")
        return

    if st.session_state.get("checklists_region") != region_code:
        st.warning("Region changed — click Load hotspots to refresh the list.")

    loc_ids = [h["locId"] for h in hotspots if h.get("locId")]
    labels = {h["locId"]: hotspot_label(h) for h in hotspots if h.get("locId")}
    current_loc = st.session_state.get("checklists_loc_id")
    if current_loc not in loc_ids:
        current_loc = DEFAULT_HOTSPOT_ID if DEFAULT_HOTSPOT_ID in loc_ids else loc_ids[0]
        st.session_state.checklists_loc_id = current_loc
    index = loc_ids.index(current_loc)

    loc_id = st.selectbox(
        "Hotspot (top 100 by all-time species)",
        options=loc_ids,
        index=index,
        format_func=lambda lid: labels.get(lid, lid),
    )
    end_date = st.date_input(
        "Last observation day",
        value=st.session_state.get("checklist_end_date", date.today()),
        max_value=date.today(),
        help="Include checklists on this day and the prior days in the range.",
    )
    days_back = st.slider("Days to include", min_value=1, max_value=30, value=7)
    page_size = 50

    if st.button("Show checklists"):
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
                )
            except requests.HTTPError as exc:
                st.error(
                    f"eBird API error: {exc.response.status_code if exc.response else exc}"
                )
                return
            except Exception as exc:
                st.error(str(exc))
                return
        st.session_state.checklists_loc_id = loc_id
        st.session_state.checklist_days = days_back
        st.session_state.checklist_end_date = end_date
        st.session_state.checklist_summaries = summaries
        st.session_state.checklist_rows = first_page
        st.session_state.checklist_shown = len(first_page)
        st.session_state.checklist_life = life_for_region
        st.session_state.checklist_world_life = life_for_world
        st.session_state.checklist_hotspot_name = labels.get(loc_id, loc_id)

    rows = st.session_state.get("checklist_rows")
    if rows is None:
        return

    summaries = st.session_state.get("checklist_summaries") or []
    shown = st.session_state.get("checklist_shown", len(rows))
    total = len(summaries)
    hotspot_name = st.session_state.get(
        "checklist_hotspot_name", labels.get(loc_id, loc_id)
    )
    stored_end = st.session_state.get("checklist_end_date", end_date)
    stored_days = st.session_state.get("checklist_days", days_back)
    stored_start = stored_end - timedelta(days=stored_days - 1)
    st.write(
        f"Showing **{len(rows)}** of **{total}** checklist(s) at **{hotspot_name}** "
        f"from **{stored_start.isoformat()}** to **{stored_end.isoformat()}**."
    )

    if not rows and total == 0:
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
                try:
                    more = enrich_checklists(
                        EBirdClient(),
                        summaries[start:end],
                        life_for_region,
                        life_for_world,
                    )
                except requests.HTTPError as exc:
                    st.error(
                        f"eBird API error: "
                        f"{exc.response.status_code if exc.response else exc}"
                    )
                    return
                except Exception as exc:
                    st.error(str(exc))
                    return
            st.session_state.checklist_rows = rows + more
            st.session_state.checklist_shown = end
            st.rerun()

    loaded_rows = st.session_state.get("checklist_rows", [])
    species_summary = build_species_summary(loaded_rows)
    if species_summary:
        st.subheader("Species summary")
        st.caption(
            "Across currently loaded checklists: max count on any one list, "
            "and how many of those lists included the species."
        )
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
            if st.button("Open gallery from summary", type="primary", key="gallery_from_summary"):
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
            for item in filtered:
                photo_col, text_col = st.columns([1, 5], vertical_alignment="center")
                with photo_col:
                    render_species_photo(
                        item.get("code"),
                        scientific_name=item.get("sciName") or None,
                        width=64,
                    )
                with text_col:
                    marker = new_bird_marker(
                        bool(item.get("New_region")),
                        bool(item.get("New_world")),
                        scope=life_scope,
                    )
                    if life_scope != "all":
                        marker = ""
                    st.markdown(
                        f"**{item['Species']}**{marker}  \n"
                        f"Max count: {item['Max count']} · "
                        f"Checklists: {item['Checklists']}"
                    )
            st.caption(f"{len(filtered)} species in summary.")

    st.subheader("Checklists")
    for row in loaded_rows:
        sub_id = row.get("subId") or row.get("subID")
        date_label = " ".join(
            part for part in [row.get("obsDt"), row.get("obsTime")] if part
        )
        species = row.get("numSpecies", "?")
        observer = row.get("userDisplayName") or "Unknown observer"
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

        with st.container(border=True):
            st.markdown(
                f"**[{date_label}]({checklist_url})** · {species} species · "
                f"{observer}{new_label}"
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


st.set_page_config(page_title="Birds", page_icon="🪶", layout="centered")
get_api_key()  # ingest ?EBIRD_API_KEY=… into session when present
render_ebird_rate_limit_notices()
if st.session_state.get("gallery_birds"):
    render_gallery()
else:
    render_checklists()
