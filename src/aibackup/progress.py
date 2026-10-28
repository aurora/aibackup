"""Single-line progress display on stderr (only when it is a terminal)."""

from __future__ import annotations

import shutil
import sys
import time


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{seconds:.1f}s"


class Progress:
    def __init__(self, stream=None, enabled: bool | None = None):
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.total = 0
        self.done = 0
        self.label = ""
        self.started = time.monotonic()
        self._last = 0.0
        self._shown = False

    def begin(self, total: int) -> None:
        self.total, self.done, self.started = total, 0, time.monotonic()


    def set_label(self, label: str, force: bool = False) -> None:
        """``force`` shows the label immediately, e.g. before a long step without progress."""
        self.label = label
        self._render(force)

    def advance(self, n: int) -> None:
        self.done += n
        self._render()

    def _render(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.2:
            return
        self._last = now
        elapsed = max(now - self.started, 1e-6)
        pct = min(self.done / self.total, 1.0) if self.total else 0.0
        line = f"{pct:4.0%}  {human(self.done)} / {human(self.total)}  {human(self.done / elapsed)}/s  {self.label}"
        width = max(shutil.get_terminal_size().columns - 1, 20)
        self.stream.write("\r" + line[:width].ljust(width))
        self.stream.flush()
        self._shown = True

    def clear(self) -> None:
        if self._shown:
            width = max(shutil.get_terminal_size().columns - 1, 20)
            self.stream.write("\r" + " " * width + "\r")
            self.stream.flush()
            self._shown = False


class NullProgress(Progress):
    def __init__(self):
        super().__init__(enabled=False)
