# <img src="icon.png" width="40" align="center"> Comexio Integration for Home Assistant

🌍 *[🇩🇪 Auf Deutsch lesen (Read this in German)](#-deutsch)*

[![GitHub release (latest by date)](https://img.shields.io/github/v/release/kayl-codes/homeassistant-comexio?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=plastic)](https://github.com/hacs/integration)
[![Project Stage](https://img.shields.io/badge/project%20stage-development-yellow.svg?style=plastic)](#)
[![GitHub all releases](https://img.shields.io/github/downloads/kayl-codes/homeassistant-comexio/total?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/releases)

[![GitHub commits since latest release](https://img.shields.io/github/commits-since/kayl-codes/homeassistant-comexio/latest?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/commits/master)
[![GitHub commit activity](https://img.shields.io/github/commit-activity/m/kayl-codes/homeassistant-comexio?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/graphs/commit-activity)
[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/kayl-codes/homeassistant-comexio/ci.yml?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/actions)

This custom integration seamlessly and locally connects **Comexio IO-Servers** to Home Assistant. It was designed to build a high-performance, real-time bridge between the Comexio logic world and Home Assistant. The focus is on blazing-fast speed (Local Push) and fully automated, intelligent interface management.

## ❤️ Support
This integration is actively maintained and updated in my spare time.

If it has helped you, consider supporting ongoing development, bug fixes, compatibility updates, and future enhancements:

- ❤️ GitHub Sponsors: https://github.com/sponsors/kayl-codes
- ☕ Buy Me a Coffee: https://buymeacoffee.com/kayl74

Every contribution is greatly appreciated. Thank you for your support!

## ✨ Core Features

- ⚡ **Real-Time Status (Local Push):** Uses Home Assistant Webhooks and dynamically generated LUA scripts in Comexio. Status changes of inputs/outputs and markers are pushed to Home Assistant without delay (no polling).
- 🤖 **Smart Web-IO Lifecycle Management:** Automatically detects missing webhooks in Comexio and offers to create, repair, or clean them up directly via the HA dashboard.
- 🔄 **Deep Delta-Sync:** If a name or type changes in Comexio, an intelligent comparison updates *only* the affected commands (delta), without breaking existing function plans.
- 🛠️ **Integrated Repair Dialogs (HA Repairs):** In case of inconsistencies between HA and Comexio, the integration creates interactive repair suggestions directly in the HA dashboard.
- 📴 **Offline Extension Handling:** When a Comexio extension module goes offline, all its entities and its sub-device are automatically removed from the HA device list. A diagnostic sensor on the hub device shows which extensions are currently offline.
- 🧩 **Function Plan Management & Backups:** Manage Comexio function plans (Logikpläne) directly from HA — wire markers to Web-IO commands, sort plan layouts, and roll back any plan to an automatically captured backup snapshot (SharePoint-style per-plan versioning).
- 🖼️ **Live Function Plan Preview:** An interactive SVG diagram of any function plan, rendered at Comexio Studio's own layout, with live wire colors, search, and a debug console — plus an experimental wiring health-check and a signal-flow diagram.
- 🔐 **Read-Only & Trigger Markers:** Tag a marker `[RO]` to expose it as a read-only sensor instead of a writable switch/number — protects security-critical values from accidental overwrites, enforced both at the entity level and in the `set_value` service. Tag it `[TRIG]` (or the legacy `[TP]` alias) to get a one-shot "virtual button" entity instead; the marker resets itself via a dedicated, auto-managed `HA - TRIGGER` function plan, no manual wiring needed.
- 🩺 **Web-IO Range Guard:** A nightly check (plus an on-demand trigger button) verifies and automatically corrects Min/Max drift for eligible analog Web-IO commands against Comexio's live config, with a summary notification of checked/fixed/failed/excluded commands.
- 🔒 **Secure Authentication:** Full support for the modern RSA login method (v11) for administrative tasks as well as Basic Auth for standard API calls.

## 📦 Supported Entities

| Platform | Comexio Source | Features |
| :--- | :--- | :--- |
| `sensor` | Analog IOs (QI, TL, AI) | Automatic detection of temperature (°C), power (W), current (A), etc. via `$ioTypes`. |
| `binary_sensor` | Digital Inputs | Auto-detection of motion detectors, window and door contacts based on the name. |
| `switch` | Digital Outputs (Q) & Markers | Switches physical relays (classified as outlets) and digital markers. |
| `number` | Analog Markers | Sets setpoints with automatic range checking (e.g., target temperature). |
| `sensor` / `binary_sensor` | Read-Only Markers (`[RO]`) | Analog/digital markers tagged `[RO]` are exposed as read-only sensors instead of writable `number`/`switch` entities. |
| `button` | Trigger Markers (`[TRIG]` / `[TP]`) | One-shot "virtual button" entity for trigger-tagged markers; the marker resets itself via a dedicated, auto-managed `HA - TRIGGER` function plan. |
| `button` | System Functions | Manual "Smart-Sync" trigger and cancel button directly from the HA device view. |
| `select` | Function Plans | Lists all Comexio function plans; used as the default target for the function-plan services. |
| `sensor` (diagnostic) | Integration | `Offline Extensions` — shows how many extension modules are currently offline and lists their names as a state attribute. |
| `sensor` (diagnostic) | Integration | `Function Plan Backups` — total number of stored plan snapshots, per-plan details as state attributes. |
| `sensor` (diagnostic) | Integration | `Bus Workload` — internal Comexio bus/CPU load in %, polled independently every 10 s. |
| `binary_sensor` (diagnostic) | Integration | `SD Card Present` — whether the Comexio server currently reports an SD card. |
| `update` (diagnostic) | Integration | `Firmware` — one per extension module plus the IO-Server base, showing installed/available firmware version. Read-only (no install action). |
| `button` (diagnostic) | Integration | `Web-IO Range Check` — manually triggers the nightly analog Min/Max range guard on demand. |

## 🚀 Installation

### Option 1: HACS (Recommended)
Since this integration is not (yet) in the standard HACS store, you can add it as a custom repository:
1. Go to **HACS** > **Integrations**.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add the URL of this GitHub repository and select the category **Integration**.
4. Click Download and restart Home Assistant.

### Option 2: Manual
1. Download this repository as a ZIP file.
2. Copy the `comexio` folder into your `custom_components` directory of Home Assistant.
3. Restart Home Assistant.

## ⚙️ Configuration (Setup)

1. In Home Assistant, go to **Settings > Devices & Services**.
2. Click **Add Integration** in the bottom right corner and search for **Comexio**.
3. Fill in your connection details:
   - **Host:** IP address of your Comexio server.
   - **Username / Password:** Your credentials for the web interface (Admin).
   - **API User / Pass (Optional):** For faster API calls via Basic Auth.
4. After the setup, the integration logs into Comexio and downloads the configuration.

> [!TIP]
> **Initial Setup:** When the integration starts for the first time, it will propose to automatically create the Web-IO device class in Comexio via the Home Assistant "Repairs" menu. Confirm this to activate full webhook functionality!

> [!TIP]
> **Changing connection details later:** If your Comexio IP address or passwords change, you do **not** need to re-add the integration. Open *Settings → Devices & Services → Comexio → ⋮ → Reconfigure* and update host, username, and passwords directly. Leave password fields blank to keep the existing password.

> [!NOTE]
> **Settings (⚙️)** — The gear icon opens non-security options: entity naming schemas, import flags, polling interval, notifications, and cover detection keywords.

📖 **[Full configuration guide with screenshots →](CONFIGURATION.md)**

## 🧠 Smart Lifecycle Management (How it works)

The integration manages the interface to Comexio completely autonomously. The system consists of three phases:

### 1️⃣ Periodic Audit (Consistency Check)
The *Data Update Coordinator* regularly checks in the background whether the configuration in HA still matches the Web-IO commands created in Comexio 100%.

### 2️⃣ Detection of Mismatches
If you adjust the logic in Comexio, HA detects this immediately and classifies the issue:
- ➕ **Missing:** An IO/Marker exists but doesn't have a webhook in Comexio yet.
- ✏️ **Rename:** A name was changed in Comexio, but the webhook still has the old name.
- 🔧 **Type Mismatch:** A type (Digital/Analog) has changed.
- 🗑️ **Orphan:** A webhook refers to a device that has long been deleted.
- 🌐 **IP Mismatch:** The IP of your HA server has changed.

### 3️⃣ Manual Repair (Delta Sync)
Instead of blindly overwriting everything (which might break your Comexio logic), HA creates a **Repair Issue**.
You can then decide via click whether you want to perform a **Full Sync** or a targeted **Delta-Sync** (e.g., "Only delete orphans"). The integration pauses between write operations (`asyncio.sleep`) to avoid overloading the Comexio server.

## 📴 Offline Extension Handling

If a Comexio extension module (e.g. an RC or UD bus device) is offline or not reachable, the integration detects this automatically during the next coordinator poll and takes the following actions:

1. **Entities removed:** All sensors, switches, and binary sensors belonging to the offline extension are removed from the HA entity registry.
2. **Sub-device removed:** The extension's sub-device is deleted from the HA device list so no stale "Unavailable" card remains.
3. **Diagnostic sensor updated:** The `Offline Extensions` sensor on the hub device increments its count and lists the offline extension names in its `extensions` attribute.

When the extension comes back online, a full HA restart or integration reload will re-create all its entities and sub-device automatically.

> [!NOTE]
> Detection works by checking the `Identifier` field returned by the Comexio API. An online extension returns a serial number (e.g. `8505-2057-2326`), while an offline one only returns its model code (e.g. `5010`) without dashes.

**Renaming an extension in Comexio** is also handled automatically: the same stable serial number is used to detect the rename at the next HA restart, and the affected entities' `unique_id`s plus the device identifier/name are rewritten in place — `entity_id`s and statistics history are preserved instead of new, orphaned entities being created.

## 📊 Bus Workload Monitoring

The integration polls Comexio's internal bus/CPU workload (`Bus Workload` sensor, in %) and SD card presence (`SD Card Present`) independently of the main coordinator, every 10 seconds — fast enough to catch short spikes without waiting for the regular audit interval.

The integration only exposes the raw reading; it does not hard-code an "overloaded" threshold or alerting logic. Use Home Assistant's own tools for that — they already handle debounce/hysteresis correctly:

- **Sustained overload alert** — a `numeric_state` trigger with a `for:` duration on `sensor.comexio_<server>_bus_workload`:
  ```yaml
  trigger:
    - trigger: numeric_state
      entity_id: sensor.comexio_<server>_bus_workload
      above: 80
      for: "00:01:00"
  action:
    - action: notify.persistent_notification
      data:
        title: "Comexio bus overload"
        message: "Bus workload has been above 80% for over a minute."
  ```
- **Value-crossing without duration** — add a `threshold` helper (*Settings → Devices & Services → Helpers → Create Helper → Threshold*) pointing at the sensor for a ready-made `binary_sensor` with built-in hysteresis.

## 🔧 Extension Firmware Updates

Comexio can check every extension module on the local bus for available firmware updates — but it warns that running this check can briefly interrupt extension outputs. The integration therefore never polls it on a schedule. Instead, it piggybacks on the IO-Server software version it already tracks (`Version` diagnostic sensor): whenever that version changes, the extension firmware check runs once, at the next nightly window (04:00) — a base update makes a matching extension firmware update likely, so this catches it without any unattended risk on a normal day.

Results show up as one `update.*` entity per extension module (in that extension's device) plus one for the IO-Server base itself, each reporting installed/available firmware version through HA's standard `update` entity (visible in the Settings → *Updates available* overview). These are read-only — Comexio's install trigger isn't wired up, so use the Comexio admin UI to actually apply an update once one is found.

A diagnostic **"Check Firmware Now"** button forces the check on demand, bypassing the version gate (but not the underlying risk — the same brief output interruption Comexio warns about applies). Use it to test the `update.*` entities right away instead of waiting for both a version change and the nightly window.

## 🧩 Function Plan Management (Logikplan)

The integration can manage Comexio **function plans** directly from Home Assistant. All actions are available under *Developer Tools → Actions* (or in automations) and require the admin (RSA) credentials. Active plans are stopped, edited, and re-activated automatically.

| Action | What it does |
| :--- | :--- |
| `comexio.function_plan_connect` | Wires markers to their Web-IO commands in a plan (single IDs, lists, or `*` for all). |
| `comexio.function_plan_sort` | Sorts all plan elements by marker ID and snaps them to exact grid positions. |
| `comexio.function_plan_visualize` | Shows a text overview of all connections and unconnected elements. |
| `comexio.function_plan_stop` / `..._activate` | Manual plan lifecycle control (stop / save + activate). |
| `comexio.function_plan_restore` | Rolls a plan back to a stored backup snapshot — optionally as an independent copy instead of overwriting the source. |
| `comexio.function_plan_list_backups` | Returns all stored snapshots as a structured service response — filterable by plan, name, backup type, slot, and age; sortable by timestamp, plan, or slot. |
| `comexio.function_plan_delete_backups` / `..._purge_orphaned_backups` | Deletes one snapshot, all snapshots of a plan, or every stored backup — or, for purge, only the snapshots of plans that no longer exist. |
| `comexio.function_plan_search` | Finds which plans contain elements matching a text query (same wildcard syntax as the preview card's search bar). |
| `comexio.function_plan_analyze` / `..._flow_diagram` | *Experimental:* flags likely wiring mistakes, and lays a plan out by signal-flow topology instead of its physical position. |

📖 **[Function Plan Preview guide →](FUNCTION_PLAN_PREVIEW.md)** — live SVG diagram, dashboard card, search, and debug box.

### 🗂️ Automatic Backups & Restore

Function plans are backed up automatically — no configuration needed:

- **Auto backups:** On every coordinator poll, each plan is snapshotted *only if its content changed* (hash delta). The **3 newest versions per plan** are kept.
- **Change backups:** Right before the integration modifies a plan (connect, sort, restore, …), a safety snapshot is stored — the **10 newest per plan**.
- Snapshots live in HA's `.storage` folder and are therefore included in regular Home Assistant backups. Snapshots of deleted plans are kept.
- Restore (`function_plan_restore`) brings back **structure, element positions, and canvas paper/DPI settings**, takes a pre-restore safety snapshot first, and verifies success via content hash. Pick the **Snapshot** field for a one-click, unambiguous target (plan + type + slot + timestamp in one option); the advanced `fub_id`/`kind`/`slot` fields are a manual alternative for scripting and are ignored whenever `snapshot` is set.
- If the original plan was **deleted**, or its ID was **reused by an unrelated plan**, restore rebuilds it as a **new plan** by default (`on_conflict: new_id`) — or, with `confirm: true` and `on_conflict: force_override`, deliberately overwrites whatever now occupies that ID.
- The diagnostic sensor **Function Plan Backups** shows the total snapshot count with per-plan details as attributes.

### 🧱 Managed Cluster Plans

For large installations, marker/Web-IO pairs can be distributed across auto-managed plans named like `HA - Marker [1-100]`. The plan name prefix and the maximum number of marker pairs per plan are configurable in the integration options. Managed plans are labelled with a comment element inside Comexio so they are easy to recognise.

## 🐛 Troubleshooting

- **"Web-IO device is blocked (in use)":** You are trying to do a *Full Sync*, but the Web-IO device is already connected in a function plan in Comexio. The integration detects this and falls back automatically and safely to the *Delta-Sync* to patch only individual commands.
- **No updates in HA (Webhooks don't arrive):** Make sure that the IP address stored in Comexio matches HA. If the HA IP has changed, you will be offered the "Update IP" option in the repair menu.
- **Extension entities don't reappear after coming back online:** Reload the integration via *Settings → Devices & Services → Comexio → ⋮ → Reload*. The coordinator will re-detect the extension as online and restore all its entities.

## 🤝 Contributing
Pull Requests are highly welcome! If you find bugs or have feature requests, please create an issue in the GitHub repository.

## 📄 License
This project is licensed under the MIT License.

---

# 🇩🇪 Deutsch

🌍 *[🇬🇧 Read this in English](#comexio-integration-for-home-assistant)*

# <img src="icon.png" width="40" align="center"> Comexio Integration für Home Assistant

[![GitHub release (latest by date)](https://img.shields.io/github/v/release/kayl-codes/homeassistant-comexio?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=plastic)](https://github.com/hacs/integration)
[![Project Stage](https://img.shields.io/badge/project%20stage-development-yellow.svg?style=plastic)](#)
[![GitHub all releases](https://img.shields.io/github/downloads/kayl-codes/homeassistant-comexio/total?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/releases)

[![GitHub commits since latest release](https://img.shields.io/github/commits-since/kayl-codes/homeassistant-comexio/latest?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/commits/master)
[![GitHub commit activity](https://img.shields.io/github/commit-activity/m/kayl-codes/homeassistant-comexio?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/graphs/commit-activity)
[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/kayl-codes/homeassistant-comexio/ci.yml?style=plastic)](https://github.com/kayl-codes/homeassistant-comexio/actions)

Diese Custom Integration bindet **Comexio IO-Server** nahtlos und lokal in Home Assistant ein. Sie wurde entwickelt, um eine performante, echtzeitfähige Brücke zwischen der Comexio-Logikwelt und Home Assistant zu schlagen. Der Fokus liegt auf rasanter Geschwindigkeit (Local Push) und einer vollständig automatisierten, intelligenten Verwaltung der Schnittstelle.

## ❤️ Support
Diese Integration wird aktiv in meiner Freizeit gewartet und weiterentwickelt.

Wenn sie dir geholfen hat, freue ich mich über deine Unterstützung für laufende Entwicklung, Bugfixes, Kompatibilitätsupdates und neue Features:

- ❤️ GitHub Sponsors: https://github.com/sponsors/kayl-codes
- ☕ Buy Me a Coffee: https://buymeacoffee.com/kayl74

Jede Unterstützung wird sehr geschätzt. Danke!

## ✨ Kern-Features

- ⚡ **Echtzeit-Status (Local Push):** Nutzt Home Assistant Webhooks und dynamisch generierte LUA-Skripte in Comexio. Statusänderungen von Ein-/Ausgängen und Merkern werden ohne Verzögerung (Polling) an Home Assistant gepusht.
- 🤖 **Smartes Web-IO Lifecycle Management:** Erkennt fehlende Webhooks in Comexio automatisch und bietet im HA-Dashboard an, diese anzulegen, zu reparieren oder zu bereinigen.
- 🔄 **Deep Delta-Sync:** Ändert sich ein Name oder Typ in Comexio, aktualisiert ein intelligenter Abgleich *nur* die betroffenen Befehle (Delta), ohne bestehende Logikpläne zu zerstören.
- 🛠️ **Integrierte Reparatur-Dialoge (HA Repairs):** Bei Unstimmigkeiten zwischen HA und Comexio erstellt die Integration interaktive Reparatur-Vorschläge direkt im HA-Dashboard.
- 📴 **Offline-Extension-Erkennung:** Geht ein Comexio-Erweiterungsmodul offline, werden alle zugehörigen Entitäten und das Sub-Device automatisch aus der HA-Geräteliste entfernt. Ein Diagnose-Sensor am Hub-Device zeigt, welche Module gerade offline sind.
- 🧩 **Funktionsplan-Verwaltung & Backups:** Comexio-Funktionspläne (Logikpläne) direkt aus HA verwalten — Merker mit Web-IO-Befehlen verdrahten, Plan-Layouts sortieren und jeden Plan auf einen automatisch erfassten Backup-Snapshot zurücksetzen (Versionierung je Plan wie in SharePoint).
- 🖼️ **Live-Logikplan-Vorschau:** Ein interaktives SVG-Diagramm jedes Funktionsplans im Original-Layout von Comexio Studio, mit Live-Drahtfarben, Suche und Debug-Konsole — dazu ein experimenteller Verdrahtungs-Check und ein Signalfluss-Diagramm.
- 🔐 **Nur-Lese- & Trigger-Merker:** Markiere einen Merker mit `[RO]`, um ihn als reinen Nur-Lese-Sensor statt als beschreibbaren Switch/Number bereitzustellen — schützt sicherheitskritische Werte vor versehentlichem Überschreiben, durchgesetzt sowohl auf Entitäts-Ebene als auch im `set_value`-Service. Mit `[TRIG]` (oder dem Legacy-Alias `[TP]`) wird daraus stattdessen eine einmalig auslösende „virtuelle Taster"-Entität; der Merker setzt sich selbst über einen dediziert automatisch verwalteten `HA - TRIGGER`-Funktionsplan zurück, keine manuelle Verdrahtung nötig.
- 🩺 **Web-IO Bereichs-Wächter:** Eine nächtliche Prüfung (plus ein Button zum manuellen Auslösen) gleicht Min/Max aller passend zuordenbaren analogen Web-IO-Befehle mit der Live-Konfiguration von Comexio ab und korrigiert automatisch jede Abweichung, inklusive Zusammenfassungs-Benachrichtigung über geprüfte/korrigierte/fehlgeschlagene/ausgeschlossene Befehle.
- 🔒 **Sichere Authentifizierung:** Volle Unterstützung für das moderne RSA-Login-Verfahren (v11) für administrative Aufgaben sowie Basic Auth für Standard-API-Aufrufe.

## 📦 Unterstützte Entitäten

| Plattform | Comexio Quelle | Features |
| :--- | :--- | :--- |
| `sensor` | Analoge IOs (QI, TL, AI) | Automatische Erkennung von Temperatur (°C), Leistung (W), Strom (A), etc. via `$ioTypes`. |
| `binary_sensor` | Digitale Eingänge | Auto-Erkennung von Bewegungsmeldern, Fenster- und Türkontakten anhand des Namens. |
| `switch` | Digitale Ausgänge (Q) & Merker | Schaltet physische Relais (als Steckdose/Outlet klassifiziert) und digitale Merker. |
| `number` | Analoge Merker | Setzt Sollwerte (Setpoints) mit automatischer Bereichsprüfung (z. B. Temp-Soll). |
| `sensor` / `binary_sensor` | Nur-Lese-Merker (`[RO]`) | Analoge/digitale Merker mit `[RO]`-Tag werden als reine Nur-Lese-Sensoren statt als beschreibbare `number`/`switch`-Entitäten bereitgestellt. |
| `button` | Trigger-Merker (`[TRIG]` / `[TP]`) | Einmalig auslösende „virtuelle Taster"-Entität für Trigger-markierte Merker; der Merker setzt sich selbst über einen dediziert automatisch verwalteten `HA - TRIGGER`-Funktionsplan zurück. |
| `button` | System-Funktionen | Manueller "Smart-Sync" Abgleich und Abbruch direkt aus der HA-Geräteansicht. |
| `select` | Funktionspläne | Listet alle Comexio-Funktionspläne; dient als Standard-Ziel für die Funktionsplan-Actions. |
| `sensor` (Diagnose) | Integration | `Offline Extensions` — zeigt, wie viele Erweiterungsmodule gerade offline sind, und listet deren Namen als State-Attribut. |
| `sensor` (Diagnose) | Integration | `Function Plan Backups` — Gesamtzahl der gespeicherten Plan-Snapshots, Details je Plan als State-Attribute. |
| `sensor` (Diagnose) | Integration | `Bus Workload` — interne Comexio Bus-/CPU-Auslastung in %, unabhängig alle 10 s abgefragt. |
| `binary_sensor` (Diagnose) | Integration | `SD Card Present` — ob der Comexio-Server aktuell eine SD-Karte meldet. |
| `update` (Diagnose) | Integration | `Firmware` — je eine pro Erweiterungsmodul plus für den IO-Server-Grundbaustein, zeigt installierte/verfügbare Firmware-Version. Reine Anzeige (kein Install-Button). |
| `button` (Diagnose) | Integration | `Web-IO Range Check` — löst die nächtliche Analog-Min/Max-Bereichsprüfung manuell auf Abruf aus. |

## 🚀 Installation

### Option 1: HACS (Empfohlen)
Da diese Integration (noch) nicht im Standard-HACS Store ist, kannst du sie als Custom Repository hinzufügen:
1. Gehe in HACS zu **Integrationen**.
2. Klicke oben rechts auf die drei Punkte und wähle **Benutzerdefinierte Repositorys**.
3. Füge die URL dieses GitHub-Repositories hinzu und wähle die Kategorie **Integration**.
4. Klicke auf Herunterladen und starte Home Assistant neu.

### Option 2: Manuell
1. Lade dir das Repository als ZIP herunter.
2. Kopiere den Ordner `comexio` in dein `custom_components` Verzeichnis von Home Assistant.
3. Starte Home Assistant neu.

## ⚙️ Konfiguration (Setup)

1. Gehe in Home Assistant zu **Einstellungen > Geräte & Dienste**.
2. Klicke unten rechts auf **Integration hinzufügen** und suche nach **Comexio**.
3. Fülle die Verbindungsdaten aus:
   - **Host:** IP-Adresse deines Comexio-Servers.
   - **Benutzername / Passwort:** Deine Zugangsdaten für die Weboberfläche (Admin).
   - **API User / Pass (Optional):** Für schnellere API-Aufrufe via Basic Auth.
4. Nach dem Setup meldet sich die Integration in Comexio an und lädt die Konfiguration herunter.

> [!TIP]
> **Verbindungsdaten nachträglich ändern:** Wenn sich IP-Adresse oder Passwörter ändern, musst du die Integration **nicht** neu einrichten. Öffne die Integration unter *Einstellungen → Geräte & Dienste → Comexio → ⋮ → Neu konfigurieren* und passe Host, Benutzername und Passwörter direkt an. Felder für Passwörter leer lassen bedeutet "bestehendes Passwort beibehalten".

> [!NOTE]
> **Einstellungen (⚙️)** — Über das Zahnrad-Icon der Integration erreichst du nicht-sicherheitskritische Optionen: Namensschemas für Entities, Import-Flags, Poll-Intervall, Benachrichtigungen und Rollladen-Schlüsselwörter.

📖 **[Vollständige Konfigurationsanleitung mit Screenshots →](CONFIGURATION.md)**

> [!TIP]
> **Initial Setup:** Wenn die Integration das erste Mal startet, wird sie im Home Assistant Reparatur-Menü ("Repairs") vorschlagen, die Web-IO Geräteklasse in Comexio automatisch anzulegen. Bestätige dies, um die volle Webhook-Funktionalität zu aktivieren!

## 🧠 Smart Lifecycle Management (Wie es funktioniert)

Die Integration verwaltet die Schnittstelle zu Comexio komplett autonom. Das System besteht aus drei Phasen:

### 1️⃣ Periodischer Audit (Konsistenzprüfung)
Der *Data Update Coordinator* prüft im Hintergrund regelmäßig, ob die Konfiguration in HA noch zu 100 % mit den angelegten Web-IO Befehlen in Comexio übereinstimmt.

### 2️⃣ Erkennung von Abweichungen (Mismatches)
Wenn du in Comexio Logik anpasst, erkennt HA das sofort und klassifiziert den Fehler:
- ➕ **Missing:** Ein IO/Merker existiert, hat aber noch keinen Webhook in Comexio.
- ✏️ **Rename:** Ein Name wurde in Comexio geändert, aber der Webhook heißt noch alt.
- 🔧 **Type Mismatch:** Ein Typ (Digital/Analog) hat sich geändert.
- 🗑️ **Orphan:** Ein Webhook verweist auf ein Gerät, das längst gelöscht wurde.
- 🌐 **IP Mismatch:** Die IP deines HA-Servers hat sich geändert.

### 3️⃣ Manuelle Reparatur (Delta Sync)
Anstatt blind alles zu überschreiben (und dabei womöglich deine Comexio-Logik zu beschädigen), erstellt HA ein **Reparatur-Issue**.
Du kannst dann per Klick entscheiden, ob du einen **Full Sync** oder einen **gezielten Delta-Sync** (z.B. "Nur Waisen löschen") durchführen möchtest. Die Integration pausiert zwischen den Schreibvorgängen (`asyncio.sleep`), um den Comexio-Server nicht zu überlasten.

## 📴 Offline-Extension-Erkennung

Wenn ein Comexio-Erweiterungsmodul (z. B. ein RC- oder UD-Bus-Gerät) offline oder nicht erreichbar ist, erkennt die Integration das automatisch beim nächsten Coordinator-Poll und handelt wie folgt:

1. **Entitäten entfernt:** Alle Sensoren, Schalter und Binär-Sensoren des Moduls werden aus der HA Entity Registry entfernt.
2. **Sub-Device entfernt:** Das Sub-Device des Moduls verschwindet aus der HA-Geräteliste — keine veraltete "Nicht verfügbar"-Karte.
3. **Diagnose-Sensor aktualisiert:** Der `Offline Extensions`-Sensor am Hub-Device erhöht seinen Zähler und listet die Namen der Offline-Module im `extensions`-Attribut.

Kommt das Modul wieder online, stellt ein Reload der Integration (*Einstellungen → Geräte & Dienste → Comexio → ⋮ → Neu laden*) alle Entitäten und das Sub-Device automatisch wieder her.

> [!NOTE]
> Die Erkennung basiert auf dem `Identifier`-Feld der Comexio-API. Ein Online-Modul liefert eine Seriennummer (z. B. `8505-2057-2326`), ein Offline-Modul nur den Geräte-Code ohne Bindestriche (z. B. `5010`).

**Eine Umbenennung eines Erweiterungsmoduls** in Comexio wird ebenfalls automatisch nachgezogen: dieselbe stabile Seriennummer erkennt die Umbenennung beim nächsten HA-Neustart, und die `unique_id`s der betroffenen Entitäten sowie Geräte-Identifier/-Name werden direkt angepasst — `entity_id`s und Statistik-Historie bleiben erhalten, statt neue, verwaiste Entitäten anzulegen.

## 📊 Busauslastungs-Überwachung

Die Integration fragt die interne Comexio Bus-/CPU-Auslastung (`Bus Workload`-Sensor, in %) und den SD-Karten-Status (`SD Card Present`) unabhängig vom Haupt-Coordinator alle 10 Sekunden ab — schnell genug, um kurze Lastspitzen zu erfassen, ohne auf das reguläre Audit-Intervall zu warten.

Die Integration liefert bewusst nur den Rohwert; eine "überlastet"-Schwelle oder Alarmlogik ist nicht fest einprogrammiert. Dafür sind Home Assistants eigene Bordmittel gedacht — sie behandeln Entprellung/Hysterese bereits korrekt:

- **Alarm bei anhaltender Überlast** — ein `numeric_state`-Trigger mit `for:`-Dauer auf `sensor.comexio_<server>_bus_workload`:
  ```yaml
  trigger:
    - trigger: numeric_state
      entity_id: sensor.comexio_<server>_bus_workload
      above: 80
      for: "00:01:00"
  action:
    - action: notify.persistent_notification
      data:
        title: "Comexio Busüberlastung"
        message: "Die Busauslastung liegt seit über einer Minute über 80 %."
  ```
- **Schwellwert ohne Zeitkomponente** — ein `Schwellenwert`-Helper (*Einstellungen → Geräte & Dienste → Helfer → Helfer erstellen → Schwellenwert*), der auf den Sensor zeigt, liefert einen fertigen `binary_sensor` mit eingebauter Hysterese.

## 🔧 Firmware-Updates der Erweiterungen

Comexio kann jedes Erweiterungsmodul am lokalen Bus auf verfügbare Firmware-Updates prüfen — warnt aber, dass dieser Check kurzzeitig die Ausgänge der Module unterbrechen kann. Die Integration fragt ihn deshalb nie nach einem festen Zeitplan ab. Stattdessen wird die bereits getrackte IO-Server-Softwareversion genutzt (`Version`-Diagnose-Sensor): Ändert sich diese Version, läuft der Erweiterungs-Firmware-Check genau einmal, beim nächsten nächtlichen Zeitfenster (04:00) — ein Update des Grundbausteins macht ein passendes Firmware-Update der Erweiterungen wahrscheinlich, damit wird das ohne unbeaufsichtigtes Risiko im Normalbetrieb erfasst.

Die Ergebnisse erscheinen als je eine `update.*`-Entität pro Erweiterungsmodul (im jeweiligen Erweiterungs-Device) sowie eine für den IO-Server-Grundbaustein selbst, jeweils mit installierter/verfügbarer Firmware-Version über die HA-Standard-`update`-Entität (sichtbar in *Einstellungen → Verfügbare Updates*). Diese sind reine Anzeige — der Install-Aufruf von Comexio ist nicht angebunden, ein gefundenes Update wird weiterhin über die Comexio-Admin-Oberfläche eingespielt.

Ein Diagnose-Button **„Firmware jetzt prüfen“** erzwingt die Prüfung sofort, unabhängig vom Versions-Gate (das zugrunde liegende Risiko bleibt aber bestehen — dieselbe kurze Ausgangs-Unterbrechung, vor der Comexio warnt). Damit lassen sich die `update.*`-Entitäten sofort testen, statt auf Versionswechsel und Nachtfenster zu warten.

## 🧩 Funktionsplan-Verwaltung (Logikplan)

Die Integration kann Comexio-**Funktionspläne** direkt aus Home Assistant verwalten. Alle Actions findest du unter *Entwicklerwerkzeuge → Aktionen* (oder in Automationen); sie benötigen die Admin-Zugangsdaten (RSA). Aktive Pläne werden automatisch gestoppt, bearbeitet und wieder aktiviert.

| Action | Funktion |
| :--- | :--- |
| `comexio.function_plan_connect` | Verdrahtet Merker mit ihren Web-IO-Befehlen im Plan (einzelne IDs, Listen oder `*` für alle). |
| `comexio.function_plan_sort` | Sortiert alle Plan-Elemente nach Merker-ID und richtet sie exakt am Raster aus. |
| `comexio.function_plan_visualize` | Zeigt eine Text-Übersicht aller Verbindungen und unverbundenen Elemente. |
| `comexio.function_plan_stop` / `..._activate` | Manuelle Lifecycle-Steuerung (Stoppen / Speichern + Aktivieren). |
| `comexio.function_plan_restore` | Setzt einen Plan auf einen gespeicherten Backup-Snapshot zurück — optional als unabhängige Kopie statt Überschreiben des Original-Plans. |
| `comexio.function_plan_list_backups` | Liefert alle Snapshots als strukturierte Service-Response — filterbar nach Plan, Name, Backup-Typ, Slot und Alter; sortierbar nach Zeitstempel, Plan oder Slot. |
| `comexio.function_plan_delete_backups` / `..._purge_orphaned_backups` | Löscht einen Snapshot, alle Snapshots eines Plans oder sämtliche Backups — bzw. beim Purge nur die Snapshots nicht mehr existierender Pläne. |
| `comexio.function_plan_search` | Findet Pläne mit Elementen, die zu einem Suchtext passen (gleiche Platzhalter-Syntax wie die Suchleiste der Vorschau-Karte). |
| `comexio.function_plan_analyze` / `..._flow_diagram` | *Experimentell:* markiert wahrscheinliche Verdrahtungsfehler bzw. ordnet einen Plan nach Signalfluss statt nach physischer Position an. |

📖 **[Logikplan-Vorschau — Anleitung →](FUNCTION_PLAN_PREVIEW.md)** — Live-SVG-Diagramm, Dashboard-Karte, Suche und Debug-Box.

### 🗂️ Automatische Backups & Restore

Funktionspläne werden automatisch gesichert — ganz ohne Konfiguration:

- **Auto-Backups:** Bei jedem Coordinator-Poll wird jeder Plan *nur bei geändertem Inhalt* (Hash-Delta) gesichert. Es bleiben die **3 neuesten Versionen je Plan** erhalten.
- **Change-Backups:** Unmittelbar bevor die Integration einen Plan verändert (Connect, Sort, Restore, …), wird ein Sicherheits-Snapshot angelegt — die **10 neuesten je Plan**.
- Die Snapshots liegen im `.storage`-Ordner von HA und sind damit Teil der regulären Home-Assistant-Backups. Snapshots gelöschter Pläne bleiben erhalten.
- Der Restore (`function_plan_restore`) stellt **Struktur, Element-Positionen und die Papier-/DPI-Einstellung der Zeichenfläche** wieder her, legt vorher einen Pre-Restore-Snapshot an und verifiziert den Erfolg per Inhalts-Hash. Das Feld **Snapshot** wählt das genaue Ziel mit einem Klick (Plan + Typ + Slot + Zeitstempel in einer Option); die erweiterten Felder `fub_id`/`kind`/`slot` sind eine manuelle Alternative für Skripte und werden ignoriert, sobald `snapshot` gesetzt ist.
- Wurde der ursprüngliche Plan **gelöscht** oder seine ID **von einem anderen Plan wiederverwendet**, legt der Restore standardmäßig einen **neuen Plan** an (`on_conflict: new_id`) — oder überschreibt mit `confirm: true` und `on_conflict: force_override` bewusst den Plan, der die ID aktuell belegt.
- Der Diagnose-Sensor **Function Plan Backups** zeigt die Gesamtzahl der Snapshots mit Details je Plan als Attribute.

### 🧱 Verwaltete Cluster-Pläne

Für große Installationen können Merker/Web-IO-Paare auf automatisch verwaltete Pläne mit Namen wie `HA - Marker [1-100]` verteilt werden. Präfix und maximale Paar-Anzahl je Plan sind in den Integrations-Optionen einstellbar. Verwaltete Pläne werden in Comexio mit einem Kommentar-Element gekennzeichnet und sind so leicht erkennbar.

## 🐛 Fehlerbehebung (Troubleshooting)

- **"Web-IO Gerät ist blockiert (in use)":** Du versuchst, einen *Full Sync* zu machen, aber das Web-IO Gerät ist in Comexio bereits in einem Logikplan verbunden. Die Integration erkennt das und fällt automatisch und sicher auf den *Delta-Sync* zurück, um nur die Einzelbefehle zu patchen.
- **Keine Updates in HA (Webhooks kommen nicht an):** Stelle sicher, dass die in Comexio hinterlegte IP-Adresse mit HA übereinstimmt. Falls sich die HA-IP geändert hat, wird dir im Reparatur-Menü die Option "Update IP" angeboten.
- **Extension-Entitäten erscheinen nach Wiederherstellung nicht:** Lade die Integration über *Einstellungen → Geräte & Dienste → Comexio → ⋮ → Neu laden* neu. Der Coordinator erkennt das Modul als online und legt alle Entitäten neu an.

## 🤝 Mitwirken
Pull Requests sind herzlich willkommen! Wenn du Fehler findest oder Feature-Wünsche hast, erstelle bitte ein Issue im GitHub Repository.

## 📄 Lizenz
Dieses Projekt steht unter der MIT-Lizenz.
