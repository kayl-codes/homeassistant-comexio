# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A HACS custom integration for [Comexio IO-Server](https://www.comexio.com/) — a local building-automation controller. It exposes Comexio Markers and physical IOs as Home Assistant entities, and manages a "Web-IO" webhook class on the Comexio server so it can push live values back to HA.

- **HACS domain:** `comexio`
- **IoT class:** Local Push (webhooks from Comexio → HA)
- **Minimum HA:** 2026.8.0
- **Python:** ≥ 3.12

## Development commands

```bash
# Lint
ruff check .

# Format
ruff format .

# Format check only (CI mode)
ruff format --check .

# Pre-commit (runs ruff + whitespace/YAML/JSON checks)
pre-commit run --all-files
```

There is no automated test suite. Manual testing requires a live HA + Comexio instance.

## Code quality rules

- Line length: 120 (see `pyproject.toml`)
- Ruff rule sets: B, C4, E, F, I, SIM, UP, W
- Sourcery enabled for all files except `.github/` and `tests/`
- Cognitive complexity ≤ 15; no duplicated string literals (extract constants)

## Terminology: "function plan"

One concept, three historical names — the rules are:

- **function plan** — the canonical English term everywhere: code identifiers (`function_plan_*` / `FUNCTION_PLAN_*` / `FunctionPlan*`), services, entity names, repair dialogs, notifications, logs, README (EN), en/fr/es translations. It matches Comexio's own FUP/FUB vocabulary (`run_fup`, `fub_id` — Funktionsplan, IEC 61131-3 Function Block Diagram).
- **Logikplan** — the correct German *translation* only: `de.json` and the German README section. Never in English UI text or code.
- **`logikplan_*`** (lowercase German spelling) — frozen legacy spelling of *persisted* identifiers: the five option keys in `const.py`, the `Store` storage keys (backup + catalog), and the plan-selector unique_id `comexio_{server_id}_logikplan_plan_selector`. Never rename these without migration code.
- The former internal code name `logicplan` was fully renamed to `function_plan` (2026-07); do not reintroduce it.

## Architecture overview

All integration code lives in `custom_components/comexio/`.

### Data flow

```
Comexio IO-Server
    │  RSA login (admin session)          GET /admin/function_function_module/home
    │  Live states (markers)              POST /board/dashboard/refresh/
    │  Web-IO write/delete (sync)         POST /admin/web_io/...
    │  API write (values)                 GET /api/?action=set&...  (Basic Auth)
    │
    ▼
ComexioAPI (api.py)
    │  parse_config()  →  markers[], io[], webio_commands{}
    │
    ▼
ComexioCoordinator (coordinator.py)   ← DataUpdateCoordinator
    │  _async_update_data() polls on schedule (default 15 min)
    │  Smart Audit: compares HA entity map vs. Comexio Web-IO commands
    │    → creates HA Repair issue if mismatch detected
    │  update_marker() / update_io_by_name()  ← called by webhook handler
    │
    ├── Entities: sensor / switch / number / button / binary_sensor
    └── Webhook: POST /api/webhook/comexio_{server_id}  ← push from Comexio
```

### Module roles

| File | Role |
|------|------|
| `__init__.py` | Entry setup, webhook registration, orphan entity cleanup, `update_listener` |
| `api.py` | `ComexioAPI`: RSA login, config HTML scraping/parsing, full Web-IO lifecycle |
| `coordinator.py` | `ComexioCoordinator`: polling, audit logic, webhook state merging, sync lock |
| `button.py` | Sync button (delta vs. recreate strategy), cancel button, `press_action` service |
| `repairs.py` | HA Repairs flow — lets user pick audit fix action from the UI |
| `config_flow.py` | Config entry setup wizard with auto-discovery (DNS `comexio.*`) |
| `options_flow.py` | Reconfigure existing entry (credentials, schema, intervals, flags) |
| `sensor.py` | Analog IOs as `SensorEntity` + `ComexioSyncStatusSensor` (diagnostic) |
| `switch.py` | Digital markers/IOs as switches |
| `binary_sensor.py` | Digital IOs as binary sensors |
| `number.py` | Analog markers/IOs as number entities |
| `services.py` | `generate_web_io` service |
| `const.py` | All constants; `KNOWN_DOMAINS` list drives HA IP / DNS resolution |

### Two Comexio data types

- **Markers** (`FubModules["2"]`): server-side state variables (digital or analog). Live values fetched via dashboard refresh endpoint. IDs are integers; webhook payload `{"type": "marker", "id": "...", "value": ...}`.
- **IOs** (`FubModules["1"]`): physical inputs/outputs on extension modules. Whether binary or analog is determined by `$ioTypes` (scraped from admin page). Webhook payload `{"type": "io", "ext": "...", "io": "...", "value": ...}`.

### Two authentication paths

- **Admin session (RSA):** `api.login()` — uses RSA PKCS1v15 to encrypt credentials, stores the cookie. Required for config scraping and Web-IO management.
- **API (Basic Auth):** `api.set_value()` — used for writing values to markers and IOs. Configured separately via `CONF_API_USERNAME` / `CONF_API_PASSWORD`.

### Sync strategy (button.py)

The sync button (`ComexioSyncButton.async_handle_press`) chooses between two strategies:

- **Delta-Sync:** Targeted create/rename/delete/type-fix of individual Web-IO commands via `save_single_command` / `delete_single_command`. Used when action ETA < `SYNC_DURATION_RECREATE` (~79 s).
- **Full-Recreate (Fast-Track):** Deletes entire device + class, uploads fresh JSON template via `upload_web_io`, then creates device instance. Used when Delta ETA > threshold *and* device is not linked in Comexio logic.

The coordinator's `last_audit_results` dict (populated every poll) drives both strategies. The sync lock (`_sync_lock`) prevents concurrent runs.

### Unique ID scheme

| Entity type | Pattern |
|-------------|---------|
| Marker entity | `comexio_{server_id}_m{id}` |
| IO entity | `comexio_{server_id}_{ext_name}_{identifier}` |
| Sync button | `comexio_{server_id}_webio_sync_start_btn` |
| Cancel button | `comexio_{server_id}_webio_sync_cancel_btn` |
| Status sensor | `comexio_{server_id}_webio_sync_status_sensor` |

### Entity naming (configurable)

Both `schema_marker` and `schema_io` are `str.format_map(SafeDict(...))` templates. `SafeDict` (in `api.py`) leaves unknown `{keys}` unchanged. Default schemas:

- Marker: `"M{MarkerId} {MarkerTitle}"`
- IO: `"{ExtName} {IoId} {IoTitle}"`
- Available placeholders: `ServerAlias`, `MarkerId`, `MarkerTitle`, `ExtName`, `IoId`, `IoTitle`

### Race-condition guards (see inline comments)

- **R1** (`coordinator.py`): Webhook updates arriving *during* a `get_raw_config` HTTP round-trip are preserved — coordinator clears dirty sets before the fetch, then prefers webhook values over stale API snapshot.
- **R2** (`__init__.py`, `button.py`): `_skip_next_listener_reload` flag prevents a double-reload when the sync button writes to `audit_ignored` in options (the explicit reload after sync is the canonical one).
- **R4** (`coordinator.py`): `_sync_lock` (asyncio.Lock) ensures only one sync run at a time.

### GitHub Actions — HACS-spezifische Ausnahme

`home-assistant/actions/hassfest@master` und `hacs/action@main` **müssen** auf ihren Branch-Refs bleiben (kein SHA-Pinning) — weil Pinning die Validierung einfrieren würde. Die daraus resultierende SonarQube-Warnung S7637 ist als **"Safe"** in SonarCloud zu markieren.

### Translations

`translations/strings.json` is the authoritative source; `en.json`, `de.json`, `fr.json`, `es.json` mirror it. Add new translation keys to all files. The repair flow reads `hass.config.language` for inline strings not covered by the HA translation system.
