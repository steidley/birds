from pathlib import Path
import json

import streamlit as st

BIRD_LIST_PATH = Path(__file__).parent / "birdList"
IMAGE_CACHE_PATH = Path(__file__).parent / "bird_images.json"


def load_birds() -> list[tuple[str, str]]:
    birds: list[tuple[str, str]] = []
    for line in BIRD_LIST_PATH.read_text().splitlines():
        line = line.strip()
        if not line or " — " not in line:
            continue
        name, url = line.split(" — ", 1)
        birds.append((name.strip(), url.strip()))
    return birds


def load_image_cache() -> dict[str, list[str]]:
    if not IMAGE_CACHE_PATH.exists():
        return {}
    return json.loads(IMAGE_CACHE_PATH.read_text())


birds = load_birds()
image_cache = load_image_cache()

if "index" not in st.session_state:
    st.session_state.index = 0

st.set_page_config(page_title="Bird List", page_icon="🪶", layout="centered")
st.title("Bird List")

total = len(birds)
index = st.session_state.index
name, url = birds[index]

st.caption(f"{index + 1} of {total}")
st.header(name)

images = image_cache.get(name, [])
if images:
    cols = st.columns(len(images))
    for col, image_url in zip(cols, images):
        with col:
            st.image(image_url, use_container_width=True)
else:
    st.info("No photos cached yet. Run `python fetch_images.py`.")

st.link_button("Open on eBird", url, use_container_width=True)

col_prev, col_next = st.columns(2)
with col_prev:
    if st.button("← Previous", use_container_width=True, disabled=index == 0):
        st.session_state.index -= 1
        st.rerun()
with col_next:
    if st.button("Next →", use_container_width=True, disabled=index == total - 1):
        st.session_state.index += 1
        st.rerun()

selected = st.selectbox(
    "Jump to bird",
    options=range(total),
    index=index,
    format_func=lambda i: birds[i][0],
)
if selected != index:
    st.session_state.index = selected
    st.rerun()
