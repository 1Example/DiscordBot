# Installation

Full setup for the bot, the music stack and the web dashboard.

## The three repositories

| Repository | What it is | How it gets installed |
|---|---|---|
| [`1Example/DiscordBot`](https://github.com/1Example/DiscordBot) | The bot. Red plus 33 bundled cogs | You install this |
| [`1Example/PyLav`](https://github.com/1Example/PyLav) | Music library | Pulled in automatically |
| [`1Example/Red-Web-Dashboard`](https://github.com/1Example/Red-Web-Dashboard) | Web control panel | You install this, separately |

PyLav is never installed by hand — `requirements/base.in` references it by commit and
pip fetches it. Do not delete or rename that repository; installs break without it.

The dashboard is a standalone Flask process that talks to the bot over JSON-RPC. It
does not import `redbot` at all, which is why it is not bundled and why it can live in
its own virtual environment.

---

## 1. Prerequisites

**Python must be exactly 3.11.** PyLav requires `>=3.11,<3.12` and the bot inherits
that. 3.12 will refuse to install; 3.10 fails later at import.

In an **administrator** PowerShell:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

choco upgrade git --params "/GitOnlyOnPath /WindowsTerminal" -y
choco upgrade visualstudio2022-workload-vctools -y
choco upgrade python311 -y
choco upgrade temurin17 -y
choco upgrade postgresql14 --params '/Password:CHANGEME' -y
```

Restart the machine afterwards so PATH updates apply.

Notes:

- **Git** is required at install time, not just for cloning — pip fetches two
  dependencies from git URLs.
- **MSVC build tools** cover the packages with no Windows wheel.
- **Java 17 or newer** is what PyLav's managed node checks for (`_has_java()` requires
  `>= (17, 0)`). Temurin 17 satisfies it. You do **not** need to install Lavalink —
  PyLav downloads and manages the node itself.
- **PostgreSQL** is mandatory for PyLav. Version 14 is what upstream targets.

## 2. Prepare the database

In `psql` as the `postgres` superuser:

```sql
CREATE USER pylav WITH PASSWORD 'your-password-here';
CREATE DATABASE pylav_db;
ALTER DATABASE pylav_db OWNER TO pylav;
```

Then connect to `pylav_db` and enable the extensions. PyLav tries to create these
itself, but will fail if its user lacks permission:

```sql
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

## 3. Configure PyLav

Create `pylav.yaml` in the home directory of the user that runs the bot
(`%userprofile%`). PyLav writes a default on first run if you skip this, but it will
point at the wrong database.

```yaml
PYLAV__POSTGRES_HOST: localhost
PYLAV__POSTGRES_PORT: 5432
PYLAV__POSTGRES_USER: pylav
PYLAV__POSTGRES_PASSWORD: your-password-here
PYLAV__POSTGRES_DB: pylav_db
PYLAV__JAVA_EXECUTABLE: java
```

Full reference: [`pylav.example.yaml`](https://github.com/1Example/PyLav/blob/develop/pylav.example.yaml).

## 4. Virtual environment

From a normal **Command Prompt** — not administrator, not PowerShell:

```
py -3.11 -m venv "%userprofile%\redenv"
"%userprofile%\redenv\Scripts\activate.bat"
```

Do not pass `--system-site-packages`. Without full isolation, installing the bot
downgrades packages in your global Python — PyLav pins `anyio<4`, `asyncpg<0.30`,
`watchfiles<0.23` and `importlib-metadata<8`, and those downgrades will escape the
environment.

Confirm isolation before continuing:

```
python -c "import sys; print(sys.prefix); print(sys.base_prefix)"
```

The two paths must differ. If they match, you are not in the venv.

## 5. Install the bot

```
python -m pip install -U pip wheel
python -m pip install "Red-DiscordBot @ git+https://github.com/1Example/DiscordBot@V3/develop"
```

This takes several minutes. It clones PyLav and compiles anything
lacking a Windows wheel. pip downgrading itself mid-install is expected — PyLav
declares `pip` and `wheel` as runtime dependencies.

Verify:

```
python -c "import redbot, pathlib; p=pathlib.Path(redbot.__file__).parent/'cogs'; print(redbot.__version__); print(len(list(p.glob('*/info.json'))), 'info.json')"
```

Expect `3.5.25.dev1+gfork1` and **33** `info.json`. A lower count means cog assets are
missing and those cogs will raise `FileNotFoundError` on load rather than anything
descriptive.

## 6. Migrating an existing instance

Skip this on a fresh install.

Back up first with `redbot-setup backup`.

Bundled cogs live on Red's **core path**, which `CogManager.paths()` ranks *below* the
Downloader install path. Any cog you previously installed through Downloader keeps
winning, so the fork appears to have done nothing. Remove them from inside Discord
before switching:

```
[p]cog uninstall audio plcontroller pleffects plnotifier plytradio levelup rss tickets vrtutils
[p]repo delete pylav-cogs
[p]repo delete 1example-cogs
```

Your data survives. `Config` keys on cog name plus identifier, and the bundled code is
byte-identical to the Downloader versions, so playlists, nodes and per-guild settings
carry over. Red's old built-in Audio is a different cog and its settings are orphaned,
not migrated.

## 7. Install the dashboard

A separate virtual environment is recommended — it shares no code with the bot and
avoids a second round of dependency churn.

```
py -3.11 -m venv "%userprofile%\dashenv"
"%userprofile%\dashenv\Scripts\activate.bat"
python -m pip install git+https://github.com/1Example/Red-Web-Dashboard@main
```

---

## Running

**Terminal 1 — the bot.** RPC must be enabled or the dashboard never connects:

```
"%userprofile%\redenv\Scripts\activate.bat"
redbot <instance name> --rpc
```

**In Discord**, load the cogs:

```
[p]load dashboard
[p]load audio
```

**Terminal 2 — the dashboard:**

```
"%userprofile%\dashenv\Scripts\activate.bat"
reddash
```

Open <http://localhost:42356>.

Defaults are `--host 0.0.0.0 --port 42356 --rpc-port 6133`. The RPC port must match the
bot's; if you pass `--rpc-port` to `redbot`, pass the same value to `reddash`.

`0.0.0.0` binds every interface. Firewall the port or put it behind a reverse proxy
with TLS before exposing it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `PackageNotFoundError: Py-Lav` | PyLav not installed — reinstall the bot rather than the cogs |
| Music cogs fail with `FileNotFoundError` | `info.json` missing from the build; check the step 5 count |
| Dashboard stuck connecting | Bot not started with `--rpc`, or RPC ports differ |
| `[p]load audio` fails | PostgreSQL unreachable, or `pylav.yaml` credentials wrong |
| Managed node won't start | `java --version` reports below 17, or Java is not on PATH |
| Old cog behaviour after upgrading | Downloader copies still installed — see step 6 |
| Install fails on a git dependency | Git missing from PATH, or a pinned commit no longer reachable |

## Updating

```
"%userprofile%\redenv\Scripts\activate.bat"
python -m pip install -U --force-reinstall "Red-DiscordBot @ git+https://github.com/1Example/DiscordBot@V3/develop"
```

Bundled cogs do not update through `[p]cog update` — they ship with the package.

PyLav is pinned to an exact commit in `requirements/base.in`, so rebuilds are
reproducible. Bump that pin deliberately when you want new upstream code; leaving
it on a moving branch means a reinstall months from now can pull different code
with no change on your side.
