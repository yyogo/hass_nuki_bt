"""DataUpdateCoordinator for hass_nuki_bt."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import datetime

from typing import TYPE_CHECKING, Any

import async_timeout

from bleak import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback, CALLBACK_TYPE, CoreState
from .pyNukiBt import NukiDevice, NukiConst

if TYPE_CHECKING:
    from collections.abc import Callable
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

DEVICE_STARTUP_TIMEOUT = 300


class NukiDataUpdateCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Class to manage fetching Nuki data."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        ble_device: BLEDevice,
        device: NukiDevice,
        base_unique_id: str,
        device_name: str,
        connectable: bool,
        security_pin: int = None,
    ) -> None:
        """Initialize global nuki data updater."""
        super().__init__(
            hass=hass,
            logger=logger,
            address=ble_device.address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=connectable,
        )
        self.ble_device = ble_device
        self.device = device
        self.device_name = device_name
        self.base_unique_id = base_unique_id
        self.model = None
        self.last_nuki_log_entry = {"index" : 0}
        self._security_pin = security_pin
        self._nuki_listeners: dict[
            CALLBACK_TYPE, tuple[CALLBACK_TYPE, object | None]
        ] = {}
        self._unsubscribe_nuki_callbacks = None
        self.is_available = False
        self.last_updated = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

    @callback
    def _async_start(self) -> None:
        self._unsubscribe_nuki_callbacks = self.device.subscribe(
            self.async_update_nuki_listeners
        )
        return super()._async_start()

    @callback
    def _async_stop(self) -> None:
        if self._unsubscribe_nuki_callbacks is not None:
            self._unsubscribe_nuki_callbacks()
        return super()._async_stop()

    @callback
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""

        @callback
        def remove_listener() -> None:
            """Remove update listener."""
            self._nuki_listeners.pop(remove_listener)

        self._nuki_listeners[remove_listener] = (update_callback, context)
        return remove_listener

    @callback
    def async_update_nuki_listeners(self) -> None:
        """Update all registered listeners."""
        for update_callback, _ in list(self._nuki_listeners.values()):
            update_callback()

    @callback
    def _needs_poll(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        return (
            self.hass.state == CoreState.running
            and self.device.poll_needed(seconds_since_last_poll)
            and bool(
                bluetooth.async_ble_device_from_address(
                    self.hass, service_info.device.address, connectable=True
                )
            )
        )

    async def _async_update(
        self, service_info: bluetooth.BluetoothServiceInfoBleak = None
    ) -> None:
        """Poll the device."""
        if service_info:
            self.device.set_ble_device(service_info.device)
        async with self.device.connection():
            await self.device.update_state()
            if self._security_pin:
                # get the latest log entry
                # todo: check if Nuki logging is enabled
                logs = await self.device.request_log_entries(
                    security_pin=self._security_pin, count=1
                )
                if logs:
                    if logs[0].type == NukiConst.LogEntryType.LOCK_ACTION:
                        # todo: handle other log types
                        self.last_nuki_log_entry = logs[0]
                    elif logs[0].index > self.last_nuki_log_entry["index"]:
                        # if there are new log entries, get max 10 entries
                        logs = await self.device.request_log_entries(
                            security_pin=self._security_pin,
                            count=min(10, logs[0].index - self.last_nuki_log_entry["index"]),
                            start_index=logs[0].index,
                        )
                        for log in logs:
                            if log.type == NukiConst.LogEntryType.LOCK_ACTION:
                                self.last_nuki_log_entry = log
                                break
        self.is_available = True
        self.last_updated = datetime.datetime.now(datetime.UTC)
        self.async_update_nuki_listeners()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        self.ble_device = service_info.device
        self.device.parse_advertisement_data(
            service_info.device, service_info.advertisement
        )
        self.is_available = True
        super()._async_handle_bluetooth_event(service_info, change)

    async def async_wait_ready(self) -> bool:
        """Wait for the device to be ready."""
        with contextlib.suppress(asyncio.TimeoutError):
            async with async_timeout.timeout(DEVICE_STARTUP_TIMEOUT):
                try:
                    await self._async_update()
                except BleakError:
                    return False
                return True
        return False

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        self.is_available = False
        self.async_update_nuki_listeners()

