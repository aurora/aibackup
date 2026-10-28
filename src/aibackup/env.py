"""Description of the system whose agent data is backed up or restored."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

OS_NAMES = ("windows", "linux", "macos")


def current_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def sep_for(os_name: str) -> str:
    return "\\" if os_name == "windows" else "/"


@dataclass(frozen=True)
class Env:
    """Target/source system.

    ``home`` is where the home directory is reachable on *this* machine, while
    ``home_path`` is how the agents on that system see it. Both are the same
    unless the tool operates on a foreign home (e.g. a WSL home from Windows).
    """

    os: str
    home: Path
    home_path: str
    vars: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def current(cls) -> "Env":
        home = Path.home()
        return cls(current_os(), home, str(home), dict(os.environ))

    @classmethod
    def custom(cls, os_name: str | None, home: Path | None, home_path: str | None) -> "Env":
        """Env for a home other than the current user's.

        Environment variables are ignored because they describe the current system.
        """
        if os_name is None and home is None and home_path is None:
            return cls.current()
        os_name = os_name or current_os()
        home = (home or Path.home()).absolute()
        if home_path is None:
            if os_name != current_os():
                raise ValueError("--home-path is required when --platform differs from the current OS")
            home_path = str(home)
        return cls(os_name, home, home_path, {})

    @property
    def sep(self) -> str:
        return sep_for(self.os)

    def logical(self, path: Path) -> str:
        """Path as seen by agents on this system."""
        try:
            rel = path.relative_to(self.home)
        except ValueError:
            return str(path)
        return join(self.home_path, rel.parts, self.os)

    def var_path(self, name: str, default_rel: str) -> Path:
        value = self.vars.get(name)
        if value:
            return Path(value).expanduser()
        return self.home / default_rel

    # Standard base directories --------------------------------------------

    def appdata(self) -> Path:
        return self.var_path("APPDATA", "AppData/Roaming")

    def localappdata(self) -> Path:
        return self.var_path("LOCALAPPDATA", "AppData/Local")

    def xdg_data(self) -> Path:
        return self.var_path("XDG_DATA_HOME", ".local/share")

    def xdg_config(self) -> Path:
        return self.var_path("XDG_CONFIG_HOME", ".config")

    def mac_app_support(self) -> Path:
        return self.home / "Library" / "Application Support"

    def user_config_dir(self) -> Path:
        """Per-OS location used by Electron apps such as VS Code."""
        if self.os == "windows":
            return self.appdata()
        if self.os == "macos":
            return self.mac_app_support()
        return self.xdg_config()


def join(base: str, parts: tuple[str, ...] | list[str], os_name: str) -> str:
    sep = sep_for(os_name)
    if not parts:
        return base
    stripped = base.rstrip("/\\") if base not in ("/", "\\") else ""
    return stripped + sep + sep.join(parts)
