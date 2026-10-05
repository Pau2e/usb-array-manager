import ctypes
import unittest
from ctypes import wintypes

from PySide6.QtWidgets import QApplication, QWidget

from usb_array_manager.services.device_monitor import (
    DBT_DEVICEARRIVAL,
    DBT_DEVICEREMOVECOMPLETE,
    DBT_DEVNODES_CHANGED,
    WM_DEVICECHANGE,
    WindowsDeviceEventFilter,
    is_inventory_change,
)


class DeviceMonitorTests(unittest.TestCase):
    def test_recognizes_inventory_change_events(self) -> None:
        for event_code in (
            DBT_DEVICEARRIVAL,
            DBT_DEVICEREMOVECOMPLETE,
            DBT_DEVNODES_CHANGED,
        ):
            with self.subTest(event_code=event_code):
                self.assertTrue(is_inventory_change(WM_DEVICECHANGE, event_code))

    def test_ignores_unrelated_windows_messages(self) -> None:
        self.assertFalse(is_inventory_change(0x000F, DBT_DEVICEARRIVAL))
        self.assertFalse(is_inventory_change(WM_DEVICECHANGE, 0x8001))

    def test_native_filter_calls_callback(self) -> None:
        received: list[bool] = []
        event_filter = WindowsDeviceEventFilter(lambda: received.append(True))
        message = wintypes.MSG()
        message.message = WM_DEVICECHANGE
        message.wParam = DBT_DEVICEARRIVAL

        handled = event_filter.nativeEventFilter(None, ctypes.addressof(message))

        self.assertFalse(handled)
        self.assertEqual(received, [True])

    def test_qt_forwards_windows_device_change_message(self) -> None:
        application = QApplication.instance() or QApplication([])
        received: list[bool] = []
        event_filter = WindowsDeviceEventFilter(lambda: received.append(True))
        window = QWidget()
        window_handle = int(window.winId())
        application.installNativeEventFilter(event_filter)

        try:
            ctypes.windll.user32.SendMessageW(
                window_handle,
                WM_DEVICECHANGE,
                DBT_DEVNODES_CHANGED,
                0,
            )
            application.processEvents()
        finally:
            application.removeNativeEventFilter(event_filter)
            window.close()

        self.assertEqual(received, [True])


if __name__ == "__main__":
    unittest.main()
