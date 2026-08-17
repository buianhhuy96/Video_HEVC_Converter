#!/usr/bin/env bash
# build_deb.sh — build the video-hevc-converter .deb package.
#
# Usage: bash build_deb.sh [VERSION]
#   VERSION defaults to 1.0.0 or the VERSION environment variable.
#
# Requires: dpkg-deb (Debian/Ubuntu default). On other distros, install
# 'dpkg' or run this script inside a Debian container.
#
# Output: video-hevc-converter_<version>_all.deb in the repo root.

set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
log()  { printf "%s[+]%s %s\n" "$GREEN"  "$NC" "$*"; }
warn() { printf "%s[!]%s %s\n" "$YELLOW" "$NC" "$*" >&2; }
err()  { printf "%s[x]%s %s\n" "$RED"    "$NC" "$*" >&2; }

PKG=video-hevc-converter
ARCH=all
VERSION="${1:-${VERSION:-1.0.0}}"

if [[ ! -d debian ]] || [[ ! -d app ]]; then
    err "Run this script from the repo root (debian/ and app/ must exist)."
    exit 1
fi
if ! command -v dpkg-deb >/dev/null 2>&1; then
    err "dpkg-deb is required. Install with: sudo apt install dpkg-dev"
    exit 1
fi

BUILD_ROOT="build/${PKG}_${VERSION}_${ARCH}"
log "Cleaning $BUILD_ROOT"
rm -rf "$BUILD_ROOT"

log "Laying out package tree"
install -d "$BUILD_ROOT/DEBIAN"
install -d "$BUILD_ROOT/usr/lib/$PKG"
install -d "$BUILD_ROOT/usr/share/$PKG"
install -d "$BUILD_ROOT/usr/share/doc/$PKG"
install -d "$BUILD_ROOT/lib/systemd/system"

log "Copying Python source"
install -m 0644 app/*.py "$BUILD_ROOT/usr/lib/$PKG/"

log "Copying data files (config template, requirements.txt)"
install -m 0644 debian/config.yaml       "$BUILD_ROOT/usr/share/$PKG/config.yaml"
install -m 0644 debian/requirements.txt  "$BUILD_ROOT/usr/share/$PKG/requirements.txt"

log "Copying systemd unit"
install -m 0644 debian/${PKG}.service    "$BUILD_ROOT/lib/systemd/system/${PKG}.service"

log "Copying docs"
install -m 0644 README.md                "$BUILD_ROOT/usr/share/doc/$PKG/README.md"

log "Generating control file (version=$VERSION)"
sed "s/@VERSION@/$VERSION/" debian/control.in > "$BUILD_ROOT/DEBIAN/control"

log "Copying maintainer scripts"
install -m 0755 debian/postinst "$BUILD_ROOT/DEBIAN/postinst"
install -m 0755 debian/prerm    "$BUILD_ROOT/DEBIAN/prerm"
install -m 0755 debian/postrm   "$BUILD_ROOT/DEBIAN/postrm"

# Make sure control-script line endings are LF (matters when built on Windows).
for f in postinst prerm postrm; do
    if [[ -f "$BUILD_ROOT/DEBIAN/$f" ]]; then
        sed -i 's/\r$//' "$BUILD_ROOT/DEBIAN/$f" 2>/dev/null || true
    fi
done

log "Building package"
dpkg-deb --root-owner-group --build "$BUILD_ROOT" > /dev/null
DEB_FILE="${PKG}_${VERSION}_${ARCH}.deb"
mv "build/${DEB_FILE}" "./${DEB_FILE}"

log "Built ./${DEB_FILE}"

# Optional: run lintian if present (nice sanity check, non-fatal).
if command -v lintian >/dev/null 2>&1; then
    log "Running lintian (informational, non-fatal)"
    lintian "./${DEB_FILE}" || true
fi

printf "\nInstall on the NAS with:\n"
printf "  %ssudo apt install ./${DEB_FILE}%s\n" "$YELLOW" "$NC"
printf "Then:\n"
printf "  %ssystemctl status video-hevc-converter%s\n"      "$YELLOW" "$NC"
printf "  %sjournalctl -u video-hevc-converter -f%s\n"      "$YELLOW" "$NC"
printf "  Web UI at %shttp://<nas-ip>:8080%s\n"             "$GREEN"  "$NC"
