# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### ✨ New Features
- **Function Plan Services:** Seven new services to manage Comexio function plans (Logikpläne) directly from Home Assistant: `function_plan_connect` (wire markers to their Web-IO commands), `function_plan_sort` (sort elements by marker ID and snap to grid), `function_plan_stop` / `function_plan_activate` (plan lifecycle), `function_plan_visualize` (connection overview), `function_plan_restore` (roll back a plan to a backup snapshot), and `function_plan_list_backups` (structured service response, like a REST query).
- **Automatic Function Plan Backups:** Every coordinator poll captures a hash-delta snapshot of all function plans — per plan, SharePoint-style versioning with 3 auto slots. Before each HA-side plan modification an additional safety snapshot is stored (10 change slots per plan). Both live in HA's `.storage` and are included in normal HA backups.
- **Function Plan Restore:** Roll back structure *and* element positions of a single plan to any stored snapshot (`kind` = auto/change, `slot` = version). A pre-restore safety snapshot is taken automatically; success is verified via content hash.
- **Diagnostic Sensor — Function Plan Backups:** Shows the total number of stored snapshots; per-plan details in the state attributes.
- **Backup List as Service Response:** `function_plan_list_backups` returns all snapshots (plan, type, slot, timestamp, operation) as a response visible directly in the Actions UI — with optional filters for plan, name substring, backup type, slot, and maximum age, plus a selectable sort order (`newest` / `oldest` / `plan` / `fub_id` / `slot`).
- **Managed Cluster Plans:** Markers can be distributed automatically across managed function plans (`<prefix> - Marker [1-100]` …) with configurable plan prefix and maximum marker pairs per plan (new options). Managed plans are labelled with a comment element in Comexio.
- **Plan Selector Entity:** A `select` entity lists all function plans; services use it as the default target when no plan is specified. A dedicated `Auto (cluster plans)` option switches to cluster mode: the sync button then distributes missing markers across the managed cluster plans instead of a single fixed plan.
- **Dynamic Service Dropdowns:** Real plan names appear in the plan dropdowns of all function-plan services; the restore and list dropdowns list only plans that actually have backup snapshots. With a single Comexio instance the instance field is pre-filled automatically.
- **Ignored Markers:** Marker IDs (single values and ranges, e.g. `1-5, 12, 30-40`) can be excluded from entity creation via the options flow; entries are normalized and stale IDs are cleaned up automatically.
- **Bus Workload Monitoring:** New diagnostic `Bus Workload` sensor (%) and `SD Card Present` binary sensor, polled independently of the main coordinator every 10 seconds. The integration exposes the raw reading only — sustained-overload alerting is left to a native HA automation (`numeric_state` trigger with a `for:` duration) or a `threshold` helper, as documented in the README.
- **Extension Firmware Updates:** New read-only `update` entity per extension module (plus one for the IO-Server base) showing installed/available firmware versions. Since Comexio warns the check can briefly interrupt extension outputs, it is never polled on a schedule — it only runs once, at the next nightly 04:00 window, when the already-tracked IO-Server software version has changed since the last check. A new diagnostic "Check Firmware Now" button lets you force the check on demand (e.g. for testing), bypassing the version gate but not the underlying risk. The last checked result is persisted to survive HA restarts, so the entities don't reset to "Unknown" and the version gate doesn't spuriously re-arm just because the process restarted.

### 🛠️ Core & Stability Improvements
- **Bulk plan loading:** All function plans are fetched concurrently (semaphore-limited) in a background task after each poll — no impact on HA startup time.
- **Repair flows hardened:** Repair issue dialogs now use explicit description strings instead of `translation_key`, fixing missing texts in the Repairs UI.
- **Service context helper:** Shared resolver for instance, plan and login across all function-plan services with consistent English error notifications.
- **Multi-plan aware ignored-marker handling:** The link check and the entity cleanup now cover *all* managed plans (selected plan plus every cluster plan) instead of only the first one — markers wired in later cluster plans are detected and cleaned up correctly, with a pre-change safety snapshot per affected plan.
- **Consolidated sync notifications:** The pair-adding step updates a single progress notification (overall pair counter, elapsed time and ETA) instead of stacking one notification per plan, a "Finalizing" status bridges the gap between the last pair and the summary (activation/sort/restart phase), and the sync finishes with a detailed summary — per plan: pairs added, duration, sort/activation result.
- **Realistic "Add to Function Plan" ETA:** The repair dialog estimates the function-plan add step from the actual pair count (~1 s per pair plus a fixed finalize overhead) instead of a flat 90-second guess — 25 pairs now show ~35 s instead of ~1:30 min.
- **No sort pass for freshly created cluster plans:** New cluster plans place their marker pairs directly at the final grid positions (sorted by marker ID) and are activated automatically afterwards — the separate sort run (plus its notification) only happens when pairs are added to an already existing plan.
- **Unified "function plan" terminology:** The English UI now consistently says *function plan* (Comexio's own FUP vocabulary; previously a mix of "Logikplan" and "Logicplan" across entity names, repair dialogs, notifications and option labels). The plan selector entity is named "Function Plans", the backup selector "Function Plan Backup"; the German translation keeps "Logikplan". Persisted option/storage keys and entity unique IDs are unchanged — no migration needed.
- **Sort leaves comment elements alone:** `function_plan_sort` no longer moves the "Administrated by HomeAssistant" comment (type-14 elements keep their position), and the comment element's text width is set to "Sehr Breit" via a follow-up properties save (`savefupcommentelement`) so the full text fits on one line.

### 🐛 Bug Fixes & Refactoring
- **Cluster plan creation:** `create_fup` verified the new plan against the wrong config section (`FubModules` instead of `Fubs`) and crashed, leaving a freshly created cluster plan empty and unwired. Verification now reads the plan metadata, so auto-created cluster plans are filled in the same sync run.
- **Function-plan gap detection rebuilt:** "Missing in function plan" is now derived from the actual plan wiring (bulk `loadelements` snapshot) instead of the server's `WebCommandIoId` field — that field survives plan deletion and is not updated by API wiring, so deleting a plan left all its markers looking "wired" forever (and freshly wired pairs looked missing). Deleting a plan now correctly re-flags all affected markers on the next poll.
- **Entity naming:** The plan selector entity is now named "Function Plans" (was the duplicated "Logikplan Plan").
- **Service instance detection:** Service calls without an explicit `config_entry` no longer abort silently when webhook bookkeeping entries exist alongside the coordinator.
- **Marker lookup:** Marker IDs are compared as integers when connecting plans — explicit marker lists and `*` (all) now resolve correctly.
- **`function_plan_connect` missing safety snapshot:** Unlike every other plan-mutating service, `function_plan_connect` never took a pre-change backup snapshot before wiring markers to their Web-IO commands — a failed or unwanted run had no rollback point. It now takes one, consistent with `function_plan_sort` and the restore path.
- **Missing repair-dialog abort translations:** The `sync_mismatch` and `missing_webio_class` repair flows abort with reasons (`already_in_sync`, `entry_not_found`, `missing_entry_id`, `missing_server_id`, `sync_failed`) that had no translated text in any of the 5 language files, showing a generic fallback instead. Added to all languages; a stray, incorrectly-nested `already_in_sync` key in `de.json` (outside the `issues.*.fix_flow.abort` path HA actually reads) was also corrected.
- **Single-instance service default:** With exactly one configured Comexio instance, every service's `config_entry` field is now actually pre-filled in Developer Tools > Actions, instead of always requiring a manual pick — the behaviour that the "Dynamic Service Dropdowns" feature above already claimed but never delivered.
- **Inactive IOs unresolvable in the Function Plan preview:** IOs with `Active=False` (an extension slot the user prepared but hasn't wired up) were dropped entirely during config parsing, so a plan element referencing one showed a generic "IO ref=&lt;id&gt;" instead of its real name and never rendered greyed-out like Comexio Studio does. Inactive IOs still get no HA entity/webhook (Comexio itself refuses to wire them), but are now kept in a separate unfiltered list used for plan-preview label/geometry resolution.
- **Unlabeled-but-wired markers got no entity:** A Comexio marker without a label is normally excluded from import entirely — but one already wired into a function plan needs a real entity so the plan preview can resolve its type and live value. Such markers are now imported with a synthetic "#nn" title (matching Comexio Studio's own convention) and greyed out in the preview like an inactive IO; a later real rename in Comexio, or removal from every plan, reverts them to the normal path automatically. Detected via the same bulk plan snapshot used for the Web-IO wiring audit, with a stored-backup fallback for the first poll after a restart (before that snapshot has loaded), and an immediate extra refresh whenever the wired set changes.
- **`delete_single_command`** returns `False` on failure instead of raising, so delta-sync continues with the remaining commands.
- **Options form:** `ignored_markers` field no longer loses its stored value when reopening the options dialog.
- **Analog marker Web-IO push stopped silently at extreme values:** The generated Web-IO command for analog markers hardcoded `Min: 0, Max: 100`; Comexio's Web-IO push mechanism validates against that range and silently stops sending updates once the live value overflows it. Analog markers have no configurable range on the Comexio side, so the command's Min/Max are now set to the full signed-32-bit span instead.
- **`function_plan_sort`'s plan dropdown offered every plan, not just HA-managed ones:** The service already refuses to sort a plan it doesn't manage at runtime, but the Actions UI dropdown listed all live plans, so picking an unmanaged one always ended in a rejection notification. The dropdown now only lists HA-managed cluster plans; the other function-plan services are unaffected.
- **`function_plan_visualize`'s text diagram mislabeled non-marker/Web-IO elements:** Blocks, time modules, calendar functions, constants and comments showed as opaque `Typ{n} ref={id}` instead of their real name — the visualize handler had its own narrower labeling logic instead of the shared resolver already used by `function_plan_search` and the backup-diff viewer. Now uses the same resolver, so all element kinds get a readable label.

### ⚠️ Requirements & Notes
- After updating, a **full HA restart** is required (new platform files and translation keys).
- Function-plan services require admin (RSA) credentials; they stop/re-activate active plans automatically during edits.

---

## [0.8.1] — 2026-06-19

### ✨ New Features
- **Extension Offline Repair Issue:** When an extension module goes offline *during runtime*, a HA Repair notification is created listing the affected module(s). It auto-resolves when all modules return online. Modules that are already offline at startup are logged at INFO level only — no spurious alert for intentionally decommissioned hardware. (#11)

### 🛠️ Core & Stability Improvements
- **Statistics unit migration reliability:** Detection now uses the correct `statistics_unit_of_measurement` key (HA 2024+) and the guard condition is tightened to `not stored_unit`, preventing the "Fixed 93 mismatches" message from firing on every restart once the migration has already run. (#9)
- **Bootstrap deadlock eliminated:** `async_block_till_done()` replaced with `asyncio.sleep()` in the background statistics-fix task — HA startup no longer hangs up to 5 minutes when the recorder queue is active. (#9)
- **Offline extension orphan guard:** Entity IDs of offline extension IOs are snapshotted before the cleanup loop and excluded from orphaned-statistics detection, preventing false Repair issues from reappearing after every restart. (#9)
- **aiohttp session warning suppressed:** `ComexioAPI.close()` is now a no-op since `async_create_clientsession` manages the session lifecycle — HA no longer warns about the integration closing a managed session on full restart. (#10)

### 🐛 Bug Fixes & Refactoring
- **`unit_class` deprecation warning resolved:** `async_update_statistics_metadata` now passes `new_unit_class=None` (universally valid), eliminating the 2026.11 deprecation warning. (#9)
- **`max=0` silently replaced with `100.0`:** Falsy-check bug in `number.py` caused a valid `max` of `0` on analog IO outputs to be overridden. Fixed by checking `is not None` instead. (#9)

### ⚠️ Requirements & Notes
- After updating, a full HA restart is recommended (coordinator behaviour change for extension offline handling).

---

## [0.8.0] — 2026-06-16

### ✨ New Features
- **Reconfigure Flow:** Connection credentials (host, username, admin password, API user/pass) are now editable via **⋮ → Reconfigure** without re-adding the integration. Blank password fields preserve the existing stored value; a login test runs before saving.
- **Polling Interval Dropdown:** The free-range slider (1–1440 min) is replaced by a curated dropdown: 1 / 5 / 10 / 15 / 30 / 45 / 60 / 120 / 300 / 600 / 1440 min.
- **Offline Extension Detection:** Extension modules that are offline (no dash-separated serial in the `Identifier` field) are now detected automatically on every coordinator poll.
- **Sub-device Auto-Removal:** When an extension module goes offline, all its entities are removed from the HA entity registry and the corresponding sub-device disappears from the device list — no stale "unavailable" entries.
- **Diagnostic Sensor — Offline Extensions:** A new `Offline Extensions` sensor on the hub device shows the count of offline extensions and lists them via the `extensions` state attribute.
- **Orphaned Statistics Repair on Startup:** After entity cleanup, orphaned-statistics detection now runs immediately so the HA Repair issue appears right away.

### 🛠️ Core & Stability Improvements
- **Options flow cleanup:** Credential fields removed from the options form — connection details live exclusively in `config_entry.data` and are managed via the new reconfigure flow.
- **Coordinator credential source:** `async_config_entry_updated` now reads credentials directly from `entry.data`, preventing stale options values from overriding a reconfigure.
- `state_class` returns `None` for sensors of offline extensions, preventing recorder unit-mismatch warnings.
- Transition logging: coordinator logs which extensions went offline / came back online on each state change.
- `async_migrate_entity_ids()` extracted to coordinator as a shared helper, reducing cognitive complexity.

### 📖 Documentation
- New `CONFIGURATION.md` — bilingual (EN + DE) guide with all UI screenshots and field descriptions.
- README updated with reconfigure tip, options note, and link to the new configuration guide.

### ⚠️ Requirements & Notes
- After updating, a **full HA restart** is required (new translation keys).
- Existing entries: credentials stored in `config_entry.options` from the old options flow are automatically cleaned up the first time you save via Reconfigure.

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
