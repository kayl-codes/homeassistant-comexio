# Version: 0.7.8
import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    QUICKBUS_IO_DATA_KEY,
    SOURCE_CATEGORIES,
    WebioClass,
    extension_device_name,
    hub_era_object_id,
    marker_device_name,
    one_line,
    quickbus_device_name,
    quickbus_io_platform,
    quickbus_io_unique_id,
    stable_object_id,
)
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)


def hub_device_id(coordinator: ComexioCoordinator) -> str | None:
    """Resolve the hub device's registry id for via_device_id linkage.

    Returns None (instead of raising) if the hub device isn't registered yet, so a
    dependent entity still gets created — just without the via_device_id link — rather
    than having its whole platform setup aborted by an uncaught ValueError.
    """
    try:
        return dr.async_get_device_id_by_identifier(
            coordinator.hass, (DOMAIN, coordinator.server_id), config_entry_id=coordinator.config_entry.entry_id
        )
    except ValueError:
        _LOGGER.error(
            "Hub device for server '%s' (entry %s) not found in the device registry; "
            "affected entities will be created without a via_device_id link",
            coordinator.server_id,
            coordinator.config_entry.entry_id,
        )
        return None


def build_device_info(
    coordinator: ComexioCoordinator,
    *,
    identifiers: set[tuple[str, str]],
    name: str,
    model: str,
    via_identifier: str | None = None,
) -> DeviceInfo:
    """Build a sub-device's device_info, linked to its parent device via via_device_id.

    The parent is the device of via_identifier (a Quickbus device's host extension) once that is
    registered, else the hub — the platforms set up in parallel, so on the very first setup the
    host may not exist yet; update_quickbus_devices moves it once all platforms are set up. A host
    with no entities of its own (only Quickbus IOs) never gets a device, so its Quickbus devices
    stay on the hub.
    """
    info: DeviceInfo = {
        "identifiers": identifiers,
        "name": name,
        "manufacturer": "Comexio",
        "model": model,
    }
    parent = via_identifier and dr.async_get(coordinator.hass).async_get_device_by_identifier(
        (DOMAIN, via_identifier), coordinator.config_entry.entry_id
    )
    if via_identifier and not parent:
        _LOGGER.debug("Parent device %s not registered yet, linking %s to the hub", via_identifier, one_line(name))
    if via_id := parent.id if parent else hub_device_id(coordinator):
        info["via_device_id"] = via_id
    return info


def function_plan_device_info(coordinator: ComexioCoordinator) -> DeviceInfo:
    """Sub-device grouping every function plan entity (selectors, preview, backups, run states).

    The '#' in the name is deliberate: HA lists a config entry's devices by name, and every
    sub-device starts with "<server> ", so '#' (sorting before digits and letters) places this
    group right below the hub instead of between the extension modules.
    """
    return build_device_info(
        coordinator,
        identifiers={(DOMAIN, f"{coordinator.server_id}_function_plans")},
        name=f"{coordinator.server_id} # Function plans",
        model="Function Plan Group",
    )


class ComexioFunctionPlanEntityMixin:
    """Function plan entity: lives on the function plan sub-device, keeps its hub-era entity_id.

    These entities sat on the hub device (named after the server) before, so HA derived their
    entity_id from "<server> <name>" — the ids the plan card docs and existing automations use.
    On the sub-device HA would derive "<server> # Function plans <name>" instead, so a new
    install requests the old id explicitly (same hook and registry semantics as
    ComexioStableEntityIdMixin: existing entities keep their entity_id either way).
    Subclasses set _hub_era_name to the English name the entity had on the hub — frozen, since
    the display names dropped their "Function Plan" prefix once the device itself carried it.
    """

    coordinator: ComexioCoordinator
    entity_id: str
    _hub_era_name: str

    @property
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

    def add_to_platform_start(
        self, hass: HomeAssistant, platform: EntityPlatform, parallel_updates: asyncio.Semaphore | None
    ) -> None:
        super().add_to_platform_start(hass, platform, parallel_updates)  # type: ignore[misc]
        self.entity_id = f"{platform.domain}.{hub_era_object_id(self.coordinator.server_id, self._hub_era_name)}"


class ComexioStableEntityIdMixin:
    """Requests entity_id "<domain>.<stable_object_id(unique_id)>" instead of a name-derived one.

    Hooked into add_to_platform_start because that is the first point the platform (and so
    the domain) is known, and it runs before HA derives the entity_id. HA only uses the
    requested id for a new registry entry — an existing entity keeps its entity_id, and one
    restored from deleted_entities gets its old entity_id back — but stores it as
    suggested_object_id either way, so HA's "recreate entity ID" and
    async_regenerate_entity_id then yield the same stable id for existing entities too
    (unless the user gave the entity a custom name, which takes precedence in HA).
    If the id is already taken by another entity, HA silently appends "_2".
    """

    unique_id: str | None
    entity_id: str

    def add_to_platform_start(
        self, hass: HomeAssistant, platform: EntityPlatform, parallel_updates: asyncio.Semaphore | None
    ) -> None:
        super().add_to_platform_start(hass, platform, parallel_updates)  # type: ignore[misc]
        if self.unique_id:
            self.entity_id = f"{platform.domain}.{stable_object_id(self.unique_id)}"


class ComexioIOEntity(ComexioStableEntityIdMixin, CoordinatorEntity):
    """Shared base for all IO entities attached to an extension module.

    Centralises device_info and offline-availability logic that would otherwise
    be duplicated across sensor, switch, and binary_sensor platforms.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._io_id: str = io["id"]
        self._ext_name: str = io["ext_name"]
        self._attr_unique_id = f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
        self._attr_name = io["ha_name"]

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, extension_device_identifier(self.coordinator.server_id, self._ext_name))},
            name=extension_device_name(self.coordinator.server_id, self._ext_name),
            model="Extension Module",
        )

    @property
    def available(self) -> bool:
        return super().available and self._ext_name not in self.coordinator.offline_extensions


class ComexioQuickbusIOEntity(ComexioIOEntity):
    """Shared base for the IO entities of a Quickbus device (multitouch, motion detector, ...).

    Its identifiers (G1, TL1, ...) repeat on every device of the same kind, so the unique_id
    carries the IO's unique Id instead; each device gets its own HA device. Availability
    follows the host extension (ext_name) the device hangs on.
    """

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, io)
        self._attr_unique_id = quickbus_io_unique_id(server_id, io["id"])
        self._quickbus_device_id: str = str(io["quickbus"]["device_id"])
        self._quickbus_device_name: str = io["quickbus"]["name"]

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, quickbus_device_identifier(self.coordinator.server_id, self._quickbus_device_id))},
            name=quickbus_device_name(self.coordinator.server_id, self._quickbus_device_name),
            model="Quickbus Device",
            via_identifier=extension_device_identifier(self.coordinator.server_id, self._ext_name),
        )


def quickbus_device_identifier(server_id: str, device_id: str | int) -> str:
    """Device registry identifier of a Quickbus device (its Comexio device id, not its name)."""
    return f"{server_id}_qb{device_id}".lower()


def extension_device_identifier(server_id: str, ext_name: str) -> str:
    """Device registry identifier of an extension module."""
    return f"{server_id}_{ext_name}".lower()


def update_quickbus_devices(coordinator: ComexioCoordinator) -> None:
    """Bring the registered Quickbus devices in line with the polled data: host extension and name.

    The platforms set up in parallel, so on the very first setup a Quickbus device can be
    registered before its host extension's device and falls back to the hub; every host that has
    a device by now gets its Quickbus devices. HA applies device_info only when an entity is added,
    so a device renamed in Comexio is renamed here (a name set in HA wins over it anyway).
    """
    dev_reg = dr.async_get(coordinator.hass)
    entry_id = coordinator.config_entry.entry_id
    server_id = coordinator.server_id
    # One host per Quickbus device: its IOs all hang on the same extension.
    devices = {
        str(io["quickbus"]["device_id"]): (io["ext_name"], io["quickbus"]["name"])
        for io in coordinator.data.get(QUICKBUS_IO_DATA_KEY, [])
    }
    for device_id, (ext_name, name) in devices.items():
        device = dev_reg.async_get_device_by_identifier(
            (DOMAIN, quickbus_device_identifier(server_id, device_id)), entry_id
        )
        if device is None:
            continue  # no entity of it was added (offline host)
        changes: dict[str, Any] = {}
        if device.name != (current_name := quickbus_device_name(server_id, name)):
            changes["name"] = current_name
        host = dev_reg.async_get_device_by_identifier(
            (DOMAIN, extension_device_identifier(server_id, ext_name)), entry_id
        )
        if host is None:
            _LOGGER.debug("Host %s of %s has no device, it stays on the hub", one_line(ext_name), one_line(device.name))
        elif device.via_device_id != host.id:
            changes["via_device_id"] = host.id
        if changes:
            dev_reg.async_update_device(device.id, **changes)


def track_quickbus_devices(coordinator: ComexioCoordinator) -> Callable[[], None]:
    """Run update_quickbus_devices now (all platforms set up) and after every poll; returns the unsubscribe."""
    update_quickbus_devices(coordinator)
    last_data = coordinator.data

    def _on_update() -> None:
        nonlocal last_data
        # Webhook pushes and the other in-place refreshes keep the data object: no new poll, nothing to update.
        if coordinator.data is not last_data:
            last_data = coordinator.data
            update_quickbus_devices(coordinator)

    return coordinator.async_add_listener(_on_update)


def quickbus_ios_for_platform(
    coordinator: ComexioCoordinator, conf: dict[str, Any], platform: str
) -> list[dict[str, Any]]:
    """The imported Quickbus IOs that get an entity on this platform (read-only: binary_sensor/sensor).

    Same platform and offline rule as the entity cleanup in __init__ (_quickbus_entity_uids).
    """
    include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
    return [
        io
        for io in coordinator.data.get(QUICKBUS_IO_DATA_KEY, [])
        if quickbus_io_platform(io) == platform and (not io.get("offline") or include_offline)
    ]


class ComexioMarkerEntity(ComexioStableEntityIdMixin, CoordinatorEntity):
    """Shared base for all marker entities (writable and read-only).

    Centralises unique_id/name/device_info that would otherwise be duplicated
    across the switch, number, binary_sensor, and sensor platforms.

    Markers and KNX objects are near-identical twins (see const.SOURCE_CATEGORIES):
    the three coordinator/API touch-points a domain entity needs — value read, HA
    write, cache update — are routed through the _source_* helpers below, keyed on
    the ``_SOURCE`` class attribute, so ComexioKnxEntity can reuse every domain
    subclass unchanged by just flipping ``_SOURCE``.
    """

    _attr_has_entity_name = True
    _SOURCE: WebioClass = WebioClass.MARKER

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        infix = SOURCE_CATEGORIES[self._SOURCE].unique_id_infix
        self._attr_unique_id = f"comexio_{server_id}_{infix}{self._marker_id}".lower()
        self._attr_name = marker["ha_name"]

    @property
    def _source_label(self) -> str:
        """Short category label ('Marker'/'KNX') for user-facing error messages."""
        return SOURCE_CATEGORIES[self._SOURCE].label

    @property
    def _source_value(self) -> Any:
        """Latest cached value for this source, or None if unknown."""
        return self.coordinator.source_states(self._SOURCE).get(self._marker_id)

    async def _async_source_write(self, value: float | int) -> bool:
        """Push a value to Comexio via the API (marker/KNX write path)."""
        return await self.coordinator.api.set_value(self._SOURCE.value, self._marker_id, value)

    def _source_cache_update(self, value: float | int) -> None:
        """Optimistically update the coordinator's value cache after a write."""
        self.coordinator.update_source(self._SOURCE, self._marker_id, value)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, f"{self.coordinator.server_id}_markers")},
            name=marker_device_name(self.coordinator.server_id),
            model="Marker Group",
        )


async def _async_write_via_bridge(coordinator: ComexioCoordinator, subject: str, k_id: str, value: float | int) -> bool:
    """Push a value to a K-element's bridge Marker — never to the K-element itself.

    Shared by ComexioKnxEntity (single K-element domains) and ComexioKnxDpt3Entity (DPT3.x
    composite pairs): api.set_value("knx", ...) writes ?knx=K<id> directly — confirmed live
    2026-09-16 to have NO effect: a K object is blind/read-only via Comexio's API (see
    project_knx_objects memory), exactly why Entwurf A "Merker-Brücke" exists at all. Every
    HA-initiated write must instead land on the K-element's bridge Marker, which Comexio's
    own Marker -> KNX object wiring then carries onward for real.

    Raises rather than returning False for the two "can't even attempt this" cases (bridge
    data not loaded yet / no bridge wired) so the user sees why, instead of the generic
    "Failed to turn on/set" a plain False return produces at the call site — those two are a
    different failure mode from an HTTP write actually failing, which still returns False and
    lets that generic message stand.
    """
    bridge_marker_by_k_id = coordinator._knx_bridge_marker_by_k_id()
    if bridge_marker_by_k_id is None:
        raise HomeAssistantError(f"Comexio {subject}: bridge Marker data not loaded yet — try again shortly")
    bridge_marker_id = bridge_marker_by_k_id.get(k_id)
    if bridge_marker_id is None:
        raise HomeAssistantError(
            f"Comexio {subject}: no bridge Marker wired yet — run Full Sync or the 'Create KNX bridge "
            "Markers + feedback loop' repair action first"
        )
    return await coordinator.api.set_value("marker", bridge_marker_id, value)


class ComexioKnxEntity(ComexioMarkerEntity):
    """Shared base for all KNX-object entities (blind implementation, see project_knx_objects memory).

    KNX objects are modeled 1:1 on markers; only the unique_id infix ('k', via
    SOURCE_CATEGORIES) and the sub-device grouping differ, so every ComexioMarker*
    domain subclass works for KNX by additionally inheriting from this mixin.
    """

    _SOURCE = WebioClass.KNX

    async def _async_source_write(self, value: float | int) -> bool:
        """Push a value to Comexio for a KNX object — via its bridge Marker (see _async_write_via_bridge)."""
        return await _async_write_via_bridge(
            self.coordinator, f"{self._source_label} {self._marker_id}", self._marker_id, value
        )

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, f"{self.coordinator.server_id}_knx")},
            name=f"{self.coordinator.server_id} KNX",
            model="KNX Group",
        )


class ComexioKnxDpt3Entity(ComexioStableEntityIdMixin, CoordinatorEntity):
    """Shared base for DPT3.x (Dimmer 3.007 / Blinds 3.008) composite KNX entities.

    Comexio splits a DPT3.x KNX object into two K-elements sharing one KnxDeviceId — a
    digital control bit (direction) and an analog 3-bit step code (0=break, 1-7=move), see
    aiocomexio parse_config's DPT3 pairing and project_knx_write_path_design memory ("Punkt 4,
    Hälfte (b)"). cover.py (Blinds) and light.py (Dimmer, best-effort brightness) each build
    one HA entity per pair instead of exposing the two K-elements as separate generic
    switch/number entities. Both halves write through their own bridge Marker exactly like
    ComexioKnxEntity._async_source_write — never through the K-element itself, which is
    blind/read-only via Comexio's API.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ComexioCoordinator,
        server_id: str,
        direction_item: dict[str, Any],
        stepcode_item: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._direction_id = str(direction_item["id"])
        self._stepcode_id = str(stepcode_item["id"])
        infix = SOURCE_CATEGORIES[WebioClass.KNX].unique_id_infix
        self._attr_unique_id = f"comexio_{server_id}_{infix}{self._direction_id}_{infix}{self._stepcode_id}".lower()
        self._attr_name = stepcode_item["ha_name"]

    async def _async_write_direction(self, direction: int) -> bool:
        """Write the control-bit K-element's bridge Marker (see class docstring)."""
        return await self._async_write_bridge(self._direction_id, direction)

    async def _async_write_stepcode(self, stepcode: int) -> bool:
        """Write the step-code K-element's bridge Marker (see class docstring)."""
        return await self._async_write_bridge(self._stepcode_id, stepcode)

    async def _async_write_bridge(self, k_id: str, value: float | int) -> bool:
        """Write one K-element's bridge Marker (see _async_write_via_bridge)."""
        return await _async_write_via_bridge(self.coordinator, f"K{k_id}", k_id, value)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, f"{self.coordinator.server_id}_knx")},
            name=f"{self.coordinator.server_id} KNX",
            model="KNX Group",
        )
