"""Header settings gear that opens Settings in a separate browser window."""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "settings_opener",
    path=str(Path(__file__).parent),
)


def settings_opener(
    *,
    screen: str = "default",
    region: str = "",
    help_text: str = "Settings",
    key: str | None = None,
) -> None:
    """Render a compact settings icon that opens ``?screen=settings`` in a popup."""
    _COMPONENT(
        screen=str(screen or "default"),
        region=str(region or ""),
        help_text=str(help_text or "Settings"),
        key=key,
        default=None,
    )
