#!/usr/bin/env bash
# Commit the bot's files (those given that exist and changed) and push them to main.
# Several workflows can push at the same time (two voice messages in a row = two "AI tasks" runs),
# so a rejected push pulls the new commits (rebase) and tries again. Files that only grow line by
# line (feedback.md, costs.jsonl, images.jsonl) merge by union (.gitattributes), so they never conflict;
# if a rebase still fails, it is aborted so the next attempt starts clean.
# Usage: bash save_state.sh "commit message" file...
set -u
msg=$1; shift
# The rebase in "git pull --rebase" re-commits our commit, so it needs an identity too, not only "git commit".
export GIT_AUTHOR_NAME="github-actions[bot]" GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL
for f in "$@"; do [ -e "$f" ] && git add -- "$f"; done
git diff --cached --quiet && exit 0
git commit -qm "$msg"
for i in 1 2 3 4 5; do
  if git pull -q --rebase origin main; then
    git push -q origin HEAD:main && exit 0
  else
    git rebase --abort 2>/dev/null
  fi
  sleep $((i * 3))
done
echo "could not push: $msg" >&2
exit 1
