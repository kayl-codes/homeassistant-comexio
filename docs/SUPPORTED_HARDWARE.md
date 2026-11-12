# Comexio — Supported Hardware

🌍 *[🇩🇪 Auf Deutsch lesen (Read this in German)](#-deutsch)*

Which Comexio extensions, Quickbus devices and 1-Wire sensors work with this integration, and how far each one has been tested. For the entity types in general, see the [README](../README.md#-supported-entities).

**Tested with:** integration v0.40.0, Comexio IO-Server Gen3, Comexio firmware 11.1.5, on one installation. All tests during development run on a Comexio IO-Server Gen3. Modules are listed by their type as Comexio shows it on its IO page, not by the name you give them.

How the integration chooses the entity:

- An identifier starting with `Q` is an output, one starting with `I` is an input. Any other identifier goes by its IO type in Comexio.
- The IO type also decides between on/off and a value, and sets the unit. The same `AI2` can therefore be a `binary_sensor` on one installation and a voltage `sensor` on another.
- Inactive IOs get no entity.

---

## ✅ Tested and working

These run on a live installation with the hardware connected.

### Extension modules

| Module | IOs | In Home Assistant | Direction |
| :--- | :--- | :--- | :--- |
| **IO-Server 3G**, **IO-Extension 2G** | `Q1`–`Q9` | `switch` (outlet) | read / write |
| | `I1`–`I8` | `binary_sensor` | read |
| | `AI1`–`AI8` (digital type) | `binary_sensor`, some with device class `motion` | read |
| | `AI1`–`AI8` (0–10 V type) | `sensor` voltage (V) | read |
| **Universal-Dimmer** | `Q2`–`Q4` (%) | `number` | read / write |
| | `AO1`–`AO4` (0–10 V, 1–10 V types) | `number` in % or V depending on the type, step 0.1, one decimal; `switch` for the *Digital 10V* type | read / write |
| | `I1`–`I8` | `binary_sensor` | read |
| **LED Dimmer** | `Q1`–`Q9` (%) | `number` 0–100 | read / write |
| | `QI1`–`QI9` (%) | `sensor` without device class | read |
| **EnOcean Unit** | `TL1` | `sensor` temperature (°C) | read |
| | `EI1_WIN`, `EI2_WIN` | `sensor` without unit | read |

Each module also has a firmware `update` entity.

### Quickbus devices

Quickbus IOs are always read-only, because the Comexio API refuses writes to them.

| Device | IOs | In Home Assistant |
| :--- | :--- | :--- |
| **Digital Input** | 8 digital | `binary_sensor` |
| **Wallbox** | 20 values | `sensor` current (A), voltage (V), power (W), energy (Wh); `binary_sensor` |
| **Multitouch, Motion, Bus Connector, Digital Input V2** | digital IOs, temperature, `HUM`, `LUX`, other % values | `binary_sensor`; `sensor` temperature (°C), humidity (%, only `HUM`), illuminance (lx, from `LUX`), other % values without device class |

---

## ⚠️ Not fully working or not tested

### Known problems on connected hardware

| Module | IOs | Problem | Status |
| :--- | :--- | :--- | :--- |
| **IO-Server**, **IO-Extension 2G** | `AO1`–`AO4` (0–10 V) | Writing works since aiocomexio 0.8.3: the integration writes them like Comexio's own IO page, because the Comexio API refuses them with `Forbidden`. A value set in Comexio was also reported as not showing up in Home Assistant. | Writing tested live on a Universal-Dimmer; feedback for these modules and the missing value open, [#259](https://github.com/kayl-codes/homeassistant-comexio/issues/259) |

### Tested without hardware

These are offline on the test installation, so **no live value or write has ever been seen**. They only get entities with the option *Include offline extensions*. The check covered the entity type, name and unit. An `ok` therefore means "the entity looks right", not "it works".

| Extension | IOs | IO type | In Home Assistant | Result |
| :--- | :--- | :--- | :--- | :--- |
| **Smart Meter**, **Smart Meter 2G** | 80 IOs: `E1`–`E3`, `DS1`, `DQ1`, ... | Wh/kWh, W, VA, var | `sensor` energy, power, apparent power, reactive power | ok, awaiting feedback from a real meter ([#249](https://github.com/kayl-codes/homeassistant-comexio/issues/249)) |
| **IP-Roomcontroller** | `ProximitySensor`, `InternalError`, `Panic` | digital | `binary_sensor` | ok |
| | `LuxSensor` | lux | `sensor` illuminance (lx) | ok in unit tests; `lux` to `lx` seen live only on Quickbus `LUX` |
| **Universal-Aktor** | `Q1`–`Q16` | digital | `switch` | ok |
| | `QI1`–`QI4`, `TL1`, `UL1` | A, °C, V | `sensor` | ok |
| **0–10 V Output Module** (CME1210, `Uax-AO` in Comexio) | `AO1`–`AO4` | 0–10 V | `number`, step 0.1 | entity ok; writes the same way as the Universal-Dimmer's AOs (#259) |
| **Uax-PT1000** | `T1`, `T2` | −50 to 120 °C | `sensor` temperature | ok |
| **Multibridge** | `Q1` | % | `number` 0–100 | ok |
| | `I1`, `OVC1`, `OTMP` | digital | `binary_sensor` | ok |
| | `QI1` | W | `sensor` power | ok |
| **Doorstation** | `PIR1`, `DI1`–`DI3` | digital | `binary_sensor` | ok |
| | `NameLED1` | digital | `switch` | ok |
| | `Screenshot1` | 131 | `binary_sensor` | read-only, although Comexio writes it |
| **Touchpanel**, **Touchpanel-2G** | `DisplayOn1` | digital | `switch` | ok |
| | `Bright1`, `Volume1` | 28 (%) | `sensor` | read-only |
| | `Signal1`, `Mute1` | 6 | `binary_sensor` | read-only |
| **Touchpanel** | `PIR1` | digital | `binary_sensor` | ok |
| **Touchpanel-2G** | `LightMode1` | 28 (%) | `sensor` | read-only |
| | `R`, `G`, `B` | 111 | `sensor` | read-only |
| | `S1`–`S3`, `Alarm1`, `LightMute1` | 6 | `binary_sensor` | read-only |
| **Dali Bridge** | `DG1`–`DG16` | 119 (0–100) | `sensor` without unit | read-only |
| **Multiroom-Server** | `Next1`, `Prev1`, `Play1`, `Pause1` | digital | `switch` (outlet) | ok, a button would fit better |
| | `NOSLP1`, `Toggle1`, `Alarm1`, `Bell1`, `Wake1`, `Custom1` | 3 | `binary_sensor` | read-only |
| | `CurVol1` | 56 (%) | `sensor` | read-only |

"Read-only, although Comexio writes it" applies to every IO marked read-only in this table: Comexio flags them as outputs, but their identifier starts with neither `Q` nor `I` and their IO type counts as an input.

`TL1` and `UL1` on **Dali Bridge** and **Modbus Bridge**, and `BVol1` on **Doorstation**, are inactive in Comexio, so they got no entity and remain unchecked.

### 1-Wire sensors

The sensors of Comexio's 1-Wire bus are read-only. Their values arrive only by push, see [1-Wire sensors](../README.md#-1-wire-sensors) in the README. The test installation has only dummy sensors, so on it no real sensor value has been seen. A user confirmed real temperature sensors (`OT`) working after the release of v0.40.0. What was checked here: the device `<instance> 1-Wire`, the entities with their names and ids, and the *unavailable* state plus the diagnostic `1-Wire problem` sensor for a sensor that cannot be read.

| Prefix | Meaning | In Home Assistant | Result |
| :--- | :--- | :--- | :--- |
| `OT` | temperature | `sensor` temperature (°C) | works, confirmed by a user with real sensors |
| `OH` | humidity | `sensor` humidity (%) | entity ok, value awaiting a real sensor |
| `OP`, `OILL`, `OVAD`, `OVDD`, `OIB`, other | air pressure, brightness, voltages, ... | `sensor` showing the bare number, without unit | entity ok, value awaiting a real sensor |
| any, digital type | digital sensor | `binary_sensor` | not tested |

### Not tested at all

| Hardware | Note |
| :--- | :--- |
| **Any other module** | Please report what you see on [GitHub Issues](https://github.com/kayl-codes/homeassistant-comexio/issues). |

---

# 🇩🇪 Deutsch

Welche Comexio-Erweiterungen, Quickbus-Geräte und 1-Wire-Sensoren mit dieser Integration funktionieren und wie weit sie jeweils getestet sind. Die Entitätstypen allgemein stehen in der [README](../README.md#-unterstützte-entitäten).

**Getestet mit:** Integration v0.40.0, Comexio IO-Server Gen3, Comexio-Firmware 11.1.5, auf einer Installation. Alle Tests während der Entwicklung laufen auf einem Comexio IO-Server Gen3. Die Module stehen hier mit ihrem Typ, wie ihn Comexio auf der IO-Seite anzeigt, nicht mit dem selbst vergebenen Namen.

So wählt die Integration die Entität:

- Ein Bezeichner mit `Q` am Anfang ist ein Ausgang, einer mit `I` ein Eingang. Alle anderen richten sich nach ihrem IO-Typ in Comexio.
- Der IO-Typ entscheidet außerdem zwischen Ein/Aus und Wert und legt die Einheit fest. Derselbe `AI2` kann also auf einer Installation ein `binary_sensor` sein und auf einer anderen ein Spannungs-`sensor`.
- Inaktive IOs bekommen keine Entität.

---

## ✅ Getestet und funktionsfähig

Diese laufen auf einer Live-Installation mit angeschlossener Hardware.

### Erweiterungsmodule

| Modul | IOs | In Home Assistant | Richtung |
| :--- | :--- | :--- | :--- |
| **IO-Server 3G**, **IO-Extension 2G** | `Q1`–`Q9` | `switch` (outlet) | lesen / schreiben |
| | `I1`–`I8` | `binary_sensor` | lesen |
| | `AI1`–`AI8` (digitaler Typ) | `binary_sensor`, teils mit Geräteklasse `motion` | lesen |
| | `AI1`–`AI8` (Typ 0–10 V) | `sensor` Spannung (V) | lesen |
| **Universal-Dimmer** | `Q2`–`Q4` (%) | `number` | lesen / schreiben |
| | `AO1`–`AO4` (Typen 0–10 V, 1–10 V) | `number` in % oder V je nach Typ, Schritt 0,1, eine Nachkommastelle; `switch` beim Typ *Digital 10V* | lesen / schreiben |
| | `I1`–`I8` | `binary_sensor` | lesen |
| **LED Dimmer** | `Q1`–`Q9` (%) | `number` 0–100 | lesen / schreiben |
| | `QI1`–`QI9` (%) | `sensor` ohne Geräteklasse | lesen |
| **EnOcean Unit** | `TL1` | `sensor` Temperatur (°C) | lesen |
| | `EI1_WIN`, `EI2_WIN` | `sensor` ohne Einheit | lesen |

Jedes Modul hat zusätzlich eine `update`-Entität für die Firmware.

### Quickbus-Geräte

Quickbus-IOs sind immer nur lesbar, weil die Comexio-API Schreibzugriffe darauf ablehnt.

| Gerät | IOs | In Home Assistant |
| :--- | :--- | :--- |
| **Digital Input** | 8 digitale | `binary_sensor` |
| **Wallbox** | 20 Werte | `sensor` Strom (A), Spannung (V), Leistung (W), Energie (Wh); `binary_sensor` |
| **Multitouch, Motion, Bus Connector, Digital Input V2** | digitale IOs, Temperatur, `HUM`, `LUX`, weitere %-Werte | `binary_sensor`; `sensor` Temperatur (°C), Luftfeuchte (%, nur `HUM`), Beleuchtungsstärke (lx, aus `LUX`), weitere %-Werte ohne Geräteklasse |

---

## ⚠️ Nicht voll funktionsfähig oder nicht getestet

### Bekannte Probleme bei angeschlossener Hardware

| Modul | IOs | Problem | Stand |
| :--- | :--- | :--- | :--- |
| **IO-Server**, **IO-Extension 2G** | `AO1`–`AO4` (0–10 V) | Schreiben geht seit aiocomexio 0.8.3: Die Integration schreibt sie wie Comexios eigene IO-Seite, weil die Comexio-API sie mit `Forbidden` ablehnt. Außerdem wurde gemeldet, dass ein in Comexio gesetzter Wert nicht in Home Assistant ankam. | Schreiben live an einem Universal-Dimmer getestet; Rückmeldung für diese Module und der fehlende Wert offen, [#259](https://github.com/kayl-codes/homeassistant-comexio/issues/259) |

### Ohne Hardware getestet

Diese sind auf der Testinstallation offline, deshalb **wurde noch nie ein Live-Wert oder Schreibzugriff beobachtet**. Entitäten gibt es nur mit der Option *Offline-Erweiterungen einbeziehen*. Geprüft sind Entitätstyp, Name und Einheit. „ok“ heißt also „die Entität sieht richtig aus“, nicht „es funktioniert“.

| Erweiterung | IOs | IO-Typ | In Home Assistant | Ergebnis |
| :--- | :--- | :--- | :--- | :--- |
| **Smart Meter**, **Smart Meter 2G** | 80 IOs: `E1`–`E3`, `DS1`, `DQ1`, ... | Wh/kWh, W, VA, var | `sensor` Energie, Leistung, Scheinleistung, Blindleistung | ok, Rückmeldung mit echtem Zähler offen ([#249](https://github.com/kayl-codes/homeassistant-comexio/issues/249)) |
| **IP-Raumcontroller** | `ProximitySensor`, `InternalError`, `Panic` | digital | `binary_sensor` | ok |
| | `LuxSensor` | lux | `sensor` Beleuchtungsstärke (lx) | ok in Unit-Tests; `lux` zu `lx` live nur an Quickbus-`LUX` gesehen |
| **Universal-Aktor** | `Q1`–`Q16` | digital | `switch` | ok |
| | `QI1`–`QI4`, `TL1`, `UL1` | A, °C, V | `sensor` | ok |
| **0–10 V Output Modul** (CME1210, `Uax-AO` in Comexio) | `AO1`–`AO4` | 0–10 V | `number`, Schritt 0,1 | Entität ok; schreibt wie die AOs des Universal-Dimmers (#259) |
| **Uax-PT1000** | `T1`, `T2` | −50 bis 120 °C | `sensor` Temperatur | ok |
| **Multibridge** | `Q1` | % | `number` 0–100 | ok |
| | `I1`, `OVC1`, `OTMP` | digital | `binary_sensor` | ok |
| | `QI1` | W | `sensor` Leistung | ok |
| **Doorstation** | `PIR1`, `DI1`–`DI3` | digital | `binary_sensor` | ok |
| | `NameLED1` | digital | `switch` | ok |
| | `Screenshot1` | 131 | `binary_sensor` | nur lesbar, obwohl Comexio ihn beschreibt |
| **Touchpanel**, **Touchpanel-2G** | `DisplayOn1` | digital | `switch` | ok |
| | `Bright1`, `Volume1` | 28 (%) | `sensor` | nur lesbar |
| | `Signal1`, `Mute1` | 6 | `binary_sensor` | nur lesbar |
| **Touchpanel** | `PIR1` | digital | `binary_sensor` | ok |
| **Touchpanel-2G** | `LightMode1` | 28 (%) | `sensor` | nur lesbar |
| | `R`, `G`, `B` | 111 | `sensor` | nur lesbar |
| | `S1`–`S3`, `Alarm1`, `LightMute1` | 6 | `binary_sensor` | nur lesbar |
| **Dali Bridge** | `DG1`–`DG16` | 119 (0–100) | `sensor` ohne Einheit | nur lesbar |
| **Multiroom-Server** | `Next1`, `Prev1`, `Play1`, `Pause1` | digital | `switch` (outlet) | ok, ein Button würde besser passen |
| | `NOSLP1`, `Toggle1`, `Alarm1`, `Bell1`, `Wake1`, `Custom1` | 3 | `binary_sensor` | nur lesbar |
| | `CurVol1` | 56 (%) | `sensor` | nur lesbar |

„Nur lesbar“ heißt in dieser Tabelle immer: Comexio führt den IO als Ausgang, sein Bezeichner beginnt aber weder mit `Q` noch mit `I`, und sein IO-Typ zählt als Eingang.

`TL1` und `UL1` auf **Dali Bridge** und **Modbus Bridge** sowie `BVol1` auf **Doorstation** sind in Comexio inaktiv, haben also keine Entität bekommen und bleiben ungeprüft.

### 1-Wire-Sensoren

Die Sensoren des 1-Wire-Busses von Comexio sind nur lesbar. Ihre Werte kommen ausschließlich per Push, siehe [1-Wire-Sensoren](../README.md#-1-wire-sensoren) in der README. Die Testinstallation hat nur Dummy-Sensoren, auf ihr wurde also kein Wert eines echten Sensors beobachtet. Ein Nutzer hat nach dem Release von v0.40.0 bestätigt, dass echte Temperatursensoren (`OT`) funktionieren. Hier geprüft sind: das Gerät `<Instanz> 1-Wire`, die Entitäten mit Namen und IDs sowie der Zustand *nicht verfügbar* samt Diagnose-Sensor `1-Wire-Problem` bei einem Sensor, der nicht lesbar ist.

| Präfix | Bedeutung | In Home Assistant | Ergebnis |
| :--- | :--- | :--- | :--- |
| `OT` | Temperatur | `sensor` Temperatur (°C) | funktioniert, von einem Nutzer mit echten Sensoren bestätigt |
| `OH` | Feuchte | `sensor` Luftfeuchte (%) | Entität ok, Wert wartet auf einen echten Sensor |
| `OP`, `OILL`, `OVAD`, `OVDD`, `OIB`, andere | Luftdruck, Helligkeit, Spannungen, ... | `sensor` mit der nackten Zahl, ohne Einheit | Entität ok, Wert wartet auf einen echten Sensor |
| beliebig, digitaler Typ | digitaler Sensor | `binary_sensor` | nicht getestet |

### Gar nicht getestet

| Hardware | Hinweis |
| :--- | :--- |
| **Alle anderen Module** | Bitte melde deine Beobachtungen über [GitHub Issues](https://github.com/kayl-codes/homeassistant-comexio/issues). |
