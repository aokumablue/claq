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
rem first because it is installed to %WINDIR% by the official installer and
rem selects the newest Python 3.
rem
rem Structural rules this file must keep (a regression test asserts them):
rem
rem 1. No interpreter is launched inside a parenthesized block, and the exit
rem    is a BARE `exit /b`. cmd expands %VAR% inside `if (...)` blocks at PARSE
rem    time, so `... & exit /b %ERRORLEVEL%` there returns the errorlevel from
rem    BEFORE the interpreter ran -- a deny (exit 2) would have been reported to
rem    the host as 0, silently allowing every blocked tool call on Windows.
rem    Bare `exit /b` leaves the current errorlevel untouched.
rem 2. The WindowsApps filter lives in the `where` pipeline, never in a
rem    per-iteration `echo ... | findstr` inside the for-block: after a for
rem    variable is expanded, the resulting line is re-parsed, so a real install
rem    path such as C:\Program Files (x86)\... would break the block.
rem 3. The findstr pattern must not end with a backslash immediately before the
rem    closing quote. findstr is parsed by the C runtime, where \" is an escaped
rem    quote -- `/c:"\WindowsApps\"` would silently never match.
rem
rem When nothing usable is found this exits 0 after writing a diagnostic to
rem stderr, matching launcher.py's documented fail-open policy (CLAUDE.md
rem "ランタイム前提"): a fail-closed exit would deny the Bash/Edit tool calls
rem needed to repair PATH and leave the session unrecoverable.

setlocal EnableExtensions DisableDelayedExpansion
set "PLE4_ROOT=%~dp0.."
set "PLE4_LAUNCHER=%PLE4_ROOT%\src\ple4\launcher.py"
set "PLE4_PY="
set "PLE4_PYARGS="

if defined PLE4_PYTHON set "PLE4_PY=%PLE4_PYTHON%"
if defined PLE4_PY goto :ple4_exec

call :ple4_pick py
if defined PLE4_PY set "PLE4_PYARGS=-3"
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python3
if defined PLE4_PY goto :ple4_exec

>&2 echo ERROR: ple4 found no usable Python 3.12+ (tried PLE4_PYTHON, py -3, python, python3; Microsoft Store alias stubs are ignored). Hooks are disabled until one is on PATH; set PLE4_PYTHON to an absolute interpreter path to recover.
>&2 echo {"ple4ProtectionDisabled": true, "reason": "python_not_found", "candidates": ["PLE4_PYTHON", "py -3", "python", "python3"], "requiredVersion": "3.12+"}
exit /b 0

:ple4_exec
"%PLE4_PY%" %PLE4_PYARGS% "%PLE4_LAUNCHER%" %*
exit /b

:ple4_pick
for /f "delims=" %%P in ('where %1 2^>nul ^| findstr /i /v /c:"\WindowsApps"') do if not defined PLE4_PY set "PLE4_PY=%%P"
exit /b 0
