#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

sync_dependencies=true
clean_build=true

usage() {
    printf '%s\n' \
        'Build the self-contained RBS Desktop application.' \
        '' \
        'Usage: tools/build_desktop.sh [options]' \
        '' \
        'Options:' \
        '  --skip-sync  Reuse the current environment without running uv sync.' \
        '  --no-clean   Reuse PyInstaller analysis caches for a faster rebuild.' \
        '  -h, --help   Show this help text.'
}

while (($# > 0)); do
    case "$1" in
        --skip-sync)
            sync_dependencies=false
            ;;
        --no-clean)
            clean_build=false
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
    printf '%s\n' 'RBS Desktop is a macOS application; it can only be built on macOS.' >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'RBS Desktop builds require uv: https://docs.astral.sh/uv/' >&2
    exit 1
fi

cd "${PROJECT_ROOT}"

if [[ "${sync_dependencies}" == true ]]; then
    uv sync --extra desktop --extra build --frozen
fi

uv run --no-sync python tools/build_third_party_licenses.py

pyinstaller_options=(--noconfirm)
if [[ "${clean_build}" == true ]]; then
    pyinstaller_options+=(--clean)
fi

uv run --no-sync pyinstaller "${pyinstaller_options[@]}" rbs-desktop.spec
uv run --no-sync python tools/audit_desktop_bundle.py

artifact="${PROJECT_ROOT}/dist/RBS Desktop.app"
desktop_executable="${artifact}/Contents/MacOS/RBS Desktop"
solver_executable="${artifact}/Contents/MacOS/rbs-solver"

if [[ ! -e "${artifact}" ]]; then
    printf 'Build finished, but the expected artifact was not found: %s\n' "${artifact}" >&2
    exit 1
fi

if [[ ! -x "${desktop_executable}" || ! -x "${solver_executable}" ]]; then
    printf '%s\n' 'Build finished, but one or more bundled executables are missing.' >&2
    exit 1
fi

# These commands exit during argument parsing, before any native window or
# solver work starts. They still exercise each frozen bootloader and dependency
# layout, which catches broken multipackage bundles immediately.
"${desktop_executable}" --version
"${solver_executable}" --version

printf '\nBuilt RBS Desktop: %s\n' "${artifact}"
