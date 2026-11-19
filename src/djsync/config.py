"""Configuration loading. Config lives in ~/.djsync/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("DJSYNC_HOME", Path.home() / ".djsync"))
CONFIG_PATH = HOME / "config.toml"



@dataclass
class Config:
    client_id: str = ""
    redirect_uri: str = "http://127.0.0.1:8888/callback"
    watch_dir: Path = Path("~/Downloads").expanduser()
    library_dir: Path = Path("~/Music/DJ").expanduser()
    move_files: bool = True
    extra_dirs: list[Path] = field(default_factory=lambda: [Path("~/Music").expanduser()])
    recent_days: int = 90
    rekordbox_mode: str = "auto"
    rekordbox_db_path: str = ""
    playlist_prefix: str = "djsync-downloads-"
    threshold: int = 88
    file_threshold: int = 75
    stores: list[str] = field(
        default_factory=lambda: ["beatport", "bandcamp", "deezer"]
    )
    port: int = 5055
    agent_enabled: bool = True
    agent_auto: bool = True
    agent_model: str = "claude-opus-5-5"
    agent_effort: str = "medium"
    agent_backend: str = "auto"
    beatport_direct: bool = True
    claude_code_model: str = "sonnet"
    claude_path: str = ""
    home: Path = HOME

    @property
    def search_dirs(self) -> list[Path]:
        """Downloads first, then the extra folders (no duplicates)."""
        out = []
        for d in [self.watch_dir, *self.extra_dirs]:
            if d not in out:
                out.append(d)
        return out

    @property
    def state_path(self) -> Path:
        return self.home / "state.json"

    @property
    def spotify_cache(self) -> Path:
        return self.home / "spotify_token.json"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"


def write_default(path: Path = CONFIG_PATH) -> bool:
    """Write a starter config. Returns False if one already exists."""
    if path.exists():
        return False
    save(Config(home=path.parent), path)
    return True


def load(path: Path = CONFIG_PATH) -> Config:
    data: dict = {}
    if path.exists():
        data = tomllib.loads(path.read_text())
    sp = data.get("spotify", {})
    paths = data.get("paths", {})
    rb = data.get("rekordbox", {})
    cfg = Config(home=path.parent)
    cfg.client_id = os.environ.get("SPOTIPY_CLIENT_ID", sp.get("client_id", ""))
    cfg.redirect_uri = sp.get("redirect_uri", cfg.redirect_uri)
    cfg.watch_dir = Path(paths.get("watch_dir", "~/Downloads")).expanduser()
    cfg.library_dir = Path(paths.get("library_dir", "~/Music/DJ")).expanduser()
    cfg.move_files = bool(paths.get("move_files", True))
    cfg.extra_dirs = [Path(d).expanduser() for d in paths.get("extra_dirs", ["~/Music"])]
    cfg.recent_days = int(paths.get("recent_days", 90))
    cfg.rekordbox_mode = rb.get("mode", "auto")
    if cfg.rekordbox_mode not in ("auto", "db", "xml"):
        raise ValueError("[rekordbox] mode must be auto, db or xml")
    cfg.rekordbox_db_path = rb.get("db_path", "")
    cfg.playlist_prefix = rb.get("playlist_prefix", "djsync-downloads-")
    cfg.threshold = int(data.get("match", {}).get("threshold", 88))
    cfg.file_threshold = int(data.get("match", {}).get("file_threshold", 75))
    cfg.stores = list(data.get("stores", {}).get("enabled", cfg.stores))
    cfg.port = int(data.get("web", {}).get("port", 5055))
    ag = data.get("agent", {})
    cfg.agent_enabled = bool(ag.get("enabled", True))
    cfg.agent_auto = bool(ag.get("auto", True))
    cfg.agent_model = ag.get("model", cfg.agent_model)
    cfg.agent_effort = ag.get("effort", cfg.agent_effort)
    cfg.agent_backend = ag.get("backend", "auto")
    cfg.beatport_direct = bool(ag.get("beatport_search", True))
    if cfg.agent_backend not in ("auto", "claude-code", "api"):
        raise ValueError('[agent] backend must be "auto", "claude-code" or "api"')
    cfg.claude_code_model = ag.get("claude_code_model", "sonnet")
    cfg.claude_path = ag.get("claude_path", "")
    return cfg


def _q(value) -> str:
    """A TOML value. JSON strings and lists are valid TOML for what we store."""
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Path):
        value = _short(value)
    if isinstance(value, list):
        return "[" + ", ".join(_q(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _short(p: Path) -> str:
    """/Users/me/Music -> ~/Music, so the file reads nicely and survives a username change."""
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def dump(cfg: Config) -> str:
    """The config as a commented config.toml."""
    return f"""\
# djsync configuration. You can also change all of this on the Settings page.

[spotify]
# Create an app at https://developer.spotify.com/dashboard
# and add this exact Redirect URI to it.
client_id = {_q(cfg.client_id)}
redirect_uri = {_q(cfg.redirect_uri)}

[paths]
# Folder your store purchases download into. Files here are moved on import.
watch_dir = {_q(cfg.watch_dir)}
# Other folders to look in (e.g. Apple Music purchases land in ~/Music). Only files
# from the last `recent_days` days count, and they're copied on import, never moved.
extra_dirs = {_q(cfg.extra_dirs)}
recent_days = {_q(cfg.recent_days)}
# Where imported tracks go (your DJ music folder).
library_dir = {_q(cfg.library_dir)}
# true = move files out of the downloads folder, false = copy them.
move_files = {_q(cfg.move_files)}

[rekordbox]
# "auto": write straight into rekordbox (it must be closed), or an XML file if that fails.
# "db": rekordbox only.   "xml": XML file only.
mode = {_q(cfg.rekordbox_mode)}
# Path to master.db, only if djsync can't find it by itself.
db_path = {_q(cfg.rekordbox_db_path)}
playlist_prefix = {_q(cfg.playlist_prefix)}

[match]
# 0-100. Higher = stricter matching between Spotify and your library.
threshold = {_q(cfg.threshold)}
# 0-100. How closely a downloaded file must match an approved track.
file_threshold = {_q(cfg.file_threshold)}

[stores]
# Stores to show links for, in order.
enabled = {_q(cfg.stores)}

[agent]
# The research agent finds the best place to buy each approved track.
#   "claude-code": Claude Code on this computer (your Claude Pro/Max plan)
#   "api":         the Claude API with ANTHROPIC_API_KEY (pay per use)
#   "auto":        Claude Code if it's installed, otherwise the API
backend = {_q(cfg.agent_backend)}
# Claude Code only: "sonnet", "haiku", "opus", or "" for Claude Code's default.
claude_code_model = {_q(cfg.claude_code_model)}
# Claude Code only: full path to the `claude` command, if djsync can't find it.
claude_path = {_q(cfg.claude_path)}
# Search Beatport's own catalogue first.
beatport_search = {_q(cfg.beatport_direct)}
enabled = {_q(cfg.agent_enabled)}
# Research tracks as soon as you approve them.
auto = {_q(cfg.agent_auto)}
# API only:
model = {_q(cfg.agent_model)}
effort = {_q(cfg.agent_effort)}

[web]
port = {_q(cfg.port)}
"""


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or (cfg.home / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump(cfg))
    tmp.replace(path)
    return path
