#!/usr/bin/env bash
# run.sh — build (if needed) and start the Video HEVC Converter container.
# Run from the repo root: bash run.sh [--build] [--logs]
#
#   --build   force a fresh image build even if the image already exists
#             (needed after Dockerfile changes)
#   --logs    follow container logs after starting (Ctrl+C to detach; the
#             container keeps running in the background)

set -euo pipefail

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
log()  { printf "%s[+]%s %s\n" "$GREEN"  "$NC" "$*"; }
warn() { printf "%s[!]%s %s\n" "$YELLOW" "$NC" "$*" >&2; }
err()  { printf "%s[x]%s %s\n" "$RED"    "$NC" "$*" >&2; }

BUILD=""
FOLLOW=""
for arg in "$@"; do
    case "$arg" in
        --build) BUILD="--build" ;;
        --logs)  FOLLOW="1" ;;
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

# First run needs --build to compile the image; subsequent runs reuse it
# unless the user asks for a rebuild.
if [[ -z "$BUILD" ]] && ! docker image inspect video_hevc_converter-video-converter >/dev/null 2>&1 \
                    && ! docker image inspect video-hevc-converter-video-converter >/dev/null 2>&1; then
    log "First run — building image."
    BUILD="--build"
fi

log "Starting video-converter ..."
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
