#!/bin/bash
REGISTRY="localhost:8042"
IMAGE="orbi-monitor"

echo "Tags for ${IMAGE}:"
TAGS=$(curl -s http://${REGISTRY}/v2/${IMAGE}/tags/list \
  | tr ',' '\n' \
  | grep -o '"[^"]*"' \
  | grep -v 'tags\|name\|orbi' \
  | tr -d '"')

if [ -z "$TAGS" ]; then
  echo "  No tags found or registry unreachable"
  exit 1
fi

for TAG in $TAGS; do
  DIGEST=$(curl -s -I \
    -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
    http://${REGISTRY}/v2/${IMAGE}/manifests/${TAG} \
    | grep -i "docker-content-digest" \
    | tr -d '\r' \
    | awk '{print $2}')
  echo "  ${TAG} -> ${DIGEST}"
done

echo ""
echo "Local image ID:"
docker inspect --format='{{.Id}}' ${IMAGE} 2>/dev/null || echo "  Image not found locally"

echo ""
echo "Local image created:"
docker inspect --format='{{.Created}}' ${IMAGE} 2>/dev/null
