"""Touch/mouse swipeable image for the bird gallery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "swipe_image",
    path=str(Path(__file__).parent),
)


def swipe_image(
    image_url: str,
    *,
    height: int = 420,
    show_hint: bool = True,
    key: str | None = None,
) -> dict[str, Any] | None:
    """Render an image that reports swipe gestures.

    Returns ``{"action": "...", "t": <ms>}`` when the user swipes:
      - image_next / image_prev for horizontal swipes
      - bird_next / bird_prev for vertical swipes
    """
    return _COMPONENT(
        image_url=image_url,
        height=height,
        show_hint=show_hint,
        key=key,
        default=None,
    )
