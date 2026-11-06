"""The plan preview renders a block's extended view from the right block settings (#146 follow-up)."""

from http import HTTPStatus
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import CONF_SERVER_ID, DOMAIN

# A block with an auto-hidden input; element 408 switches its extended view on ("autohide": "1").
CATALOG = {"fub_base": {"77": {"in": ["a", "b"], "in_hide": ["b"], "out": ["q"], "out_hide": []}}}
ELEMENTS = {"408": {"reference": {"type": "5", "ref_id": "77"}, "position_x": 10, "position_y": 10}}
EXPANDED = {"408": {"autohide": "1"}}


async def _rendered_ref_id(hass: HomeAssistant, entry: MockConfigEntry, source: str, block_settings: Any) -> str:
    """The catalog key element 408 was rendered with for one preview call."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    with (
        patch.object(coordinator.function_plan_catalog, "async_get_catalog", AsyncMock(return_value=CATALOG)),
        patch("custom_components.comexio.coordinator.render_plan_svg", return_value="<svg/>") as render,
    ):
        await coordinator.async_generate_plan_preview(
            1, "Test1", ELEMENTS, {}, source, label_metadata=None, block_settings=block_settings
        )
    render_elements, _connections, render_catalog = render.call_args.args[:3]
    ref_id = render_elements["408"]["reference"]["ref_id"]
    assert ref_id in render_catalog["fub_base"]
    return ref_id


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_a_live_preview_uses_the_polled_block_settings(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    mock_comexio_api[0].block_settings = EXPANDED

    assert await _rendered_ref_id(hass, loaded_entry, "live", block_settings=None) == "77:expanded"


async def test_a_snapshot_preview_uses_the_stored_block_settings(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The live table switches the extended view on, the backup's does not: the backup wins."""
    mock_comexio_api[0].block_settings = EXPANDED

    assert await _rendered_ref_id(hass, loaded_entry, "snapshot:auto:0", block_settings={}) == "77"
    assert await _rendered_ref_id(hass, loaded_entry, "snapshot:auto:1", block_settings=EXPANDED) == "77:expanded"


PREVIEW_ATTRIBUTES = ("fub_id", "plan_name", "source", "captured_at")


def _preview_image_attributes(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    unique_id = f"comexio_{entry.data[CONF_SERVER_ID]}_plan_preview_image"
    entity_id = er.async_get(hass).async_get_entity_id("image", DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return {key: state.attributes.get(key) for key in PREVIEW_ATTRIBUTES}


async def test_the_preview_image_names_the_shown_plan(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """The plan card compares these attributes with the orphaned-plans choice before a delete."""
    await _rendered_ref_id(hass, loaded_entry, "snapshot:change:2", block_settings={})

    assert _preview_image_attributes(hass, loaded_entry) == {
        "fub_id": 1,
        "plan_name": "Test1",
        "source": "snapshot:change:2",
        "captured_at": None,
    }


async def test_an_orphan_render_names_its_snapshot_by_capture_time(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Slots shift after a delete; the card matches an orphan render by its snapshot's captured_at."""
    coordinator = hass.data[DOMAIN][loaded_entry.entry_id]
    await coordinator._async_publish_plan_preview(
        3, "Garage", "orphan:auto:0", "<svg/>", captured_at="2026-01-02T03:04:05+00:00"
    )
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert _preview_image_attributes(hass, loaded_entry) == {
        "fub_id": 3,
        "plan_name": "Garage",
        "source": "orphan:auto:0",
        "captured_at": "2026-01-02T03:04:05+00:00",
    }
    # The served SVG names the same render, so the card can tell it from a newer one (stamp_preview_identity).
    assert coordinator.last_plan_preview_svg == (
        '<svg data-comexio-source="orphan:auto:0" data-comexio-captured-at="2026-01-02T03:04:05+00:00"/>'
    )


async def test_the_backup_selector_shares_its_device_with_the_only_preview_image(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A trigger-only plan card finds the image as the comexio image entity on the backup selector's device."""
    registry = er.async_get(hass)
    server_id = loaded_entry.data[CONF_SERVER_ID]
    backup_selector_id = registry.async_get_entity_id("select", DOMAIN, f"comexio_{server_id}_plan_backup_selector")
    assert backup_selector_id is not None
    backup_selector = registry.async_get(backup_selector_id)
    assert backup_selector is not None
    images = [
        entry
        for entry in er.async_entries_for_device(registry, backup_selector.device_id)
        if entry.domain == "image" and entry.platform == DOMAIN
    ]
    assert [(image.unique_id, image.translation_key) for image in images] == [
        (f"comexio_{server_id}_plan_preview_image", "plan_preview")
    ]


async def test_a_restored_preview_without_a_render_names_no_plan(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A last_plan_preview without an SVG names no image: no identity."""
    coordinator = hass.data[DOMAIN][loaded_entry.entry_id]
    coordinator.last_plan_preview = {
        "fub_id": 1,
        "plan_name": "Test1",
        "source": "orphan:auto:0",
        "captured_at": "2026-01-02T03:04:05+00:00",
    }
    assert coordinator.last_plan_preview_svg is None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert _preview_image_attributes(hass, loaded_entry) == dict.fromkeys(PREVIEW_ATTRIBUTES)


STORED_PREVIEW = {
    "fub_id": 3,
    "plan_name": "Garage",
    "source": "live",
    "captured_at": None,
    "generated_at": "2026-01-02T03:04:05+00:00",
}


def _preview_store_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}_plan_preview_{entry.entry_id}"


async def _write_public_file(hass: HomeAssistant, entry: MockConfigEntry) -> pathlib.Path:
    """The preview copy older releases left in config/www, served under /local/ without authentication."""
    public_file = pathlib.Path(hass.config.path("www", f"comexio_{entry.data[CONF_SERVER_ID]}_plan_preview.svg"))

    def write() -> None:
        public_file.parent.mkdir(parents=True, exist_ok=True)
        public_file.write_text("<svg/>", encoding="utf-8")

    await hass.async_add_executor_job(write)
    return public_file


@pytest.mark.logged_exception  # the coordinator logs the failed first refresh with its traceback
async def test_a_setup_that_cannot_reach_comexio_still_deletes_the_public_file(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], api_returns: dict
) -> None:
    """Review (Sourcery, #272): while Comexio stays unreachable the setup retries, the public copy must not stay."""
    public_file = await _write_public_file(hass, mock_config_entry)
    api_returns["get_raw_config"] = OSError("connection reset")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not await hass.async_add_executor_job(public_file.exists)


async def test_a_restart_restores_the_preview_from_the_store_and_deletes_the_public_file(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    hass_storage: dict[str, Any],
) -> None:
    """Loaded before the platforms: image and info sensor name the last preview right after the setup."""
    server_id = mock_config_entry.data[CONF_SERVER_ID]
    public_file = await _write_public_file(hass, mock_config_entry)
    hass_storage[_preview_store_key(mock_config_entry)] = {
        "version": 1,
        "key": _preview_store_key(mock_config_entry),
        "data": {"svg": "<svg/>", "preview": STORED_PREVIEW},
    }
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _preview_image_attributes(hass, mock_config_entry) == {
        "fub_id": 3,
        "plan_name": "Garage",
        "source": "live",
        "captured_at": None,
    }
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"comexio_{server_id}_plan_preview_sensor")
    assert sensor_id is not None
    sensor_state = hass.states.get(sensor_id)
    assert sensor_state is not None
    assert sensor_state.state == "Garage"
    assert "entity_picture" not in sensor_state.attributes
    assert not await hass.async_add_executor_job(public_file.exists)


async def test_the_restored_preview_is_only_served_with_authentication(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    hass_storage: dict[str, Any],
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The point of (dh): the URL the service returns serves the SVG to a logged-in client only."""
    hass_storage[_preview_store_key(mock_config_entry)] = {
        "version": 1,
        "key": _preview_store_key(mock_config_entry),
        "data": {"svg": "<svg/>", "preview": STORED_PREVIEW},
    }
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    url = hass.data[DOMAIN][mock_config_entry.entry_id].plan_preview_image_url()
    assert url is not None
    image_state = hass.states.get(url.removeprefix("/api/image_proxy/"))
    assert image_state is not None
    assert image_state.state == STORED_PREVIEW["generated_at"]

    response = await (await hass_client()).get(url)
    assert response.status == HTTPStatus.OK
    assert await response.read() == b"<svg/>"
    assert (await (await hass_client_no_auth()).get(url)).status == HTTPStatus.FORBIDDEN


async def test_a_reload_keeps_a_preview_whose_delayed_save_was_still_pending(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    await _rendered_ref_id(hass, loaded_entry, "snapshot:change:2", block_settings={})
    assert _preview_store_key(loaded_entry) not in hass_storage

    assert await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert _preview_image_attributes(hass, loaded_entry)["source"] == "snapshot:change:2"


async def test_removing_the_entry_removes_its_stored_preview(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    await _rendered_ref_id(hass, loaded_entry, "live", block_settings=None)
    await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert _preview_store_key(loaded_entry) in hass_storage

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert _preview_store_key(loaded_entry) not in hass_storage


async def test_a_failing_saved_config_removal_still_removes_the_stored_preview(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Review (Sourcery, #272): the preview names the user's wiring, it must go even if the other store stays."""
    await _rendered_ref_id(hass, loaded_entry, "live", block_settings=None)
    await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert _preview_store_key(loaded_entry) in hass_storage
    saved_config = MagicMock(async_remove=AsyncMock(side_effect=OSError("read-only")))

    with patch("custom_components.comexio.saved_config_store", return_value=saved_config):
        await hass.config_entries.async_remove(loaded_entry.entry_id)
        await hass.async_block_till_done()

    saved_config.async_remove.assert_awaited_once()
    assert _preview_store_key(loaded_entry) not in hass_storage
