# djsync

Finds songs you've liked on Spotify that aren't in your rekordbox library. You
untick the ones you don't want, buy the rest from a DJ store, and djsync adds the
purchased files to a rekordbox playlist named `djsync-downloads-YYYY-MM-DD`.

```
Spotify Liked Songs ──► compare with rekordbox ──► review page (untick what you don't want)
                                                          │
             rekordbox playlist  ◄── import files ◄── buy on Beatport / Bandcamp
   djsync-downloads-2026-11-14       from ~/Downloads
```

djsync doesn't download from streaming services. You buy the tracks yourself,
and djsync finds the files in your downloads folder and imports them.

## Getting started (for friends)

You need a Mac with **rekordbox 6 or 7** and a **Spotify** account. A **Claude
Pro or Max** plan is optional; it lets djsync find the best place to buy each
track.

1. **Install.** Open Terminal (press ⌘ Space, type *Terminal*, press Return),
   paste this line and press Return:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/ashweta1/djsync/main/install.sh | bash
   ```

   If it says you need Python, install it from
   [python.org/downloads](https://www.python.org/downloads/), then paste the
   line again. Everything goes into your home folder, and you won't be asked
   for an admin password.
2. **Open djsync.** Double-click **djsync** on your Desktop. The first time,
   macOS may say it can't check the file. If so, right-click it, choose
   **Open**, then **Open** again. A Terminal window opens, which you keep open
   while using djsync, and your browser shows djsync.
3. **Follow the setup page.** It walks you through creating a free Spotify app
   (about five minutes), connecting Spotify, and checking your rekordbox
   library. Each step shows ✓ when it's done.
4. **Use it.** On the board, click **Scan Spotify**.

**To update**, paste the install line into Terminal again. Your settings and
track list are kept. **Settings** in the top bar has all the options: folders,
playlist name, which stores to show, and the research agent.

### Optional: the research agent

With a Claude Pro or Max plan, install Claude Code (paste this into Terminal):

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then run `claude` once, type `/login` and sign in. The setup page shows ✓ once
djsync can find it. If you'd rather pay per use with a Claude API key, choose
**Claude API key** under Settings → Research agent and set `ANTHROPIC_API_KEY`.

## The board

`djsync review` opens a board with four columns. Each track is a card that
moves from left to right:

| 1. Scan | 2. Buy | 3. Import | 4. Playlists |
|---|---|---|---|
| New tracks from Spotify that aren't in rekordbox. **✓** approves a card and **✕** skips it. You can also tick several, then **Approve selected** or **Skip unselected**. | Approved tracks, each with the best place to buy it. | Tracks whose file has turned up in Downloads or Music. **Import** adds them to rekordbox. | Each day's `djsync-downloads-<date>` playlist and its tracks. |

**Buy** is the widest column by default, since that's where most of the time
goes. Click any column's header to make that column the wide one instead;
djsync remembers your choice. Each Buy card shows the agent's pick with its
label, release date, length, BPM and key, plus links to the other versions
Beatport has, such as Original, Extended or remixes. After buying a track,
click **✓** on its card: it dims and says *Bought, waiting for the download*
until the file arrives and the card moves to Import.

A card that just moved is highlighted in its new column. Skipped tracks, and
ones that reached rekordbox some other way, are under **Archive** in the
header. On a phone, swipe between the columns.

### Choosing what to scan

**Scan Spotify** reads your **Liked Songs** by default. Click the **▾** next
to it to choose more: it lists all your playlists (made or followed) with their
track counts and a filter box. Tick any combination and click **Scan selected**.
djsync remembers the choice for the next **Scan Spotify**. Each Scan card says
which playlist it came from, and a track in several playlists appears once.
Spotify doesn't let apps read some of its own generated playlists, such as
Discover Weekly. If one can't be read, djsync says so and scans the rest.

After updating to this version, Spotify asks you to log in once more, because
djsync now also asks to read your playlists.

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
   Bandcamp and Deezer. Search terms
   leave out "(Original Mix)" and "feat." but keep remix names. Buy the track
   and let it download. Deezer doesn't sell downloads; its link is there so you
   can listen and check you have the right version.
   **Research agent:** when you approve tracks, an AI agent (Claude, with web
   search) finds the best place to buy each one:
   1. It searches **Beatport's own catalogue first**: the same search as the
      box on beatport.com, run from your Mac. That finds new releases that web
      search engines haven't indexed yet. If a result has the same ISRC as the
      Spotify track, it's picked straight away. Otherwise Claude compares
      Beatport's results with the Spotify track: artist, title, remix/version,
      length and ISRC. It picks the
      Extended or Original Mix when Spotify has a radio edit, but never a
      different remix.
   2. If Beatport doesn't have it, it **searches the web** for another
      legitimate store selling the download (Bandcamp, Beatsource, Traxsource,
      Apple/iTunes, Amazon, Qobuz, 7digital, Boomkat, Bleep) or an official
      free download on SoundCloud or Hypeddit.
   3. If no store sells it, it gives the **Deezer** link so you can still listen.

   Each track then shows a **Buy on Beatport ↗** button (or the store it
   found), the exact version, how confident the match is and a one-line reason.
   Links are only accepted from known stores, and only if they really appeared
   in the search results, so the agent can't invent a link or send you to a
   pirate site. Use **Find best link** or **Research again** on any track, or
   **Find best links for N tracks** for all of them. The plain store searches
   are still there too.

4. **Find the files**: djsync looks in your downloads folder **and all its
   subfolders**. It also checks files added to **~/Music** in the last 90 days,
   which is where iTunes/Apple Music purchases land. Files from Music are
   *copied* into your DJ folder, never moved, so your Apple Music library stays
   as it is, including unpacked zip files. Hidden folders and your DJ
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
   them to rekordbox and adds them to the day's `djsync-downloads-<date>` playlist.

## Install for development

```bash
git clone https://github.com/ashweta1/djsync.git
cd djsync
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
djsync
```

## Usage

```bash
djsync                   # open the board (same as the Desktop launcher)
djsync review --scan     # scan Spotify, then open the board
```

All the steps can be done from the review page. Or use the command line:

```bash
djsync scan              # list liked songs that aren't in rekordbox
djsync research          # find the best place to buy each approved track (--again to redo)
djsync beatport-test "Whoja Vu Jiya Jale"   # check djsync can read Beatport's search
djsync import --dry-run  # show which downloaded files match approved tracks
djsync import            # import them into today's djsync-downloads-<date> playlist
djsync import --watch    # keep checking every 30s until all approved tracks arrive
djsync import --retry    # rekordbox was open last time? quit it, then add those playlists into rekordbox
```

### How the playlist is written

`[rekordbox] mode` in the config controls this:

| mode | what happens |
|---|---|
| `auto` (default) | Writes directly to rekordbox's database. If that fails, it writes an XML file instead. |
| `db` | Database only. **rekordbox must be closed.** `master.db` is backed up to `~/.djsync/backups/` before every write. |
| `xml` | Writes `~/.djsync/xml/djsync-downloads-<date>.xml`. In rekordbox, go to **Preferences → Advanced → Database → rekordbox xml** and choose this file. The playlist then appears under **rekordbox xml** in the sidebar: right-click it and choose **Import Playlist**. |

All imports on the same day go into the same `djsync-downloads-<date>`
playlist. A second import adds to it rather than creating a new one, and a
track is never added twice, so retrying is always safe.

If rekordbox was open during an import, djsync saves the playlist as XML
and the **Imported** section says **"Not in rekordbox yet"**. Quit rekordbox
and click **Retry: add to rekordbox** (or run `djsync import --retry`). It adds
the files already in your DJ folder to rekordbox under the same playlist name.

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
watch_dir = "~/Downloads"    # where store purchases land (moved on import)
extra_dirs = ["~/Music"]     # also look here, recent files only (copied on import)
recent_days = 90             # how far back to look in extra_dirs
library_dir = "~/Music/DJ"   # where imported tracks are moved
move_files = true            # false = copy instead of move

[match]
threshold = 88               # 0-100; raise it if wrong tracks match, lower it if owned tracks still show up
file_threshold = 75          # 0-100; lower it if downloaded files aren't found, raise it if the wrong file is picked

[agent]
backend = "auto"             # "claude-code" (your Claude plan), "api" (API key), or auto
claude_code_model = "sonnet" # Claude Code only: "sonnet", "haiku", "opus", or "" for its default
enabled = true               # the research agent
auto = true                  # research tracks as soon as you approve them
model = "claude-opus-5-5"     # API only
effort = "medium"            # API only: low | medium | high

[stores]
enabled = ["beatport", "bandcamp", "deezer"]   # also: "itunes", "amazon"
```

State such as pending, approved and skipped tracks is stored in
`~/.djsync/state.json`.

## Development

```bash
pip install -e '.[dev]'
pytest
```
