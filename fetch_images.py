"""Fetch the first 4 Macaulay Library photo URLs from each eBird species page."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
BIRD_LIST_PATH = ROOT / "birdList"
CACHE_PATH = ROOT / "bird_images.json"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ASSET_RE = re.compile(
    r"https://cdn\.download\.ams\.birds\.cornell\.edu/api/v1/asset/(\d+)/"
)
IMAGE_URL = "https://cdn.download.ams.birds.cornell.edu/api/v1/asset/{asset_id}/640"


def load_birds() -> list[tuple[str, str]]:
    birds: list[tuple[str, str]] = []
    for line in BIRD_LIST_PATH.read_text().splitlines():
        line = line.strip()
        if not line or " — " not in line:
            continue
        name, url = line.split(" — ", 1)
        birds.append((name.strip(), url.strip()))
    return birds


def dump_dom(url: str) -> str:
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--virtual-time-budget=8000",
            "--dump-dom",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def extract_image_urls(html: str, limit: int = 4) -> list[str]:
    seen: list[str] = []
    for match in ASSET_RE.finditer(html):
        asset_id = match.group(1)
        if asset_id in seen:
            continue
        seen.append(asset_id)
        if len(seen) >= limit:
            break
    return [IMAGE_URL.format(asset_id=asset_id) for asset_id in seen]


def main() -> None:
    birds = load_birds()
    cache: dict[str, list[str]] = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())

    for i, (name, url) in enumerate(birds, start=1):
        if name in cache and len(cache[name]) >= 4:
            print(f"[{i}/{len(birds)}] skip {name}")
            continue
        print(f"[{i}/{len(birds)}] fetch {name} …", flush=True)
        html = dump_dom(url)
        images = extract_image_urls(html)
        cache[name] = images
        CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")
        print(f"  → {len(images)} images")

    print(f"Wrote {CACHE_PATH}")


if __name__ == "__main__":
    main()
