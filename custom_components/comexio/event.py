"""Event platform: one doorbell event per bell button (DI1-DI3) of a Doorstation.

The bell buttons are inputs of the Doorstation's extension and already exist as binary sensors. A binary sensor shows
that a button is down, but not that it was pressed: a press that ends between two reads, or a short pulse, leaves no
trace in a state. The event entity rings once for every webhook push that reports the button down, so automations and
notifications get the press itself. A poll never rings: it reads the value, but does not say when it changed.
"""

from collections.abc import Mapping
import logging
from typing import Any

from aiocomexio.doorstation import Doorstation
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    DOORBELL_EVENT_RING,
    doorstation_doorbell_unique_id,
    extension_device_name,
    io_push_signal,
    offline_io_skipped,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorbell_ios, doorstation_ext_name
from .entity import ComexioStableEntityIdMixin, build_device_info, extension_device_identifier

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up a doorbell event for every bell button of the Doorstations the polls report, now and later."""
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}
    if not conf.get("import_ios", True):
        return
    sync = _DoorbellSync(coordinator, async_add_entities, conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False))
    sync()
    entry.async_on_unload(coordinator.async_add_listener(sync))


class _DoorbellSync:
    """Coordinator listener: an event entity for every bell button that was not seen before.

    The Doorstations come with a poll of their own (DoorstationTracker), later than the IOs on the first setup of an
    entry, and a failed read of them leaves none known: the buttons are picked up with the poll that brings both.
    An entity is never removed here: it turns unavailable with its Doorstation (see ComexioDoorbellEvent), and the
    orphan cleanup in async_setup_entry keeps its registry entry, like the camera's.
    """

    def __init__(
        self, coordinator: ComexioCoordinator, async_add_entities: AddEntitiesCallback, include_offline: bool
    ) -> None:
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._include_offline = include_offline
        # The unique ids of the events added: the Comexio id of an IO may be handed to another IO, the button stays.
        self._added: set[str] = set()

    def _new_bells(self) -> list["ComexioDoorbellEvent"]:
        coordinator = self._coordinator
        # One entity per unique id: two IOs of one button in a single poll must not become two entities.
        bells = {
            bell.unique_id: bell
            for io in doorbell_ios((coordinator.data or {}).get("io", []), coordinator.doorstations.known)
            if not offline_io_skipped(io, self._include_offline)
            for bell in [ComexioDoorbellEvent(coordinator, io)]
        }
        return [bell for unique_id, bell in bells.items() if unique_id not in self._added]

    @callback
    def __call__(self) -> None:
        if new := self._new_bells():
            self._added.update(str(bell.unique_id) for bell in new)
            self._async_add_entities(new)


class ComexioDoorbellEvent(ComexioStableEntityIdMixin, CoordinatorEntity[ComexioCoordinator], EventEntity):
    """A bell button of a Doorstation: rings once for every push that reports it down.

    Comexio pushes an IO when its value changes, so a push of a value above zero is the button going down. The
    push is taken for the press whatever the value before it was: a poll that read the button down just before the
    push arrived must not swallow the ring. The entity sits on the device of the Doorstation's extension, next to
    the IOs (PIR1, DI1-DI3, ...) and the camera.
    """

    _attr_has_entity_name = True
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [DOORBELL_EVENT_RING]
    _attr_translation_key = "doorbell"

    def __init__(self, coordinator: ComexioCoordinator, io: Mapping[str, Any]) -> None:
        super().__init__(coordinator)
        # The IO is one of a known Doorstation (doorbell_ios), whose extension id is an integer.
        self._ext_id = int(io["ext_id"])
        self._known_name: str = io["ext_name"]
        identifier = str(io["identifier"])
        self._identifier = identifier
        self._attr_unique_id = doorstation_doorbell_unique_id(coordinator.server_id, self._ext_id, identifier)
        # "DI1" -> "1": the button's number names the entity ("Doorbell 1").
        self._attr_translation_placeholders = {"button": identifier.upper().removeprefix("DI")}

    @property
    def _doorstation(self) -> Doorstation | None:
        return self.coordinator.doorstations.known.get(self._ext_id)

    @property
    def _ext_name(self) -> str:
        """The extension's current name: a rename in Comexio shows in the IO data before the next setup."""
        doorstation = self._doorstation
        if doorstation and (known := doorstation_ext_name(doorstation, (self.coordinator.data or {}).get("io", []))):
            self._known_name = known
        # The last name seen stays the entity's while the Doorstation is unknown: the offline set is looked up by it.
        return self._known_name

    @property
    def device_info(self) -> DeviceInfo:
        """The extension's device, the one its IOs sit on."""
        server_id = self.coordinator.server_id
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, extension_device_identifier(server_id, self._ext_name))},
            name=extension_device_name(server_id, self._ext_name),
            model="Extension Module",
        )

    @property
    def available(self) -> bool:
        """Unavailable with the extension (offline) and with a Doorstation the polls no longer report."""
        return (
            super().available
            and self._ext_name not in (self.coordinator.offline_extensions or ())
            and self._doorstation is not None
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                io_push_signal(self.coordinator.server_id, self._ext_id, self._identifier),
                self._handle_push,
            )
        )

    @callback
    def _handle_push(self, value: float | None) -> None:
        """A push stored the IO's value: a value above zero is the button going down.

        While the entity is unavailable (its extension offline, its Doorstation no longer reported) the ring is
        dropped: written as it is, the timestamp would show up as a late ring once the entity is available again.
        """
        if value is None or value <= 0:
            return
        if not self.available:
            _LOGGER.debug("Ignoring a ring of %s while it is unavailable", self.entity_id)
            return
        self._trigger_event(DOORBELL_EVENT_RING)
        self.async_write_ha_state()
