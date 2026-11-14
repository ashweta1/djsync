"""The two main operations: scan (Spotify vs rekordbox) and import (files -> rekordbox)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import files as filemod
from . import rekordbox, spotify
from .config import Config
from .matching import Index, TrackKey, file_score
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


def _scan_downloads(cfg: Config) -> list[filemod.AudioFile]:
    # Don't look inside the DJ library folder if it happens to sit under Downloads.
    return filemod.scan(cfg.watch_dir, exclude=[cfg.library_dir])


def score(t: Track, f: filemod.AudioFile) -> int:
    return file_score(t.artist, t.title, f.artist, f.title, f.text)


def candidates(cfg: Config, t: Track, limit: int = 40) -> list[tuple[filemod.AudioFile, int]]:
    """Files in Downloads ranked by how well they match ``t`` (for the attach page)."""
    ranked = [(f, score(t, f)) for f in _scan_downloads(cfg)]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:limit]


def match_files(cfg: Config, st: State) -> tuple[list[tuple[Track, Path]], list[Track]]:
    """Pair approved tracks with downloaded files.

    Files you attached by hand always win. The rest are matched on tags and
    filename, best score first, each file used at most once.
    """
    approved = st.by_status(APPROVED)
    if not approved:
        return [], []
    matched: list[tuple[Track, Path]] = []
    auto: list[Track] = []
    used: set[Path] = set()
    for t in approved:
        if t.file and Path(t.file).is_file():
            matched.append((t, Path(t.file)))
            used.add(Path(t.file).resolve())
        else:
            auto.append(t)

    audio = [f for f in _scan_downloads(cfg) if f.path.resolve() not in used] if auto else []
    pairs = []
    for ti, t in enumerate(auto):
        for fi, f in enumerate(audio):
            sc = score(t, f)
            if sc >= cfg.file_threshold:
                pairs.append((sc, ti, fi))
    pairs.sort(reverse=True)
    got_t, got_f = {}, set()
    for sc, ti, fi in pairs:
        if ti not in got_t and fi not in got_f:
            got_t[ti] = fi
            got_f.add(fi)
    waiting = []
    for ti, t in enumerate(auto):
        if ti in got_t:
            matched.append((t, audio[got_t[ti]].path))
        else:
            waiting.append(t)
    order = {t.id: i for i, t in enumerate(approved)}
    matched.sort(key=lambda m: order[m[0].id])
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
    from_downloads = cfg.watch_dir.resolve() in src.resolve().parents
    (shutil.move if cfg.move_files and from_downloads else shutil.copy2)(src, dst)
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
    else:
        # Nothing was written: undo the moves/copies so the next import starts clean.
        for src, dst in moves:
            if src == dst or not dst.exists():
                continue
            if src.exists():
                dst.unlink()  # it was a copy
            else:
                shutil.move(dst, src)
    st.save()
    return res
