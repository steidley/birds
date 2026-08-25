"""Browser GPS button (Geolocation API → Streamlit via URL query params)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "current_location",
    path=str(Path(__file__).parent),
)

# Query params written by the component after navigator.geolocation succeeds.
GPS_QUERY_LAT = "ors_gps_lat"
GPS_QUERY_LNG = "ors_gps_lng"
GPS_QUERY_ACC = "ors_gps_acc"
GPS_QUERY_ERROR = "ors_gps_error"
GPS_QUERY_T = "ors_gps_t"
GPS_QUERY_KEYS = (
    GPS_QUERY_LAT,
    GPS_QUERY_LNG,
    GPS_QUERY_ACC,
    GPS_QUERY_ERROR,
    GPS_QUERY_T,
)


def current_location_button(
    *,
    label: str = "Reset location from GPS",
    key: str | None = None,
) -> dict[str, Any] | None:
    """Render a button that requests the browser Geolocation API.

    The iframe writes ``ors_gps_*`` query params on the parent URL (and also
    returns a component value). Prefer consuming the query params in Python.
    """
    return _COMPONENT(
        label=label,
        key=key,
        default=None,
    )
