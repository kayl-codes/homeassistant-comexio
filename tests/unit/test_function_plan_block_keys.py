"""Backups stay correct after Comexio shifted its logic-block ids (function_plan_block_keys.py)."""

import asyncio
import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioConnectionError
from aiocomexio.function_plan import plan_hash
from aiocomexio.reference_catalog import fub_base_key
import aiohttp
import pytest

from custom_components.comexio import (
    function_plan_backup as backup_module,
    function_plan_catalog as catalog_module,
    select as select_module,
)
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.function_plan_backup import SNAPSHOT_COMEXIO_VERSION, FunctionPlanBackupManager
from custom_components.comexio.function_plan_block_keys import (
    BLOCK_IDS_UNCHECKED,
    CHECK_PLAUSIBLE,
    CHECK_UNRESOLVED,
    CHECK_UNVERIFIED,
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_NO_KEY,
    SNAPSHOT_KEYS,
    SNAPSHOT_KEYS_SOURCE,
    SOURCE_BACKFILLED,
    SOURCE_BACKFILLED_PLAUSIBLE,
    SOURCE_BACKFILLED_UNVERIFIED,
    SOURCE_CAPTURED,
    UNCHECKED_NO_CATALOG,
    UNCHECKED_NO_KEYS,
    UNRESOLVED_BLOCKS,
    UNRESOLVED_REF_PREFIX,
    UNVERIFIED_BLOCK_PROBLEMS,
    backfill_block_keys,
    block_check,
    block_ids_changed,
    capture_block_keys,
    catalog_entry_key,
    implausible_block_wiring,
    resolve_block_ids,
)
from custom_components.comexio.function_plan_catalog import FunctionPlanCatalogManager
from custom_components.comexio.services import backup as backup_service
from tests.common import load_json_fixture

SERVER_ID = "cx1"
OR_KEY = "or/dd/d"
NOT_KEY = "not/d/d"
ADD_KEY = "add/aa/a"


def _block(name: str, key: str | None, in_types: list[int], out_types: list[int], autogrow: int = 0) -> dict:
    block = {
        "name": name,
        "n_in": len(in_types),
        "n_out": len(out_types),
        "autogrow": autogrow,
        "in_types": in_types,
        "out_types": out_types,
    }
    if key:
        block["key"] = key
    return block


OR = _block("or", OR_KEY, [0, 0], [0], autogrow=16)
NOT = _block("not", NOT_KEY, [0], [0])
ADD = _block("add", ADD_KEY, [1, 1], [1])
# The catalog the fixture plan was captured with: element 4 is an Oder (id 5), element 7 a Nicht (id 23).
FUB_BASE = {"5": OR, "23": NOT, "40": ADD}
# The same blocks after an app install shifted them; id 5 now names the analog adder.
SHIFTED = {"6": OR, "24": NOT, "5": ADD}
CHANGED_AT = "2026-09-01T00:00:00+00:00"


@pytest.fixture
def snapshot() -> dict[str, Any]:
    fixture = load_json_fixture("function_plan.json")
    plan = {"elements": fixture["elements"], "connections": fixture["connections"]}
    return {
        **plan,
        "hash": plan_hash(plan),
        "captured_at": "2026-09-10T08:00:00+00:00",
        "plan_name": "Lights",
        SNAPSHOT_KEYS: capture_block_keys(plan["elements"], FUB_BASE),
        SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED,
    }


def _ref_id(snap: dict[str, Any], elem_id: str) -> Any:
    return snap["elements"][elem_id]["reference"]["ref_id"]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_derived_key_matches_the_library_key() -> None:
    """Catalog entries cached before keys existed must derive the key the library computes."""
    raw = load_json_fixture("reference_catalog_raw.json")["FubModules"]["5"]["6"]  # 3-input Oder, Pos unordered

    assert catalog_entry_key({"name": "or", "in_types": [0, 0, 0], "out_types": [0]}) == fub_base_key(raw)


def test_stored_key_wins_over_the_derived_one() -> None:
    assert catalog_entry_key({**NOT, "key": "not/a/a"}) == "not/a/a"


def test_capture_freezes_the_keys_of_the_blocks_the_plan_uses(snapshot: dict[str, Any]) -> None:
    assert snapshot[SNAPSHOT_KEYS] == {"23": NOT_KEY, "5": OR_KEY}


@pytest.mark.parametrize(
    ("new_base", "changed"),
    [
        ({"5": {**OR, "display_name": "Oder-Baustein"}, "23": NOT, "40": ADD}, False),  # text only
        ({"5": {**OR, "key": None}, "23": NOT, "40": ADD}, False),  # cached before keys existed
        (SHIFTED, True),
        ({"5": OR, "23": NOT}, True),  # a block disappeared
    ],
)
def test_block_ids_changed_only_when_an_id_names_another_block(new_base: dict, changed: bool) -> None:
    assert block_ids_changed(FUB_BASE, new_base) is changed


# ---------------------------------------------------------------------------
# Use: translation to today's ids
# ---------------------------------------------------------------------------


def test_shifted_block_ids_are_translated(snapshot: dict[str, Any]) -> None:
    stored = copy.deepcopy(snapshot)

    resolved = resolve_block_ids(snapshot, SHIFTED)

    assert (_ref_id(resolved, "4"), _ref_id(resolved, "7")) == (6, 24)
    assert resolved[UNRESOLVED_BLOCKS] == []
    # A restore verifies against the translated plan, not the stored ids.
    assert resolved["hash"] == plan_hash(resolved) != stored["hash"]
    assert snapshot == stored  # the stored backup is never rewritten


def test_unchanged_block_ids_return_the_snapshot_itself(snapshot: dict[str, Any]) -> None:
    assert resolve_block_ids(snapshot, FUB_BASE) is snapshot


def test_swapped_block_ids_are_translated_at_once(snapshot: dict[str, Any]) -> None:
    resolved = resolve_block_ids(snapshot, {"23": OR, "5": NOT})

    assert (_ref_id(resolved, "4"), _ref_id(resolved, "7")) == (23, 5)


@pytest.mark.parametrize(
    ("fub_base", "reason"),
    [({"6": OR, "40": ADD}, REASON_MISSING), ({"6": OR, "24": NOT, "25": NOT}, REASON_AMBIGUOUS)],
)
def test_a_block_without_a_unique_live_counterpart_is_marked(
    snapshot: dict[str, Any], fub_base: dict, reason: str
) -> None:
    resolved = resolve_block_ids(snapshot, fub_base)

    assert resolved[UNRESOLVED_BLOCKS] == [{"element": "7", "ref_id": 23, "key": NOT_KEY, "reason": reason}]
    element = resolved["elements"]["7"]
    assert element["reference"]["ref_id"] == f"{UNRESOLVED_REF_PREFIX}23"
    assert element["name"].startswith("⚠ ")
    assert _ref_id(resolved, "4") == 6


def test_an_ambiguous_key_keeps_the_stored_id_while_it_still_fits(snapshot: dict[str, Any]) -> None:
    assert resolve_block_ids(snapshot, {**FUB_BASE, "24": NOT}) is snapshot


def test_a_block_unknown_at_capture_keeps_its_stored_id_and_is_listed(snapshot: dict[str, Any]) -> None:
    snapshot[SNAPSHOT_KEYS] = {"5": OR_KEY}

    resolved = resolve_block_ids(snapshot, SHIFTED)

    assert resolved[UNRESOLVED_BLOCKS] == [{"element": "7", "ref_id": 23, "key": None, "reason": REASON_NO_KEY}]
    # Kept unmarked: with the opt-in, a restore writes it exactly as stored.
    assert resolved["elements"]["7"] == snapshot["elements"]["7"]
    assert _ref_id(resolved, "4") == 6


@pytest.mark.parametrize(
    ("keys", "fub_base", "unchecked"),
    [(None, SHIFTED, UNCHECKED_NO_KEYS), ({"5": OR_KEY, "23": NOT_KEY}, {}, UNCHECKED_NO_CATALOG)],
)
def test_without_keys_or_catalog_the_stored_ids_are_kept_but_flagged(
    snapshot: dict[str, Any], keys: Any, fub_base: dict, unchecked: str
) -> None:
    if keys is None:
        del snapshot[SNAPSHOT_KEYS]

    resolved = resolve_block_ids(snapshot, fub_base)

    assert resolved[BLOCK_IDS_UNCHECKED] == unchecked
    assert resolved["elements"] is snapshot["elements"]


def test_a_plan_without_blocks_needs_no_check(snapshot: dict[str, Any]) -> None:
    snapshot["elements"] = {k: v for k, v in snapshot["elements"].items() if v["reference"]["type"] != 5}
    del snapshot[SNAPSHOT_KEYS]

    assert resolve_block_ids(snapshot, {}) is snapshot


# ---------------------------------------------------------------------------
# Wiring plausibility and backfill
# ---------------------------------------------------------------------------


def _wire(source: tuple[int, int], sink: tuple[int, int]) -> dict[str, Any]:
    return {
        "input": {"FubElementId": source[0], "IOPos": source[1], "Inverted": False},
        "output": [{"FubElementId": sink[0], "IOPos": sink[1], "Inverted": False}],
    }


def test_the_captured_wiring_fits_its_own_catalog(snapshot: dict[str, Any]) -> None:
    assert implausible_block_wiring(snapshot["elements"], snapshot["connections"], FUB_BASE) == []


@pytest.mark.parametrize(
    ("fub_base", "problem"),
    [
        (FUB_BASE, "element 7: no wire to check the block against"),
        ({"5": OR}, "element 7: block id 23 unknown today"),
    ],
)
def test_a_block_the_wiring_cannot_vouch_for_is_reported(
    snapshot: dict[str, Any], fub_base: dict, problem: str
) -> None:
    """Regression: an unwired block passed as plausible although nothing about it was checked."""
    del snapshot["connections"]["13"]  # the Nicht's only wire

    assert problem in implausible_block_wiring(snapshot["elements"], snapshot["connections"], fub_base)


def test_inputs_up_to_the_autogrow_limit_fit(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["90"] = _wire((2, 0), (4, 5))  # Oder grown to 6 inputs

    assert implausible_block_wiring(snapshot["elements"], snapshot["connections"], FUB_BASE) == []


@pytest.mark.parametrize(
    ("wire", "fub_base", "problem"),
    [
        (_wire((2, 0), (7, 1)), FUB_BASE, "input 1 beyond the not block's inputs"),
        (_wire((4, 1), (5, 0)), FUB_BASE, "output 1 beyond the or block's outputs"),
        (_wire((4, 0), (7, 0)), {**FUB_BASE, "23": ADD}, "input 0 wired to an output of another data type"),
    ],
)
def test_wiring_that_does_not_fit_todays_blocks_is_reported(
    snapshot: dict[str, Any], wire: dict, fub_base: dict, problem: str
) -> None:
    snapshot["connections"]["90"] = wire

    problems = implausible_block_wiring(snapshot["elements"], snapshot["connections"], fub_base)

    assert [p for p in problems if problem in p]


@pytest.mark.parametrize(
    ("captured_at", "fub_base", "source"),
    [
        ("2026-09-10T08:00:00+00:00", FUB_BASE, SOURCE_BACKFILLED),  # after the last id change
        ("2026-08-01T08:00:00+00:00", FUB_BASE, SOURCE_BACKFILLED_PLAUSIBLE),
        # Before the change, and today's id 5 is a one-input Nicht: the Oder's second input doesn't fit.
        ("2026-08-01T08:00:00+00:00", {"5": NOT, "23": OR}, SOURCE_BACKFILLED_UNVERIFIED),
    ],
)
def test_backfill_trusts_todays_catalog_only_as_far_as_it_can(
    snapshot: dict[str, Any], captured_at: str, fub_base: dict, source: str
) -> None:
    del snapshot[SNAPSHOT_KEYS], snapshot[SNAPSHOT_KEYS_SOURCE]
    snapshot["captured_at"] = captured_at

    assert backfill_block_keys(snapshot, fub_base, CHANGED_AT) == source
    assert snapshot[SNAPSHOT_KEYS_SOURCE] == source
    assert snapshot[SNAPSHOT_KEYS] == capture_block_keys(snapshot["elements"], fub_base)


def test_backfill_leaves_captured_keys_alone(snapshot: dict[str, Any]) -> None:
    stored = copy.deepcopy(snapshot)

    assert backfill_block_keys(snapshot, SHIFTED, CHANGED_AT) is None
    assert snapshot == stored


# ---------------------------------------------------------------------------
# Catalog: when did the block ids last change?
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store, keyed by storage key."""

    saved: dict[str, Any] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.saved.get(self.key)

    async def async_save(self, data: Any) -> None:
        FakeStore.saved[self.key] = data


def _raw_config(ids: dict[str, str], or_text: str = "Oder") -> dict[str, Any]:
    """Admin config with an Oder and a Nicht block under the given ids ({"or": id, "not": id})."""
    ports = {"or": ([0, 0], [0]), "not": ([0], [0])}
    modules = {
        block_id: {
            "Id": int(block_id),
            "Name": name,
            "input": [{"Pos": pos, "Type": t} for pos, t in enumerate(ports[name][0])],
            "output": [{"Pos": pos, "Type": t} for pos, t in enumerate(ports[name][1])],
        }
        for name, block_id in ids.items()
    }
    return {
        "FubTypes": {"5": "fubBase"},
        "FubModules": {"5": modules},
        "FubBaseI18N": {"or": {"name": or_text}, "not": {"name": "Nicht"}},
    }


def _at(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=UTC)


async def _update(manager: FunctionPlanCatalogManager, raw: dict[str, Any], day: int) -> tuple[str | None, str]:
    with patch.object(catalog_module.dt_util, "utcnow", return_value=_at(day)):
        await manager.async_update_from_raw_config(raw, "11.1.4")
    _fub_base, changed_at = await manager.async_get_fub_base()
    return changed_at, (await manager.async_get_catalog())["fetched_at"]


def _legacy_catalog() -> dict[str, Any]:
    """A catalog cached before keys and the block-id stamp existed."""
    return {
        "fetched_at": _at(1).isoformat(),
        "comexio_version": "11.1.4",
        "fub_types": {"5": "fubBase"},
        "fub_base": {"5": {**OR, "key": None}, "23": {**NOT, "key": None}},
    }


@pytest.fixture
def catalog_manager(monkeypatch: pytest.MonkeyPatch) -> FunctionPlanCatalogManager:
    FakeStore.saved = {}
    monkeypatch.setattr(catalog_module, "Store", FakeStore)
    return FunctionPlanCatalogManager(MagicMock(), SERVER_ID)


def test_catalog_stamps_only_block_id_changes(catalog_manager: FunctionPlanCatalogManager) -> None:
    ids = {"or": "5", "not": "23"}

    async def run() -> list[tuple[str | None, str]]:
        return [
            await _update(catalog_manager, _raw_config(ids), 1),
            await _update(catalog_manager, _raw_config(ids, or_text="ODER"), 2),  # UI text only
            await _update(catalog_manager, _raw_config({"or": "6", "not": "24"}), 3),  # app install
        ]

    first, text_change, shift = asyncio.run(run())

    assert first == (_at(1).isoformat(), _at(1).isoformat())
    assert text_change == (_at(1).isoformat(), _at(2).isoformat())
    assert shift == (_at(3).isoformat(), _at(3).isoformat())


def test_catalog_cached_before_the_stamp_falls_back_to_its_fetched_at(
    catalog_manager: FunctionPlanCatalogManager,
) -> None:
    FakeStore.saved[f"{DOMAIN}_logikplan_catalog_{SERVER_ID}"] = _legacy_catalog()

    changed_at, fetched_at = asyncio.run(_update(catalog_manager, _raw_config({"or": "5", "not": "23"}), 5))

    # Re-saved for the new key field, but no id moved: still the old (conservative) moment.
    assert (changed_at, fetched_at) == (_at(1).isoformat(), _at(5).isoformat())


def test_catalog_cached_before_the_stamp_sees_a_real_shift(catalog_manager: FunctionPlanCatalogManager) -> None:
    FakeStore.saved[f"{DOMAIN}_logikplan_catalog_{SERVER_ID}"] = _legacy_catalog()

    changed_at, _fetched_at = asyncio.run(_update(catalog_manager, _raw_config({"or": "6", "not": "24"}), 5))

    assert changed_at == _at(5).isoformat()


def test_catalog_stores_the_library_key(catalog_manager: FunctionPlanCatalogManager) -> None:
    raw = _raw_config({"or": "5", "not": "23"})

    async def run() -> dict[str, Any]:
        await _update(catalog_manager, raw, 1)
        return (await catalog_manager.async_get_fub_base())[0]

    fub_base = asyncio.run(run())

    assert {ref_id: entry["key"] for ref_id, entry in fub_base.items()} == {
        ref_id: fub_base_key(entry) for ref_id, entry in raw["FubModules"]["5"].items()
    }


def test_a_catalog_not_verified_in_the_last_poll_is_not_offered(catalog_manager: FunctionPlanCatalogManager) -> None:
    """A kept catalog may name shifted ids: backups must neither freeze nor translate with it."""

    async def state() -> tuple[tuple[dict[str, Any], str | None], dict[str, Any]]:
        return await catalog_manager.async_get_fub_base(), catalog_manager.fub_base_sync()

    async def run() -> list[tuple[tuple[dict[str, Any], str | None], dict[str, Any]]]:
        states = [await state()]  # loaded from disk, not compared yet
        await _update(catalog_manager, _raw_config({"or": "5", "not": "23"}), 1)
        states.append(await state())
        await _update(catalog_manager, _raw_config({"or": "5"}), 2)  # shrink-guarded: catalog kept
        states.append(await state())
        await _update(catalog_manager, {}, 3)  # admin page without the Fub* vars
        states.append(await state())
        return states

    before, verified, shrunk, missing = asyncio.run(run())

    assert before == shrunk == missing == (({}, None), {})
    assert before[1] is shrunk[1] is missing[1]  # one stable object: the selector's memo keeps hitting
    assert set(verified[0][0]) == set(verified[1]) == {"5", "23"}


# ---------------------------------------------------------------------------
# Backup manager: freeze at capture, translate on use, backfill
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    FakeStore.saved = {}
    monkeypatch.setattr(backup_module, "Store", FakeStore)
    return FakeStore.saved


def _catalog(fub_base: dict) -> SimpleNamespace:
    return SimpleNamespace(async_get_fub_base=AsyncMock(return_value=(fub_base, CHANGED_AT)))


def test_a_backup_freezes_its_keys_and_is_read_with_todays_ids(
    stores: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    catalog = _catalog(FUB_BASE)
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, catalog)
    plan = {"elements": snapshot["elements"], "connections": snapshot["connections"]}

    async def run() -> dict[str, Any] | None:
        await manager.async_change_backup(1, plan, "Lights", "sort")
        catalog.async_get_fub_base.return_value = (SHIFTED, CHANGED_AT)
        return await manager.async_get_snapshot("change", 1, "Lights")

    resolved = asyncio.run(run())

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert (stored[SNAPSHOT_KEYS], stored[SNAPSHOT_KEYS_SOURCE]) == ({"23": NOT_KEY, "5": OR_KEY}, SOURCE_CAPTURED)
    assert (_ref_id(stored, "4"), _ref_id(stored, "7")) == (5, 23)
    assert resolved is not None
    assert (_ref_id(resolved, "4"), _ref_id(resolved, "7")) == (6, 24)


def test_an_auto_backup_freezes_its_keys(stores: dict[str, Any], snapshot: dict[str, Any]) -> None:
    catalog = _catalog(FUB_BASE)
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, catalog)
    plan = {"elements": snapshot["elements"], "connections": snapshot["connections"]}

    async def run() -> list[list[dict[str, Any]]]:
        changed = [await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}})]
        catalog.async_get_fub_base.return_value = (SHIFTED, CHANGED_AT)
        changed.append(await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}))
        return changed

    first, after_shift = asyncio.run(run())

    history = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert len(first) == 1
    assert after_shift == []  # an id shift alone is no plan change
    assert (history[0][SNAPSHOT_KEYS], history[0][SNAPSHOT_KEYS_SOURCE]) == (
        {"23": NOT_KEY, "5": OR_KEY},
        SOURCE_CAPTURED,
    )


@pytest.mark.parametrize("catalog", [None, _catalog({"5": OR})], ids=["no catalog", "unknown block id"])
def test_keys_the_catalog_cannot_vouch_for_are_left_to_the_backfill(
    stores: dict[str, Any], snapshot: dict[str, Any], catalog: SimpleNamespace | None
) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, catalog)
    plan = {"elements": snapshot["elements"], "connections": snapshot["connections"]}

    resolved = asyncio.run(_change_backup_and_read(manager, plan))

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert SNAPSHOT_KEYS not in stored
    assert resolved is not None
    assert _ref_id(resolved, "4") == 5
    assert resolved[BLOCK_IDS_UNCHECKED]


async def _change_backup_and_read(manager: FunctionPlanBackupManager, plan: dict) -> dict[str, Any] | None:
    await manager.async_change_backup(1, plan, "Lights", "sort")
    return await manager.async_get_snapshot("change", 1, "Lights")


def test_backfill_fills_every_legacy_snapshot_once(stores: dict[str, Any], snapshot: dict[str, Any]) -> None:
    legacy = {k: v for k, v in snapshot.items() if k not in (SNAPSHOT_KEYS, SNAPSHOT_KEYS_SOURCE)}
    older = {**copy.deepcopy(legacy), "captured_at": "2026-08-01T08:00:00+00:00"}
    stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] = {"1": {"Lights": [copy.deepcopy(legacy), older]}}
    stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"] = {"1": {"Lights": [snapshot]}}
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, _catalog(FUB_BASE))

    async def run() -> tuple[dict[str, int], dict[str, int]]:
        return await manager.async_backfill_block_keys(), await manager.async_backfill_block_keys()

    first, second = asyncio.run(run())

    assert first == {SOURCE_BACKFILLED: 1, SOURCE_BACKFILLED_PLAUSIBLE: 1}
    assert second == {}
    auto = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert [snap[SNAPSHOT_KEYS] for snap in auto] == [{"23": NOT_KEY, "5": OR_KEY}] * 2


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


MISSING_BLOCK = {"element": "7", "ref_id": 23, "key": NOT_KEY, "reason": REASON_MISSING}
NO_KEY_BLOCK = {"element": "7", "ref_id": 23, "key": None, "reason": REASON_NO_KEY}
_NEEDS_OPT_IN = [
    {UNRESOLVED_BLOCKS: [NO_KEY_BLOCK]},
    {BLOCK_IDS_UNCHECKED: UNCHECKED_NO_KEYS},
    {BLOCK_IDS_UNCHECKED: UNCHECKED_NO_CATALOG},
    {SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED},
]


@pytest.mark.parametrize(
    ("snapshot_fields", "accept", "refused"),
    [
        ({UNRESOLVED_BLOCKS: [MISSING_BLOCK, NO_KEY_BLOCK]}, True, True),
        *((fields, False, True) for fields in _NEEDS_OPT_IN),
        *((fields, True, False) for fields in _NEEDS_OPT_IN),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_PLAUSIBLE, UNRESOLVED_BLOCKS: []}, False, False),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED}, False, False),
    ],
)
def test_restore_refuses_blocks_it_cannot_place(snapshot_fields: dict, accept: bool, refused: bool) -> None:
    error = backup_service._block_ids_restore_error({"elements": {}, **snapshot_fields}, accept)

    assert (error is not None) is refused
    if refused and not accept:
        assert "accept_unverified_blocks: true" in error


def test_a_missing_block_is_named_with_its_reason() -> None:
    error = backup_service._block_ids_restore_error({UNRESOLVED_BLOCKS: [MISSING_BLOCK]}, True)

    assert error is not None
    assert "element 7: block id 23 (not/d/d): no block of this kind exists" in error
    assert "accept_unverified_blocks" not in error  # no opt-in can place a block that is gone


def _run_restore(
    snapshot: dict[str, Any],
    *,
    logged_in: bool = False,
    live_version: str | None = None,
    live_name: str = "Kitch",
    fetch_error: Exception | None = None,
    page: dict[str, Any] | None = None,
    **data: Any,
) -> tuple[SimpleNamespace, MagicMock]:
    api = SimpleNamespace(login=AsyncMock(return_value=logged_in), comexio_version="11.0.2")

    async def fetch_config() -> dict[str, Any]:
        if fetch_error is not None:
            raise fetch_error
        if page is not None:
            return page
        api.comexio_version = live_version  # the fresh page carries today's firmware, not the last poll's
        return {"FubModules": {}, "Fubs": {"42": {"Id": 42, "Name": live_name}}}  # Comexio's shape: int Id

    api.get_raw_config = AsyncMock(side_effect=fetch_config)
    api.update_fub_cache_entry = MagicMock()
    call = SimpleNamespace(data={"confirm": True, **data})
    with (
        patch.object(backup_service, "_resolve_restore_params", AsyncMock(return_value=(42, "auto", 0, "Kitch"))),
        patch.object(backup_service, "_resolve_backup_identity", AsyncMock(return_value=("Kitch", None))),
        patch.object(backup_service, "_resolve_restore_snapshot", AsyncMock(return_value=snapshot)),
        patch.object(backup_service.persistent_notification, "async_create") as notify,
        patch.object(backup_service, "_restore_plan_as_copy", AsyncMock()) as as_copy,
        patch.object(backup_service, "_restore_plan_in_place", AsyncMock()) as in_place,
        patch.object(backup_service, "_restore_plan_as_new", AsyncMock()) as as_new,
        patch.object(backup_service, "_refresh_service_descriptions", AsyncMock()),
    ):
        coordinator = SimpleNamespace(server_id="iosrv1")
        asyncio.run(backup_service._run_function_plan_restore(MagicMock(), call, coordinator, api, plan_hash))
    api.restore_as_copy, api.restore_in_place, api.restore_as_new = as_copy, in_place, as_new
    return api, notify


def _notification_titles(notify: MagicMock) -> list[str]:
    return [c.kwargs.get("title") for c in notify.call_args_list]


def _restores_run(api: SimpleNamespace) -> int:
    """How many restore helpers (in place, as new, as copy) the restore awaited."""
    return api.restore_in_place.await_count + api.restore_as_new.await_count + api.restore_as_copy.await_count


@pytest.mark.parametrize(("logged_in", "warned"), [(True, True), (False, False)])
def test_a_restore_warns_about_other_firmware_only_once_it_proceeds(logged_in: bool, warned: bool) -> None:
    """(bn)(8): the warning comes after the login, so a restore that stops there does not announce it."""
    snapshot = {"plan_name": "Kitch", SNAPSHOT_COMEXIO_VERSION: "11.0.2"}

    api, notify = _run_restore(snapshot, logged_in=logged_in, live_version="11.1.4", as_copy=True, new_plan_name="Copy")

    assert (backup_service._TITLE_RESTORE_FIRMWARE in _notification_titles(notify)) is warned
    assert api.restore_as_copy.await_count == int(warned)  # the warning never replaces the restore


@pytest.mark.parametrize(
    ("live_name", "confirm", "warned"),
    [("Kitch", False, True), ("Other", False, False), ("Other", True, True)],
    ids=["no conflict", "conflict refused", "conflict confirmed"],
)
def test_an_in_place_restore_warns_only_once_the_conflict_check_lets_it_proceed(
    live_name: str, confirm: bool, warned: bool
) -> None:
    snapshot = {"plan_name": "Kitch", SNAPSHOT_COMEXIO_VERSION: "11.0.2"}

    api, notify = _run_restore(snapshot, logged_in=True, live_version="11.1.4", live_name=live_name, confirm=confirm)

    assert (backup_service._TITLE_RESTORE_FIRMWARE in _notification_titles(notify)) is warned
    assert _restores_run(api) == int(warned)


@pytest.mark.parametrize("data", [{}, {"as_copy": True, "new_plan_name": "Copy"}], ids=["in place", "as copy"])
def test_the_firmware_check_uses_the_freshly_fetched_version(data: dict[str, Any]) -> None:
    """The last poll saw 11.0.2 like the backup; the firmware changed since, which only a fresh fetch shows."""
    snapshot = {"plan_name": "Kitch", SNAPSHOT_COMEXIO_VERSION: "11.0.2"}

    api, notify = _run_restore(snapshot, logged_in=True, live_version="11.1.4", **data)

    assert backup_service._TITLE_RESTORE_FIRMWARE in _notification_titles(notify)
    assert _restores_run(api) == 1
    warning = next(c for c in notify.call_args_list if c.kwargs.get("title") == backup_service._TITLE_RESTORE_FIRMWARE)
    assert warning.kwargs["notification_id"] == "comexio_restore_firmware_iosrv1_42_Kitch"


_RETRY = "try again"
_USE_COPY = "'Restore as copy' still works"
# (page, fetch error, hint in the abort notice) for every plan list an in-place restore must not trust.
UNREAD_PLAN_LISTS = [
    pytest.param({}, None, _RETRY, id="page not readable"),
    pytest.param({"FubModules": {}}, None, _RETRY, id="no plan list"),
    pytest.param({"FubModules": {}, "Fubs": [{"Name": "Kitch"}]}, None, _USE_COPY, id="list not keyed by id"),
    pytest.param({"FubModules": {}, "Fubs": {"42": "not a plan"}}, None, _USE_COPY, id="malformed entry"),
    pytest.param({"FubModules": {}, "Fubs": {"42": None}}, None, _USE_COPY, id="null entry"),
    pytest.param({"FubModules": {}, "Fubs": {"42": {"Id": 42}}}, None, _USE_COPY, id="entry without name"),
    # 42 missing from a list that was not read cleanly — it may still run
    pytest.param({"FubModules": {}, "Fubs": {"7": "not a plan"}}, None, _USE_COPY, id="malformed list"),
    # the entry itself reads fine, but a list with non-plans is not trusted as a whole
    pytest.param(
        {"FubModules": {}, "Fubs": {"42": {"Name": "Kitch"}, "7": None}}, None, _USE_COPY, id="malformed sibling"
    ),
    # 42 missing next to a plan dict without a name — the list's shape is off, 42 may still run
    pytest.param({"FubModules": {}, "Fubs": {"7": {"Id": 7}}}, None, _USE_COPY, id="nameless sibling"),
    pytest.param(
        {"FubModules": {}, "Fubs": {"42": {"Name": "Kitch"}, "7": {"Id": 7}}},
        None,
        _USE_COPY,
        id="nameless sibling next to the plan",
    ),
    # 42 filed under another key: looking it up by id would miss a plan that still runs
    pytest.param(
        {"FubModules": {}, "Fubs": {"Kitch": {"Id": 42, "Name": "Kitch"}}}, None, _USE_COPY, id="plan not under its id"
    ),
    pytest.param(
        {"FubModules": {}, "Fubs": {"42": {"Id": 42, "Name": "Kitch"}, "7": {"Id": 8, "Name": "Other"}}},
        None,
        _USE_COPY,
        id="sibling under another plan's id",
    ),
    # an entry without Id is only trusted under a key that str(fub_id) can spell
    pytest.param({"FubModules": {}, "Fubs": {"Kitch": {"Name": "Kitch"}}}, None, _USE_COPY, id="plan under a name"),
    pytest.param({"FubModules": {}, "Fubs": {"042": {"Name": "Kitch"}}}, None, _USE_COPY, id="plan under a padded id"),
    pytest.param(None, aiohttp.ClientError("down"), _RETRY, id="unreachable"),
    # no aiohttp cause: _raise_transport_error raises it as is
    pytest.param(None, ComexioConnectionError("down"), _RETRY, id="unreachable without cause"),
]


@pytest.mark.parametrize(("page", "fetch_error", "hint"), UNREAD_PLAN_LISTS)
def test_an_unread_plan_list_stops_an_in_place_restore(
    page: dict[str, Any] | None, fetch_error: Exception | None, hint: str
) -> None:
    """An unread list must not look like a deleted plan: confirm would rebuild a plan that still runs."""
    api, notify = _run_restore({"plan_name": "Kitch"}, logged_in=True, page=page, fetch_error=fetch_error)

    api.restore_as_new.assert_not_awaited()
    api.restore_in_place.assert_not_awaited()
    message = notify.call_args.args[1]
    assert "Restore of 'Kitch' aborted" in message
    assert "Nothing was changed" in message
    assert hint in message


@pytest.mark.parametrize(
    "fubs", [{}, {"7": {"Id": 7, "Name": "Other"}}], ids=["empty list (PHP's [] decoded to {})", "other plans only"]
)
def test_a_read_plan_list_without_the_plan_means_it_was_deleted(fubs: dict[str, Any]) -> None:
    """A cleanly read list without the plan: it is gone, rebuilt as new."""
    api, _notify = _run_restore({"plan_name": "Kitch"}, logged_in=True, page={"FubModules": {}, "Fubs": fubs})

    api.restore_as_new.assert_awaited_once()
    assert api.restore_as_new.await_args.kwargs["old_id_still_live"] is False
    api.restore_in_place.assert_not_awaited()


def test_a_plan_among_others_is_restored_in_place() -> None:
    # one entry with Comexio's int Id, one without an Id (filed by its key)
    page = {"FubModules": {}, "Fubs": {"7": {"Id": 7, "Name": "Other"}, "42": {"Name": "Kitch"}}}

    api, _notify = _run_restore({"plan_name": "Kitch"}, logged_in=True, page=page)

    api.restore_in_place.assert_awaited_once()
    api.restore_as_new.assert_not_awaited()


@pytest.mark.parametrize(("page", "fetch_error", "hint"), UNREAD_PLAN_LISTS)
def test_an_unread_plan_list_does_not_stop_a_copy(
    page: dict[str, Any] | None, fetch_error: Exception | None, hint: str
) -> None:
    """The copy only fetches for the firmware version; it reports Comexio errors itself."""
    snapshot = {"plan_name": "Kitch", SNAPSHOT_COMEXIO_VERSION: "11.0.2"}

    api, _notify = _run_restore(
        snapshot, logged_in=True, page=page, fetch_error=fetch_error, as_copy=True, new_plan_name="Copy"
    )

    api.restore_as_copy.assert_awaited_once()


def test_a_refused_restore_does_not_warn_about_firmware() -> None:
    snapshot = {"plan_name": "Kitch", SNAPSHOT_COMEXIO_VERSION: "11.0.2", UNRESOLVED_BLOCKS: [MISSING_BLOCK]}

    _api, notify = _run_restore(snapshot, logged_in=True, live_version="11.1.4")

    assert backup_service._TITLE_RESTORE_FIRMWARE not in _notification_titles(notify)


@pytest.mark.parametrize("data", [{}, {"as_copy": True, "new_plan_name": "Copy"}], ids=["in place", "as copy"])
def test_a_refused_restore_stops_before_touching_comexio(data: dict[str, Any]) -> None:
    api, notify = _run_restore({"plan_name": "Kitch", UNRESOLVED_BLOCKS: [MISSING_BLOCK]}, **data)

    api.login.assert_not_awaited()
    assert "element 7: block id 23" in notify.call_args.args[1]


@pytest.mark.parametrize(("accept", "proceeds"), [(False, False), (True, True)])
def test_the_service_reads_the_opt_in(accept: bool, proceeds: bool) -> None:
    unverified = {"plan_name": "Kitch", SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED}

    api, _notify = _run_restore(unverified, accept_unverified_blocks=accept)

    # login is mocked to fail, so a restore that passed the block check stops right there.
    assert api.login.await_count == int(proceeds)


def test_an_unverified_restore_names_the_blocks() -> None:
    unverified = {
        SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED,
        UNVERIFIED_BLOCK_PROBLEMS: ["element 7: no wire to check the block against"],
    }

    error = backup_service._block_ids_restore_error(unverified, False)

    assert error is not None
    assert "- element 7: no wire to check the block against" in error
    assert "accept_unverified_blocks: true" in error


# ---------------------------------------------------------------------------
# block_check: what the plan card's restore dialog explains
# ---------------------------------------------------------------------------


def test_an_unverified_snapshot_is_read_with_its_problems(snapshot: dict[str, Any]) -> None:
    del snapshot["connections"]["13"]  # the Nicht's only wire
    snapshot[SNAPSHOT_KEYS_SOURCE] = SOURCE_BACKFILLED_UNVERIFIED

    resolved = resolve_block_ids(snapshot, FUB_BASE)

    check = block_check(resolved)
    assert check is not None
    assert check["status"] == CHECK_UNVERIFIED
    assert "element 7: no wire to check the block against" in check["problems"]
    assert UNVERIFIED_BLOCK_PROBLEMS not in snapshot  # the stored snapshot stays untouched
    assert resolved["hash"] == snapshot["hash"]  # no id moved


def test_an_unverified_snapshot_is_checked_with_todays_ids(snapshot: dict[str, Any]) -> None:
    snapshot[SNAPSHOT_KEYS_SOURCE] = SOURCE_BACKFILLED_UNVERIFIED
    snapshot["connections"]["90"] = _wire((2, 0), (7, 1))  # one input too many for the Nicht

    resolved = resolve_block_ids(snapshot, SHIFTED)

    assert _ref_id(resolved, "7") == 24
    assert resolved[UNVERIFIED_BLOCK_PROBLEMS] == implausible_block_wiring(
        resolved["elements"], snapshot["connections"], SHIFTED
    )
    assert any("input 1 beyond the not block's inputs" in p for p in resolved[UNVERIFIED_BLOCK_PROBLEMS])
    assert resolved["hash"] == plan_hash(resolved)


@pytest.mark.parametrize(
    ("snapshot_fields", "expected"),
    [
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED}, None),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED}, None),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_PLAUSIBLE}, {"status": CHECK_PLAUSIBLE, "problems": []}),
        (
            {UNRESOLVED_BLOCKS: [MISSING_BLOCK, NO_KEY_BLOCK]},
            {
                "status": CHECK_UNRESOLVED,
                "problems": [
                    "element 7: block id 23 (not/d/d): no block of this kind exists on Comexio today "
                    "(app or firmware removed it?)"
                ],
            },
        ),
        (
            {UNRESOLVED_BLOCKS: [NO_KEY_BLOCK]},
            {
                "status": CHECK_UNVERIFIED,
                "problems": [
                    "element 7: block id 23 (no key): the block was unknown to the block catalog when the "
                    "backup was taken"
                ],
            },
        ),
        (
            {BLOCK_IDS_UNCHECKED: UNCHECKED_NO_CATALOG},
            {
                "status": CHECK_UNVERIFIED,
                "problems": ["Comexio's block catalog could not be verified in the last poll"],
            },
        ),
        (
            # A plausible backup is unverified while it cannot be checked at all.
            {SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_PLAUSIBLE, BLOCK_IDS_UNCHECKED: UNCHECKED_NO_KEYS},
            {
                "status": CHECK_UNVERIFIED,
                "problems": ["the block ids of this backup have not been checked yet (wait for the next backup cycle)"],
            },
        ),
        (
            {UNRESOLVED_BLOCKS: [{**MISSING_BLOCK, "reason": REASON_AMBIGUOUS}]},
            {
                "status": CHECK_UNRESOLVED,
                "problems": ["element 7: block id 23 (not/d/d): several blocks on Comexio today match it"],
            },
        ),
        (
            # Today's catalog finds nothing wrong any more: the backfill verdict still needs a reason.
            {SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED, UNVERIFIED_BLOCK_PROBLEMS: []},
            {
                "status": CHECK_UNVERIFIED,
                "problems": ["the wiring of this backup did not fit the blocks when its block ids were derived"],
            },
        ),
    ],
    ids=[
        "captured",
        "backfilled",
        "plausible",
        "unresolved",
        "no key",
        "no catalog",
        "plausible but unchecked",
        "ambiguous",
        "unverified without problems",
    ],
)
def test_block_check_tells_how_far_the_block_ids_can_be_trusted(
    snapshot_fields: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    assert block_check({"elements": {}, **snapshot_fields}) == expected


def test_the_backup_selector_reads_the_check_from_the_caches(stores: dict[str, Any], snapshot: dict[str, Any]) -> None:
    unverified = {**copy.deepcopy(snapshot), SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED}
    del unverified["connections"]["13"]
    stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] = {"1": {"Lights": [unverified, snapshot]}}
    catalog = SimpleNamespace(fub_base_sync=MagicMock(return_value=FUB_BASE))
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, catalog)
    asyncio.run(manager._async_ensure_loaded())

    check = manager.block_check_sync("auto", 1, "Lights", 0)

    assert check is not None
    assert check["status"] == CHECK_UNVERIFIED
    assert manager.block_check_sync("auto", 1, "Lights", 1) is None  # captured: exact
    assert manager.block_check_sync("auto", 1, "Lights", 2) is None  # no such slot
    catalog.fub_base_sync.return_value = {}  # no verified catalog right now
    assert manager.block_check_sync("auto", 1, "Lights", 1) == {
        "status": CHECK_UNVERIFIED,
        "problems": ["Comexio's block catalog could not be verified in the last poll"],
    }


def test_the_backup_selector_check_is_computed_once_per_state(stores: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """The selector's attributes are read on every webhook push: the check must not be redone each time."""
    legacy = {k: v for k, v in snapshot.items() if k not in (SNAPSHOT_KEYS, SNAPSHOT_KEYS_SOURCE)}
    stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] = {"1": {"Lights": [legacy]}}
    catalog = SimpleNamespace(fub_base_sync=MagicMock(return_value=FUB_BASE))
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, catalog)
    asyncio.run(manager._async_ensure_loaded())
    stored = manager._stored_snapshot("auto", 1, "Lights", 0)
    assert stored is not None

    with patch.object(backup_module, "resolve_block_ids", wraps=resolve_block_ids) as resolve:
        before_backfill = [manager.block_check_sync("auto", 1, "Lights", 0) for _ in range(3)]
        backfill_block_keys(stored, FUB_BASE, CHANGED_AT)  # the backup cycle fills the keys in place
        after_backfill = manager.block_check_sync("auto", 1, "Lights", 0)
        catalog.fub_base_sync.return_value = dict(FUB_BASE)  # a catalog update replaces the dict
        manager.block_check_sync("auto", 1, "Lights", 0)

    assert resolve.call_count == 3
    assert before_backfill[0] is not None
    assert before_backfill == [before_backfill[0]] * 3
    assert before_backfill[0]["status"] == CHECK_UNVERIFIED  # not backfilled yet
    assert after_backfill is None  # captured after the last id change: exact


def test_the_backup_selector_check_is_kept_without_a_catalog(stores: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """No verified catalog is a lasting state too: the empty fallback must not defeat the memo."""
    stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] = {"1": {"Lights": [snapshot]}}
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    asyncio.run(manager._async_ensure_loaded())

    with patch.object(backup_module, "resolve_block_ids", wraps=resolve_block_ids) as resolve:
        checks = [manager.block_check_sync("auto", 1, "Lights", 0) for _ in range(3)]

    assert resolve.call_count == 1
    assert checks == [checks[0]] * 3


@pytest.mark.parametrize(
    ("snapshot_fields", "accept", "warning"),
    [
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_UNVERIFIED}, True, "restoring unverified block ids (accepted)"),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_BACKFILLED_PLAUSIBLE}, False, "block ids derived from today's catalog"),
        ({SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED}, True, None),
    ],
    ids=["accepted unverified", "plausible", "captured"],
)
def test_a_restore_on_inexact_block_ids_leaves_a_warning(
    caplog: pytest.LogCaptureFixture, snapshot_fields: dict[str, Any], accept: bool, warning: str | None
) -> None:
    with caplog.at_level("WARNING", logger=backup_service.__name__):
        backup_service._log_block_ids_trust({"elements": {}, **snapshot_fields}, "Kitch", accept)

    messages = [record.getMessage() for record in caplog.records]
    if warning is None:
        assert messages == []
    else:
        assert len(messages) == 1
        assert warning in messages[0]


AUTO_ENTRY = {"kind": "auto", "slot": 1, "captured_at": "2026-09-10T08:00:00+00:00"}
CHANGE_ENTRY = {"kind": "change", "slot": 1, "captured_at": "2026-09-11T08:00:00+00:00", "operation": "sort"}


def _backup_selector(selected: str, orphan_view: bool, block_check_sync: MagicMock) -> Any:
    entity = select_module.ComexioPlanBackupSelectEntity.__new__(select_module.ComexioPlanBackupSelectEntity)
    entity._selected = selected
    entity._last_orphan = None
    entity._orphan_plans = []
    orphan_row = ("Old (ID 9) — 1 backups", {"fub_id": 9, "plan_name": "Old", "kind": "change", "slot": 0})
    entity.coordinator = SimpleNamespace(
        _restore_lock=asyncio.Lock(),
        orphaned_plans_view_active=lambda: orphan_view,
        orphaned_backup_options=lambda: [orphan_row] if selected != "none" else [],
        orphaned_backup_choice=lambda label: orphan_row[1] if label == orphan_row[0] else None,
        get_active_function_plan_fub_id=lambda: 1,
        api=SimpleNamespace(fub_data={"1": {"Name": "Lights"}}),
        function_plan_backup=SimpleNamespace(
            plan_backups_for_identity_sync=lambda _fub_id, _name: [AUTO_ENTRY, CHANGE_ENTRY],
            block_check_sync=block_check_sync,
        ),
    )
    return entity


@pytest.mark.parametrize(
    ("orphan_view", "pick_backup", "expected_call"),
    [
        (False, True, ("change", 1, "Lights", 1)),
        (False, False, None),  # Live: nothing to restore
        (True, True, ("change", 9, "Old", 0)),
        (True, False, None),  # deleted-plans view without a chosen row
    ],
    ids=["plan backup", "live", "deleted plan", "deleted plans view, nothing chosen"],
)
def test_the_backup_selector_shows_the_chosen_backups_check(
    orphan_view: bool, pick_backup: bool, expected_call: tuple | None
) -> None:
    check = {"status": CHECK_UNVERIFIED, "problems": []}
    block_check_sync = MagicMock(return_value=check)
    label = "Old (ID 9) — 1 backups" if orphan_view else backup_module.format_backup_label(CHANGE_ENTRY)
    unpicked = "none" if orphan_view else select_module.LIVE_BACKUP_OPTION
    entity = _backup_selector(label if pick_backup else unpicked, orphan_view, block_check_sync)

    attrs = entity.extra_state_attributes

    if expected_call is None:
        assert attrs["block_check"] is None
        block_check_sync.assert_not_called()
    else:
        assert attrs["block_check"] == check
        block_check_sync.assert_called_once_with(*expected_call)
