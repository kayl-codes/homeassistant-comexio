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
- **Blocking I/O:** no blocking calls in the event loop; Comexio serialises requests, so avoid needless round-trips.

## Tests

- `tests/unit/` holds pure-logic tests with synthetic fixtures in `tests/fixtures/comexio/` (never real
  installation data). Every bug fix in pure logic needs a regression test that fails without the fix.
- Flag deleted or loosened assertions and snapshot updates that are not explained in the PR.
- Test function parameters carry type annotations.

## GitHub Actions

- Pin actions to full commit SHAs with a `# vX.Y.Z` comment, except `home-assistant/actions/hassfest@master` and
  `hacs/action@main`, which intentionally track their branch.
- Every workflow has a least-privilege `permissions:` block; secrets go through `env:`, never inline in `run:`.
