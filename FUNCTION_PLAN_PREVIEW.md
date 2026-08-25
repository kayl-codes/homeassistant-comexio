# Comexio — Function Plan Preview Guide

🌍 *[🇩🇪 Auf Deutsch lesen (Read this in German)](#-deutsch)*

This guide covers the **Function Plan Preview** — an interactive, live-updating diagram of a Comexio function plan (Logikplan) rendered directly inside Home Assistant. For general setup and options, see the [Configuration Guide](CONFIGURATION.md); for the full action list, see the [README](README.md#-function-plan-management-logikplan).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installing the Dashboard Card](#2-installing-the-dashboard-card)
3. [Adding the Card to a Dashboard](#3-adding-the-card-to-a-dashboard)
4. [Selecting and Rendering a Plan](#4-selecting-and-rendering-a-plan)
5. [Card Features](#5-card-features)
6. [Related Actions](#6-related-actions)
7. [Action Reference (services.yaml)](#7-action-reference-servicesyaml)

---

## 1. Overview

The Function Plan Preview renders a Comexio function plan as an SVG diagram at the same
layout Comexio Studio uses — same element positions, same WebIO labels, same wire
routing. It exists for two things:

- **Visual verification** — see how a plan is wired without opening Comexio Studio.
- **Live debugging** — wires turn **red** while their value is "high" (`1`), and this now
  updates automatically as values change on the bus. This covers both real
  markers/IOs and block-internal outputs (e.g. an AND/OR gate or a timer block that has
  no marker of its own).

It can also render a **stored backup snapshot** completely offline (no live Comexio
connection needed) — handy to check "is this the version I want" before running
`function_plan_restore`.

The feature is made up of three integration entities (select, button, image) plus one
optional custom dashboard card (`comexio-plan-card`) that turns the plain image into an
interactive diagram with search, hover, and a debug console.

---

## 2. Installing the Dashboard Card

The rendering engine and its three entities need no extra setup — they are created
automatically. The **interactive card**, however, is a separate frontend resource that
HACS does not register automatically (it ships as a `.js` file inside the integration,
not as its own HACS "plugin" repository). Add it once:

1. Copy **both** `custom_components/comexio/frontend/comexio-plan-card.js` and
   `custom_components/comexio/frontend/comexio-plan-card-utils.js` into your Home
   Assistant `config/www/` folder (the main file imports the second one — they must sit
   in the same folder).
2. Go to **Settings → Dashboards → ⋮ (top right) → Resources → Add Resource**.
3. URL: `/local/comexio-plan-card.js`, Resource type: **JavaScript Module**.
4. Hard-refresh your browser. The browser console should log
   `comexio-plan-card vX.Y.Z loaded`.

> **Note:** Browsers (and Home Assistant's service worker) can cache JavaScript modules
> aggressively. If you ever replace the file with a newer version, give it a new
> filename (e.g. `comexio-plan-card-v2.js`) and update the resource URL — bumping only a
> `?v=` query string is not reliably picked up.

Without this custom card, the plain `image.*_plan_preview` entity still works on a
regular **Picture Entity** card — you just lose search, hover, live wire colors and the
debug box (the image itself renders correctly, but the picture-entity card can't
interact with it).

---

## 3. Adding the Card to a Dashboard

Three entities work together (replace `iosrv1` with your instance name):

| Entity | Role |
|---|---|
| `select.iosrv1_function_plans` | Choose which plan to preview. |
| `button.iosrv1_preview` | Render the selected plan (creates/updates the SVG). |
| `image.iosrv1_plan_preview` | Holds the last rendered SVG — the picture source for the card. |

Example dashboard section:

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: select.iosrv1_function_plans
  - type: tile
    entity: button.iosrv1_preview
  - type: custom:comexio-plan-card
    entity: image.iosrv1_plan_preview
```

---

## 4. Selecting and Rendering a Plan

- Pick a plan in the **Function Plans** select entity, then press the **Preview**
  button once to render it.
- If no plan is selected, actions default to whatever the integration is currently
  tracking (the last plan used).
- **Structural** changes (elements or wires added/removed in Comexio) need a fresh
  button press. **Live wire colors**, however, update themselves automatically
  afterwards — no further presses needed while values just change.
- To preview a **stored backup** instead of the live plan, use the
  `function_plan_visualize` action with its `snapshot` field and `format: svg` — this
  works fully offline.

---

## 5. Card Features

- **Search bar** — case-insensitive substring search across every element label
  (markers, IOs, WebIOs, blocks, constants, comments). Supports wildcards: `?` matches
  exactly one non-space character, `*` matches anything. Matches get a highlighted
  border, everything else dims, and a match counter is shown. The same syntax is used
  by the `function_plan_search` action.
- **Live wire colors** — a wire turns red once its value is `1` (digital high). This
  covers both real markers/IOs and block-internal outputs, via a background poll of
  Comexio's live connection values that runs only while a plan is on screen.
- **Hover & tooltips** — hovering an element shows its full label as a native browser
  tooltip; hovering any wire highlights every branch of that electrical net together.
- **Zoom controls** — magnifier −/+ buttons next to the search bar; the current zoom
  level is remembered per browser. Click the percentage label to reset to 100%.
- **Debug box** — toggle it via the console icon in the toolbar. It shows a live log of
  value changes on the plan currently displayed (fed by the same webhook pushes the
  integration already receives), and offers:
  - an input field to write a value directly, e.g. `M107=1` or `IOX2#Q3=0,5` (backed by
    the `set_value` action);
  - command history (↑ / ↓ arrows) and autocomplete over the plan's own targets;
  - click an element to fill its address into the input field; double-click a
    writable digital element to instantly toggle it;
  - an exclude filter (same wildcard syntax as the search bar) to hide noisy entries —
    on first use in a given browser it defaults to hiding
    `*#TL1, *#UL1, *#AI*, *#QI*`;
  - pause/play and a clear ("eraser") button.

  Opening the debug box also switches the live-value poll to a faster cadence
  (0.5 s instead of the normal 2 s) for snappier feedback while actively debugging.
- **Plan Analysis (experimental)** — a popup (toolbar icon) with two tabs:
  - **Findings** — runs `function_plan_analyze`, a read-only wiring health-check that flags
    likely mistakes (conflicting writers, unwired Set/Reset pins, dead outputs, suspicious
    double-wiring) and also reports the recognized "virtual button" self-reset idiom as a
    benign pattern rather than a false alarm. Click a finding to jump to and highlight the
    element on the diagram.
  - **Flow Diagram** — runs `function_plan_flow_diagram`, laying the same plan out by signal
    flow (input → logic → output) instead of its physical Comexio Studio position; wire
    hover-highlighting (including fan-out junction dots) works the same as on the main
    diagram.

  Both tabs work against a stored snapshot too, entirely offline. Since this popup issues its
  own service calls, only one card's Analyse popup can be open per plan at a time — a second
  card claiming it takes over cleanly instead of the first card's results leaking through.
- **Auto-stop + extend** — a live preview's background wire-value poll automatically stops
  after 15 minutes if left open (e.g. a forgotten browser tab), instead of polling forever. The
  debug box's `/extend <minutes>` command (backed by `function_plan_preview_extend`) extends the
  current session's window on demand.
- **Help dialog** — the toolbar's help icon opens a short reference of every control described
  in this section, directly inside the card.

---

## 6. Related Actions

Available under **Developer Tools → Actions**:

| Action | What it does |
|---|---|
| `comexio.function_plan_visualize` | Renders a plan (or a stored snapshot) as SVG into the Plan Preview image entity — or returns a text summary of connections and unconnected elements. |
| `comexio.function_plan_search` | Finds which plans contain elements matching a search text — same syntax as the card's search bar. Auto-selects the plan when exactly one match is found. |
| `comexio.function_plan_analyze` | *Experimental.* Read-only wiring health-check for a live plan or stored snapshot; backs the Plan Analysis popup's Findings tab. |
| `comexio.function_plan_flow_diagram` | *Experimental.* Renders a live plan or stored snapshot as a signal-flow diagram (input → logic → output); backs the Plan Analysis popup's Flow Diagram tab. |
| `comexio.set_value` | Writes a raw value to a marker or IO — the backend of the debug box's input field. |
| `comexio.function_plan_debug_session` | Internal — called automatically by the card when its debug box opens or closes. Not meant for manual use. |
| `comexio.function_plan_preview_extend` | Internal — called automatically by the debug box's `/extend <minutes>` command. Not meant for manual use. |

---

## 7. Action Reference (services.yaml)

For reference, this is the **current** content of
`custom_components/comexio/services.yaml`, defining every action of the integration
(including the ones listed above):

```yaml
# Version: 0.3.3
# NOTE: fub_id options are populated at runtime by _update_services_yaml_plans on integration load.
generate_web_io:
  name: Web-IO Export / Upload
  description: Generates a JSON for Comexio import, or uploads the device class directly to the server.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance the action should run for.
      required: true
      selector:
        config_entry:
          integration: comexio
    upload:
      name: Upload directly
      description: If enabled, the device class is sent directly to Comexio and overwritten there.
      default: false
      selector:
        boolean:

function_plan_connect:
  name: Function Plan Connect
  description: >
    Wires markers to their Web-IO commands in the given function plan. Active plans
    are automatically stopped, edited and reactivated.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity."
      required: false
      selector:
        select:
          options: []
          custom_value: true
    all_markers:
      name: All markers
      description: "When enabled, wires every marker that has a matching Web-IO command (ignores marker_id)."
      default: false
      selector:
        boolean:
    marker_id:
      name: Marker IDs
      description: "Comma-separated list of marker IDs (e.g. 'M10,M11,42'). M prefix optional. '*' = all."
      default: "2"
      selector:
        text:
    canvas_format:
      name: Paper format (optional)
      description: "Paper format of the function plan. 'Auto' = determine from the plan's own settings (recommended, default)."
      default: "Auto"
      selector:
        select:
          options:
            - "Auto"
            - "A5"
            - "A4"
            - "A3"

function_plan_sort:
  name: Function Plan Sort
  description: >
    Sorts all elements of a function plan by marker ID and snaps them to exact
    grid positions. Active plans are automatically stopped, sorted and reactivated.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity."
      required: false
      selector:
        select:
          options: []
          custom_value: true
    canvas_format:
      name: Paper format (optional)
      description: "Paper format of the function plan. 'Auto' = determine from the plan's own settings (recommended, default)."
      default: "Auto"
      selector:
        select:
          options:
            - "Auto"
            - "A5"
            - "A4"
            - "A3"

function_plan_stop:
  name: Function Plan Stop
  description: Stops a function plan (stop_fup). Useful for manually testing the plan lifecycle.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity."
      required: false
      selector:
        select:
          options: []
          custom_value: true

function_plan_activate:
  name: Function Plan Activate
  description: Saves and activates a function plan (run_fup). Equivalent to manually saving/activating in the Comexio UI.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity."
      required: false
      selector:
        select:
          options: []
          custom_value: true

function_plan_restore:
  name: Function Plan Restore
  description: >
    Restores a function plan from a stored backup snapshot (auto or pre-change backup).
    Two ways to pick the target — use ONE, not both: (1) "Snapshot" (recommended) — pick
    the exact snapshot directly; (2) Advanced: Plan + Backup type + Version manually
    (e.g. for scripting). Setting "Snapshot" makes the advanced fields ignored.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    snapshot:
      name: Snapshot (recommended)
      description: "Pick the exact snapshot to restore, with plan, fub_id, type/slot and timestamp shown together. Overrides the advanced Plan/Backup type/Version fields below when set."
      required: false
      selector:
        select:
          options: []
    fub_id:
      name: Plan
      description: "Manual alternative to 'Snapshot' above (e.g. for scripting) — ignored whenever 'Snapshot' is set. Only plans with existing backup snapshots are listed. Leave both empty to use the plan selected in the 'Function Plans' entity."
      required: false
      advanced: true
      selector:
        select:
          options: []
    kind:
      name: Backup type
      description: "Manual alternative to 'Snapshot' above — ignored whenever 'Snapshot' is set. auto (default when left empty) = cyclic backups written on every detected change (3 slots per plan); change = safety snapshots taken right before each HA-side plan modification (10 slots per plan)."
      required: false
      advanced: true
      selector:
        select:
          options:
            - auto
            - change
    slot:
      name: Version / slot
      description: "Manual alternative to 'Snapshot' above — ignored whenever 'Snapshot' is set. Which version to restore: 0 (default when left empty) = newest snapshot, 1 = the one before, and so on."
      required: false
      advanced: true
      selector:
        number:
          min: 0
          max: 9
          mode: box
    on_conflict:
      name: On conflict (optional)
      description: >
        Only relevant if the plan was deleted, or its ID is now used by a different plan.
        new_id = create a fresh plan and rebuild it from the snapshot (default, safe).
        force_override = restore directly onto the live plan that now occupies this ID,
        overwriting it. Requires 'confirm'.
      required: false
      selector:
        select:
          options:
            - new_id
            - force_override
    confirm:
      name: Confirm conflict resolution
      description: >
        Must be enabled to proceed when the plan was deleted or its ID now belongs to a
        different plan. Ignored for a normal restore onto an unchanged, still-existing plan.
      default: false
      selector:
        boolean:
    as_copy:
      name: Restore as copy
      description: >
        Restore the snapshot as a brand-new, independent plan instead of overwriting the
        live plan — the source plan and its backup history are left untouched. Requires
        'new_plan_name'. Ignores on_conflict/confirm (the source plan is never touched).
      default: false
      advanced: true
      selector:
        boolean:
    new_plan_name:
      name: Name for the copy
      description: "Required when 'Restore as copy' is enabled — must not already be in use by a live plan."
      required: false
      advanced: true
      selector:
        text:
    auto_start:
      name: Auto-start after restore
      description: >
        Start/activate the plan after restoring it (default). Disabled: the snapshot's
        structure is still fully restored, but the plan is left stopped afterward.
      default: true
      selector:
        boolean:

function_plan_delete_backups:
  name: Function Plan Delete Backups
  description: >
    Deletes stored function plan backup snapshots. Pick ONE of "Snapshot" (deletes just that
    one snapshot) or "Plan" (deletes ALL snapshots — auto and change — of that plan). Leave
    BOTH empty to delete ALL stored backups of ALL plans on this instance — fresh ones are
    then rebuilt automatically starting with the next backup cycle / next plan change.
    Requires "confirm".
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    snapshot:
      name: Snapshot (optional)
      description: "Delete only this exact snapshot. Overrides 'Plan' below when set."
      required: false
      selector:
        select:
          options: []
    fub_id:
      name: Plan (optional)
      description: "Delete ALL snapshots of this plan — ignored whenever 'Snapshot' is set. Leave both empty to delete ALL backups of ALL plans."
      required: false
      advanced: true
      selector:
        select:
          options: []
    confirm:
      name: Confirm deletion
      description: "Must be enabled — otherwise nothing is deleted."
      required: true
      default: false
      selector:
        boolean:

function_plan_purge_orphaned_backups:
  name: Function Plan Purge Orphaned Backups
  description: >
    Deletes backup snapshots (auto and change) of plans that no longer exist live in Comexio
    (deleted directly in Comexio Studio) and whose newest snapshot is older than the
    configured retention period (Options → Function Plan, default 6 months). A live plan's
    backups are never touched, no matter how old. Runs automatically on the periodic backup
    cycle too — this service is normally only needed to force an out-of-schedule cleanup.
    Requires "confirm".
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    confirm:
      name: Confirm purge
      description: "Must be enabled — otherwise nothing is purged."
      required: true
      default: false
      selector:
        boolean:

function_plan_list_backups:
  name: Function Plan List Backups
  description: Returns stored function plan backup snapshots (plan, type, slot, timestamp, operation) as a service response. All filters are optional and can be combined.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional filter)
      description: "Only show snapshots of this plan."
      required: false
      selector:
        select:
          options: []
    plan_name:
      name: Plan name contains (optional filter)
      description: "Only show snapshots whose plan name contains this text (case-insensitive), e.g. 'Licht'."
      required: false
      selector:
        text:
    kind:
      name: Backup type (optional filter)
      description: "Only show one backup type: auto (cyclic) or change (pre-change safety snapshots)."
      required: false
      default: all
      selector:
        select:
          options:
            - all
            - auto
            - change
    slot:
      name: Version / slot (optional filter)
      description: "Only show snapshots in this slot (0 = newest). Leave empty for all slots."
      required: false
      selector:
        number:
          min: 0
          max: 9
          mode: box
    max_age:
      name: Maximum age (optional filter)
      description: "Only show snapshots captured within this time span (e.g. last 2 hours)."
      required: false
      selector:
        duration:
          enable_day: true
    order_by:
      name: Sort by (optional)
      description: "Sort order of the results: newest / oldest (by timestamp), plan (name A-Z, newest first within a plan), fub_id (plan ID, newest first within a plan), slot (slot number, then plan name)."
      required: false
      default: newest
      selector:
        select:
          options:
            - newest
            - oldest
            - plan
            - fub_id
            - slot
    export_as_json:
      name: Export as JSON (optional)
      description: "If enabled, the response contains ONLY a 'json' field with the full result as a pretty-printed JSON string — handy to copy & paste into a shell (e.g. PowerShell) for further filtering. Disabled (default): the normal structured fields."
      required: false
      default: false
      selector:
        boolean:
    diff:
      name: Include diff vs. predecessor (optional)
      description: "If enabled, each snapshot (except the oldest known one of its plan/type) gets a 'diff' field showing added/removed markers, IOs and connections compared to the previous snapshot of the SAME type (auto vs. auto, change vs. change). Disabled by default to keep the normal response compact."
      required: false
      default: false
      selector:
        boolean:

function_plan_visualize:
  name: Function Plan Visualize
  description: >
    Shows the state of a function plan (connections + unwired elements) as a text
    notification or as an SVG diagram (Plan Preview sensor). Two ways to pick the
    source — use only ONE: (1) "Snapshot" — render a stored backup exactly as
    captured, entirely offline; (2) "Plan" (live) — render the plan's current
    state in Comexio.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity. Ignored when 'Snapshot' is set."
      required: false
      selector:
        select:
          options: []
          custom_value: true
    snapshot:
      name: Snapshot (optional)
      description: "Render this stored backup snapshot instead of the live plan — no Comexio connection needed. Overrides 'Plan' above when set."
      required: false
      advanced: true
      selector:
        select:
          options: []
    format:
      name: Output format (optional)
      description: "text (default) = notification listing connections/unwired elements. svg = renders a diagram at Comexio's original layout positions into the Plan Preview sensor (entity_picture) and returns its URL."
      required: false
      default: text
      selector:
        select:
          options:
            - text
            - svg

function_plan_analyze:
  name: Function Plan Analyze
  description: >
    Manual, read-only "plan health check": looks for likely wiring mistakes (conflicting
    writers, unwired Set/Reset pins, dead outputs, suspicious double-wiring) and also
    reports recognized benign patterns (the "virtueller Taster" self-reset idiom). Never
    scheduled automatically. Result comes back as a service response only — no
    notification — meant to be shown in a popup by the Plan Preview card. Two ways to
    pick the source — use only ONE: (1) "Snapshot" — analyze a stored backup exactly as
    captured, entirely offline; (2) "Plan" (live) — analyze the plan's current state in
    Comexio.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity. Ignored when 'Snapshot' is set."
      required: false
      selector:
        select:
          options: []
          custom_value: true
    snapshot:
      name: Snapshot (optional)
      description: "Analyze this stored backup snapshot instead of the live plan — no Comexio connection needed. Overrides 'Plan' above when set."
      required: false
      advanced: true
      selector:
        select:
          options: []

function_plan_flow_diagram:
  name: Function Plan Flow Diagram
  description: >
    Renders a plan as a signal-flow diagram: elements arranged by topological order
    (input -> logic -> output, top to bottom) instead of their physical Comexio Studio
    position. Elements with no wiring at all are left out. Never scheduled
    automatically. Result comes back as a service response only (the SVG itself) — no
    notification — meant to be shown in a popup by the Plan Preview card. Two ways to
    pick the source — use only ONE: (1) "Snapshot" — render a stored backup exactly as
    captured, entirely offline; (2) "Plan" (live) — render the plan's current state in
    Comexio.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    fub_id:
      name: Plan (optional)
      description: "Pick a plan, or leave empty to use the plan selected in the 'Function Plans' entity. Ignored when 'Snapshot' is set."
      required: false
      selector:
        select:
          options: []
          custom_value: true
    snapshot:
      name: Snapshot (optional)
      description: "Render this stored backup snapshot instead of the live plan — no Comexio connection needed. Overrides 'Plan' above when set."
      required: false
      advanced: true
      selector:
        select:
          options: []

set_value:
  name: Set Value
  description: >
    Writes a raw value to a Comexio marker or IO via the Comexio API (Basic Auth).
    Same write path the switch/number entities use — this is the backend of the plan
    card's debug command input. Only targets known from the current Comexio
    configuration are accepted. Failures are reported as a notification.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    target:
      name: Target
      description: "Marker as 'M<id>' (e.g. M107) or IO as '<Extension>#<IO>' (e.g. IOX2#Q3)."
      required: true
      selector:
        text:
    value:
      name: Value
      description: "Value to write — digital: 0/1, analog: any number (comma or dot decimals)."
      required: true
      selector:
        text:

function_plan_debug_session:
  name: Function Plan Debug Session
  description: >
    Internal — called by the plan card when its debug box opens or closes. Switches how
    often the live plan preview's wire colors are refreshed from Comexio: faster while the
    debug box is open, background cadence otherwise. No effect without a live preview on
    display.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    open:
      name: Debug box open
      description: "true = the plan card's debug box just opened (fast refresh); false = it closed (background refresh)."
      default: false
      selector:
        boolean:

function_plan_preview_extend:
  name: Function Plan Preview Extend
  description: >
    Internal — called by the plan card's debug box `/extend <minutes>` command. Extends
    how long the currently displayed live plan preview keeps polling Comexio for wire
    values before auto-stopping (default 15 minutes, to avoid a forgotten open tab
    polling forever). Applies only to the plan preview currently on display, in memory
    only — the next plan you open resets to the 15-minute default. No effect without a
    live preview on display.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    minutes:
      name: Minutes
      description: New auto-stop window in minutes, counted from now (1-1440).
      required: true
      selector:
        number:
          min: 1
          max: 1440
          mode: box

function_plan_search:
  name: Function Plan Search
  description: >
    Finds which function plans contain elements matching a text query. Searches the
    human-readable labels of every element (markers, IOs, WebIOs, blocks, time modules,
    constants, comments) in every live plan — same syntax as the preview card's search
    box: plain text = case-insensitive substring, wildcards ? (one non-space character)
    and * (anything). Results come as a notification and as a service response. When
    exactly ONE plan matches, the 'Function Plans' selector is set to it automatically —
    a follow-up action without a plan then targets the plan just found.
  fields:
    config_entry:
      name: Comexio instance
      description: Select the Comexio instance (optional with a single instance).
      required: false
      selector:
        config_entry:
          integration: comexio
    query:
      name: Search text
      description: "e.g. 'M33', 'Küche', 'M22?' or 'IOX3 #*'"
      required: true
      selector:
        text:
```

---

*[← Back to README](README.md)*

---

# 🇩🇪 Deutsch

🌍 *[🇬🇧 Read this in English](#comexio--function-plan-preview-guide)*

Diese Anleitung beschreibt die **Logikplan-Vorschau** — ein interaktives, live
aktualisiertes Diagramm eines Comexio-Funktionsplans (Logikplan), direkt in Home
Assistant dargestellt. Für die allgemeine Einrichtung siehe die
[Konfigurationsanleitung](CONFIGURATION.md), für die vollständige Aktionsliste siehe die
[README](README.md#-funktionsplan-verwaltung-logikplan).

---

## Inhaltsverzeichnis

1. [Überblick](#1-überblick)
2. [Installation der Dashboard-Karte](#2-installation-der-dashboard-karte)
3. [Karte zum Dashboard hinzufügen](#3-karte-zum-dashboard-hinzufügen)
4. [Plan auswählen und rendern](#4-plan-auswählen-und-rendern)
5. [Funktionen der Karte](#5-funktionen-der-karte)
6. [Zugehörige Aktionen](#6-zugehörige-aktionen)
7. [Aktions-Referenz (services.yaml)](#7-aktions-referenz-servicesyaml)

---

## 1. Überblick

Die Logikplan-Vorschau rendert einen Comexio-Funktionsplan als SVG-Diagramm im selben
Layout, das auch Comexio Studio verwendet — gleiche Element-Positionen, gleiche
WebIO-Beschriftungen, gleiche Drahtführung. Sie dient zwei Zwecken:

- **Visuelle Kontrolle** — die Verdrahtung eines Plans ansehen, ohne Comexio Studio zu
  öffnen.
- **Live-Debugging** — Drähte werden **rot**, solange ihr Wert "high" (`1`) ist, und
  das aktualisiert sich automatisch. Das gilt sowohl für echte Merker/IOs als auch für
  bausteininterne Ausgänge (z. B. ein Und/Oder-Gatter oder ein Zeitglied ohne eigenen
  Merker).

Zusätzlich kann ein **gespeicherter Backup-Snapshot** komplett offline dargestellt
werden (keine Live-Verbindung zu Comexio nötig) — praktisch, um vor einem
`function_plan_restore` zu prüfen, ob es wirklich der gewünschte Stand ist.

Das Feature besteht aus drei Integrations-Entitäten (Auswahl, Taster, Bild) sowie einer
optionalen Custom-Dashboard-Karte (`comexio-plan-card`), die aus dem reinen Bild ein
interaktives Diagramm mit Suche, Hover und Debug-Konsole macht.

---

## 2. Installation der Dashboard-Karte

Die Render-Engine und ihre drei Entitäten benötigen keine zusätzliche Einrichtung — sie
werden automatisch angelegt. Die **interaktive Karte** ist jedoch eine separate
Frontend-Ressource, die HACS nicht automatisch registriert (sie liegt als `.js`-Datei
innerhalb der Integration, nicht als eigenes HACS-"Plugin"-Repository). Einmalig
hinzufügen:

1. **Beide** Dateien `custom_components/comexio/frontend/comexio-plan-card.js` und
   `custom_components/comexio/frontend/comexio-plan-card-utils.js` in den
   `config/www/`-Ordner von Home Assistant kopieren (die Hauptdatei importiert die
   zweite — beide müssen im selben Ordner liegen).
2. **Einstellungen → Dashboards → ⋮ (oben rechts) → Ressourcen → Ressource
   hinzufügen** öffnen.
3. URL: `/local/comexio-plan-card.js`, Ressourcentyp: **JavaScript-Modul**.
4. Browser per Hard-Refresh neu laden. In der Browser-Konsole sollte
   `comexio-plan-card vX.Y.Z loaded` erscheinen.

> **Hinweis:** Browser (und der Service Worker von Home Assistant) cachen
> JavaScript-Module mitunter hartnäckig. Wird die Datei später aktualisiert, empfiehlt
> sich ein neuer Dateiname (z. B. `comexio-plan-card-v2.js`) samt angepasster
> Ressourcen-URL — nur den `?v=`-Parameter hochzuzählen wird nicht zuverlässig erkannt.

Ohne diese Custom-Karte funktioniert die reine `image.*_plan_preview`-Entität auch auf
einer normalen **Bild-Entität**-Karte — dabei fehlen dann aber Suche, Hover, Live-
Drahtfarben und die Debug-Box (das Bild selbst wird korrekt dargestellt, die
Bild-Entität-Karte kann nur nicht damit interagieren).

---

## 3. Karte zum Dashboard hinzufügen

Drei Entitäten arbeiten zusammen (Instanzname `iosrv1` durch die eigene Instanz
ersetzen):

| Entität | Rolle |
|---|---|
| `select.iosrv1_function_plans` | Plan für die Vorschau auswählen. |
| `button.iosrv1_preview` | Ausgewählten Plan rendern (erzeugt/aktualisiert das SVG). |
| `image.iosrv1_plan_preview` | Enthält das zuletzt gerenderte SVG — Bildquelle für die Karte. |

Beispiel-Dashboard-Abschnitt:

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: select.iosrv1_function_plans
  - type: tile
    entity: button.iosrv1_preview
  - type: custom:comexio-plan-card
    entity: image.iosrv1_plan_preview
```

> Die Auswahl-Entität heißt in der Home-Assistant-Oberfläche unabhängig von der
> Sprache **"Function Plans"** (bewusst nicht übersetzt).

---

## 4. Plan auswählen und rendern

- Plan in der **Function Plans**-Auswahl wählen, dann einmal den **Vorschau**-Taster
  drücken.
- Ist nichts ausgewählt, verwenden Aktionen den zuletzt von der Integration
  verwendeten Plan.
- **Strukturelle** Änderungen (Elemente/Drähte in Comexio hinzugefügt/entfernt)
  benötigen einen erneuten Tasterdruck. **Live-Drahtfarben** hingegen aktualisieren
  sich danach von selbst — kein weiterer Druck nötig, solange sich nur Werte ändern.
- Um stattdessen ein **gespeichertes Backup** anzuzeigen, die Aktion
  `function_plan_visualize` mit dem Feld `snapshot` und `format: svg` verwenden — das
  funktioniert vollständig offline.

---

## 5. Funktionen der Karte

- **Suchleiste** — Groß-/Kleinschreibung wird ignoriert, Substring-Suche über jede
  Element-Beschriftung (Merker, IOs, WebIOs, Bausteine, Konstanten, Kommentare).
  Platzhalter: `?` steht für genau ein Nicht-Leerzeichen, `*` für beliebig viele
  Zeichen. Treffer bekommen einen hervorgehobenen Rahmen, der Rest wird abgedunkelt,
  ein Trefferzähler wird angezeigt. Dieselbe Syntax nutzt die Aktion
  `function_plan_search`.
- **Live-Drahtfarben** — ein Draht wird rot, sobald sein Wert `1` (digital high) ist.
  Das gilt sowohl für echte Merker/IOs als auch für bausteininterne Ausgänge, über
  einen Hintergrund-Abruf der Live-Verbindungswerte von Comexio, der nur läuft,
  solange ein Plan angezeigt wird.
- **Hover & Tooltips** — beim Überfahren eines Elements erscheint dessen volle
  Beschriftung als natives Browser-Tooltip; beim Überfahren eines Drahts werden alle
  Äste desselben elektrischen Netzes gemeinsam hervorgehoben.
- **Zoom-Steuerung** — Lupe-Minus/Plus-Buttons neben der Suchleiste; die Zoomstufe
  wird pro Browser gemerkt. Klick auf die Prozent-Anzeige setzt auf 100 % zurück.
- **Debug-Box** — über das Konsolen-Icon in der Werkzeugleiste umschaltbar. Zeigt ein
  Live-Protokoll der Wertänderungen des aktuell angezeigten Plans (gespeist aus
  denselben Webhook-Pushes, die die Integration ohnehin empfängt), dazu:
  - ein Eingabefeld, um direkt einen Wert zu schreiben, z. B. `M107=1` oder
    `IOX2#Q3=0,5` (nutzt die Aktion `set_value`);
  - Befehlshistorie (Pfeiltasten ↑ / ↓) und Autovervollständigung über die Ziele des
    angezeigten Plans;
  - Klick auf ein Element übernimmt dessen Adresse ins Eingabefeld; Doppelklick auf ein
    beschreibbares digitales Element schaltet dessen Wert sofort um;
  - ein Ausschlussfilter (gleiche Platzhalter-Syntax wie die Suche), um störende
    Einträge auszublenden — beim ersten Öffnen in einem Browser standardmäßig
    `*#TL1, *#UL1, *#AI*, *#QI*`;
  - Pause/Wiedergabe sowie ein Löschen-Button (Radiergummi).

  Beim Öffnen der Debug-Box schaltet der Live-Werte-Abruf zusätzlich auf ein
  schnelleres Intervall um (0,5 s statt der üblichen 2 s) — für unmittelbareres
  Feedback während des aktiven Debuggens.

---

## 6. Zugehörige Aktionen

Verfügbar unter **Entwicklerwerkzeuge → Aktionen**:

| Aktion | Was sie tut |
|---|---|
| `comexio.function_plan_visualize` | Rendert einen Plan (oder einen gespeicherten Snapshot) als SVG in die Plan-Vorschau-Bildentität — oder liefert eine Text-Übersicht der Verbindungen und unverbundenen Elemente. |
| `comexio.function_plan_search` | Findet, welche Pläne Elemente enthalten, die zu einem Suchtext passen — gleiche Syntax wie die Suchleiste der Karte. Wählt den Plan automatisch aus, wenn genau ein Treffer gefunden wird. |
| `comexio.set_value` | Schreibt einen Rohwert auf einen Merker oder eine IO — das Backend des Eingabefelds der Debug-Box. |
| `comexio.function_plan_debug_session` | Intern — wird automatisch von der Karte aufgerufen, wenn deren Debug-Box geöffnet oder geschlossen wird. Nicht für manuelle Nutzung gedacht. |

---

## 7. Aktions-Referenz (services.yaml)

Zur Referenz der **aktuelle** Inhalt von `custom_components/comexio/services.yaml`, der
sämtliche Aktionen der Integration definiert (einschließlich der oben gelisteten):

*(identisch zum englischen Abschnitt oben — [siehe dort](#7-action-reference-servicesyaml), die Datei enthält ausschließlich englischsprachige Feldnamen/Beschreibungen, wie in der gesamten Integration üblich.)*

---

*[← Zurück zur README](README.md)*
