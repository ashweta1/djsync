"""Persistent state: which liked tracks are pending, approved, skipped, imported."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

PENDING = "pending"      # liked on Spotify, not in rekordbox, awaiting your review
APPROVED = "approved"    # you want it - waiting for the purchased file to show up
SKIPPED = "skipped"      # you deselected it - won't be suggested again
IMPORTED = "imported"    # file found and added to rekordbox
STATUSES = (PENDING, APPROVED, SKIPPED, IMPORTED)


@dataclass
class Track:
    id: str
    artist: str
    title: str
    album: str = ""
    added_at: str = ""
    isrc: str = ""
    duration_ms: int = 0
    image: str = ""
    spotify_url: str = ""
    status: str = PENDING
    file: str = ""
    playlist: str = ""


@dataclass
class State:
    tracks: dict[str, Track] = field(default_factory=dict)
    last_scan: str = ""
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        st = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text())
            st.last_scan = raw.get("last_scan", "")
            st.tracks = {k: Track(**v) for k, v in raw.get("tracks", {}).items()}
        return st

    def save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"last_scan": self.last_scan,
             "tracks": {k: asdict(v) for k, v in self.tracks.items()}},
            indent=2,
        ))
        tmp.replace(self.path)

    def by_status(self, status: str) -> list[Track]:
        items = [t for t in self.tracks.values() if t.status == status]
        return sorted(items, key=lambda t: t.added_at, reverse=True)

    def set_status(self, ids: list[str], status: str) -> None:
        for i in ids:
            if i in self.tracks:
                self.tracks[i].status = status
