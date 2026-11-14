"""Read your Spotify Liked Songs via the official Web API (read-only)."""

from __future__ import annotations

from collections.abc import Iterator

from .config import Config
from .state import Track

SCOPE = "user-library-read"


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


def liked_songs(sp, stop_before: set[str] | None = None) -> Iterator[Track]:
    """Yield liked tracks, newest first.

    If ``stop_before`` is given, stop once we reach a run of tracks we've
    already seen (Liked Songs is ordered by date added, so older ones are known).
    """
    offset, seen_streak = 0, 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = page.get("items", [])
        for item in items:
            t = item.get("track") or {}
            if not t.get("id") or t.get("is_local"):
                continue
            if stop_before is not None and t["id"] in stop_before:
                seen_streak += 1
                if seen_streak >= 50:
                    return
                continue
            seen_streak = 0
            images = (t.get("album") or {}).get("images") or []
            yield Track(
                id=t["id"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                title=t.get("name", ""),
                album=(t.get("album") or {}).get("name", ""),
                added_at=item.get("added_at", ""),
                isrc=(t.get("external_ids") or {}).get("isrc", ""),
                duration_ms=t.get("duration_ms", 0),
                image=images[-1]["url"] if images else "",
                spotify_url=(t.get("external_urls") or {}).get("spotify", ""),
            )
        if not page.get("next"):
            return
        offset += len(items)
