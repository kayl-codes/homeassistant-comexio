Comexio Integration for Home Assistant
Diese Integration bindet Comexio IO-Server nahtlos in Home Assistant ein. Sie wurde entwickelt, um eine Brücke zwischen der Comexio-Logikwelt und der modernen HA-Oberfläche zu schlagen, wobei der Fokus auf Geschwindigkeit (Echtzeit-Updates) und Automatisierung der Konfiguration liegt.
✨ Kern-Features

    Echtzeit-Status (Local Push): Nutzt HA-Webhooks und dynamisch generierte LUA-Skripte in Comexio, um Statusänderungen von Ein-/Ausgängen und Merkern ohne Verzögerung an Home Assistant zu senden.
    Automatisches Web-IO Lifecycle Management: Die Integration erkennt fehlende Webhooks in Comexio und bietet an, diese automatisch anzulegen oder zu reparieren.
    Smart Delta Sync: Änderungen an Namen oder Typen in Comexio werden erkannt. Ein intelligenter Abgleich aktualisiert nur das Nötigste, um bestehende Logikpläne in Comexio nicht zu stören.
    Integrierte Reparatur-Dialoge (Repairs): Bei Unstimmigkeiten zwischen HA und Comexio erstellt die Integration "Reparatur-Vorschläge" im HA-Dashboard inklusive geschätzter Dauer der Durchführung.
    Sichere Authentifizierung: Unterstützt das moderne RSA-Login-Verfahren (v11) für administrative Aufgaben und Basic Auth für die API.

🛠 Unterstützte Entitäten
Plattform	Comexio Quelle	Features
Sensor	Analoge IOs (QI, TL, AI)	Automatische Erkennung von Temperatur, Leistung (W) und Strom (A).
Binary Sensor	Digitale Eingänge	Auto-Erkennung von Bewegungsmeldern, Fenster- und Tür-Kontakten.
Switch	Digitale Ausgänge (Q) & Merker	Schaltet Relais (als Outlet klassifiziert) und digitale Merker.
Number	Analoge Merker	Setzt Sollwerte (Setpoints) mit Bereichsprüfung (z.B. Temp-Soll).
Button	System-Funktionen	Manueller Smart-Sync Abgleich direkt aus dem HA-Gerät.
🚀 Installation & Einrichtung

    Kopiere den Ordner comexio in dein custom_components Verzeichnis.
    Starte Home Assistant neu.
    Gehe zu Einstellungen > Geräte & Dienste > Integration hinzufügen und suche nach "Comexio".
    Gib die IP deiner Anlage und die Zugangsdaten für die Admin-Oberfläche sowie (optional) einen API-Benutzer an.

    [!TIP]
    Die Integration schlägt dir automatisch vor, eine Web-IO Geräteklasse in Comexio anzulegen. Bestätige dies einfach über den Reparatur-Dialog (Repairs), um die volle Webhook-Funktionalität zu aktivieren.

⚙️ Technische Details (für Experten)

    Audit-Logik: Der Coordinator vergleicht bei jedem Start den MD5-Hash der Konfiguration und führt einen Deep-Scan der Web-IO Befehle durch.
    LUA-Injection: Die Integration generiert LUA-Code, der sicherstellt, dass Comexio-Daten im korrekten JSON-Format an HA gesendet werden, inklusive Fehlerbehandlung für analoge Komma-Werte.
    Serielle Updates: Da der Webserver der Comexio-Anlage Schreibvorgänge seriell verarbeitet, nutzt die Integration eine Queue-Logik mit Pausen, um Instabilitäten während des Syncs zu vermeiden.

------------------------------------------------------------------------------------------------------------------------

1️⃣ Erstmalige Einrichtung (Initial Setup)
Wenn die Integration in Home Assistant hinzugefügt wird, passiert folgendes:

    Authentifizierung: Die Integration meldet sich per RSA-Handshake am Comexio-Server an.
    Konfigurations-Download: Die Datei lädt die vollständige Systemkonfiguration (inkl. aller Erweiterungen, Merker und Web-IOs) herunter.
    Erstellung der Entitäten:
        Merker: Werden als number (analog) oder switch/binary_sensor (digital) angelegt.
        Ein- und Ausgänge (IOs): Die Integration nutzt die von Comexio gemeldeten Typen (InOutputTypeId), um die Entitäten exakt abzubilden.
        Spezialfall QI-Eingänge: Werden als analoge Sensoren für Strom oder Leistung erkannt.

2️⃣ Der laufende Betrieb (Periodischer Audit)
Nach der Einrichtung läuft im Hintergrund der Data Update Coordinator und führt periodisch (z.B. alle 15 Minuten) einen Konsistnez-Check durch:

    Live-Status abfragen: Alle aktuellen Sensor- und Schaltwerte werden in Echtzeit abgefragt und in HA aktualisiert.
    Smart Audit (Konsistenzprüfung):
        HA vergleicht den IST-Zustand seiner Entitäten mit den in Comexio hinterlegten HTTP-Befehlen (Webhooks) im Web-IO Menü.
        String-Logik: Für maximale Lesbarkeit und Stabilität nutzt HA intern die Typ-Bezeichnungen "digital" und "analog".

3️⃣ Erkennung von Abweichungen (Mismatches)
Solltest du in Comexio etwas ändern, erkennt der Audit dies im nächsten Durchlauf. Es gibt vier Szenarien:

    Missing: Ein Merker/IO existiert in Comexio, hat aber noch keinen Gegenpart im Web-IO (kein Webhook an HA).
    Orphan: Ein HTTP-Befehl im Web-IO von Comexio verweist auf eine Entität, die es in HA gar nicht (mehr) gibt.
    Rename: Der Name eines Merkers wurde in Comexio geändert.
    Type Mismatch: Ein Merker ist in Comexio analog, der dazugehörige Webhook meldet aber fälschlicherweise den Typ "digital".

💡 Das Verhalten: Wird eine Abweichung festgestellt, wird keine automatische Änderung durchgeführt! Home Assistant erstellt unter "Reparaturen" ein neues Issue und warnt dich im Protokoll.
4️⃣ Manuelle Reparatur (Delta Sync)
Wenn du das Reparatur-Issue in Home Assistant öffnest, kannst du die Abweichungen manuell beheben lassen.

    Aktion wählen: Du wählst im Dropdown-Menü z.B. Update Types oder Full Sync.
    Gezielte Korrektur (Delta Sync):
        Statt die gesamte Konfiguration neu hochzuladen (was bei deinen ca. 390 Webhooks zu Timeouts führt), nutzt die API Einzelschreibbefehle (save_single_command).
        HA sendet per POST-Befehl präzise Befehle an Comexio, um fehlerhafte Webhooks zu überschreiben oder fehlende hinzuzufügen.
    Führende Hand: Auf diese Weise zieht Home Assistant die Web-IO Befehle in Comexio glatt, damit sie exakt zu der dortigen Hardware- und Merker-Konfiguration passen.
