#!/usr/bin/env bash
# run.sh — build (if needed) and start the Video HEVC Converter container.
# Run from the repo root: bash run.sh [--no-build] [--logs]
#
#   --no-build  skip the image build step (fastest; use only if you know
#               nothing in app/ or Dockerfile changed since last run)
#   --logs      follow container logs after starting (Ctrl+C to detach; the
#               container keeps running in the background)

set -euo pipefail

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
log()  { printf "%s[+]%s %s\n" "$GREEN"  "$NC" "$*"; }
warn() { printf "%s[!]%s %s\n" "$YELLOW" "$NC" "$*" >&2; }
err()  { printf "%s[x]%s %s\n" "$RED"    "$NC" "$*" >&2; }

# Default: always rebuild so `git pull; ./run.sh` picks up code changes.
# Docker's layer cache makes this fast when nothing actually changed.
BUILD="--build"
FOLLOW=""
for arg in "$@"; do
    case "$arg" in
        --no-build) BUILD="" ;;
        --build)    BUILD="--build" ;;  # kept for backward compatibility
        --logs)     FOLLOW="1" ;;
        -h|--help)
            sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) err "Unknown option: $arg"; exit 1 ;;
    esac
done

if [[ ! -f docker-compose.yml ]]; then
    err "docker-compose.yml not found. Run this script from the repo root."
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    err "docker is not installed. Run 'bash install.sh' first."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    err "'docker compose' plugin not available. Run 'bash install.sh' first."
    exit 1
fi

if [[ -n "$BUILD" ]]; then
    log "Building image (cache reused when unchanged) and starting video-converter ..."
else
    log "Starting video-converter (skipping image build) ..."
fi
docker compose up -d $BUILD

# Detect the mapped host port from the compose file (defaults to 8080).
PORT=$(grep -E '^\s+- "[0-9]+:[0-9]+"' docker-compose.yml | head -n1 | sed -E 's/.*"([0-9]+):[0-9]+".*/\1/' || true)
PORT=${PORT:-8080}

# Detect a reachable LAN IP for the "open in browser" hint.
LAN_IP=""
if command -v hostname >/dev/null 2>&1; then
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
fi
LAN_IP=${LAN_IP:-<nas-ip>}

printf "\n"
log "Running. Open the UI at: ${GREEN}http://${LAN_IP}:${PORT}${NC}"
printf "  status:  %sdocker compose ps%s\n"       "$YELLOW" "$NC"
printf "  logs:    %sdocker compose logs -f%s\n"  "$YELLOW" "$NC"
printf "  stop:    %sdocker compose down%s\n"     "$YELLOW" "$NC"
printf "\n"

if [[ -n "$FOLLOW" ]]; then
    log "Following logs (Ctrl+C to detach — container keeps running):"
    docker compose logs -f
fi
