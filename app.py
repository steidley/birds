from pathlib import Path
import csv
import os
from datetime import date, timedelta

import requests
import streamlit as st
from dotenv import load_dotenv

from ebird import EBirdClient, get_api_key
from inaturalist import species_gallery, species_photo

load_dotenv(Path(__file__).parent / ".env")

LIFE_LISTS_DIR = Path(__file__).parent / "lifeLists"
DEFAULT_HOTSPOT_ID = os.environ.get("EBIRD_DEFAULT_HOTSPOT", "L364884")
WORLD_LIFE_LIST_CODE = "world"


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
def gallery_payload_for_code(
    ebird_code: str,
    scientific_name: str | None = None,
    max_photos: int = 99,
) -> dict | None:
    """Resolve gallery photos and species info for an eBird code."""
    lookup = (ebird_code or "").strip() or (scientific_name or "").strip()
    if not lookup:
        return None
    try:
        return species_gallery(
            lookup,
            scientific_name=scientific_name or None,
            max_photos=max_photos,
        )
    except requests.RequestException:
        return None


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
        cleaned.append(
            {
                "code": code,
                "name": name.split(" (", 1)[0].strip() or name,
                "sciName": bird.get("sciName") or "",
                "is_new": bool(bird.get("is_new") or bird.get("New")),
            }
        )
    if not cleaned:
        st.warning("No birds available for the gallery.")
        return
    st.session_state.gallery_birds = cleaned
    st.session_state.gallery_title = title
    st.session_state.gallery_bird_index = 0
    st.session_state.gallery_image_index = 0
    st.session_state.gallery_show_info = False
    st.rerun()


def render_gallery() -> None:
    birds = st.session_state.get("gallery_birds") or []
    if not birds:
        st.session_state.pop("gallery_birds", None)
        render_checklists()
        return

    title = st.session_state.get("gallery_title", "Gallery")
    bird_index = int(st.session_state.get("gallery_bird_index", 0))
    bird_index = max(0, min(bird_index, len(birds) - 1))
    bird = birds[bird_index]

    if st.button("← Back to checklists"):
        for key in (
            "gallery_birds",
            "gallery_title",
            "gallery_bird_index",
            "gallery_image_index",
            "gallery_show_info",
        ):
            st.session_state.pop(key, None)
        st.rerun()

    st.title(title)
    st.caption(f"Bird {bird_index + 1} of {len(birds)}")

    payload = gallery_payload_for_code(
        bird.get("code") or "",
        bird.get("sciName") or None,
    )
    photos = (payload or {}).get("photos") or []
    common = (payload or {}).get("common_name") or bird.get("name") or "Unknown"
    sci = (payload or {}).get("scientific_name") or bird.get("sciName") or ""

    nav_prev, nav_name, nav_next = st.columns([1, 4, 1], vertical_alignment="center")
    with nav_prev:
        if st.button(
            "◀ Bird",
            use_container_width=True,
            disabled=bird_index == 0,
            key="gallery_prev_bird",
        ):
            st.session_state.gallery_bird_index = bird_index - 1
            st.session_state.gallery_image_index = 0
            st.session_state.gallery_show_info = False
            st.rerun()
    with nav_name:
        label = common
        if bird.get("is_new"):
            label = f"{label} · new"
        if st.button(label, use_container_width=True, key="gallery_open_info"):
            st.session_state.gallery_show_info = not st.session_state.get(
                "gallery_show_info", False
            )
            st.rerun()
        if sci:
            st.caption(sci)
    with nav_next:
        if st.button(
            "Bird ▶",
            use_container_width=True,
            disabled=bird_index >= len(birds) - 1,
            key="gallery_next_bird",
        ):
            st.session_state.gallery_bird_index = bird_index + 1
            st.session_state.gallery_image_index = 0
            st.session_state.gallery_show_info = False
            st.rerun()

    if not photos:
        st.info("No iNaturalist photos found for this species.")
    else:
        image_index = int(st.session_state.get("gallery_image_index", 0))
        image_index = max(0, min(image_index, len(photos) - 1))
        photo = photos[image_index]

        img_prev, img_pos, img_next = st.columns([1, 2, 1])
        with img_prev:
            if st.button(
                "← Swipe",
                use_container_width=True,
                disabled=image_index == 0,
                key="gallery_prev_image",
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
                "Swipe →",
                use_container_width=True,
                disabled=image_index >= len(photos) - 1,
                key="gallery_next_image",
            ):
                st.session_state.gallery_image_index = image_index + 1
                st.rerun()

        st.image(photo["image_url"], width="stretch")
        credit_bits = [
            photo.get("source") or "iNaturalist",
            photo.get("attribution") or photo.get("author"),
            photo.get("license"),
        ]
        st.caption(
            f"Photo {image_index + 1} of {len(photos)} · "
            + " · ".join(bit for bit in credit_bits if bit)
        )

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
                            "is_new": summary_is_new_for_scope(item, life_scope)
                            if life_scope != "all"
                            else (
                                item.get("New_world") or item.get("New_region")
                            ),
                            "New": summary_is_new_for_scope(item, life_scope)
                            if life_scope != "all"
                            else item.get("New_region"),
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
                            "is_new": obs_is_new_for_scope(obs, life_scope)
                            if life_scope != "all"
                            else (
                                obs.get("is_new_world") or obs.get("is_new_region")
                                or obs.get("is_new")
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
if st.session_state.get("gallery_birds"):
    render_gallery()
else:
    render_checklists()
