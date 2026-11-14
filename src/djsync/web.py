"""Local review page: untick tracks you don't want, buy the rest, import."""

from __future__ import annotations

import threading
import webbrowser

from flask import Flask, flash, redirect, render_template, request, url_for

from . import core, stores
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
        found = {t.id: p.name for t, p in matched}
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
            stores=[stores.STORES[s][0] for s in cfg.stores if s in stores.STORES],
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

    @app.post("/move/<track_id>/<status>")
    def move(track_id: str, status: str):
        if status not in (PENDING, APPROVED, SKIPPED):
            return "bad status", 400
        with lock:
            st = state()
            st.set_status([track_id], status)
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
