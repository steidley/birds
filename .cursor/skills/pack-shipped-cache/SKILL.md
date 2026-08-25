---
name: pack-shipped-cache
description: >-
  Pack shippable cache files into zip sidecars before a git check-in, or extract
  them for local runs. Use when committing cache/ updates, preparing a PR that
  touches cache/shared or region hotspots/last-seen/checklists, or when the
  user asks to zip/pack shipped caches.
---

# Pack shipped cache

Before committing changes under `cache/` that are meant to ship with the repo,
refresh the zip sidecars so git tracks compressed archives instead of large JSON.

## Command

From the repo root:

```bash
./.venv/bin/python scripts/pack_shipped_cache.py pack
```

Force rewrite even when timestamps look fresh:

```bash
./.venv/bin/python scripts/pack_shipped_cache.py pack --force
```

Extract for a clean checkout / local run (the Streamlit app also does this on startup):

```bash
./.venv/bin/python scripts/pack_shipped_cache.py extract
```

## What gets packed

| Working path | Git artifact |
| --- | --- |
| `cache/shared/*.json` (shipped list) | `cache/shared/*.json.zip` |
| `cache/<region>/local_last_seen.json` | `…/local_last_seen.json.zip` |
| `cache/<region>/hotspots.json` | `…/hotspots.json.zip` |
| `cache/US-FL-099/checklists/{2025,2026,2026-08}/L364884/` | `…/L364884.zip` |

Logic lives in `cache_ship.py` (`pack_shipped_caches` / `ensure_shipped_caches_extracted`).

## Check-in steps

1. Update or regenerate the working JSON / checklist files as usual.
2. Run `scripts/pack_shipped_cache.py pack`.
3. Stage the `.zip` files (and any code). Do **not** stage expanded JSON / checklist trees — they are gitignored.
4. Commit.

If a zip is missing after a fresh clone, run `extract` or start the app once.

Extract never overwrites an on-disk cache that is **larger** than the zip
payload (local gallery / last-seen growth is preserved).
