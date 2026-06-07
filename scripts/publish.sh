#!/bin/bash
# bluecore（ユーザー向けリポジトリ）に dev ファイルを除いたスナップショットを push する
#
# 使い方:
#   ./scripts/publish.sh [--message <msg>] [--no-commit]
#
#   --message <msg>  コミットメッセージ（"release: <msg>" 形式）。省略時は commit hash。
#   --no-commit      コミット・push せずファイルだけ $PUBLISH_REMOTE に展開する。
set -euo pipefail

PUBLISH_REMOTE="${BLUECORE_PUBLISH_REMOTE:-$HOME/dev/bluecore}"
MESSAGE=""
NO_COMMIT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --message)
      MESSAGE="$2"
      shift 2
      ;;
    --message=*)
      MESSAGE="${1#--message=}"
      shift
      ;;
    --no-commit)
      NO_COMMIT=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

TMPDIR="$(mktemp -d)"
trap "rm -rf $TMPDIR" EXIT

echo "Cloning bluecore-dev..."
git clone --local --no-hardlinks . "$TMPDIR/repo"
cd "$TMPDIR/repo"

echo "Filtering dev-only files..."
git filter-repo \
  --invert-paths \
  --path plugins/bluecore/tests/ \
  --path plugins/bluecore/onnx/ \
  --path plugins/bluecore/src/model_build/ \
  --path scripts/ \
  --path CLAUDE.md \
  --path conftest.py \
  --force

if [[ "$NO_COMMIT" == true ]]; then
  echo "Extracting files to $PUBLISH_REMOTE (no commit)..."
  git archive HEAD | tar -x -C "$PUBLISH_REMOTE"
  echo "Done: files extracted to $PUBLISH_REMOTE (commit manually)"
  exit 0
fi

echo "Squashing to single release commit..."
RELEASE_TAG="$(git log -1 --format='%h')"
git checkout --orphan release
git add -A
git commit -m "release: ${MESSAGE:-$RELEASE_TAG}"
git branch -D main
git branch -m main

echo "Pushing to $PUBLISH_REMOTE..."
git remote add publish "$PUBLISH_REMOTE"
git push publish HEAD:main --force

git -C "$PUBLISH_REMOTE" reset --hard HEAD

echo "Done: published to $PUBLISH_REMOTE"
