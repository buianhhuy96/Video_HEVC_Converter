#!/usr/bin/env bash
# install.sh — bootstrap Video HEVC Converter on a fresh Linux NAS.
# Run from the repo root: bash install.sh
#
# What it does:
#   - Installs Docker Engine + docker compose plugin (idempotent)
#   - Adds the invoking user to the docker group
#   - Detects /dev/dri/renderD128 (Intel QSV)
#   - Detects host render/video GIDs and patches docker-compose.yml
#   - Detects host timezone and patches the TZ environment variable
#   - Creates bind-mount directories (config/, logs/, state/, tmp/)
#
# Nothing else is needed on the host — Python 3, FFmpeg with QSV drivers,
# and every other runtime dependency live inside the container image.

set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
log()  { printf "%s[+]%s %s\n" "$GREEN"  "$NC" "$*"; }
warn() { printf "%s[!]%s %s\n" "$YELLOW" "$NC" "$*" >&2; }
err()  { printf "%s[x]%s %s\n" "$RED"    "$NC" "$*" >&2; }

SUDO=""

require_repo_root() {
    if [[ ! -f docker-compose.yml || ! -f Dockerfile ]]; then
        err "Run this script from the repository root (docker-compose.yml + Dockerfile must be present)."
        exit 1
    fi
}

require_linux() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        err "This script targets Linux hosts (your NAS). Detected: $(uname -s)."
        exit 1
    fi
}

configure_sudo() {
    if [[ $EUID -eq 0 ]]; then
        SUDO=""
        return
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        err "Not running as root and 'sudo' is not installed. Re-run as root."
        exit 1
    fi
    if ! sudo -n true 2>/dev/null; then
        warn "sudo will prompt for your password when needed."
    fi
    SUDO="sudo"
}

detect_os() {
    if [[ ! -f /etc/os-release ]]; then
        err "Cannot detect OS: /etc/os-release missing."
        exit 1
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    log "Detected OS: ${PRETTY_NAME:-$ID $VERSION_ID}"
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        log "Docker already installed: $(docker --version)"
        return
    fi
    log "Installing Docker Engine via https://get.docker.com ..."
    if ! command -v curl >/dev/null 2>&1; then
        $SUDO apt-get update -qq && $SUDO apt-get install -y -qq curl
    fi
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    $SUDO sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    $SUDO systemctl enable --now docker
    log "Docker installed: $(docker --version)"
}

verify_compose() {
    if docker compose version >/dev/null 2>&1; then
        log "Docker Compose plugin OK: $(docker compose version | head -n1)"
        return
    fi
    warn "'docker compose' plugin not found; installing docker-compose-plugin ..."
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq docker-compose-plugin
    docker compose version >/dev/null 2>&1 || {
        err "docker compose still not available after install. Install it manually and re-run."
        exit 1
    }
    log "Docker Compose plugin installed."
}

add_user_to_docker_group() {
    local user
    user="${SUDO_USER:-$USER}"
    if ! getent group docker >/dev/null 2>&1; then
        warn "docker group missing (unusual). Skipping user add."
        return
    fi
    if id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
        log "User '$user' already in docker group."
        return
    fi
    $SUDO usermod -aG docker "$user"
    warn "Added '$user' to docker group. Log out and back in (or reboot) for it to take effect."
}

detect_qsv() {
    if [[ -e /dev/dri/renderD128 ]]; then
        local perms
        perms=$(stat -c "%A %U:%G" /dev/dri/renderD128)
        log "Intel QSV device /dev/dri/renderD128 present ($perms)."
    else
        warn "/dev/dri/renderD128 not found — converter will fall back to CPU (libx265)."
        warn "If the NAS has an Intel iGPU, verify it is enabled in BIOS and Intel drivers are loaded."
    fi
}

patch_gids() {
    local render_gid video_gid
    render_gid=$(getent group render 2>/dev/null | cut -d: -f3 || true)
    video_gid=$(getent group video 2>/dev/null | cut -d: -f3 || true)

    if [[ -z "$render_gid" ]]; then
        warn "'render' group not present on host — leaving docker-compose.yml unchanged for render GID."
    else
        log "Patching render GID → $render_gid in docker-compose.yml"
        # Match any current numeric value on the "render" GID line.
        sed -i.bak -E "s/- \"[0-9]+\"([[:space:]]*# render)/- \"$render_gid\"\1/" docker-compose.yml
    fi
    if [[ -z "$video_gid" ]]; then
        warn "'video' group not present on host — leaving docker-compose.yml unchanged for video GID."
    else
        log "Patching video GID → $video_gid in docker-compose.yml"
        sed -i.bak -E "s/- \"[0-9]+\"([[:space:]]*# video)/- \"$video_gid\"\1/" docker-compose.yml
    fi
    rm -f docker-compose.yml.bak 2>/dev/null || true
}

patch_timezone() {
    local tz=""
    if command -v timedatectl >/dev/null 2>&1; then
        tz=$(timedatectl show --value --property=Timezone 2>/dev/null || true)
    fi
    if [[ -z "$tz" && -f /etc/timezone ]]; then
        tz=$(tr -d '[:space:]' < /etc/timezone)
    fi
    if [[ -z "$tz" && -L /etc/localtime ]]; then
        tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
    fi
    if [[ -z "$tz" ]]; then
        warn "Could not detect host timezone; leaving TZ in docker-compose.yml unchanged."
        return
    fi
    log "Patching container TZ → $tz"
    sed -i.bak -E "s|TZ:[[:space:]]*.*|TZ: $tz|" docker-compose.yml
    rm -f docker-compose.yml.bak 2>/dev/null || true
}

create_mount_dirs() {
    mkdir -p config logs state tmp
    log "Ensured mount directories exist: config/ logs/ state/ tmp/"
}

print_next_steps() {
    printf "\n"
    log "Installation complete."
    printf "\nNext steps:\n"
    printf "  1. Edit %sdocker-compose.yml%s — update the '/volume1/...' bind mounts to point at your actual media shares.\n" "$YELLOW" "$NC"
    printf "  2. (Optional) Edit %sconfig/config.yaml%s — set 'scan_paths', 'sweep_at_time', etc.\n" "$YELLOW" "$NC"
    printf "  3. Set %sUI_PASSWORD%s in docker-compose.yml if you want the web UI password-protected.\n" "$YELLOW" "$NC"
    printf "  4. Start the service:\n"
    printf "       %sdocker compose up -d --build%s\n" "$GREEN" "$NC"
    printf "  5. Follow the logs:\n"
    printf "       %sdocker compose logs -f%s\n" "$GREEN" "$NC"
    printf "  6. Open the web UI:  %shttp://<nas-ip>:8080%s\n" "$GREEN" "$NC"
    printf "\n"
    if [[ -n "${SUDO}" ]]; then
        warn "If your user was just added to the docker group, log out and back in first, otherwise 'docker' commands will still need sudo."
    fi
}

main() {
    log "Video HEVC Converter — install script"
    require_repo_root
    require_linux
    configure_sudo
    detect_os
    install_docker
    verify_compose
    add_user_to_docker_group
    detect_qsv
    patch_gids
    patch_timezone
    create_mount_dirs
    print_next_steps
}

main "$@"
