"""Clickable map that reports dropped-pin coordinates to Streamlit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "drop_pin_map",
    path=str(Path(__file__).parent),
)


def drop_pin_map(
    *,
    lat: float | None = None,
    lng: float | None = None,
    zoom: int = 14,
    height: int = 360,
    key: str | None = None,
) -> dict[str, Any] | None:
    """Render a street map; click to drop/move a pin.

    Returns ``{"lat": float, "lng": float, "t": int}`` when the user clicks.
    """
    has_pin = lat is not None and lng is not None
    return _COMPONENT(
        lat=float(lat) if has_pin else None,
        lng=float(lng) if has_pin else None,
        has_pin=has_pin,
        zoom=int(zoom),
        height=int(height),
        key=key,
        default=None,
    )
