#!/usr/bin/env bash
# Commit the bot's files (those given that exist and changed) and push them to main.
# Several workflows can push at the same time (two voice messages in a row = two "AI tasks" runs),
# so a rejected push pulls the new commits (rebase) and tries again. Files that only grow line by
# line (feedback.md, costs.jsonl, images.jsonl) merge by union (.gitattributes), so they never conflict;
# if a rebase still fails, it is aborted so the next attempt starts clean.
# Usage: bash save_state.sh "commit message" file...
set -u
msg=$1; shift
for f in "$@"; do [ -e "$f" ] && git add -- "$f"; done
git diff --cached --quiet && exit 0
git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit -qm "$msg"
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
