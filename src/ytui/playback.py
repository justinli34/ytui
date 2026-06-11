import ctypes
import json
import os
import subprocess
from ctypes import wintypes

PIPE_PATH = r"\\.\pipe\mpv-control"
GRACEFUL_QUIT_TIMEOUT_SECONDS = 1.0
TERMINATE_TIMEOUT_SECONDS = 1.0

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


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


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
else:
    _kernel32 = None


class AudioPlayer:
    def __init__(self, url: str, *, volume: int = 100) -> None:
        self.url = url
        self.volume = volume
        self.process = None
        self._job_handle = None

    def start(self) -> None:
        self.stop()
        self.process = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--no-terminal",
                f"--volume={self.volume}",
                f"--input-ipc-server={PIPE_PATH}",
                self.url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        self._job_handle = _create_kill_on_close_job(self.process)

    def pause(self) -> None:
        self._command("set_property", "pause", True)

    def resume(self) -> None:
        self._command("set_property", "pause", False)

    def seek(self, seconds: int) -> None:
        self._command("seek", seconds, "relative")

    def seek_absolute(self, seconds: float) -> None:
        self._command("seek", max(seconds, 0.0), "absolute")

    def position(self) -> float | None:
        return self._get_number_property("time-pos")

    def duration(self) -> float | None:
        return self._get_number_property("duration")

    def set_volume(self, volume: int) -> None:
        self._command("set_property", "volume", volume)
        self.volume = volume

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            self._close_job_handle()
            return

        try:
            if process.poll() is None:
                try:
                    self._command("quit")
                    process.wait(timeout=GRACEFUL_QUIT_TIMEOUT_SECONDS)
                except FileNotFoundError, OSError, subprocess.TimeoutExpired:
                    _terminate_process(process)
        finally:
            self._close_job_handle()

    def is_running(self) -> bool:
        return self._is_running()

    def _is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _close_job_handle(self) -> None:
        job_handle = self._job_handle
        self._job_handle = None
        if job_handle is not None:
            _close_handle(job_handle)

    def _get_number_property(self, name: str) -> float | None:
        if not self._is_running():
            return None

        try:
            response = self._request("get_property", name)
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return None

        if response.get("error") != "success":
            return None

        value = response.get("data")
        if value is None:
            return None
        if not isinstance(value, str | int | float):
            return None

        try:
            return float(value)
        except TypeError, ValueError:
            return None

    def _command(self, *args: object) -> None:
        message = json.dumps({"command": list(args)}) + "\n"

        with open(PIPE_PATH, "r+b", buffering=0) as pipe:
            pipe.write(message.encode())

    def _request(self, *args: object) -> dict[str, object]:
        message = json.dumps({"command": list(args)}) + "\n"

        with open(PIPE_PATH, "r+b", buffering=0) as pipe:
            pipe.write(message.encode())
            response = pipe.readline()

        if not response:
            return {}

        return json.loads(response.decode())


def _creation_flags() -> int:
    if os.name != "nt":
        return 0

    return subprocess.CREATE_NO_WINDOW


def _create_kill_on_close_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt" or _kernel32 is None:
        return None

    job_handle = _kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return None

    try:
        limit_info = _JobObjectExtendedLimitInformation()
        limit_info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limit_info),
            ctypes.sizeof(limit_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise OSError("Could not access process handle")

        if not _kernel32.AssignProcessToJobObject(job_handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        _close_handle(job_handle)
        return None

    return job_handle


def _close_handle(handle: int) -> None:
    if os.name == "nt" and _kernel32 is not None:
        _kernel32.CloseHandle(handle)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    except OSError, subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except OSError, subprocess.TimeoutExpired:
            pass
