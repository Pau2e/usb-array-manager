from __future__ import annotations

import ctypes
import os
from pathlib import Path

from ctypes import wintypes


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_TEMPORARY = 0x00000100
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_RANDOM_ACCESS = 0x10000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_BEGIN = 0
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


if os.name != "nt":
    raise RuntimeError("Unbuffered benchmark I/O is available only on Windows.")


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.ReadFile.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
)
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = _kernel32.ReadFile.argtypes
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.SetFilePointerEx.argtypes = (
    wintypes.HANDLE,
    ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
)
_kernel32.SetFilePointerEx.restype = wintypes.BOOL
_kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
_kernel32.FlushFileBuffers.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.VirtualAlloc.argtypes = (
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.DWORD,
)
_kernel32.VirtualAlloc.restype = wintypes.LPVOID
_kernel32.VirtualFree.argtypes = (
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
)
_kernel32.VirtualFree.restype = wintypes.BOOL
_kernel32.GetDiskFreeSpaceW.argtypes = (
    wintypes.LPCWSTR,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
)
_kernel32.GetDiskFreeSpaceW.restype = wintypes.BOOL


class UnbufferedIOError(OSError):
    """Raised when a Windows unbuffered file operation fails."""


class AlignedBuffer:
    def __init__(self, size: int, *, fill_random: bool = False) -> None:
        self.size = size
        self.address = _kernel32.VirtualAlloc(
            None,
            size,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE,
        )
        if not self.address:
            raise _windows_error("Could not allocate an aligned I/O buffer")
        if fill_random:
            ctypes.memmove(self.address, os.urandom(size), size)

    def close(self) -> None:
        if self.address:
            if not _kernel32.VirtualFree(self.address, 0, MEM_RELEASE):
                raise _windows_error("Could not release the aligned I/O buffer")
            self.address = None

    def __enter__(self) -> AlignedBuffer:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


class UnbufferedFile:
    def __init__(self, path: Path, access: int, creation: int, flags: int) -> None:
        self.path = path
        self.handle = _kernel32.CreateFileW(
            str(path),
            access,
            0,
            None,
            creation,
            FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_NO_BUFFERING | flags,
            None,
        )
        if self.handle == INVALID_HANDLE_VALUE:
            self.handle = None
            raise _windows_error(f"Could not open benchmark file: {path}")

    @classmethod
    def create_for_sequential_write(cls, path: Path) -> UnbufferedFile:
        return cls(
            path,
            GENERIC_WRITE,
            CREATE_NEW,
            FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_WRITE_THROUGH,
        )

    @classmethod
    def open_for_sequential_read(cls, path: Path) -> UnbufferedFile:
        return cls(path, GENERIC_READ, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN)

    @classmethod
    def open_for_random_access(cls, path: Path) -> UnbufferedFile:
        return cls(
            path,
            GENERIC_READ | GENERIC_WRITE,
            OPEN_EXISTING,
            FILE_FLAG_RANDOM_ACCESS | FILE_FLAG_WRITE_THROUGH,
        )

    def write(self, buffer: AlignedBuffer, size: int) -> None:
        transferred = wintypes.DWORD()
        if not _kernel32.WriteFile(
            self.handle,
            buffer.address,
            size,
            ctypes.byref(transferred),
            None,
        ):
            raise _windows_error("Unbuffered benchmark write failed")
        if transferred.value != size:
            raise UnbufferedIOError("Windows reported a partial benchmark write.")

    def read(self, buffer: AlignedBuffer, size: int) -> None:
        transferred = wintypes.DWORD()
        if not _kernel32.ReadFile(
            self.handle,
            buffer.address,
            size,
            ctypes.byref(transferred),
            None,
        ):
            raise _windows_error("Unbuffered benchmark read failed")
        if transferred.value != size:
            raise UnbufferedIOError("Windows reported a partial benchmark read.")

    def seek(self, offset: int) -> None:
        new_position = ctypes.c_longlong()
        if not _kernel32.SetFilePointerEx(
            self.handle,
            offset,
            ctypes.byref(new_position),
            FILE_BEGIN,
        ):
            raise _windows_error("Could not seek in the benchmark file")
        if new_position.value != offset:
            raise UnbufferedIOError("Windows selected an unexpected file offset.")

    def flush(self) -> None:
        if not _kernel32.FlushFileBuffers(self.handle):
            raise _windows_error("Could not flush the benchmark file")

    def close(self) -> None:
        if self.handle is not None:
            if not _kernel32.CloseHandle(self.handle):
                raise _windows_error("Could not close the benchmark file")
            self.handle = None

    def __enter__(self) -> UnbufferedFile:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def get_sector_size(path: Path) -> int:
    volume_root = path.resolve().anchor
    if not volume_root:
        raise UnbufferedIOError(f"Could not determine the volume for: {path}")

    sectors_per_cluster = wintypes.DWORD()
    bytes_per_sector = wintypes.DWORD()
    free_clusters = wintypes.DWORD()
    total_clusters = wintypes.DWORD()
    if not _kernel32.GetDiskFreeSpaceW(
        volume_root,
        ctypes.byref(sectors_per_cluster),
        ctypes.byref(bytes_per_sector),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    ):
        raise _windows_error(f"Could not query sector size for: {volume_root}")
    return bytes_per_sector.value


def _windows_error(message: str) -> UnbufferedIOError:
    error = ctypes.WinError(ctypes.get_last_error())
    return UnbufferedIOError(error.errno, f"{message}: {error.strerror}")
