# birds

Streamlit app for eBird checklists, hotspots, and galleries.

Default screen: **Hot Spot Finder** (`?screen=hotspot_finder`).

## Deeplinks

All paths are relative to the app origin (for example `http://localhost:8502`).

### Screens (`screen`)

| URL | Opens |
| --- | --- |
| `?screen=hotspot_finder` | Hot Spot Finder (home) |
| `?screen=browse_hotspots` | Hotspots (catalog, map, cached JSON) |
| `?screen=location` | Location (GPS / street address) |
| `?screen=saved` | Saved galleries |
| `?screen=mine` | My checklists |
| `?screen=checklists` | Checklists |
| `?screen=region` | Region picker |
| `?screen=cache` | Checklist cache |
| `?screen=maintenance` | Cache maintenance |
| `?screen=gallery` | Gallery (only if a gallery is already open in the session) |

Aliases for `screen` (same destinations):

| Alias | Resolves to |
| --- | --- |
| `hotspots`, `finder`, `home` | `hotspot_finder` |
| `browse`, `hotspot_browse` | `browse_hotspots` |
| `galleries`, `saved_galleries` | `saved` |
| `my_checklists`, `my` | `mine` |

Hyphens and spaces are normalized (`hotspot-finder` → `hotspot_finder`).

Hotspots browse also accepts `hotspot=<locId>` (for example `?screen=browse_hotspots&hotspot=L127408`).

### Gallery openers

These open a gallery and set `screen=gallery`. They need matching local/session data (saved gallery id, cached checklist, or last Hot Spot Finder run).

| URL | Opens |
| --- | --- |
| `?screen=gallery&saved_gallery=<id>` | Saved gallery by id |
| `?screen=gallery&checklist_gallery=<subId>` | Checklist gallery (`S…` id) |
| `?screen=gallery&hotspot_gallery=<locId>` | Species from the last Hot Spot Finder run for that hotspot (`L…` id) |
| `?screen=gallery&summary_gallery=1` | Checklists species-summary gallery |

Optional bird focus (0-based index into the gallery list):

| Param | Example |
| --- | --- |
| `gallery_open` | `?screen=gallery&hotspot_gallery=L127408&gallery_open=3` |

### API key

| URL | Effect |
| --- | --- |
| `?EBIRD_API_KEY=<key>` | Store the eBird API key for this browser session |

### GPS bridge (internal)

Used by the Location screen GPS control to return coordinates to the app. Not meant for hand-authored bookmarks.

| Param | Meaning |
| --- | --- |
| `ors_gps_lat` | Latitude |
| `ors_gps_lng` | Longitude |
| `ors_gps_acc` | Accuracy (meters) |
| `ors_gps_t` | Timestamp / nonce |
| `ors_gps_error` | Error message when GPS fails |

Example: `?ors_gps_t=…&ors_gps_lat=26.37&ors_gps_lng=-80.17&ors_gps_acc=35`
