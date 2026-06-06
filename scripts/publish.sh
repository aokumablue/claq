#!/bin/bash
# deepblue（ユーザー向けリポジトリ）に dev ファイルを除いて force push する
set -euo pipefail

PUBLISH_REMOTE="${DEEPBLUE_PUBLISH_REMOTE:-$HOME/dev/deepblue}"
TMPDIR="$(mktemp -d)"
trap "rm -rf $TMPDIR" EXIT

echo "Cloning deepblue-dev..."
git clone --local . "$TMPDIR/repo"
cd "$TMPDIR/repo"

echo "Filtering dev-only files..."
git filter-repo \
  --invert-paths \
  --path plugins/deepblue/tests/ \
  --path plugins/deepblue/onnx/ \
  --path plugins/deepblue/src/model_build/ \
  --path scripts/ \
  --path CLAUDE.md \
  --path conftest.py \
  --force

echo "Pushing to $PUBLISH_REMOTE..."
git remote add publish "$PUBLISH_REMOTE"
git push publish HEAD:main --force

echo "Done: published to $PUBLISH_REMOTE"
