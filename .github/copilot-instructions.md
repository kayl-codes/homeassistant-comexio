# Copilot instructions — homeassistant-comexio

HACS custom integration (`custom_components/comexio/`, domain `comexio`) for the Comexio IO-Server, a local
building-automation controller. Python ≥ 3.12, minimum Home Assistant 2026.8.0. Parsing, scraping and Web-IO
command building live in the separate PyPI library `aiocomexio`; this repo wires them into Home Assistant.
`CLAUDE.md` in the repo root describes the architecture in detail and is the reference for these rules.

## Code review focus

- Start each review comment with a one-sentence summary of the suggested fix.
- Focus on correctness: logic errors, unhandled edge cases, race conditions, resource leaks, silent failures
  (swallowed exceptions, fallbacks that hide stale or missing data), and wrong use of Home Assistant APIs.
- Do **not** comment on formatting, import order or line length — ruff (`ruff check` + `ruff format`, line length
  120, rule sets B, C4, E, F, I, SIM, UP, W) enforces those in CI.
- Do not suggest extra defensive checks for service fields already validated by the service schema or entity
  selectors; suggest guards only where data bypasses those validators.
- Where validation guarantees a dict key exists, prefer `data["key"]` over `data.get("key")` so contract
  violations surface instead of being masked.

## Project rules to check

- **Complexity:** cognitive complexity ≤ 15 per function (SonarQube S3776); no duplicated string literals — extract
  constants (S1192). Tuning values and thresholds belong in `const.py`, not inline.
- **Stable IDs:** unique_ids and entity_ids carry only the technical address (`comexio_{server_id}_m{id}`,
  `..._k{id}`, `..._{ext_name}_{identifier}`, `..._fub{id}`); only the display name carries the Comexio
  description. Flag any change that would let a renamed description or naming schema alter a unique_id or entity_id.
- **Persisted identifiers:** the lowercase `logikplan_*` option keys, `Store` storage keys and the plan-selector
  unique_id are frozen legacy spellings. Flag renames without migration code.
- **Terminology:** "function plan" is the English term in code, services, logs, notifications and en/fr/es
  translations; "Logikplan" is the German translation, used in `de.json` and the German sections of the Markdown
  docs (and as a parenthetical gloss such as "function plan (Logikplan)"), never in English UI text or code. Never
  reintroduce `logicplan`.
- **Translations:** `translations/strings.json` is authoritative; every new key must also exist in `en.json`,
  `de.json`, `fr.json` and `es.json`. `persistent_notification` texts are English.
- **Services:** every service logs its parameters and reports its duration.
- **Concurrency:** Web-IO syncs run under the coordinator's `_sync_lock`; webhook values arriving during a config
  fetch must win over the stale API snapshot (guard R1). Flag changes that weaken these guards.
- **Poll timer:** `async_set_updated_data` cancels and reschedules the coordinator's periodic poll. Code that runs
  often — webhook pushes, plan preview refreshes, periodic ticks — must not call it, otherwise frequent calls keep
  postponing the poll indefinitely (#122). Notify only the listeners that need the update: webhook values through
  `_async_publish_pushed_value()`, coordinator entities through `async_update_listeners()`, entities fed by a
  dedicated dispatcher signal (e.g. the bus-load tick's `bus_load_signal`) through that signal alone. One-off paths
  (after a sync, a repair or a button press) may still call `async_set_updated_data`.
- **Plan existence:** whether a function plan still exists is decided by `live_plan_list()` (the plan list of the
  last poll; `None` until the first full poll), never by a bulk-load snapshot — the bulk load may omit plans. Flag
  code that drops findings, backups or watches of a plan only because a snapshot lacks it.
- **Plan identity:** a function plan is identified by its `fub_id` and its name together — Comexio reuses the
  `fub_id` of a deleted plan, and plan names are not unique. Flag code that matches backups, snapshots, watches or
  findings to a plan by only one of the two.
- **Data read from Comexio:** an entry counts as missing from a list or mapping read from Comexio (e.g. `$Fubs`)
  only after the whole list passed a shape check — every entry has the expected type, its key is a canonical id
  string, and an `Id` the entry carries matches that key. An unreadable or malformed list means "unknown", never "deleted". Flag code that turns a
  read or parse failure into "entry missing" and acts on it (#158).
- **Audit categories:** a new category in `last_audit_results` that adds to the mismatch count also goes into the
  change hash in `_log_audit_summary`, the consolidated warning line in `_log_audit_details`, and a test — otherwise
  the summary reports issues that no category shows (#153).
- **Re-check under the lock:** state checked before waiting for a lock (an armed preview, a selection generation, a
  cached object, a running sync or restore) can change while waiting; check it again once the lock is held, as
  `plan_preview.async_follow_selection` does. Flag a check followed by `async with <lock>` without that re-check.
- **Early returns:** a coordinator cycle that changes state an entity shows and then returns early still calls
  `async_update_listeners()`, otherwise the entity shows the old state until the next poll.
- **Timestamps:** a timestamp that starts a cooldown or retry interval, or records the last attempt, is taken
  after the awaited request it belongs to, not before — otherwise a slow Comexio shortens the interval.
- **Repeated calls stay quiet:** paths the plan card or a timer repeats (keepalive, retries, periodic ticks) report
  failures at debug level, without a persistent notification or repair each time — e.g.
  `_resolve_coordinator(..., quiet=True)`. Flag a repeated path that notifies on every failure.
- **Repair flows read the current entry:** before a destructive action (cleanup, delete, reset), a repair flow
  step reads the current config entry state (`options`/`data`) again instead of relying on `issue_data` alone —
  the dialog may have been open while the user changed the options, so the issue data can be stale (#160).
- **Blocking I/O:** no blocking calls in the event loop; Comexio serialises requests, so avoid needless round-trips.

## Tests

- `tests/unit/` holds pure-logic tests with synthetic fixtures in `tests/fixtures/comexio/` (never real
  installation data). Every bug fix in pure logic needs a regression test that fails without the fix.
- `tests/ha/` holds Home Assistant integration tests; they build a `ComexioAPI` only through the `mock_comexio_api`
  fixture in `tests/ha/conftest.py` and never reach the network. Flag a test that constructs or patches it elsewhere.
- Exact pins of manifest.json requirements in `tests/requirements.txt` and `tests/ha/requirements.txt` must match
  manifest.json (`scripts/generate_requirements.py --check` enforces it). `ci.yml` never names a manifest package
  itself; it installs them with `-r requirements.txt` (`test_ci_workflow_installs_from_requirements_txt`).
- Every reason a repair flow aborts with needs a translation under `issues.<translation_key>.fix_flow.abort`
  in all five translation files (`tests/unit/test_repair_translations.py`); a new fixable issue goes into that
  test's `ENTRY_STEP_BY_ISSUE` (the step `async_step_init` routes it to; the test reads the issue_id from the
  raise site). A computed `translation_key` must come from a `self.` helper returning constants or from a
  parameter whose callers pass constants; `is_fixable` stays a literal `True`/`False`.
- A test for an action that deliberately does nothing (ignore, cancel, abort) also asserts that no deleting or
  writing method ran — `assert_not_awaited()` for coroutines, `assert_not_called()` for synchronous calls such as
  `ir.async_delete_issue`. An options-flow test asserts the stored value, not only that a key is absent (#160).
- A test asserting that an issue is absent or was deleted also proves the path ran — a call of
  `async_delete_issue`/`async_create_issue` or a concrete counter value — otherwise it passes without the fix
  (#160).
- Flag deleted or loosened assertions and snapshot updates that are not explained in the PR.
- Test function parameters carry type annotations.

## GitHub Actions

- Pin actions to full commit SHAs with a `# vX.Y.Z` comment, except `home-assistant/actions/hassfest@master` and
  `hacs/action@main`, which intentionally track their branch.
- Every workflow has a least-privilege `permissions:` block; secrets go through `env:`, never inline in `run:`.
