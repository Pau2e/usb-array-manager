from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.vhdx_together import validate_vhdxs_together


class VhdxTogetherThread(QThread):
    validation_finished = Signal(object)
    validation_failed = Signal(str)
    progress = Signal(str)
    diagnostic = Signal(str)

    def __init__(self, devices: tuple[StorageDevice, ...], parent=None) -> None:
        super().__init__(parent)
        self.devices = devices
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        # The service observes this between stages; its cleanup never cancels.
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = validate_vhdxs_together(
                self.devices, progress=self.progress.emit,
                diagnostic=self.diagnostic.emit, cancel_event=self._cancel_event,
            )
        except Exception as error:
            self.validation_failed.emit(str(error))
        else:
            self.validation_finished.emit(result)
