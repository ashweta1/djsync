"""Find audio files in a folder and read their artist/title."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp4", ".ogg"}
ARCHIVE_HINT = {".zip"}  # Bandcamp/Juno sometimes deliver zips - we unpack them

_LEADING_ID = re.compile(r"^\d{4,}[_\-\s]+")       # Beatport-style "12345678_Title"
_LEADING_TRACKNO = re.compile(r"^\d{1,2}[\s.\-_]+")  # "01 - Artist - Title"


@dataclass
class AudioFile:
    path: Path
    artist: str
    title: str


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


def scan(folder: Path, recursive: bool = True) -> list[AudioFile]:
    if not folder.exists():
        return []
    unpack_archives(folder)
    it = folder.rglob("*") if recursive else folder.glob("*")
    return [
        describe(p) for p in it
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
    ]
