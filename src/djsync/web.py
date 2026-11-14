"""Local review page: untick tracks you don't want, buy the rest, import."""

from __future__ import annotations

import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from . import core, files, stores
from .config import Config
from .state import APPROVED, IMPORTED, PENDING, SKIPPED, State


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    app.secret_key = "djsync-local"  # only used for flash messages on localhost
    lock = threading.Lock()

    def state() -> State:
        return State.load(cfg.state_path)

    @app.context_processor
    def helpers():
        return {"store_links": lambda t: stores.links(t.artist, t.title, cfg.stores)}

    @app.get("/")
    def index():
        st = state()
        matched, _ = core.match_files(cfg, st)
        found = {t.id: p for t, p in matched}
        imported = st.by_status(IMPORTED)
        return render_template(
            "review.html",
            pending=st.by_status(PENDING),
            approved=st.by_status(APPROVED),
            skipped=st.by_status(SKIPPED),
            imported=imported[:50],
            found=found,
            last_scan=st.last_scan,
            watch_dir=cfg.watch_dir,
            stores=[{"key": s, "name": stores.STORES[s][0]}
                    for s in cfg.stores if s in stores.STORES],
        )

    @app.post("/scan")
    def scan():
        with lock:
            st = state()
            try:
                r = core.scan(cfg, st)
                flash(f"Checked {r.liked_checked} liked songs: {r.new_missing} new to review, "
                      f"{r.already_owned} already in rekordbox.", "ok")
            except Exception as e:
                flash(f"Scan failed: {e}", "err")
        return redirect(url_for("index"))

    @app.post("/approve")
    def approve():
        with lock:
            st = state()
            chosen = set(request.form.getlist("id"))
            pending = [t.id for t in st.by_status(PENDING)]
            st.set_status([i for i in pending if i in chosen], APPROVED)
            st.set_status([i for i in pending if i not in chosen], SKIPPED)
            st.save()
            flash(f"Approved {len(chosen & set(pending))} tracks. Buy them from the links below; "
                  f"they'll be picked up from {cfg.watch_dir}.", "ok")
        return redirect(url_for("index") + "#approved")

    @app.post("/open")
    def open_searches():
        """Open store searches as browser tabs.

        store=<key> + no id: that store, for every approved track without a file yet.
        id=<track id> + no store: that track, on every enabled store.
        """
        st = state()
        store, track_id = request.form.get("store"), request.form.get("id")
        if track_id:
            t = st.tracks.get(track_id)
            if not t:
                return "unknown track", 404
            urls = [l["url"] for l in stores.links(t.artist, t.title, cfg.stores)]
        elif store in stores.STORES:
            matched, waiting = core.match_files(cfg, st)
            urls = [stores.url(store, t.artist, t.title) for t in waiting]
        else:
            return "need store or id", 400
        n = stores.open_tabs(urls)
        if len(urls) > n:
            flash(f"Opened the first {n} of {len(urls)} searches. Click again after "
                  "you've bought these to open the next ones.", "ok")
        return redirect(url_for("index") + "#approved")

    @app.post("/rescan-downloads")
    def rescan_downloads():
        files.clear_cache()
        st = state()
        n = len(core._scan_downloads(cfg))
        matched, waiting = core.match_files(cfg, st)
        flash(f"Scanned {cfg.watch_dir} and its subfolders: {n} audio files, "
              f"{len(matched)} matched, {len(waiting)} still missing.", "ok")
        return redirect(url_for("index") + "#approved")

    @app.get("/attach/<track_id>")
    def attach_page(track_id: str):
        st = state()
        t = st.tracks.get(track_id)
        if not t:
            return "unknown track", 404
        return render_template(
            "attach.html", t=t, candidates=core.candidates(cfg, t),
            threshold=cfg.file_threshold, watch_dir=cfg.watch_dir,
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
        flash(f"Attached {path.name} to {t.artist} - {t.title}.", "ok")
        return redirect(url_for("index") + "#approved")

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

    @app.post("/detach/<track_id>")
    def detach(track_id: str):
        with lock:
            st = state()
            if track_id in st.tracks:
                st.tracks[track_id].file = ""
                st.save()
        return redirect(url_for("index") + "#approved")

    @app.post("/move/<track_id>/<status>")
    def move(track_id: str, status: str):
        if status not in (PENDING, APPROVED, SKIPPED):
            return "bad status", 400
        with lock:
            st = state()
            st.set_status([track_id], status)
            if status != APPROVED and track_id in st.tracks:
                st.tracks[track_id].file = ""
            st.save()
        return redirect(url_for("index"))

    @app.post("/import")
    def do_import():
        with lock:
            st = state()
            r = core.do_import(cfg, st)
        if r.error:
            flash(r.error, "err")
        if r.method == "db":
            flash(f"Added {len(r.matched)} tracks to rekordbox playlist '{r.playlist}'. "
                  "Open rekordbox and analyse them.", "ok")
        elif r.method == "xml":
            flash(f"Wrote {r.xml_path} with playlist '{r.playlist}'. In rekordbox: "
                  "Preferences > Advanced > Database > rekordbox xml, pick this file, then "
                  "import the playlist from the 'rekordbox xml' tree.", "ok")
        elif not r.matched:
            flash(f"No approved tracks found in {cfg.watch_dir} yet.", "err")
        return redirect(url_for("index"))

    return app


def serve(cfg: Config, open_browser: bool = True) -> None:
    app = create_app(cfg)
    url = f"http://127.0.0.1:{cfg.port}/"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"djsync review page: {url}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=cfg.port, debug=False)
