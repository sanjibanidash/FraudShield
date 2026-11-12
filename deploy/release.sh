#!/usr/bin/env bash
# Runs ON the VM (called by .github/workflows/deploy.yml, or by hand):
#   bash deploy/release.sh ghcr.io/<owner>/fraudshield <image-tag> [ghcr-user] [ghcr-token]
set -euo pipefail
REGISTRY="$1"
IMAGE_TAG="$2"
GHCR_USER="${3:-}"
GHCR_TOKEN="${4:-}"
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo ".env missing - run deploy/setup_vm.sh first" >&2
  exit 1
fi

set_env() {  # set_env KEY VALUE  -> update or append in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
set_env REGISTRY "$REGISTRY"
set_env IMAGE_TAG "$IMAGE_TAG"
mkdir -p data/raw data/batch/input

if [[ -n "$GHCR_TOKEN" ]]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
fi

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE pull --quiet
$COMPOSE up -d --remove-orphans
docker logout ghcr.io >/dev/null 2>&1 || true
docker image prune -f >/dev/null

echo "Deployed $REGISTRY:$IMAGE_TAG"
$COMPOSE ps --format "table {{.Service}}\t{{.Status}}"
