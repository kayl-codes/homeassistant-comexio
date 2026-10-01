# Comexio — Function Plan Backups

How the integration backs up Comexio function plans, how to look at and restore a backup,
and what happens to the backups of a plan that was deleted in Comexio.

🇩🇪 [Deutsche Fassung weiter unten](#-deutsch)

## Table of Contents

1. [What gets backed up](#1-what-gets-backed-up)
2. [Looking at a backup](#2-looking-at-a-backup)
3. [Restoring a backup](#3-restoring-a-backup)
4. [Backups of deleted plans](#4-backups-of-deleted-plans)
5. [Related actions and entities](#5-related-actions-and-entities)

---

## 1. What gets backed up

Every function plan in Comexio is backed up automatically, not only the plans the
integration manages. There is nothing to configure.

| Type | When | Kept per plan |
|------|------|---------------|
| **auto** | On every poll, but only if the plan's content changed since the last auto backup (content hash). | 3 newest |
| **change** | Right before the integration itself changes a plan (connect, sort, restore, …), tagged with that operation. | 10 newest |

- A plan is identified by its **ID and name** together. Comexio reuses the ID of a deleted
  plan for the next new one, so an ID alone would mix up two different plans.
- A new snapshot pushes the oldest one of its type out. Nothing else ever deletes the
  backups of a plan that still exists, no matter how old they are.
- Moving elements without changing the wiring does not create an auto backup.
- The snapshots are stored in Home Assistant's `.storage` folder, so every Home Assistant
  backup contains them too.

## 2. Looking at a backup

- **Plan card:** pick the plan, then pick a backup in the **Backup** selector.
  The card shows that snapshot instead of the live plan; **Live** switches back. See the
  [Function Plan Preview guide](FUNCTION_PLAN_PREVIEW.md).
- **Backups of deleted plans in the plan card:** pick **Orphaned plans** (last entry of the
  **Plan** selector, shown while such backups exist). The **Backup**
  selector then lists every deleted plan as `<name> (ID <n>) — <count> backups`, followed by
  its indented snapshots; the plan row shows the newest one. The preview is marked
  `[verwaist]` and shows no live values, since the ID may belong to another plan by now.
  Audit and sync keep working on the plan picked before.
- **Any backup, including those of deleted plans:** Developer tools → Actions →
  **Function Plan Visualize**, field **Snapshot**, format `svg` or `text`. Each entry reads
  `<plan name> — fub <id> — <type>[<slot>] — <date>`. This works without a connection to
  Comexio.
- **What changed:** **Function Plan Analyze** and the `diff` option of
  **Function Plan List Backups** compare snapshots.

## 3. Restoring a backup

**Function Plan Restore** (or the **Restore** button of the plan card) writes a snapshot
back to Comexio: structure, element positions and the paper/DPI setting of the canvas.

- Before it writes, it stores the current state as a change backup, and afterwards it
  checks the result against the snapshot's content hash.
- The **Snapshot** field picks the exact target in one step. The fields `fub_id`, `kind`
  and `slot` are an alternative for scripts and are ignored when `snapshot` is set.
- If the original plan was **deleted**, or its ID now belongs to **another plan**, the
  restore creates a **new plan** (`on_conflict: new_id`). With `confirm: true` and
  `on_conflict: force_override` it overwrites the plan that holds the ID now.
- In the plan card's **Orphaned plans** view, **Restore** always creates a new plan.

## 4. Backups of deleted plans

When a plan is deleted directly in Comexio, its backups stay. They are what you need to
bring the plan back, so the integration never deletes them on its own:

1. **Retention:** the option **Orphaned backup retention** (Options → Function Plan,
   default 6 months) sets how long the backups of a deleted plan are kept, counted from
   its newest backup.
2. **Repair:** once that period has passed, a repair **Backups of deleted plan &lt;name&gt;**
   appears, one per plan. It shows the former ID, the plan that holds this ID now (if
   any), the number of backups and the date of the newest one.

   A plan **renamed** in Comexio counts as deleted too: its backups from before the rename
   keep the old name, the new ones are stored under the new name. If the plan now holding
   the ID is your renamed plan, the repair is about its older history.
3. **Your decision:**
   - **Load into the plan preview** switches the plan card to **Orphaned plans** and shows
     the plan's newest backup. It decides nothing — the repair stays open.
   - **Keep** keeps the backups for good and never asks again for this plan.
   - **Delete** removes all backups of this plan.
   - **Ignore** (Home Assistant's own button) hides the repair and deletes nothing.
4. **Look first:** besides the plan card, **Function Plan Visualize** shows any backup, as
   described in [section 2](#2-looking-at-a-backup).
5. **In the plan card**, the **Orphaned plans** view has buttons next to **Restore**:
   delete the selected backup, delete all backups of the plan, and keep them (the pin) —
   pressed again on a kept plan, it takes that decision back. Each asks for confirmation.

The repair closes by itself when the plan exists again under the same ID and name, or when
its backups are deleted with one of the actions below. If the plan list cannot be read
from Comexio, no repair is raised or closed.

To delete backups without waiting for a repair:

- **Function Plan Delete Backups** deletes one snapshot, all snapshots of one plan, or
  every stored backup (requires `confirm`). Kept plans can be deleted this way too.
- **Function Plan Purge Orphaned Backups** deletes the backups of every deleted plan past
  the retention period at once, except the ones you kept (requires `confirm`).

## 5. Related actions and entities

| Name | Purpose |
|------|---------|
| `comexio.function_plan_restore` | Restore a snapshot, in place or as a new plan. |
| `comexio.function_plan_list_backups` | List snapshots as a service response, filterable and sortable, optionally with a diff. |
| `comexio.function_plan_visualize` | Render a live plan or a snapshot as SVG or text. |
| `comexio.function_plan_delete_backups` | Delete one snapshot, one plan's snapshots, or all. |
| `comexio.function_plan_purge_orphaned_backups` | Delete the expired backups of all deleted plans. |
| `comexio.function_plan_keep_backups` | Keep a deleted plan's backups for good, or take that back (`keep: false`). |
| `sensor` **Backups** (diagnostic) | Number of stored snapshots, details per plan as attributes. |
| `select` **Backup** | Picks the snapshot the plan preview shows; in the **Orphaned plans** view, a deleted plan's backup. |

---

# 🇩🇪 Deutsch

Wie die Integration Comexio-Logikpläne sichert, wie man ein Backup ansieht und
wiederherstellt, und was mit den Backups eines in Comexio gelöschten Plans passiert.

## Inhaltsverzeichnis

1. [Was gesichert wird](#1-was-gesichert-wird)
2. [Ein Backup ansehen](#2-ein-backup-ansehen)
3. [Ein Backup wiederherstellen](#3-ein-backup-wiederherstellen)
4. [Backups gelöschter Pläne](#4-backups-gelöschter-pläne)
5. [Zugehörige Aktionen und Entitäten](#5-zugehörige-aktionen-und-entitäten)

---

## 1. Was gesichert wird

Jeder Logikplan in Comexio wird automatisch gesichert, nicht nur die von der Integration
verwalteten. Einzustellen gibt es nichts.

| Typ | Wann | Aufbewahrt je Plan |
|-----|------|--------------------|
| **auto** | Bei jedem Poll, aber nur wenn sich der Inhalt seit dem letzten Auto-Backup geändert hat (Inhalts-Hash). | die 3 neuesten |
| **change** | Unmittelbar bevor die Integration selbst einen Plan ändert (Connect, Sort, Restore, …), mit dieser Aktion als Vermerk. | die 10 neuesten |

- Ein Plan wird an **ID und Name** zusammen erkannt. Comexio vergibt die ID eines
  gelöschten Plans an den nächsten neuen Plan, eine ID allein würde also zwei
  verschiedene Pläne vermischen.
- Ein neuer Snapshot verdrängt den ältesten seines Typs. Sonst löscht nichts die Backups
  eines noch existierenden Plans, egal wie alt sie sind.
- Elemente nur zu verschieben, ohne die Verdrahtung zu ändern, erzeugt kein Auto-Backup.
- Die Snapshots liegen im `.storage`-Ordner von Home Assistant und sind damit in jedem
  Home-Assistant-Backup enthalten.

## 2. Ein Backup ansehen

- **Plan-Karte:** Plan wählen, dann in der Auswahl **Backup** ein Backup
  wählen. Die Karte zeigt diesen Snapshot statt des Live-Plans; **Live** schaltet zurück.
  Siehe die [Anleitung zur Logikplan-Vorschau](FUNCTION_PLAN_PREVIEW.md).
- **Backups gelöschter Pläne in der Plan-Karte:** **Orphaned plans** wählen (letzter
  Eintrag der Auswahl **Plan**, sichtbar, solange es solche Backups gibt). Die
  Auswahl **Backup** listet dann jeden gelöschten Plan als
  `<Name> (ID <n>) — <Anzahl> backups`, darunter eingerückt seine Snapshots; die Planzeile
  zeigt den neuesten. Die Vorschau ist mit `[verwaist]` markiert und zeigt keine
  Live-Werte, weil die ID inzwischen einem anderen Plan gehören kann. Audit und Sync
  arbeiten weiter mit dem vorher gewählten Plan.
- **Jedes Backup, auch das gelöschter Pläne:** Entwicklerwerkzeuge → Aktionen →
  **Function Plan Visualize**, Feld **Snapshot**, Format `svg` oder `text`. Jeder Eintrag
  lautet `<Planname> — fub <ID> — <Typ>[<Slot>] — <Datum>`. Das funktioniert ohne
  Verbindung zu Comexio.
- **Was sich geändert hat:** **Function Plan Analyze** und die Option `diff` von
  **Function Plan List Backups** vergleichen Snapshots.

## 3. Ein Backup wiederherstellen

**Function Plan Restore** (oder der **Restore**-Knopf der Plan-Karte) schreibt einen
Snapshot zurück nach Comexio: Struktur, Element-Positionen und die Papier-/DPI-Einstellung
der Zeichenfläche.

- Vor dem Schreiben sichert er den aktuellen Stand als Change-Backup, danach prüft er das
  Ergebnis gegen den Inhalts-Hash des Snapshots.
- Das Feld **Snapshot** wählt das genaue Ziel in einem Schritt. Die Felder `fub_id`,
  `kind` und `slot` sind eine Alternative für Skripte und werden ignoriert, sobald
  `snapshot` gesetzt ist.
- Wurde der ursprüngliche Plan **gelöscht** oder gehört seine ID inzwischen einem
  **anderen Plan**, legt der Restore einen **neuen Plan** an (`on_conflict: new_id`). Mit
  `confirm: true` und `on_conflict: force_override` überschreibt er den Plan, der die ID
  jetzt belegt.
- In der Ansicht **Orphaned plans** der Plan-Karte legt **Restore** immer einen neuen Plan
  an.

## 4. Backups gelöschter Pläne

Wird ein Plan direkt in Comexio gelöscht, bleiben seine Backups erhalten. Mit ihnen lässt
sich der Plan zurückholen, deshalb löscht die Integration sie nie von selbst:

1. **Aufbewahrung:** Die Option **Aufbewahrung verwaister Backups** (Optionen →
   Logikplan, Standard 6 Monate) legt fest, wie lange die Backups eines gelöschten Plans
   aufbewahrt werden, gerechnet ab seinem neuesten Backup.
2. **Reparaturmeldung:** Ist diese Frist abgelaufen, erscheint pro Plan eine Meldung
   **Backups des gelöschten Plans &lt;Name&gt;**. Sie nennt die frühere ID, den Plan, der
   diese ID jetzt belegt (falls vorhanden), die Anzahl der Backups und das Datum des
   neuesten.

   Ein in Comexio **umbenannter** Plan zählt ebenfalls als gelöscht: Seine Backups von vor
   der Umbenennung tragen den alten Namen, die neuen liegen unter dem neuen Namen. Ist der
   Plan, der die ID jetzt belegt, dein umbenannter Plan, geht es in der Meldung um seine
   ältere Historie.
3. **Deine Entscheidung:**
   - **In Vorschau laden** schaltet die Plan-Karte auf **Orphaned plans** und zeigt das
     neueste Backup des Plans. Es entscheidet nichts — die Meldung bleibt offen.
   - **Behalten** bewahrt die Backups dauerhaft auf und fragt für diesen Plan nicht mehr.
   - **Löschen** entfernt alle Backups dieses Plans.
   - **Ignorieren** (Home Assistants eigener Knopf) blendet die Meldung aus und löscht
     nichts.
4. **Vorher ansehen:** Außer der Plan-Karte zeigt **Function Plan Visualize** jedes
   Backup, wie in [Abschnitt 2](#2-ein-backup-ansehen) beschrieben.
5. **In der Plan-Karte** hat die Ansicht **Orphaned plans** Knöpfe neben **Restore**: das
   gewählte Backup löschen, alle Backups des Plans löschen und sie behalten (die
   Stecknadel) — bei einem behaltenen Plan nimmt sie die Entscheidung zurück. Jeder Knopf
   fragt vorher nach.

Die Meldung schließt sich von selbst, wenn es den Plan unter derselben ID und demselben
Namen wieder gibt oder wenn seine Backups mit einer der Aktionen unten gelöscht werden.
Lässt sich die Planliste nicht aus Comexio lesen, wird keine Meldung angelegt oder
geschlossen.

Backups löschen, ohne auf eine Meldung zu warten:

- **Function Plan Delete Backups** löscht einen Snapshot, alle Snapshots eines Plans oder
  sämtliche Backups (erfordert `confirm`). Auch behaltene Pläne lassen sich so löschen.
- **Function Plan Purge Orphaned Backups** löscht auf einmal die Backups aller gelöschten
  Pläne, deren Frist abgelaufen ist, außer den behaltenen (erfordert `confirm`).

## 5. Zugehörige Aktionen und Entitäten

| Name | Zweck |
|------|-------|
| `comexio.function_plan_restore` | Snapshot wiederherstellen, am Platz oder als neuen Plan. |
| `comexio.function_plan_list_backups` | Snapshots als Service-Response auflisten, filter- und sortierbar, optional mit Diff. |
| `comexio.function_plan_visualize` | Live-Plan oder Snapshot als SVG oder Text darstellen. |
| `comexio.function_plan_delete_backups` | Einen Snapshot, die Snapshots eines Plans oder alle löschen. |
| `comexio.function_plan_purge_orphaned_backups` | Die abgelaufenen Backups aller gelöschten Pläne löschen. |
| `comexio.function_plan_keep_backups` | Die Backups eines gelöschten Plans dauerhaft behalten oder das zurücknehmen (`keep: false`). |
| `sensor` **Backups** (Diagnose) | Anzahl gespeicherter Snapshots, Details je Plan als Attribute. |
| `select` **Backup** | Wählt den Snapshot, den die Plan-Vorschau zeigt; in der Ansicht **Orphaned plans** ein Backup eines gelöschten Plans. |
