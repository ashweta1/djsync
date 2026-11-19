"""The two main operations: scan (Spotify vs rekordbox) and import (files -> rekordbox)."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import files as filemod
from . import rekordbox, spotify
from .config import Config
from .matching import Index, TrackKey, file_score
from .state import APPROVED, IMPORTED, IN_REKORDBOX, PENDING, State, Track


@dataclass
class ScanResult:
    liked_checked: int = 0      # tracks checked across all sources
    new_missing: int = 0
    already_owned: int = 0
    now_in_rekordbox: int = 0
    sources: int = 0
    errors: list[str] = field(default_factory=list)


def scan(cfg: Config, st: State, sp=None, library: list | None = None,
         sources: list[str] | None = None) -> ScanResult:
    """Read the chosen Spotify sources and queue tracks not already in rekordbox.

    ``sources`` is "liked" and/or playlist ids; it's remembered for next time.
    Defaults to the last choice (Liked Songs at first).
    """
    sp = sp or spotify.client(cfg)
    library = library if library is not None else rekordbox.read_library(cfg)
    index = Index([TrackKey.of(t.artist, t.title) for t in library], cfg.threshold)
    res = ScanResult()

    if sources is not None:
        st.sources = [s for s in dict.fromkeys(sources) if s] or [spotify.LIKED]
    # Refresh the playlist list, so the picker shows new playlists.
    listed = True
    try:
        st.playlists = spotify.playlists(sp)
    except Exception as e:
        listed = False
        res.errors.append(f"Couldn't list your playlists: {e}")
    names = {p["id"]: p["name"] for p in st.playlists}

    def tracks():
        for src in st.sources:
            if src == spotify.LIKED:
                res.sources += 1
                yield from spotify.liked_songs(sp)
                continue
            if listed and src not in names:
                continue  # playlist deleted or unfollowed
            res.sources += 1
            try:
                yield from spotify.playlist_tracks(sp, src, names.get(src, "playlist"))
            except Exception as e:
                # e.g. Spotify's own generated playlists, which apps can't read
                res.errors.append(f"Couldn't read playlist '{names.get(src, src)}': {e}")

    seen: set[str] = set()
    for t in tracks():
        if t.id in seen:
            continue  # same track in several playlists
        seen.add(t.id)
        res.liked_checked += 1
        if t.id in st.tracks:
            continue
        if TrackKey.of(t.artist, t.title) in index:
            res.already_owned += 1
            continue
        st.tracks[t.id] = t
        res.new_missing += 1

    # Tracks you were still reviewing or waiting on that have since appeared in
    # rekordbox (added outside djsync): keep them visible, just mark them.
    for t in list(st.by_status(PENDING)) + list(st.by_status(APPROVED)):
        if TrackKey.of(t.artist, t.title) in index:
            t.status = IN_REKORDBOX
            res.now_in_rekordbox += 1

    st.last_scan = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st.last_scan_summary = (f"{res.new_missing} new" if res.new_missing else "nothing new") + (
        f", {len(res.errors)} problem{'s' if len(res.errors) != 1 else ''}" if res.errors else "")
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
    import time

    found, seen = [], set()
    for d in cfg.search_dirs:
        is_downloads = d == cfg.watch_dir
        # Skip the DJ folder (already imported) and, when walking ~/Music, Downloads too.
        exclude = [cfg.library_dir] + [x for x in cfg.search_dirs if x != d]
        files = filemod.scan(
            d, exclude=exclude, unpack=is_downloads,
            newer_than=None if is_downloads else time.time() - cfg.recent_days * 86400,
        )
        for f in files:
            key = f.path.resolve()
            if key not in seen:
                seen.add(key)
                found.append(f)
    return found


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
    # Best score first; on a tie, the earlier file (Downloads before ~/Music, newest first).
    pairs.sort(key=lambda p: (-p[0], p[2], p[1]))
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


def do_import(cfg: Config, st: State, dry_run: bool = False,
              only: set[str] | None = None) -> ImportResult:
    """Import matched files into today's playlist. ``only`` limits it to those track ids."""
    res = ImportResult()
    res.matched, res.waiting = match_files(cfg, st)
    if only is not None:
        res.waiting += [t for t, _ in res.matched if t.id not in only]
        res.matched = [(t, p) for t, p in res.matched if t.id in only]
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
        # One XML per day: include earlier same-day imports that also aren't in
        # rekordbox yet, so the file always holds the whole day's playlist.
        earlier = [(Path(t.file), t.artist, t.title) for t in st.by_status(IMPORTED)
                   if t.playlist == name and t.import_method != "db" and Path(t.file).is_file()]
        new_paths = {p for p, _, _ in entries}
        all_entries = [e for e in earlier if e[0] not in new_paths] + entries
        res.xml_path = rekordbox.write_xml(all_entries, name, cfg.home / "xml" / f"{name}.xml")
        for t in st.by_status(IMPORTED):
            if t.playlist == name and t.import_method != "db":
                t.xml_path = str(res.xml_path)
        res.playlist = name
        res.method = "xml"

    if res.method:
        now = datetime.now().isoformat(timespec="seconds")
        for t, p in placed:
            t.status, t.file, t.playlist = IMPORTED, str(p), res.playlist
            t.import_method, t.imported_at = res.method, now
            t.xml_path = str(res.xml_path) if res.xml_path else ""
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


@dataclass
class RetryResult:
    playlist: str = ""
    added: int = 0
    missing: list[Track] = field(default_factory=list)
    error: str = ""


def retry_targets(st: State) -> list[str]:
    """Playlists that were saved to XML (or recorded before djsync tracked how)."""
    names = {t.playlist for t in st.by_status(IMPORTED) if t.import_method != "db" and t.playlist}
    return sorted(names)


def retry_db(cfg: Config, st: State, playlist: str) -> RetryResult:
    """Add an XML-only import to rekordbox's database, using the files already in your DJ folder."""
    res = RetryResult(playlist=playlist)
    tracks = [t for t in st.by_status(IMPORTED)
              if t.playlist == playlist and t.import_method != "db"]
    if not tracks:
        res.error = f"Nothing to retry for '{playlist}'."
        return res
    if rekordbox.rekordbox_running():
        res.error = "rekordbox is still open. Quit rekordbox (rekordbox menu → Quit) and try again."
        return res
    try:
        present = [t for t in tracks if t.file and Path(t.file).is_file()]
        res.missing = [t for t in tracks if t not in present]
        if not present:
            res.error = "None of this playlist's files are where djsync put them any more."
            return res
        name = rekordbox.write_db(cfg, [(Path(t.file), t.artist, t.title) for t in present], playlist)
    except Exception as e:
        res.error = f"Could not write to rekordbox: {e}"
        return res
    for t in present:
        t.playlist, t.import_method = name, "db"
    st.save()
    res.playlist, res.added = name, len(present)
    return res


# ---------------------------------------------------------------- tracks typed in by hand

_BY = re.compile(r"\s+by\s+", re.I)
_DASH = re.compile(r"\s+[-\u2013\u2014]\s+")


def parse_line(line: str) -> tuple[str, str]:
    """'Fear of Water by SYML' / 'SYML - Fear of Water' -> (artist, title).

    Without 'by' or a dash, the whole line is taken as the title.
    """
    s = line.strip().strip('"\u201c\u201d\'')
    if not s:
        return "", ""
    last_by = None
    for last_by in _BY.finditer(s):
        pass
    if last_by:  # split at the last " by ", so "Stand By Me by Ben E. King" keeps its title
        return s[last_by.end():].strip(), s[:last_by.start()].strip()
    parts = _DASH.split(s, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", s


@dataclass
class AddResult:
    added: list[Track] = field(default_factory=list)
    in_rekordbox: list[str] = field(default_factory=list)   # "Artist - Title"
    already_listed: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def add_manual(cfg: Config, st: State, text: str, sp=None, library: list | None = None) -> AddResult:
    """Add tracks you typed in (one per line) straight to Buy.

    With Spotify connected (``sp``), each one is looked up there for its ISRC, length
    and artwork, which makes the Beatport match much more reliable.
    """
    import uuid

    res = AddResult()
    index = Index([TrackKey.of(t.artist, t.title) for t in library], cfg.threshold) if library else None
    for line in text.splitlines():
        artist, title = parse_line(line)
        if not title:
            if line.strip():
                res.unreadable.append(line.strip())
            continue
        t = spotify.search_track(sp, artist, title) if sp else None
        if t is None:
            t = Track(id=f"manual-{uuid.uuid4().hex[:10]}", artist=artist, title=title,
                      added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      source="Added by hand")
        name = f"{t.artist} - {t.title}" if t.artist else t.title
        if index is not None and TrackKey.of(t.artist, t.title) in index:
            res.in_rekordbox.append(name)
            continue
        key = TrackKey.of(t.artist, t.title)
        dup = st.tracks.get(t.id) or next(
            (x for x in st.tracks.values() if TrackKey.of(x.artist, x.title) == key), None)
        if dup is not None and dup.status in (APPROVED, IMPORTED):
            res.already_listed.append(name)
            continue
        if dup is not None and dup.status == IN_REKORDBOX:
            res.in_rekordbox.append(name)
            continue
        if dup is not None:            # it was in Scan or skipped: bring that card over
            t = dup
        t.status = APPROVED
        st.tracks[t.id] = t
        res.added.append(t)
    st.save()
    return res
