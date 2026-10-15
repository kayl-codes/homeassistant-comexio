# Review checklist — homeassistant-comexio

The local review agents check every diff against these rules before a push.

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
  `fub_id` of a deleted plan, and plan names are not unique. Flag code that matches backups, snapshots, watches,
  findings or in-flight guards of a vanished plan (the set of plans whose restore runs) to a plan by only one of
  the two — a guard keyed by name alone blocks a same-named other plan (#181). A live plan can be renamed while an
  operation on it runs, so the exclusion of operations on a live plan is a lock or the `plan_edit()` marker, never a
  name-keyed set.
- **Names are no proof:** "a plan with that name exists" proves neither that a restore or create succeeded nor which
  plan it is — a half-built earlier restore or an unrelated plan with the same name matches too. A create succeeded
  only if it reports the new plan's `fub_id`; that id proves the create, not the restore built on it — a restore
  succeeded only by its final status (see *Report after the final status*). Persisted data still matches a plan by
  `fub_id` and name together (see *Plan identity*). The one valid name lookup recovers the id after Comexio confirmed a create without
  returning one, and accepts only exactly one plan with that name whose id was unknown before the create
  (`_find_created_plan`). It cannot tell that plan from a same-named plan created in Comexio Studio at the same
  moment — an accepted limit, concurrent Studio edits are out of scope; do not flag it. Flag any other name lookup
  used as a success check or as the plan's identity.
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
- **Hold the guard across the writes:** a check that refuses to start while a restore, sync, cleanup or plan edit
  runs protects only if the caller then holds the lock or the `coordinator.plan_edit()` marker across all of its
  writes, and the other operation checks that lock or marker too, in both directions. Flag a "nothing is running"
  check followed by writes without either, and a new operation that writes function plans or backups without
  joining the mutual exclusion (`refuse_while_plans_busy`, `_async_acquire_sync_lock`, `_cleanup_blocked_by_sync`,
  the restore refusal) (#176).
- **Awaits inside the guard's `try`:** every `await` between setting a guard flag (`unloading = True`, a busy flag)
  and the `try/finally` that resets it sits inside the `try` — a raised or cancelled await otherwise leaves the flag
  set forever. Flag an `await` between the flag assignment and the `try` (#181).
- **No await between reading and writing the options:** code that copies `entry.options`, changes the copy and
  saves it with `async_update_entry` has no `await` in between — another save during that await (options flow,
  watchdog, sync) is otherwise overwritten with the stale copy. Await follow-ups (push dismissal, notifications)
  after the save. Flag an `await` between the read of `entry.options` and its write (#181).
- **Unload waits for running work:** `async_unload_entry` calls `coordinator.async_wait_before_unload()` before it
  unloads the platforms; the wait is bounded and polls — it never queues on a lock — for every operation that
  writes Comexio or the options: a restore, a sync, a vanished-plan restore, a plan edit. A new
  long-running operation joins that wait and refuses to start while `coordinator.unloading` is set. Flag an
  operation that can still run or start after the unload passed its wait (#181).
- **Background tasks report the whole flow:** a task started with `async_create_background_task` (or
  `hass.async_create_task`) wraps its whole flow — waits and lookups included, not only the main call — in one
  error path that logs and reports the failure; otherwise an early exception vanishes in the task. A one-off flow
  (a restore, a service) reports each failure to the user (notification, push); a recurring task reports by the
  rule *Repeated calls stay quiet*. A follow-up after the success (repoint, confirmation push) gets its own `try`, so its failure does not
  report the finished work as failed (#181).
- **Report after the final status:** a result report (a "restored completely/OK" flag, a success push or note) is
  recorded only after the last status change — a later step (lineage move, reference repoint, warnings) can still
  turn OK into PARTIAL, and a report written earlier claims a success that did not happen (#181).
- **Early returns:** a coordinator cycle that changes state an entity shows and then returns early still calls
  `async_update_listeners()`, otherwise the entity shows the old state until the next poll.
- **Timestamps:** a timestamp that starts a cooldown or retry interval, or records the last attempt, is taken
  after the awaited request it belongs to, not before — otherwise a slow Comexio shortens the interval.
- **Repeated calls stay quiet:** paths the plan card or a timer repeats (keepalive, retries, periodic ticks) report
  failures at debug level, without a persistent notification or repair each time — e.g.
  `_resolve_coordinator(..., quiet=True)`. Flag a repeated path that notifies on every failure. A failed action that
  is retried on each check (an auto-start, an undelivered push) waits a minimum interval from `const.py` between
  attempts — the plan card checks every few seconds (#181). An open-ended series (an unanswered start, an undelivered
  push) warns only on the first failure and logs the retries at debug level; a series capped by a give-up counter
  (a refused auto-start, `FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS`) may warn on each counted attempt. Automatic
  retries are not sent while Comexio is unreachable or its state could not be read: the watchdog's
  `_plan_watch_held()` and a failed run-state read, which starts nothing. This holds back the automatic retries only —
  a start the user triggers from a repair or push is sent.
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
- A test for an action that must leave Comexio and the entities untouched (ignore, cancel, abort) asserts what the
  action does change — e.g. the saved ignore option, the dismissed issue — and that no deleting or writing method of
  the API, the entity/device registry or the recorder ran: `assert_not_awaited()` for coroutines,
  `assert_not_called()` for synchronous calls. Dismissing the action's own issue is the one expected issue-registry
  write and is asserted, not forbidden.
  An options-flow test asserts the stored value, not only that a key is absent (#160).
- A test asserting that an issue is absent also proves the evaluating path ran, otherwise it passes without the fix
  (#160): for a deletion in `tests/ha/`, the issue existed before and is gone after; in `tests/unit/`, where `ir` is
  mocked, `async_delete_issue` was called with exactly that issue_id and a counter-case (the issue still applies)
  asserts it was not; for an issue that is never raised, a concrete counter value or another result of the same run.
- Flag deleted or loosened assertions and snapshot updates that are not explained in the PR.
- Every test fails when the path it targets is missing. Assert that path's own signal — a log line, a flag, the
  arguments a mock received, the data actually stored — not only a call count that other paths reach too. A test
  of a race or timeout switches the state during the await it targets (e.g. inside the mocked call) and proves the
  timeout fired; a quick check is to break the target path (raise the timeout, drop the guard) and see it fail
  (#168).
- Test function parameters carry type annotations.

## GitHub Actions

- Pin actions to full commit SHAs with a `# vX.Y.Z` comment, except `home-assistant/actions/hassfest@master` and
  `hacs/action@main`, which intentionally track their branch.
- Every workflow has a least-privilege `permissions:` block; secrets go through `env:`, never inline in `run:`.
