from __future__ import annotations

from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter


WM_DEVICECHANGE = 0x0219
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004

_REFRESH_EVENTS = {
    DBT_DEVNODES_CHANGED,
    DBT_DEVICEARRIVAL,
    DBT_DEVICEREMOVECOMPLETE,
}


def is_inventory_change(message_code: int, event_code: int) -> bool:
    return message_code == WM_DEVICECHANGE and event_code in _REFRESH_EVENTS


class WindowsDeviceEventFilter(QAbstractNativeEventFilter):
    def __init__(self, on_device_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_device_change = on_device_change

    def nativeEventFilter(self, event_type, message):
        try:
            native_message = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):
            return False

        if is_inventory_change(native_message.message, int(native_message.wParam)):
            self._on_device_change()

        return False

