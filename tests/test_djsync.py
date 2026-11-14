import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from djsync import core, rekordbox, stores
from djsync.config import Config
from djsync.files import parse_filename
from djsync.matching import Index, TrackKey, normalize_artist, normalize_title
from djsync.rekordbox import LibraryTrack
from djsync.state import APPROVED, IMPORTED, PENDING, SKIPPED, State, Track


# --- matching -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Strobe (Original Mix)", "strobe"),
    ("Strobe - Extended Mix", "strobe"),
    ("Strobe - Radio Edit", "strobe"),
    ("Strobe [Extended]", "strobe"),
    ("Opus (feat. Someone)", "opus"),
    ("Café del Mar", "cafe del mar"),
    ("Track (Bicep Remix)", "track bicep remix"),
    ("Song - 2011 Remaster", "song"),
])
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_normalize_artist_order_and_separators():
    assert normalize_artist("Bicep & Hammer") == normalize_artist("Hammer, Bicep")
    assert normalize_artist("Fred again.. x Brian Eno") == normalize_artist("Brian Eno, Fred again..")


def test_index_matches_variants_but_not_remixes():
    lib = Index([TrackKey.of("Deadmau5", "Strobe (Original Mix)"),
                 TrackKey.of("Bicep", "Glue")])
    assert TrackKey.of("deadmau5", "Strobe - Radio Edit") in lib
    assert TrackKey.of("Bicep", "Glue") in lib
    assert TrackKey.of("Bicep", "Glue (Hammer Remix)") not in lib
    assert TrackKey.of("Someone Else", "Strobe") not in lib


def test_index_tolerates_typos():
    lib = Index([TrackKey.of("Peggy Gou", "(It Goes Like) Nanana")])
    assert TrackKey.of("Peggy Gou", "It Goes Like Nanana") in lib


# --- files / stores -------------------------------------------------------------

def test_parse_filename():
    assert parse_filename("Bicep - Glue") == ("Bicep", "Glue")
    assert parse_filename("01 - Bicep - Glue") == ("Bicep", "Glue")
    assert parse_filename("12345678_Bicep_-_Glue_(Original_Mix)") == ("Bicep", "Glue (Original Mix)")
    assert parse_filename("Glue") == ("", "Glue")


def test_store_links():
    links = stores.links("Bicep, Hammer", "Glue", ["beatport", "bandcamp", "nope"])
    assert [l["key"] for l in links] == ["beatport", "bandcamp"]
    assert "Bicep+Glue" in links[0]["url"]


# --- xml --------------------------------------------------------------------------

def test_write_xml(tmp_path):
    f = tmp_path / "My Track.mp3"
    f.write_bytes(b"")
    out = rekordbox.write_xml([(f, "Artist", "My Track")], "downloads-2026-11-14", tmp_path / "x.xml")
    root = ET.parse(out).getroot()
    track = root.find("COLLECTION/TRACK")
    assert track.get("Name") == "My Track"
    assert track.get("Location").startswith("file://localhost/")
    assert "My%20Track.mp3" in track.get("Location")
    node = root.find("PLAYLISTS/NODE/NODE")
    assert node.get("Name") == "downloads-2026-11-14"
    assert node.find("TRACK").get("Key") == track.get("TrackID")


# --- end to end with fakes -----------------------------------------------------------

class FakeSpotify:
    def __init__(self, tracks):
        self.tracks = tracks

    def current_user_saved_tracks(self, limit, offset):
        chunk = self.tracks[offset:offset + limit]
        items = [{"added_at": f"2026-11-{14 - i:02d}T00:00:00Z", "track": {
            "id": tid, "name": title, "artists": [{"name": a} for a in artist.split(", ")],
            "album": {"name": "LP", "images": []}, "external_ids": {}, "external_urls": {},
        }} for i, (tid, artist, title) in enumerate(chunk)]
        nxt = "more" if offset + limit < len(self.tracks) else None
        return {"items": items, "next": nxt}


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path / "home", watch_dir=tmp_path / "dl",
               library_dir=tmp_path / "lib", rekordbox_mode="xml")
    c.watch_dir.mkdir()
    return c


def test_scan_then_import(cfg, monkeypatch):
    liked = [("1", "Bicep", "Glue"), ("2", "Deadmau5", "Strobe - Radio Edit"),
             ("3", "Peggy Gou", "Starry Night"), ("4", "Fred again..", "Delilah")]
    library = [LibraryTrack("deadmau5", "Strobe (Original Mix)", "/x.mp3")]

    st = State.load(cfg.state_path)
    r = core.scan(cfg, st, sp=FakeSpotify(liked), library=library)
    assert (r.new_missing, r.already_owned) == (3, 1)
    assert {t.id for t in st.by_status(PENDING)} == {"1", "3", "4"}

    # user unticks Fred again.., approves the rest
    st.set_status(["1", "3"], APPROVED)
    st.set_status(["4"], SKIPPED)
    st.save()

    # rescan: skipped track is not suggested again
    st = State.load(cfg.state_path)
    core.scan(cfg, st, sp=FakeSpotify(liked), library=library)
    assert st.tracks["4"].status == SKIPPED

    # only one purchase has landed so far
    (cfg.watch_dir / "Bicep - Glue (Original Mix).mp3").write_bytes(b"")
    (cfg.watch_dir / "Unrelated - Thing.mp3").write_bytes(b"")
    st = State.load(cfg.state_path)
    res = core.do_import(cfg, st)
    assert res.method == "xml"
    assert [t.id for t, _ in res.matched] == ["1"]
    assert [t.id for t in res.waiting] == ["3"]
    assert (cfg.library_dir / "Bicep - Glue (Original Mix).mp3").exists()
    assert (cfg.watch_dir / "Unrelated - Thing.mp3").exists()
    assert res.xml_path.exists()

    st = State.load(cfg.state_path)
    assert st.tracks["1"].status == IMPORTED
    assert st.tracks["1"].playlist.startswith("downloads-")
    assert st.tracks["3"].status == APPROVED


def test_db_failure_puts_files_back(cfg, monkeypatch):
    cfg.rekordbox_mode = "db"
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    st.save()
    src = cfg.watch_dir / "Bicep - Glue.mp3"
    src.write_bytes(b"")

    def boom(*a, **k):
        raise RuntimeError("locked")
    monkeypatch.setattr(rekordbox, "write_db", boom)
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)

    res = core.do_import(cfg, State.load(cfg.state_path))
    assert "locked" in res.error and not res.method
    assert src.exists()
    assert State.load(cfg.state_path).tracks["1"].status == APPROVED


def test_review_page_renders(cfg):
    from djsync.web import create_app

    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=PENDING)
    st.tracks["2"] = Track(id="2", artist="Peggy Gou", title="Starry Night", status=APPROVED)
    st.save()
    client = create_app(cfg).test_client()
    html = client.get("/").get_data(as_text=True)
    assert "Glue" in html and "Starry Night" in html and "beatport.com" in html

    # approve with nothing ticked -> track 1 skipped
    client.post("/approve", data={})
    assert State.load(cfg.state_path).tracks["1"].status == SKIPPED


# --- one-click searches ----------------------------------------------------------

@pytest.mark.parametrize("artist,title,expected", [
    ("Bicep, Hammer", "Glue (Original Mix)", "Bicep Glue"),
    ("Bicep", "Glue - Extended Mix", "Bicep Glue"),
    ("Peggy Gou", "Starry Night (feat. Someone)", "Peggy Gou Starry Night"),
    ("Bicep", "Glue (Hammer Remix)", "Bicep Glue Hammer Remix"),
])
def test_search_query_cleanup(artist, title, expected):
    assert stores.search_query(artist, title) == expected


def test_open_all_on_store_skips_tracks_with_files(cfg, monkeypatch):
    from djsync.web import create_app

    opened = []
    monkeypatch.setattr(stores.webbrowser, "open_new_tab", opened.append)
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    st.tracks["2"] = Track(id="2", artist="Peggy Gou", title="Starry Night", status=APPROVED)
    st.save()
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")  # already bought

    client = create_app(cfg).test_client()
    assert "Open all on Beatport" in client.get("/").get_data(as_text=True)
    client.post("/open", data={"store": "beatport"})
    assert opened == ["https://www.beatport.com/search/tracks?q=Peggy+Gou+Starry+Night"]


def test_open_one_track_on_all_stores(cfg, monkeypatch):
    from djsync.web import create_app

    opened = []
    monkeypatch.setattr(stores.webbrowser, "open_new_tab", opened.append)
    st = State(path=cfg.state_path)
    st.tracks["2"] = Track(id="2", artist="Peggy Gou", title="Starry Night", status=APPROVED)
    st.save()
    create_app(cfg).test_client().post("/open", data={"id": "2"})
    assert [u.split("/")[2] for u in opened] == [
        "www.beatport.com", "bandcamp.com", "www.traxsource.com", "www.junodownload.com",
        "www.deezer.com"]


def test_open_tabs_is_capped(monkeypatch):
    opened = []
    monkeypatch.setattr(stores.webbrowser, "open_new_tab", opened.append)
    assert stores.open_tabs([f"u{i}" for i in range(50)]) == stores.MAX_TABS
    assert len(opened) == stores.MAX_TABS


# --- flexible file matching, subfolders, attach ------------------------------------

from djsync.matching import file_score


def _fs(artist, title, filename, tag_artist="", tag_title=""):
    return file_score(artist, title, tag_artist, tag_title, f"{tag_artist} {tag_title} {filename}")


@pytest.mark.parametrize("filename", [
    "Bicep - Glue (Original Mix)",
    "bicep_glue_320",
    "Glue [Bicep] 24bit",
    "01. Glue - Bicep",
    "BICEP-GLUE-WEB-2017",
    "Bicep - Glue (Radio Edit)",
])
def test_file_score_flexible_names(filename):
    assert _fs("Bicep", "Glue", filename) >= 75


@pytest.mark.parametrize("filename", [
    "Bicep - Glue (Hammer Remix)",      # different version
    "Bicep - Apricots",                 # different track
    "Someone Else - Glue",              # different artist
])
def test_file_score_rejects_wrong_files(filename):
    assert _fs("Bicep", "Glue", filename) < 75


def test_file_score_wants_remix_when_track_is_remix():
    assert _fs("Bicep", "Glue (Hammer Remix)", "Bicep - Glue (Hammer Remix)") >= 75
    assert _fs("Bicep", "Glue (Hammer Remix)", "Bicep - Glue (Original Mix)") < 75


def test_tags_beat_meaningless_filename():
    assert _fs("Bicep", "Glue", "track01", tag_artist="Bicep", tag_title="Glue") == 100


def _approved(cfg, *tracks):
    st = State(path=cfg.state_path)
    for i, (a, t) in enumerate(tracks, 1):
        st.tracks[str(i)] = Track(id=str(i), artist=a, title=t, status=APPROVED)
    st.save()
    return st


def test_subfolders_scanned_hidden_and_library_skipped(cfg):
    from djsync import files
    (cfg.watch_dir / "Beatport" / "2026-11").mkdir(parents=True)
    (cfg.watch_dir / "Beatport" / "2026-11" / "bicep_glue.mp3").write_bytes(b"")
    (cfg.watch_dir / ".Trash").mkdir()
    (cfg.watch_dir / ".Trash" / "Bicep - Glue.mp3").write_bytes(b"")
    cfg.library_dir = cfg.watch_dir / "DJ"
    cfg.library_dir.mkdir()
    (cfg.library_dir / "Peggy Gou - Starry Night.mp3").write_bytes(b"")
    names = [f.path.name for f in files.scan(cfg.watch_dir, exclude=[cfg.library_dir])]
    assert names == ["bicep_glue.mp3"]

    st = _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    matched, waiting = core.match_files(cfg, st)
    assert [(t.id, p.name) for t, p in matched] == [("1", "bicep_glue.mp3")]
    assert [t.id for t in waiting] == ["2"]


def test_best_file_wins_and_each_file_used_once(cfg):
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    (cfg.watch_dir / "Bicep - Glue (Hammer Remix).mp3").write_bytes(b"")
    st = _approved(cfg, ("Bicep", "Glue (Hammer Remix)"), ("Bicep", "Glue"))
    matched, _ = core.match_files(cfg, st)
    assert {t.title: p.name for t, p in matched} == {
        "Glue": "Bicep - Glue.mp3",
        "Glue (Hammer Remix)": "Bicep - Glue (Hammer Remix).mp3",
    }


def test_attach_file_from_elsewhere_and_import_copies_it(cfg, tmp_path):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    elsewhere = tmp_path / "usb" / "weird name 07.aiff"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    client = create_app(cfg).test_client()

    page = client.get("/attach/1").get_data(as_text=True)
    assert "Pick the file" in page

    client.post("/attach/1", data={"path": str(elsewhere)})
    st = State.load(cfg.state_path)
    assert st.tracks["1"].file == str(elsewhere.resolve())
    html = client.get("/").get_data(as_text=True)
    assert "Attached" in html and "weird name 07.aiff" in html

    res = core.do_import(cfg, State.load(cfg.state_path))
    assert res.method == "xml"
    assert elsewhere.exists()  # picked from outside Downloads -> copied, not moved
    assert (cfg.library_dir / "weird name 07.aiff").exists()


def test_attach_rejects_non_audio(cfg, tmp_path):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    create_app(cfg).test_client().post("/attach/1", data={"path": str(bad)})
    assert State.load(cfg.state_path).tracks["1"].file == ""


def test_detach_and_rescan_button(cfg, tmp_path):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    f = cfg.watch_dir / "x.mp3"
    f.write_bytes(b"")
    client = create_app(cfg).test_client()
    client.post("/attach/1", data={"path": str(f)})
    client.post("/detach/1")
    assert State.load(cfg.state_path).tracks["1"].file == ""
    assert "Rescan Downloads" in client.get("/").get_data(as_text=True)
    (cfg.watch_dir / "sub").mkdir()
    (cfg.watch_dir / "sub" / "Bicep - Glue.wav").write_bytes(b"")
    r = client.post("/rescan-downloads", follow_redirects=True).get_data(as_text=True)
    assert "2 audio files, 1 matched, 0 still missing" in r


def test_deezer_link():
    assert stores.url("deezer", "Peggy Gou", "Starry Night") == \
        "https://www.deezer.com/search/Peggy%20Gou%20Starry%20Night/track"


def test_found_file_shows_full_path(cfg, monkeypatch):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    (cfg.watch_dir / "Beatport").mkdir()
    f = cfg.watch_dir / "Beatport" / "bicep_glue_320.mp3"
    f.write_bytes(b"")
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert "File found" in html
    assert str(f) in html  # full path shown under the track


def test_tilde_filter():
    from djsync.web import create_app
    tilde = create_app(Config()).jinja_env.filters["tilde"]
    home = str(Path.home())
    assert tilde(f"{home}/Downloads/a.mp3") == "~/Downloads/a.mp3"
    assert tilde("/Volumes/USB/a.mp3") == "/Volumes/USB/a.mp3"
    assert tilde(f"{home}x/a.mp3") == f"{home}x/a.mp3"


# --- import visibility ----------------------------------------------------------------

def test_tracks_added_to_rekordbox_elsewhere_are_marked_not_dropped(cfg):
    from djsync.state import IN_REKORDBOX
    liked = [("1", "Bicep", "Glue"), ("2", "Peggy Gou", "Starry Night")]
    st = State.load(cfg.state_path)
    core.scan(cfg, st, sp=FakeSpotify(liked), library=[])
    st.set_status(["1", "2"], APPROVED)
    st.save()
    # you dragged Glue into rekordbox yourself
    r = core.scan(cfg, State.load(cfg.state_path), sp=FakeSpotify(liked),
                  library=[LibraryTrack("Bicep", "Glue", "/x.mp3")])
    assert r.now_in_rekordbox == 1
    st = State.load(cfg.state_path)
    assert st.tracks["1"].status == IN_REKORDBOX
    assert st.tracks["2"].status == APPROVED


def test_imported_playlist_shown_with_method(cfg):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    client = create_app(cfg).test_client()
    html = client.post("/import", follow_redirects=True).get_data(as_text=True)
    t = State.load(cfg.state_path).tracks["1"]
    assert t.import_method == "xml" and t.xml_path.endswith(".xml") and t.imported_at
    assert t.playlist in html
    assert "Not in rekordbox yet" in html and "Import Playlist" in html
    assert "Bicep - Glue.mp3" in html  # imported file path listed


def test_old_state_files_still_load(cfg):
    import json
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text(json.dumps({"tracks": {"1": {
        "id": "1", "artist": "A", "title": "T", "status": "imported",
        "playlist": "downloads-2026-11-01", "some_future_field": 1}}}))
    st = State.load(cfg.state_path)
    assert st.tracks["1"].import_method == ""
