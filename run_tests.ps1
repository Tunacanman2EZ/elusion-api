# run_tests.ps1 - run every Flask-side suite in this folder.
#
#     .\run_tests.ps1
#     .\run_tests.ps1 -Only security,healing      # substring match on the name
#     .\run_tests.ps1 -ShowOutput                 # print passing suites too
#     .\run_tests.ps1 -Python "C:\path\to\python.exe"
#
# Exits 0 only if every suite exited 0, so it can gate a commit. The full
# transcript is written to test_results.txt beside this script, which is what
# to paste when something fails.
#
# The Godot half has its own run_tests.ps1 in the Elusion_RPG root, same name
# and same command on purpose. Two repos, one thing to remember.
#
#
# WHY THIS EXISTS, WHICH IS NOT "TYPING IS TEDIOUS"
# ------------------------------------------------
# There are TWELVE suites in this folder. The five that get run by hand are the
# five that were written first - test_api, test_economy, test_security,
# test_throttle, test_gathering - and the other seven, including the three that
# close E-9 and E-13, have been green-by-assumption. A suite nobody runs is a
# suite that is not protecting anything, and it looks exactly like one that is.
#
# So the list is DISCOVERED, not written down. Get-ChildItem test_*.py means a
# new suite is in the run the moment the file exists, and cannot be forgotten
# by whoever adds it. A hardcoded list is a list that goes stale in silence,
# which is this project's signature failure and has now cost four security
# controls (E-13), a shop button, a stat, and three invisible prisms.
#
#
# ONE PROCESS PER SUITE, AND THAT IS NOT A STYLE CHOICE
# ----------------------------------------------------
# Every suite sets ELUSION_DB and ELUSION_OWNER BEFORE importing app.py,
# because app.py reads DB_PATH and OWNER_USERNAME at import time. A module
# imported once stays imported, so a second suite in the same interpreter would
# inherit the first one's database and owner and test nothing it meant to.
#
# ELUSION_GAMEDATA is worse. Most suites point it at a fixture they build
# themselves; test_catalogue.py deliberately POPS it so that it reads the real
# shipped gamedata.json. Run in one process and test_catalogue would validate
# whichever fixture happened to run last - which is precisely the hole it was
# written to close. Separate processes is the only arrangement where that file
# means what it says.
#
#
# THE DATABASE IS SAFE, AND HERE IS WHY RATHER THAN AN ASSURANCE
# --------------------------------------------------------------
# app.py line 31: DB_PATH = os.environ.get("ELUSION_DB", <this folder>\elusion.db)
# Every suite that touches a database sets ELUSION_DB to a temp file first, and
# _load_dotenv() only fills in keys NOT already in the environment, so .env
# cannot drag a run back onto the live file. elusion.db is never opened by a
# test. That is worth knowing rather than trusting, because the one time it is
# wrong it is wrong about real accounts and password hashes.
#
#
# ASCII ONLY. Windows PowerShell 5.1 reads a BOM-less .ps1 as CP1252, where the
# last byte of a UTF-8 em dash decodes to a right double quote and silently ends
# whatever string it is sitting in. Same rule as the Godot side. See CLAUDE.md.

[CmdletBinding()]
param(
    [string]$Python,
    [string[]]$Only,
    [switch]$ShowOutput
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path (Join-Path $root "app.py") -PathType Leaf)) {
    throw "No app.py next to this script. Keep run_tests.ps1 in the api folder."
}

# --- find an interpreter -----------------------------------------------------
#
# The venv one is the only one with Flask installed. Falling back to a bare
# `python` is allowed but SAYS SO LOUDLY, because a system interpreter produces
# ModuleNotFoundError: flask on every suite at once, which reads like the server
# is broken rather than like the wrong python was used.
if (-not $Python) {
    $venv = Join-Path $root "venv\Scripts\python.exe"
    if (Test-Path $venv -PathType Leaf) {
        $Python = $venv
    } else {
        $onPath = Get-Command python -ErrorAction SilentlyContinue
        if ($onPath) {
            $Python = $onPath.Source
            Write-Host "No venv found. Using $Python" -ForegroundColor Yellow
            Write-Host "  If every suite fails on 'No module named flask', that is why." -ForegroundColor DarkGray
            Write-Host "  Rebuild it:  python -m venv venv; venv\Scripts\pip install -r requirements.txt" -ForegroundColor DarkGray
        }
    }
}

if (-not $Python -or -not (Test-Path $Python -PathType Leaf)) {
    Write-Host "No Python interpreter found." -ForegroundColor Red
    Write-Host "Expected venv\Scripts\python.exe, or python on PATH."
    Write-Host "Point at one directly:  .\run_tests.ps1 -Python 'C:\path\to\python.exe'"
    exit 1
}

# --- collect the suites ------------------------------------------------------
$suites = Get-ChildItem -Path $root -Filter "test_*.py" -File | Sort-Object Name

if ($Only) {
    $wanted = @()
    foreach ($pattern in $Only) {
        $hit = $suites | Where-Object { $_.Name -like "*$pattern*" }
        if (-not $hit) {
            # A filter that matches nothing must not quietly run nothing.
            Write-Host "No suite matches '$pattern'." -ForegroundColor Red
            Write-Host "Available: $(($suites | ForEach-Object { $_.BaseName -replace '^test_','' }) -join ', ')"
            exit 1
        }
        $wanted += $hit
    }
    $suites = $wanted | Sort-Object Name -Unique
}

if (-not $suites) {
    Write-Host "No test_*.py files found in $root" -ForegroundColor Red
    exit 1
}

Write-Host "Python: $Python" -ForegroundColor DarkGray
Write-Host "Suites: $($suites.Count)" -ForegroundColor DarkGray
Write-Host ""

# A failure detail can echo item names and other catalogue text. Pinning stdout
# to UTF-8 means an unusual character prints as itself instead of killing the
# suite with a UnicodeEncodeError that looks like a test failure.
$env:PYTHONIOENCODING = "utf-8"

$transcript = @()
$results = @()
$grandPassed = 0
$grandFailed = 0
$parsedAll = $true

# STOP HAS TO COME OFF BEFORE THE LOOP. With ErrorActionPreference = Stop, a
# native command that writes ANYTHING to stderr under 2>&1 raises
# NativeCommandError and aborts the whole run. A python suite prints warnings to
# stderr routinely, so leaving it on would turn a noisy pass into an abort on
# suite three and never reach the other nine. The exit code is what this script
# judges by; stderr is just text.
$ErrorActionPreference = "Continue"

Push-Location $root
try {
    foreach ($suite in $suites) {
        $label = $suite.Name
        Write-Host ("  running {0,-22}" -f $label) -NoNewline

        # .ToString() ON EVERY LINE, and it is not cosmetic. Under 2>&1 the
        # first stderr line from a native command arrives as an ErrorRecord, not
        # a string, and Out-String renders an ErrorRecord with the full
        # NormalView decoration - "At run_tests.ps1:155 char:19", the offending
        # source line, CategoryInfo, FullyQualifiedErrorId. Dropped into a test
        # transcript that reads as THIS SCRIPT having thrown, three lines above
        # the actual Python traceback, and it sends you to the wrong file.
        # Flask logs warnings to stderr as a matter of course, so a passing
        # suite can produce it too.
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $output = & $Python $suite.FullName 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
        $sw.Stop()

        $text = ($output | Out-String)

        # The exit code is the SOURCE OF TRUTH. The numbers below are a
        # convenience total scraped from the suite's own summary line, and when
        # the scrape misses it says so rather than reporting a confident zero -
        # a crashed suite prints no summary at all, and calling that "0 failed"
        # would turn a traceback into a green run.
        $summary = [regex]::Matches($text, '(\d+)\s+passed,\s+(\d+)\s+failed')
        if ($summary.Count -gt 0) {
            $last = $summary[$summary.Count - 1]
            $p = [int]$last.Groups[1].Value
            $f = [int]$last.Groups[2].Value
            $grandPassed += $p
            $grandFailed += $f
            $counts = "{0} passed, {1} failed" -f $p, $f
        } else {
            $parsedAll = $false
            $counts = "no summary line - suite did not reach the end"
        }

        $secs = "{0:N1}s" -f $sw.Elapsed.TotalSeconds
        if ($code -eq 0) {
            Write-Host ("  PASS  {0,-32} {1}" -f $counts, $secs) -ForegroundColor Green
        } else {
            Write-Host ("  FAIL  {0,-32} {1}" -f $counts, $secs) -ForegroundColor Red
        }

        $results += [pscustomobject]@{ Name = $label; Code = $code; Counts = $counts }

        $transcript += ("=" * 70)
        $transcript += "$label   exit $code   $counts   $secs"
        $transcript += ("=" * 70)
        $transcript += $text.TrimEnd()
        $transcript += ""

        # Passing output is captured to the transcript but not printed, because
        # twelve suites of per-check lines buries the one thing worth reading.
        if ($code -ne 0 -or $ShowOutput) {
            Write-Host ""
            Write-Host $text.TrimEnd()
            Write-Host ""
        }
    }
} finally {
    Pop-Location
}

# --- the summary -------------------------------------------------------------
$broken = @($results | Where-Object { $_.Code -ne 0 })
$noun = if ($results.Count -eq 1) { "suite" } else { "suites" }

Write-Host ""
Write-Host ("=" * 60)
if ($parsedAll) {
    Write-Host ("  {0} {1}, {2} checks passed, {3} failed" -f $results.Count, $noun, $grandPassed, $grandFailed)
} else {
    Write-Host ("  {0} {1}, {2} checks passed, {3} failed  (at least one suite printed no summary)" -f $results.Count, $noun, $grandPassed, $grandFailed)
}

if ($broken) {
    Write-Host ""
    Write-Host "  suites that failed:" -ForegroundColor Red
    foreach ($b in $broken) {
        Write-Host ("    - {0}  ({1})" -f $b.Name, $b.Counts) -ForegroundColor Red
    }
}
Write-Host ("=" * 60)

$resultsPath = Join-Path $root "test_results.txt"
$header = @(
    "Elusion API test run - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "Python: $Python",
    "$($results.Count) $noun, $grandPassed checks passed, $grandFailed failed",
    ""
)
($header + $transcript) | Out-File -FilePath $resultsPath -Encoding UTF8
Write-Host "Transcript: $resultsPath" -ForegroundColor DarkGray

if ($broken) { exit 1 } else { exit 0 }
