"""Find audio files in a folder and read their artist/title."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp4", ".ogg"}
ARCHIVE_HINT = {".zip"}  # Bandcamp sometimes delivers zips - we unpack them

_LEADING_ID = re.compile(r"^\d{4,}[_\-\s]+")       # Beatport-style "12345678_Title"
_LEADING_TRACKNO = re.compile(r"^\d{1,2}[\s.\-_]+")  # "01 - Artist - Title"


@dataclass
class AudioFile:
    path: Path
    artist: str
    title: str

    @property
    def text(self) -> str:
        """Everything we know about the file, for flexible matching."""
        return f"{self.artist} {self.title} {self.path.stem.replace('_', ' ')}"


# (path, mtime, size) -> AudioFile, so re-scanning a big Downloads folder is fast.
_CACHE: dict[tuple[str, float, int], AudioFile] = {}


def clear_cache() -> None:
    _CACHE.clear()


def parse_filename(stem: str) -> tuple[str, str]:
    """Best-effort 'Artist - Title' from a filename stem."""
    s = stem.replace("_", " ")
    s = _LEADING_ID.sub("", s)
    s = _LEADING_TRACKNO.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if " - " in s:
        artist, title = s.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", s


def read_tags(path: Path) -> tuple[str, str]:
    try:
        import mutagen

        f = mutagen.File(path, easy=True)
        if f is not None and f.tags:
            artist = ", ".join(f.tags.get("artist", []) or [])
            title = " ".join(f.tags.get("title", []) or [])
            if title:
                return artist, title
    except Exception:
        pass
    return "", ""


def describe(path: Path) -> AudioFile:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        key = None
    if key in _CACHE:
        return _CACHE[key]
    f = _describe(path)
    if key:
        _CACHE[key] = f
    return f


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS and not path.name.startswith(".")


def _describe(path: Path) -> AudioFile:
    artist, title = read_tags(path)
    if not title:
        artist, title = parse_filename(path.stem)
    return AudioFile(path=path, artist=artist, title=title)


def unpack_archives(folder: Path) -> None:
    """Extract store zip downloads in-place (once) so their tracks can be found."""
    import zipfile

    for z in folder.glob("*.zip"):
        target = z.with_suffix("")
        if target.exists():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                members = [m for m in zf.namelist() if Path(m).suffix.lower() in AUDIO_EXTS]
                if members:
                    zf.extractall(target, members=members)
        except zipfile.BadZipFile:
            continue


def scan(folder: Path, recursive: bool = True, exclude: list[Path] = (),
         newer_than: float | None = None, unpack: bool = True) -> list[AudioFile]:
    """All audio files in ``folder`` and (by default) every subfolder, newest first.

    Hidden folders (".something") and anything under ``exclude`` are skipped without
    being walked. ``newer_than`` (a timestamp) keeps only recently changed files, which
    keeps big folders like ~/Music fast. ``unpack`` extracts store .zip downloads.
    """
    import os

    if not folder.exists():
        return []
    if unpack:
        unpack_archives(folder)
    excluded = {e.resolve() for e in exclude if e.exists()}
    out = []
    for root, dirs, names in os.walk(folder):
        here = Path(root)
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and (here / d).resolve() not in excluded] if recursive else []
        for name in names:
            p = here / name
            if name.startswith(".") or p.suffix.lower() not in AUDIO_EXTS:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if newer_than is not None and mtime < newer_than:
                continue
            out.append((mtime, p))
    out.sort(key=lambda x: x[0], reverse=True)
    return [describe(p) for _, p in out]
