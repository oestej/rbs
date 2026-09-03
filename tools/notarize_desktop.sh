#!/usr/bin/env bash

# Submit a signed artifact to Apple's notary service and staple the resulting
# ticket onto it.
#
# Both artifacts a release produces go through this. The application is
# notarized so that a stapled copy validates on a machine that is offline the
# first time it runs it, and the disk image is notarized because macOS gates
# the image a user downloads as well as the application inside it.
#
# Notarization authenticates with an App Store Connect API key, not with an
# Apple ID password.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

artifact="${PROJECT_ROOT}/dist/RBS Desktop.app"
key_path="${APPLE_API_KEY_PATH:-}"
key_id="${APPLE_API_KEY_ID:-}"
issuer_id="${APPLE_API_ISSUER_ID:-}"
wait_timeout="60m"

usage() {
    printf '%s\n' \
        'Notarize a signed artifact and staple its ticket.' \
        '' \
        'Usage: tools/notarize_desktop.sh [options]' \
        '' \
        'Options:' \
        '  --path PATH     Application bundle or disk image to notarize.' \
        '                  Default: dist/RBS Desktop.app' \
        '  --key PATH      App Store Connect API key (.p8) file.' \
        '                  Default: $APPLE_API_KEY_PATH' \
        '  --key-id ID     Key identifier. Default: $APPLE_API_KEY_ID' \
        '  --issuer ID     Issuer identifier. Default: $APPLE_API_ISSUER_ID' \
        '  --timeout SPEC  How long to wait for a verdict. Default: 60m' \
        '  -h, --help      Show this help text.'
}

while (($# > 0)); do
    case "$1" in
        --path)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            artifact="$2"
            shift
            ;;
        --key)
            [[ $# -ge 2 ]] || { printf '%s requires a path\n' "$1" >&2; exit 2; }
            key_path="$2"
            shift
            ;;
        --key-id)
            [[ $# -ge 2 ]] || { printf '%s requires an identifier\n' "$1" >&2; exit 2; }
            key_id="$2"
            shift
            ;;
        --issuer)
            [[ $# -ge 2 ]] || { printf '%s requires an identifier\n' "$1" >&2; exit 2; }
            issuer_id="$2"
            shift
            ;;
        --timeout)
            [[ $# -ge 2 ]] || { printf '%s requires a duration\n' "$1" >&2; exit 2; }
            wait_timeout="$2"
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
    printf '%s\n' 'Artifacts can only be notarized on macOS.' >&2
    exit 1
fi

if [[ ! -e "${artifact}" ]]; then
    printf 'Artifact not found: %s\n' "${artifact}" >&2
    exit 1
fi

if [[ -z "${key_path}" ]]; then
    printf '%s\n' 'No API key given; pass --key or set APPLE_API_KEY_PATH.' >&2
    exit 1
fi

if [[ -z "${key_id}" ]]; then
    printf '%s\n' 'No key identifier given; pass --key-id or set APPLE_API_KEY_ID.' >&2
    exit 1
fi

if [[ -z "${issuer_id}" ]]; then
    printf '%s\n' 'No issuer given; pass --issuer or set APPLE_API_ISSUER_ID.' >&2
    exit 1
fi

if [[ ! -f "${key_path}" ]]; then
    printf 'App Store Connect API key not found: %s\n' "${key_path}" >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# The notary service accepts an archive, not a directory. ditto's archive keeps
# the bundle's symlinks, permissions, and signature intact; `zip` does not.
upload="${artifact}"
if [[ -d "${artifact}" ]]; then
    upload="${work}/$(basename "${artifact}").zip"
    ditto -c -k --keepParent "${artifact}" "${upload}"
fi

notary_arguments=(
    --key "${key_path}"
    --key-id "${key_id}"
    --issuer "${issuer_id}"
)

printf 'Submitting %s for notarization.\n' "$(basename "${artifact}")"

submitted=0
xcrun notarytool submit "${upload}" \
    "${notary_arguments[@]}" \
    --wait \
    --timeout "${wait_timeout}" \
    --output-format json > "${work}/submission.json" || submitted=$?

status="$(plutil -extract status raw -o - "${work}/submission.json" 2>/dev/null || true)"
submission_id="$(plutil -extract id raw -o - "${work}/submission.json" 2>/dev/null || true)"

if ((submitted != 0)) || [[ "${status}" != "Accepted" ]]; then
    printf 'Notarization did not succeed (status: %s).\n' "${status:-unknown}" >&2
    if [[ -n "${submission_id}" ]]; then
        # The log names the specific binary and reason, which the verdict does not.
        xcrun notarytool log "${submission_id}" "${notary_arguments[@]}" >&2 || true
    else
        cat "${work}/submission.json" >&2
    fi
    exit 1
fi

# Stapling writes the ticket into the artifact so that Gatekeeper can approve it
# without asking Apple, which is what makes a first launch work offline.
xcrun stapler staple "${artifact}"
xcrun stapler validate "${artifact}"

if [[ -d "${artifact}" ]]; then
    spctl --assess --type exec --verbose=2 "${artifact}"
else
    spctl --assess --type open --context context:primary-signature --verbose=2 "${artifact}"
fi

printf '\nNotarized and stapled: %s\n' "${artifact}"
