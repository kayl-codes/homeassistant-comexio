# Comexio — Configuration Guide

🌍 *[🇩🇪 Auf Deutsch lesen (Read this in German)](#-deutsch)*

---

## Table of Contents

1. [Initial Setup](#1-initial-setup)
2. [Integration Overview](#2-integration-overview)
3. [Settings](#3-settings)
4. [Reconfigure Connection](#4-reconfigure-connection)
5. [WebIO Sync](#5-webio-sync)
6. [Initial Repair](#6-initial-repair)

---

## 1. Initial Setup

Navigate to **Settings → Devices & Services → Add Integration** and search for **Comexio**.

<img src="docs/assets/ui/0_setup_dialog-1_en.png" width="320">

| Field | Description |
|-------|-------------|
| **① Instance Name** | Unique prefix for all entity IDs (e.g. `iosrv1`). Cannot be changed after setup. |
| **② Host / IP** | IP address or hostname of your Comexio IO-Server. |
| **③ Username** | Admin UI username (default: `admin`). |
| **④ Password** | Admin UI password. |
| **⑤ API Username** | Basic Auth username for fast value writes (optional). |
| **⑥ API Password** | Basic Auth password (optional). |
| **⑦ Create marker entities** | Import Comexio Markers as HA entities. |
| **⑧ Create IO entities** | Import physical IOs as HA entities. |
| **⑨ Device class name** | Name of the Web-IO device class created in Comexio (default: `HomeAssistant`). |
| **⑩ Polling interval** | Background polling interval in minutes. |

After a successful login test, HA confirms the new device:

<img src="docs/assets/ui/0_setup_dialog-2_en.png" width="320">

---

## 2. Integration Overview

After setup the integration appears under **Settings → Devices & Services → Comexio**.

<img src="docs/assets/ui/1_integration_device-1_en.png" width="560">

The entry shows the instance name, IP address, device count, and entity count. The ⚙️ icon opens [Settings](#3-settings); the ⋮ menu contains **Reconfigure** and **Reload**.

---

## 3. Settings

Open via the **⚙️ icon** on the integration entry. Contains non-credential options only — connection details are managed separately via [Reconfigure](#4-reconfigure-connection).

<img src="docs/assets/ui/2_integration_conf-1_en.png" width="320">

| Field | Description |
|-------|-------------|
| **Naming schema — Markers** | Template for entity names. Variables: `{MarkerId}`, `{MarkerTitle}` |
| **Naming schema — IOs** | Template for entity names. Variables: `{ExtName}`, `{IoId}`, `{IoTitle}`. Default `{IoId} {IoTitle}` — the extension already appears via the device name. An IO without a description gets the title `#nn`. Entity IDs never depend on the schema. |
| **Create marker entities** | Enable/disable Marker import. |
| **Create IO entities** | Enable/disable IO import. |
| **Polling interval** | Dropdown (1 – 1440 min). Prefer longer intervals — live values arrive via webhook. |
| **Webhook address** | Where Comexio pushes live values. *Automatic*: HA's local address. *External HA URL*: host and port of the external URL from HA's network settings (`https` without a port means 443). *Custom address*: a URL entered in a second step (`http(s)://host[:port]`, no path). With an external or custom address the push leaves your network and goes over the internet. A plain `http://` address is therefore only accepted inside the local network (private IPs, single-label names, local domains such as `fritz.box` or `.local`); elsewhere use `https://`. If the external URL is missing or unusable, the integration falls back to the automatic address, logs a warning, and the Repair issue says that its fix writes the automatic address. After a change the Repair issue offers **Update HA Server Address (IP:Port)**, which rewrites only the address of the Web-IO devices. The Web-IO commands stay unchanged on HTTP, also for an `https://` address: Comexio picks HTTP or HTTPS itself. |
| **Show sync notifications** | Display pop-up progress messages during WebIO sync. |
| **Ignore Web-IO warnings** | Suppress the Repair issue when the Web-IO class is out of sync. |
| **Show offline extension entities** | Show entities of offline modules as *Unavailable* instead of hiding them. |
| **Cover detection keywords** | Comma-separated keywords; an analog marker whose name contains one becomes a 0–100 % number/sensor (default `rollo, jalousie, blind, markise, awning`). A marker name with `temp`, `setpoint`, `°C` or the word `Grad` becomes a °C temperature (−273 to 10,000); a bare `Soll` does not. |

---

## 4. Reconfigure Connection

Open via **⋮ → Reconfigure** on the integration entry. Allows updating host, username, and passwords without re-adding the integration.

<img src="docs/assets/ui/2_integration_reconf-1_en.png" width="320">

> **Tip:** Leave password fields blank to keep the existing password. The integration runs a connection test before saving and reloads automatically on success.

> **Rejected credentials:** If Comexio refuses the stored admin credentials (when the integration starts or on a poll), HA starts a *Re-authenticate Comexio* flow instead — enter the current username and password there. Until the credentials are updated, the configuration poll stops and the integration does not try the refused credentials again — only a Home Assistant restart or a reload of the entry tries them once more, and starts the reauth flow again if Comexio still refuses them. While the integration is first being added, refused credentials show an error in the setup form instead.

---

## 5. WebIO Sync

The integration creates and manages a *Web-IO* device class in Comexio automatically. The sync controls are exposed as diagnostic entities on the hub device.

**Idle state — ready to sync:**

<img src="docs/assets/ui/2_integration_diagnostic-1_en.png" width="300">

**Active state — sync in progress:**

<img src="docs/assets/ui/2_integration_diagnostic-2_en.png" width="300">

| Entity | Description |
|--------|-------------|
| **WebIO Sync Start** | Triggers a delta or full-recreate sync. Unavailable while a sync is running. |
| **WebIO Sync Cancel** | Cancels an in-progress sync. Only active during a running sync. The step already running finishes; no further Web-IO command, Web-IO class or function plan wiring stage starts. What already ran stays in place. The result opens with *Sync cancelled by user* and names what was left out: *Not synced* lists the Web-IO classes, *Not started* the wiring stages. The status sensor then shows `partial`. |
| **WebIO Sync Status** | Sensor reporting `idle`, `syncing`, `partial` (shown as *Incomplete*: the run finished or was cancelled, but not everything was written) or `error` (the run aborted). |

**Entity state details (developer tools):**

<img src="docs/assets/ui/2_integration_webiosync-1_en.png" width="640">

<img src="docs/assets/ui/2_integration_webiosync-2_en.png" width="640">

---

## 6. Initial Repair

When the integration starts for the first time — or the Web-IO class is missing in Comexio — a **Repair issue** appears automatically:

<img src="docs/assets/ui/3_initialrepair_dialog-1_en.png" width="360">

Confirming the repair creates the Web-IO device class and all webhook commands in Comexio. A notification tracks progress:

<img src="docs/assets/ui/3_initialrepair_notification-1_en.png" width="300">

A final notification confirms success with the elapsed time:

<img src="docs/assets/ui/3_initialrepair_notification-2_en.png" width="300">

---

*[← Back to README](README.md)*

---

# 🇩🇪 Deutsch

🌍 *[🇬🇧 Read this in English](#comexio--configuration-guide)*

---

## Inhaltsverzeichnis

1. [Ersteinrichtung](#1-ersteinrichtung)
2. [Integrationsübersicht](#2-integrationsübersicht)
3. [Einstellungen](#3-einstellungen)
4. [Verbindung neu konfigurieren](#4-verbindung-neu-konfigurieren)
5. [WebIO Sync](#5-webio-sync-1)
6. [Ersteinrichtung (Reparatur)](#6-ersteinrichtung-reparatur)

---

## 1. Ersteinrichtung

Navigiere zu **Einstellungen → Geräte & Dienste → Integration hinzufügen** und suche nach **Comexio**.

<img src="docs/assets/ui/0_setup_dialog-1_de.png" width="320">

| Feld | Beschreibung |
|------|--------------|
| **① Instanz Name** | Eindeutiger Präfix für alle Entity-IDs (z. B. `iosrv1`). Kann nach dem Setup nicht geändert werden. |
| **② Host / IP-Adresse** | IP-Adresse oder Hostname des Comexio IO-Servers. |
| **③ Benutzername** | Benutzername für die Admin-Oberfläche (Standard: `admin`). |
| **④ Passwort** | Passwort für die Admin-Oberfläche. |
| **⑤ API Benutzername** | Basic-Auth-Benutzername für schnelle API-Schreibzugriffe (optional). |
| **⑥ API Passwort** | Basic-Auth-Passwort (optional). |
| **⑦ Merker als Entitäten anlegen** | Comexio-Merker als HA-Entitäten importieren. |
| **⑧ Ein- und Ausgänge anlegen** | Physische IOs als HA-Entitäten importieren. |
| **⑨ Name der Geräteklasse** | Name der Web-IO-Geräteklasse in Comexio (Standard: `HomeAssistant`). |
| **⑩ Abfrageintervall** | Hintergrund-Polling-Intervall in Minuten. |

Nach erfolgreichem Login-Test bestätigt HA die Anlage des neuen Geräts:

<img src="docs/assets/ui/0_setup_dialog-2_de.png" width="320">

---

## 2. Integrationsübersicht

Nach dem Setup erscheint die Integration unter **Einstellungen → Geräte & Dienste → Comexio**.

<img src="docs/assets/ui/1_integration_device-1_de.png" width="560">

Der Eintrag zeigt Instanzname, IP-Adresse, Geräteanzahl und Entitätenanzahl. Das ⚙️-Icon öffnet die [Einstellungen](#3-einstellungen); das ⋮-Menü enthält **Neu konfigurieren** und **Neu laden**.

---

## 3. Einstellungen

Öffnen über das **⚙️-Icon** am Integrationseintrag. Enthält ausschließlich nicht-sicherheitskritische Optionen — Verbindungsdaten werden separat über [Neu konfigurieren](#4-verbindung-neu-konfigurieren) verwaltet.

<img src="docs/assets/ui/2_integration_conf-1_de.png" width="320">

| Feld | Beschreibung |
|------|--------------|
| **Namensschema — Merker** | Vorlage für Entity-Namen. Variablen: `{MarkerId}`, `{MarkerTitle}` |
| **Namensschema — IOs** | Vorlage für Entity-Namen. Variablen: `{ExtName}`, `{IoId}`, `{IoTitle}`. Standard `{IoId} {IoTitle}` — die Extension steht bereits im Gerätenamen. Ein IO ohne Beschreibung erhält den Titel `#nn`. Entity-IDs hängen nie vom Schema ab. |
| **Merker als Entitäten anlegen** | Merker-Import aktivieren/deaktivieren. |
| **Ein- und Ausgänge anlegen** | IO-Import aktivieren/deaktivieren. |
| **Abfrageintervall** | Dropdown (1 – 1440 min). Längere Intervalle bevorzugen — Live-Werte kommen per Webhook. |
| **Webhook-Adresse** | Wohin Comexio die Live-Werte pusht. *Automatisch*: die lokale Adresse von HA. *Externe HA-URL*: Host und Port der externen URL aus den HA-Netzwerkeinstellungen (`https` ohne Port bedeutet 443). *Eigene Adresse*: eine URL, die in einem zweiten Schritt eingegeben wird (`http(s)://host[:port]`, ohne Pfad). Mit externer oder eigener Adresse verlässt der Push dein Netzwerk und läuft über das Internet. Eine reine `http://`-Adresse wird deshalb nur im lokalen Netz akzeptiert (private IPs, Namen ohne Punkt, lokale Domains wie `fritz.box` oder `.local`); sonst `https://` verwenden. Fehlt die externe URL oder ist sie nicht nutzbar, weicht die Integration auf die automatische Adresse aus, schreibt eine Warnung ins Log, und das Reparatur-Issue sagt, dass seine Korrektur die automatische Adresse schreibt. Nach einer Änderung bietet das Reparatur-Issue **HA Server-Adresse (IP:Port) aktualisieren** an; das schreibt nur die Adresse der Web-IO-Geräte neu. Die Web-IO-Befehle bleiben unverändert auf HTTP, auch bei einer `https://`-Adresse: Comexio wählt HTTP oder HTTPS selbst. |
| **Fortschritts-Popups anzeigen** | Fortschrittsmeldungen während des WebIO-Syncs anzeigen. |
| **Web-IO-Warnung ignorieren** | Reparatur-Issue unterdrücken, wenn die Web-IO-Klasse nicht synchron ist. |
| **Offline-Extensions anzeigen** | Entitäten offline gegangener Module als *Nicht verfügbar* anzeigen statt ausblenden. |
| **Schlüsselwörter Rollladen-Erkennung** | Kommaseparierte Liste; ein analoger Merker, dessen Name eines davon enthält, wird zur 0–100-%-Zahl bzw. zum %-Sensor (Standard `rollo, jalousie, blind, markise, awning`). Ein Merkername mit `temp`, `setpoint`, `°C` oder dem Wort `Grad` wird zur °C-Temperatur (−273 bis 10 000); ein bloßes `Soll` reicht dafür nicht. |

---

## 4. Verbindung neu konfigurieren

Öffnen über **⋮ → Neu konfigurieren** am Integrationseintrag. Erlaubt das Ändern von Host, Benutzername und Passwörtern ohne Neueinrichtung der Integration.

<img src="docs/assets/ui/2_integration_reconf-1_de.png" width="320">

> **Tipp:** Passwortfelder leer lassen bedeutet "bestehendes Passwort beibehalten". Die Integration führt vor dem Speichern einen Verbindungstest durch und lädt sich bei Erfolg automatisch neu.

> **Abgelehnte Zugangsdaten:** Lehnt Comexio die gespeicherten Admin-Zugangsdaten ab (beim Start der Integration oder bei einer Abfrage), startet HA stattdessen den Ablauf *Comexio erneut anmelden* — dort den aktuellen Benutzernamen und das Passwort eingeben. Bis die Zugangsdaten aktualisiert sind, ruht die Konfigurationsabfrage, und die Integration versucht die abgelehnten Zugangsdaten nicht erneut — nur ein Neustart von Home Assistant oder ein Neuladen der Integration versucht sie noch einmal und startet erneut den Ablauf *Comexio erneut anmelden*, wenn Comexio sie weiter ablehnt. Beim erstmaligen Hinzufügen der Integration erscheint bei abgelehnten Zugangsdaten stattdessen eine Fehlermeldung im Einrichtungsformular.

---

## 5. WebIO Sync

Die Integration erstellt und verwaltet eine *Web-IO*-Geräteklasse in Comexio vollautomatisch. Die Sync-Steuerung ist über Diagnose-Entitäten am Hub-Gerät zugänglich.

**Ruhezustand — bereit:**

<img src="docs/assets/ui/2_integration_diagnostic-1_de.png" width="300">

**Aktiver Sync:**

<img src="docs/assets/ui/2_integration_diagnostic-2_de.png" width="300">

| Entität | Beschreibung |
|---------|--------------|
| **WebIO Sync Start** | Startet einen Delta- oder Full-Recreate-Sync. Während des Syncs nicht verfügbar. |
| **WebIO Sync Abbruch** | Bricht einen laufenden Sync ab. Nur während eines aktiven Syncs verfügbar. Der gerade laufende Schritt läuft noch zu Ende; danach startet kein weiterer Web-IO-Befehl, keine weitere Web-IO-Klasse und keine weitere Verdrahtungsstufe der Logikpläne. Was schon gelaufen ist, bleibt bestehen. Das Ergebnis beginnt mit *Sync cancelled by user* und nennt, was ausgelassen wurde: *Not synced* die Web-IO-Klassen, *Not started* die Verdrahtungsstufen. Der Status-Sensor steht danach auf `partial`. |
| **WebIO Sync Status** | Sensor mit den Zuständen `idle`, `syncing`, `partial` (angezeigt als *Unvollständig*: der Lauf ist fertig oder wurde abgebrochen, aber nicht alles wurde geschrieben) oder `error` (der Lauf brach ab). |

**Entitätszustände (Entwickler-Werkzeuge):**

<img src="docs/assets/ui/2_integration_webiosync-1_de.png" width="640">

<img src="docs/assets/ui/2_integration_webiosync-2_de.png" width="640">

---

## 6. Ersteinrichtung (Reparatur)

Wenn die Integration zum ersten Mal startet — oder die Web-IO-Klasse in Comexio fehlt — wird automatisch ein **Reparatur-Issue** erstellt:

<img src="docs/assets/ui/3_initialrepair_dialog-1_de.png" width="360">

Nach Bestätigung wird die Web-IO-Geräteklasse samt aller Webhook-Befehle in Comexio angelegt. Eine Benachrichtigung zeigt den Fortschritt:

<img src="docs/assets/ui/3_initialrepair_notification-1_de.png" width="300">

Eine abschließende Meldung bestätigt den Erfolg mit der benötigten Zeit:

<img src="docs/assets/ui/3_initialrepair_notification-2_de.png" width="300">

---

*[← Zurück zur README](README.md)*
