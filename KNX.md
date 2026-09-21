# Comexio — KNX Objects Guide

🌍 *[🇩🇪 Auf Deutsch lesen (Read this in German)](#-deutsch)*

This guide covers **KNX object support** — importing Comexio's KNX/EIB gateway objects ("K-Elements") as Home Assistant entities, automatic unit/device-class detection, dimmer/blind handling, and how the integration classifies ambiguous digital datapoint types. For general setup and options, see the [Configuration Guide](CONFIGURATION.md); for the full action list, see the [README](README.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Enabling KNX Entities](#2-enabling-knx-entities)
3. [Naming and Excluding Objects](#3-naming-and-excluding-objects)
4. [Automatic Unit & Device-Class Detection](#4-automatic-unit--device-class-detection)
5. [Dimmers & Blinds (DPT3.x)](#5-dimmers--blinds-dpt3x)
6. [Datapoint-Type (DPT) Classification](#6-datapoint-type-dpt-classification)
7. [The Classification Repair Flow](#7-the-classification-repair-flow)
8. [Manual Suffixes](#8-manual-suffixes)
9. [Known Limitations](#9-known-limitations)

---

## 1. Overview

Comexio can bridge a KNX/EIB installation through its own KNX gateway; every bound KNX group address shows up in Comexio as a **K-Element** ("KNX object"), alongside Markers and physical IOs. This integration can import those K-Elements as Home Assistant entities the same way it does Markers and IOs — opt-in, since not every installation uses KNX.

## 2. Enabling KNX Entities

Turn on **Create entities for KNX objects** in the integration options (off by default). Once enabled, every K-Element in Comexio is picked up on the next poll and exposed as an entity — `switch`/`number` for writable digital/analog objects, `sensor`/`binary_sensor` for read-only ones (see [Section 6](#6-datapoint-type-dpt-classification)), or `cover`/`light` for dimmer/blind pairs (see [Section 5](#5-dimmers--blinds-dpt3x)).

## 3. Naming and Excluding Objects

- **Naming Schema for KNX objects** (default `K{KnxId} {KnxTitle}`) works exactly like the Marker/IO naming schemas — available placeholders: `{KnxId}`, `{KnxTitle}`.
- **KNX object IDs to exclude from function plan connection** lets you ignore individual K-Elements by ID (comma-, semicolon-, space- or dot-separated, with or without the `K` prefix, ranges like `8-12` supported) — exactly like the Marker ignore list. An ignored K-Element gets no HA entity and is skipped by the sync/connect actions entirely.

## 4. Automatic Unit & Device-Class Detection

Every KNX object carries a real KNX datapoint type (DPT) with a well-defined physical meaning — the integration looks this up in Comexio's own DPT catalog and uses it to configure the HA entity correctly, instead of leaving it as a raw, unitless number:

- **Analog objects** (`number`/`sensor`) get their **min/max range, step size, and unit** set from the DPT's real wire encoding — e.g. a byte-scaled DPT5.001 ("Scaling", 0–100 %) gets `step = 100/255`, while an integer DPT7.x gets `step = 1`. Where HA has a matching `device_class` (e.g. `temperature` for DPT9.001, `illuminance` for DPT9.004, `energy` for DPT13.010/13.013, `voltage`/`current`/`power`/`pressure`/`humidity`/`wind_speed` for the corresponding DPT9 subtypes, `duration` for DPT7/8 time values), it is set automatically too.
- **The 3 unambiguous digital DPTs** ([Section 6](#6-datapoint-type-dpt-classification)) also get a matching `device_class` once tagged `[RO]` and exposed as `binary_sensor`: `1.005` (Alarm) → `problem`, `1.018` (Presence) → `occupancy`, `1.019` (Window/Door) → `door` (best-effort — KNX itself doesn't distinguish door from window contacts at the DPT level). The 5 ambiguous DPT1.x subtypes intentionally get no `device_class` — HA's `SwitchDeviceClass` has no matching value for them.

None of this requires configuration — it is derived purely from the DPT and applied automatically as each K-Element is picked up.

## 5. Dimmers & Blinds (DPT3.x)

Comexio splits a KNX **dimmer** (DPT3.007) or **blind/shutter** (DPT3.008) group address into *two* K-Elements sharing one KNX device: a digital direction bit and a 3-bit step code (move/stop). Rather than exposing these as two unrelated `switch`/`number` entities, the integration recognizes the pair and builds a single, proper HA entity instead:

- **DPT3.008 → `cover`:** exposes `open`/`close`/`stop`. Since KNX blinds/shutters carry no absolute position telegram, the entity's position is always unknown (`is_closed` stays `None`) — this is a control-only entity, not a position readout.
- **DPT3.007 → `light`:** exposes a full brightness slider (`light.turn_on(brightness=...)`, `light.turn_off`). Since a KNX dimmer has no readback channel either, the brightness is a **best-effort HA-side estimate** — the integration times how long it holds the move telegram based on the requested brightness delta (assuming a configurable full 0–255 sweep time), then sends a stop telegram. The estimate is persisted across HA restarts but will drift if the actuator is also operated manually or by another system, since there is no way to read the real value back from the bus.

Both directions always write through the same KNX bridge Marker mechanism the integration already uses for plain KNX writes — nothing about the underlying sync/wiring changes.

## 6. Datapoint-Type (DPT) Classification

Digital K-Elements are classified by their **KNX datapoint type (DPT)**, because the same DPT can mean very different things depending on the real-world wiring:

- **Unambiguous DPTs** — `1.005` (Alarm), `1.018` (Presence), `1.019` (Window/Door) are, in practice, always a read-only status telegram. The integration automatically renames the underlying K-Element in Comexio (never the raw KNX bus point) to append `[RO]`, so it is exposed as a `sensor`/`binary_sensor` instead of a writable `switch`. This happens silently on the next poll — no confirmation needed, since a real ETS import never carries this suffix to begin with, and once tagged the object no longer matches the auto-classification candidates again.
- **Ambiguous DPTs** — `1.001` (Schalter), `1.002` (Bool), `1.003` (Freigabe), `1.004` (Flanke), `1.006` (Binärwert) are used in the field for genuine toggle switches, momentary push-buttons ("Taster" — by far the most common real-world wiring for KNX switch objects), *and* pure status readbacks alike. The DPT alone can't tell these apart — see [Section 7](#7-the-classification-repair-flow).

### DPT → HA Entity Reference

**Digital (DPT1.x):**

| DPT | KNX Name | Classification | Resulting Entity | `device_class` |
| :--- | :--- | :--- | :--- | :--- |
| 1.001 | Schalter (Switch) | ambiguous | `switch` (default) / `binary_sensor` (`[RO]`) / `button` (`[TRIG]`) | — |
| 1.002 | Bool | ambiguous | same as above | — |
| 1.003 | Freigabe (Enable) | ambiguous | same as above | — |
| 1.004 | Flanke (Ramp) | ambiguous | same as above | — |
| 1.006 | Binärwert (Binary value) | ambiguous | same as above | — |
| **1.005** | **Alarm** | **unambiguous — always auto-tagged `[RO]`, never a writable switch** | `binary_sensor` | `problem` |
| **1.018** | **Anwesenheit (Presence)** | **unambiguous — always auto-tagged `[RO]`, never a writable switch** | `binary_sensor` | `occupancy` |
| **1.019** | **Tür/Fenster (Window/Door)** | **unambiguous — always auto-tagged `[RO]`, never a writable switch** | `binary_sensor` | `door` |

> **1.005, 1.018 and 1.019 are always read-only** — there is no choice involved and no Repair issue for them. The integration renames the K-Element with `[RO]` on the next poll automatically, exactly as described above; they can never end up as a writable `switch`, unlike the 5 ambiguous DPT1.x subtypes above them.

**Analog (writable → `number`, `[RO]`-tagged → `sensor`):**

| DPT | KNX Name | Unit | `device_class` |
| :--- | :--- | :--- | :--- |
| 5.001 | Scaling | % | — |
| 5.003 | Angle | ° | — |
| 5.004 | Percent_U8 | % | — |
| 5.005 | DecimalFactor | — | — |
| 5.006 | Tariff | — | — |
| 5.010 | Value_1_Ucount (pulse counter) | — | — |
| 6.001 | Percent_V8 | % | — |
| 6.010 | Value_1_Count | — | — |
| 7.001 | Value_2_Ucount | — | — |
| 7.002 | Value_2_Ucount | ms | `duration` |
| 7.003 | Value_2_Ucount | 10ms | — |
| 7.004 | Value_2_Ucount | 100ms | — |
| 7.005 | Value_2_Ucount | s | `duration` |
| 7.006 | Value_2_Ucount | min | `duration` |
| 7.007 | Value_2_Ucount | h | `duration` |
| 8.001 | Value_2_Count | — | — |
| 8.002 | Value_2_Count | ms | `duration` |
| 8.003 | Value_2_Count | 10ms | — |
| 8.004 | Value_2_Count | 100ms | — |
| 8.005 | Value_2_Count | s | `duration` |
| 8.006 | Value_2_Count | min | `duration` |
| 8.007 | Value_2_Count | h | `duration` |
| 8.010 | Percent_V16 | % | — |
| 9.001 | Value_Temp | °C | `temperature` |
| 9.002 | Value_Tempd (temperature difference) | K | `temperature_delta` |
| 9.004 | Value_Lux | lx | `illuminance` |
| 9.005 | Value_Wsp (wind speed) | m/s | `wind_speed` |
| 9.006 | Value_Pres (pressure) | Pa | `pressure` |
| 9.007 | Value_Humidity | % | `humidity` |
| 9.008 | Value_AirQuality | ppm | — |
| 9.020 | Value_Volt | V | `voltage` |
| 9.021 | Value_Curr | mA | `current` |
| 9.024 | Power | kW | `power` |
| 12.001 | Value_4_Ucount | — | — |
| 13.001 | Value_4_Count | — | — |
| 13.010 | ActiveEnergy | Wh | `energy` |
| 13.011 | ApparentEnergy | VAh | — |
| 13.012 | ReactiveEnergy | VARh | — |
| 13.013 | ActiveEnergy_kWh | kWh | `energy` |
| 13.014 | ApparentEnergy_kVAh | kVAh | — |
| 13.015 | ReactiveEnergy_kVARh | kVARh | — |
| 13.100 | LongDeltaTimeSec | s | `duration` |
| 17.001 | SceneNumber | — | — |
| 18.001 | SceneControl (scene number half) | — | — |

3.007 and 3.008 (Dimmer/Blind) are excluded above — they never end up as a plain `number`, see [Section 5](#5-dimmers--blinds-dpt3x). A missing `device_class` doesn't mean the entity is unstyled — it still gets the correct unit, min/max and step, just no HA-native unit-conversion/statistics category; it also always gets a KNX icon.

## 7. The Classification Repair Flow

For every currently-ambiguous, not-yet-classified K-Element, the integration raises a HA **Repair issue** titled *"KNX objects need classification (N)"* (Settings → System → Repairs). Opening it walks through the objects **one at a time** — each form shows the object's name and how many more are left, with three choices:

| Option | Effect |
| :--- | :--- |
| 🔇 **Leave as a writable switch** *(default)* | No change in Comexio. The object's ID is remembered internally so it won't be asked about again — unless the classification is manually reverted (see below). |
| 🔒 **Read-only sensor** (`[RO]`) | Renames the K-Element in Comexio, appending `[RO]`. The integration reloads so the entity switches from `switch` to `sensor`/`binary_sensor`. |
| 🔘 **Push-button / Taster** (`[TRIG]`) | Renames the K-Element in Comexio, appending `[TRIG]`. Wired through the same auto-managed trigger function plan already used for `[TRIG]`-tagged Markers — pressing the HA button fires a one-shot pulse instead of toggling a persistent state. |

The default is deliberately the safe no-op (**Leave**), not a rename — the form advances to the next object immediately after submit, so a default that already commits a rename would risk misclassifying an object if you click/confirm through the list too quickly. A rename can't be undone by this flow once applied, while "leave" can always be revisited later.

The issue is re-evaluated on **every coordinator poll** (not just once) — an object you've already answered won't reappear (renamed objects no longer match the ambiguous-DPT criteria; "leave" answers are excluded via the internal ignore list), but a genuinely new ambiguous K-Element (freshly added in Comexio, or a suffix removed manually) will raise the issue again. There is no dedicated action to force this check — a normal integration reload (*Settings → Devices & Services → Comexio → ⋮ → Reload*) triggers an immediate poll and thus an immediate re-check instead of waiting for the next scheduled one.

## 8. Manual Suffixes

Just like Markers, a KNX object's `[RO]`/`[TRIG]` (or legacy `[TP]`) suffix can also be added manually in Comexio at any time — the integration only automates the two cases in [Section 6](#6-datapoint-type-dpt-classification).

## 9. Known Limitations

- **KNX analog values above ~1,000,000 get rounded/corrupted:** This is a Comexio firmware bug, not an integration issue — confirmed present up to firmware 11.1.4, with a fix planned for a later release. The integration sets each KNX object's Web-IO Min/Max to its real DPT range (e.g. DPT12.001's 0..4294967295), so an object whose DPT range exceeds ~1,000,000 can hit this bug on affected firmware; objects with a narrower DPT range (the vast majority) are unaffected.
- **Dimmer/blind brightness and open/closed state are estimates, not readouts:** see [Section 5](#5-dimmers--blinds-dpt3x) — DPT3.x has no bus readback channel at all, so HA's notion of the current state can drift from reality if the actuator is also operated another way.

---

*[← Back to README](README.md)*

---

# 🇩🇪 Deutsch

🌍 *[🇬🇧 Read this in English](#comexio--knx-objects-guide)*

Diese Anleitung beschreibt die **KNX-Objekt-Unterstützung** — den Import von Comexios KNX/EIB-Gateway-Objekten ("K-Elemente") als Home-Assistant-Entitäten, die automatische Einheiten-/Device-Class-Erkennung, die Behandlung von Dimmern/Jalousien sowie die Klassifizierung mehrdeutiger digitaler Datenpunkttypen. Für die allgemeine Einrichtung siehe die [Konfigurationsanleitung](CONFIGURATION.md), für die vollständige Aktionsliste siehe die [README](README.md).

---

## Inhaltsverzeichnis

1. [Überblick](#1-überblick)
2. [KNX-Entitäten aktivieren](#2-knx-entitäten-aktivieren)
3. [Benennung und Ausschluss von Objekten](#3-benennung-und-ausschluss-von-objekten)
4. [Automatische Einheiten- und Device-Class-Erkennung](#4-automatische-einheiten--und-device-class-erkennung)
5. [Dimmer & Jalousien (DPT3.x)](#5-dimmer--jalousien-dpt3x)
6. [Datenpunkttyp-(DPT)-Klassifizierung](#6-datenpunkttyp-dpt-klassifizierung)
7. [Der Klassifizierungs-Reparatur-Flow](#7-der-klassifizierungs-reparatur-flow)
8. [Manuelle Suffixe](#8-manuelle-suffixe)
9. [Bekannte Einschränkungen](#9-bekannte-einschränkungen)

---

## 1. Überblick

Comexio kann eine KNX/EIB-Installation über sein eigenes KNX-Gateway anbinden; jede gebundene KNX-Gruppenadresse erscheint in Comexio als **K-Element** ("KNX-Objekt"), neben Merkern und physischen IOs. Diese Integration kann diese K-Elemente genau wie Merker und IOs als Home-Assistant-Entitäten importieren — als Opt-in, da nicht jede Installation KNX nutzt.

## 2. KNX-Entitäten aktivieren

Aktiviere **KNX-Objekte als Entitäten anlegen** in den Integrations-Optionen (standardmäßig aus). Ist die Option aktiv, wird jedes K-Element in Comexio beim nächsten Poll erkannt und als Entität angelegt — `switch`/`number` für beschreibbare digitale/analoge Objekte, `sensor`/`binary_sensor` für reine Lese-Objekte (siehe [Abschnitt 6](#6-datenpunkttyp-dpt-klassifizierung)), oder `cover`/`light` für Dimmer-/Jalousie-Paare (siehe [Abschnitt 5](#5-dimmer--jalousien-dpt3x)).

## 3. Benennung und Ausschluss von Objekten

- **Namensschema für KNX-Objekte** (Standard `K{KnxId} {KnxTitle}`) funktioniert genau wie die Merker-/IO-Namensschemas — verfügbare Platzhalter: `{KnxId}`, `{KnxTitle}`.
- **KNX-Objekt-IDs vom Logikplan-Verbinden ausschließen** ermöglicht es, einzelne K-Elemente per ID zu ignorieren (Komma-, Semikolon-, Leerzeichen- oder Punkt-getrennt, mit oder ohne `K`-Präfix, Bereiche wie `8-12` möglich) — genau wie bei der Merker-Ignorierliste. Ein ignoriertes K-Element erhält keine HA-Entität und wird von Sync-/Connect-Aktionen komplett übersprungen.

## 4. Automatische Einheiten- und Device-Class-Erkennung

Jedes KNX-Objekt trägt einen echten KNX-Datenpunkttyp (DPT) mit fest definierter physikalischer Bedeutung — die Integration schlägt diesen im DPT-Katalog von Comexio selbst nach und konfiguriert die HA-Entität entsprechend, statt sie als rohen, einheitenlosen Zahlenwert zu belassen:

- **Analoge Objekte** (`number`/`sensor`) bekommen ihren **Min/Max-Bereich, ihre Schrittweite und ihre Einheit** aus der realen Bus-Kodierung des DPT gesetzt — z. B. erhält ein byte-skalierter DPT5.001 ("Scaling", 0–100 %) `step = 100/255`, während ein ganzzahliger DPT7.x `step = 1` bekommt. Wo HA eine passende `device_class` kennt (z. B. `temperature` für DPT9.001, `illuminance` für DPT9.004, `energy` für DPT13.010/13.013, `voltage`/`current`/`power`/`pressure`/`humidity`/`wind_speed` für die entsprechenden DPT9-Subtypen, `duration` für DPT7/8-Zeitwerte), wird sie ebenfalls automatisch gesetzt.
- **Die 3 eindeutigen digitalen DPTs** ([Abschnitt 6](#6-datenpunkttyp-dpt-klassifizierung)) bekommen ebenfalls eine passende `device_class`, sobald sie mit `[RO]` getaggt und als `binary_sensor` exponiert sind: `1.005` (Alarm) → `problem`, `1.018` (Anwesenheit) → `occupancy`, `1.019` (Tür/Fenster) → `door` (Best-Effort — KNX unterscheidet auf DPT-Ebene selbst nicht zwischen Tür- und Fensterkontakt). Die 5 mehrdeutigen DPT1.x-Subtypen bekommen bewusst keine `device_class` — HAs `SwitchDeviceClass` kennt dafür keinen passenden Wert.

Das alles erfordert keine Konfiguration — es wird rein aus dem DPT abgeleitet und automatisch angewendet, sobald das K-Element erkannt wird.

## 5. Dimmer & Jalousien (DPT3.x)

Comexio zerlegt eine KNX-**Dimmer**- (DPT3.007) oder **Jalousie/Rollo**-Gruppenadresse (DPT3.008) in *zwei* K-Elemente, die sich ein KNX-Gerät teilen: ein digitales Richtungsbit und einen 3-Bit-Schrittcode (Fahren/Stopp). Statt diese als zwei unabhängige `switch`/`number`-Entitäten zu exponieren, erkennt die Integration das Paar und baut daraus eine einzige, passende HA-Entität:

- **DPT3.008 → `cover`:** bietet `open`/`close`/`stop`. Da KNX-Jalousien/Rollos kein absolutes Positions-Telegramm senden, bleibt die Position der Entität immer unbekannt (`is_closed` bleibt `None`) — reine Steuer-Entität, kein Positions-Readout.
- **DPT3.007 → `light`:** bietet einen vollen Helligkeits-Slider (`light.turn_on(brightness=...)`, `light.turn_off`). Da auch ein KNX-Dimmer keinen Rücklesekanal hat, ist die Helligkeit eine **HA-seitige Best-Effort-Schätzung** — die Integration misst, wie lange sie das Fahr-Telegramm je nach angeforderter Helligkeitsänderung hält (basierend auf einer konfigurierbaren vollen 0–255-Fahrzeit), und sendet danach ein Stopp-Telegramm. Die Schätzung wird über HA-Neustarts hinweg gespeichert, driftet aber, wenn der Aktor auch manuell oder von einem anderen System bedient wird, da der reale Wert nicht vom Bus zurückgelesen werden kann.

Beide Richtungen schreiben immer über denselben KNX-Brücken-Merker-Mechanismus, den die Integration schon für einfache KNX-Schreibvorgänge nutzt — an der zugrundeliegenden Sync-/Verdrahtungslogik ändert sich nichts.

## 6. Datenpunkttyp-(DPT)-Klassifizierung

Digitale K-Elemente werden anhand ihres **KNX-Datenpunkttyps (DPT)** klassifiziert, da derselbe DPT je nach realer Verdrahtung ganz unterschiedliche Dinge bedeuten kann:

- **Eindeutige DPTs** — `1.005` (Alarm), `1.018` (Anwesenheit), `1.019` (Tür/Fenster) sind in der Praxis so gut wie immer eine reine Status-Rückmeldung. Die Integration benennt das zugrundeliegende K-Element in Comexio automatisch um (niemals den rohen KNX-Buspunkt selbst) und hängt `[RO]` an, sodass es als `sensor`/`binary_sensor` statt als beschreibbarer `switch` erscheint. Das geschieht beim nächsten Poll ohne Rückfrage — ein echter ETS-Import trägt dieses Suffix ohnehin nie von sich aus, und einmal getaggt fällt das Objekt aus der Kandidatenmenge für die automatische Klassifizierung heraus.
- **Mehrdeutige DPTs** — `1.001` (Schalter), `1.002` (Bool), `1.003` (Freigabe), `1.004` (Flanke), `1.006` (Binärwert) werden in der Praxis sowohl für echte Umschalter, für Taster (Momentkontakt — die mit Abstand häufigste reale Verdrahtung für KNX-Schaltobjekte) als auch für reine Status-Rückmeldungen genutzt. Der DPT allein kann das nicht unterscheiden — siehe [Abschnitt 7](#7-der-klassifizierungs-reparatur-flow).

### DPT → HA-Entitätstyp-Referenz

**Digital (DPT1.x):**

| DPT | KNX-Name | Klassifizierung | Resultierende Entität | `device_class` |
| :--- | :--- | :--- | :--- | :--- |
| 1.001 | Schalter | mehrdeutig | `switch` (Standard) / `binary_sensor` (`[RO]`) / `button` (`[TRIG]`) | — |
| 1.002 | Bool | mehrdeutig | wie oben | — |
| 1.003 | Freigabe | mehrdeutig | wie oben | — |
| 1.004 | Flanke | mehrdeutig | wie oben | — |
| 1.006 | Binärwert | mehrdeutig | wie oben | — |
| **1.005** | **Alarm** | **eindeutig — immer automatisch `[RO]`-getaggt, nie ein beschreibbarer Switch** | `binary_sensor` | `problem` |
| **1.018** | **Anwesenheit** | **eindeutig — immer automatisch `[RO]`-getaggt, nie ein beschreibbarer Switch** | `binary_sensor` | `occupancy` |
| **1.019** | **Tür/Fenster** | **eindeutig — immer automatisch `[RO]`-getaggt, nie ein beschreibbarer Switch** | `binary_sensor` | `door` |

> **1.005, 1.018 und 1.019 sind immer schreibgeschützt** — hier gibt es keine Wahl und keine Reparaturmeldung. Die Integration benennt das K-Element beim nächsten Poll automatisch mit `[RO]` um, genau wie oben beschrieben; sie können nie als beschreibbarer `switch` enden, anders als die 5 mehrdeutigen DPT1.x-Subtypen darüber.

**Analog (beschreibbar → `number`, `[RO]`-getaggt → `sensor`):**

| DPT | KNX-Name | Einheit | `device_class` |
| :--- | :--- | :--- | :--- |
| 5.001 | Scaling | % | — |
| 5.003 | Angle | ° | — |
| 5.004 | Percent_U8 | % | — |
| 5.005 | DecimalFactor | — | — |
| 5.006 | Tariff | — | — |
| 5.010 | Value_1_Ucount (Impulszähler) | — | — |
| 6.001 | Percent_V8 | % | — |
| 6.010 | Value_1_Count | — | — |
| 7.001 | Value_2_Ucount | — | — |
| 7.002 | Value_2_Ucount | ms | `duration` |
| 7.003 | Value_2_Ucount | 10ms | — |
| 7.004 | Value_2_Ucount | 100ms | — |
| 7.005 | Value_2_Ucount | s | `duration` |
| 7.006 | Value_2_Ucount | min | `duration` |
| 7.007 | Value_2_Ucount | h | `duration` |
| 8.001 | Value_2_Count | — | — |
| 8.002 | Value_2_Count | ms | `duration` |
| 8.003 | Value_2_Count | 10ms | — |
| 8.004 | Value_2_Count | 100ms | — |
| 8.005 | Value_2_Count | s | `duration` |
| 8.006 | Value_2_Count | min | `duration` |
| 8.007 | Value_2_Count | h | `duration` |
| 8.010 | Percent_V16 | % | — |
| 9.001 | Value_Temp | °C | `temperature` |
| 9.002 | Value_Tempd (Temperaturdifferenz) | K | `temperature_delta` |
| 9.004 | Value_Lux | lx | `illuminance` |
| 9.005 | Value_Wsp (Windgeschwindigkeit) | m/s | `wind_speed` |
| 9.006 | Value_Pres (Druck) | Pa | `pressure` |
| 9.007 | Value_Humidity | % | `humidity` |
| 9.008 | Value_AirQuality | ppm | — |
| 9.020 | Value_Volt | V | `voltage` |
| 9.021 | Value_Curr | mA | `current` |
| 9.024 | Power | kW | `power` |
| 12.001 | Value_4_Ucount | — | — |
| 13.001 | Value_4_Count | — | — |
| 13.010 | ActiveEnergy | Wh | `energy` |
| 13.011 | ApparentEnergy | VAh | — |
| 13.012 | ReactiveEnergy | VARh | — |
| 13.013 | ActiveEnergy_kWh | kWh | `energy` |
| 13.014 | ApparentEnergy_kVAh | kVAh | — |
| 13.015 | ReactiveEnergy_kVARh | kVARh | — |
| 13.100 | LongDeltaTimeSec | s | `duration` |
| 17.001 | SceneNumber | — | — |
| 18.001 | SceneControl (Szenennummer-Hälfte) | — | — |

3.007 und 3.008 (Dimmer/Rollo) sind oben ausgenommen — sie enden nie als reines `number`, siehe [Abschnitt 5](#5-dimmer--jalousien-dpt3x). Eine fehlende `device_class` bedeutet nicht, dass die Entität ungestylt bleibt — sie bekommt trotzdem die korrekte Einheit, Min/Max und Schrittweite, nur keine HA-native Einheiten-Umrechnungs-/Statistik-Kategorie; außerdem immer ein KNX-Icon.

## 7. Der Klassifizierungs-Reparatur-Flow

Für jedes aktuell mehrdeutige, noch nicht klassifizierte K-Element öffnet die Integration ein HA-**Repair-Issue** mit dem Titel *"KNX objects need classification (N)"* (Einstellungen → System → Reparaturen). Beim Öffnen wird jedes Objekt **einzeln nacheinander** abgefragt — jedes Formular zeigt den Objektnamen und wie viele noch folgen, mit drei Auswahlmöglichkeiten:

| Option | Wirkung |
| :--- | :--- |
| 🔇 **Als beschreibbaren Schalter belassen** *(Standard)* | Keine Änderung in Comexio. Die ID wird intern gemerkt, damit nicht erneut gefragt wird — außer die Entscheidung wird manuell rückgängig gemacht (siehe unten). |
| 🔒 **Nur-Lese-Sensor** (`[RO]`) | Benennt das K-Element in Comexio um und hängt `[RO]` an. Die Integration lädt neu, damit die Entität von `switch` zu `sensor`/`binary_sensor` wechselt. |
| 🔘 **Taster** (`[TRIG]`) | Benennt das K-Element in Comexio um und hängt `[TRIG]` an. Wird über denselben automatisch verwalteten Trigger-Funktionsplan verdrahtet wie `[TRIG]`-Merker — ein Druck auf den HA-Button löst einen einmaligen Impuls statt eines dauerhaften Zustandswechsels aus. |

Der Standard ist bewusst der sichere No-Op (**Belassen**), keine Umbenennung — das Formular springt direkt nach dem Absenden zum nächsten Objekt, ein Standard, der bereits eine Umbenennung festschreibt, würde bei zu schnellem Durchklicken riskieren, ein Objekt falsch zu klassifizieren. Eine Umbenennung lässt sich über diesen Flow nicht rückgängig machen, während "Belassen" jederzeit später nachgeholt werden kann.

Das Issue wird bei **jedem Coordinator-Poll** neu ausgewertet (nicht nur einmalig) — ein bereits beantwortetes Objekt taucht nicht erneut auf (umbenannte Objekte erfüllen die Mehrdeutigkeits-Kriterien nicht mehr; "Belassen"-Antworten werden über die interne Ignorierliste ausgeschlossen), aber ein tatsächlich neues mehrdeutiges K-Element (neu in Comexio angelegt, oder ein manuell entferntes Suffix) löst das Issue erneut aus. Es gibt keine eigene Aktion, um diese Prüfung manuell zu erzwingen — ein normaler Neuladen der Integration (*Einstellungen → Geräte & Dienste → Comexio → ⋮ → Neu laden*) stößt einen sofortigen Poll und damit eine sofortige Neu-Prüfung an, statt auf das nächste geplante Intervall zu warten.

## 8. Manuelle Suffixe

Genau wie bei Merkern kann das `[RO]`/`[TRIG]`-Suffix (bzw. das Legacy-`[TP]`) eines KNX-Objekts jederzeit auch manuell in Comexio vergeben werden — die Integration automatisiert nur die beiden Fälle aus [Abschnitt 6](#6-datenpunkttyp-dpt-klassifizierung).

## 9. Bekannte Einschränkungen

- **KNX-Analogwerte oberhalb von ca. 1.000.000 werden gerundet/verfälscht:** Das ist ein Comexio-Firmware-Bug, kein Integrations-Problem — bestätigt bis Firmware 11.1.4, ein Fix ist für eine spätere Version geplant. Die Integration setzt das Web-IO-Min/Max jedes KNX-Objekts auf dessen reale DPT-Range (z. B. 0..4294967295 bei DPT12.001) — ein Objekt, dessen DPT-Range über ca. 1.000.000 hinausgeht, kann diesen Bug auf betroffener Firmware auslösen; Objekte mit schmalerer DPT-Range (die große Mehrheit) sind nicht betroffen.
- **Helligkeit/Position von Dimmer und Jalousie sind Schätzungen, kein Readout:** siehe [Abschnitt 5](#5-dimmer--jalousien-dpt3x) — DPT3.x hat gar keinen Rücklesekanal vom Bus, HAs Vorstellung vom aktuellen Zustand kann daher von der Realität abweichen, wenn der Aktor auch anderweitig bedient wird.

---

*[← Zurück zur README](README.md)*
