#!/bin/bash

# ─── Get current version from latest git tag ────────────────────────
CURRENT=$(git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
if [ -z "$CURRENT" ]; then
  CURRENT="v0.0.0"
fi

IFS='.' read -r MAJOR MINOR PATCH <<< "${CURRENT#v}"

# ─── Handle arguments ────────────────────────────────────────────────
case "$1" in
  major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
  patch|"") PATCH=$((PATCH + 1)) ;;
  *)
    echo "Usage: $0 [major|minor|patch]"
    exit 1
    ;;
esac

NEW_VERSION="v$MAJOR.$MINOR.$PATCH"

# ─── Check for changes ───────────────────────────────────────────────
if git diff --quiet && git diff --cached --quiet; then
  echo "⚠️  No changes to commit — only creating tag $NEW_VERSION"
  read -p "Continue? (y/n): " CONFIRM
  if [ "$CONFIRM" != "y" ]; then exit 0; fi
else
  # Stage all changes
  git add -A

  echo ""
  echo "📝 Changed files:"
  git diff --cached --name-only
  echo ""

  # Prompt for commit message
  read -p "Commit message: " MSG
  if [ -z "$MSG" ]; then
    echo "❌ Commit message required"
    exit 1
  fi

  git commit -m "$MSG"
fi

# ─── Tag and push ────────────────────────────────────────────────────
echo ""
echo "🏷️  Tagging $CURRENT → $NEW_VERSION"
git tag "$NEW_VERSION"
git push origin main --tags

echo ""
echo "✅ Done! GitHub Actions will build and push ghcr.io/phillwall72/orbi-monitor:latest"
echo "   Version : $NEW_VERSION"
echo "   Then Force Update in Unraid to deploy."
