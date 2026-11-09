#!/usr/bin/env bash
# Construit l'image DevisMaker pour linux/amd64 + linux/arm64 et la pousse sur un registre public.
#
#   docker login                                   # (ou : docker login ghcr.io)
#   IMAGE=docker.io/<compte>/devismaker ./scripts/publish_image.sh 0.3.0
#
# Variables : IMAGE (obligatoire), PLATFORMS (défaut linux/amd64,linux/arm64), SKIP_TEST=1 pour sauter l'essai local.
# Sur Mac Apple Silicon, l'étape amd64 passe par l'émulation QEMU de Docker Desktop : compter 10 à 20 min.
set -euo pipefail

VERSION="${1:-}"
: "${IMAGE:?IMAGE=registre/compte/devismaker requis (ex. docker.io/moncompte/devismaker)}"
[ -n "$VERSION" ] || { echo "usage : IMAGE=... $0 <version>   (ex. 0.3.0)"; exit 1; }
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
cd "$(dirname "$0")/.."

[ ! -e .env ] || grep -q '^\.env$' .dockerignore || { echo ".env non exclu par .dockerignore : arrêt"; exit 1; }
docker buildx inspect devis-builder >/dev/null 2>&1 \
  || docker buildx create --name devis-builder --driver docker-container >/dev/null
docker buildx use devis-builder

# 1. Essai local sur l'architecture du Mac : l'image démarre et /health répond
if [ "${SKIP_TEST:-0}" != 1 ]; then
  echo "== essai local"
  docker buildx build --load --build-arg APP_VERSION="$VERSION" -t devismaker:test .
  cid=$(docker run -d --rm -p 18000:8000 devismaker:test)
  trap 'docker stop "$cid" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do curl -fsS http://localhost:18000/health && break; sleep 2; done
  echo
  curl -fsS http://localhost:18000/health | grep -q "\"version\":\"$VERSION\"" || { echo "échec de l'essai local"; exit 1; }
  docker stop "$cid" >/dev/null; trap - EXIT
fi

# 2. Build multi-architecture et publication (version + latest)
echo "== publication $IMAGE:$VERSION ($PLATFORMS)"
docker buildx build --platform "$PLATFORMS" --build-arg APP_VERSION="$VERSION" \
  -t "$IMAGE:$VERSION" -t "$IMAGE:latest" --push .

docker buildx imagetools inspect "$IMAGE:$VERSION"
echo
echo "Publié : $IMAGE:$VERSION  (vérifier que le dépôt est PUBLIC sur le registre)"
