@echo off
REM ==========================================================================
REM  t24_run.cmd  -- launch the T24 multi-server runner from a cmd prompt.
REM  All the real logic lives in t24_run.sh (bash); this just finds Git Bash
REM  and runs it. plink.exe (PuTTY) must be on PATH for the SSH/password part.
REM
REM  Usage:  open cmd, cd to the folder that has your CSV, then run:
REM             t24_run.cmd
REM  (The CSV is looked for in your CURRENT directory, not the script folder.)
REM ==========================================================================
setlocal

set "SCRIPT=%~dp0t24_run.sh"
if not exist "%SCRIPT%" (
  echo ERROR: t24_run.sh not found next to this launcher ^(%SCRIPT%^).
  exit /b 1
)

REM --- locate Git Bash (bash.exe) -------------------------------------------
set "BASH="
for %%B in (
  "%ProgramFiles%\Git\bin\bash.exe"
  "%ProgramFiles%\Git\usr\bin\bash.exe"
  "%ProgramFiles(x86)%\Git\bin\bash.exe"
  "%LocalAppData%\Programs\Git\bin\bash.exe"
) do if not defined BASH if exist "%%~B" set "BASH=%%~B"

if not defined BASH (
  for /f "delims=" %%B in ('where bash 2^>nul') do if not defined BASH set "BASH=%%B"
)

if not defined BASH (
  echo ERROR: Could not find Git Bash ^(bash.exe^).
  echo        Install Git for Windows, or add bash.exe to PATH.
  exit /b 1
)

REM Run the script; it uses the CURRENT directory to find the CSV.
"%BASH%" "%SCRIPT%"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
