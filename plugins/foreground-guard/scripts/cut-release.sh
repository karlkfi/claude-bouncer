#!/usr/bin/env bash
#
# cut-release.sh — cut a foreground-guard release: bump, commit, push, tag, publish.
#
# Automates docs/development/release-process.md end to end. A release is three
# artifacts that must agree: the version string (in two files), an annotated git
# tag, and a GitHub Release. This script produces all three from one command,
# with preflight checks that refuse anything the process doc calls an
# anti-pattern (one-sided bump, non-fast-forward main, dirty tree, red tests).
#
# The version string lives in exactly two files, kept identical:
#   - .claude-plugin/plugin.json          -> "version"
#   - .claude-plugin/marketplace.json     -> plugins[0].version
#
# The bump commit is the one sanctioned direct-to-main push (see the doc's
# "direct-to-main exception"): it is pure — two lines, no logic — and is tagged
# only after it lands on main.
#
# Usage:
#   scripts/cut-release.sh [options] <version | patch | minor | major>
#
#   <version>   explicit target, e.g. 0.2.0 (must be > current)
#   patch|minor|major   bump one component of the current version
#
# Options:
#   --dry-run           run every check and print the plan + notes; mutate nothing
#   --yes, -y           skip the final confirmation prompt (for automation)
#   --notes-file FILE   use FILE as the release-notes body verbatim (skip generation)
#   --no-edit           do not open $EDITOR to curate generated notes
#   -h, --help          show this help
#
# The bump level (patch/minor/major) is a human judgment — see the doc. This
# script will compute a keyword bump for convenience but does not choose one.
#
# Requires: git, python3, gh (authenticated). Run it from a checkout whose HEAD
# is level with origin/main (rebase first if behind).

set -euo pipefail

# --- locate the repo ---------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "error: not inside a git repository" >&2
  exit 1
}
cd "$REPO_ROOT"

PLUGIN_JSON=".claude-plugin/plugin.json"
MARKETPLACE_JSON=".claude-plugin/marketplace.json"
REPO_SLUG="karlkfi/claude-foreground-guard"

# --- output helpers ----------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; RST=""
fi
step() { printf '%s==>%s %s\n' "$BOLD" "$RST" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%swarn%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'; exit "${1:-0}"; }

# --- parse args --------------------------------------------------------------
DRY_RUN=0
ASSUME_YES=0
NOTES_FILE=""
NO_EDIT=0
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY_RUN=1; shift ;;
    -y|--yes)      ASSUME_YES=1; shift ;;
    --notes-file)  NOTES_FILE="${2:-}"; [[ -n "$NOTES_FILE" ]] || die "--notes-file needs a path"; shift 2 ;;
    --no-edit)     NO_EDIT=1; shift ;;
    -h|--help)     usage 0 ;;
    -*)            die "unknown option: $1 (try --help)" ;;
    *)             [[ -z "$TARGET" ]] || die "unexpected extra argument: $1"; TARGET="$1"; shift ;;
  esac
done
[[ -n "$TARGET" ]] || usage 1
[[ -z "$NOTES_FILE" || -f "$NOTES_FILE" ]] || die "notes file not found: $NOTES_FILE"

# --- semver helpers ----------------------------------------------------------
is_semver() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

# read current version from plugin.json (source of truth); assert marketplace agrees
read_current_version() {
  local pv mv
  pv="$(python3 - "$PLUGIN_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["version"])
PY
)" || die "could not read $PLUGIN_JSON"
  mv="$(python3 - "$MARKETPLACE_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["plugins"][0]["version"])
PY
)" || die "could not read $MARKETPLACE_JSON"
  [[ "$pv" == "$mv" ]] || die "version files already disagree: $PLUGIN_JSON=$pv, $MARKETPLACE_JSON=$mv (fix before releasing)"
  printf '%s' "$pv"
}

# compute NEW from a keyword, or validate an explicit version
compute_new_version() {
  local cur="$1" spec="$2" major minor patch
  IFS=. read -r major minor patch <<<"$cur"
  case "$spec" in
    major) echo "$((major + 1)).0.0" ;;
    minor) echo "${major}.$((minor + 1)).0" ;;
    patch) echo "${major}.${minor}.$((patch + 1))" ;;
    *)     is_semver "$spec" || die "invalid version '$spec' (want X.Y.Z or patch|minor|major)"; echo "$spec" ;;
  esac
}

# strictly-greater check: is $1 > $2 ?
version_gt() {
  local a="$1" b="$2"
  [[ "$a" != "$b" ]] && [[ "$(printf '%s\n%s\n' "$a" "$b" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" == "$a" ]]
}

# --- preflight ---------------------------------------------------------------
step "Preflight checks"
command -v git   >/dev/null || die "git not found"
command -v python3 >/dev/null || die "python3 not found"
command -v gh    >/dev/null || die "gh (GitHub CLI) not found — needed to publish the release"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run: gh auth login)"

[[ -f "$PLUGIN_JSON" && -f "$MARKETPLACE_JSON" ]] || die "version files not found — run from the repo root"

# working tree must be clean so the bump commit is pure
[[ -z "$(git status --porcelain)" ]] || die "working tree is not clean — commit or stash first (the bump commit must be pure)"

step "Fetching origin/main"
git fetch --quiet origin main || die "git fetch origin main failed"

HEAD_SHA="$(git rev-parse HEAD)"
MAIN_SHA="$(git rev-parse origin/main)"
if [[ "$HEAD_SHA" != "$MAIN_SHA" ]]; then
  if git merge-base --is-ancestor "$HEAD_SHA" "$MAIN_SHA"; then
    die "HEAD is behind origin/main — rebase first: git rebase origin/main"
  else
    die "HEAD has commits not on origin/main — the bump must sit directly atop main. Land or drop them first."
  fi
fi
ok "HEAD is level with origin/main ($(git rev-parse --short HEAD))"

CUR_VERSION="$(read_current_version)"
NEW_VERSION="$(compute_new_version "$CUR_VERSION" "$TARGET")"
is_semver "$NEW_VERSION" || die "computed version '$NEW_VERSION' is not valid semver"
version_gt "$NEW_VERSION" "$CUR_VERSION" || die "new version $NEW_VERSION is not greater than current $CUR_VERSION"
TAG="v$NEW_VERSION"

# tag must not already exist locally or on the remote
! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || die "tag $TAG already exists locally"
[[ -z "$(git ls-remote --tags origin "refs/tags/$TAG")" ]] || die "tag $TAG already exists on origin"

PREV_TAG="$(git tag --list 'v*' --sort=-v:refname | head -1)"
ok "current version $CUR_VERSION → new version ${BOLD}$NEW_VERSION${RST} (previous tag: ${PREV_TAG:-none})"

# --- tests -------------------------------------------------------------------
step "Running test suite"
if python3 -m unittest discover tests >/tmp/cut-release-tests.$$ 2>&1; then
  ok "$(tail -1 /tmp/cut-release-tests.$$)"
  rm -f /tmp/cut-release-tests.$$
else
  cat /tmp/cut-release-tests.$$ >&2
  rm -f /tmp/cut-release-tests.$$
  die "tests failed — fix before releasing"
fi

# --- release notes -----------------------------------------------------------
# Assemble notes from the PRs merged since the previous tag: feat/fix titles
# become curated bullets, everything else is folded into a summary line. The
# intro line and curation are meant to be edited before publishing.
generate_notes() {
  local out="$1" range
  if [[ -n "$PREV_TAG" ]]; then range="${PREV_TAG}..HEAD"; else range="HEAD"; fi

  # PR numbers referenced in the commit range (merge commits + squash subjects).
  # `|| true`: grep exits 1 on no match, which pipefail would turn into a fatal
  # error for a first release with no referenced PRs.
  local prs
  prs="$(git log $range --pretty='%s%n%b' | grep -oE '#[0-9]+' | tr -d '#' | sort -un || true)"

  local feats=() others=()
  local n title author line
  for n in $prs; do
    if ! line="$(gh pr view "$n" --repo "$REPO_SLUG" --json title,author,url \
        --jq '[.title, .author.login, .url] | @tsv' 2>/dev/null)"; then
      continue
    fi
    IFS=$'\t' read -r title author url <<<"$line"
    case "$title" in
      feat*|fix*) feats+=("* ${title} by @${author} in ${url}") ;;
      *)          others+=("#${n}") ;;
    esac
  done

  {
    if [[ "$TARGET" == "major" ]]; then echo "Major release."
    elif [[ "$TARGET" == "minor" || "$NEW_VERSION" =~ \.0$ ]]; then echo "Minor release."
    else echo "Patch release."
    fi
    echo "<one-line theme — edit me>"
    echo
    if [[ ${#feats[@]} -gt 0 ]]; then printf '%s\n' "${feats[@]}"; echo; fi
    if [[ ${#others[@]} -gt 0 ]]; then
      echo "Also includes docs/chore/test changes ($(IFS=', '; echo "${others[*]}"))."
      echo
    fi
    if [[ -n "$PREV_TAG" ]]; then
      echo "**Full Changelog**: https://github.com/${REPO_SLUG}/compare/${PREV_TAG}...${TAG}"
    fi
  } >"$out"
}

NOTES_TMP=""
if [[ -n "$NOTES_FILE" ]]; then
  NOTES_PATH="$NOTES_FILE"
else
  NOTES_TMP="$(mktemp -t cut-release-notes.XXXXXX)"
  NOTES_PATH="$NOTES_TMP"
  step "Generating release notes from PRs since ${PREV_TAG:-repo start}"
  generate_notes "$NOTES_PATH"
  # let a human curate the intro line and bullets unless told not to
  if [[ $DRY_RUN -eq 0 && $NO_EDIT -eq 0 && $ASSUME_YES -eq 0 && -t 0 ]]; then
    "${EDITOR:-vi}" "$NOTES_PATH"
  fi
fi
# An EXIT trap's status becomes the script's, so this must not end on a false
# test: with --notes-file there is no tempfile, and `[[ -n "" ]] && rm` would
# report a fully successful release as exit 1.
cleanup() { if [[ -n "$NOTES_TMP" ]]; then rm -f "$NOTES_TMP"; fi; }
trap cleanup EXIT

# --- plan --------------------------------------------------------------------
echo
step "Release plan"
cat <<PLAN
  version   $CUR_VERSION → $NEW_VERSION
  bump      $PLUGIN_JSON, $MARKETPLACE_JSON
  commit    chore(release): bump version to $NEW_VERSION  (direct to main)
  tag       $TAG (annotated) → the bump commit
  release   gh release create $TAG --latest
  notes:
${DIM}$(sed 's/^/    /' "$NOTES_PATH")${RST}
PLAN

if [[ $DRY_RUN -eq 1 ]]; then
  echo
  ok "dry run — nothing was changed. Re-run without --dry-run to cut $TAG."
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  echo
  read -r -p "Proceed with the release above? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || die "aborted by user (nothing changed)"
fi

# --- execute -----------------------------------------------------------------
# bump both files with a formatting-preserving regex replace (json.dump would
# reorder keys and lose the compact marketplace style).
bump_file() {
  local file="$1"
  python3 - "$file" "$CUR_VERSION" "$NEW_VERSION" <<'PY'
import re, sys
path, cur, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
pat = re.compile(r'("version"\s*:\s*")' + re.escape(cur) + r'(")')
new_src, count = pat.subn(r'\g<1>' + new + r'\g<2>', src)
if count != 1:
    sys.exit(f"expected exactly one version={cur} in {path}, found {count}")
open(path, "w").write(new_src)
PY
}

step "Bumping version files to $NEW_VERSION"
bump_file "$PLUGIN_JSON"      || die "failed to bump $PLUGIN_JSON"
bump_file "$MARKETPLACE_JSON" || die "failed to bump $MARKETPLACE_JSON"

# lockstep sanity: both files must now read the new version
[[ "$(read_current_version)" == "$NEW_VERSION" ]] || die "post-bump version mismatch — aborting before commit"
ok "both files at $NEW_VERSION"

step "Committing the bump"
git commit --quiet -am "chore(release): bump version to $NEW_VERSION"
BUMP_SHA="$(git rev-parse HEAD)"
ok "committed $(git rev-parse --short HEAD)"

step "Pushing the bump to main"
git push origin "HEAD:main" || die "push to main failed — the bump commit is local ($BUMP_SHA); resolve and re-run push"
ok "origin/main now at $NEW_VERSION"

step "Tagging $TAG"
git tag -a "$TAG" -m "$TAG" "$BUMP_SHA"
git push origin "$TAG" || die "tag push failed — tag exists locally; run: git push origin $TAG"
ok "pushed $TAG"

step "Publishing the GitHub Release"
gh release create "$TAG" --repo "$REPO_SLUG" --title "$TAG" --latest --notes-file "$NOTES_PATH" \
  || die "gh release create failed — tag is pushed; retry: gh release create $TAG --latest --notes-file <file>"

echo
ok "${GRN}Released $TAG${RST}"
gh release view "$TAG" --repo "$REPO_SLUG" --json url --jq '.url'
