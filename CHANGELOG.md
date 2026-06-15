# Changelog

All notable changes to this project are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### ✨ New Features
- **Offline Extension Detection:** Extension modules that are offline (no dash-separated serial in the `Identifier` field) are now detected automatically on every coordinator poll.
- **Sub-device Auto-Removal:** When an extension module goes offline, all its entities are excluded from entity creation and removed from the HA entity registry. The corresponding sub-device is then deleted from the HA device registry so it disappears entirely from the device list — no stale "unavailable" entries.
- **Diagnostic Sensor — Offline Extensions:** A new `Offline Extensions` sensor is added to the hub device. It shows the count of currently offline extensions and exposes the list via `extensions` state attribute.
- **Orphaned Statistics Repair on Startup:** After entity cleanup, the integration now immediately re-runs orphaned-statistics detection so that a HA Repair issue is raised right away (previously only on the next scheduled poll, up to 15 minutes later).

### 🛠️ Core & Stability Improvements
- `state_class` returns `None` for sensors belonging to an offline extension, preventing recorder unit-mismatch warnings when the extension goes offline with an incomplete `$ioTypes` map.
- Transition logging: coordinator logs which extensions went offline / came back online on every state change.
- `manifest.json` now declares `after_dependencies: ["recorder"]` so the recorder is guaranteed to be available before the integration checks for orphaned statistics.
- `async_migrate_entity_ids()` extracted to coordinator as a shared helper (used by both the repair flow and the migration button), reducing cognitive complexity.

---

## [0.7.5] — 2026-05-22

### ✨ New Features
- **Sub-Device Grouping:** Physical extension modules (e.g. `iosrv1 RC1`) are now registered as individual sub-devices in Home Assistant, making the device page much cleaner.
- **Server-ID Prefix for Sub-Devices:** All sub-device names are prefixed with the server ID to ensure consistent and predictable sorting across multiple servers.
- **Entity-ID Migration:** A `Fix Entity IDs` button and a HA Repair issue detect and auto-correct duplicated server-ID prefixes in entity IDs left behind by older versions.
- **Orphaned Statistics Cleanup:** A `Clean Up Statistics` button and a HA Repair issue detect and remove long-term statistics entries that no longer have a matching entity (e.g. after a rename or removal).
- **CI / Quality Gate:** Added GitHub Actions workflows for `hassfest`, `HACS`, Bandit, CodeQL, and SonarCloud. Quality Gate must pass before merging.
- **Dynamic README Badges:** All shield badges now use shields.io dynamic sources (release, downloads, commit activity, CI status).

### 🛠️ Core & Stability Improvements
- SonarCloud code-quality findings resolved; false-positive `S7503` and `S7637` hotspots marked appropriately.
- Orphaned-statistics detection extended to cover legacy naming pattern (`sensor.comexio_server_{id}_...`).
- `async_clear_statistics` correctly awaited in both repair flow and cleanup button.

---

## [0.7.1] — 2026-05-14

### ✨ New Features
- **Custom Naming Schemas:** Define your own naming patterns for IOs and Markers in the options menu (e.g. `{ExtName} {IoId} {IoTitle}`). Uses `SafeDict` so unknown placeholders are left unchanged instead of causing a crash.
- **Core-Compliant Entity IDs:** Server alias removed from the default naming schema to leverage HA's native device-name prefixing (`has_entity_name = True`), preventing duplicate server names in `entity_id`.
- **Stable Unique IDs:** Standardised unique-ID generation across all platforms (e.g. `comexio_iosrv1_ud1_q4`) for long-term database stability.

---

## [0.7.0] — 2026-05-12

### 🐛 Bug Fixes
- **Web-IO Device Creation:** Complete overhaul of the API payload for creating Web-IO devices to strictly match Comexio requirements. Fixed missing identifiers and authentication bugs.
- **Authentication Flag:** `form_login` now correctly set to `2` ("Never") to prevent Comexio from blocking webhook execution.
- **Sync Error Handling:** Dedicated `sync_error` state; the UI button and sync sensor now reliably display an error state if Delta-Sync or upload fails.

---

## [0.6.4] — 2026-05-07

### ✨ New Features
- **Sync Status Sensor:** New `Web-IO Sync Status` sensor shows current sync mode, progress (%), and active step — visible on the device page and dashboards.
- **Diagnostic Grouping:** System-level entities (sync buttons, status sensor) grouped under the `Diagnostic` section on the HA device page.

### 🛠️ Core & Stability Improvements
- All platforms now strictly adhere to `has_entity_name = True` for clean, predictable `entity_id` generation.
- Backend UI pop-ups standardised to English to bypass HA Core's global localisation limitations.

### 🐛 Bug Fixes
- Fixed a string termination syntax error in `sensor.py`.

---

## [0.6.3] — 2026-05-06

### ✨ New Features
- **Smart Auto-Discovery:** During setup, the integration scans for known Comexio DNS hostnames (`comexio`, `comexio.local`, `comexio.fritz.box`, etc.) and pre-fills the IP address if found.
- **Dynamic Host / IP Updates:** Change the host/IP address via *Settings → Devices & Services → Comexio → Configure* without reinstalling the integration.

### 🐛 Bug Fixes
- Fixed a minor evaluation bug in the `config_flow` initialisation step.
- Extracted hardcoded fallback IPs into a centralised `DEFAULT_HOST` constant.

---

## [0.6.2] — 2026-05-06

### 🛠️ Core & Stability Improvements
- Added comprehensive static type hints across all entity platforms (`sensor`, `binary_sensor`, `switch`, `number`, `button`) for better IDE support and HA Core compliance.

---

## [0.6.1] — 2026-05-05

### 🛠️ Core & Stability Improvements
- Added local brand images (`brand/icon.png`, `brand/logo.png`) inside the integration folder. HA 2025.3+ serves these directly without an external brands proxy.

---

## [0.6.0] — 2026-05-05

### 🎉 Initial Public Release
- First HACS-compatible release. Restructured into `custom_components/comexio/` with `hacs.json`.
- Removed hardcoded default credentials from `config_flow.py`.
- Enforced Unix (LF) line endings for native Linux/HA compatibility.
- Added bilingual README (English & German) with step-by-step installation guide.

[Unreleased]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.7.5...HEAD
[0.7.5]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.7.1...0.7.5
[0.7.1]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.7.0...0.7.1
[0.7.0]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.6.4...0.7.0
[0.6.4]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.6.3...0.6.4
[0.6.3]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.6.2...0.6.3
[0.6.2]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.6.1...0.6.2
[0.6.1]: https://github.com/kayl-codes/homeassistant-comexio/compare/0.6.0...0.6.1
[0.6.0]: https://github.com/kayl-codes/homeassistant-comexio/releases/tag/0.6.0
