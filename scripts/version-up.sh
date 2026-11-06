#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_VERSION=""
BUMP_PART=""

usage() {
  cat <<'EOF'
Usage: bash scripts/version-up.sh (--version X.Y.Z | --bump major|minor|patch)

Options:
  --version VERSION   New version to write to all version fields
  --bump PART         Increment PART of the current version (lower parts reset to 0)
  --help              Show this help

The repository version and the plugin version are kept identical.
EOF
}

fail() {
  printf '[version-up] Error: %s\n' "$1" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || fail "--version requires a value"
      TARGET_VERSION="$2"
      shift 2
      ;;
    --bump)
      [[ $# -ge 2 ]] || fail "--bump requires a value"
      BUMP_PART="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      fail "unknown argument: $1"
      ;;
    *)
      fail "unexpected argument: $1"
      ;;
  esac
done

if [[ $# -gt 0 ]]; then
  fail "unexpected argument: $1"
fi

if [[ -n "${TARGET_VERSION}" && -n "${BUMP_PART}" ]]; then
  fail "--version and --bump are mutually exclusive"
fi

if [[ -z "${TARGET_VERSION}" && -z "${BUMP_PART}" ]]; then
  fail "--version or --bump is required"
fi

if [[ -n "${TARGET_VERSION}" && ! "${TARGET_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  fail "invalid version format: ${TARGET_VERSION}"
fi

case "${BUMP_PART}" in
  ""|major|minor|patch) ;;
  *) fail "invalid bump part: ${BUMP_PART}" ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required"
fi

python3 - "${REPO_ROOT}" "${TARGET_VERSION}" "${BUMP_PART}" <<'PY'
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"[version-up] Error: {message}")


repo_root = Path(sys.argv[1])
target_version = sys.argv[2]
bump_part = sys.argv[3]

paths = {
    "pyproject": repo_root / "plugins" / "claq" / "pyproject.toml",
    "plugin_json": repo_root / "plugins" / "claq" / ".claude-plugin" / "plugin.json",
    "marketplace": repo_root / ".claude-plugin" / "marketplace.json",
    "version_py": repo_root / "plugins" / "claq" / "src" / "claq" / "mem" / "__init__.py",
}

for path in paths.values():
    if not path.exists():
        fail(f"required file not found: {path}")
    if not path.is_file():
        fail(f"required file is not a regular file: {path}")

pyproject_text = paths["pyproject"].read_text(encoding="utf-8")
pyproject_data = tomllib.loads(pyproject_text)
pyproject_version = pyproject_data.get("project", {}).get("version")
if not isinstance(pyproject_version, str):
    fail(f"version field not found in {paths['pyproject']}")

plugin_data = json.loads(paths["plugin_json"].read_text(encoding="utf-8"))
marketplace_data = json.loads(paths["marketplace"].read_text(encoding="utf-8"))

version_py_text = paths["version_py"].read_text(encoding="utf-8")
version_py_match = re.search(r'(?m)^__version__ = "([^"]+)"$', version_py_text)
if not version_py_match:
    fail(f"__version__ field not found in {paths['version_py']}")

marketplace_plugins = marketplace_data.get("plugins")
marketplace_version = None
if isinstance(marketplace_plugins, list) and marketplace_plugins:
    first_plugin = marketplace_plugins[0]
    if isinstance(first_plugin, dict):
        marketplace_version = first_plugin.get("version")

current_versions = [
    pyproject_version,
    plugin_data.get("version"),
    marketplace_version,
    version_py_match.group(1),
]

if any(version is None for version in current_versions):
    fail("version field missing from one of the version sources")

if len(set(current_versions)) != 1:
    fail("version mismatch before update")

if bump_part:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pyproject_version):
        fail(f"current version is not X.Y.Z: {pyproject_version}")
    major, minor, patch = (int(part) for part in pyproject_version.split("."))
    target_version = {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[bump_part]

if len(re.findall(r'(?m)^version = "([^"]+)"$', pyproject_text)) != 1:
    fail(f"failed to locate exactly one version field in {paths['pyproject']}")

updated_pyproject_text = re.sub(
    r'(?m)^version = "([^"]+)"$',
    f'version = "{target_version}"',
    pyproject_text,
    count=1,
)

plugin_data["version"] = target_version
# marketplace_version が None でないことを上で確かめたので、plugins[0] は dict。
marketplace_plugins[0]["version"] = target_version

if len(re.findall(r'(?m)^__version__ = "([^"]+)"$', version_py_text)) != 1:
    fail(f"failed to locate exactly one __version__ field in {paths['version_py']}")

updated_version_py_text = re.sub(
    r'(?m)^__version__ = "([^"]+)"$',
    f'__version__ = "{target_version}"',
    version_py_text,
    count=1,
)

paths["pyproject"].write_text(updated_pyproject_text, encoding="utf-8")
paths["plugin_json"].write_text(json.dumps(plugin_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
paths["marketplace"].write_text(json.dumps(marketplace_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
paths["version_py"].write_text(updated_version_py_text, encoding="utf-8")

verify_pyproject = re.search(r'(?m)^version = "([^"]+)"$', paths["pyproject"].read_text(encoding="utf-8"))
verify_plugin = json.loads(paths["plugin_json"].read_text(encoding="utf-8")).get("version")
verify_marketplace_data = json.loads(paths["marketplace"].read_text(encoding="utf-8"))
verify_marketplace_plugins = verify_marketplace_data.get("plugins")
verify_marketplace = None
if isinstance(verify_marketplace_plugins, list) and verify_marketplace_plugins:
    verify_marketplace = verify_marketplace_plugins[0].get("version")
verify_version_py = re.search(r'(?m)^__version__ = "([^"]+)"$', paths["version_py"].read_text(encoding="utf-8"))

verifications = [
    verify_pyproject.group(1) if verify_pyproject else None,
    verify_plugin,
    verify_marketplace,
    verify_version_py.group(1) if verify_version_py else None,
]
if any(version != target_version for version in verifications):
    fail("post-update verification failed")

print(f"[version-up] Updated version to {target_version}")
PY
