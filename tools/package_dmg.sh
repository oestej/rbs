#!/usr/bin/env bash

# Wrap a built RBS Desktop application in the disk image that ships to users.
# dmgbuild writes the usual drag-to-Applications Finder window: the application
# on the left, an Applications symlink on the right, and an arrow between them.
# packaging/dmg_settings.py owns that layout.
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

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Disk images require uv: https://docs.astral.sh/uv/' >&2
    exit 1
fi

if [[ ! -d "${application}" ]]; then
    printf 'Application bundle not found: %s\n' "${application}" >&2
    printf '%s\n' 'Run tools/build_desktop.sh first.' >&2
    exit 1
fi

# dmgbuild copies by path, so a relative --app would depend on the caller's cwd.
application="$(cd "${application}" && pwd)"

app_version="$(sed -n 's/^__version__ = "\([0-9][0-9.]*\)"$/\1/p' "${VERSION_SOURCE}")"
if [[ "$(printf '%s' "${app_version}" | grep -c .)" != 1 ]]; then
    printf 'Could not read a single application version from %s\n' "${VERSION_SOURCE}" >&2
    exit 1
fi

if [[ -z "${output}" ]]; then
    output="${PROJECT_ROOT}/dist/RBS-Desktop-${app_version}-macos-$(uname -m).dmg"
fi

mkdir -p "$(dirname "${output}")"
output="$(cd "$(dirname "${output}")" && pwd)/$(basename "${output}")"
rm -f "${output}"

# dmgbuild copies the bundle with ditto, so the code signature and any stapled
# notarization ticket the application already carries stay intact.
uv --directory "${PROJECT_ROOT}" run --frozen --extra dmg dmgbuild \
    --settings "${PROJECT_ROOT}/packaging/dmg_settings.py" \
    --detach-retries 20 \
    -D "app=${application}" \
    -D "icon=${PROJECT_ROOT}/packaging/assets/rbs.icns" \
    "RBS Desktop ${app_version}" \
    "${output}"

hdiutil verify "${output}" >/dev/null

# A disk image is not code, so it takes a plain signature: no hardened runtime
# and no entitlements. The timestamp is still required for notarization. Apple's
# timestamp service can refuse a request the same way it does for the app, so
# this one signature retries those refusals instead of failing the package step.
if [[ -n "${identity}" ]]; then
    codesign_arguments=(--sign "${identity}" --force --timestamp)
    if [[ -n "${keychain}" ]]; then
        codesign_arguments+=(--keychain "${keychain}")
    fi
    delay=5
    signed=0
    for attempt in 1 2 3 4 5 6 7 8; do
        if codesign_output="$(codesign "${codesign_arguments[@]}" "${output}" 2>&1)"; then
            [[ -n "${codesign_output}" ]] && printf '%s\n' "${codesign_output}"
            signed=1
            break
        fi
        printf '%s\n' "${codesign_output}" >&2
        case "${codesign_output}" in
            *"A timestamp was expected but was not found"*|*"timestamp service is not available"*)
                ;;
            *)
                printf 'Giving up on: %s\n' "${output}" >&2
                exit 1
                ;;
        esac
        if ((attempt == 8)); then
            break
        fi
        printf 'codesign attempt %d failed for %s; retrying in %ds.\n' \
            "${attempt}" "${output}" "${delay}" >&2
        sleep "${delay}"
        delay=$((delay * 2))
        if ((delay > 60)); then
            delay=60
        fi
    done
    if ((signed == 0)); then
        printf 'Giving up on: %s\n' "${output}" >&2
        exit 1
    fi
    codesign --verify --strict --verbose=2 "${output}"
else
    printf '%s\n' 'No Developer ID identity available; the disk image is unsigned.' >&2
fi

printf '\nPackaged RBS Desktop: %s\n' "${output}"
