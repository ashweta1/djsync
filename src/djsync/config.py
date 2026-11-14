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
playlist_prefix = "downloads-"

[match]
# 0-100. Higher = stricter matching between Spotify and your library.
threshold = 88

[stores]
# Stores to show buy links for, in order.
enabled = ["beatport", "bandcamp", "traxsource", "juno"]

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
    rekordbox_mode: str = "auto"
    rekordbox_db_path: str = ""
    playlist_prefix: str = "downloads-"
    threshold: int = 88
    stores: list[str] = field(
        default_factory=lambda: ["beatport", "bandcamp", "traxsource", "juno"]
    )
    port: int = 5055
    home: Path = HOME

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
    cfg.rekordbox_mode = rb.get("mode", "auto")
    if cfg.rekordbox_mode not in ("auto", "db", "xml"):
        raise ValueError("[rekordbox] mode must be auto, db or xml")
    cfg.rekordbox_db_path = rb.get("db_path", "")
    cfg.playlist_prefix = rb.get("playlist_prefix", "downloads-")
    cfg.threshold = int(data.get("match", {}).get("threshold", 88))
    cfg.stores = list(data.get("stores", {}).get("enabled", cfg.stores))
    cfg.port = int(data.get("web", {}).get("port", 5055))
    return cfg
