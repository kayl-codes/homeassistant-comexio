// Version: 0.9.7
// Comexio function plan preview card — renders the plan-preview SVG INLINE (not via <img>).
//
// Why inline: an SVG inside an <img> is static — no :hover rules, no <title> tooltips,
// no JS. This card fetches the SVG text from the image entity, then swaps it into the
// DOM in one synchronous step (fetch first, swap after), so live updates can never
// show a blank loading frame either.
//
// Dashboard config:
//   type: custom:comexio-plan-card
//   entity: image.system_iosrv1_plan_vorschau

// Pure, `this`-independent helpers (pattern matching, timestamp formatting) live in a
// sibling module — see comexio-plan-card-utils.js. Deployment note: this split means the
// card now ships as TWO files that must be copied together into the same www/hacsfiles
// folder (relative import path below), not just this one file as before.
import { matchesPattern, fmtTs } from "./comexio-plan-card-utils.js";

// Version banner: lets the user verify in the browser console WHICH build actually
// executes — ?v= query bumps proved unreliable against the service-worker cache.
console.info("comexio-plan-card v0.9.7 (Auto-Stop-Meldung im Log, ! als eigene Hilfe-Zeile) loaded");

// Seed filter for a card whose filter was never touched (localStorage key absent):
// hides the periodically chattering analog inputs. A deliberately cleared filter
// ("" in storage) stays cleared — the default must never resurrect itself.
const DEFAULT_DEBUG_FILTER = "*#TL1, *#UL1, *#AI*, *#QI*";

class ComexioPlanCard extends HTMLElement {
  constructor() {
    super();
    this._stamp = null; // last image_last_updated we rendered
    this._fetchSeq = 0; // guards against out-of-order fetch responses
    this._pattern = "";
    this._zoom = 1;
    this._debugOn = false;
    this._debugLines = []; // ring buffer of {ts, text, cls}, capped at 200 lines
    this._unsubDebug = null; // active comexio_plan_event subscription (null = none)
    this._subPending = false; // guards against double-subscribe while awaiting
    this._history = []; // debug command history, persisted per entity (newest last)
    this._histIdx = null; // null = not navigating; index into _history (length = draft row)
    this._histDraft = ""; // what was typed before history navigation started
    this._targets = new Map(); // set_value targets of the shown plan (from data-target attrs)
    this._labelToTarget = new Map(); // event label -> command address (M107, BASE#UL1) for the filter
    this._sugIdx = -1; // highlighted autocomplete suggestion (-1 = none)
    this._filterPatterns = []; // debug-log exclude patterns (comma-separated user input)
    this._logPaused = false; // pause button: drop incoming events (deliberately NOT persisted)
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("comexio-plan-card: 'entity' is required (the plan preview image entity)");
    }
    this._config = config;
    // Restore the per-entity zoom factor (survives reloads; purely client-side).
    const saved = Number(localStorage.getItem(`comexio-plan-zoom:${config.entity}`));
    this._zoom = saved >= 0.25 && saved <= 4 ? saved : 1;
    this._debugOn = localStorage.getItem(`comexio-plan-debug:${config.entity}`) === "1";
    let hist = null;
    try {
      hist = JSON.parse(localStorage.getItem(`comexio-plan-cmdhist:${config.entity}`));
    } catch {
      // corrupt entry — start with an empty history
    }
    this._history = Array.isArray(hist) ? hist.filter((h) => typeof h === "string") : [];
    if (!this.shadowRoot) {
      this._buildDom();
    }
    this._zoomLabel.textContent = `${Math.round(this._zoom * 100)} %`;
    const storedFilter = localStorage.getItem(`comexio-plan-debugfilter:${config.entity}`);
    const savedFilter = storedFilter === null ? DEFAULT_DEBUG_FILTER : storedFilter;
    this._filterInput.value = savedFilter;
    this._setDebugFilter(savedFilter);
    const savedHeight = Number(localStorage.getItem(`comexio-plan-debugheight:${config.entity}`));
    if (savedHeight >= 60 && savedHeight <= 800) {
      this._setLogHeight(savedHeight, false);
    }
    this._applyDebugVisibility();
  }

  _buildDom() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        /* overflow: visible — an overflow other than visible on ha-card would turn IT
           into the sticky containing block and kill the bottom-pinned debug box. */
        ha-card { padding: 8px; overflow: visible; }
        .toolbar { display: flex; align-items: center; gap: 8px; padding: 0 4px 8px 4px; }
        .toolbar input, .debug input {
          flex: 1; min-width: 0; padding: 4px 8px;
          border: 1px solid var(--divider-color, #888); border-radius: 6px;
          background: var(--card-background-color, transparent);
          color: var(--primary-text-color, inherit); font: inherit;
        }
        .toolbar .hits { font-size: 0.85em; color: var(--secondary-text-color, #888); white-space: nowrap; }
        .toolbar button {
          background: none; border: none; padding: 2px; margin: 0; cursor: pointer;
          color: var(--secondary-text-color, #888); line-height: 0;
        }
        .toolbar button:hover { color: var(--primary-text-color, #000); }
        .toolbar button.active { color: var(--primary-color, #03a9f4); }
        .toolbar ha-icon { --mdc-icon-size: 20px; }
        .toolbar .zoom-label {
          font-size: 0.85em; color: var(--secondary-text-color, #888);
          min-width: 42px; text-align: center; cursor: pointer; white-space: nowrap;
        }
        .cross-results {
          padding: 0 4px 8px 4px; font-size: 0.85em; color: var(--secondary-text-color, #888);
        }
        .cross-results[hidden] { display: none; }
        .plan { line-height: 0; overflow-x: auto; }
        /* Sticky bottom: when the card is taller than the window, the debug box pins to
           the bottom of the dashboard scrollport and the plan scrolls behind it (opaque
           background + z-index cover the overlap). At the card end it settles back into
           its natural flow position. */
        .debug {
          position: sticky; bottom: 0; z-index: 2;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border-top: 1px solid var(--divider-color, #888);
          margin-top: 8px; padding: 8px 4px 8px 4px;
        }
        .debug[hidden] { display: none; }
        /* Drag grip: pulls the log height up/down; the chosen height persists per entity. */
        .debug-grip {
          height: 10px; margin: -6px 0 2px 0; cursor: ns-resize; touch-action: none;
          display: flex; align-items: center; justify-content: center;
        }
        .debug-grip::before {
          content: ""; width: 48px; height: 3px; border-radius: 2px;
          background: var(--divider-color, #888);
        }
        .debug-grip:hover::before { background: var(--primary-color, #03a9f4); }
        .debug-filter { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
        .debug-filter > ha-icon { --mdc-icon-size: 16px; color: var(--secondary-text-color, #888); flex: none; }
        .debug-filter.active > ha-icon { color: var(--primary-color, #03a9f4); }
        .debug-filter input { font-size: 0.85em; }
        .debug-filter button {
          background: none; border: none; padding: 2px; margin: 0; cursor: pointer;
          color: var(--secondary-text-color, #888); line-height: 0; flex: none;
        }
        .debug-filter button:hover { color: var(--primary-text-color, #000); }
        .debug-filter button ha-icon { --mdc-icon-size: 18px; }
        /* Paused logging is a state worth noticing — warning colour, not the usual blue. */
        .debug-filter button.paused, .debug-filter button.paused:hover { color: var(--warning-color, #ff9800); }
        .debug-log {
          font-family: var(--code-font-family, monospace); font-size: 0.8em; line-height: 1.5;
          max-height: 180px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
          color: var(--secondary-text-color, #888);
        }
        .debug-log .cmd { color: var(--primary-text-color, #000); }
        .debug-log .err { color: var(--error-color, #c62828); }
        .debug input.debug-cmd { display: block; width: 100%; box-sizing: border-box; margin-top: 6px; }
        .debug-sug {
          border: 1px solid var(--divider-color, #888); border-radius: 6px; margin-top: 4px;
          background: var(--card-background-color, #fff); font-size: 0.85em; overflow: hidden;
        }
        .debug-sug[hidden] { display: none; }
        .debug-sug div {
          padding: 2px 8px; cursor: pointer;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .debug-sug div.sel, .debug-sug div:hover { background: rgba(240, 192, 0, 0.25); }
        .debug-sug .sug-target { font-family: var(--code-font-family, monospace); }
        /* Click-to-fill: plan nodes act as buttons ONLY while the debug box is open. */
        .plan.debug-live g.node-g[data-target] { cursor: pointer; }
        .plan svg { width: 100%; height: auto; display: block; }
        .msg { padding: 16px; color: var(--secondary-text-color, #888); line-height: 1.4; }

        /* Node texts must not swallow the pointer — the body rect below carries the
           <title> tooltip and the hover highlight. */
        .plan .node-g text { pointer-events: none; }

        /* Node hover: 2.5x border on the body rect. */
        .plan g.node-g:hover rect { stroke-width: 2.5; }

        /* Wire hover: yellow + 2x (1.2 -> 2.4), applied to the WHOLE net (all fan-out
           branches + junction dots sharing the same data-net). Triggered via the
           invisible wide hit path the card clones next to each wire. Specificity
           (element + 2 classes) deliberately beats the SVG's own edge-hot rules. */
        .plan path.edge-hit {
          stroke: transparent; stroke-width: 10; fill: none; pointer-events: stroke;
        }
        .plan path.edge-line.edge-hover { stroke: #f0c000; stroke-width: 2.4; }
        .plan circle.edge-junction.edge-hover { fill: #f0c000; }

        /* Search: matched nodes get a yellow border, everything else fades. */
        .plan svg.searching g.node-g:not(.search-hit) { opacity: 0.25; }
        .plan svg.searching path.edge-line, .plan svg.searching path.edge-hit,
        .plan svg.searching circle { opacity: 0.25; }
        .plan g.node-g.search-hit rect { stroke: #f0c000; stroke-width: 3; }
        .plan g.node-g.search-hit text.node-comment { fill: #f0c000; }

        /* Help dialog: native <dialog> renders in the top layer, so it always sits
           above the plan/debug box regardless of the card's own stacking context. */
        .help-dialog {
          border: 1px solid var(--divider-color, #888); border-radius: 8px; padding: 16px;
          width: min(640px, 92vw); max-height: 80vh; overflow-y: auto;
          background: var(--card-background-color, #fff); color: var(--primary-text-color, inherit);
          font: inherit;
        }
        .help-dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
        .help-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .help-head h3 { margin: 0; font-size: 1.1em; }
        .help-close {
          background: none; border: none; padding: 2px; margin: 0; cursor: pointer;
          color: var(--secondary-text-color, #888); line-height: 0;
        }
        .help-close:hover { color: var(--primary-text-color, #000); }
        .help-dialog table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
        .help-dialog th, .help-dialog td {
          text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--divider-color, #ddd);
          vertical-align: top;
        }
        .help-dialog th { color: var(--secondary-text-color, #888); font-weight: 600; }
        .help-dialog td:first-child { white-space: nowrap; }
        .help-dialog code {
          font-family: var(--code-font-family, monospace); background: rgba(128, 128, 128, 0.15);
          padding: 0 3px; border-radius: 3px;
        }
        .help-dialog em { color: var(--secondary-text-color, #888); font-style: normal; font-size: 0.9em; }
      </style>
      <ha-card>
        <div class="toolbar">
          <button class="help-toggle" title="Hilfe: Bedienung der Karte" aria-label="Hilfe anzeigen"><ha-icon icon="mdi:help-circle-outline"></ha-icon></button>
          <input type="search" placeholder="Suche… (z. B. M14, M1?, IOX3 #*) — Enter: alle Pläne durchsuchen" aria-label="Suche im Plan">
          <span class="hits"></span>
          <button class="zoom-out" title="Verkleinern" aria-label="Verkleinern"><ha-icon icon="mdi:magnify-minus-outline"></ha-icon></button>
          <span class="zoom-label" title="Klick: zurück auf 100 %">100 %</span>
          <button class="zoom-in" title="Vergrößern" aria-label="Vergrößern"><ha-icon icon="mdi:magnify-plus-outline"></ha-icon></button>
          <button class="debug-toggle" title="Debug-Box ein-/ausblenden" aria-label="Debug-Box umschalten"><ha-icon icon="mdi:console-line"></ha-icon></button>
        </div>
        <div class="cross-results" hidden></div>
        <div class="plan"></div>
        <div class="debug" hidden>
          <div class="debug-grip" title="Ziehen: Log-Höhe ändern"></div>
          <div class="debug-filter" title="Signale aus dem Log ausblenden — Komma-getrennt, Wildcards wie in der Suche (M1?, BASE#*, *UL1*); Strg+Klick im Plan fügt hinzu">
            <button class="debug-pause" title="Logging anhalten" aria-label="Logging anhalten/fortsetzen"><ha-icon icon="mdi:pause"></ha-icon></button>
            <button class="debug-clear" title="Log leeren (/clear)" aria-label="Log leeren"><ha-icon icon="mdi:eraser"></ha-icon></button>
            <ha-icon icon="mdi:filter-outline"></ha-icon>
            <input class="debug-filter-input" type="text"
                   placeholder="Ausblenden… z. B. BASE#UL1, *UL1*, M1? (Komma-getrennt, Strg+Klick im Plan fügt hinzu)"
                   aria-label="Log-Filter: auszublendende Signale">
          </div>
          <div class="debug-log" aria-live="polite"></div>
          <input class="debug-cmd" type="text"
                 placeholder="Befehl… z. B. M107=1 oder IOX2#Q3=0 (↑/↓ Historie, ! erzwingt, /clear, /history)"
                 aria-label="Debug-Befehl">
          <div class="debug-sug" hidden></div>
        </div>
      </ha-card>
      <dialog class="help-dialog">
        <div class="help-head">
          <h3>Bedienung der Plan-Vorschau</h3>
          <button class="help-close" title="Schließen" aria-label="Schließen"><ha-icon icon="mdi:close"></ha-icon></button>
        </div>
        <table>
          <thead><tr><th>Aktion</th><th>Wirkung</th></tr></thead>
          <tbody>
            <tr><td>Suche tippen</td><td>Hebt passende Elemente hervor (gelber Rahmen), der Rest wird abgedunkelt. Platzhalter: <code>?</code> = ein Zeichen, <code>*</code> = beliebig viele (z. B. <code>M1?</code>, <code>IOX3#*</code>).</td></tr>
            <tr><td>Enter in der Suche</td><td>Durchsucht alle Pläne. Bei genau einem Treffer wird der Plan automatisch ausgewählt.</td></tr>
            <tr><td>Hover über Element</td><td>Zeigt den vollen Namen als Tooltip.</td></tr>
            <tr><td>Hover über Draht</td><td>Hebt das gesamte elektrische Netz (alle Äste) gelb hervor.</td></tr>
            <tr><td>Lupe − / +</td><td>Zoom verkleinern / vergrößern.</td></tr>
            <tr><td>Klick auf %-Anzeige</td><td>Setzt den Zoom auf 100&nbsp;% zurück.</td></tr>
            <tr><td>Konsolen-Symbol</td><td>Blendet die Debug-Box ein/aus.</td></tr>
            <tr><td>Klick auf Element <em>(Debug-Box offen)</em></td><td>Übernimmt die Adresse des Elements ins Befehlsfeld.</td></tr>
            <tr><td>Doppelklick, digital + beschreibbar <em>(Debug-Box offen)</em></td><td>Schaltet den Wert sofort um (0 ↔ 1).</td></tr>
            <tr><td>Doppelklick, analog + beschreibbar <em>(Debug-Box offen)</em></td><td>Füllt das Befehlsfeld mit <code>Ziel=</code> vor, Wert selbst eintippen.</td></tr>
            <tr><td>Doppelklick auf Eingang <em>(Debug-Box offen)</em></td><td>Keine Wirkung — Eingänge sind nur lesbar.</td></tr>
            <tr><td>Strg+Klick auf Element <em>(Debug-Box offen)</em></td><td>Fügt das Signal dem Log-Ausblendfilter hinzu.</td></tr>
            <tr><td>Befehlsfeld: <code>Ziel=Wert</code></td><td>Schreibt den Wert, z. B. <code>M107=1</code> oder <code>IOX2#Q3=0,5</code>.</td></tr>
            <tr><td>Befehlsfeld: <code>!</code> am Ende</td><td>Erzwingt das Schreiben von einem Merker oder IO, auch wenn es nicht im angezeigten Plan vorhanden ist, z. B. <code>M107=1!</code>.</td></tr>
            <tr><td>Befehlsfeld: ↑ / ↓</td><td>Blättert durch die Befehls-Historie.</td></tr>
            <tr><td>Befehlsfeld: Tab / Enter bei Vorschlag</td><td>Übernimmt den markierten Autocomplete-Vorschlag.</td></tr>
            <tr><td>Befehlsfeld: <code>/clear</code>, <code>/history</code></td><td>Leert das Log bzw. zeigt die Befehls-Historie im Log an.</td></tr>
            <tr><td>Befehlsfeld: <code>/extend &lt;Minuten&gt;</code></td><td>Verlängert den Live-Poll dieser Vorschau (Standard: automatischer Stopp nach 15 Minuten) einmalig auf die angegebene Dauer — gilt nur für diese Sitzung, nicht gespeichert.</td></tr>
            <tr><td>Filter-Eingabe (Trichter-Symbol)</td><td>Blendet passende Signale aus dem Log aus — Komma-getrennt, gleiche Platzhalter-Syntax wie die Suche.</td></tr>
            <tr><td>Pause-Symbol</td><td>Hält das Live-Logging an bzw. setzt es fort (verworfene Ereignisse werden nicht nachgeliefert).</td></tr>
            <tr><td>Radiergummi-Symbol</td><td>Leert das Log.</td></tr>
            <tr><td>Griff über dem Log ziehen</td><td>Ändert die Höhe der Log-Box.</td></tr>
          </tbody>
        </table>
      </dialog>`;
    this._helpDialog = root.querySelector(".help-dialog");
    root.querySelector(".help-toggle").addEventListener("click", () => this._helpDialog.showModal());
    root.querySelector(".help-close").addEventListener("click", () => this._helpDialog.close());
    // Click on the backdrop (event target is the <dialog> itself, not a descendant) closes it too.
    this._helpDialog.addEventListener("click", (ev) => {
      if (ev.target === this._helpDialog) {
        this._helpDialog.close();
      }
    });
    this._input = root.querySelector("input");
    this._hitsEl = root.querySelector(".hits");
    this._zoomLabel = root.querySelector(".zoom-label");
    root.querySelector(".zoom-out").addEventListener("click", () => this._setZoom(this._zoom / 1.25));
    root.querySelector(".zoom-in").addEventListener("click", () => this._setZoom(this._zoom * 1.25));
    this._zoomLabel.addEventListener("click", () => this._setZoom(1));
    this._resultsEl = root.querySelector(".cross-results");
    this._planEl = root.querySelector(".plan");
    this._debugEl = root.querySelector(".debug");
    this._logEl = root.querySelector(".debug-log");
    this._cmdInput = root.querySelector(".debug-cmd");
    this._debugBtn = root.querySelector(".debug-toggle");
    this._debugBtn.addEventListener("click", () => this._toggleDebug());
    this._filterRow = root.querySelector(".debug-filter");
    this._filterInput = root.querySelector(".debug-filter-input");
    this._filterInput.addEventListener("input", () => this._setDebugFilter(this._filterInput.value));
    root.querySelector(".debug-clear").addEventListener("click", () => this._clearLog());
    this._pauseBtn = root.querySelector(".debug-pause");
    this._pauseBtn.addEventListener("click", () => this._toggleLogPause());
    // Log-height drag grip: pointer capture keeps the drag alive outside the handle.
    // The box is bottom-pinned, so dragging UP means MORE height (inverted delta).
    const grip = root.querySelector(".debug-grip");
    grip.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      grip.setPointerCapture(ev.pointerId);
      const startY = ev.clientY;
      const startH = this._logEl.getBoundingClientRect().height;
      const onMove = (mev) => this._setLogHeight(startH + (startY - mev.clientY), false);
      const onUp = () => {
        grip.removeEventListener("pointermove", onMove);
        grip.removeEventListener("pointerup", onUp);
        this._setLogHeight(this._logEl.getBoundingClientRect().height, true);
      };
      grip.addEventListener("pointermove", onMove);
      grip.addEventListener("pointerup", onUp);
    });
    this._sugEl = root.querySelector(".debug-sug");
    this._cmdInput.addEventListener("keydown", (ev) => this._onCmdKey(ev));
    this._cmdInput.addEventListener("input", () => this._updateSuggestions());
    this._cmdInput.addEventListener("blur", () => this._hideSuggestions());
    this._crossSearchInFlight = false;
    this._input.addEventListener("input", () => {
      this._pattern = this._input.value.trim();
      this._resultsEl.hidden = true; // stale cross-plan result from a previous query
      this._applySearch();
    });
    this._input.addEventListener("keydown", (ev) => {
      console.debug("comexio-plan-card: keydown", ev.key, "pattern=", this._pattern);
      if (ev.key === "Enter" && this._pattern) {
        this._searchAllPlans();
      }
    });
  }

  // Cross-plan search: delegates to the function_plan_search service (loads every live
  // plan from the Comexio server), so it only runs on explicit Enter, never per keystroke.
  // An unambiguous single-plan hit is auto-selected in the "Function Plans" select entity by
  // the service itself — the card only has to surface the result text here.
  async _searchAllPlans() {
    if (this._crossSearchInFlight) {
      return;
    }
    const query = this._pattern;
    console.debug("comexio-plan-card: _searchAllPlans start, query=", query);
    this._crossSearchInFlight = true;
    this._resultsEl.textContent = "Suche in allen Plänen…";
    this._resultsEl.hidden = false;
    try {
      // hass.callService() does not reliably forward return_response through to the
      // websocket call — go straight to the connection so the service response is
      // guaranteed to come back.
      const result = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "comexio",
        service: "function_plan_search",
        service_data: { query },
        return_response: true,
      });
      console.debug("comexio-plan-card: _searchAllPlans result", result);
      this._renderCrossResults(result?.response);
    } catch (err) {
      console.warn("comexio-plan-card: cross-plan search failed", err);
      this._resultsEl.textContent = "Suche in allen Plänen fehlgeschlagen.";
    } finally {
      this._crossSearchInFlight = false;
    }
  }

  _renderCrossResults(data) {
    if (!data) {
      // Older HA frontends may not return the service response payload — the backend
      // still posted a persistent_notification with the same result as a fallback.
      this._resultsEl.textContent = "Suche gestartet — Ergebnis siehe Benachrichtigung.";
      return;
    }
    const { plan_count: planCount, match_count: matchCount, results, selector_set: selectorSet } = data;
    if (!planCount) {
      this._resultsEl.textContent = `Kein Treffer für "${this._pattern}" in allen Plänen.`;
    } else if (planCount === 1 && selectorSet) {
      const r = results[0];
      this._resultsEl.textContent = `→ automatisch gewählt: ${r.plan_name} (${r.matches.length} Treffer)`;
    } else {
      const perPlan = results.map((r) => `${r.plan_name} (${r.matches.length})`).join(" · ");
      this._resultsEl.textContent = `${matchCount} Treffer in ${planCount} Plänen: ${perPlan}`;
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (this._debugOn) {
      this._ensureDebugSubscription(); // deferred until hass exists (also re-arms after reconnect)
    }
    const st = hass.states[this._config.entity];
    if (!st || st.state === "unavailable" || st.state === "unknown") {
      this._stamp = null;
      // textContent, not innerHTML — entity id and state are outside data.
      const msg = document.createElement("div");
      msg.className = "msg";
      msg.textContent = `Keine Vorschau verfügbar (${this._config.entity}: ${st ? st.state : "nicht gefunden"}).`;
      this._planEl.replaceChildren(msg);
      return;
    }
    if (st.state === this._stamp) {
      return; // same image_last_updated — nothing new to fetch
    }
    this._stamp = st.state;
    this._loadSvg(st.attributes.entity_picture, st.state);
  }

  async _loadSvg(url, stamp) {
    if (!url) {
      return;
    }
    const seq = ++this._fetchSeq;
    try {
      // entity_picture is a stable proxy URL — bust the browser cache per update.
      const sep = url.includes("?") ? "&" : "?";
      const resp = await fetch(`${url}${sep}state=${encodeURIComponent(stamp)}`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const text = await resp.text();
      if (seq !== this._fetchSeq) {
        return; // a newer update already superseded this response
      }
      // Atomic swap: the old SVG stays on screen until the new one is fully here.
      // Trust boundary: the SVG is generated by our own integration (labels are
      // html.escape'd server-side) and served via HA's authenticated image proxy —
      // no third-party content ever flows through here.
      // nosemgrep: javascript.browser.security.insecure-document-method, javascript.browser.security.insecure-innerhtml
      this._planEl.innerHTML = text;
      this._enhance();
      this._applySearch();
    } catch (err) {
      console.warn("comexio-plan-card: preview fetch failed, keeping previous image", err);
    }
  }

  _enhance() {
    const svg = this._planEl.querySelector("svg");
    if (!svg) {
      return;
    }
    this._applyZoom();
    // Wires are 1.2px — too thin to hover. Clone an invisible 10px-wide hit path
    // right next to each wire (same z-order, so nodes drawn later still win).
    // Hovering highlights the WHOLE net (all branches + junction dots with the same
    // data-net) — the branches of a fan-out share their trunk segment, so lighting
    // only the hovered path would look like a broken wire.
    for (const wire of svg.querySelectorAll("path.edge-line")) {
      const hit = wire.cloneNode(false);
      hit.setAttribute("class", "edge-hit");
      hit.removeAttribute("stroke-width");
      delete hit.dataset.net;
      const net = wire.dataset.net ?? null;
      const members = () => (net === null ? [wire] : svg.querySelectorAll(`[data-net="${net}"]`));
      hit.addEventListener("mouseenter", () => {
        for (const el of members()) {
          el.classList.add("edge-hover");
        }
      });
      hit.addEventListener("mouseleave", () => {
        for (const el of members()) {
          el.classList.remove("edge-hover");
        }
      });
      wire.after(hit);
    }
    // Debug-box support: collect the plan's set_value targets (markers/IOs annotated by
    // the renderer) for autocomplete + command validation, and wire up click-to-fill /
    // double-click-to-toggle. The handlers no-op while the debug box is closed.
    this._targets = new Map();
    this._labelToTarget = new Map(); // lets the log filter resolve "BASE UL1 …" -> BASE#UL1
    for (const node of svg.querySelectorAll("g.node-g[data-target]")) {
      const { target } = node.dataset;
      const info = {
        analog: node.dataset.analog === "1",
        writable: node.dataset.writable === "1",
        value: node.dataset.value,
        label: node.dataset.label || target,
      };
      this._targets.set(target, info);
      this._labelToTarget.set(info.label, target);
      node.addEventListener("click", (ev) => this._nodeClick(target, info, false, ev));
      node.addEventListener("dblclick", (ev) => {
        ev.preventDefault(); // no text-selection flash on the SVG labels
        this._nodeClick(target, info, true, ev);
      });
    }
    this._planEl.classList.toggle("debug-live", this._debugOn);
  }

  _setZoom(zoom) {
    this._zoom = Math.min(4, Math.max(0.25, zoom));
    try {
      localStorage.setItem(`comexio-plan-zoom:${this._config.entity}`, String(this._zoom));
    } catch {
      // private mode / storage full — zoom just won't persist
    }
    this._applyZoom();
  }

  // Zoom baseline (100 %) = current fit: 1:1 studio units, capped at card width.
  // The SVG carries its own inline max-width (server-side 1:1 cap) — at 100 % we
  // leave it alone; zoomed we override it and let .plan scroll horizontally.
  _applyZoom() {
    this._zoomLabel.textContent = `${Math.round(this._zoom * 100)} %`;
    const svg = this._planEl.querySelector("svg");
    const vb = svg?.viewBox?.baseVal;
    if (!vb || vb.width <= 0) {
      return;
    }
    if (this._zoom === 1) {
      svg.style.width = "";
      svg.style.maxWidth = `${vb.width}px`;
    } else {
      const baseline = Math.min(this._planEl.clientWidth || vb.width, vb.width);
      svg.style.width = `${baseline * this._zoom}px`;
      svg.style.maxWidth = "none";
    }
  }

  // ---- Debug box: timestamped event log + raw write commands -------------------------
  // The integration fires comexio_plan_event on the HA bus for every webhook value push
  // that belongs to the plan currently shown in the live preview (filtering happens
  // server-side, so an idle card costs nothing). Commands go to comexio.set_value.

  _toggleDebug() {
    this._debugOn = !this._debugOn;
    try {
      localStorage.setItem(`comexio-plan-debug:${this._config.entity}`, this._debugOn ? "1" : "0");
    } catch {
      // private mode / storage full — the toggle just won't persist
    }
    this._applyDebugVisibility();
  }

  _applyDebugVisibility() {
    this._debugEl.hidden = !this._debugOn;
    this._debugBtn.classList.toggle("active", this._debugOn);
    this._planEl.classList.toggle("debug-live", this._debugOn); // pointer cursor on nodes
    this._hideSuggestions();
    if (this._debugOn) {
      this._ensureDebugSubscription();
    } else {
      this._dropDebugSubscription();
    }
    this._setDebugSession(this._debugOn);
  }

  // Tells the backend to speed up the live plan preview's Stufe-2 wire-value poll to
  // 0.5s (matching the webhook-debounce cadence) while the debug box is open, so the
  // colored wires keep up with what the log is showing; falls back to the slower
  // background cadence once closed. Fire-and-forget — an older backend without this
  // service (not yet updated) must not break the debug box itself.
  _setDebugSession(open) {
    if (!this._hass) {
      return;
    }
    this._hass.callService("comexio", "function_plan_debug_session", { open }).catch((err) => {
      console.warn("comexio-plan-card: function_plan_debug_session failed", err);
    });
  }

  async _ensureDebugSubscription() {
    if (this._unsubDebug || this._subPending || !this._hass) {
      return;
    }
    this._subPending = true;
    try {
      this._unsubDebug = await this._hass.connection.subscribeEvents(
        (ev) => this._onPlanEvent(ev),
        "comexio_plan_event"
      );
      this._debugLine("Debug-Box aktiv — lausche auf Wert-Änderungen im angezeigten Plan…", "cmd");
    } catch (err) {
      console.warn("comexio-plan-card: event subscription failed", err);
    } finally {
      this._subPending = false;
    }
    if (!this._debugOn) {
      this._dropDebugSubscription(); // toggled off again while the subscribe was in flight
    }
  }

  _dropDebugSubscription() {
    if (this._unsubDebug) {
      this._unsubDebug();
      this._unsubDebug = null;
    }
  }

  disconnectedCallback() {
    this._dropDebugSubscription(); // re-armed via `set hass` when the card reattaches
    if (this._debugOn) {
      this._setDebugSession(false); // don't leave the backend stuck in the fast cadence
    }
  }

  // Exclude filter: comma-separated patterns in COMMAND notation (M107, BASE#UL1 — same
  // spelling as the debug command line), matched with the token-anchored wildcard
  // semantics of the plan search. Matching events are silently dropped — they never
  // reach the log buffer. Plain label patterns ("BASE UL1") keep working too.
  _setDebugFilter(raw) {
    this._filterPatterns = raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    this._filterRow.classList.toggle("active", this._filterPatterns.length > 0);
    try {
      localStorage.setItem(`comexio-plan-debugfilter:${this._config.entity}`, raw);
    } catch {
      // private mode / storage full — the filter just won't persist
    }
  }

  // Ctrl+Klick on a plan node routes here: append the node's command address to the
  // filter (comma-separated, no duplicates) and activate it immediately.
  _addFilterPattern(target) {
    if (this._filterPatterns.some((p) => p.toLowerCase() === target.toLowerCase())) {
      return; // already filtered
    }
    // Strip trailing whitespace/commas one char at a time (no quantified regex — avoids
    // the super-linear-backtracking pattern static analysis flags on `[\s,]+$`).
    let current = this._filterInput.value;
    while (current.length > 0 && /[\s,]/.test(current[current.length - 1])) {
      current = current.slice(0, -1);
    }
    const next = current ? `${current}, ${target}` : target;
    this._filterInput.value = next;
    this._setDebugFilter(next);
    this._debugLine(`Filter: ${target} wird ausgeblendet (Eintrag im Filterfeld entfernen zum Aufheben)`, "cmd");
  }

  // A pattern hides an event when it hits the label ("BASE UL1 Versorgungsspannung"),
  // the command address resolved from the shown plan ("BASE#UL1"), or — as fallback for
  // custom naming schemas — the label with the pattern's '#' read as a space.
  _eventHidden(label, target) {
    return this._filterPatterns.some(
      (p) =>
        matchesPattern(label, p) ||
        (target && matchesPattern(target, p)) ||
        (p.includes("#") && matchesPattern(label, p.replaceAll("#", " ")))
    );
  }

  // Pause button: incoming events are dropped (not buffered) — after resume the log
  // continues live. Deliberately not persisted so a reload never starts silently paused.
  _toggleLogPause() {
    this._logPaused = !this._logPaused;
    this._pauseBtn.classList.toggle("paused", this._logPaused);
    this._pauseBtn.querySelector("ha-icon").setAttribute("icon", this._logPaused ? "mdi:play" : "mdi:pause");
    this._pauseBtn.title = this._logPaused ? "Logging fortsetzen" : "Logging anhalten";
    this._debugLine(
      this._logPaused ? "Logging angehalten — eingehende Ereignisse werden verworfen" : "Logging fortgesetzt",
      "cmd"
    );
  }

  _onPlanEvent(ev) {
    if (this._logPaused) {
      return; // pause button active — drop the event
    }
    const d = ev.data || {};
    if (d.type === "system") {
      // Backend status line (e.g. auto-stop), not a signal value — always shown, never
      // subject to the exclude filter below.
      this._debugLine(d.message, "cmd", ev.time_fired);
      return;
    }
    const label = d.label ?? `${d.type} ${d.id}`;
    const target = d.type === "marker" ? `M${d.id}` : this._labelToTarget.get(label) || null;
    if (this._eventHidden(label, target)) {
      return; // user chose to hide this signal from the log
    }
    const value = typeof d.value === "number" ? String(d.value).replace(".", ",") : String(d.value);
    this._debugLine(`${label} = ${value}`, "", ev.time_fired);
  }

  _setLogHeight(height, persist) {
    const h = Math.round(Math.min(800, Math.max(60, height)));
    this._logEl.style.height = `${h}px`;
    this._logEl.style.maxHeight = "none"; // a fixed height overrides the auto-grow cap
    if (persist) {
      try {
        localStorage.setItem(`comexio-plan-debugheight:${this._config.entity}`, String(h));
      } catch {
        // private mode / storage full — the height just won't persist
      }
    }
  }

  _clearLog() {
    this._debugLines = [];
    this._logEl.replaceChildren();
  }

  // Local slash commands — handled entirely in the card, nothing goes to the server.
  _runSlashCommand(raw) {
    const cmd = raw.slice(1).toLowerCase();
    if (cmd === "clear") {
      this._clearLog();
      return;
    }
    if (cmd === "history" || cmd === "historie") {
      if (!this._history.length) {
        this._debugLine("Befehls-Historie ist leer.", "cmd");
        return;
      }
      this._debugLine(`Befehls-Historie (${this._history.length}, älteste zuerst):`, "cmd");
      for (const h of this._history) {
        this._debugLine(`  ${h}`, "cmd");
      }
      return;
    }
    const extendMatch = cmd.match(/^extend\s+(\d+)$/);
    if (extendMatch) {
      this._runExtendCommand(Number(extendMatch[1]));
      return;
    }
    this._debugLine(`Unbekannter Befehl: ${raw} — verfügbar: /clear, /history, /extend <Minuten>`, "err");
  }

  // Extends the live preview's auto-stop window (default 15 min) for this arm only — not
  // persisted, resets to the default the next time a plan is opened. See coordinator's
  // set_preview_auto_stop_extension / the function_plan_preview_extend service.
  async _runExtendCommand(minutes) {
    if (minutes < 1 || minutes > 1440) {
      this._debugLine(`✗ /extend ${minutes}: Bitte eine Minutenanzahl zwischen 1 und 1440 angeben`, "err");
      return;
    }
    if (!this._hass) {
      return;
    }
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "comexio",
        service: "function_plan_preview_extend",
        service_data: { minutes },
        return_response: true,
      });
      const r = result?.response;
      if (r?.success === false) {
        this._debugLine(`✗ /extend ${minutes}: ${r.error || "fehlgeschlagen"}`, "err");
      } else {
        this._debugLine(`✓ Live-Poll bleibt jetzt ${minutes} Minuten aktiv`, "cmd");
      }
    } catch (err) {
      this._debugLine(`✗ /extend ${minutes}: ${err?.message || err}`, "err");
    }
  }

  _debugLine(text, cls = "", when = null) {
    const ts = fmtTs(when ? new Date(when) : new Date());
    this._debugLines.push({ ts, text, cls });
    if (this._debugLines.length > 200) {
      this._debugLines.splice(0, this._debugLines.length - 200);
    }
    // Stick to the bottom (newest line) unless the user scrolled up to read history.
    const nearBottom = this._logEl.scrollTop + this._logEl.clientHeight >= this._logEl.scrollHeight - 8;
    this._logEl.replaceChildren(
      ...this._debugLines.map((line) => {
        const div = document.createElement("div");
        div.textContent = `${line.ts}  ${line.text}`;
        if (line.cls) {
          div.className = line.cls;
        }
        return div;
      })
    );
    if (nearBottom) {
      this._logEl.scrollTop = this._logEl.scrollHeight;
    }
  }

  async _sendCommand(rawInput) {
    const raw = rawInput.trim();
    if (!raw) {
      return;
    }
    if (raw.startsWith("/")) {
      // Slash commands stay out of the persisted history — /history output stays clean.
      this._cmdInput.value = "";
      this._hideSuggestions();
      this._runSlashCommand(raw);
      return;
    }
    // "M107=1" or "IOX2#Q3=0,5" — a trailing "!" forces sending even when the target is
    // not part of the currently shown plan (the service still validates it globally).
    const m = raw.match(/^([^=\s!]+)\s*=\s*(-?[\d.,]+)\s*(!?)$/);
    if (!m) {
      this._debugLine(`Ungültiger Befehl: "${raw}" — erwartet z. B. M107=1 oder IOX2#Q3=0 (! erzwingt)`, "err");
      return;
    }
    const [, target, value, force] = m;
    const info = this._lookupTarget(target);
    if (!force) {
      const guardError = this._targetGuardError(target, value, info);
      if (guardError) {
        this._debugLine(guardError, "err");
        return;
      }
    }
    this._pushHistory(raw);
    this._cmdInput.value = "";
    this._hideSuggestions();
    this._debugLine(`→ ${target} = ${value} …`, "cmd");
    await this._submitCommand(target, value);
  }

  // Guard against typos: only targets of the SHOWN plan pass without "!" (the
  // input stays filled so the user can just append the "!" to override).
  _targetGuardError(target, value, info) {
    if (this._targets.size && !info) {
      return `✗ ${target} ist nicht im aktuellen Plan — mit ${target}=${value}! erzwingen`;
    }
    if (info && !info.writable) {
      return `✗ ${target} ist ein Eingang (nur lesbar) — die Comexio-API kann Eingänge nicht schreiben`;
    }
    return null;
  }

  async _submitCommand(target, value) {
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "comexio",
        service: "set_value",
        service_data: { target, value },
        return_response: true,
      });
      const r = result?.response;
      if (r?.success === false) {
        this._debugLine(`✗ ${target}: ${r.error || "Schreiben fehlgeschlagen"}`, "err");
      } else {
        const durationSuffix = r?.duration != null ? ` (${r.duration}s)` : "";
        this._debugLine(`✓ ${target} = ${value} geschrieben${durationSuffix}`, "cmd");
      }
    } catch (err) {
      this._debugLine(`✗ ${target}: ${err?.message || err}`, "err");
    }
  }

  // ---- Debug input UX: command history, autocomplete, click-to-fill ------------------

  _onCmdKey(ev) {
    const sugOpen = !this._sugEl.hidden;
    if (ev.key === "Escape" && sugOpen) {
      this._hideSuggestions();
      ev.preventDefault();
      return;
    }
    if ((ev.key === "ArrowDown" || ev.key === "ArrowUp") && sugOpen) {
      this._navigateSuggestions(ev.key);
      ev.preventDefault();
      return;
    }
    if (ev.key === "Tab" && sugOpen && this._sugEl.children.length) {
      this._completeSuggestion(this._sugEl.children[Math.max(this._sugIdx, 0)].dataset.target);
      ev.preventDefault();
      return;
    }
    if (ev.key === "Enter") {
      this._handleEnterKey(sugOpen);
      return;
    }
    if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
      this._histNav(ev.key === "ArrowUp" ? -1 : 1);
      ev.preventDefault();
    }
  }

  // Suggestions take precedence over history while the list is open.
  _navigateSuggestions(key) {
    const count = this._sugEl.children.length;
    if (key === "ArrowDown") {
      this._sugIdx = (this._sugIdx + 1) % count;
    } else if (this._sugIdx <= 0) {
      this._sugIdx = count - 1;
    } else {
      this._sugIdx -= 1;
    }
    [...this._sugEl.children].forEach((el, i) => el.classList.toggle("sel", i === this._sugIdx));
  }

  _handleEnterKey(sugOpen) {
    if (sugOpen && this._sugIdx >= 0) {
      this._completeSuggestion(this._sugEl.children[this._sugIdx].dataset.target);
    } else {
      this._hideSuggestions();
      this._sendCommand(this._cmdInput.value);
    }
  }

  _histNav(dir) {
    if (!this._history.length) {
      return;
    }
    if (this._histIdx === null) {
      // Entering history: remember the draft so ArrowDown past the newest restores it.
      this._histDraft = this._cmdInput.value;
      this._histIdx = this._history.length;
    }
    this._histIdx = Math.min(Math.max(this._histIdx + dir, 0), this._history.length);
    this._cmdInput.value = this._histIdx === this._history.length ? this._histDraft : this._history[this._histIdx];
    this._hideSuggestions();
  }

  _pushHistory(cmd) {
    if (this._history.at(-1) !== cmd) {
      this._history.push(cmd);
      if (this._history.length > 50) {
        this._history.splice(0, this._history.length - 50);
      }
      try {
        localStorage.setItem(`comexio-plan-cmdhist:${this._config.entity}`, JSON.stringify(this._history));
      } catch {
        // private mode / storage full — history just won't persist
      }
    }
    this._histIdx = null;
    this._histDraft = "";
  }

  _lookupTarget(name) {
    const lc = name.toLowerCase();
    for (const [target, info] of this._targets) {
      if (target.toLowerCase() === lc) {
        return { target, ...info };
      }
    }
    return null;
  }

  _updateSuggestions() {
    this._histIdx = null; // typing ends history navigation
    const m = this._cmdInput.value.match(/^\s*([^=\s]+)$/); // only while the target is typed (no '=' yet)
    if (!this._debugOn || !m) {
      this._hideSuggestions();
      return;
    }
    const q = m[1].toLowerCase();
    const starts = [];
    const contains = [];
    for (const [target, info] of this._targets) {
      const tl = target.toLowerCase();
      if (tl === q) {
        continue; // already fully typed
      }
      if (tl.startsWith(q)) {
        starts.push([target, info]);
      } else if (tl.includes(q) || info.label.toLowerCase().includes(q)) {
        contains.push([target, info]);
      }
    }
    const items = [...starts, ...contains].slice(0, 8);
    if (!items.length) {
      this._hideSuggestions();
      return;
    }
    this._sugIdx = -1;
    this._sugEl.replaceChildren(
      ...items.map(([target, info]) => {
        const div = document.createElement("div");
        div.dataset.target = target;
        const t = document.createElement("span");
        t.className = "sug-target";
        t.textContent = target;
        const hint = info.label !== target ? `  ${info.label}` : "";
        div.append(t, `${hint}${info.writable ? "" : "  (nur lesbar)"}`);
        // mousedown, not click: preventDefault keeps the focus in the input.
        div.addEventListener("mousedown", (mev) => {
          mev.preventDefault();
          this._completeSuggestion(target);
        });
        return div;
      })
    );
    this._sugEl.hidden = false;
  }

  _hideSuggestions() {
    if (this._sugEl) {
      this._sugEl.hidden = true;
    }
    this._sugIdx = -1;
  }

  _completeSuggestion(target) {
    const info = this._lookupTarget(target);
    // Writable targets complete straight to "TARGET=" so the value can follow;
    // read-only ones (inputs) complete to the bare name.
    this._cmdInput.value = info?.writable ? `${target}=` : target;
    this._hideSuggestions();
    this._cmdInput.focus();
  }

  _nodeClick(target, info, dbl, ev) {
    if (!this._debugOn) {
      return; // plan clicks only mean something while the debug box is open
    }
    if (ev && (ev.ctrlKey || ev.metaKey)) {
      // Strg+Klick: hide this signal from the debug log instead of filling the input.
      if (!dbl) {
        this._addFilterPattern(target);
      }
      return; // the dblclick that follows a double Strg+Klick must not toggle anything
    }
    if (!dbl) {
      // Single click: replace whatever is in the input with the element's address.
      this._cmdInput.value = target;
      this._hideSuggestions();
      this._cmdInput.focus();
      return;
    }
    if (!info.writable) {
      return; // IO inputs cannot be written — double-click stays deliberately silent
    }
    if (info.analog) {
      // Analog targets: prefill up to the '=', the value is the user's call.
      this._cmdInput.value = `${target}=`;
      this._hideSuggestions();
      this._cmdInput.focus();
      return;
    }
    // Digital marker / digital IO output: write the INVERTED current value immediately.
    const cur = Number(String(info.value ?? "").replace(",", "."));
    const inv = Math.abs(cur - 1) < 1e-9 ? 0 : 1;
    this._cmdInput.value = `${target}=${inv}`;
    this._sendCommand(this._cmdInput.value);
  }

  _matches(label) {
    return matchesPattern(label, this._pattern);
  }

  _applySearch() {
    const svg = this._planEl.querySelector("svg");
    if (!svg) {
      return;
    }
    const active = this._pattern.length > 0;
    svg.classList.toggle("searching", active);
    let hits = 0;
    for (const node of svg.querySelectorAll("g.node-g")) {
      const hit = active && this._matches(node.dataset.label || "");
      node.classList.toggle("search-hit", hit);
      if (hit) {
        hits += 1;
      }
    }
    this._hitsEl.textContent = active ? `${hits} Treffer` : "";
  }

  getCardSize() {
    return 10;
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find((e) => e.startsWith("image.") && e.includes("plan"));
    return { entity: entity || "image.plan_vorschau" };
  }
}

customElements.define("comexio-plan-card", ComexioPlanCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "comexio-plan-card",
  name: "Comexio Plan Card",
  description: "Function plan preview with live updates, hover effects, tooltips, search, zoom and a debug box (inline SVG).",
});
