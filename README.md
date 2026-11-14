# djsync

Finds songs you've liked on Spotify that aren't in your rekordbox library. You
untick the ones you don't want, buy the rest from a DJ store, and djsync adds the
purchased files to a rekordbox playlist named `downloads-YYYY-MM-DD`.

```
Spotify Liked Songs ──► compare with rekordbox ──► review page (untick what you don't want)
                                                          │
             rekordbox playlist  ◄── import files ◄── buy on Beatport / Bandcamp / Traxsource / Juno
          downloads-2026-11-14       from ~/Downloads
```

djsync doesn't download from streaming services. You buy the tracks yourself,
and djsync finds the files in your downloads folder and imports them.

## What it does

1. **Scan**: reads your Liked Songs through the official Spotify API, using
   read-only access. It compares them with your rekordbox collection by artist
   and title, ignoring differences like `(Original Mix)`, `Extended Mix`,
   `Radio Edit`, `feat.`, accents, and `&`/`,`/`x` between artist names.
   Remixes count as different tracks.
2. **Review**: opens a local web page listing the missing tracks, all ticked.
   Untick the ones you don't want and click **Approve**. Tracks you untick
   aren't suggested again, but you can restore them from the Skipped list.
3. **Buy**: each approved track has one-click search links for Beatport,
   Bandcamp, Traxsource, Juno and Deezer. **Open all on Beatport** opens a
   Beatport search tab for every approved track that doesn't have a file yet
   (up to 30 at a time), and there's a button for each of the other stores too.
   **All stores** on a track opens its search on every store. Search terms
   leave out "(Original Mix)" and "feat." but keep remix names. Buy the track
   and let it download. Deezer doesn't sell downloads; its link is there so you
   can listen and check you have the right version.
4. **Find the files**: djsync looks in your downloads folder **and all its
   subfolders**, including unpacked zip files. Hidden folders and your DJ
   library folder are skipped. It matches files by their tags and by filename,
   so names like `bicep_glue_320.mp3`, `Glue [Bicep] 24bit.wav` or
   `03 So U Kno.flac` inside an artist folder are still found. A different
   remix of the same song is **not** matched. Click **Rescan Downloads**
   after new files arrive.
5. **Attach a file by hand**: if a file isn't picked up, or the wrong one was,
   click **Attach file…** or **Wrong file?** on the track. You can then:
   - pick from all audio files in Downloads, best match first, with a filter box
   - click **Choose in Finder…** to open a normal macOS file picker and choose
     the file from anywhere
   - paste a path (in Finder, ⌥⌘C copies a file's path)

   Files you attach always take priority over automatic matches. A file picked
   from outside Downloads is copied into your DJ folder, not moved.
6. **Import**: djsync moves the matched files into your music folder, adds
   them to rekordbox and creates the `downloads-<date>` playlist.

## Install (macOS)

You need Python 3.11 or later (`brew install python`).

```bash
git clone https://github.com/ashweta1/djsync.git
cd djsync
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
djsync init
```

### Spotify app (one time)

1. Go to <https://developer.spotify.com/dashboard> and click **Create app**.
2. Add the redirect URI `http://127.0.0.1:8888/callback` exactly. Spotify no
   longer accepts `localhost`.
3. Tick **Web API**, save, and copy the **Client ID**.
4. Paste the Client ID into `~/.djsync/config.toml` as `client_id = "..."`.

djsync uses PKCE login, so it doesn't need a client secret. The first scan
opens your browser so you can log in to Spotify. After that, the login is
cached in `~/.djsync/spotify_token.json`.

### rekordbox access

djsync uses [pyrekordbox](https://github.com/dylanljones/pyrekordbox) to read
rekordbox 6/7's encrypted `master.db`. If it reports that it can't unlock the
database (common on rekordbox 7), run this once:

```bash
python -m pyrekordbox download-key
```

## Usage

```bash
djsync review --scan     # scan Spotify, then open the review page
```

All the steps can be done from the review page. Or use the command line:

```bash
djsync scan              # list liked songs that aren't in rekordbox
djsync import --dry-run  # show which downloaded files match approved tracks
djsync import            # import them and create the downloads-<date> playlist
djsync import --watch    # keep checking every 30s until all approved tracks arrive
```

### How the playlist is written

`[rekordbox] mode` in the config controls this:

| mode | what happens |
|---|---|
| `auto` (default) | Writes directly to rekordbox's database. If that fails, it writes an XML file instead. |
| `db` | Database only. **rekordbox must be closed.** `master.db` is backed up to `~/.djsync/backups/` before every write. |
| `xml` | Writes `~/.djsync/xml/downloads-<date>.xml`. In rekordbox, go to **Preferences → Advanced → Database → rekordbox xml** and choose this file. The playlist then appears under **rekordbox xml** in the sidebar: right-click it and choose **Import Playlist**. |

After importing, open the playlist in rekordbox and analyse the new tracks to
get their BPM, key and waveforms.

Writing directly to the database relies on pyrekordbox's write support, which
its authors call experimental. Your `master.db` is backed up before every
write. If anything goes wrong, quit rekordbox and copy a backup back to
`~/Library/Pioneer/rekordbox/master.db`, or set `mode = "xml"`.

## Configuration

`~/.djsync/config.toml`:

```toml
[paths]
watch_dir = "~/Downloads"    # where store purchases land
library_dir = "~/Music/DJ"   # where imported tracks are moved
move_files = true            # false = copy instead of move

[match]
threshold = 88               # 0-100; raise it if wrong tracks match, lower it if owned tracks still show up
file_threshold = 75          # 0-100; lower it if downloaded files aren't found, raise it if the wrong file is picked

[stores]
enabled = ["beatport", "bandcamp", "traxsource", "juno", "deezer"]   # also: "itunes", "amazon"
```

State such as pending, approved and skipped tracks is stored in
`~/.djsync/state.json`.

## Development

```bash
pip install -e '.[dev]'
pytest
```
