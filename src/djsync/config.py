"""Configuration loading. Config lives in ~/.djsync/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("DJSYNC_HOME", Path.home() / ".djsync"))
CONFIG_PATH = HOME / "config.toml"

DEFAULT_CONFIG = """\
# djsync configuration

[spotify]
# Create an app at https://developer.spotify.com/dashboard
# and add this exact Redirect URI to it.
client_id = ""
redirect_uri = "http://127.0.0.1:8888/callback"

[paths]
# Folder your store purchases download into. djsync scans it for approved tracks.
watch_dir = "~/Downloads"
# Other folders to look in for purchased tracks (e.g. iTunes/Apple Music purchases
# land in ~/Music). Only files from the last `recent_days` days are considered, and
# they're copied into your DJ folder on import, never moved.
extra_dirs = ["~/Music"]
recent_days = 90
# Where imported tracks are moved to (your DJ music folder).
library_dir = "~/Music/DJ"
# true = move files out of watch_dir, false = copy them.
move_files = true

[rekordbox]
# "auto": write directly to rekordbox's database (rekordbox must be closed),
#         falling back to an XML file if that fails.
# "db":   database only.   "xml": XML file only.
mode = "auto"
# Optional: path to master.db if pyrekordbox can't find it automatically.
db_path = ""
playlist_prefix = "djsync-downloads-"

[match]
# 0-100. Higher = stricter matching between Spotify and your library.
threshold = 88
# 0-100. How closely a downloaded file must match an approved track. Files are
# matched on tags AND filename, so this can be looser. Lower it if files aren't
# found; raise it if the wrong file gets picked.
file_threshold = 75

[stores]
# Stores to show buy links for, in order.
enabled = ["beatport", "bandcamp", "deezer"]

[agent]
# The research agent searches Beatport for each approved track, picks the best
# match and gives you a direct link (another store, or Deezer, if Beatport
# doesn't have it).
#   backend = "claude-code": uses Claude Code on this Mac, i.e. your Claude Pro/Max
#                            plan. Install Claude Code and sign in once with `claude`.
#   backend = "api":         uses the Claude API with ANTHROPIC_API_KEY (pay per use).
#   backend = "auto":        Claude Code if installed, else the API.
backend = "auto"
# Claude Code only: model to use - "sonnet" (default), "haiku" (lighter on your
# plan's allowance), "opus" (most thorough), or "" for Claude Code's own default.
claude_code_model = "sonnet"
enabled = true
# Research tracks automatically as soon as you approve them.
auto = true
# API only:
model = "claude-opus-5-5"
# API only: low | medium | high - higher is more thorough and costs more
effort = "medium"

[web]
port = 5055
"""


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG)
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
    if cfg.agent_backend not in ("auto", "claude-code", "api"):
        raise ValueError('[agent] backend must be "auto", "claude-code" or "api"')
    cfg.claude_code_model = ag.get("claude_code_model", "sonnet")
    cfg.claude_path = ag.get("claude_path", "")
    return cfg
