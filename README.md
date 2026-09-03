# RBS

Residency block scheduler. Residents, rotations, and rules go in; a validated
52-week block schedule comes out, placed by OR-Tools CP-SAT. It ships as the
**RBS Desktop** application for macOS on Apple Silicon; `rbs ui` is the local
browser workspace used for development. A hosted cloud version is in progress and not live yet.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

rbs validate data/sample_input.json
rbs schedule data/sample_input.json -o schedule.json
rbs ui                      # browser workspace at http://127.0.0.1:8080
```

## The workspace

Everything lives in workspaces: residents, rotations, clinic policy, locks, and
the solved schedule. Tabs cover **Block Schedule**, **Clinic Schedule**,
**Residents**, **Rotations**, **Clinic**, and **Configuration** (annual calendar
start, automatic locking, training levels, solver tuning).

Closing a workspace in the browser build deletes it permanently. Clean
workspaces close at once; unsaved changes get a confirmation dialog whose
destructive button arms after a short countdown. Opening a file always *adds*
it alongside what is already open — never a replace.

## Commands

| Command | Purpose |
| --- | --- |
| `rbs validate INPUT` | Load and validate an instance |
| `rbs schedule INPUT -o OUT [--engine stub\|cp_sat]` | Ingest → solve → write schedule JSON |
| `rbs schema input\|case\|catalog\|output` | Print JSON Schema |
| `rbs dump-sample -o PATH` | Write the bundled example case |
| `rbs dump-catalog -o PATH` | Write the default rotation catalog |
| `rbs ui [--db PATH] [--port N] [--desktop\|--cloud]` | Local browser workspace + packaging previews |
| `rbs-desktop [WORKSPACE.rbsc]` | Native document application |
| `rbs-cloud [--host H] [--port P] [--sweep-only]` | Hosted multi-user build (in development) |
| `rbs-solver < REQUEST.json > RESPONSE.json` | Standalone JSON solver process |

The normal case JSON omits `rotations`, `requirements`, and `clinic_policy`;
the runner fills those from `--catalog` or the bundled `data/catalog.json`.

## Running it: Desktop first

| | `rbs-desktop` | `rbs ui` | `rbs-cloud` |
| --- | --- | --- | --- |
| Status | **The shipped application** | Local development workspace | In development — not live |
| Database | Ephemeral cache; the `.rbsc` file is the record | Local SQLite desk | Per-user datasets |
| File flow | Native Open/Save dialogs, one document per window | Upload / download | Upload / authenticated download |
| Whole-database replace | Yes | Yes | No — opening a file already covers migration |

Build the desktop artifact with `tools/build_desktop.sh` (`dist/RBS Desktop.app`,
self-contained, no system Python needed) and wrap it for distribution with
`tools/package_dmg.sh`.
`rbs ui --desktop` previews desktop chrome locally (native file actions stay
inert); `--cloud` previews hosted chrome while remaining single-user with no
retention.

Desktop keeps application preferences (palette, solver tuning, auto-locking) in
a validated `settings.json` under `~/Library/Application Support/RBS Desktop`.
Unsaved work survives a crash via a private checkpoint that is recovered once
and removed on orderly exit. Diagnostics live in
`~/Library/Logs/RBS Desktop` with a **Help → Export Logs…** ZIP for support
requests.

## Cloud (in development — not live yet)

Multi-user hosting is in progress: one deployment serving several people, with
a durable control plane (users, dataset mapping, activity clocks) separate from
evicted per-user schedule data, behind a proxy that vouches for identity — RBS
itself never authenticates anyone. There is no live deployment yet; the seams
already exist in `rbs.cloud` and surface in the UI behind `rbs ui --cloud`.

## File formats

- `.rbsc` workspaces are schema v6 and validated strictly — there are no legacy
  upgrades, only the current shape.
- Constraint catalogs (rotations, curricula, clinic policy) are schema v5;
  `data/catalog.json` is the bundled default.
- `schedule.json` carries the result plus `meta` describing validation status,
  raw solver status, and attending-count metrics.

## How it fits together

The UI never calls CP-SAT directly. It shells out to `rbs-solver` with one
JSON document on stdin and reads one back on stdout
(`protocol: "rbs.solve"`, version 4). Set `RBS_SOLVER_COMMAND` to swap the
executable; the desktop bundle ships its own so no system Python is needed.

```text
UI / CLI / hosted adapters
        │  SolverProblem + SolverConfig (+ prior Schedule)
        ▼
public rbs.solver API ── JSON process protocol
        │
        ▼
private rbs.solver.core (CP-SAT compilation and decoding)
        │  Schedule
        ▼
caller
```

Workspaces go through a small core with packaging adapters:

```text
UI components → WorkspaceController → WorkspaceRepository
                                      ├─ SQLite Store (local/desktop)
                                      └─ per-user Stores (hosted)
```

Every mutation carries the `workspace_revision` it read; the Store checks it in
the same transaction as the write, so concurrent edits, solves, and saves
cannot silently overwrite each other. `rbs.desktop` owns native APIs and
`rbs.cloud` owns identity, retention, and solve capacity — neither leaks into
shared code, and static packaging tests keep it that way.

## Development

```bash
python3 -m pytest tests/ -q -p no:cacheprovider
ruff check src/rbs/ tests/
tools/build_desktop.sh        # frozen desktop artifact per-OS (see below)
tools/package_dmg.sh          # macOS disk image around the built application
```

User-facing changes accumulate in [CHANGELOG.md](CHANGELOG.md) under
`Unreleased`; the release checklist lives in [docs/releases.md](docs/releases.md).
Pushing a `vX.Y.Z` tag builds and publishes the macOS application from that
tagged commit.
Application copy follows [docs/ui-glossary.md](docs/ui-glossary.md) and the
visual roles in [docs/visual-system.md](docs/visual-system.md).

## License

Copyright 2026 Jason Mitchell. Open Software License 3.0 — see [LICENSE](LICENSE).
