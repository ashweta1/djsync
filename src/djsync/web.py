"""Local review page: untick tracks you don't want, buy the rest, import."""

from __future__ import annotations

import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

from flask import Flask, flash, redirect, render_template, request, url_for

from . import config as configmod
from . import core, files, research, setup, stores
from .config import Config
from .state import APPROVED, IMPORTED, IN_REKORDBOX, PENDING, SKIPPED, State


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    app.secret_key = "djsync-local"  # only used for flash messages on localhost
    lock = threading.Lock()
    researching: set[str] = set()   # track ids the agent is working on right now

    def start_research(ids: list[str]) -> int:
        """Research these tracks in a background thread. Returns how many started."""
        st = state()
        todo = [st.tracks[i] for i in ids if i in st.tracks and i not in researching]
        if not todo:
            return 0
        with lock:
            researching.update(t.id for t in todo)

        def save(track, finding):
            with lock:
                s = state()
                if track.id in s.tracks:
                    research.apply(s.tracks[track.id], finding)
                    s.save()
                researching.discard(track.id)

        def run():
            try:
                research.research_many(cfg, todo, on_done=save)
            finally:
                with lock:
                    researching.difference_update(t.id for t in todo)

        threading.Thread(target=run, daemon=True).start()
        return len(todo)

    app.config["start_research"] = start_research

    def state() -> State:
        return State.load(cfg.state_path)

    @app.context_processor
    def helpers():
        return {
            "store_links": lambda t: stores.links(t.artist, t.title, cfg.stores),
            "beatport_search": lambda t: stores.url("beatport", t.artist, t.title),
            "can_reveal": sys.platform == "darwin",
            "researching": researching,
            "agent_enabled": cfg.agent_enabled,
            "needs_setup": setup.needs_setup(cfg),
        }

    @app.template_filter("hue")
    def hue(text: str) -> int:
        """Stable colour for tracks without cover art."""
        return sum(ord(c) * (i + 1) for i, c in enumerate(text or "")) % 360

    @app.template_filter("initial")
    def initial(text: str) -> str:
        """First letter or digit, for placeholder covers: '(It Goes Like) Nanana' -> 'I'."""
        return next((c.upper() for c in text or "" if c.isalnum()), "♪")

    @app.template_filter("ago")
    def ago(stamp: str) -> str:
        """'2026-11-14T09:12:00Z' -> '3h ago'."""
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return stamp or ""
        if dt.tzinfo is None:
            dt = dt.astimezone()
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 90:
            return "just now"
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= n:
                v = int(secs // n)
                return f"{v}{unit} ago" if unit != "d" or v < 30 else dt.strftime("%-d %b %Y")
        return "just now"

    def where() -> str:
        """'~/Downloads and ~/Music' for messages."""
        names = [tilde(d) for d in cfg.search_dirs]
        return " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]

    def back(hl=None):
        """Back to the board (or the archive, if that's where you were).

        ``hl`` is track ids to highlight, so you can see where a card moved to.
        """
        args = {}
        if request.referrer:
            view = parse_qs(urlparse(request.referrer).query).get("view", [None])[0]
            if view == "archive":
                args["view"] = view
        if hl:
            args["hl"] = ",".join(hl)
        return redirect(url_for("index", **args))

    @app.template_filter("tilde")
    def tilde(path) -> str:
        """/Users/me/Downloads/x.mp3 -> ~/Downloads/x.mp3"""
        p, home = str(path), str(Path.home())
        return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p

    # ------------------------------------------------------------------ setup
    @app.get("/setup")
    def setup_page():
        return render_template("setup.html", checks=setup.all_checks(cfg), cfg=cfg,
                               redirect_uri=cfg.redirect_uri)

    @app.post("/setup/spotify-id")
    def setup_spotify_id():
        cid = request.form.get("client_id", "").strip()
        if not cid or not cid.isalnum():
            flash("That doesn't look like a Spotify Client ID: it's a long string of letters and numbers.", "err")
        else:
            if cid != cfg.client_id and cfg.spotify_cache.exists():
                cfg.spotify_cache.unlink()  # a new app needs a new login
            cfg.client_id = cid
            configmod.save(cfg)
        return redirect(url_for("setup_page") + "#spotify_login")

    @app.post("/setup/connect-spotify")
    def setup_connect_spotify():
        """Log in to Spotify: opens the Spotify login page and waits for you to approve."""
        from . import spotify

        if not cfg.client_id:
            flash("Add your Spotify Client ID first.", "err")
            return redirect(url_for("setup_page"))
        try:
            spotify.client(cfg).current_user()
        except BaseException as e:  # spotipy raises SystemExit-like errors on bad IDs
            flash(f"Spotify login didn't work: {e}. Check the Client ID and that the app's "
                  f"Redirect URI is exactly {cfg.redirect_uri}.", "err")
        return redirect(url_for("setup_page"))

    @app.post("/setup/rekordbox-key")
    def setup_rekordbox_key():
        """rekordbox 7 encrypts its library; pyrekordbox can download the key it needs."""
        try:
            out = subprocess.run([sys.executable, "-m", "pyrekordbox", "download-key"],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                flash("Couldn't get the rekordbox key: "
                      + ((out.stderr or out.stdout).strip().splitlines() or ["unknown error"])[-1], "err")
        except Exception as e:
            flash(f"Couldn't get the rekordbox key: {e}", "err")
        return redirect(url_for("setup_page") + "#rekordbox")

    # --------------------------------------------------------------- settings
    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", cfg=cfg, all_stores=stores.STORES,
                               saved=bool(request.args.get("saved")),
                               config_path=cfg.home / "config.toml")

    @app.post("/settings")
    def settings_save():
        f = request.form
        errors = []

        def path(name, default):
            v = f.get(name, "").strip()
            return Path(v).expanduser() if v else default

        def number(name, lo, hi, default):
            try:
                n = int(f.get(name, default))
            except ValueError:
                errors.append(f"{name.replace('_', ' ')} must be a number")
                return default
            if not lo <= n <= hi:
                errors.append(f"{name.replace('_', ' ')} must be between {lo} and {hi}")
                return default
            return n

        cid = f.get("client_id", "").strip()
        if cid and not cid.isalnum():
            errors.append("the Spotify Client ID should be letters and numbers only")
        prefix = f.get("playlist_prefix", "").strip()
        if not prefix:
            errors.append("the playlist prefix can't be empty")
        mode = f.get("rekordbox_mode", "auto")
        backend = f.get("agent_backend", "auto")
        model = f.get("claude_code_model", "sonnet")
        chosen_stores = [s for s in f.getlist("stores") if s in stores.STORES]
        if not chosen_stores:
            errors.append("choose at least one store")
        new = dict(
            watch_dir=path("watch_dir", cfg.watch_dir),
            library_dir=path("library_dir", cfg.library_dir),
            extra_dirs=[Path(x.strip()).expanduser() for x in f.get("extra_dirs", "").splitlines() if x.strip()],
            move_files=bool(f.get("move_files")),
            recent_days=number("recent_days", 1, 3650, cfg.recent_days),
            rekordbox_mode=mode if mode in ("auto", "db", "xml") else "auto",
            rekordbox_db_path=f.get("rekordbox_db_path", "").strip(),
            playlist_prefix=prefix or cfg.playlist_prefix,
            threshold=number("threshold", 50, 100, cfg.threshold),
            file_threshold=number("file_threshold", 40, 100, cfg.file_threshold),
            stores=chosen_stores or cfg.stores,
            agent_enabled=bool(f.get("agent_enabled")),
            agent_auto=bool(f.get("agent_auto")),
            agent_backend=backend if backend in ("auto", "claude-code", "api") else "auto",
            claude_code_model=model if model in ("sonnet", "haiku", "opus", "") else "sonnet",
            claude_path=f.get("claude_path", "").strip(),
            beatport_direct=bool(f.get("beatport_direct")),
        )
        if errors:
            flash("Not saved: " + "; ".join(errors) + ".", "err")
            return redirect(url_for("settings_page"))
        if cid != cfg.client_id and cfg.spotify_cache.exists():
            cfg.spotify_cache.unlink()  # different Spotify app: log in again
        cfg.client_id = cid
        for k, v in new.items():
            setattr(cfg, k, v)  # takes effect right away, no restart needed
        configmod.save(cfg)
        return redirect(url_for("settings_page", saved=1))

    @app.get("/")
    def index():
        st = state()
        matched, _ = core.match_files(cfg, st)
        found = {t.id: p for t, p in matched}
        # Group imports by playlist, newest first.
        groups: dict[str, dict] = {}
        for t in sorted(st.by_status(IMPORTED), key=lambda t: t.imported_at, reverse=True):
            g = groups.setdefault(t.playlist, {"name": t.playlist, "xml": "", "at": t.imported_at,
                                               "tracks": [], "pending": 0, "unknown": 0})
            g["tracks"].append(t)
            if t.import_method == "xml":
                g["pending"] += 1
                g["xml"] = g["xml"] or t.xml_path
            elif not t.import_method:
                g["unknown"] += 1
        for g in groups.values():
            g["method"] = ("db" if not g["pending"] and not g["unknown"]
                           else "xml" if g["pending"] else "")
        playlists = list(groups.values())
        pending, approved = st.by_status(PENDING), st.by_status(APPROVED)
        ready = [t for t in approved if t.id in found]
        # Still to buy first, then the ones you've bought (waiting for the download).
        to_buy = sorted((t for t in approved if t.id not in found), key=lambda t: t.bought)
        skipped, in_rb = st.by_status(SKIPPED), st.by_status(IN_REKORDBOX)
        names = {p["id"]: p["name"] for p in st.playlists}
        chosen = [("Liked Songs" if s == "liked" else names.get(s)) for s in st.sources]
        chosen = [c for c in chosen if c]
        sources_label = (chosen[0] if len(chosen) == 1 else
                         f"{chosen[0]} + {len(chosen) - 1} more" if chosen else "Liked Songs")
        return render_template(
            "review.html",
            sources=st.sources,
            last_scan_summary=st.last_scan_summary,
            sources_label=sources_label,
            spotify_playlists=st.playlists,
            view="archive" if request.args.get("view") == "archive" else "board",
            hl=set(filter(None, request.args.get("hl", "").split(","))),
            pending=pending,
            approved=approved,
            to_buy=to_buy,
            ready=ready,
            skipped=skipped,
            playlists=playlists[:20],
            in_rekordbox=in_rb,
            found=found,
            prefix=cfg.playlist_prefix,
            today_playlist=f"{cfg.playlist_prefix}{date.today():%Y-%m-%d}",
            last_scan=st.last_scan,
            watch_dir=cfg.watch_dir,
            search_dirs=cfg.search_dirs,
            stores=[{"key": s, "name": stores.STORES[s][0]}
                    for s in cfg.stores if s in stores.STORES],
        )

    @app.post("/scan")
    def scan():
        with lock:
            st = state()
            try:
                sources = None
                if request.form.get("picker"):
                    sources = request.form.getlist("source")
                    if not sources:
                        flash("Tick at least one: Liked Songs or a playlist.", "err")
                        return redirect(url_for("index", pick=1))
                r = core.scan(cfg, st, sources=sources)
                for err in r.errors:
                    flash(err, "err")
            except Exception as e:
                flash(f"Scan failed: {e}", "err")
        return back()

    @app.post("/playlists/refresh")
    def refresh_playlists():
        """Fetch your playlist list from Spotify (no scan), then reopen the picker."""
        from . import spotify

        with lock:
            st = state()
            if request.form.get("picker"):
                st.sources = request.form.getlist("source") or st.sources  # keep ticks
            try:
                st.playlists = spotify.playlists(spotify.client(cfg))
                st.save()
            except Exception as e:
                flash(f"Couldn't load your playlists: {e}", "err")
        return redirect(url_for("index", pick=1))

    @app.post("/approve")
    def approve():
        with lock:
            st = state()
            chosen = set(request.form.getlist("id"))
            pending = [t.id for t in st.by_status(PENDING)]
            if request.form.get("action") == "skip_rest":
                # "Skip unselected": the unticked cards leave Scan; ticked ones stay.
                rest = [i for i in pending if i not in chosen]
                st.set_status(rest, SKIPPED)
                st.save()
                return back()
            st.set_status([i for i in pending if i in chosen], APPROVED)
            st.save()
        approved_now = [i for i in pending if i in chosen]
        if cfg.agent_enabled and cfg.agent_auto and approved_now:
            start_research(approved_now)
        return back(approved_now)

    @app.post("/add")
    def add_tracks():
        """Tracks typed in by hand ('Fear of Water by SYML'), one per line, straight to Buy."""
        from . import rekordbox, spotify

        text = request.form.get("tracks", "")
        sp = None
        if cfg.client_id and cfg.spotify_cache.exists():  # only if already logged in: no pop-ups
            try:
                sp = spotify.client(cfg)
            except BaseException:
                sp = None
        try:
            library = rekordbox.read_library(cfg)
        except Exception:
            library = None  # can't check rekordbox right now; add anyway
        with lock:
            r = core.add_manual(cfg, state(), text, sp=sp, library=library)
        if r.in_rekordbox:
            flash("Already in your rekordbox library, not added: " + "; ".join(r.in_rekordbox), "err")
        if r.already_listed:
            flash("Already on the board: " + "; ".join(r.already_listed), "err")
        if r.unreadable:
            flash("Couldn't read: " + "; ".join(r.unreadable) + ". Try 'Title by Artist'.", "err")
        ids = [t.id for t in r.added]
        if ids and cfg.agent_enabled and cfg.agent_auto:
            start_research(ids)
        return back(ids)

    @app.post("/research")
    def research_route():
        """Find the best store link: one track (id=...) or every approved track without one."""
        st = state()
        track_id = request.form.get("id")
        if track_id:
            ids = [track_id]
        else:
            matched, waiting = core.match_files(cfg, st)
            ids = [t.id for t in waiting if not t.researched_at]
        start_research(ids)
        return back(ids if track_id else None)  # highlight the card you clicked

    @app.get("/research-status")
    def research_status():
        return {"running": len(researching)}

    @app.post("/rescan-downloads")
    def rescan_downloads():
        files.clear_cache()  # the board re-reads the folders when it reloads
        return back()

    @app.get("/attach/<track_id>")
    def attach_page(track_id: str):
        st = state()
        t = st.tracks.get(track_id)
        if not t:
            return "unknown track", 404
        return render_template(
            "attach.html", t=t, candidates=core.candidates(cfg, t),
            threshold=cfg.file_threshold, watch_dir=cfg.watch_dir, search_dirs=cfg.search_dirs,
            recent_days=cfg.recent_days,
            can_browse=sys.platform == "darwin",
        )

    def _attach(track_id: str, path: Path):
        if not files.is_audio(path):
            flash(f"Not an audio file: {path}", "err")
            return redirect(url_for("attach_page", track_id=track_id))
        with lock:
            st = state()
            t = st.tracks.get(track_id)
            if not t:
                return "unknown track", 404
            t.file = str(path.resolve())
            if t.status != APPROVED:
                t.status = APPROVED
            st.save()
        return back([track_id])

    @app.post("/attach/<track_id>")
    def attach(track_id: str):
        return _attach(track_id, Path(request.form.get("path", "")).expanduser())

    @app.post("/attach/<track_id>/browse")
    def attach_browse(track_id: str):
        """Open a native macOS file picker (the server runs on your Mac)."""
        start = cfg.watch_dir if cfg.watch_dir.exists() else Path.home()
        script = (
            'POSIX path of (choose file with prompt "Choose the audio file for this track" '
            f'default location (POSIX file "{str(start).replace(chr(34), "")}"))'
        )
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True,
                                 text=True, timeout=600)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            flash("Couldn't open the file picker. Paste the file path instead.", "err")
            return redirect(url_for("attach_page", track_id=track_id))
        if out.returncode != 0 or not out.stdout.strip():
            return redirect(url_for("attach_page", track_id=track_id))  # cancelled
        return _attach(track_id, Path(out.stdout.strip()))

    @app.post("/reveal/<track_id>")
    def reveal(track_id: str):
        """Select the matched file in a Finder window."""
        st = state()
        matched, _ = core.match_files(cfg, st)
        path = next((p for t, p in matched if t.id == track_id), None)
        if path is not None:
            try:
                subprocess.run(["open", "-R", str(path)], timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                flash("Couldn't open Finder.", "err")
        return back()

    @app.post("/bought/<track_id>")
    def bought(track_id: str):
        """Mark (or unmark) a Buy card as bought: it dims and waits for the download."""
        with lock:
            st = state()
            t = st.tracks.get(track_id)
            if not t:
                return "unknown track", 404
            t.bought = not t.bought
            st.save()
        return back([track_id])

    @app.post("/detach/<track_id>")
    def detach(track_id: str):
        with lock:
            st = state()
            if track_id in st.tracks:
                st.tracks[track_id].file = ""
                st.save()
        return back()

    @app.post("/move/<track_id>/<status>")
    def move(track_id: str, status: str):
        if status not in (PENDING, APPROVED, SKIPPED):
            return "bad status", 400
        with lock:
            st = state()
            st.set_status([track_id], status)
            if status != APPROVED and track_id in st.tracks:
                st.tracks[track_id].file = ""
                st.tracks[track_id].bought = False
            st.save()
        if status == APPROVED and cfg.agent_enabled and cfg.agent_auto:
            start_research([track_id])
        return back([track_id])

    @app.post("/import")
    def do_import():
        only = set(request.form.getlist("id")) or None  # ✓ on one card: just that track
        with lock:
            st = state()
            r = core.do_import(cfg, st, only=only)
        if r.error:
            flash(r.error, "err")
        if not r.method and not r.matched:
            flash(f"No approved tracks found in {where()} yet.", "err")
        return back(sorted(only) if only and r.method else None)

    @app.post("/retry-db")
    def retry_db():
        with lock:
            st = state()
            r = core.retry_db(cfg, st, request.form.get("playlist", ""))
        if r.error:
            flash(r.error, "err")
        elif r.missing:
            flash(f"{len(r.missing)} file(s) were missing and were left out of '{r.playlist}': "
                  + ", ".join(f"{t.artist} - {t.title}" for t in r.missing), "err")
        return back()

    @app.post("/open-rekordbox")
    def open_rekordbox():
        """Bring up rekordbox (there's no link that opens a specific playlist)."""
        for cmd in (["open", "-a", "rekordbox"], ["open", "-b", "com.pioneerdj.rekordboxdj"]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        else:
            flash("Couldn't open rekordbox automatically. Open it from Applications.", "err")
        return back()

    @app.post("/reveal-path")
    def reveal_path():
        """Show a djsync file (an XML export or an imported track) in Finder."""
        st = state()
        path = request.form.get("path", "")
        allowed = {t.xml_path for t in st.tracks.values()} | {t.file for t in st.tracks.values()}
        if path and path in allowed and Path(path).exists():
            try:
                subprocess.run(["open", "-R", path], timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                flash("Couldn't open Finder.", "err")
        else:
            flash("That file isn't there any more.", "err")
        return back()

    return app


def serve(cfg: Config, open_browser: bool = True) -> None:
    app = create_app(cfg)
    url = f"http://127.0.0.1:{cfg.port}/" + ("setup" if setup.needs_setup(cfg) else "")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"djsync is running at {url}\nLeave this window open while you use it. "
          "Press Ctrl+C (or close the window) to stop.")
    try:
        app.run(host="127.0.0.1", port=cfg.port, debug=False)
    except OSError as e:
        if "in use" not in str(e).lower():
            raise
        # djsync is probably already running: just open it.
        print(f"djsync seems to be running already. Opening {url}")
        if open_browser:
            webbrowser.open(url)
