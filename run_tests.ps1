# run_tests.ps1 - run every Flask-side suite in this folder.
#
#     .\run_tests.ps1
#     .\run_tests.ps1 -Only security,healing      # substring match on the name
#     .\run_tests.ps1 -ShowOutput                 # print passing suites too
#     .\run_tests.ps1 -Python "C:\path\to\python.exe"
#     .\run_tests.ps1 -Jobs 1                     # one at a time, as it always was
#     .\run_tests.ps1 -Jobs 4                     # four at once (default: the CPUs, at most 6)
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
# The suites that get run by hand are the five that were written first -
# test_api, test_economy, test_security, test_throttle, test_gathering - and
# the rest, including the ones that close E-9 and E-13, have been
# green-by-assumption. A suite nobody runs is a suite that is not protecting
# anything, and it looks exactly like one that is.
#
# NO COUNT IS WRITTEN IN THIS HEADER, and that is the same decision CLAUDE.md
# makes about its own check totals. These sentences used to open with 'There
# are TWELVE suites in this folder' and go on to say 'the other seven'. There
# are twenty-six as this is written, so both numbers were wrong, and any number
# put here would be wrong again by the next suite. A number in a comment cannot
# fail. The script counts; the comment does not.
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
# app.py CONFIG: DB_PATH = os.environ.get("ELUSION_DB", <this folder>\elusion.db)
# Every suite that touches a database sets ELUSION_DB to a temp file first, and
# envfile.load() only fills in keys NOT already in the environment, so .env
# cannot drag a run back onto the live file - and since ELUSION_DB in a .env
# now really is read (it was ignored until envfile.py), that sentence carries
# weight. test_deploy.py checks a real variable beats the file. elusion.db is
# never opened by a test. That is worth knowing rather than trusting, because
# the one time it is wrong it is wrong about real accounts and password hashes.
#
#
# SIDE BY SIDE, BECAUSE NOTHING IS SHARED ANY MORE
# ------------------------------------------------
# Every suite keeps its databases in a folder of its own (testdir.py: one made
# per run and removed after it) and binds its servers to port 0, so two suites
# - or two copies of one - running at once cannot touch each other. Shown, not
# supposed: two copies of test_chardelete.py at once used to die on "That
# username is already taken"; since testdir.py both pass (UPGRADES.md, item 2).
# So -Jobs runs several at once, and that is the default.
#
# WHAT A RUN MEANS DOES NOT CHANGE. Each suite is still its own python process
# with the same environment, started the same way and read the same way (one
# function below reads every suite's output, whichever mode ran it); the
# transcript still holds each suite's output whole, in name order; the console
# still prints one line per suite in name order - each as soon as it and every
# suite before it have finished; and the exit code is still 0 only if every
# suite exited 0. -Jobs 1 is the old loop itself, not a pool of one.
#
# START-JOB, NOT START-PROCESS, and not ForEach-Object -Parallel (PowerShell 7
# only - and this has to run under the Windows PowerShell 5.1 a PC opens by
# default, as well as the pwsh CI uses). A job runs the very pipeline the
# sequential loop runs - `& $Python suite 2>&1`, every line .ToString()'d - so
# stderr stays interleaved with stdout exactly as before. Start-Process could
# only hand the two streams back as two separate files, and a traceback would
# no longer sit where it happened. The price is one PowerShell process per job,
# a second or so each, paid side by side.
#
# STOPPED WITH Ctrl+C, the jobs still running are stopped and removed on the
# way out. A python suite a stopped job had started may finish on its own; it
# writes only to its own temp folder.
#
#
# ASCII ONLY. Windows PowerShell 5.1 reads a BOM-less .ps1 as CP1252, where the
# last byte of a UTF-8 em dash decodes to a right double quote and silently ends
# whatever string it is sitting in. Same rule as the Godot side. See CLAUDE.md.

[CmdletBinding()]
param(
    [string]$Python,
    [string[]]$Only,
    [switch]$ShowOutput,
    # How many suites run at once. 0 (the default) is one per CPU, at most 6:
    # past that the suites queue on the disk and the database locks rather than
    # finish sooner. 1 is the sequential run, unchanged.
    [int]$Jobs = 0
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

$suites = @($suites)
if ($Jobs -lt 1) {
    $Jobs = [Environment]::ProcessorCount
    if ($Jobs -gt 6) { $Jobs = 6 }
    if ($Jobs -lt 1) { $Jobs = 1 }
}
if ($Jobs -gt $suites.Count) { $Jobs = $suites.Count }

Write-Host "Python: $Python" -ForegroundColor DarkGray
Write-Host "Suites: $($suites.Count)" -ForegroundColor DarkGray
if ($Jobs -gt 1) {
    Write-Host "At once: $Jobs  (a line appears when its suite and every one above it have finished)" -ForegroundColor DarkGray
}
Write-Host ""

# A failure detail can echo item names and other catalogue text. Pinning stdout
# to UTF-8 means an unusual character prints as itself instead of killing the
# suite with a UnicodeEncodeError that looks like a test failure.
$env:PYTHONIOENCODING = "utf-8"

$transcript = @()
$results = @()
$wall = [System.Diagnostics.Stopwatch]::StartNew()
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

# ONE READER FOR EVERY SUITE, whichever way it was run: the counts, the
# PASS/FAIL line, the results and the transcript are made here and nowhere
# else, so the two modes cannot drift apart in what they report.
function Complete-Suite {
    # $code NOT typed [int]: a suite whose python never ran leaves no exit
    # code ($null), and [int] would turn that into 0 - a pass. Untyped, $null
    # is not 0, and the suite is counted as failed, as it always was.
    param([string]$label, $output, $code, [double]$seconds)

    $text = ($output | Out-String)

    # The exit code is the SOURCE OF TRUTH. The numbers below are a
    # convenience total scraped from the suite's own summary line, and when
    # the scrape misses it says so rather than reporting a confident zero -
    # a crashed suite prints no summary at all, and calling that "0 failed"
    # would turn a traceback into a green run.
    # THREE SHAPES, BECAUSE THE SUITES PRINT THREE SHAPES. The original
    # pattern matched only "N passed, M failed", and five suites do not
    # write that: broadcast, maintenance, recovery and teleport end with
    # "passed: N   failed: M", and killwatch with "PASSED N  FAILED M".
    #
    # The cost was not cosmetic. Those five were reported as "no summary
    # line - suite did not reach the end" on every clean run, the footer
    # permanently carried the "(at least one suite printed no summary)"
    # caveat, and 201 passing checks were missing from the total - so the
    # number at the bottom was wrong, and the one warning that should mean
    # "a suite crashed" meant nothing at all because it was always on.
    #
    # ORDERED MOST-SPECIFIC FIRST and the LAST match in the text wins, both
    # for the same reason as before: a suite that prints a per-section
    # tally before its final one must be read from the end.
    $patterns = @(
        '(\d+)\s+passed,\s+(\d+)\s+failed',
        'passed:\s*(\d+)\s+failed:\s*(\d+)',
        'PASSED\s+(\d+)\s+FAILED\s+(\d+)'
    )
    $p = $null; $f = $null
    foreach ($pattern in $patterns) {
        $summary = [regex]::Matches($text, $pattern)
        if ($summary.Count -gt 0) {
            $last = $summary[$summary.Count - 1]
            $p = [int]$last.Groups[1].Value
            $f = [int]$last.Groups[2].Value
            break
        }
    }
    if ($null -ne $p) {
        $script:grandPassed += $p
        $script:grandFailed += $f
        $counts = "{0} passed, {1} failed" -f $p, $f
    } else {
        # STILL SAYS SO RATHER THAN GUESSING ZERO. A suite that crashes
        # prints no summary at all, and calling that "0 failed" would turn
        # a traceback into a green run. That reasoning was right; it was
        # only firing on the wrong suites.
        $script:parsedAll = $false
        $counts = "no summary line - suite did not reach the end"
    }

    $secs = "{0:N1}s" -f $seconds
    if ($code -eq 0) {
        Write-Host ("  PASS  {0,-32} {1}" -f $counts, $secs) -ForegroundColor Green
    } else {
        Write-Host ("  FAIL  {0,-32} {1}" -f $counts, $secs) -ForegroundColor Red
    }

    $script:results += [pscustomobject]@{ Name = $label; Code = $code; Counts = $counts }

    $script:transcript += ("=" * 70)
    $script:transcript += "$label   exit $code   $counts   $secs"
    $script:transcript += ("=" * 70)
    $script:transcript += $text.TrimEnd()
    $script:transcript += ""

    # Passing output is captured to the transcript but not printed, because
    # twelve suites of per-check lines buries the one thing worth reading.
    if ($code -ne 0 -or $ShowOutput) {
        Write-Host ""
        Write-Host $text.TrimEnd()
        Write-Host ""
    }
}

Push-Location $root
try {
    if ($Jobs -le 1) {
        # THE SEQUENTIAL RUN, as it has always been.
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
            #
            # AN INTERPRETER THAT CANNOT RUN AT ALL - a broken venv, a file
            # that is not a program - throws here rather than exiting. Before
            # 19 Oct that throw ended the loop, the summary read "0 suites, 0
            # failed" and the run EXITED 0: a commit gate that passed with
            # nothing run. Caught now, said in the suite's own output, and the
            # suite counted as failed ($null is not an exit code of 0).
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $code = $null
            try {
                $output = & $Python $suite.FullName 2>&1 | ForEach-Object { $_.ToString() }
                $code = $LASTEXITCODE
            } catch {
                $output = @("run_tests.ps1: $Python could not be run: $($_.Exception.Message)")
            }
            $sw.Stop()

            Complete-Suite $label $output $code $sw.Elapsed.TotalSeconds
        }
    } else {
        # SIDE BY SIDE (see the header). The job runs the same pipeline as the
        # loop above, from this folder, with this environment - a job's process
        # inherits it, and PYTHONIOENCODING is set again inside to be sure -
        # and hands back the lines, the exit code and the suite's own time.
        $runSuite = {
            param($py, $file, $dir)
            Set-Location $dir
            $env:PYTHONIOENCODING = "utf-8"
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $exit = $null
            try {
                $lines = & $py $file 2>&1 | ForEach-Object { $_.ToString() }
                $exit = $LASTEXITCODE
            } catch {
                $lines = @("run_tests.ps1: $py could not be run: $($_.Exception.Message)")
            }
            $sw.Stop()
            [pscustomobject]@{ Lines = @($lines); Code = $exit; Seconds = $sw.Elapsed.TotalSeconds }
        }

        $running = @{}   # job id -> the suite's place in name order
        $done = @{}      # place -> what its job handed back
        $started = 0
        $shown = 0
        try {
            while ($shown -lt $suites.Count) {
                while ($running.Count -lt $Jobs -and $started -lt $suites.Count) {
                    $job = Start-Job -ScriptBlock $runSuite -ArgumentList $Python, $suites[$started].FullName, $root
                    $running[$job.Id] = $started
                    $started += 1
                }
                if ($running.Count -gt 0) {
                    $null = Wait-Job -Job @(Get-Job -Id @($running.Keys)) -Any
                }
                foreach ($job in @(Get-Job -Id @($running.Keys))) {
                    if ($job.State -eq "Running" -or $job.State -eq "NotStarted") { continue }
                    $place = $running[$job.Id]
                    $handed = $null
                    try {
                        $handed = @(Receive-Job -Job $job -ErrorAction SilentlyContinue) |
                            Where-Object { $_ -ne $null -and $_.PSObject.Properties["Code"] } |
                            Select-Object -Last 1
                    } catch {
                        $handed = $null
                    }
                    if ($null -eq $handed) {
                        # THE JOB ITSELF FAILED, before or around the suite - not
                        # a suite failing its checks. Said as such, and counted
                        # as a failure: a run that could not ask is not a pass.
                        $why = $job.ChildJobs[0].JobStateInfo.Reason
                        $handed = [pscustomobject]@{
                            Lines = @("run_tests.ps1: the job running this suite ended $($job.State): $why")
                            Code = 1
                            Seconds = 0.0
                        }
                    }
                    Remove-Job -Job $job -Force
                    $running.Remove($job.Id)
                    $done[$place] = $handed
                }
                # IN NAME ORDER: a suite is shown once it and every suite above
                # it have finished, so the console reads as the sequential run.
                while ($done.ContainsKey($shown)) {
                    $handed = $done[$shown]
                    $done.Remove($shown)
                    $lines = @()
                    if ($null -ne $handed.Lines) { $lines = @($handed.Lines | ForEach-Object { [string]$_ }) }
                    Write-Host ("  running {0,-22}" -f $suites[$shown].Name) -NoNewline
                    Complete-Suite $suites[$shown].Name $lines $handed.Code ([double]$handed.Seconds)
                    $shown += 1
                }
            }
        } finally {
            foreach ($id in @($running.Keys)) {
                Stop-Job -Id $id -ErrorAction SilentlyContinue
                Remove-Job -Id $id -Force -ErrorAction SilentlyContinue
            }
        }
    }
} finally {
    Pop-Location
}

# --- the summary -------------------------------------------------------------
$broken = @($results | Where-Object { $_.Code -ne 0 })
# EVERY SUITE FOUND MUST HAVE BEEN RUN. Whatever stopped a run part-way - an
# error this script did not foresee, a job lost - a run that did not reach
# every suite is not a pass, and says which it did not reach.
$reached = @($results | ForEach-Object { $_.Name })
$missed = @($suites | Where-Object { $reached -notcontains $_.Name } | ForEach-Object { $_.Name })
$noun = if ($results.Count -eq 1) { "suite" } else { "suites" }

Write-Host ""
$wall.Stop()
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
if ($missed) {
    Write-Host ""
    Write-Host "  suites that never ran:" -ForegroundColor Red
    foreach ($m in $missed) {
        Write-Host ("    - {0}" -f $m) -ForegroundColor Red
    }
}
Write-Host ("  {0:N0}s, {1} at a time" -f $wall.Elapsed.TotalSeconds, $Jobs) -ForegroundColor DarkGray
Write-Host ("=" * 60)

$resultsPath = Join-Path $root "test_results.txt"
$header = @(
    "Elusion API test run - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "Python: $Python",
    "$($results.Count) $noun, $grandPassed checks passed, $grandFailed failed",
    ("{0:N0}s, {1} at a time" -f $wall.Elapsed.TotalSeconds, $Jobs),
    $(if ($missed) { "NEVER RAN: " + ($missed -join ", ") } else { "" }),
    ""
)
($header + $transcript) | Out-File -FilePath $resultsPath -Encoding UTF8
Write-Host "Transcript: $resultsPath" -ForegroundColor DarkGray

if ($broken -or $missed) { exit 1 } else { exit 0 }
