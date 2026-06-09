import json
import subprocess

PIPE_PATH = r"\\.\pipe\mpv-control"


class AudioPlayer:
    def __init__(self, url: str, *, volume: int = 100) -> None:
        self.url = url
        self.volume = volume
        self.process = None

    def start(self) -> None:
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
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

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
        if process is None or process.poll() is not None:
            return

        try:
            self._command("quit")
        except FileNotFoundError, OSError:
            process.terminate()

    def is_running(self) -> bool:
        return self._is_running()

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
