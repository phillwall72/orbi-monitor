#!/bin/bash

REGISTRY="plex-nas:8042"
IMAGE="orbi-monitor"
BUILD_DIR="/mnt/user/appdata/orbi-monitor"
DATA_DIR="$BUILD_DIR/data"
VERSION_FILE="$DATA_DIR/version.txt"

# ─── Ensure version file exists ─────────────────────────────────────
mkdir -p $DATA_DIR
if [ ! -f "$VERSION_FILE" ]; then
  echo "1.0.0" > $VERSION_FILE
  echo "📝 Created version file starting at 1.0.0"
fi

CURRENT=$(cat $VERSION_FILE)
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"

# ─── Handle arguments ───────────────────────────────────────────────
case "$1" in
  major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
  patch|"") PATCH=$((PATCH + 1)) ;;
  version)
    echo "Current version : $CURRENT"
    echo "Container image : $(docker inspect --format='{{.Config.Image}}' $IMAGE 2>/dev/null || echo 'not running')"
    exit 0
    ;;
  *)
    echo "Usage: $0 [major|minor|patch|version]"
    exit 1
    ;;
esac

NEW_VERSION="$MAJOR.$MINOR.$PATCH"

# ─── Build directly with registry-prefixed tags ─────────────────────
echo "📦 Building $IMAGE:$NEW_VERSION..."

docker build \
  -t $REGISTRY/$IMAGE:$NEW_VERSION \
  -t $REGISTRY/$IMAGE:latest \
  $BUILD_DIR

if [ $? -ne 0 ]; then
  echo "❌ Build failed"
  exit 1
fi

# ─── Push to registry ───────────────────────────────────────────────
echo "🚀 Pushing to registry..."
docker push $REGISTRY/$IMAGE:$NEW_VERSION
docker push $REGISTRY/$IMAGE:latest

# ─── Remove local build images immediately ──────────────────────────
echo "🧹 Cleaning up local build images..."
docker rmi $REGISTRY/$IMAGE:$NEW_VERSION
docker rmi $REGISTRY/$IMAGE:latest

# ─── Save version ───────────────────────────────────────────────────
echo "$NEW_VERSION" > $VERSION_FILE

echo ""
echo "✅ Done!"
echo "   Previous : $CURRENT"
echo "   New      : $NEW_VERSION"
echo ""
echo "   Registry : http://plex-nas:8042"
echo "   Tags     : $(curl -s http://$REGISTRY/v2/$IMAGE/tags/list)"
echo ""
echo "   Now hit Force Update in Unraid UI to pull and deploy."

