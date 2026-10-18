#!/usr/bin/env bash
# Update the feed and publish it as a single fresh commit on the `data` branch.
# Because that branch is force-pushed as one commit every time, deleted photos
# do not pile up in git history and the repository stays small.
set -euo pipefail

REMOTE_URL="${REMOTE_URL:?REMOTE_URL is required}"
BRANCH="${DATA_BRANCH:-data}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CHANNELS="${CHANNELS_FILE:-$HERE/../channels.txt}"
WORK="$(mktemp -d)/data"

if git ls-remote --exit-code --heads "$REMOTE_URL" "$BRANCH" >/dev/null 2>&1; then
  git clone --quiet --depth=1 --branch "$BRANCH" "$REMOTE_URL" "$WORK"
else
  git init --quiet -b "$BRANCH" "$WORK"
  git -C "$WORK" remote add origin "$REMOTE_URL"
fi

python3 "$HERE/update.py" --channels "$CHANNELS" --out "$WORK"

cd "$WORK"
git config user.name  "feed-bot"
git config user.email "feed-bot@users.noreply.github.com"
git checkout --quiet --orphan snapshot
git add -A
git commit --quiet -m "update $(date -u +%Y-%m-%dT%H:%MZ)"
git push --quiet --force origin "snapshot:refs/heads/$BRANCH"
echo "published to branch '$BRANCH'"
