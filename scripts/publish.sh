#!/bin/bash
# deepblue（ユーザー向けリポジトリ）に dev ファイルを除いたスナップショットを1コミットで push する
set -euo pipefail

PUBLISH_REMOTE="${DEEPBLUE_PUBLISH_REMOTE:-$HOME/dev/deepblue}"
COMMIT_MSG="${1:-}"
TMPDIR="$(mktemp -d)"
trap "rm -rf $TMPDIR" EXIT

echo "Cloning deepblue-dev..."
git clone --local --no-hardlinks . "$TMPDIR/repo"
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

echo "Squashing to single release commit..."
RELEASE_TAG="$(git log -1 --format='%h')"
git checkout --orphan release
git add -A
git commit -m "${COMMIT_MSG:-release: $RELEASE_TAG}"
git branch -D main
git branch -m main

echo "Pushing to $PUBLISH_REMOTE..."
git remote add publish "$PUBLISH_REMOTE"
git push publish HEAD:main --force

# ワーキングツリーをプッシュ内容に同期
git -C "$PUBLISH_REMOTE" reset --hard HEAD

echo "Done: published to $PUBLISH_REMOTE"
