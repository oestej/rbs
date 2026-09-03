#!/usr/bin/env bash

# Wrap a built RBS Desktop application in the disk image that ships to users.
# The image holds the application beside an Applications symlink, which is the
# drag-to-install layout macOS users expect.
#
# macOS gates a downloaded disk image in its own right, so the image is signed
# here when an identity is available. Notarizing it is a separate step, because
# it can only run once the image exists: see tools/notarize_desktop.sh.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly VERSION_SOURCE="${PROJECT_ROOT}/src/rbs/__init__.py"

application="${PROJECT_ROOT}/dist/RBS Desktop.app"
output=""
identity="${APPLE_DEVELOPER_ID:-}"
keychain="${SIGNING_KEYCHAIN:-}"

usage() {
    printf '%s\n' \
        'Package a built RBS Desktop application as a distributable disk image.' \
        '' \
        'Usage: tools/package_dmg.sh [options]' \
        '' \
        'Options:' \
        '  --app PATH      Application bundle to package.' \
        '                  Default: dist/RBS Desktop.app' \
        '  --output PATH   Disk image to write.' \
        '                  Default: dist/RBS-Desktop-VERSION-macos-ARCH.dmg' \
        '  --identity NAME Developer ID Application identity to sign the image' \
        '                  with. Default: $APPLE_DEVELOPER_ID. The image is left' \
        '                  unsigned when no identity is available.' \
        '  --keychain PATH Keychain holding the certificate.' \
        '                  Default: $SIGNING_KEYCHAIN, else the search list.' \
        '  -h, --help      Show this help text.'
}

while (($# > 0)); do
    case "$1" in
        --app)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            application="$2"
            shift
            ;;
        --output)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            output="$2"
            shift
            ;;
        --identity)
            [[ $# -ge 2 ]] || { printf '%s requires a name\n' "$1" >&2; exit 2; }
            identity="$2"
            shift
            ;;
        --keychain)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            keychain="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf '%s\n' 'Disk images can only be built on macOS.' >&2
    exit 1
fi

if [[ ! -d "${application}" ]]; then
    printf 'Application bundle not found: %s\n' "${application}" >&2
    printf '%s\n' 'Run tools/build_desktop.sh first.' >&2
    exit 1
fi

app_version="$(sed -n 's/^__version__ = "\([0-9][0-9.]*\)"$/\1/p' "${VERSION_SOURCE}")"
if [[ "$(printf '%s' "${app_version}" | grep -c .)" != 1 ]]; then
    printf 'Could not read a single application version from %s\n' "${VERSION_SOURCE}" >&2
    exit 1
fi

if [[ -z "${output}" ]]; then
    output="${PROJECT_ROOT}/dist/RBS-Desktop-${app_version}-macos-$(uname -m).dmg"
fi

staging="$(mktemp -d)"
trap 'rm -rf "${staging}"' EXIT

# ditto preserves the bundle's symlinks, permissions, and extended attributes,
# along with the code signature and any stapled notarization ticket the
# application already carries.
ditto "${application}" "${staging}/$(basename "${application}")"
ln -s /Applications "${staging}/Applications"

mkdir -p "$(dirname "${output}")"
rm -f "${output}"
hdiutil create \
    -volname "RBS Desktop ${app_version}" \
    -srcfolder "${staging}" \
    -fs HFS+ \
    -format UDZO \
    -ov \
    "${output}" >/dev/null

hdiutil verify "${output}" >/dev/null

# A disk image is not code, so it takes a plain signature: no hardened runtime
# and no entitlements. The timestamp is still required for notarization.
if [[ -n "${identity}" ]]; then
    codesign_arguments=(--sign "${identity}" --force --timestamp)
    if [[ -n "${keychain}" ]]; then
        codesign_arguments+=(--keychain "${keychain}")
    fi
    codesign "${codesign_arguments[@]}" "${output}"
    codesign --verify --strict --verbose=2 "${output}"
else
    printf '%s\n' 'No Developer ID identity available; the disk image is unsigned.' >&2
fi

printf '\nPackaged RBS Desktop: %s\n' "${output}"
