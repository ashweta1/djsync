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
        "www.beatport.com", "bandcamp.com", "www.traxsource.com", "www.junodownload.com"]


def test_open_tabs_is_capped(monkeypatch):
    opened = []
    monkeypatch.setattr(stores.webbrowser, "open_new_tab", opened.append)
    assert stores.open_tabs([f"u{i}" for i in range(50)]) == stores.MAX_TABS
    assert len(opened) == stores.MAX_TABS
