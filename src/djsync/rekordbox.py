"""Read the rekordbox collection and add tracks + a playlist.

Two ways to write:
  * db  - directly into rekordbox 6/7's master.db via pyrekordbox. rekordbox must
          be closed. master.db is backed up first.
  * xml - write a rekordbox XML file you import via
          Preferences > Advanced > Database > rekordbox xml.
"""

from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .config import Config


@dataclass
class LibraryTrack:
    artist: str
    title: str
    path: str


def _open_db(cfg: Config):
    try:
        from pyrekordbox import MasterDatabase as DB
    except ImportError:  # pyrekordbox < 0.5
        from pyrekordbox import Rekordbox6Database as DB
    if cfg.rekordbox_db_path:
        return DB(path=cfg.rekordbox_db_path)
    return DB()


def read_library(cfg: Config) -> list[LibraryTrack]:
    db = _open_db(cfg)
    try:
        out = []
        for c in db.get_content():
            artist = ""
            try:
                artist = c.ArtistName or ""
            except Exception:
                pass
            out.append(LibraryTrack(artist=artist, title=c.Title or "", path=c.FolderPath or ""))
        return out
    finally:
        db.close()


def rekordbox_running() -> bool:
    try:
        from pyrekordbox.utils import get_rekordbox_pid

        return bool(get_rekordbox_pid())
    except Exception:
        return False


def _backup(db, cfg: Config) -> Path | None:
    src = Path(getattr(db, "db_directory", "")) / "master.db"
    if not src.exists():
        return None
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    dst = cfg.backup_dir / f"master-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(src, dst)
    return dst


def _unique_playlist_name(existing: set[str], name: str) -> str:
    if name not in existing:
        return name
    n = 2
    while f"{name}-{n}" in existing:
        n += 1
    return f"{name}-{n}"


def playlist_exists(cfg: Config, name: str) -> bool:
    db = _open_db(cfg)
    try:
        return db.get_playlist(Name=name).first() is not None
    finally:
        db.close()


def write_db(cfg: Config, files: list[tuple[Path, str, str]], playlist_name: str) -> str:
    """Add files to rekordbox's database and put them in a new playlist.

    ``files`` is a list of (path, artist, title). Returns the playlist name used.
    """
    if rekordbox_running():
        raise RuntimeError("rekordbox is running. Quit rekordbox and try again.")
    db = _open_db(cfg)
    try:
        _backup(db, cfg)
        existing = {p.Name for p in db.get_playlist()}
        playlist_name = _unique_playlist_name(existing, playlist_name)
        playlist = db.create_playlist(playlist_name)
        for path, artist, title in files:
            found = db.get_content(FolderPath=str(path)).first()
            if found is None:
                kwargs = {"Title": title or path.stem}
                if artist:
                    a = db.get_artist(Name=artist).first()
                    if a is None:
                        a = db.add_artist(artist)
                    kwargs["ArtistID"] = a.ID
                found = db.add_content(str(path), **kwargs)
            db.add_to_playlist(playlist, found)
        db.commit()
        return playlist_name
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _location(path: Path) -> str:
    return "file://localhost" + quote(str(path.resolve()).replace(os.sep, "/"))


def write_xml(files: list[tuple[Path, str, str]], playlist_name: str, out: Path) -> Path:
    """Write a rekordbox-importable XML with the tracks and one playlist."""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.0.0", Company="AlphaTheta")
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(files)))
    for i, (path, artist, title) in enumerate(files, start=1):
        ET.SubElement(
            coll, "TRACK", TrackID=str(i), Name=title or path.stem, Artist=artist,
            Kind=path.suffix.lstrip(".").upper() + " File", Location=_location(path),
        )
    pls = ET.SubElement(root, "PLAYLISTS")
    rootnode = ET.SubElement(pls, "NODE", Type="0", Name="ROOT", Count="1")
    node = ET.SubElement(
        rootnode, "NODE", Name=playlist_name, Type="1", KeyType="0", Entries=str(len(files))
    )
    for i in range(1, len(files) + 1):
        ET.SubElement(node, "TRACK", Key=str(i))
    ET.indent(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out, encoding="UTF-8", xml_declaration=True)
    return out
