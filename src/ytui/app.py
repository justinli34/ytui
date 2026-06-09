from __future__ import annotations

from io import StringIO
from threading import Lock, Thread
from time import monotonic
from typing import Literal

from blessed import Terminal
from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ytui import DOWNLOADS_DIR
from ytui.download import download_audio
from ytui.models import VideoSearchResult
from ytui.playback import AudioPlayer
from ytui.search import search

SEARCH_CONTROLS_HELP_LINES = (
    ("Ctrl+P", "show/hide controls"),
    ("Enter", "search"),
    ("Down", "focus results"),
    ("Shift+Up/Down", "change volume"),
    ("Esc", "quit"),
)
RESULTS_CONTROLS_HELP_LINES = (
    ("Ctrl+P", "show/hide controls"),
    ("Enter", "play highlighted result"),
    ("Space", "pause/resume playback"),
    ("Ctrl+S", "download highlighted result"),
    ("Up/Down", "select result"),
    ("Shift+Up/Down", "change volume"),
    ("Left/Right", "scrub playback"),
    ("/", "focus search"),
    ("Esc", "quit"),
)
SEARCH_RESULT_COUNT = 15
PROGRESS_UPDATE_INTERVAL = 0.25
DOWNLOAD_COMPLETE_MESSAGE_SECONDS = 5.0
SEARCH_CURSOR_BLINK_INTERVAL = 0.5
SCRUB_SECONDS = 10
VOLUME_STEP = 5
MIN_VOLUME = 0
MAX_VOLUME = 100
FocusPanel = Literal["search", "results"]
RESULTS_TABLE_BOX = box.Box("╭──╮\n│  │\n├──┤\n│  │\n├──┤\n├──┤\n│  │\n╰──╯\n")


class App:
    def __init__(self) -> None:
        self.term = Terminal()
        self.query = ""
        self.last_search_query = ""
        self.focus: FocusPanel = "search"
        self.results: list[VideoSearchResult] = []
        self.selected_index = 0
        self.search_error_message = ""
        self.playback_error_message = ""
        self.download_message = ""
        self.download_lock = Lock()
        self.download_thread: Thread | None = None
        self.download_needs_render = False
        self.download_message_expires_at: float | None = None
        self.downloading_result: VideoSearchResult | None = None
        self.is_searching = False
        self.player: AudioPlayer | None = None
        self.playing_result: VideoSearchResult | None = None
        self.is_paused = False
        self.playback_position = 0.0
        self.playback_duration: float | None = None
        self.volume = 100
        self.show_controls_help = False
        self.search_cursor_visible = True

    def run(self) -> None:
        try:
            with self.term.fullscreen(), self.term.cbreak(), self.term.hidden_cursor():
                self._render()
                next_progress_update = monotonic() + PROGRESS_UPDATE_INTERVAL
                next_cursor_blink = monotonic() + SEARCH_CURSOR_BLINK_INTERVAL

                while True:
                    now = monotonic()
                    if now >= next_progress_update:
                        if self._sync_playback_status() or self._sync_download_status():
                            self._render()

                        next_progress_update += PROGRESS_UPDATE_INTERVAL
                        if next_progress_update <= now:
                            next_progress_update = now + PROGRESS_UPDATE_INTERVAL

                    if now >= next_cursor_blink:
                        if self.focus == "search":
                            self.search_cursor_visible = not self.search_cursor_visible
                            self._render()
                        else:
                            self.search_cursor_visible = True

                        next_cursor_blink += SEARCH_CURSOR_BLINK_INTERVAL
                        if next_cursor_blink <= now:
                            next_cursor_blink = now + SEARCH_CURSOR_BLINK_INTERVAL

                    timeout = max(
                        0.0,
                        min(next_progress_update, next_cursor_blink) - monotonic(),
                    )
                    key = self.term.inkey(timeout=timeout)

                    if not key:
                        continue

                    if key.name in {"KEY_ESCAPE", "KEY_EXIT"} or key == "\x03":
                        break

                    if self._handle_global_key(key):
                        continue

                    if self.focus == "search":
                        self._handle_search_key(key)
                    else:
                        self._handle_results_key(key)
        finally:
            self._stop_player()

    def _handle_search_key(self, key: object) -> None:
        self.search_cursor_visible = True

        if getattr(key, "name", None) == "KEY_ENTER" or key == "\n":
            self._search()
            return

        if getattr(key, "name", None) == "KEY_DOWN":
            if self.results:
                self.focus = "results"
                self._render()
            return

        if getattr(key, "name", None) in {"KEY_BACKSPACE", "KEY_DELETE"} or key in {
            "\b",
            "\x7f",
        }:
            self.query = self.query[:-1]
            self._render()
            return

        if getattr(key, "is_sequence", False):
            return

        text = str(key)
        if text.isprintable():
            self.query += text
            self._render()

    def _handle_results_key(self, key: object) -> None:
        if getattr(key, "name", None) == "KEY_ENTER" or key == "\n":
            self._play_selected_result()
        elif getattr(key, "name", None) == "KEY_UP":
            self._move_selection(-1)
        elif getattr(key, "name", None) == "KEY_DOWN":
            self._move_selection(1)
        elif getattr(key, "name", None) == "KEY_LEFT":
            self._scrub_current_playback(-SCRUB_SECONDS)
        elif getattr(key, "name", None) == "KEY_RIGHT":
            self._scrub_current_playback(SCRUB_SECONDS)
        elif key == " ":
            self._toggle_current_playback()
        elif key == "\x13":
            self._download_selected_result()
        elif key == "/":
            self.focus = "search"
            self.search_cursor_visible = True
            self._render()

    def _handle_global_key(self, key: object) -> bool:
        key_name = getattr(key, "name", None)
        if key_name == "KEY_CTRL_P" or key == "\x10":
            self.show_controls_help = not self.show_controls_help
            self._render()
            return True
        if key_name in {"KEY_SUP", "KEY_SHIFT_UP"}:
            self._change_volume(VOLUME_STEP)
            return True
        if key_name in {"KEY_SDOWN", "KEY_SHIFT_DOWN"}:
            self._change_volume(-VOLUME_STEP)
            return True

        return False

    def _search(self) -> None:
        query = self.query.strip()
        self.search_error_message = ""
        if not query:
            self.results = []
            self.selected_index = 0
            self.focus = "search"
            self._render()
            return

        self.is_searching = True
        self._render()

        try:
            self.results = search(query, SEARCH_RESULT_COUNT)
        except Exception as error:
            self.results = []
            self.selected_index = 0
            self.focus = "search"
            self.search_error_message = f"Search failed: {error}"
        else:
            self.selected_index = 0
            self.last_search_query = query
            self.focus = "results" if self.results else "search"
        finally:
            self.is_searching = False
            self._render()

    def _play_selected_result(self) -> None:
        if not self.results:
            self._render()
            return

        result = self.results[self.selected_index]
        if self.playing_result is result and self.player is not None and self.player.is_running():
            if self.is_paused:
                self._toggle_pause()
            return

        self._play_result(result)

    def _toggle_current_playback(self) -> None:
        if self.player is None:
            self._render()
            return

        self._toggle_pause()

    def _scrub_current_playback(self, seconds: int) -> None:
        if self.player is None or not self.player.is_running():
            self._render()
            return

        try:
            self.player.seek(seconds)
        except Exception as error:
            self.playback_error_message = f"Playback seek failed: {error}"
        else:
            self.playback_error_message = ""

        self._sync_playback_status(force=True)
        self._render()

    def _change_volume(self, offset: int) -> None:
        volume = max(MIN_VOLUME, min(MAX_VOLUME, self.volume + offset))
        player_is_running = self.player is not None and self.player.is_running()

        if player_is_running:
            try:
                self.player.set_volume(volume)
            except Exception as error:
                self.playback_error_message = f"Volume change failed: {error}"
                self._render()
                return
            else:
                self.playback_error_message = ""

        self.volume = volume

        self._sync_playback_status(force=True)
        self._render()

    def _download_selected_result(self) -> None:
        if not self.results:
            self._render()
            return

        with self.download_lock:
            if self.download_thread is not None and self.download_thread.is_alive():
                if self.downloading_result is not None:
                    self.download_message = f"Already downloading: {self.downloading_result.title}"
                else:
                    self.download_message = "A download is already running."
                self.download_message_expires_at = None
                self._render()
                return

            result = self.results[self.selected_index]
            self.downloading_result = result
            self.download_message = f"Downloading: {result.title}"
            self.download_message_expires_at = None
            self.download_needs_render = False
            self.download_thread = Thread(
                target=self._download_result,
                args=(result,),
                daemon=True,
            )
            self.download_thread.start()

        self._render()

    def _download_result(self, result: VideoSearchResult) -> None:
        try:
            download_audio(result.url)
        except Exception as error:
            message = f"Download failed: {error}"
            expires_at = None
        else:
            message = f"Downloaded to {DOWNLOADS_DIR}"
            expires_at = monotonic() + DOWNLOAD_COMPLETE_MESSAGE_SECONDS

        with self.download_lock:
            self.download_message = message
            self.download_message_expires_at = expires_at
            self.downloading_result = None
            self.download_needs_render = True

    def _sync_download_status(self) -> bool:
        with self.download_lock:
            should_render = self.download_needs_render
            self.download_needs_render = False
            if (
                self.download_message
                and self.download_message_expires_at is not None
                and monotonic() >= self.download_message_expires_at
            ):
                self.download_message = ""
                self.download_message_expires_at = None
                should_render = True

        return should_render

    def _play_result(self, result: VideoSearchResult) -> None:
        self._stop_player()
        player = AudioPlayer(result.url, volume=self.volume)

        try:
            player.start()
        except Exception as error:
            self.playback_error_message = f"Could not start playback: {error}"
            self._render()
            return

        self.player = player
        self.playing_result = result
        self.is_paused = False
        self.playback_position = 0.0
        self.playback_duration = result.duration
        self.playback_error_message = ""
        self._sync_playback_status(force=True)
        self._render()

    def _toggle_pause(self) -> None:
        if self.player is None:
            return

        try:
            if self.is_paused:
                self.player.resume()
                self.is_paused = False
            else:
                self.player.pause()
                self.is_paused = True
        except Exception as error:
            self.playback_error_message = f"Playback control failed: {error}"
        else:
            self.playback_error_message = ""

        self._sync_playback_status(force=True)
        self._render()

    def _stop_player(self) -> None:
        if self.player is None:
            return

        try:
            self.player.stop()
        except OSError:
            pass

        self.player = None
        self.is_paused = False

    def _sync_playback_status(self, *, force: bool = False) -> bool:
        if self.player is None:
            return False

        previous_state = self._playback_render_state()
        if not self.player.is_running():
            self.player = None
            self.is_paused = False
            return True

        position = self.player.position()
        duration = self.player.duration()
        if position is not None:
            self.playback_position = position
        if duration is not None:
            self.playback_duration = duration

        return force or previous_state != self._playback_render_state()

    def _move_selection(self, offset: int) -> None:
        if not self.results:
            return

        self.selected_index = (self.selected_index + offset) % len(self.results)
        self._render()

    def _render(self) -> None:
        print(self.term.home + self.term.clear, end="")
        print(self._rich_render(), end="", flush=True)

    def _rich_render(self) -> str:
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=max(self.term.width, 40),
            height=max(self.term.height, 12),
        )

        console.print(Align.center(self._header_panel(), width=console.width))
        console.print(self._results_status_table())
        console.print(self._footer_line(console.width))

        if self._error_message():
            output.write(self._error_message_overlay())
        elif self.download_message:
            output.write(self._download_message_overlay())
        if self.show_controls_help:
            output.write(self._controls_help_overlay())

        return output.getvalue()

    def _footer_line(self, width: int) -> Text:
        controls_hint = "Ctrl+P: controls"
        volume_label = f"Volume: {self.volume}%"
        gap = max(1, width - len(controls_hint) - len(volume_label))
        return Text(f"{volume_label}{' ' * gap}{controls_hint}", style="dim")

    def _header_panel(self) -> Panel:
        prompt = Text()
        prompt.append("Search: ", style=f"bold {self._focus_color('search')}")
        prompt.append(self.query)
        if self.focus == "search":
            prompt.append("|" if self.search_cursor_visible else " ")

        body = Text()
        body.append_text(prompt)

        return Panel(
            body,
            title="ytui",
            border_style=self._focus_border("search"),
            padding=(0, 1),
            box=box.ROUNDED,
            width=max(32, round(self.term.width * 0.8)),
        )

    def _results_status_table(self) -> Table:
        table = Table(
            expand=True,
            show_header=False,
            show_lines=True,
            border_style=self._focus_border("results"),
            box=RESULTS_TABLE_BOX,
            padding=(0, 1),
        )
        table.add_column()
        table.add_row(self._results_table())
        table.add_row(self._status_body())
        return table

    def _results_table(self) -> Table:
        table = Table(
            expand=True,
            show_header=False,
            box=None,
            pad_edge=False,
        )
        table.add_column("Title", ratio=4, overflow="ellipsis", no_wrap=True)
        table.add_column("Channel", ratio=2, overflow="ellipsis", no_wrap=True)
        table.add_column("Duration", width=8, no_wrap=True)

        if not self.results:
            table.add_row("No results yet.", "", "")
            return table

        visible_count = self._visible_result_count()
        start_index = min(
            max(0, self.selected_index - visible_count + 1),
            max(0, len(self.results) - visible_count),
        )
        visible_results = self.results[start_index : start_index + visible_count]

        for index, result in enumerate(visible_results, start=start_index):
            is_selected = index == self.selected_index
            style = f"black on {self._focus_color('results')}" if is_selected else None

            table.add_row(
                result.title,
                result.channel,
                _format_duration(result.duration),
                style=style,
            )

        return table

    def _status_body(self) -> RenderableType:

        if self.playing_result is None:
            title = "No track selected"
        elif self.player is not None and self.is_paused:
            title = f"Paused: {self.playing_result.title}"
        elif self.player is not None:
            title = f"Playing: {self.playing_result.title}"
        else:
            title = self.playing_result.title
        duration = self.playback_duration
        position = self.playback_position
        if duration is not None and duration > 0:
            position = min(position, duration)

        lines: list[RenderableType] = [
            Align.center(Text(_truncate(title, max(10, self.term.width - 18)))),
        ]

        position_label = _format_duration(position)
        duration_label = _format_duration(duration)
        time_width = max(len(position_label), len(duration_label))
        progress_line = Text()
        progress_line.append(f"{position_label:>{time_width}} ", style="dim")
        progress_line.append(
            _progress_bar(position, duration, max(10, min(40, self.term.width - 22)))
        )
        progress_line.append(f" {duration_label:<{time_width}}", style="dim")
        lines.append(Align.center(progress_line))

        return Group(*lines)

    def _controls_help_panel(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold red", no_wrap=True)
        table.add_column(style="white")

        for key, description in self._controls_help_lines():
            table.add_row(key, description)

        return Panel(
            table,
            title="Controls",
            border_style="red",
            padding=(0, 1),
            box=box.ROUNDED,
        )

    def _controls_help_lines(self) -> tuple[tuple[str, str], ...]:
        if self.focus == "results":
            return RESULTS_CONTROLS_HELP_LINES

        return SEARCH_CONTROLS_HELP_LINES

    def _download_message_panel(self) -> Panel:
        message_width = max(10, min(52, self.term.width - 8))
        return Panel(
            Text(_truncate(self.download_message, message_width), style="white"),
            title="Download",
            border_style="green",
            padding=(0, 1),
            box=box.ROUNDED,
        )

    def _download_message_overlay(self) -> str:
        return self._bottom_left_overlay(self._download_message_panel())

    def _error_message(self) -> str:
        return self.playback_error_message or self.search_error_message

    def _error_message_panel(self) -> Panel:
        message_width = max(10, min(52, self.term.width - 8))
        return Panel(
            Text(_truncate(self._error_message(), message_width), style="yellow"),
            title="Error",
            border_style="yellow",
            padding=(0, 1),
            box=box.ROUNDED,
        )

    def _error_message_overlay(self) -> str:
        return self._bottom_left_overlay(self._error_message_panel())

    def _bottom_left_overlay(self, panel: Panel) -> str:
        overlay_width = min(58, max(24, self.term.width - 4))
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=overlay_width,
        )
        console.print(panel)

        lines = output.getvalue().splitlines()
        row = max(0, self.term.height - len(lines))
        column = 0

        overlay = StringIO()
        for offset, line in enumerate(lines):
            overlay.write(self.term.move_yx(row + offset, column))
            overlay.write(line)

        return overlay.getvalue()

    def _controls_help_overlay(self) -> str:
        overlay_width = min(46, max(34, self.term.width - 4))
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=overlay_width,
        )
        console.print(self._controls_help_panel())

        lines = output.getvalue().splitlines()
        row = max(0, self.term.height - len(lines))
        column = max(0, self.term.width - overlay_width)

        overlay = StringIO()
        for offset, line in enumerate(lines):
            overlay.write(self.term.move_yx(row + offset, column))
            overlay.write(line)

        return overlay.getvalue()

    def _focus_border(self, panel: FocusPanel) -> str:
        return "red" if self.focus == panel else "white"

    def _focus_color(self, panel: FocusPanel) -> str:
        return "red" if self.focus == panel else "white"

    def _visible_result_count(self) -> int:
        reserved_rows = 12
        return max(1, self.term.height - reserved_rows)

    def _playback_render_state(self) -> tuple[object, ...]:
        duration = self.playback_duration
        position = self.playback_position
        if duration is not None and duration > 0:
            position = min(position, duration)

        return (
            self.player is not None,
            self.is_paused,
            self.playing_result.title if self.playing_result is not None else None,
            self.playback_error_message,
            self.volume,
            _format_duration(position),
            _format_duration(duration),
        )


def _progress_bar(position: float, duration: float | None, width: int) -> Text:
    if duration is None or duration <= 0:
        completed = 0
    else:
        completed = round((position / duration) * width)

    completed = max(0, min(width, completed))

    bar = Text()
    bar.append("━" * completed)
    bar.append("─" * (width - completed), style="dim")
    return bar


def _format_duration(duration: float | None) -> str:
    if duration is None:
        return "-"

    seconds = int(duration)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value

    if width <= 3:
        return "." * width

    return f"{value[: width - 3]}..."
