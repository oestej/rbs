#!/usr/bin/env bash

# Wrap a built RBS Desktop application in the disk image that ships to users.
# The image holds the application beside an Applications symlink, which is the
# drag-to-install layout macOS users expect.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly VERSION_SOURCE="${PROJECT_ROOT}/src/rbs/__init__.py"

application="${PROJECT_ROOT}/dist/RBS Desktop.app"
output=""

usage() {
    printf '%s\n' \
        'Package a built RBS Desktop application as a distributable disk image.' \
        '' \
        'Usage: tools/package_dmg.sh [options]' \
        '' \
        'Options:' \
        '  --app PATH     Application bundle to package.' \
        '                 Default: dist/RBS Desktop.app' \
        '  --output PATH  Disk image to write.' \
        '                 Default: dist/RBS-Desktop-VERSION-macos-ARCH.dmg' \
        '  -h, --help     Show this help text.'
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
# including the ad-hoc code signature PyInstaller applies on Apple Silicon.
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

printf '\nPackaged RBS Desktop: %s\n' "${output}"
