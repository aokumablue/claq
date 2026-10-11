#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYPROJECT="${REPO_ROOT}/plugins/claq/pyproject.toml"

fail() {
  printf '[bump-minor] Error: %s\n' "$1" >&2
  exit 1
}

current="$(sed -n 's/^version = "\([0-9]*\.[0-9]*\.[0-9]*\)"$/\1/p' "${PYPROJECT}" | head -n 1)"
[[ -n "${current}" ]] || fail "version not found in ${PYPROJECT}"

IFS=. read -r major minor patch <<< "${current}"
next="${major}.$((minor + 1)).0"

bash "${SCRIPT_DIR}/version-up.sh" --version "${next}"
