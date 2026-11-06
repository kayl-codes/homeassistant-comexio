"""(dh) The plan preview is kept in .storage, never in config/www, which HA serves without authentication."""

import asyncio
from collections.abc import Callable
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import coordinator as coordinator_module, plan_preview
from custom_components.comexio.const import DOMAIN, PLAN_PREVIEW_SAVE_DELAY_SEC
from custom_components.comexio.coordinator import (
    ComexioCoordinator,
    legacy_plan_preview_path,
    plan_preview_store,
    stored_plan_preview,
)
from custom_components.comexio.image import ComexioPlanPreviewImage
from custom_components.comexio.sensor import ComexioPlanPreviewSensor

SERVER_ID = "cx1"
GENERATED_AT = "2026-10-03T05:30:00+00:00"
PREVIEW = {"fub_id": 19, "plan_name": "Test1", "source": "live", "captured_at": None, "generated_at": GENERATED_AT}
LEGACY_NAME = f"comexio_{SERVER_ID}_plan_preview.svg"
STORED = {"svg": "<svg x='1'/>", "preview": PREVIEW}
COMPONENT_DIR = pathlib.Path(coordinator_module.__file__).parent


async def _run_in_executor(func: Callable[..., Any], *args: Any) -> Any:
    return func(*args)


def _coordinator(config_dir: pathlib.Path, stored: Any = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER_ID
    coordinator.hass = MagicMock()
    coordinator.hass.config.path = lambda *parts: str(config_dir.joinpath(*parts))
    coordinator.hass.async_add_executor_job = _run_in_executor
    coordinator._plan_preview_store = MagicMock(async_load=AsyncMock(return_value=stored), async_save=AsyncMock())
    coordinator._plan_preview_rendered = False
    coordinator._plan_preview_store_closed = False
    coordinator.last_plan_preview = None
    coordinator.last_plan_preview_svg = None
    return coordinator


def _legacy_file(config_dir: pathlib.Path) -> pathlib.Path:
    legacy = config_dir / "www" / LEGACY_NAME
    legacy.parent.mkdir()
    legacy.write_text("<svg/>", encoding="utf-8")
    return legacy


def test_the_store_is_keyed_by_the_entry() -> None:
    assert plan_preview_store(MagicMock(), "entry123").key == f"{DOMAIN}_plan_preview_entry123"


def test_the_legacy_file_is_the_one_older_releases_wrote_to_www(tmp_path: pathlib.Path) -> None:
    hass = MagicMock()
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))

    assert legacy_plan_preview_path(hass, SERVER_ID) == tmp_path / "www" / LEGACY_NAME


def test_a_stored_preview_is_restored_as_svg_and_metadata() -> None:
    assert stored_plan_preview(STORED) == ("<svg x='1'/>", PREVIEW)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        {},
        {"svg": None, "preview": PREVIEW},
        {"svg": "<svg/>"},
        {"svg": "<svg/>", "preview": None},
        {"svg": "<svg/>", "preview": {**PREVIEW, "generated_at": None}},
    ],
    ids=["never-saved", "not-a-dict", "empty", "no-svg", "no-preview", "preview-none", "no-timestamp"],
)
def test_nothing_usable_is_not_restored(stored: Any) -> None:
    assert stored_plan_preview(stored) is None


def _remove_legacy(coordinator: ComexioCoordinator) -> None:
    asyncio.run(coordinator_module.async_remove_legacy_plan_preview(coordinator.hass, SERVER_ID))


def test_loading_restores_the_preview_and_leaves_the_www_folder_alone(tmp_path: pathlib.Path) -> None:
    """The www copy is not imported (the plan card renders anew when it opens): its delete runs before."""
    legacy = _legacy_file(tmp_path)
    coordinator = _coordinator(tmp_path, stored=STORED)

    asyncio.run(coordinator.async_load_plan_preview())

    assert coordinator.last_plan_preview_svg == "<svg x='1'/>"
    assert coordinator.last_plan_preview == PREVIEW
    assert legacy.read_text(encoding="utf-8") == "<svg/>"


def test_the_public_www_copy_is_deleted(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    legacy = _legacy_file(tmp_path)
    coordinator = _coordinator(tmp_path)

    with caplog.at_level("INFO", logger=coordinator_module.__name__):
        _remove_legacy(coordinator)

    assert not legacy.exists()
    assert "Deleted the old plan preview" in caplog.text


def test_an_unreadable_store_does_not_fail_the_setup(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """A preview is not worth a failed setup."""
    coordinator = _coordinator(tmp_path)
    coordinator._plan_preview_store.async_load = AsyncMock(side_effect=HomeAssistantError("read error"))

    with caplog.at_level("WARNING", logger=coordinator_module.__name__):
        asyncio.run(coordinator.async_load_plan_preview())

    assert "Could not read the last plan preview" in caplog.text
    assert coordinator.last_plan_preview is None


def test_a_store_without_a_usable_preview_is_logged(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(tmp_path, stored={"svg": "<svg/>"})

    with caplog.at_level("WARNING", logger=coordinator_module.__name__):
        asyncio.run(coordinator.async_load_plan_preview())

    assert "Ignoring the stored plan preview" in caplog.text
    assert coordinator.last_plan_preview is None


def test_a_www_copy_that_cannot_be_deleted_is_logged(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _coordinator(tmp_path)

    def refuse(_path: pathlib.Path) -> bool:
        raise PermissionError("read-only")

    monkeypatch.setattr(coordinator_module, "_remove_legacy_preview_file", refuse)
    with caplog.at_level("WARNING", logger=coordinator_module.__name__):
        _remove_legacy(coordinator)  # does not raise: the setup goes on

    assert "delete it by hand" in caplog.text


def test_no_www_copy_is_nothing_to_report(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(tmp_path)

    with caplog.at_level("INFO", logger=coordinator_module.__name__):
        _remove_legacy(coordinator)
        asyncio.run(coordinator.async_load_plan_preview())

    assert caplog.text == ""


def test_a_published_preview_is_saved_delayed_with_the_newest_render(tmp_path: pathlib.Path) -> None:
    """A live preview re-renders every few seconds: the save reads the data when it runs, not per render."""
    coordinator = _coordinator(tmp_path)

    asyncio.run(coordinator._async_publish_plan_preview(1, "Old", "live", "<svg/>"))
    asyncio.run(coordinator._async_publish_plan_preview(2, "New", "live", "<svg/>"))

    data_func, delay = coordinator._plan_preview_store.async_delay_save.call_args.args
    assert delay == PLAN_PREVIEW_SAVE_DELAY_SEC
    data = data_func()
    assert data["svg"] == coordinator.last_plan_preview_svg == '<svg data-comexio-source="live"/>'
    assert data["preview"] is coordinator.last_plan_preview
    assert data["preview"]["plan_name"] == "New"
    assert not (tmp_path / "www").exists()  # nothing lands where /local/ would serve it


def test_shutdown_saves_a_pending_preview_once(tmp_path: pathlib.Path) -> None:
    """The coordinator after a reload loads the store: a save still pending must not be lost."""
    coordinator = _coordinator(tmp_path)
    asyncio.run(coordinator._async_publish_plan_preview(1, "Plan", "live", "<svg/>"))

    asyncio.run(coordinator._async_close_plan_preview_store())
    asyncio.run(coordinator._async_close_plan_preview_store())

    coordinator._plan_preview_store.async_save.assert_awaited_once()
    saved = coordinator._plan_preview_store.async_save.await_args.args[0]
    assert saved == {"svg": coordinator.last_plan_preview_svg, "preview": coordinator.last_plan_preview}


def test_shutdown_saves_again_after_the_delayed_save_ran(tmp_path: pathlib.Path) -> None:
    """Review (Sourcery, #272): HA's Store only logs a failed delayed write, the shutdown is its retry."""
    coordinator = _coordinator(tmp_path)
    asyncio.run(coordinator._async_publish_plan_preview(1, "Plan", "live", "<svg/>"))
    data_func, _delay = coordinator._plan_preview_store.async_delay_save.call_args.args
    data_func()  # the delayed save ran (its write may have failed, HA does not tell)

    asyncio.run(coordinator._async_close_plan_preview_store())

    coordinator._plan_preview_store.async_save.assert_awaited_once()
    assert coordinator._plan_preview_store.async_save.await_args.args[0]["preview"]["plan_name"] == "Plan"


def test_a_render_during_an_unload_that_may_still_be_refused_keeps_its_timer(tmp_path: pathlib.Path) -> None:
    """A refused unload runs no shutdown: without the timer this preview would never be saved."""
    coordinator = _coordinator(tmp_path)
    coordinator.unloading = True

    asyncio.run(coordinator._async_publish_plan_preview(1, "Plan", "live", "<svg/>"))

    coordinator._plan_preview_store.async_delay_save.assert_called_once()


def test_a_render_finishing_after_the_shutdown_saves_nothing(tmp_path: pathlib.Path) -> None:
    """A delayed save outliving the shutdown could overwrite a newer preview of the next coordinator."""
    coordinator = _coordinator(tmp_path)
    asyncio.run(coordinator._async_close_plan_preview_store())

    asyncio.run(coordinator._async_publish_plan_preview(1, "Plan", "live", "<svg/>"))
    asyncio.run(coordinator._async_close_plan_preview_store())

    coordinator._plan_preview_store.async_delay_save.assert_not_called()
    coordinator._plan_preview_store.async_save.assert_not_awaited()


def test_shutdown_without_a_new_render_saves_nothing(tmp_path: pathlib.Path) -> None:
    coordinator = _coordinator(tmp_path)

    asyncio.run(coordinator._async_close_plan_preview_store())

    coordinator._plan_preview_store.async_save.assert_not_awaited()


def test_the_image_proxy_url_names_the_registered_preview_image(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(pathlib.Path())
    lookups: list[tuple[str, str, str]] = []

    def entity_id(domain: str, platform: str, unique_id: str) -> str | None:
        lookups.append((domain, platform, unique_id))
        return "image.cx1_plan_preview"

    registry = MagicMock(async_get_entity_id=entity_id)
    registry.async_get.return_value = MagicMock(disabled=False)
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: registry)

    assert coordinator.plan_preview_image_url() == "/api/image_proxy/image.cx1_plan_preview"
    assert lookups == [("image", DOMAIN, f"comexio_{SERVER_ID}_plan_preview_image")]


def test_no_image_proxy_url_without_a_registered_preview_image(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(pathlib.Path())
    registry = MagicMock(async_get_entity_id=MagicMock(return_value=None))
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: registry)

    assert coordinator.plan_preview_image_url() is None


@pytest.mark.parametrize(("svg", "served"), [("<svg/>", b"<svg/>"), (None, None)], ids=["restored", "nothing-yet"])
def test_the_image_serves_the_coordinators_svg_only(svg: str | None, served: bytes | None) -> None:
    image = ComexioPlanPreviewImage.__new__(ComexioPlanPreviewImage)
    image.coordinator = MagicMock(last_plan_preview_svg=svg)

    assert asyncio.run(image.async_image()) == served


def test_the_info_sensor_offers_no_picture_of_its_own() -> None:
    """Frenck's finding: its /local/ entity_picture exposed the preview. The picture is the image entity's."""
    sensor = ComexioPlanPreviewSensor.__new__(ComexioPlanPreviewSensor)
    sensor.coordinator = MagicMock(last_plan_preview=PREVIEW)

    assert sensor.entity_picture is None


@pytest.mark.parametrize("entity", [MagicMock(disabled=True), None], ids=["disabled", "entry-gone"])
def test_no_image_proxy_url_for_an_image_nobody_can_load(monkeypatch: pytest.MonkeyPatch, entity: Any) -> None:
    """The proxy answers 404 for it: a URL would promise an image nobody can load."""
    coordinator = _coordinator(pathlib.Path())
    registry = MagicMock(async_get_entity_id=MagicMock(return_value="image.cx1_plan_preview"))
    registry.async_get.return_value = entity
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: registry)

    assert coordinator.plan_preview_image_url() is None


def test_only_the_legacy_cleanup_still_names_the_www_folder() -> None:
    """Anything under config/www is served without authentication: no new code may write a preview there."""
    users = [
        path.relative_to(COMPONENT_DIR).as_posix()
        for path in COMPONENT_DIR.rglob("*.py")
        if '"www"' in (source := path.read_text(encoding="utf-8")) or "/local/comexio" in source
    ]

    assert users == ["coordinator.py"]


def test_an_orphan_preview_that_was_not_shown_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = MagicMock(async_generate_orphaned_plan_preview=AsyncMock(return_value=False))
    coordinator.orphaned_backup_choice.return_value = {"fub_id": 7, "plan_name": "Lights", "kind": "auto", "slot": 0}
    monkeypatch.setattr(plan_preview, "_backup_selector_state", lambda _coordinator: "Lights")

    render = plan_preview._async_render_orphaned(coordinator)
    with pytest.raises(HomeAssistantError, match="could not be shown"):
        asyncio.run(render)
