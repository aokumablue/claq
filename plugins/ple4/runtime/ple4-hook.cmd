@echo off
rem ple4 hook launcher -- Windows side.
rem
rem hooks.json names the extension-less sibling "ple4-hook"; cmd.exe appends
rem PATHEXT to an explicitly pathed command, so this .cmd is what actually runs
rem on Windows while POSIX hosts exec the sh script of the same base name.
rem
rem Resolution order: %PLE4_PYTHON% -> py -3 -> python -> python3.
rem Candidates found under ...\WindowsApps\ are skipped: those are Microsoft
rem Store "App execution alias" stubs that own the names python.exe/python3.exe
rem without shipping an interpreter. Invoking one prints
rem "Python was not found" and exits 9009, which is exactly how every ple4 hook
rem failed on Windows (release-verify 2026-09-03). The `py` launcher is tried
rem first because it is installed to %WINDIR% by the official installer, is
rem never shadowed by a Store alias, and selects the newest Python 3.
rem
rem When nothing usable is found this exits 0 after writing a diagnostic to
rem stderr, matching launcher.py's documented fail-open policy (CLAUDE.md
rem "ランタイム前提"): a fail-closed exit would deny the Bash/Edit tool calls
rem needed to repair PATH and leave the session unrecoverable.

setlocal EnableExtensions DisableDelayedExpansion
set "PLE4_ROOT=%~dp0.."
set "PLE4_LAUNCHER=%PLE4_ROOT%\src\ple4\launcher.py"

if defined PLE4_PYTHON (
  "%PLE4_PYTHON%" "%PLE4_LAUNCHER%" %*
  exit /b %ERRORLEVEL%
)

set "PLE4_PY="
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PLE4_PY set "PLE4_PY=%%P"
if defined PLE4_PY (
  "%PLE4_PY%" -3 "%PLE4_LAUNCHER%" %*
  exit /b %ERRORLEVEL%
)

call :ple4_pick python
if defined PLE4_PY goto :ple4_exec
call :ple4_pick python3
if defined PLE4_PY goto :ple4_exec

>&2 echo ERROR: ple4 found no usable Python 3.12+ (tried PLE4_PYTHON, py -3, python, python3; Microsoft Store alias stubs are ignored). Hooks are disabled until one is on PATH; set PLE4_PYTHON to an absolute interpreter path to recover.
>&2 echo {"ple4ProtectionDisabled": true, "reason": "python_not_found", "candidates": ["PLE4_PYTHON", "py -3", "python", "python3"], "requiredVersion": "3.12+"}
exit /b 0

:ple4_exec
"%PLE4_PY%" "%PLE4_LAUNCHER%" %*
exit /b %ERRORLEVEL%

:ple4_pick
for /f "delims=" %%P in ('where %1 2^>nul') do (
  if not defined PLE4_PY (
    echo.%%P| findstr /i /c:"\WindowsApps\" >nul || set "PLE4_PY=%%P"
  )
)
exit /b 0
