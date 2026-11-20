@echo off
rem claq hook launcher -- Windows side.
rem
rem Claude Code runs hooks.json's `command` through Git Bash on Windows, so the
rem POSIX sibling "claq-hook" runs there, not this file. This .cmd is reached by
rem hosts that read the `powershell` field, which names it via the call operator:
rem & "...\runtime\claq-hook.cmd" <module> <args...>, and by a host that runs
rem `command` with cmd.exe (cmd.exe appends PATHEXT to the extension-less path).
rem The Windows CI job launches it through the `powershell` field.
rem
rem Resolution order: %CLAQ_PYTHON% -> py -3 -> python -> python3.
rem Candidates found under ...\WindowsApps\ are skipped: those are Microsoft
rem Store "App execution alias" stubs that own the names python.exe/python3.exe
rem without shipping an interpreter. Invoking one prints
rem "Python was not found" and exits 9009, which would make every claq hook
rem fail on Windows. The `py` launcher is tried
rem first because it is installed to %WINDIR% by the official installer and
rem selects the newest Python 3.
rem
rem EVERY candidate is probed for 3.12+ with the SAME arguments it would be
rem exec'd with before it is adopted. A host that has py.exe in %WINDIR% but no
rem registered Python 3 resolves `py -3` to a candidate that exits non-zero
rem without ever running launcher.py: protection would be off, no diagnostic
rem would be printed, and a perfectly good `python3` further down the list would
rem never be tried.
rem
rem `python3` is NOT exempt from the probe here, even though the POSIX wrapper
rem skips it for that one name. This is a deliberate asymmetry backed by a
rem capability difference, not an oversight: on POSIX an unusable `python3`
rem still reaches launcher.py, which prints claq's own
rem `unsupported_python_version` diagnostic, so skipping the probe costs a
rem diagnostic-shaped failure and saves an interpreter start per hook. On
rem Windows the realistic failure is a Store alias stub that exits 9009 before
rem any Python runs -- no launcher, no diagnostic, protection silently off. The
rem denylist below only knows the CURRENT install location of those stubs, so
rem exempting `python3` would mean any stub Microsoft relocates gets adopted
rem unprobed. The probe is the only check that does not depend on where the stub
rem happens to live.
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
rem    quote -- `/c:"\WindowsApps\"` would silently never match. The absolute-path
rem    tests below append a `|` sentinel inside the quotes for the same reason,
rem    so no comparison ever ends in `\"`.
rem 4. The lookup is `where "$PATH:<name>.exe"`, never a bare `where <name>`.
rem    Bare `where` searches the CURRENT DIRECTORY before PATH. Hooks run with
rem    the project root as cwd, so a python.exe/python.bat shim committed to a
rem    repository would be adopted ahead of the real interpreter and handed the
rem    hook's stdin (the tool-input JSON). The $PATH: form restricts the search
rem    to PATH, matching what `command -v` does on the POSIX side, and pinning
rem    .exe keeps the resolved kind of file the same on both platforms.
rem 5. Every %VAR% that can hold a filesystem path is expanded INSIDE double
rem    quotes, including inside `echo`. cmd re-parses the line after percent
rem    expansion, so a bare `echo ... %CLAQ_LAUNCHER% ...` turns an install path
rem    containing `&` into command execution -- and Windows account names may
rem    contain `&`, so `C:\Users\A&B\...` happens without an attacker.
rem 6. No diagnostic embeds a path in the JSON line. Quoting cannot make an
rem    arbitrary Windows path valid JSON (a `"` in the path still breaks it),
rem    and the POSIX side drops the path for the same reason, so both platforms
rem    keep paths on the human-readable line only.
rem
rem 7. PATH is CHECKED for empty entries, never rewritten. A zero-length PATH
rem    element (`;;`, or a leading/trailing `;`) means "current directory", and
rem    hooks run with the project root as cwd, so `where "$PATH:..."` would
rem    still reach a python.exe committed to the repository -- rule 4 keeps the
rem    search out of cwd only while PATH itself names no relative directory.
rem    Detecting the empty-entry shape needs no iteration: a `%PATH:;;=...%`
rem    substitution plus two substring tests, every expansion inside quotes.
rem    REWRITING PATH would need a per-entry loop and therefore delayed
rem    expansion, which stays disabled because `!` is legal in Windows paths.
rem    On detection the wrapper fails open with a diagnostic and requires an
rem    absolute CLAQ_PYTHON, mirroring what the POSIX side does when PATH holds
rem    no absolute entry at all.
rem
rem    An absolute CLAQ_PYTHON is honoured BEFORE this check on purpose. The
rem    check only guards the interpreter lookup, which an absolute CLAQ_PYTHON
rem    already bypasses. Child processes started by bare name (git) are found by
rem    CreateProcess, which searches the current directory before PATH whatever
rem    PATH holds, so refusing here would not close that route and would only
rem    take away the CLAQ_PYTHON recovery (docs/adr/hook-failure-direction.md).
rem
rem    KNOWN RESIDUAL: a NON-EMPTY relative entry (`PATH=foo;C:\Windows`) is
rem    still not caught. That case does need the per-entry loop, so it stays
rem    open; the POSIX side strips it because a POSIX shell can iterate PATH
rem    without the `!` hazard.
rem
rem    The check is written so a malformed test leaves CLAQ_PATH_RISK unset and
rem    resolution proceeds as if PATH were clean -- a broken check degrades to
rem    no check, never to "protection off on Windows".
rem
rem When nothing usable is found this exits 0 after writing a diagnostic to
rem stderr, matching launcher.py's documented fail-open policy (CLAUDE.md
rem "ランタイム前提"): a fail-closed exit would deny the Bash/Edit tool calls
rem needed to repair PATH and leave the session unrecoverable. The missing
rem launcher is checked FIRST, before any interpreter is chosen, because
rem `python <missing file>` exits 2 and PreToolUse reads exit 2 as deny -- an
rem incomplete install would otherwise turn fail-open into "deny every tool
rem call", the exact inversion the POSIX wrapper guards against at the same spot.
rem An unreadable-but-present launcher produces the same exit 2, so readability
rem is checked here too (`type` is an internal command, so it adds no lookup
rem surface); the POSIX side pairs `[ -f ]` with `[ -r ]` for that reason.

setlocal EnableExtensions DisableDelayedExpansion
set "CLAQ_ROOT=%~dp0.."
set "CLAQ_LAUNCHER=%CLAQ_ROOT%\src\claq\launcher.py"
set "CLAQ_PY="
set "CLAQ_PYARGS="

if not exist "%CLAQ_LAUNCHER%" goto :claq_no_launcher
type "%CLAQ_LAUNCHER%" >nul 2>&1
if errorlevel 1 goto :claq_no_launcher

if defined CLAQ_PYTHON goto :claq_override

rem Rule 7. Every expansion is inside double quotes, so a PATH holding `&` or
rem `^` is never re-parsed as a command. If any line here fails to parse,
rem CLAQ_PATH_RISK stays unset and resolution proceeds exactly as before.
set "CLAQ_PATH_RISK="
set "CLAQ_PATH_PROBE=%PATH:;;=;@;%"
if not "%CLAQ_PATH_PROBE%|"=="%PATH%|" set "CLAQ_PATH_RISK=1"
if "%PATH:~0,1%|"==";|" set "CLAQ_PATH_RISK=1"
if "%PATH:~-1%|"==";|" set "CLAQ_PATH_RISK=1"
if defined CLAQ_PATH_RISK goto :claq_path_unsafe

call :claq_pick py -3
if defined CLAQ_PY goto :claq_exec

call :claq_pick python
if defined CLAQ_PY goto :claq_exec

call :claq_pick python3
if defined CLAQ_PY goto :claq_exec

>&2 echo ERROR: claq found no usable Python 3.12+ (tried CLAQ_PYTHON, py -3, python, python3; Microsoft Store alias stubs are ignored). Hooks are disabled until one is on PATH; set CLAQ_PYTHON to an absolute interpreter path to recover.
>&2 echo {"claqProtectionDisabled": true, "reason": "python_not_found", "candidates": ["CLAQ_PYTHON", "py -3", "python", "python3"], "requiredVersion": "3.12+"}
exit /b 0

:claq_override
rem CLAQ_PYTHON accepts absolute paths only: C:\..., C:/..., and UNC \\... or
rem //.... cmd.exe resolves a bare or dot-relative name against the CURRENT
rem DIRECTORY first, so a relative override would hand the hook's stdin (the
rem tool-input JSON) to a binary committed to the repository being edited. The
rem POSIX wrapper rejects the same shapes -- there `CLAQ_PYTHON=./python3` was
rem reproduced as a working takeover, so this is a real hole on both sides, not
rem symmetry for its own sake. A rejected override does NOT fall through to the
rem PATH candidates: an explicit override silently becoming a different
rem interpreter breaks the only promise the variable makes.
if "%CLAQ_PYTHON:~1,2%|"==":\|" goto :claq_override_ok
if "%CLAQ_PYTHON:~1,2%|"==":/|" goto :claq_override_ok
if "%CLAQ_PYTHON:~0,2%|"=="\\|" goto :claq_override_ok
if "%CLAQ_PYTHON:~0,2%|"=="//|" goto :claq_override_ok
>&2 echo ERROR: CLAQ_PYTHON must be an absolute interpreter path; got "%CLAQ_PYTHON%". Hooks are disabled until it is fixed (a relative name resolves against the current directory, which hooks do not control).
>&2 echo {"claqProtectionDisabled": true, "reason": "claq_python_not_absolute"}
exit /b 0

:claq_override_ok
set "CLAQ_PY=%CLAQ_PYTHON%"
goto :claq_exec

:claq_path_unsafe
>&2 echo ERROR: PATH contains an empty entry (";;" or a leading/trailing ";"), which cmd.exe resolves as the CURRENT DIRECTORY. Hooks run with the project root as cwd, so an interpreter committed to the repository could be adopted and handed the hook's stdin. Hooks are disabled until PATH is fixed; set CLAQ_PYTHON to an absolute interpreter path to recover.
>&2 echo {"claqProtectionDisabled": true, "reason": "path_has_empty_entry"}
exit /b 0

:claq_no_launcher
>&2 echo ERROR: claq launcher is missing or unreadable at "%CLAQ_LAUNCHER%". Hooks are disabled until the plugin install is repaired (reinstall the plugin).
>&2 echo {"claqProtectionDisabled": true, "reason": "launcher_not_found"}
exit /b 0

:claq_exec
"%CLAQ_PY%" %CLAQ_PYARGS% "%CLAQ_LAUNCHER%" %*
exit /b

:claq_pick
rem %1 = executable base name, %2 = the argument it must be exec'd with (if any).
rem The probe runs with %2 so the interpreter proven here is the interpreter that
rem actually runs: probing bare `py` and then exec'ing `py -3` can select two
rem different runtimes. stdin is redirected from nul so the probe can never
rem consume the hook's tool-input JSON; the exec line above deliberately keeps
rem stdin, because the hook body reads it.
set "CLAQ_PY="
set "CLAQ_PYARGS=%~2"
for /f "delims=" %%P in ('where "$PATH:%1.exe" 2^>nul ^| findstr /i /v /c:"\WindowsApps"') do if not defined CLAQ_PY set "CLAQ_PY=%%P"
if not defined CLAQ_PY exit /b 0
"%CLAQ_PY%" %CLAQ_PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" <nul >nul 2>&1
if errorlevel 1 set "CLAQ_PY="
exit /b 0
