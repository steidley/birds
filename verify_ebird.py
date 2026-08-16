"""Verify eBird API configuration."""

import requests

from ebird import EBirdClient, get_api_key


def main() -> None:
    if not get_api_key() or get_api_key() == "your_ebird_api_key_here":
        raise SystemExit(
            "No EBIRD_API_KEY found.\n"
            "1. Get a key: https://ebird.org/api/keygen\n"
            "2. Put it in .env (copy from .env.example)\n"
            "   or .streamlit/secrets.toml for Streamlit"
        )

    try:
        info = EBirdClient().verify()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise SystemExit(
            f"eBird API request failed ({status}). Check that EBIRD_API_KEY is valid."
        ) from exc

    detail = info.get("comName") or info.get("detail") or "authenticated"
    print(f"eBird API OK — {detail}")


if __name__ == "__main__":
    main()
