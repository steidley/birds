"""Console logging for outbound HTTP API calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode


def _now_stamp() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]


def _format_value(value: Any, *, max_len: int = 120) -> str:
    if value is None:
        return "None"
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        preview = ",".join(str(item) for item in items[:8])
        if len(items) > 8:
            preview = f"{preview},…(+{len(items) - 8})"
        text = f"[{preview}]"
    else:
        text = str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def format_params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    parts = [
        f"{key}={_format_value(value)}"
        for key, value in params.items()
        if value is not None
    ]
    return " ".join(parts)


def log_api_send(
    service: str,
    summary: str,
    *,
    url: str | None = None,
    params: dict[str, Any] | None = None,
    attempt: int | None = None,
    **details: Any,
) -> None:
    """Log an outbound API call at send time."""
    bits = [f"[api] {_now_stamp()} → {service}: {summary}"]
    if attempt is not None:
        bits.append(f"attempt={attempt}")
    detail_text = format_params({**details, **(params or {})})
    if detail_text:
        bits.append(detail_text)
    if url:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        bits.append(f"url={url}{query}")
    print(" ".join(bits), flush=True)


def log_api_done(
    service: str,
    summary: str,
    *,
    started: float,
    status: int | None = None,
    **details: Any,
) -> None:
    """Log completion timing for an outbound API call."""
    import time

    elapsed_ms = (time.perf_counter() - started) * 1000
    bits = [
        f"[api] {_now_stamp()} ← {service}: {summary}",
        f"{elapsed_ms:.0f}ms",
    ]
    if status is not None:
        bits.append(f"status={status}")
    detail_text = format_params(details)
    if detail_text:
        bits.append(detail_text)
    print(" ".join(bits), flush=True)
