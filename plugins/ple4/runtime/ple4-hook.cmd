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
rem still reaches launcher.py, which prints ple4's own
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
rem    expansion, so a bare `echo ... %PLE4_LAUNCHER% ...` turns an install path
rem    containing `&` into command execution -- and Windows account names may
rem    contain `&`, so `C:\Users\A&B\...` happens without an attacker.
rem 6. No diagnostic embeds a path in the JSON line. Quoting cannot make an
rem    arbitrary Windows path valid JSON (a `"` in the path still breaks it),
rem    and the POSIX side drops the path for the same reason, so both platforms
rem    keep paths on the human-readable line only.
rem
rem KNOWN RESIDUAL (POSIX-only fix): the POSIX wrapper strips PATH entries that
rem are not absolute paths, because a zero-length PATH element means "current
rem directory" and hooks run with the project root as cwd. cmd has the same
rem `;;` case, but filtering PATH here would require iterating its entries,
rem which needs delayed expansion -- deliberately disabled below because `!` is
rem legal in Windows paths and delayed expansion would corrupt them. Rule 4
rem already keeps the search out of cwd for the common case; closing the
rem empty-element case is left open rather than shipping an unverifiable
rem PATH-rewriting loop (cmd.exe cannot be executed from the darwin dev host).
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
set "PLE4_ROOT=%~dp0.."
set "PLE4_LAUNCHER=%PLE4_ROOT%\src\ple4\launcher.py"
set "PLE4_PY="
set "PLE4_PYARGS="

if not exist "%PLE4_LAUNCHER%" goto :ple4_no_launcher
type "%PLE4_LAUNCHER%" >nul 2>&1
if errorlevel 1 goto :ple4_no_launcher

if defined PLE4_PYTHON goto :ple4_override

call :ple4_pick py -3
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python
if defined PLE4_PY goto :ple4_exec

call :ple4_pick python3
if defined PLE4_PY goto :ple4_exec

>&2 echo ERROR: ple4 found no usable Python 3.12+ (tried PLE4_PYTHON, py -3, python, python3; Microsoft Store alias stubs are ignored). Hooks are disabled until one is on PATH; set PLE4_PYTHON to an absolute interpreter path to recover.
>&2 echo {"ple4ProtectionDisabled": true, "reason": "python_not_found", "candidates": ["PLE4_PYTHON", "py -3", "python", "python3"], "requiredVersion": "3.12+"}
exit /b 0

:ple4_override
rem PLE4_PYTHON accepts absolute paths only: C:\..., C:/..., and UNC \\... or
rem //.... cmd.exe resolves a bare or dot-relative name against the CURRENT
rem DIRECTORY first, so a relative override would hand the hook's stdin (the
rem tool-input JSON) to a binary committed to the repository being edited. The
rem POSIX wrapper rejects the same shapes -- there `PLE4_PYTHON=./python3` was
rem reproduced as a working takeover, so this is a real hole on both sides, not
rem symmetry for its own sake. A rejected override does NOT fall through to the
rem PATH candidates: an explicit override silently becoming a different
rem interpreter breaks the only promise the variable makes.
if "%PLE4_PYTHON:~1,2%|"==":\|" goto :ple4_override_ok
if "%PLE4_PYTHON:~1,2%|"==":/|" goto :ple4_override_ok
if "%PLE4_PYTHON:~0,2%|"=="\\|" goto :ple4_override_ok
if "%PLE4_PYTHON:~0,2%|"=="//|" goto :ple4_override_ok
>&2 echo ERROR: PLE4_PYTHON must be an absolute interpreter path; got "%PLE4_PYTHON%". Hooks are disabled until it is fixed (a relative name resolves against the current directory, which hooks do not control).
>&2 echo {"ple4ProtectionDisabled": true, "reason": "ple4_python_not_absolute"}
exit /b 0

:ple4_override_ok
set "PLE4_PY=%PLE4_PYTHON%"
goto :ple4_exec

:ple4_no_launcher
>&2 echo ERROR: ple4 launcher is missing or unreadable at "%PLE4_LAUNCHER%". Hooks are disabled until the plugin install is repaired (reinstall the plugin).
>&2 echo {"ple4ProtectionDisabled": true, "reason": "launcher_not_found"}
exit /b 0

:ple4_exec
"%PLE4_PY%" %PLE4_PYARGS% "%PLE4_LAUNCHER%" %*
exit /b

:ple4_pick
rem %1 = executable base name, %2 = the argument it must be exec'd with (if any).
rem The probe runs with %2 so the interpreter proven here is the interpreter that
rem actually runs: probing bare `py` and then exec'ing `py -3` can select two
rem different runtimes. stdin is redirected from nul so the probe can never
rem consume the hook's tool-input JSON; the exec line above deliberately keeps
rem stdin, because the hook body reads it.
set "PLE4_PY="
set "PLE4_PYARGS=%~2"
for /f "delims=" %%P in ('where "$PATH:%1.exe" 2^>nul ^| findstr /i /v /c:"\WindowsApps"') do if not defined PLE4_PY set "PLE4_PY=%%P"
if not defined PLE4_PY exit /b 0
"%PLE4_PY%" %PLE4_PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" <nul >nul 2>&1
if errorlevel 1 set "PLE4_PY="
exit /b 0
