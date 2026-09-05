#Requires -Version 5.1
<#
.SYNOPSIS
    Sets up the bot and web dashboard in isolated Python 3.11 environments.

.DESCRIPTION
    Creates two virtual environments, installs the bot (which pulls PyLav) and the
    dashboard, then verifies the install. Does NOT install system prerequisites or
    configure PostgreSQL - see INSTALL.md steps 1-3 for those.

.PARAMETER BotEnv
    Path for the bot's virtual environment. Defaults to %USERPROFILE%\redenv.

.PARAMETER DashEnv
    Path for the dashboard's virtual environment. Defaults to %USERPROFILE%\dashenv.

.PARAMETER SkipDashboard
    Install only the bot.

.EXAMPLE
    .\setup-windows.ps1
.EXAMPLE
    .\setup-windows.ps1 -BotEnv D:\bots\redenv -SkipDashboard
#>
[CmdletBinding()]
param(
    [string]$BotEnv    = (Join-Path $env:USERPROFILE 'redenv'),
    [string]$DashEnv   = (Join-Path $env:USERPROFILE 'dashenv'),
    [switch]$SkipDashboard
)

$ErrorActionPreference = 'Stop'

$BotRepo  = 'git+https://github.com/1Example/DiscordBot@V3/develop'
$DashRepo = 'git+https://github.com/1Example/Red-Web-Dashboard@main'
$ExpectedCogAssets = 33

function Write-Step ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   ($m) { Write-Host "    $m"   -ForegroundColor Green }
function Write-Warn ($m) { Write-Host "    $m"   -ForegroundColor Yellow }

# --- Preflight -------------------------------------------------------------

Write-Step 'Checking prerequisites'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found on PATH. pip fetches two dependencies from git URLs and will fail without it. See INSTALL.md step 1."
}
Write-Ok "git: $((git --version) -replace 'git version ','')"

# py -3.11 is the reliable selector; a bare `python` may be any version.
try   { $pyVersion = (& py -3.11 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>&1) }
catch { throw "Python 3.11 not found. PyLav requires >=3.11,<3.12 exactly. Install with: choco upgrade python311 -y" }

if ($pyVersion -notmatch '^3\.11\.') {
    throw "Expected Python 3.11.x, got $pyVersion. Neither 3.10 nor 3.12 will work."
}
Write-Ok "python: $pyVersion"

$java = Get-Command java -ErrorAction SilentlyContinue
if (-not $java) {
    Write-Warn "java not found. PyLav's managed Lavalink node needs Java 17+. Music cogs will not start."
} else {
    # Version goes to stderr on most JDKs.
    $javaRaw = (& java -version 2>&1 | Out-String)
    if ($javaRaw -match 'version "(\d+)') {
        if ([int]$Matches[1] -lt 17) {
            Write-Warn "Java $($Matches[1]) found, but PyLav requires 17 or newer. Install: choco upgrade temurin17 -y"
        } else {
            Write-Ok "java: $($Matches[1])"
        }
    }
}

# --- Bot -------------------------------------------------------------------

Write-Step "Creating bot environment at $BotEnv"

if (Test-Path $BotEnv) {
    Write-Warn "Already exists, reusing it."
} else {
    # No --system-site-packages: PyLav's pins (anyio<4, asyncpg<0.30, watchfiles<0.23)
    # would otherwise downgrade packages in the global interpreter.
    & py -3.11 -m venv $BotEnv
    Write-Ok 'Created.'
}

$BotPython = Join-Path $BotEnv 'Scripts\python.exe'
if (-not (Test-Path $BotPython)) { throw "No interpreter at $BotPython - venv creation failed." }

Write-Step 'Installing the bot (several minutes; clones PyLav)'
& $BotPython -m pip install -U pip wheel
& $BotPython -m pip install $BotRepo
if ($LASTEXITCODE -ne 0) { throw 'Bot install failed. See the pip output above.' }

Write-Step 'Verifying'

$verify = @'
import pathlib, sys, redbot
cogs = pathlib.Path(redbot.__file__).parent / "cogs"
info = len(list(cogs.glob("*/info.json")))
assets = len(list(cogs.glob("levelup/generator/**/*")))
print(redbot.__version__)
print(info)
print(assets)
print(sys.prefix != sys.base_prefix)
'@

$out = & $BotPython -c $verify
if ($LASTEXITCODE -ne 0) { throw 'Import check failed - the package installed but will not load.' }

$version, $infoCount, $assetCount, $isolated = $out -split "`r?`n"

Write-Ok "version: $version"

if ($isolated -ne 'True') {
    Write-Warn 'Environment is NOT isolated - dependency changes may have hit your global Python.'
} else {
    Write-Ok 'environment: isolated'
}

if ([int]$infoCount -lt $ExpectedCogAssets) {
    Write-Warn "Only $infoCount of $ExpectedCogAssets info.json files present. Cogs missing theirs raise FileNotFoundError on load."
} else {
    Write-Ok "cog metadata: $infoCount/$ExpectedCogAssets"
}

if ([int]$assetCount -lt 1) {
    Write-Warn 'levelup assets missing - the levelup cog will fail at runtime.'
} else {
    Write-Ok "levelup assets: $assetCount"
}

# --- Dashboard -------------------------------------------------------------

if (-not $SkipDashboard) {
    Write-Step "Creating dashboard environment at $DashEnv"

    # Separate env on purpose: reddash never imports redbot, so sharing one only
    # risks a second round of dependency churn.
    if (Test-Path $DashEnv) { Write-Warn 'Already exists, reusing it.' }
    else { & py -3.11 -m venv $DashEnv; Write-Ok 'Created.' }

    $DashPython = Join-Path $DashEnv 'Scripts\python.exe'

    Write-Step 'Installing the dashboard'
    & $DashPython -m pip install -U pip wheel
    & $DashPython -m pip install $DashRepo
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard install failed.' }
    Write-Ok 'Installed.'
}

# --- Next steps ------------------------------------------------------------

Write-Host @"

Done.

Still required before the music cogs will start:
  - PostgreSQL running, with a database and the citext / pg_trgm / uuid-ossp extensions
  - pylav.yaml in $env:USERPROFILE holding those credentials
  See INSTALL.md steps 2 and 3.

Start the bot (RPC must be on, or the dashboard cannot connect):
  $BotEnv\Scripts\activate.bat
  redbot <instance name> --rpc

Then in Discord:
  [p]load dashboard
  [p]load audio

Start the dashboard in a second terminal:
  $DashEnv\Scripts\activate.bat
  reddash

  -> http://localhost:42356

Upgrading an existing bot? Run the [p]cog uninstall / [p]repo delete steps in
INSTALL.md section 6 first, or Downloader copies will shadow the bundled cogs.
"@ -ForegroundColor White
