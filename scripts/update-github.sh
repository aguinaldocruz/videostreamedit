#!/usr/bin/env bash
# Maintain the GitHub presentation for VideoStreamEdit.
# Default: validate local presentation and show planned metadata changes.
# Use --apply to update GitHub metadata, --commit to commit local changes,
# and --push to push the current branch after committing.
set -Eeuo pipefail

APPLY=0
COMMIT=0
PUSH=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --commit) COMMIT=1 ;;
    --push) APPLY=1; COMMIT=1; PUSH=1 ;;
    --help|-h) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
command -v git >/dev/null || { echo 'git is required' >&2; exit 1; }

REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$REMOTE" ]]; then
  echo 'No origin remote configured; cannot update GitHub metadata.' >&2
  exit 1
fi
if ! command -v gh >/dev/null; then
  echo 'gh CLI is required for --apply. Install it from https://cli.github.com/.' >&2
  exit 1
fi
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
if [[ -z "$REPO" ]]; then
  echo 'The origin repository could not be resolved by gh.' >&2
  exit 1
fi

DESCRIPTION='MP3Tag-style Plex media stream editor for movies and TV shows, with safe queued changes, subtitle inspection, reports, and incremental indexes.'
TOPICS=(plex media-management mkv ffmpeg subtitles audio docker self-hosted homelab)

echo "Repository: $REPO"
echo "Branch: $(git branch --show-current)"
echo "Validating local presentation…"
python3 -m compileall -q app tools language-id
docker compose config -q
git diff --check
[[ -f README.md && -f LICENSE && -f SECURITY.md && -f CONTRIBUTING.md ]] || { echo 'Required project documentation is missing.' >&2; exit 1; }

echo 'Local documentation and Compose configuration are valid.'
if (( APPLY )); then
  gh auth status >/dev/null
  gh repo edit "$REPO" --description "$DESCRIPTION" --enable-issues --enable-wiki=false --enable-projects=false
  for topic in "${TOPICS[@]}"; do
    gh repo edit "$REPO" --add-topic "$topic"
  done
  echo "Updated GitHub description and topics."
else
  echo 'Dry run: use --apply to update GitHub description and topics.'
fi

if (( COMMIT )); then
  # Git's ignore rules exclude /config, databases, keys, caches, and logs.
  # Stage the complete source tree so --push represents the latest project.
  git add -A
  if git diff --cached --quiet; then
    echo 'No local presentation changes to commit.'
  else
    git commit -m 'docs: improve GitHub project presentation'
    echo 'Committed local documentation and GitHub metadata updates.'
  fi
fi
if (( PUSH )); then
  git push origin "$(git branch --show-current)"
fi

echo 'Done.'
