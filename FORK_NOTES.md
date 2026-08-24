# Red-DiscordBot fork — bundled audio + cogs

Base: Red-DiscordBot `3.5.25.dev1`, tagged as `3.5.25.dev1+gfork1`.

33 cogs ship inside `redbot/cogs/` and load with `[p]load <name>` — no Downloader,
no repo add. Red does not keep a registry of core cogs; `CogManager._find_core_cog`
just imports `redbot.cogs.<name>`, so a vendored folder is indistinguishable from a
built-in one.

## What changed

| File | Change | Why |
|---|---|---|
| `redbot/cogs/audio/` | Red's Audio deleted, PyLav's `audio` put in its place | Both are named `audio`; they cannot coexist |
| `redbot/cogs/` | +32 cogs vendored | The point of the fork |
| `requirements/base.in`, `base.txt` | `Red-Lavalink` removed, cog deps added | Nothing outside the old `cogs/audio` imported lavalink |
| `MANIFEST.in` | asset rules added | See "packaging" below |
| `setup.py` | `python_requires` → `>=3.11,<3.12` | PyLav pins `>=3.11,<3.12` |
| `redbot/__init__.py` | `_VERSION` → `3.5.25.dev1+gfork1` | Tells forked instances apart in `[p]info` |

### Bundled cogs

**Music (PyLav):** `audio` `plconfig` `plcontroller` `pleffects` `pllocal` `pllyrics`
`plmanagednode` `plmigrator` `plnodes` `plnotifier` `plplaylists` `plradio` `plutils`
`plytradio`

**Utility / fun:** `botstatus` `commandlock` `dashboard` `embedutils` `emojisteal`
`extendedmodlog` `hunting` `infochannel` `insult` `levelup` `rolesyncer` `roletools`
`rss` `simplecasino` `splitorstealgame` `tickets` `timestamp` `vrtutils` `welcome`

`plcontroller` is PyLav-Cogs' version, not 1Example's — 1Example's copy predates
`dashboard_integration.py` and would not appear in the web dashboard.

### Deliberately excluded

- **`mafiagame`** — 38 MB of PNGs, roughly 80% of the source weight.
- **`aiuser`** — pulls `modal`, `tiktoken`, `fastembed`, `trafilatura`; a heavy and
  fast-moving dependency set to bind to your bot's release cycle.

Both still install fine through Downloader. To bundle one anyway, drop the folder into
`redbot/cogs/`, append its `info.json` requirements to `requirements/base.in` and
`base.txt`, and reinstall — the `MANIFEST.in` rules already cover their asset layouts.

## Packaging

Red's original `MANIFEST.in` only collected `redbot/**/data *` and `locales/*.po`.
That is enough for Red's own cogs and wrong for these. Two failure modes it caused:

1. **`info.json` was not shipped.** 24 of the bundled cogs call
   `get_end_user_data_statement(__file__)` in `__init__.py`, which reads `info.json`
   at import time. Missing file means the cog raises on load, not a cosmetic warning.
2. **Assets outside `data/` were dropped** — `levelup/generator/` and
   `levelup/dashboard/` (css, js, png, webp, ttf) and `embedutils/editor.html`. The
   package installs cleanly and then fails at runtime.

Verified against a real build: 33 `info.json`, 53 `levelup/generator` assets and 81
font/image files are present in the wheel. `include_package_data` defaults to true
here because `pyproject.toml` has a `[project]` table, so `MANIFEST.in` governs both
sdist and wheel.

## One pinned dependency you must not "clean up"

```
webcolors==1.3
```

`rss/color.py` builds `_RGB_NAME_MAP` from `webcolors.css3_hex_to_names` at module
scope. That attribute is gone by webcolors 1.12. Loosening the pin breaks `rss` on
import. This was tested, not assumed.

## Upgrading your existing instance

Back up first: `redbot-setup backup`.

**Step 1 is the one people skip.** `CogManager.paths()` orders the install path
*above* the core path, so Downloader-installed copies keep winning after you upgrade
and it looks like the fork did nothing.

```
[p]cog uninstall audio plcontroller pleffects plnotifier plytradio levelup rss tickets vrtutils ...
[p]repo delete pylav-cogs
[p]repo delete 1example-cogs
```

Then, in the venv:

```bash
python3.11 -V                       # must be 3.11.x
pip uninstall -y Red-DiscordBot Red-Lavalink
pip install .                       # or: pip install git+<your fork url>
```

PyLav additionally needs **PostgreSQL** and a **Lavalink node** running before
`[p]load audio` will work. Configure via the `PYLAV__POSTGRES_*` environment
variables.

### Your data survives

`Config` keys on cog name plus identifier. The vendored code is byte-identical to
what you are running now, so `audio` keeps `identifier=208903205982044161` and every
playlist, node and per-guild setting carries over untouched.

The exception is Red's built-in Audio: it is a different cog, so its old settings are
orphaned. Harmless, but they are not migrated into PyLav.

### After upgrading

Bundled cogs no longer update via `[p]cog update`. They update when you pull upstream
and reinstall.

## Tracking upstream Red

```bash
git remote add upstream https://github.com/Cog-Creators/Red-DiscordBot
git fetch upstream && git merge upstream/develop
```

Conflicts stay confined to five files — `requirements/base.in`, `requirements/base.txt`,
`MANIFEST.in`, `setup.py`, `redbot/__init__.py` — plus the deleted `redbot/cogs/audio`,
which will resurface on any upstream change to Red's Audio and needs re-deleting. Keep
your changes in one commit per file to make those merges mechanical.
