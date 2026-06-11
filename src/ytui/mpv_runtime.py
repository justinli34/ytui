from __future__ import annotations

import ctypes
import itertools
import json
import os
import socket
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

MpvProcess = subprocess.Popen[bytes]

_ENDPOINT_COUNTER = itertools.count()
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class ChildProcessLifetime(Protocol):
    def close(self) -> None: ...


class _MpvRuntime(Protocol):
    def create_ipc_endpoint(self) -> MpvIpcEndpoint: ...

    def creation_flags(self) -> int: ...

    def bind_child_process_lifetime(self, process: MpvProcess) -> ChildProcessLifetime: ...


class MpvIpcEndpoint:
    def __init__(self, path: str) -> None:
        self.path = path

    def command(self, *args: object) -> None:
        self._exchange(_command_message(*args), read_response=False)

    def request(self, *args: object) -> dict[str, object]:
        response = self._exchange(_command_message(*args), read_response=True)
        if not response:
            return {}

        data = json.loads(response.decode())
        if not isinstance(data, dict):
            return {}

        return data

    def close(self) -> None:
        pass

    def _exchange(self, message: bytes, *, read_response: bool) -> bytes:
        raise NotImplementedError


class _WindowsIpcEndpoint(MpvIpcEndpoint):
    def _exchange(self, message: bytes, *, read_response: bool) -> bytes:
        with open(self.path, "r+b", buffering=0) as pipe:
            pipe.write(message)
            if read_response:
                return pipe.readline()

        return b""


class _PosixIpcEndpoint(MpvIpcEndpoint):
    def __init__(self, path: str, socket_family: int) -> None:
        super().__init__(path)
        self._socket_family = socket_family

    def close(self) -> None:
        try:
            Path(self.path).unlink()
        except FileNotFoundError:
            pass

    def _exchange(self, message: bytes, *, read_response: bool) -> bytes:
        with socket.socket(self._socket_family, socket.SOCK_STREAM) as ipc:
            ipc.connect(self.path)
            ipc.sendall(message)
            if read_response:
                return _read_socket_line(ipc)

        return b""


class _NoopChildProcessLifetime:
    def close(self) -> None:
        pass


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsChildProcessLifetime:
    def __init__(self, kernel32: ctypes.WinDLL, job_handle: int) -> None:
        self._kernel32 = kernel32
        self._job_handle = job_handle

    def close(self) -> None:
        job_handle = self._job_handle
        self._job_handle = 0
        if job_handle:
            self._kernel32.CloseHandle(job_handle)


class _WindowsMpvRuntime:
    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL

    def create_ipc_endpoint(self) -> MpvIpcEndpoint:
        return _WindowsIpcEndpoint(rf"\\.\pipe\ytui-mpv-{os.getpid()}-{next(_ENDPOINT_COUNTER)}")

    def creation_flags(self) -> int:
        return subprocess.CREATE_NO_WINDOW

    def bind_child_process_lifetime(self, process: MpvProcess) -> ChildProcessLifetime:
        job_handle = self._kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            return _NoopChildProcessLifetime()

        try:
            limit_info = _JobObjectExtendedLimitInformation()
            limit_info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel32.SetInformationJobObject(
                job_handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limit_info),
                ctypes.sizeof(limit_info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            process_handle = getattr(process, "_handle", None)
            if process_handle is None:
                raise OSError("Could not access process handle")

            if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self._kernel32.CloseHandle(job_handle)
            return _NoopChildProcessLifetime()

        return _WindowsChildProcessLifetime(self._kernel32, job_handle)


class _PosixMpvRuntime:
    def __init__(self) -> None:
        socket_family = getattr(socket, "AF_UNIX", None)
        if socket_family is None:
            raise OSError("Unix sockets are not available on this platform")

        self._socket_family = socket_family

    def create_ipc_endpoint(self) -> MpvIpcEndpoint:
        path = (
            Path(tempfile.gettempdir()) / f"ytui-mpv-{os.getpid()}-{next(_ENDPOINT_COUNTER)}.sock"
        )
        return _PosixIpcEndpoint(str(path), self._socket_family)

    def creation_flags(self) -> int:
        return 0

    def bind_child_process_lifetime(self, process: MpvProcess) -> ChildProcessLifetime:
        return _NoopChildProcessLifetime()


def _command_message(*args: object) -> bytes:
    return (json.dumps({"command": list(args)}) + "\n").encode()


def _read_socket_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []

    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break

        chunks.append(chunk)
        if b"\n" in chunk:
            break

    response, _, _ = b"".join(chunks).partition(b"\n")
    return response


def _create_runtime() -> _MpvRuntime:
    if os.name == "nt":
        return _WindowsMpvRuntime()

    return _PosixMpvRuntime()


MPV_RUNTIME = _create_runtime()
