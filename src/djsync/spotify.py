"""Read your Spotify Liked Songs and playlists via the official Web API (read-only)."""

from __future__ import annotations

from collections.abc import Iterator

from .config import Config
from .state import Track

# Read-only: Liked Songs, plus your own and followed playlists (including private ones).
SCOPE = "user-library-read playlist-read-private playlist-read-collaborative"
LIKED = "liked"  # the source id we use for Liked Songs


def client(cfg: Config):
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE

    if not cfg.client_id:
        raise SystemExit(
            "No Spotify client_id. Create an app at https://developer.spotify.com/dashboard, "
            f"add the redirect URI {cfg.redirect_uri}, and put the client ID in "
            f"{cfg.home / 'config.toml'}."
        )
    cfg.home.mkdir(parents=True, exist_ok=True)
    auth = SpotifyPKCE(
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri,
        scope=SCOPE,
        cache_handler=spotipy.cache_handler.CacheFileHandler(cache_path=str(cfg.spotify_cache)),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth)


def _track(t: dict, added_at: str = "", source: str = "") -> Track | None:
    if not t or not t.get("id") or t.get("is_local") or t.get("type", "track") != "track":
        return None
    album = t.get("album") or {}
    images = album.get("images") or []
    return Track(
        id=t["id"],
        artist=", ".join(a["name"] for a in t.get("artists", [])),
        title=t.get("name", ""),
        album=album.get("name", ""),
        added_at=added_at,
        isrc=(t.get("external_ids") or {}).get("isrc", ""),
        duration_ms=t.get("duration_ms", 0),
        image=images[-1]["url"] if images else "",
        spotify_url=(t.get("external_urls") or {}).get("spotify", ""),
        source=source,
    )


def liked_songs(sp) -> Iterator[Track]:
    """Your Liked Songs, newest first."""
    offset = 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = page.get("items", [])
        for item in items:
            tr = _track(item.get("track") or {}, item.get("added_at", ""), "Liked Songs")
            if tr:
                yield tr
        if not page.get("next"):
            return
        offset += len(items)


def playlists(sp) -> list[dict]:
    """Your playlists (made or followed): [{id, name, count, owner}]."""
    out, offset = [], 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        items = page.get("items", [])
        for p in items:
            if not p or not p.get("id"):
                continue
            tracks = p.get("tracks") or p.get("items") or {}
            out.append({
                "id": p["id"],
                "name": p.get("name") or "(untitled)",
                "count": tracks.get("total", 0) if isinstance(tracks, dict) else 0,
                "owner": (p.get("owner") or {}).get("display_name", ""),
            })
        if not page.get("next"):
            return out
        offset += len(items)


def playlist_tracks(sp, playlist_id: str, name: str = "") -> Iterator[Track]:
    """Tracks in one playlist (podcast episodes and local files are skipped)."""
    offset = 0
    while True:
        page = sp.playlist_items(playlist_id, limit=100, offset=offset, additional_types=("track",))
        items = page.get("items", [])
        for item in items:
            tr = _track(item.get("track") or item.get("item") or {}, item.get("added_at", ""), name)
            if tr:
                yield tr
        if not page.get("next"):
            return
        offset += len(items)


def search_track(sp, artist: str, title: str, source: str = "Added by hand") -> Track | None:
    """Best Spotify match for a track you typed in, or None if Spotify doesn't have it."""
    from datetime import datetime, timezone

    from .matching import TrackKey, similar

    q = f'track:"{title}" artist:"{artist}"' if artist else title
    try:
        items = (sp.search(q=q, type="track", limit=10).get("tracks") or {}).get("items") or []
    except Exception:
        return None
    want = TrackKey.of(artist, title)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in items:
        got = TrackKey.of(", ".join(a["name"] for a in item.get("artists", [])), item.get("name", ""))
        if similar(want, got, 80) if artist else similar(TrackKey("", want.title), TrackKey("", got.title), 85):
            return _track(item, now, source)
    return None
