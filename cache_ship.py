"""Pack and extract shippable cache files for git (zip sidecars).

Tracked in git: ``*.json.zip`` and checklist-dir ``*.zip``.
Working copies: expanded JSON / checklist directories (gitignored).

Run ``scripts/pack_shipped_cache.py`` before committing cache updates.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from typing import Any, Iterable

from ebird import CACHE_DIR, CACHE_SHARED_DIR

# Shared JSON caches that ship with the repo (zipped).
SHIPPED_SHARED_JSON = (
    "birdnet_code_cache.json",
    "ebird_region_list_cache.json",
    "ebird_region_species_cache.json",
    "ebird_taxonomy_cache.json",
    "inaturalist_cache.json",
    "inaturalist_gallery_cache.json",
    "inaturalist_similar_cache.json",
)

# Duda Farms sample checklists for key-free deploy tests.
SHIPPED_CHECKLIST_DIRS = (
    Path("US-FL-099/checklists/2025/L364884"),
    Path("US-FL-099/checklists/2026/L364884"),
    Path("US-FL-099/checklists/2026-08/L364884"),
)

COMPRESS_LEVEL = 9


def json_zip_path(json_path: Path) -> Path:
    """Sidecar zip path for a single JSON cache file."""
    return Path(f"{json_path}.zip")


def dir_zip_path(directory: Path) -> Path:
    """Sidecar zip path for a shipped checklist directory."""
    return Path(f"{directory}.zip")


def iter_shipped_region_json() -> list[Path]:
    """``local_last_seen.json`` / ``hotspots.json`` that currently exist on disk."""
    if not CACHE_DIR.exists():
        return []
    found: list[Path] = []
    for region_dir in sorted(CACHE_DIR.iterdir()):
        if (
            not region_dir.is_dir()
            or region_dir.name.startswith(".")
            or region_dir.name == "shared"
        ):
            continue
        for name in ("local_last_seen.json", "hotspots.json"):
            path = region_dir / name
            if path.is_file() or json_zip_path(path).is_file():
                found.append(path)
    return found


def shipped_json_targets() -> list[Path]:
    """All single-file JSON caches that are meant to be committed as zips."""
    targets = [CACHE_SHARED_DIR / name for name in SHIPPED_SHARED_JSON]
    targets.extend(iter_shipped_region_json())
    return targets


def shipped_checklist_dirs() -> list[Path]:
    return [CACHE_DIR / relative for relative in SHIPPED_CHECKLIST_DIRS]


def _needs_extract_file(json_path: Path, archive: Path) -> bool:
    if not archive.is_file():
        return False
    if not json_path.is_file():
        return True
    return archive.stat().st_mtime > json_path.stat().st_mtime


def _needs_extract_dir(directory: Path, archive: Path) -> bool:
    if not archive.is_file():
        return False
    if not directory.is_dir():
        return True
    try:
        next(directory.iterdir())
    except StopIteration:
        return True
    except OSError:
        return True
    newest = max(
        (path.stat().st_mtime for path in directory.rglob("*") if path.is_file()),
        default=0.0,
    )
    return archive.stat().st_mtime > newest


def _needs_pack_file(json_path: Path, archive: Path) -> bool:
    if not json_path.is_file():
        return False
    if not archive.is_file():
        return True
    return json_path.stat().st_mtime > archive.stat().st_mtime


def _needs_pack_dir(directory: Path, archive: Path) -> bool:
    if not directory.is_dir():
        return False
    files = [path for path in directory.rglob("*") if path.is_file()]
    if not files:
        return False
    if not archive.is_file():
        return True
    newest = max(path.stat().st_mtime for path in files)
    return newest > archive.stat().st_mtime


def pack_json_file(json_path: Path, *, force: bool = False) -> dict[str, Any]:
    """Write ``path.json.zip`` containing ``path.name``."""
    archive = json_zip_path(json_path)
    if not json_path.is_file():
        return {
            "path": str(json_path),
            "archive": str(archive),
            "status": "missing",
        }
    if not force and not _needs_pack_file(json_path, archive):
        return {
            "path": str(json_path),
            "archive": str(archive),
            "status": "fresh",
            "bytes_in": json_path.stat().st_size,
            "bytes_out": archive.stat().st_size,
        }
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=COMPRESS_LEVEL,
    ) as zip_file:
        zip_file.write(json_path, arcname=json_path.name)
    return {
        "path": str(json_path),
        "archive": str(archive),
        "status": "packed",
        "bytes_in": json_path.stat().st_size,
        "bytes_out": archive.stat().st_size,
    }


def pack_checklist_dir(directory: Path, *, force: bool = False) -> dict[str, Any]:
    """Write ``dir.zip`` with each file stored under its basename (flat)."""
    archive = dir_zip_path(directory)
    if not directory.is_dir():
        return {
            "path": str(directory),
            "archive": str(archive),
            "status": "missing",
        }
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        return {
            "path": str(directory),
            "archive": str(archive),
            "status": "empty",
        }
    if not force and not _needs_pack_dir(directory, archive):
        return {
            "path": str(directory),
            "archive": str(archive),
            "status": "fresh",
            "files": len(files),
            "bytes_out": archive.stat().st_size,
        }
    archive.parent.mkdir(parents=True, exist_ok=True)
    bytes_in = 0
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=COMPRESS_LEVEL,
    ) as zip_file:
        for path in files:
            bytes_in += path.stat().st_size
            # Keep relative path under the directory so nested files survive.
            arcname = path.relative_to(directory).as_posix()
            zip_file.write(path, arcname=arcname)
    return {
        "path": str(directory),
        "archive": str(archive),
        "status": "packed",
        "files": len(files),
        "bytes_in": bytes_in,
        "bytes_out": archive.stat().st_size,
    }


def extract_json_file(json_path: Path, *, force: bool = False) -> dict[str, Any]:
    """Expand ``path.json.zip`` to ``path.json`` when missing or older."""
    archive = json_zip_path(json_path)
    if not archive.is_file():
        return {
            "path": str(json_path),
            "archive": str(archive),
            "status": "no_archive",
        }
    if not force and not _needs_extract_file(json_path, archive):
        return {
            "path": str(json_path),
            "archive": str(archive),
            "status": "fresh",
        }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zip_file:
        names = zip_file.namelist()
        if not names:
            return {
                "path": str(json_path),
                "archive": str(archive),
                "status": "empty_archive",
            }
        # Prefer the expected basename; otherwise take the first file member.
        member = json_path.name if json_path.name in names else names[0]
        with zip_file.open(member) as source:
            json_path.write_bytes(source.read())
    return {
        "path": str(json_path),
        "archive": str(archive),
        "status": "extracted",
        "bytes": json_path.stat().st_size,
    }


def extract_checklist_dir(directory: Path, *, force: bool = False) -> dict[str, Any]:
    """Expand ``dir.zip`` into ``dir/``."""
    archive = dir_zip_path(directory)
    if not archive.is_file():
        return {
            "path": str(directory),
            "archive": str(archive),
            "status": "no_archive",
        }
    if not force and not _needs_extract_dir(directory, archive):
        return {
            "path": str(directory),
            "archive": str(archive),
            "status": "fresh",
        }
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zip_file:
        zip_file.extractall(directory)
    files = sum(1 for path in directory.rglob("*") if path.is_file())
    return {
        "path": str(directory),
        "archive": str(archive),
        "status": "extracted",
        "files": files,
    }


def pack_shipped_caches(*, force: bool = False) -> list[dict[str, Any]]:
    """Create/update zip sidecars for every shippable cache artifact."""
    results: list[dict[str, Any]] = []
    for path in shipped_json_targets():
        results.append(pack_json_file(path, force=force))
    for directory in shipped_checklist_dirs():
        results.append(pack_checklist_dir(directory, force=force))
    return results


def ensure_shipped_caches_extracted(*, force: bool = False) -> list[dict[str, Any]]:
    """Expand zip sidecars into working JSON / checklist directories.

    Safe to call on every app start: skips archives that are already up to date.
    """
    results: list[dict[str, Any]] = []
    # Shared files always (zip may exist even if json does not yet).
    for name in SHIPPED_SHARED_JSON:
        results.append(extract_json_file(CACHE_SHARED_DIR / name, force=force))
    # Region JSON: discover from zip sidecars and/or existing json.
    if CACHE_DIR.exists():
        seen: set[Path] = set()
        for region_dir in sorted(CACHE_DIR.iterdir()):
            if (
                not region_dir.is_dir()
                or region_dir.name.startswith(".")
                or region_dir.name == "shared"
            ):
                continue
            for name in ("local_last_seen.json", "hotspots.json"):
                path = region_dir / name
                if path in seen:
                    continue
                seen.add(path)
                if path.is_file() or json_zip_path(path).is_file():
                    results.append(extract_json_file(path, force=force))
    for directory in shipped_checklist_dirs():
        results.append(extract_checklist_dir(directory, force=force))
    return results


def _format_bytes(value: object) -> str:
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f}MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f}KB"
    return f"{int(size)}B"


def print_results(results: Iterable[dict[str, Any]], *, action: str) -> int:
    """Print a compact report. Returns count of actionable rows."""
    actionable = 0
    for row in results:
        status = str(row.get("status") or "")
        if status in {"packed", "extracted"}:
            actionable += 1
        label = Path(str(row.get("archive") or row.get("path") or "")).name
        detail_bits: list[str] = []
        if row.get("bytes_in") is not None and row.get("bytes_out") is not None:
            detail_bits.append(
                f"{_format_bytes(row['bytes_in'])} → {_format_bytes(row['bytes_out'])}"
            )
        elif row.get("bytes") is not None:
            detail_bits.append(_format_bytes(row["bytes"]))
        if row.get("files") is not None:
            detail_bits.append(f"{row['files']} files")
        detail = f" ({', '.join(detail_bits)})" if detail_bits else ""
        print(f"{action} {status:12} {label}{detail}")
    return actionable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pack or extract shippable cache zip sidecars."
    )
    parser.add_argument(
        "command",
        choices=("pack", "extract"),
        help="pack = create zips for git; extract = expand for local/runtime use",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite even when timestamps say the target is already fresh",
    )
    args = parser.parse_args(argv)
    if args.command == "pack":
        results = pack_shipped_caches(force=args.force)
        changed = print_results(results, action="pack")
        print(f"Packed {changed} archive(s); {len(results)} total targets.")
    else:
        results = ensure_shipped_caches_extracted(force=args.force)
        changed = print_results(results, action="extract")
        print(f"Extracted {changed} archive(s); {len(results)} total targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
