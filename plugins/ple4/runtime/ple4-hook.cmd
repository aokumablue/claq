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
rem Every candidate except the literal name `python3` is probed for 3.12+ with
rem the SAME arguments it would be exec'd with before it is adopted. A host that
rem has py.exe in %WINDIR% but no registered Python 3 resolves `py -3` to a
rem candidate that exits non-zero without ever running launcher.py: protection
rem would be off, no diagnostic would be printed, and a perfectly good `python3`
rem further down the list would never be tried. `python3` itself is taken
rem unprobed because the literal name already asserts Python 3 and launcher.py
rem reports an unsupported version on its own -- the same single rule the POSIX
rem wrapper follows, so neither platform trusts a name the other probes.
rem
rem Structural rules this file must keep (a regression test asserts them):
rem
rem 1. No interpreter is launched inside a parenthesized block, and the exit
rem    is a BARE `exit /b`. cmd expands %VAR% inside `if (...)` blocks at PARSE
rem    time, so `... & exit /b %ERRORLEVEL%` there returns the errorlevel from
rem    BEFORE the interpreter ran -- a deny (exit 2) would have been reported to
rem    the host as 0, silently allowing every blocked tool call on Windows.
rem    Bare `exit /b` leaves the current errorlevel untouched. The `if errorlevel
rem    N` BUILTIN is fine and is how the probe reads its result: it is compared
rem    at run time, unlike a %ERRORLEVEL% percent-expansion.
rem 2. The WindowsApps filter lives in the `where` pipeline, never in a
rem    per-iteration `echo ... | findstr` inside the for-block: after a for
rem    variable is expanded, the resulting line is re-parsed, so a real install
rem    path such as C:\Program Files (x86)\... would break the block.
rem 3. The findstr pattern must not end with a backslash immediately before the
rem    closing quote. findstr is parsed by the C runtime, where \" is an escaped
rem    quote -- `/c:"\WindowsApps\"` would silently never match.
rem 4. The lookup is `where "$PATH:<name>.exe"`, never a bare `where <name>`.
rem    Bare `where` searches the CURRENT DIRECTORY before PATH. Hooks run with
rem    the project root as cwd, so a python.exe/python.bat shim committed to a
rem    repository would be adopted ahead of the real interpreter and handed the
rem    hook's stdin (the tool-input JSON). The $PATH: form restricts the search
rem    to PATH, matching what `command -v` does on the POSIX side, and pinning
rem    .exe keeps the resolved kind of file the same on both platforms.
rem
rem When nothing usable is found this exits 0 after writing a diagnostic to
rem stderr, matching launcher.py's documented fail-open policy (CLAUDE.md
rem "ランタイム前提"): a fail-closed exit would deny the Bash/Edit tool calls
rem needed to repair PATH and leave the session unrecoverable. The missing
rem launcher is checked FIRST, before any interpreter is chosen, because
rem `python <missing file>` exits 2 and PreToolUse reads exit 2 as deny -- an
rem incomplete install would otherwise turn fail-open into "deny every tool
rem call", the exact inversion the POSIX wrapper guards against at the same spot.

setlocal EnableExtensions DisableDelayedExpansion
set "PLE4_ROOT=%~dp0.."
set "PLE4_LAUNCHER=%PLE4_ROOT%\src\ple4\launcher.py"
set "PLE4_PY="
set "PLE4_PYARGS="

if not exist "%PLE4_LAUNCHER%" goto :ple4_no_launcher

if defined PLE4_PYTHON set "PLE4_PY=%PLE4_PYTHON%"
if defined PLE4_PY goto :ple4_exec

call :ple4_pick py -3
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python3
if defined PLE4_PY goto :ple4_exec

>&2 echo ERROR: ple4 found no usable Python 3.12+ (tried PLE4_PYTHON, py -3, python, python3; Microsoft Store alias stubs are ignored). Hooks are disabled until one is on PATH; set PLE4_PYTHON to an absolute interpreter path to recover.
>&2 echo {"ple4ProtectionDisabled": true, "reason": "python_not_found", "candidates": ["PLE4_PYTHON", "py -3", "python", "python3"], "requiredVersion": "3.12+"}
exit /b 0

:ple4_no_launcher
rem Backslashes are doubled so the diagnostic stays parseable JSON: a raw
rem C:\Users\... path would make \U an invalid escape sequence.
set "PLE4_LAUNCHER_JSON=%PLE4_LAUNCHER:\=\\%"
>&2 echo ERROR: ple4 launcher is missing or unreadable at %PLE4_LAUNCHER%. Hooks are disabled until the plugin install is repaired (reinstall the plugin).
>&2 echo {"ple4ProtectionDisabled": true, "reason": "launcher_not_found", "launcher": "%PLE4_LAUNCHER_JSON%"}
exit /b 0

:ple4_exec
"%PLE4_PY%" %PLE4_PYARGS% "%PLE4_LAUNCHER%" %*
exit /b

:ple4_pick
rem %1 = executable base name, %2 = the argument it must be exec'd with (if any).
rem The probe runs with %2 so the interpreter proven here is the interpreter that
rem actually runs: probing bare `py` and then exec'ing `py -3` can select two
rem different runtimes.
set "PLE4_PY="
set "PLE4_PYARGS=%~2"
for /f "delims=" %%P in ('where "$PATH:%1.exe" 2^>nul ^| findstr /i /v /c:"\WindowsApps"') do if not defined PLE4_PY set "PLE4_PY=%%P"
if not defined PLE4_PY exit /b 0
if /i "%1"=="python3" exit /b 0
"%PLE4_PY%" %PLE4_PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 set "PLE4_PY="
exit /b 0
