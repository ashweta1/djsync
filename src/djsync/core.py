"""The two main operations: scan (Spotify vs rekordbox) and import (files -> rekordbox)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import files as filemod
from . import rekordbox, spotify
from .config import Config
from .matching import Index, TrackKey
from .state import APPROVED, IMPORTED, PENDING, State, Track


@dataclass
class ScanResult:
    liked_checked: int = 0
    new_missing: int = 0
    already_owned: int = 0


def scan(cfg: Config, st: State, sp=None, library: list | None = None) -> ScanResult:
    """Fetch Liked Songs and queue the ones not already in rekordbox."""
    sp = sp or spotify.client(cfg)
    library = library if library is not None else rekordbox.read_library(cfg)
    index = Index([TrackKey.of(t.artist, t.title) for t in library], cfg.threshold)

    res = ScanResult()
    for t in spotify.liked_songs(sp):
        res.liked_checked += 1
        if t.id in st.tracks:
            continue
        if TrackKey.of(t.artist, t.title) in index:
            res.already_owned += 1
            continue
        st.tracks[t.id] = t
        res.new_missing += 1

    # Anything still pending that has since appeared in rekordbox: drop it.
    for t in list(st.by_status(PENDING)) + list(st.by_status(APPROVED)):
        if TrackKey.of(t.artist, t.title) in index:
            del st.tracks[t.id]

    st.last_scan = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st.save()
    return res


@dataclass
class ImportResult:
    matched: list[tuple[Track, Path]] = field(default_factory=list)
    waiting: list[Track] = field(default_factory=list)
    playlist: str = ""
    method: str = ""
    xml_path: Path | None = None
    error: str = ""


def match_files(cfg: Config, st: State) -> tuple[list[tuple[Track, Path]], list[Track]]:
    approved = st.by_status(APPROVED)
    if not approved:
        return [], []
    audio = filemod.scan(cfg.watch_dir)
    index = Index([TrackKey.of(f.artist, f.title) for f in audio], cfg.threshold)
    matched, waiting, used = [], [], set()
    for t in approved:
        i = index.find(TrackKey.of(t.artist, t.title))
        if i is not None and i not in used:
            used.add(i)
            matched.append((t, audio[i].path))
        else:
            waiting.append(t)
    return matched, waiting


def _place(cfg: Config, src: Path) -> Path:
    if cfg.library_dir.resolve() in src.resolve().parents:
        return src
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    dst = cfg.library_dir / src.name
    n = 1
    while dst.exists():
        dst = cfg.library_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    (shutil.move if cfg.move_files else shutil.copy2)(src, dst)
    return dst


def do_import(cfg: Config, st: State, dry_run: bool = False) -> ImportResult:
    res = ImportResult()
    res.matched, res.waiting = match_files(cfg, st)
    if not res.matched or dry_run:
        return res

    if cfg.rekordbox_mode == "db" and rekordbox.rekordbox_running():
        res.error = "rekordbox is running. Quit rekordbox and import again."
        return res

    moves = [(p, _place(cfg, p)) for _, p in res.matched]
    placed = [(t, dst) for (t, _), (_, dst) in zip(res.matched, moves)]
    entries = [(p, t.artist, t.title) for t, p in placed]
    name = f"{cfg.playlist_prefix}{date.today():%Y-%m-%d}"

    if cfg.rekordbox_mode in ("auto", "db"):
        try:
            res.playlist = rekordbox.write_db(cfg, entries, name)
            res.method = "db"
        except Exception as e:
            if cfg.rekordbox_mode == "db":
                res.error = f"Could not write to rekordbox database: {e}"
            else:
                res.error = f"Database write failed ({e}); wrote XML instead."
    if not res.method and cfg.rekordbox_mode != "db":
        res.xml_path = rekordbox.write_xml(entries, name, cfg.home / "xml" / f"{name}.xml")
        res.playlist = name
        res.method = "xml"

    if res.method:
        for t, p in placed:
            t.status, t.file, t.playlist = IMPORTED, str(p), res.playlist
        res.matched = placed
    elif cfg.move_files:
        # Nothing was written: put the files back so the next import finds them.
        for src, dst in moves:
            if src != dst and dst.exists() and not src.exists():
                shutil.move(dst, src)
    st.save()
    return res
