import urllib.error
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
               library_dir=tmp_path / "lib", rekordbox_mode="xml",
               extra_dirs=[tmp_path / "music"])
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
    assert st.tracks["1"].playlist.startswith("djsync-downloads-")
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
    html = client.get("/").get_data(as_text=True)  # one board, all four columns
    for col in ("Scan", "Buy", "Import", "Playlists"):
        assert f">{col}</h2>" in html
    assert "Glue" in html and "Starry Night" in html and "beatport.com" in html

    # "Skip unselected" with nothing ticked -> track 1 skipped
    client.post("/approve", data={"action": "skip_rest"})
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


def test_removed_stores_are_ignored_in_old_configs():
    links = stores.links("Bicep", "Glue", ["beatport", "traxsource", "juno", "deezer"])
    assert [l["key"] for l in links] == ["beatport", "deezer"]


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
    imp = r.split('id="c-import"')[1].split('id="c-pl"')[0]
    assert "Glue" in imp and "Bicep - Glue.wav" in imp


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


# --- retry XML-only imports ----------------------------------------------------------

def _xml_import(cfg):
    _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    (cfg.watch_dir / "Peggy Gou - Starry Night.mp3").write_bytes(b"")
    res = core.do_import(cfg, State.load(cfg.state_path))
    assert res.method == "xml"
    return res.playlist


def test_retry_adds_xml_playlist_to_rekordbox(cfg, monkeypatch):
    name = _xml_import(cfg)
    calls = []
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)
    monkeypatch.setattr(rekordbox, "write_db", lambda c, entries, n: calls.append((entries, n)) or n)
    st = State.load(cfg.state_path)
    assert core.retry_targets(st) == [name]
    r = core.retry_db(cfg, st, name)
    assert not r.error and r.added == 2
    entries, used = calls[0]
    assert used == name
    assert all(p.parent == cfg.library_dir for p, _, _ in entries)  # files from the DJ folder
    st = State.load(cfg.state_path)
    assert {t.import_method for t in st.by_status(IMPORTED)} == {"db"}
    assert core.retry_targets(st) == []


def test_retry_refuses_while_rekordbox_open(cfg, monkeypatch):
    name = _xml_import(cfg)
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: True)
    r = core.retry_db(cfg, State.load(cfg.state_path), name)
    assert "Quit rekordbox" in r.error
    assert State.load(cfg.state_path).tracks["1"].import_method == "xml"


def test_retry_skips_missing_files(cfg, monkeypatch):
    name = _xml_import(cfg)
    Path(State.load(cfg.state_path).tracks["2"].file).unlink()
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)
    monkeypatch.setattr(rekordbox, "write_db", lambda c, entries, n: n)
    st = State.load(cfg.state_path)
    r = core.retry_db(cfg, st, name)
    assert r.added == 1 and [t.id for t in r.missing] == ["2"]


def test_retry_button_and_cli(cfg, monkeypatch, capsys):
    from djsync import cli, config as cfgmod
    from djsync.web import create_app
    name = _xml_import(cfg)
    client = create_app(cfg).test_client()
    assert "Retry: add to rekordbox" in client.get("/").get_data(as_text=True)
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)
    monkeypatch.setattr(rekordbox, "write_db", lambda c, entries, n: n)
    monkeypatch.setattr(cfgmod, "load", lambda: cfg)
    assert cli.main(["import", "--retry"]) == 0
    assert f"{name}: added 2 tracks" in capsys.readouterr().out
    html = client.get("/").get_data(as_text=True)
    assert "In rekordbox" in html and "Retry: add to rekordbox" not in html


# --- same-day imports go into one playlist ------------------------------------------

class FakeDB:
    """Just enough of pyrekordbox's MasterDatabase for write_db."""
    class Q(list):
        def first(self):
            return self[0] if self else None

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def __init__(self, folder):
        self.db_directory = folder
        self.playlists, self.content, self.artists, self.songs = [], [], [], []
        self.commits = 0

    def _q(self, rows, **kw):
        return self.Q(r for r in rows if all(getattr(r, k) == v for k, v in kw.items()))

    def get_playlist(self, **kw): return self._q(self.playlists, **kw)
    def get_content(self, **kw): return self._q(self.content, **kw)
    def get_artist(self, **kw): return self._q(self.artists, **kw)
    def get_playlist_songs(self, **kw): return self._q(self.songs, **kw)

    def create_playlist(self, name):
        p = self.Obj(ID=len(self.playlists) + 1, Name=name, Attribute=0)
        self.playlists.append(p)
        return p

    def add_artist(self, name):
        a = self.Obj(ID=len(self.artists) + 1, Name=name)
        self.artists.append(a)
        return a

    def add_content(self, path, **kw):
        c = self.Obj(ID=len(self.content) + 1, FolderPath=path, **kw)
        self.content.append(c)
        return c

    def add_to_playlist(self, pl, c):
        self.songs.append(self.Obj(PlaylistID=pl.ID, ContentID=c.ID))

    def commit(self): self.commits += 1
    def rollback(self): pass
    def close(self): pass


def test_write_db_appends_to_same_day_playlist_without_duplicates(cfg, tmp_path, monkeypatch):
    db = FakeDB(tmp_path)
    monkeypatch.setattr(rekordbox, "_open_db", lambda c: db)
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    rekordbox.write_db(cfg, [(a, "Bicep", "Glue")], "djsync-downloads-2026-11-14")
    rekordbox.write_db(cfg, [(a, "Bicep", "Glue"), (b, "Bicep", "Apricots")],
                       "djsync-downloads-2026-11-14")
    assert [p.Name for p in db.playlists] == ["djsync-downloads-2026-11-14"]
    assert len(db.content) == 2 and len(db.songs) == 2  # a not added twice
    assert [x.Name for x in db.artists] == ["Bicep"]


def test_default_prefix():
    assert Config().playlist_prefix == "djsync-downloads-"


def test_second_import_same_day_joins_playlist_and_xml(cfg):
    _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    first = core.do_import(cfg, State.load(cfg.state_path))
    (cfg.watch_dir / "Peggy Gou - Starry Night.mp3").write_bytes(b"")
    second = core.do_import(cfg, State.load(cfg.state_path))
    assert first.playlist == second.playlist and first.xml_path == second.xml_path
    root = ET.parse(second.xml_path).getroot()
    assert sorted(t.get("Name") for t in root.find("COLLECTION")) == ["Glue", "Starry Night"]
    assert root.find("PLAYLISTS/NODE/NODE").get("Entries") == "2"

    from djsync.web import create_app
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert html.count(first.playlist) >= 1 and "2 tracks" in html


def test_retry_after_two_same_day_xml_imports(cfg, tmp_path, monkeypatch):
    test_second_import_same_day_joins_playlist_and_xml(cfg)
    db = FakeDB(tmp_path)
    monkeypatch.setattr(rekordbox, "_open_db", lambda c: db)
    monkeypatch.setattr(rekordbox, "rekordbox_running", lambda: False)
    st = State.load(cfg.state_path)
    [name] = core.retry_targets(st)
    r = core.retry_db(cfg, st, name)
    assert r.added == 2 and len(db.playlists) == 1 and len(db.songs) == 2
    # retrying again changes nothing
    assert core.retry_targets(State.load(cfg.state_path)) == []


# --- research agent ------------------------------------------------------------------

import io
import json as _json
from types import SimpleNamespace as NS

from djsync import research


def _resp(answer, result_urls, stop="end_turn"):
    blocks = [NS(type="server_tool_use"),
              NS(type="web_search_tool_result", content=[NS(url=u) for u in result_urls]),
              NS(type="text", text=_json.dumps(answer))]
    return NS(content=blocks, stop_reason=stop)


class FakeClaude:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _no_deezer(url, timeout=15):
    url = getattr(url, 'full_url', url)
    return io.BytesIO(b'{"error": {}}')


GLUE = Track(id="1", artist="Bicep", title="Glue", isrc="GBCFB1700200", duration_ms=269000)
BP = "https://www.beatport.com/track/glue/9283745"


def _answer(**kw):
    a = {"found": True, "store": "Beatport", "url": BP, "title": "Glue", "mix": "Original Mix",
         "artists": "Bicep", "confidence": "high", "reason": "Same artist, title and ISRC."}
    a.update(kw)
    return a


def test_research_picks_beatport_link_from_results():
    client = FakeClaude(_resp(_answer(url=BP + "?utm=x"), [BP, "https://www.beatport.com/release/glue/1"]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert (f.store, f.store_name, f.url) == ("beatport.com", "Beatport", BP)
    assert f.label == "Glue (Original Mix) - Bicep" and f.confidence == "high"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["tools"][0]["type"] == "web_search_20260209"
    assert "GBCFB1700200" in call["messages"][0]["content"] and "4:29" in call["messages"][0]["content"]


def test_research_rejects_links_not_in_search_results():
    client = FakeClaude(_resp(_answer(url="https://www.beatport.com/track/glue/1111"), [BP]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.url == "" and "ignored" in f.note


def test_research_rejects_non_store_sites():
    pirate = "https://free-mp3-download.example/bicep-glue"
    client = FakeClaude(_resp(_answer(store="Some site", url=pirate), [pirate]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.url == ""


def test_research_other_store_when_not_on_beatport():
    bc = "https://bicep.bandcamp.com/track/glue"
    client = FakeClaude(_resp(_answer(store="Bandcamp", url=bc, mix=""), [bc]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert (f.store, f.store_name, f.url) == ("bandcamp.com", "Bandcamp", bc)
    assert f.label == "Glue - Bicep"


def test_research_falls_back_to_deezer():
    def deezer(url, timeout=15):
        url = getattr(url, 'full_url', url)
        assert "isrc:GBCFB1700200" in url
        return io.BytesIO(b'{"id": 1, "link": "https://www.deezer.com/track/1"}')
    client = FakeClaude(_resp({"found": False, "store": "", "url": "", "title": "", "mix": "",
                               "artists": "", "confidence": "low", "reason": "Not sold anywhere."}, []))
    f = research.research(Config(), GLUE, client, opener=deezer)
    assert (f.store, f.url) == ("deezer", "https://www.deezer.com/track/1")
    assert f.note == "Not sold anywhere."


def test_deezer_search_fallback_checks_the_match():
    def deezer(url, timeout=15):
        url = getattr(url, 'full_url', url)
        if "isrc:" in url:
            return io.BytesIO(b'{"error": {"type": "DataException"}}')
        return io.BytesIO(_json.dumps({"data": [
            {"title": "Glue", "artist": {"name": "Someone Else"}, "link": "https://www.deezer.com/track/9"},
            {"title": "Glue", "artist": {"name": "Bicep"}, "link": "https://www.deezer.com/track/2"},
        ]}).encode())
    assert research.deezer_link(GLUE, deezer) == "https://www.deezer.com/track/2"


def test_research_continues_after_pause_turn():
    client = FakeClaude(NS(content=[NS(type="web_search_tool_result", content=[NS(url=BP)])],
                           stop_reason="pause_turn"),
                        _resp(_answer(), []))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.url == BP and len(client.calls) == 2
    assert client.calls[1]["messages"][1]["role"] == "assistant"


def test_research_retries_without_schema_on_400():
    class Bad(Exception):
        status_code = 400
    client = FakeClaude(Bad("format not supported with citations"), _resp(_answer(), [BP]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.url == BP
    assert "format" in client.calls[0]["output_config"]
    assert "format" not in client.calls[1]["output_config"]


def test_research_reports_errors_instead_of_crashing():
    client = FakeClaude(RuntimeError("no API key"))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert "no API key" in f.error and f.url == ""


def test_review_page_shows_agent_results(cfg):
    from djsync.web import create_app
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    research.apply(st.tracks["1"], research.Finding(
        store="beatport.com", store_name="Beatport", url=BP, label="Glue (Extended Mix) - Bicep",
        confidence="high", note="Exact match.", deezer_url="https://www.deezer.com/track/1"))
    st.tracks["2"] = Track(id="2", artist="Peggy Gou", title="Starry Night", status=APPROVED)
    st.save()
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert "Buy on Beatport" in html and BP in html and 'class="conf high"' in html
    assert "https://www.deezer.com/track/1" in html
    assert "Find best sources (1)" in html  # Starry Night not researched yet


def test_approving_starts_research(cfg, monkeypatch):
    from djsync.web import create_app
    seen = []
    monkeypatch.setattr(research, "research_many", lambda c, tracks, on_done=None: seen.extend(t.id for t in tracks))
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=PENDING)
    st.save()
    create_app(cfg).test_client().post("/approve", data={"id": ["1"]})
    import time
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    assert seen == ["1"]


def test_board_columns_and_moving_cards(cfg, monkeypatch):
    from djsync.web import create_app
    started = []
    monkeypatch.setattr(research, "research_many", lambda c, tracks, on_done=None: started.extend(t.id for t in tracks))
    st = State(path=cfg.state_path)
    for i, (a, t) in enumerate([("Bicep", "Glue"), ("Peggy Gou", "Starry Night"), ("Overmono", "So U Kno")]):
        st.tracks[str(i)] = Track(id=str(i), artist=a, title=t, status=PENDING)
    st.save()
    client = create_app(cfg).test_client()

    # approve only the ticked card; unticked ones stay in Scan
    r = client.post("/approve", data={"id": ["0"], "action": "approve"})
    assert r.headers["Location"].endswith("/?hl=0")
    st = State.load(cfg.state_path)
    assert [st.tracks[i].status for i in "012"] == [APPROVED, PENDING, PENDING]

    # quick approve (✓) on a single card also starts research
    r = client.post("/move/1/approved")
    assert r.headers["Location"].endswith("/?hl=1")
    import time
    for _ in range(50):
        if "1" in started:
            break
        time.sleep(0.02)
    assert "1" in started

    # once the file lands, the card is in the Import column with an Import button
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    html = client.get("/?hl=0").get_data(as_text=True)
    imp = html.split('id="c-import"')[1].split('id="c-pl"')[0]
    assert "Glue" in imp and "Import 1 into rekordbox" in imp and 'class="card ready arrive' in imp
    buy = html.split('id="c-buy"')[1].split('id="c-import"')[0]
    assert "Starry Night" in buy and "Glue" not in buy

    # after import it's listed under its playlist in column 4
    client.post("/import")
    html = client.get("/").get_data(as_text=True)
    pl = html.split('id="c-pl"')[1]
    assert "djsync-downloads-" in pl and "Glue" in pl

    # skip from the archive view goes back to the archive
    client.post("/move/2/skipped")
    r = client.post("/move/2/pending", headers={"Referer": "http://localhost/?view=archive"})
    assert "view=archive" in r.headers["Location"]
    assert "Archive" in client.get("/?view=archive").get_data(as_text=True)


def test_ago_and_hue_filters():
    from djsync.web import create_app
    env = create_app(Config()).jinja_env
    from datetime import datetime, timedelta, timezone
    three_h = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert env.filters["ago"](three_h) == "3h ago"
    assert env.filters["ago"]("") == ""
    assert 0 <= env.filters["hue"]("Bicep Glue") < 360
    assert env.filters["initial"]("(It Goes Like) Nanana") == "I"
    assert env.filters["initial"]("...") == "♪"


# --- Claude Code backend (uses your Claude plan) ---------------------------------------

def _cc_output(answer, result_text_urls, *, is_error=False, subtype="success", result_msg=None):
    links = _json.dumps([{"title": "x", "url": u} for u in result_text_urls])
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "q"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": f'Web search results for query: "q"\n\nLinks: {links}'}]}},
        {"type": "result", "subtype": subtype, "is_error": is_error,
         "result": result_msg if result_msg is not None else _json.dumps(answer),
         **({"structured_output": answer} if answer is not None else {})},
    ]
    return "\n".join(_json.dumps(l) for l in lines)


class FakeRun:
    def __init__(self, stdout="", stderr="", exc=None):
        self.stdout, self.stderr, self.exc, self.cmd = stdout, stderr, exc, None

    def __call__(self, cmd, **kw):
        self.cmd, self.kw = cmd, kw
        if self.exc:
            raise self.exc
        return NS(stdout=self.stdout, stderr=self.stderr, returncode=0)


def test_claude_code_backend_finds_beatport_link(tmp_path):
    cfg = Config(home=tmp_path, agent_backend="claude-code")
    url = "https://www.beatport.com/track/-/20817455"
    run = FakeRun(_cc_output(_answer(url=url), [url, "https://www.mixgraph.io/tracks/bicep-glue"]))
    answer, seen = research.ask_claude_code(cfg, GLUE, run=run)
    assert answer["url"] == url and url in seen
    cmd = run.cmd
    assert cmd[1] == "-p" and "GBCFB1700200" in cmd[2]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert "--json-schema" in cmd and "--no-session-persistence" in cmd
    assert run.kw["cwd"] == str(tmp_path)  # not inside some project folder


def test_claude_code_link_must_be_in_its_search_results(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path, agent_backend="claude-code")
    run = FakeRun(_cc_output(_answer(url="https://www.beatport.com/track/glue/999"), [BP]))
    real = research.ask_claude_code
    monkeypatch.setattr(research, "ask_claude_code", lambda c, t, cands=None: real(c, t, run=run, cands=cands))
    f = research.research(cfg, GLUE, opener=_no_deezer)  # no client -> Claude Code backend
    assert f.url == "" and "ignored" in f.note


def test_claude_code_usage_limit_is_explained(tmp_path):
    cfg = Config(home=tmp_path, agent_backend="claude-code")
    run = FakeRun(_cc_output(None, [], is_error=True, subtype="error_during_execution",
                             result_msg="Claude usage limit reached. Your limit will reset at 5pm."))
    with pytest.raises(RuntimeError, match="usage limit reached; try again later"):
        research.ask_claude_code(cfg, GLUE, run=run)


def test_claude_code_not_installed(tmp_path):
    cfg = Config(home=tmp_path, agent_backend="claude-code")
    with pytest.raises(RuntimeError, match="isn't installed"):
        research.ask_claude_code(cfg, GLUE, run=FakeRun(exc=FileNotFoundError()))


def test_backend_auto(monkeypatch):
    monkeypatch.setattr(research.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert research.backend(Config()) == "claude-code"
    monkeypatch.setattr(research.shutil, "which", lambda name: None)
    monkeypatch.setattr(research, "CLAUDE_PLACES", ())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert research.backend(Config()) == "api"
    assert research.backend(Config(agent_backend="claude-code")) == "claude-code"


def test_claude_code_uses_sonnet_by_default(tmp_path):
    from djsync import config as cfgmod
    assert Config().claude_code_model == "sonnet"
    p = tmp_path / "config.toml"
    p.write_text("[agent]\nbackend = 'claude-code'\n")
    assert cfgmod.load(p).claude_code_model == "sonnet"
    p.write_text("[agent]\nclaude_code_model = 'haiku'\n")
    assert cfgmod.load(p).claude_code_model == "haiku"
    url = "https://www.beatport.com/track/-/1"
    run = FakeRun(_cc_output(_answer(url=url), [url]))
    research.ask_claude_code(Config(home=tmp_path), GLUE, run=run)
    assert run.cmd[run.cmd.index("--model") + 1] == "sonnet"


# --- "always Deezer" fixes ---------------------------------------------------------------

def test_beatport_link_matches_on_track_id_not_slug():
    # search result has a "-" slug; Claude answers with the tidy slug: same track
    client = FakeClaude(_resp(_answer(url="https://www.beatport.com/track/glue/20817455"),
                              ["https://www.beatport.com/track/-/20817455"]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.store == "beatport.com" and f.url.endswith("/20817455")


def test_beatport_different_id_still_rejected():
    client = FakeClaude(_resp(_answer(url="https://www.beatport.com/track/glue/111"),
                              ["https://www.beatport.com/track/-/20817455"]))
    assert research.research(Config(), GLUE, client, opener=_no_deezer).url == ""


def test_claude_code_tool_result_as_list_of_blocks(tmp_path):
    url = "https://www.beatport.com/track/-/20817455"
    links = _json.dumps([{"title": "x", "url": url}])
    lines = [
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [
            {"type": "text", "text": f"Links: {links}"}]}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "",
         "structured_output": _answer(url=url)},
    ]
    run = FakeRun("\n".join(_json.dumps(l) for l in lines))
    answer, seen = research.ask_claude_code(Config(home=tmp_path), GLUE, run=run)
    assert url in seen


def test_claude_code_older_version_retries_without_new_flags(tmp_path):
    url = "https://www.beatport.com/track/-/1"
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if "--json-schema" in cmd:
            return NS(stdout="", stderr="error: unknown option '--json-schema'", returncode=1)
        return NS(stdout=_cc_output(None, [url], result_msg=_json.dumps(_answer(url=url))),
                  stderr="", returncode=0)
    answer, seen = research.ask_claude_code(Config(home=tmp_path), GLUE, run=run)
    assert answer["url"] == url and len(calls) == 2
    assert "--tools" not in calls[1] and "--json-schema" not in calls[1]
    assert "--allowedTools" in calls[1] and "--model" in calls[1]


def test_claude_code_not_signed_in_is_explained(tmp_path):
    run = FakeRun(_cc_output(None, [], is_error=True, subtype="error_during_execution",
                             result_msg="Invalid API key · Please run /login"))
    with pytest.raises(RuntimeError, match="isn't signed in"):
        research.ask_claude_code(Config(home=tmp_path), GLUE, run=run)


def test_error_shown_even_when_falling_back_to_deezer(cfg):
    from djsync.web import create_app
    def deezer(url, timeout=15):
        url = getattr(url, 'full_url', url)
        return io.BytesIO(b'{"id": 1, "link": "https://www.deezer.com/track/1"}')
    f = research.research(cfg, GLUE, FakeClaude(RuntimeError("Claude Code isn't signed in")), opener=deezer)
    assert f.store == "deezer" and "isn't signed in" in f.error
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    research.apply(st.tracks["1"], f)
    st.save()
    html = create_app(cfg).test_client().get("/?tab=buy").get_data(as_text=True)
    assert "isn&#39;t signed in" in html or "isn't signed in" in html
    assert "Search Beatport" in html and "beatport.com/search/tracks?q=Bicep+Glue" in html


@pytest.mark.parametrize("answer_url", [
    "http://classic.beatport.com/track/glue-original-mix/9597224",
    "https://beatport.com/track/glue/9597224?utm_source=x",
    "https://www.beatport.com/track/glue/9597224/",
])
def test_beatport_url_variants_are_the_same_page(answer_url):
    client = FakeClaude(_resp(_answer(url=answer_url),
                              ["http://classic.beatport.com/track/glue-original-mix/9597224"]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.store == "beatport.com"
    assert f.url.startswith("https://www.beatport.com/track/") and f.url.endswith("/9597224")


def test_beatport_release_page_accepted_when_in_results():
    rel = "https://www.beatport.com/release/it-goes-like-nanana/4165364"
    client = FakeClaude(_resp(_answer(url=rel, confidence="medium"), [rel]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.store == "beatport.com" and f.url == rel


def test_beatport_release_and_track_ids_dont_mix():
    client = FakeClaude(_resp(_answer(url="https://www.beatport.com/release/x/123"),
                              ["https://www.beatport.com/track/x/123"]))
    assert research.research(Config(), GLUE, client, opener=_no_deezer).url == ""


def test_low_confidence_match_is_check_not_buy(cfg):
    from djsync.web import create_app
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Peggy Gou", title="Nanana", status=APPROVED)
    research.apply(st.tracks["1"], research.Finding(
        store="beatport.com", store_name="Beatport", url=BP, label="Dance - Peggy Gou", confidence="low"))
    st.save()
    html = create_app(cfg).test_client().get("/?tab=buy").get_data(as_text=True)
    assert 'class="store-link store-beatport-com unsure"' in html and "Buy on Beatport" not in html
    assert "Search Beatport" in html


def test_beatport_language_versions_are_the_same_page():
    client = FakeClaude(_resp(_answer(url="https://www.beatport.com/track/glue/20817455"),
                              ["https://www.beatport.com/ja/track/-/20817455"]))
    f = research.research(Config(), GLUE, client, opener=_no_deezer)
    assert f.url == "https://www.beatport.com/track/glue/20817455"
    assert research._norm_url("https://www.beatport.com/fr/release/x/1") == "https://www.beatport.com/release/x/1"



# --- also look in ~/Music ---------------------------------------------------------------

def test_music_folder_recent_files_found_and_copied_not_moved(cfg):
    import os, time
    music = cfg.extra_dirs[0]
    (music / "Music" / "Media" / "Bicep" / "Isles").mkdir(parents=True)
    bought = music / "Music" / "Media" / "Bicep" / "Isles" / "03 Glue.m4a"
    bought.write_bytes(b"")
    old = music / "Old Stuff" / "Peggy Gou - Starry Night.mp3"
    old.parent.mkdir()
    old.write_bytes(b"")
    long_ago = time.time() - 400 * 86400
    os.utime(old, (long_ago, long_ago))          # older than recent_days: ignored
    hidden = music / ".Trash" / "Bicep - Glue.mp3"
    hidden.parent.mkdir()
    hidden.write_bytes(b"")

    _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    matched, waiting = core.match_files(cfg, State.load(cfg.state_path))
    assert [(t.title, p) for t, p in matched] == [("Glue", bought)]
    assert [t.title for t in waiting] == ["Starry Night"]

    res = core.do_import(cfg, State.load(cfg.state_path))
    assert res.method == "xml"
    assert bought.exists()                       # Apple Music library left intact
    assert (cfg.library_dir / "03 Glue.m4a").exists()


def test_dj_folder_inside_music_is_not_scanned(cfg):
    cfg.library_dir = cfg.extra_dirs[0] / "DJ"
    cfg.library_dir.mkdir(parents=True)
    (cfg.library_dir / "Bicep - Glue.mp3").write_bytes(b"")   # already imported
    _approved(cfg, ("Bicep", "Glue"))
    matched, _ = core.match_files(cfg, State.load(cfg.state_path))
    assert matched == []


def test_downloads_files_still_moved_and_preferred(cfg):
    music = cfg.extra_dirs[0]
    music.mkdir()
    (music / "Bicep - Glue.mp3").write_bytes(b"")
    dl = cfg.watch_dir / "Bicep - Glue.mp3"
    dl.write_bytes(b"")
    _approved(cfg, ("Bicep", "Glue"))
    core.do_import(cfg, State.load(cfg.state_path))
    assert (music / "Bicep - Glue.mp3").exists()
    assert not dl.exists() and (cfg.library_dir / "Bicep - Glue.mp3").exists()


def test_extra_dirs_config(tmp_path):
    from djsync import config as cfgmod
    p = tmp_path / "config.toml"
    p.write_text('[paths]\nwatch_dir = "/dl"\nextra_dirs = ["/a", "/dl"]\nrecent_days = 7\n')
    c = cfgmod.load(p)
    assert c.search_dirs == [Path("/dl"), Path("/a")] and c.recent_days == 7
    assert Config().extra_dirs == [Path("~/Music").expanduser()]


# --- Beatport's own search -------------------------------------------------------------

from djsync import beatport


def _next_page(data):
    return ('<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
            + _json.dumps(data) + "</script></body></html>")


SEARCH_STYLE = {"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {"data": [
    {"track_id": 20817455, "track_name": "Jiya Jale", "mix_name": "Original Mix",
     "artists": [{"artist_name": "Whoja Vu"}], "label": {"label_name": "Malaal Records"},
     "isrc": "QZXXX2600001", "bpm": 122, "key_name": "A min", "length": 400000 // 1000,
     "release": {"release_name": "Malaal"}, "publish_date": "2026-04-03"},
    {"track_id": 20817460, "track_name": "Jiya Jale", "mix_name": "Extended Mix",
     "artists": [{"artist_name": "Whoja Vu"}]},
]}}}]}}}}

CATALOG_STYLE = {"props": {"pageProps": {"tracks": [
    {"id": 123, "name": "Glue", "mix_name": "Original Mix", "slug": "glue",
     "artists": [{"id": 1, "name": "Bicep", "slug": "bicep"}],
     "release": {"id": 9, "name": "Bicep", "label": {"name": "Ninja Tune"}},
     "length_ms": 269000, "bpm": 130, "key": {"name": "E Minor"}, "isrc": "GBCFB1700200"},
]}}}


def test_beatport_parse_search_style_json():
    cands, found = beatport.parse(_next_page(SEARCH_STYLE))
    assert found and [c.url for c in cands] == [
        "https://www.beatport.com/track/jiya-jale/20817455",
        "https://www.beatport.com/track/jiya-jale/20817460"]
    c = cands[0]
    assert (c.artists, c.mix, c.label, c.isrc, c.bpm) == ("Whoja Vu", "Original Mix", "Malaal Records", "QZXXX2600001", "122")


def test_beatport_parse_catalog_style_json():
    [c], _ = beatport.parse(_next_page(CATALOG_STYLE))
    assert c.url == "https://www.beatport.com/track/glue/123"
    assert (c.artists, c.label, c.length, c.key) == ("Bicep", "Ninja Tune", "4:29", "E Minor")


def test_beatport_parse_falls_back_to_links():
    html = ('<a href="/track/jiya-jale/20817455"><span>Jiya Jale</span> Original Mix</a>'
            '<a href="/release/malaal/1">Malaal</a>')
    [c], found = beatport.parse(html)
    assert not found and c.url.endswith("/20817455") and "Jiya Jale" in c.title


def _page_opener(html):
    def opener(url, timeout=15):
        url = getattr(url, "full_url", url)
        if "beatport.com/search" in url:
            return io.BytesIO(html.encode())
        return io.BytesIO(b'{"error": {}}')
    return opener


def test_research_isrc_match_skips_claude():
    t = Track(id="1", artist="Bicep", title="Glue", isrc="GBCFB1700200")
    client = FakeClaude()  # would fail if called
    f = research.research(Config(), t, client, opener=_page_opener(_next_page(CATALOG_STYLE)))
    assert f.url == "https://www.beatport.com/track/glue/123" and f.confidence == "high"
    assert client.calls == []


def test_research_claude_picks_from_beatport_results():
    t = Track(id="1", artist="Whoja Vu", title="Jiya Jale")
    ext = "https://www.beatport.com/track/jiya-jale/20817460"
    # Claude picks the Extended Mix; it didn't web-search, so seen comes only from Beatport.
    client = FakeClaude(_resp(_answer(url=ext, title="Jiya Jale", mix="Extended Mix", artists="Whoja Vu"), []))
    f = research.research(Config(), t, client, opener=_page_opener(_next_page(SEARCH_STYLE)))
    assert f.url == ext and f.label == "Jiya Jale (Extended Mix) - Whoja Vu"
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Beatport's own search" in prompt and "20817455" in prompt and "Malaal Records" in prompt


def test_research_name_match_when_claude_unavailable():
    t = Track(id="1", artist="Whoja Vu", title="Jiya Jale")
    f = research.research(Config(), t, FakeClaude(RuntimeError("Not logged in")),
                          opener=_page_opener(_next_page(SEARCH_STYLE)))
    assert f.store == "beatport.com" and f.confidence == "low" and not f.error
    assert "check the version" in f.note


def test_beatport_search_can_be_turned_off():
    t = Track(id="1", artist="Bicep", title="Glue", isrc="GBCFB1700200")
    client = FakeClaude(_resp({"found": False, "store": "", "url": "", "title": "", "mix": "",
                               "artists": "", "confidence": "low", "reason": "x"}, []))
    f = research.research(Config(beatport_direct=False), t, client,
                          opener=_page_opener(_next_page(CATALOG_STYLE)))
    assert f.url == "" and len(client.calls) == 1


def test_beatport_test_command(monkeypatch, capsys):
    from djsync import cli, config as cfgmod
    monkeypatch.setattr(cfgmod, "load", lambda: Config())
    monkeypatch.setattr(beatport, "search", lambda a, q, *x, **k: beatport.Result(
        candidates=beatport.parse(_next_page(SEARCH_STYLE))[0], status=200, found_json=True))
    assert cli.main(["beatport-test", "Whoja", "Vu", "Jiya", "Jale"]) == 0
    out = capsys.readouterr().out
    assert "page data found: yes" in out and "2 track(s)" in out



def test_buy_card_has_no_duplicate_links(cfg):
    from djsync.web import create_app
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    research.apply(st.tracks["1"], research.Finding(
        store="beatport.com", store_name="Beatport", url=BP, label="Glue (Extended Mix) - Bicep",
        confidence="high", deezer_url="https://www.deezer.com/track/1"))
    st.save()
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    card = html.split('class="card buy')[1].split("</article>")[0]
    assert card.count(BP) == 1                                   # the store name is the link
    foot = card.split('class="card-foot"')[1].split('class="more-list"')[0]
    assert "Search Beatport" not in foot                         # confident Beatport pick: no extra search
    assert card.count("beatport.com/search") == 1                # ...but still once in the ⋯ menu
    assert card.count("Listen on Deezer") == 1 and card.count("Bandcamp") == 1
    assert "btn primary" not in card                             # no loud buttons on cards


# --- scanning playlists as well as Liked Songs ------------------------------------------

def _sp_track(tid, artist, title):
    return {"id": tid, "name": title, "artists": [{"name": artist}], "album": {"name": "LP", "images": []},
            "external_ids": {}, "external_urls": {}, "type": "track"}


class FakeSpotifyPlaylists:
    def __init__(self, liked, playlists, broken=()):
        self.liked, self.pls, self.broken = liked, playlists, set(broken)

    def current_user_saved_tracks(self, limit, offset):
        items = [{"added_at": "2026-11-10T00:00:00Z", "track": _sp_track(*t)} for t in self.liked[offset:offset + limit]]
        return {"items": items, "next": None}

    def current_user_playlists(self, limit, offset):
        items = [{"id": pid, "name": name, "tracks": {"total": len(tr)}, "owner": {"display_name": "me"}}
                 for pid, (name, tr) in self.pls.items()]
        return {"items": items, "next": None}

    def playlist_items(self, pid, limit, offset, additional_types):
        if pid in self.broken:
            raise RuntimeError("404 Not Found")
        name, tr = self.pls[pid]
        items = [{"added_at": "2026-11-12T00:00:00Z", "track": _sp_track(*t)} for t in tr]
        items.append({"added_at": "x", "track": {"id": "ep1", "type": "episode", "name": "Podcast"}})
        return {"items": items, "next": None}


def _fake_sp():
    return FakeSpotifyPlaylists(
        liked=[("1", "Bicep", "Glue")],
        playlists={"pA": ("Warm-up", [("2", "Overmono", "So U Kno"), ("1", "Bicep", "Glue")]),
                   "pB": ("Peak time", [("3", "Kettama", "It Gets Better")]),
                   "pC": ("Discover Weekly", [("4", "X", "Y")])},
        broken={"pC"})


def test_scan_defaults_to_liked_songs_only(cfg):
    st = State.load(cfg.state_path)
    r = core.scan(cfg, st, sp=_fake_sp(), library=[])
    assert set(st.tracks) == {"1"} and r.sources == 1
    assert [p["name"] for p in st.playlists] == ["Warm-up", "Peak time", "Discover Weekly"]
    assert st.sources == ["liked"]


def test_scan_selected_playlists_dedupes_and_remembers(cfg):
    st = State.load(cfg.state_path)
    r = core.scan(cfg, st, sp=_fake_sp(), library=[], sources=["liked", "pA", "pC"])
    assert set(st.tracks) == {"1", "2"}                   # Glue only once; episode skipped
    assert st.tracks["2"].source == "Warm-up" and st.tracks["1"].source == "Liked Songs"
    assert r.liked_checked == 2 and r.new_missing == 2
    assert any("Discover Weekly" in e for e in r.errors)  # unreadable playlist reported, scan goes on
    st2 = State.load(cfg.state_path)
    assert st2.sources == ["liked", "pA", "pC"]            # remembered for next time
    core.scan(cfg, st2, sp=_fake_sp(), library=[])        # next scan uses that choice
    assert st2.sources == ["liked", "pA", "pC"]


def test_scan_skips_deleted_playlists(cfg):
    st = State.load(cfg.state_path)
    st.sources = ["pGone", "pB"]
    r = core.scan(cfg, st, sp=_fake_sp(), library=[])
    assert set(st.tracks) == {"3"} and r.sources == 1 and not r.errors


def test_picker_on_the_board(cfg, monkeypatch):
    from djsync.web import create_app
    monkeypatch.setattr(core.spotify, "client", lambda c: _fake_sp())
    monkeypatch.setattr(core.rekordbox, "read_library", lambda c: [])
    client = create_app(cfg).test_client()

    # before any scan: Liked Songs ticked, offer to load playlists
    html = client.get("/").get_data(as_text=True)
    assert 'value="liked" checked' in html and "Load my playlists" in html

    client.post("/playlists/refresh", data={"picker": "1", "source": ["liked"]})
    html = client.get("/?pick=1").get_data(as_text=True)
    assert "Warm-up" in html and "Peak time" in html and 'value="pA" ' in html

    # tick a playlist and scan
    client.post("/scan", data={"picker": "1", "source": ["liked", "pB"]})
    html = client.get("/").get_data(as_text=True)
    assert "Liked Songs + 1 more" in html
    assert 'value="pB" checked' in html and "♫ Peak time" in html

    # nothing ticked: refused, nothing changes
    r = client.post("/scan", data={"picker": "1"})
    assert "pick=1" in r.headers["Location"]
    assert State.load(cfg.state_path).sources == ["liked", "pB"]



def test_find_best_source_is_first_on_the_card(cfg):
    from djsync.web import create_app
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Bicep", title="Glue", status=APPROVED)
    research.apply(st.tracks["1"], research.Finding(
        store="deezer", store_name="Deezer", url="https://www.deezer.com/track/1", label="Glue - Bicep"))
    st.save()
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    card = html.split('class="card buy')[1].split("</article>")[0]
    foot = card.split('class="card-foot"')[1].split('class="more-list"')[0]
    assert foot.lstrip(">").strip().startswith("<form") and "Find best source" in foot.split("Search Beatport")[0]
    assert card.count("Find best source") == 1 and "Research again" not in card
    assert 'class="store-link store-deezer"' in card



# --- no pop-ups: results show on the board, errors in a bar ------------------------------

def test_no_popups_and_clicked_card_is_highlighted(cfg, monkeypatch):
    from djsync.web import create_app
    monkeypatch.setattr(research, "research_many", lambda c, tracks, on_done=None: None)
    _approved(cfg, ("Bicep", "Glue"))
    client = create_app(cfg).test_client()
    r = client.post("/research", data={"id": "1"})
    assert r.headers["Location"].endswith("/?hl=1")          # the card you clicked
    html = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "toast" not in html and "errbar" not in html
    assert 'class="card buy arrive' in html


def test_errors_shown_in_a_bar_not_a_popup(cfg):
    from djsync.web import create_app
    client = create_app(cfg).test_client()
    html = client.post("/import", follow_redirects=True).get_data(as_text=True)
    assert 'class="errbar"' in html and "No approved tracks found" in html


def test_scan_summary_under_scan_title(cfg):
    from djsync.web import create_app
    st = State.load(cfg.state_path)
    core.scan(cfg, st, sp=_fake_sp(), library=[], sources=["liked", "pC"])
    assert st.last_scan_summary == "1 new, 1 problem"
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert "1 new, 1 problem" in html.split('id="c-scan"')[1].split("</header>")[0]


# --- ✓ / ✕ on Buy and Import cards ------------------------------------------------------

def test_import_one_track_with_tick(cfg):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    (cfg.watch_dir / "Peggy Gou - Starry Night.mp3").write_bytes(b"")
    client = create_app(cfg).test_client()
    r = client.post("/import", data={"id": "2"})          # ✓ on Starry Night only
    assert r.headers["Location"].endswith("/?hl=2")
    st = State.load(cfg.state_path)
    assert st.tracks["2"].status == IMPORTED and st.tracks["1"].status == APPROVED
    assert (cfg.watch_dir / "Bicep - Glue.mp3").exists()   # the other file wasn't touched
    html = client.get(r.headers["Location"]).get_data(as_text=True)
    pl = html.split('id="c-pl"')[1]
    assert '<li class="arrive">' in pl and "Starry Night" in pl
    imp = html.split('id="c-import"')[1].split('id="c-pl"')[0]
    assert "Glue" in imp and "Starry Night" not in imp


def test_buy_and_import_cards_have_quick_buttons(cfg):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"), ("Peggy Gou", "Starry Night"))
    (cfg.watch_dir / "Bicep - Glue.mp3").write_bytes(b"")
    html = create_app(cfg).test_client().get("/").get_data(as_text=True)
    buy = html.split('class="card buy')[1].split("</article>")[0]
    ready = html.split('class="card ready')[1].split("</article>")[0]
    assert 'class="ic no"' in buy and 'class="ic ok"' not in buy
    assert 'class="ic ok"' in ready and 'class="ic no"' in ready
    assert ">Skip<" not in buy and ">Skip<" not in ready   # not repeated in the ⋯ menus
    client = create_app(cfg).test_client()
    client.post("/move/2/skipped")
    assert State.load(cfg.state_path).tracks["2"].status == SKIPPED


# --- Buy column: details, other versions, bought marker -----------------------------------

def test_research_keeps_pick_details_and_other_versions():
    t = Track(id="1", artist="Whoja Vu", title="Jiya Jale")
    ext = "https://www.beatport.com/track/jiya-jale/20817460"
    client = FakeClaude(_resp(_answer(url=ext, title="Jiya Jale", mix="Extended Mix", artists="Whoja Vu"), []))
    f = research.research(Config(), t, client, opener=_page_opener(_next_page(SEARCH_STYLE)))
    assert f.url == ext
    assert [a["label"] for a in f.alts] == ["Original Mix"]        # the version not picked
    assert f.alts[0]["url"].endswith("/20817455")
    assert "Malaal Records" in f.alts[0]["meta"] and "122 BPM" in f.alts[0]["meta"]


def test_research_isrc_pick_has_details():
    t = Track(id="1", artist="Bicep", title="Glue", isrc="GBCFB1700200")
    f = research.research(Config(), t, FakeClaude(), opener=_page_opener(_next_page(CATALOG_STYLE)))
    assert f.meta == "Ninja Tune · 4:29 · 130 BPM · E Minor" and f.alts == []


def test_buy_card_shows_details_alts_and_bought(cfg):
    from djsync.web import create_app
    st = State(path=cfg.state_path)
    st.tracks["1"] = Track(id="1", artist="Whoja Vu", title="Jiya Jale", status=APPROVED)
    research.apply(st.tracks["1"], research.Finding(
        store="beatport.com", store_name="Beatport", url="https://www.beatport.com/track/jiya-jale/2",
        label="Jiya Jale (Extended Mix) - Whoja Vu", confidence="high", meta="Malaal · 2026-04-03 · 6:40 · 122 BPM",
        alts=[{"url": "https://www.beatport.com/track/jiya-jale/1", "label": "Original Mix", "meta": "x"}]))
    st.tracks["2"] = Track(id="2", artist="Bicep", title="Glue", status=APPROVED)
    st.save()
    client = create_app(cfg).test_client()
    html = client.get("/").get_data(as_text=True)
    card = html.split('class="card buy')[1].split("</article>")[0]
    assert "Malaal · 2026-04-03 · 6:40 · 122 BPM" in card
    assert "Other versions" in card and "Original Mix ↗" in card
    assert html.split('id="c-buy"')[1].split("</header>")[0].count(">2<") == 1   # 2 to buy

    # ✓ marks it bought: dimmed, sorted after the rest, counted separately
    r = client.post("/bought/1")
    assert r.headers["Location"].endswith("/?hl=1")
    assert State.load(cfg.state_path).tracks["1"].bought
    html = client.get("/").get_data(as_text=True)
    buy = html.split('id="c-buy"')[1].split('id="c-import"')[0]
    assert buy.index("Glue") < buy.index("Jiya Jale")                  # still-to-buy first
    assert "1 bought, waiting for the download" in buy and "Bought · waiting for the download" in buy
    client.post("/bought/1")                                           # ✓ again: undo
    assert not State.load(cfg.state_path).tracks["1"].bought


def test_bought_flag_cleared_when_skipped(cfg):
    from djsync.web import create_app
    _approved(cfg, ("Bicep", "Glue"))
    client = create_app(cfg).test_client()
    client.post("/bought/1")
    client.post("/move/1/skipped")
    assert not State.load(cfg.state_path).tracks["1"].bought


# --- stage 1: setup checklist, settings page, first run -------------------------------------

from djsync import config as configmod
from djsync import setup as setupmod


def test_config_save_roundtrip(tmp_path):
    c = Config(home=tmp_path, client_id='abc123', extra_dirs=[Path.home() / "Music", Path("/Volumes/USB")],
               stores=["beatport", "deezer"], agent_auto=False, recent_days=30, claude_code_model="haiku")
    p = configmod.save(c)
    text = p.read_text()
    assert 'extra_dirs = ["~/Music", "/Volumes/USB"]' in text and "# djsync configuration" in text
    c2 = configmod.load(p)
    for f in ("client_id", "extra_dirs", "stores", "agent_auto", "recent_days", "claude_code_model"):
        assert getattr(c, f) == getattr(c2, f), f


def test_setup_checks(cfg):
    checks = {c.key: c for c in setupmod.all_checks(cfg, read=lambda c: [1, 2, 3])}
    assert not checks["spotify_app"].ok and not checks["spotify_login"].ok
    assert checks["rekordbox"].ok and "3 tracks" in checks["rekordbox"].detail
    assert checks["folders"].ok
    assert setupmod.needs_setup(cfg)

    def broken(c):
        raise RuntimeError("file is not a database")
    rb = setupmod.rekordbox_library(cfg, read=broken)
    assert not rb.ok and "key download" in rb.detail

    agent = setupmod.research_agent(cfg, which=lambda name: None)
    assert not agent.ok and agent.optional and "Install Claude Code" in agent.detail


def test_setup_page_and_saving_client_id(cfg, monkeypatch):
    from djsync.web import create_app
    monkeypatch.setattr(core.rekordbox, "read_library", lambda c: [1] * 1234)
    client = create_app(cfg).test_client()
    html = client.get("/setup").get_data(as_text=True)
    assert "Set up djsync" in html and "developer.spotify.com/dashboard" in html
    assert cfg.redirect_uri in html and "1,234 tracks" in html
    assert "Finish setting up djsync" in client.get("/").get_data(as_text=True)

    client.post("/setup/spotify-id", data={"client_id": "not valid!"})
    assert cfg.client_id == ""
    client.post("/setup/spotify-id", data={"client_id": "1a2b3c4d5e"})
    assert cfg.client_id == "1a2b3c4d5e"
    assert configmod.load(cfg.home / "config.toml").client_id == "1a2b3c4d5e"

    cfg.spotify_cache.parent.mkdir(parents=True, exist_ok=True)
    cfg.spotify_cache.write_text("{}")                     # logged in
    assert "Finish setting up djsync" not in client.get("/").get_data(as_text=True)


def test_settings_page_saves_and_applies(cfg):
    from djsync.web import create_app
    client = create_app(cfg).test_client()
    html = client.get("/settings").get_data(as_text=True)
    assert "Downloads folder" in html and 'name="claude_code_model"' in html
    form = {
        "client_id": "abc123", "watch_dir": "~/Downloads/Bought", "extra_dirs": "~/Music\n/Volumes/USB\n",
        "recent_days": "30", "library_dir": "~/Music/DJ", "move_files": "on",
        "playlist_prefix": "new-", "rekordbox_mode": "xml", "rekordbox_db_path": "",
        "agent_enabled": "on", "agent_backend": "claude-code", "claude_code_model": "haiku",
        "beatport_direct": "on", "stores": ["beatport", "deezer"], "threshold": "90", "file_threshold": "70",
    }
    r = client.post("/settings", data=form)
    assert r.headers["Location"].endswith("/settings?saved=1")
    assert cfg.watch_dir == Path("~/Downloads/Bought").expanduser()      # applied right away
    assert cfg.extra_dirs == [Path.home() / "Music", Path("/Volumes/USB")]
    assert (cfg.playlist_prefix, cfg.rekordbox_mode, cfg.claude_code_model) == ("new-", "xml", "haiku")
    assert cfg.agent_auto is False and cfg.stores == ["beatport", "deezer"]
    saved = configmod.load(cfg.home / "config.toml")
    assert saved.watch_dir == cfg.watch_dir and saved.threshold == 90
    assert "✓ Saved" in client.get(r.headers["Location"]).get_data(as_text=True)


def test_settings_rejects_bad_values(cfg):
    from djsync.web import create_app
    client = create_app(cfg).test_client()
    html = client.post("/settings", data={"playlist_prefix": "", "recent_days": "abc", "stores": []},
                       follow_redirects=True).get_data(as_text=True)
    assert "Not saved" in html and "playlist prefix" in html and "recent days" in html
    assert cfg.playlist_prefix == "djsync-downloads-"
    assert not (cfg.home / "config.toml").exists()


def test_plain_djsync_opens_the_board(monkeypatch, tmp_path):
    from djsync import cli, web
    monkeypatch.setattr(configmod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(configmod, "load", lambda: Config(home=tmp_path))
    opened = []
    monkeypatch.setattr(web, "serve", lambda c, open_browser=True: opened.append(open_browser))
    assert cli.main([]) == 0
    assert opened == [True] and (tmp_path / "config.toml").exists()    # first run made settings



# --- started from the Desktop: no Terminal PATH, python.org certificates ---------------------

def test_find_claude_outside_path(tmp_path, monkeypatch):
    exe = tmp_path / ".local" / "bin" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(research, "CLAUDE_PLACES", ("~/.local/bin/claude",))
    assert research.find_claude(Config(), which=lambda n: None) == str(exe)
    assert research.backend(Config()) == "claude-code" or research.shutil.which("claude")
    # an explicit path in Settings wins
    other = tmp_path / "elsewhere" / "claude"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert research.find_claude(Config(claude_path=str(other)), which=lambda n: None) == str(other)


def test_requests_use_certifi_bundle():
    from djsync import net
    import certifi
    assert net._CONTEXT.verify_mode.name == "CERT_REQUIRED"
    assert certifi.where()


def test_certificate_error_is_explained():
    def opener(req, timeout=15):
        raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    r = beatport.search("Bicep", "Glue", opener)
    assert "certificates" in r.error and r.candidates == []


# --- adding tracks by hand -------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("Fear of Water by SYML", ("SYML", "Fear of Water")),
    ("SYML - Fear of Water", ("SYML", "Fear of Water")),
    ("SYML – Fear of Water", ("SYML", "Fear of Water")),
    ('"Stand By Me by Ben E. King"', ("Ben E. King", "Stand By Me")),
    ("Glue", ("", "Glue")),
    ("   ", ("", "")),
])
def test_parse_line(line, expected):
    assert core.parse_line(line) == expected


class FakeSpotifySearch:
    def search(self, q, type, limit):
        if "Fear of Water" in q:
            return {"tracks": {"items": [
                {"id": "sp1", "name": "Fear of the Water", "artists": [{"name": "Someone"}], "album": {}},
                {"id": "sp2", "name": "Fear of Water", "artists": [{"name": "SYML"}],
                 "album": {"name": "In My Body", "images": [{"url": "https://img/x.jpg"}]},
                 "external_ids": {"isrc": "USX123"}, "duration_ms": 240000, "type": "track"},
            ]}}
        return {"tracks": {"items": []}}


def test_add_manual_uses_spotify_details_and_goes_to_buy(cfg):
    st = State.load(cfg.state_path)
    r = core.add_manual(cfg, st, "Fear of Water by SYML\nUnknown Artist - Rare Dub\n\nbad", sp=FakeSpotifySearch(),
                        library=[LibraryTrack("Bicep", "Glue", "/x.mp3")])
    ids = [t.id for t in r.added]
    assert ids[0] == "sp2" and ids[1].startswith("manual-")     # Spotify match vs typed-in only
    t = State.load(cfg.state_path).tracks["sp2"]
    assert (t.status, t.isrc, t.album, t.source) == (APPROVED, "USX123", "In My Body", "Added by hand")
    rare = State.load(cfg.state_path).tracks[ids[1]]
    assert (rare.artist, rare.title, rare.status) == ("Unknown Artist", "Rare Dub", APPROVED)
    assert len(r.added) == 3                                    # "bad" is read as a title on its own


def test_add_manual_skips_what_you_already_have(cfg):
    st = State.load(cfg.state_path)
    st.tracks["p1"] = Track(id="p1", artist="Peggy Gou", title="Starry Night", status=PENDING)
    st.tracks["a1"] = Track(id="a1", artist="Overmono", title="So U Kno", status=APPROVED)
    st.save()
    r = core.add_manual(cfg, State.load(cfg.state_path),
                        "Glue by Bicep\nOvermono - So U Kno\nStarry Night by Peggy Gou",
                        library=[LibraryTrack("Bicep", "Glue", "/x.mp3")])
    assert r.in_rekordbox == ["Bicep - Glue"] and r.already_listed == ["Overmono - So U Kno"]
    assert [t.id for t in r.added] == ["p1"]                     # was in Scan: moved to Buy
    assert State.load(cfg.state_path).tracks["p1"].status == APPROVED


def test_add_tracks_on_the_board(cfg, monkeypatch):
    from djsync.web import create_app
    started = []
    monkeypatch.setattr(research, "research_many", lambda c, tracks, on_done=None: started.extend(t.id for t in tracks))
    monkeypatch.setattr(core.rekordbox, "read_library", lambda c: [LibraryTrack("Bicep", "Glue", "/x")])
    client = create_app(cfg).test_client()
    html = client.get("/").get_data(as_text=True)
    assert "＋ Add tracks" in html and 'name="tracks"' in html
    r = client.post("/add", data={"tracks": "Fear of Water by SYML\nGlue by Bicep"})
    st = State.load(cfg.state_path)
    [new] = [t for t in st.tracks.values() if t.title == "Fear of Water"]
    assert r.headers["Location"].endswith(f"/?hl={new.id}")
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started == [new.id]                                   # research starts right away
    html = client.get(r.headers["Location"]).get_data(as_text=True)
    buy = html.split('id="c-buy"')[1].split('id="c-import"')[0]
    assert "Fear of Water" in buy and "added by hand" in buy
    assert "Already in your rekordbox library, not added: Bicep - Glue" in html
