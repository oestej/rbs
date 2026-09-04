#!/usr/bin/env bash

# Sign a built RBS Desktop application with an Apple Developer ID certificate.
#
# codesign seals a bundle by hashing everything inside it, so signing runs
# inside out: every nested Mach-O file first, then the solver executable that
# sits beside the main one in Contents/MacOS, then the bundle itself. Signing
# in any other order invalidates the seal above whatever was signed late.
#
# Every signature carries the hardened runtime and a secure timestamp. The
# notary service rejects builds without both, so they are not optional even
# when signing locally to reproduce a problem.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

application="${PROJECT_ROOT}/dist/RBS Desktop.app"
entitlements="${PROJECT_ROOT}/packaging/entitlements.plist"
identity="${APPLE_DEVELOPER_ID:-}"
keychain="${SIGNING_KEYCHAIN:-}"

usage() {
    printf '%s\n' \
        'Sign a built RBS Desktop application with a Developer ID certificate.' \
        '' \
        'Usage: tools/sign_desktop.sh [options]' \
        '' \
        'Options:' \
        '  --app PATH           Application bundle to sign.' \
        '                       Default: dist/RBS Desktop.app' \
        '  --identity NAME      Developer ID Application identity to sign with.' \
        '                       Default: $APPLE_DEVELOPER_ID' \
        '  --entitlements PATH  Hardened runtime entitlements to request.' \
        '                       Default: packaging/entitlements.plist' \
        '  --keychain PATH      Keychain holding the certificate.' \
        '                       Default: $SIGNING_KEYCHAIN, else the search list.' \
        '  -h, --help           Show this help text.'
}

while (($# > 0)); do
    case "$1" in
        --app)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            application="$2"
            shift
            ;;
        --identity)
            [[ $# -ge 2 ]] || { printf '%s requires a name\n' "$1" >&2; exit 2; }
            identity="$2"
            shift
            ;;
        --entitlements)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            entitlements="$2"
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
    printf '%s\n' 'Applications can only be signed on macOS.' >&2
    exit 1
fi

if [[ ! -d "${application}" ]]; then
    printf 'Application bundle not found: %s\n' "${application}" >&2
    printf '%s\n' 'Run tools/build_desktop.sh first.' >&2
    exit 1
fi

if [[ ! -f "${entitlements}" ]]; then
    printf 'Entitlements file not found: %s\n' "${entitlements}" >&2
    exit 1
fi

if [[ -z "${identity}" ]]; then
    printf '%s\n' \
        'No signing identity given. Pass --identity, or set APPLE_DEVELOPER_ID to' \
        'the full name of a Developer ID Application certificate, for example:' \
        '  "Developer ID Application: Example Inc. (ABCDE12345)"' >&2
    exit 1
fi

identity_arguments=(-v -p codesigning)
if [[ -n "${keychain}" ]]; then
    identity_arguments+=("${keychain}")
fi

if ! security find-identity "${identity_arguments[@]}" | grep -qF -- "${identity}"; then
    printf 'No usable codesigning identity matches: %s\n' "${identity}" >&2
    printf '%s\n' 'Available identities:' >&2
    security find-identity "${identity_arguments[@]}" >&2
    exit 1
fi

codesign_arguments=(--sign "${identity}" --force --timestamp --options runtime)
if [[ -n "${keychain}" ]]; then
    codesign_arguments+=(--keychain "${keychain}")
fi

# codesign waits about fifteen seconds, then fails with "A timestamp was
# expected but was not found" when Apple's timestamp service does not answer.
# A hosted runner can sit in front of a dark minute or two of that service,
# and a bundle this size asks a few hundred times in a row even after it
# recovers. Wait until the host responds, then retry timestamp refusals with
# a growing delay. Other codesign failures (a bad identity, a malformed
# binary) are not the same problem and should not be retried.
timestamp_refused() {
    case "$1" in
        *"A timestamp was expected but was not found"*|*"timestamp service is not available"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

wait_for_timestamp_service() {
    local attempt delay=5
    for attempt in 1 2 3 4 5 6; do
        if curl -fsS -o /dev/null --connect-timeout 10 --max-time 20 \
            "http://timestamp.apple.com/ts01"; then
            return 0
        fi
        printf 'timestamp.apple.com did not answer (attempt %d); retrying in %ds.\n' \
            "${attempt}" "${delay}" >&2
        sleep "${delay}"
        delay=$((delay * 2))
        if ((delay > 30)); then
            delay=30
        fi
    done
    printf '%s\n' \
        'timestamp.apple.com never answered; signing will still be attempted.' >&2
}

sign() {
    local attempt delay=5 output
    for attempt in 1 2 3 4 5 6 7 8; do
        if output="$(codesign "${codesign_arguments[@]}" "$@" 2>&1)"; then
            [[ -n "${output}" ]] && printf '%s\n' "${output}"
            return 0
        fi
        printf '%s\n' "${output}" >&2
        if ! timestamp_refused "${output}"; then
            printf 'Giving up on: %s\n' "${!#}" >&2
            return 1
        fi
        if ((attempt == 8)); then
            break
        fi
        printf 'codesign attempt %d failed for %s; retrying in %ds.\n' \
            "${attempt}" "${!#}" "${delay}" >&2
        sleep "${delay}"
        delay=$((delay * 2))
        if ((delay > 60)); then
            delay=60
        fi
    done
    printf 'Giving up on: %s\n' "${!#}" >&2
    return 1
}

wait_for_timestamp_service

# Contents/MacOS is skipped here and signed below: those two executables take
# entitlements, and the bundle's own signature has to be the last one applied.
printf 'Signing nested binaries in %s\n' "${application}"
nested=0
while IFS= read -r -d '' candidate; do
    case "$(file -b "${candidate}")" in
        Mach-O*) ;;
        *) continue ;;
    esac
    sign "${candidate}"
    nested=$((nested + 1))
done < <(
    find "${application}/Contents" -type f ! -path "${application}/Contents/MacOS/*" -print0
)

if ((nested == 0)); then
    printf '%s\n' 'No nested binaries were found, which a real bundle always has.' >&2
    exit 1
fi

printf 'Signed %d nested binaries.\n' "${nested}"

# The solver is a second executable inside Contents/MacOS. A bundle signature
# covers only the main executable named in Info.plist, so this one is signed on
# its own, before the bundle seals the directory around it.
sign --entitlements "${entitlements}" "${application}/Contents/MacOS/rbs-solver"

sign --entitlements "${entitlements}" "${application}"

# Checks the seal and every nested signature. Gatekeeper acceptance is not
# checked here: it stays negative until tools/notarize_desktop.sh has run.
codesign --verify --deep --strict --verbose=2 "${application}"

printf '\nSigned RBS Desktop: %s\n' "${application}"
