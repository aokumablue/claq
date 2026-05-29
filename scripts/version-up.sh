#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_VERSION=""

usage() {
  cat <<'EOF'
Usage: bash scripts/version-up.sh --version X.Y.Z

Options:
  --version VERSION   New version to write to all version fields
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

if [[ -z "${TARGET_VERSION}" ]]; then
  fail "--version is required"
fi

if [[ ! "${TARGET_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  fail "invalid version format: ${TARGET_VERSION}"
fi

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required"
fi

python3 - "${REPO_ROOT}" "${TARGET_VERSION}" <<'PY'
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

paths = {
    "pyproject": repo_root / "plugins" / "deepblue" / "pyproject.toml",
    "plugin_json": repo_root / "plugins" / "deepblue" / ".claude-plugin" / "plugin.json",
    "marketplace": repo_root / ".claude-plugin" / "marketplace.json",
    "version_py": repo_root / "plugins" / "deepblue" / "src" / "deepblue" / "mem" / "__init__.py",
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

if len(re.findall(r'(?m)^version = "([^"]+)"$', pyproject_text)) != 1:
    fail(f"failed to locate exactly one version field in {paths['pyproject']}")

updated_pyproject_text, pyproject_count = re.subn(
    r'(?m)^version = "([^"]+)"$',
    f'version = "{target_version}"',
    pyproject_text,
    count=1,
)
if pyproject_count != 1:
    fail(f"failed to update {paths['pyproject']}")

plugin_data["version"] = target_version
if not isinstance(marketplace_plugins, list) or not marketplace_plugins:
    fail(f"plugins[0] not found in {paths['marketplace']}")
marketplace_first_plugin = marketplace_plugins[0]
if not isinstance(marketplace_first_plugin, dict):
    fail(f"plugins[0] is not an object in {paths['marketplace']}")
marketplace_first_plugin["version"] = target_version

if len(re.findall(r'(?m)^__version__ = "([^"]+)"$', version_py_text)) != 1:
    fail(f"failed to locate exactly one __version__ field in {paths['version_py']}")

updated_version_py_text, version_py_count = re.subn(
    r'(?m)^__version__ = "([^"]+)"$',
    f'__version__ = "{target_version}"',
    version_py_text,
    count=1,
)
if version_py_count != 1:
    fail(f"failed to update {paths['version_py']}")

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
