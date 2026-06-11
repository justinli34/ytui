import json
import subprocess

from ytui.mpv_runtime import MPV_RUNTIME, ChildProcessLifetime, MpvIpcEndpoint, MpvProcess


class AudioPlayer:
    def __init__(self, url: str, *, volume: int = 100) -> None:
        self.url = url
        self.volume = volume
        self.process: MpvProcess | None = None
        self._ipc: MpvIpcEndpoint | None = None
        self._lifetime: ChildProcessLifetime | None = None

    def start(self) -> None:
        self.stop()

        ipc = MPV_RUNTIME.create_ipc_endpoint()
        try:
            process = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--no-terminal",
                    f"--volume={self.volume}",
                    f"--input-ipc-server={ipc.path}",
                    self.url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=MPV_RUNTIME.creation_flags(),
            )
        except Exception:
            ipc.close()
            raise

        try:
            lifetime = MPV_RUNTIME.bind_child_process_lifetime(process)
        except Exception:
            _kill_process(process)
            ipc.close()
            raise

        self.process = process
        self._ipc = ipc
        self._lifetime = lifetime

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
        ipc = self._ipc
        lifetime = self._lifetime
        self.process = None
        self._ipc = None
        self._lifetime = None

        try:
            if process is not None and process.poll() is None:
                try:
                    if ipc is None:
                        raise OSError("mpv IPC endpoint is not available")

                    ipc.command("quit")
                    process.wait()
                except FileNotFoundError, OSError:
                    _kill_process(process)
        finally:
            if lifetime is not None:
                lifetime.close()
            if ipc is not None:
                ipc.close()

    def is_running(self) -> bool:
        if self.process is None:
            return False
        if self.process.poll() is None:
            return True

        self.stop()
        return False

    def _is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

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
        self._active_ipc().command(*args)

    def _request(self, *args: object) -> dict[str, object]:
        return self._active_ipc().request(*args)

    def _active_ipc(self) -> MpvIpcEndpoint:
        if self._ipc is None:
            raise OSError("mpv IPC endpoint is not available")

        return self._ipc


def _kill_process(process: MpvProcess) -> None:
    if process.poll() is not None:
        return

    try:
        process.kill()
    except OSError:
        return

    process.wait()
