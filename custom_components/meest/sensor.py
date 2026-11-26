"""Sensor platform for the Meest parcel tracker integration."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MeestConfigEntry
from .const import DOMAIN, ParcelStatus
from .coordinator import MeestCoordinator, bucket
from .device import ATTRIBUTION, build_device_info
from .parcels import parse_iso

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out to all entities; HA's per-entity
# update throttling adds nothing here.
PARALLEL_UPDATES = 0

_ALL_BUCKETS = (
    "incoming_active",
    "outgoing_active",
    "incoming_delivered",
    "outgoing_delivered",
)


def _active_barcodes(coordinator: MeestCoordinator) -> set[str]:
    """Barcodes of every active parcel, in both directions."""
    return {
        parcel.get("barcode", "")
        for name in ("incoming_active", "outgoing_active")
        for parcel in bucket(coordinator.data, name)
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeestConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Meest sensor entities from a config entry."""
    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = entry.runtime_data.coordinator
    current_barcodes = _active_barcodes(coordinator)
    entry_id = entry.entry_id

    # Remove per-parcel sensors whose barcode is no longer active (removed, or
    # delivered between restarts). Scoped to the sensor domain so it never
    # touches the refresh button or the summary and diagnostic sensors.
    registry = er.async_get(hass)
    non_parcel_unique_ids = {
        f"{entry_id}_incoming_parcels",
        f"{entry_id}_next_delivery",
        f"{entry_id}_en_route_to_pickup_point",
        f"{entry_id}_awaiting_pickup",
        f"{entry_id}_delivered_parcels",
        f"{entry_id}_outgoing_parcels",
        f"{entry_id}_outgoing_delivered_parcels",
        f"{entry_id}_last_update",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry_id):
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{entry_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            barcode = entity_entry.unique_id[len(f"{entry_id}_") :]
            if barcode not in current_barcodes:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        MeestIncomingParcelsSensor(
            coordinator, entry, async_add_entities, current_barcodes
        ),
    ]
    entities.extend(
        MeestParcelSensor(coordinator, entry, barcode)
        for barcode in sorted(current_barcodes)
    )
    entities.extend(
        [
            MeestNextDeliverySensor(coordinator, entry),
            MeestEnRouteToPickupPointSensor(coordinator, entry),
            MeestAwaitingPickupSensor(coordinator, entry),
            MeestDeliveredParcelsSensor(coordinator, entry),
            MeestOutgoingParcelsSensor(coordinator, entry),
            MeestOutgoingDeliveredParcelsSensor(coordinator, entry),
            MeestLastUpdateSensor(coordinator, entry),
        ]
    )
    async_add_entities(entities)


class _MeestEntity(CoordinatorEntity[MeestCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: MeestCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = build_device_info(entry)


class _MeestListSensor(_MeestEntity):
    """A count with the parcels themselves as an attribute."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"parcels"})
    _bucket_name = ""

    def __init__(self, coordinator: MeestCoordinator, entry: ConfigEntry) -> None:
        """Initialise the summary sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"

    def _parcels(self) -> list[dict]:
        return bucket(self.coordinator.data, self._bucket_name)

    @property
    def native_value(self) -> int:
        """Return the number of parcels in the list."""
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the parcels in the list."""
        return {"parcels": self._parcels()}


class MeestIncomingParcelsSensor(_MeestListSensor):
    """Summary sensor: count of active incoming parcels.

    Also spawns a per-parcel sensor for each new barcode (both directions) and
    removes stale ones through the registry — self-removal races and leaves
    ghost entities.
    """

    _attr_translation_key = "incoming_parcels"
    _bucket_name = "incoming_active"

    def __init__(
        self,
        coordinator: MeestCoordinator,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
        known_barcodes: set[str] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._known_barcodes: set[str] = known_barcodes or set()

    def _handle_coordinator_update(self) -> None:
        current_barcodes = _active_barcodes(self.coordinator)

        new_barcodes = current_barcodes - self._known_barcodes
        if new_barcodes:
            self._async_add_entities(
                MeestParcelSensor(self.coordinator, self._entry, barcode)
                for barcode in new_barcodes
            )

        removed_barcodes = self._known_barcodes - current_barcodes
        if removed_barcodes:
            registry = er.async_get(self.hass)
            for barcode in removed_barcodes:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self._entry.entry_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_barcodes = current_barcodes
        super()._handle_coordinator_update()


class MeestParcelSensor(_MeestEntity):
    """Per-parcel sensor reporting the status of one Meest parcel."""

    _attr_translation_key = "parcel"
    _unrecorded_attributes = frozenset({"raw", "history"})

    def __init__(
        self, coordinator: MeestCoordinator, entry: ConfigEntry, barcode: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._barcode = barcode
        self._attr_unique_id = f"{entry.entry_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}

    def _get_parcel(self) -> dict[str, Any] | None:
        # A parcel lives in exactly one list; look in all of them so the
        # sensor stays right in the cycle where it moves to delivered.
        for name in _ALL_BUCKETS:
            for parcel in bucket(self.coordinator.data, name):
                if parcel.get("barcode") == self._barcode:
                    return parcel
        return None

    @property
    def native_value(self) -> str | None:
        """Return the native value of the sensor."""
        parcel = self._get_parcel()
        return parcel.get("status") if parcel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}


class MeestNextDeliverySensor(_MeestEntity):
    """Earliest expected delivery datetime across active incoming parcels."""

    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MeestCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_next_delivery"

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        now = dt_util.utcnow()
        for parcel in bucket(self.coordinator.data, "incoming_active"):
            moment = parse_iso(parcel.get("planned_from"))
            if moment is None:
                continue
            if (parse_iso(parcel.get("planned_to")) or moment) < now:
                continue
            result.append((moment, parcel))
        return result

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
            "receiver": earliest.get("receiver"),
            "contents": earliest.get("contents"),
        }


class MeestEnRouteToPickupPointSensor(_MeestListSensor):
    """Incoming parcels destined for a pickup point but not yet there."""

    _attr_translation_key = "en_route_to_pickup_point"

    def _parcels(self) -> list[dict]:
        return [
            parcel
            for parcel in bucket(self.coordinator.data, "incoming_active")
            if parcel.get("pickup")
            and parcel.get("status")
            not in (ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING)
        ]


class MeestAwaitingPickupSensor(_MeestListSensor):
    """Incoming parcels that have arrived at a pickup point."""

    _attr_translation_key = "awaiting_pickup"

    def _parcels(self) -> list[dict]:
        return [
            parcel
            for parcel in bucket(self.coordinator.data, "incoming_active")
            if parcel.get("status") is ParcelStatus.AT_PICKUP_POINT
        ]


class MeestDeliveredParcelsSensor(_MeestListSensor):
    """Recently delivered incoming Meest parcels."""

    _attr_translation_key = "delivered_parcels"
    _bucket_name = "incoming_delivered"


class MeestOutgoingParcelsSensor(_MeestListSensor):
    """Active outgoing parcels."""

    _attr_translation_key = "outgoing_parcels"
    _bucket_name = "outgoing_active"


class MeestOutgoingDeliveredParcelsSensor(_MeestListSensor):
    """Recently delivered outgoing parcels."""

    _attr_translation_key = "outgoing_delivered_parcels"
    _bucket_name = "outgoing_delivered"


class MeestLastUpdateSensor(_MeestEntity):
    """Diagnostic sensor reporting when Meest was last polled successfully."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MeestCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_update"

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        return self.coordinator.last_success_time
