@echo off
REM Renders the day's promoted Tier 2 set, unattended.
REM
REM Local, never a GitHub Action, and the reason is licensing rather than
REM convenience: a deck's price page is raw Tiingo OHLCV and its fundamentals
REM page is XBRL, so the file is vendor data. A public repo's Release assets are
REM downloadable, which would make publishing one redistribution -- and a static
REM dashboard opened from file:// cannot read the private R2 bucket either, so
REM there is nowhere in the cloud for these to go and be useful.
REM
REM Same pattern as run_finality.cmd: scheduled rather than run from a session,
REM because it has to land at a specific clock time and a session cannot be
REM relied on to still be alive.
REM
REM Timing. The nightly `prices` sweep starts 03:30 UTC and takes about 95
REM minutes, so the year partition is republished around 05:05 UTC -- 00:05
REM US-Central. This runs at 01:30 local, which leaves an hour and a half of
REM slack for a slow sweep. Running earlier would promote yesterday's session
REM and look completely normal doing it.
REM
REM Register it (one line, from a shell in the repo):
REM
REM   schtasks /create /tn "Market Radar decks" /tr "%CD%\scripts\run_decks.cmd" ^
REM     /sc daily /st 01:30 /f
REM
REM The machine has to be awake. `schtasks /change /tn "Market Radar decks"
REM /ri 60 /du 08:00` makes it retry hourly through the morning instead, which
REM is the right answer for a laptop that sleeps.
cd /d "C:\Users\oj\Market Radar"
echo ==== %DATE% %TIME% ==== >> ".decks\run.log"
".venv\Scripts\python.exe" -m marketradar.cli decks --promoted --out ".decks" --keep-runs 30 >> ".decks\run.log" 2>&1
echo exit=%ERRORLEVEL% >> ".decks\run.log"
