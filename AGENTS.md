# RBS guide for coding agents

This file applies to the whole repository. It describes the product decisions and
engineering boundaries that should survive individual feature requests. Read the relevant
code and tests as well; this is a map, not a replacement for them.

## What we are building

RBS is a residency block scheduler for the people who run medical training programs.
Residents, program curricula, rotation rules, clinic policy, time away, special events, and
manual locks go in. RBS produces a usable academic-year block schedule and the associated
half-day clinic schedule, then exports schedules for the people who need them.

The primary product is **RBS Desktop**, a local-first macOS document application. The user's
`.rbsc` file is their durable record; the desktop SQLite database is a working cache. `rbs ui`
is the local browser development workspace. The hosted product has real architectural seams,
but it is still in development and is not a live service. Do not let speculative cloud needs
make the shipped desktop workflow worse.

The user should not need to understand constraint programming. Put domain concepts in the UI,
make hard rules and soft preferences distinct, and turn failures into specific actions the
user can take. A technically correct solver that destroys work, returns an invalid schedule,
or reports an opaque infeasibility is a product failure.

When making tradeoffs, favor these outcomes:

- Protect work the user has entered and make destructive actions unmistakable.
- Let a program build and repair its configuration incrementally.
- Keep previously useful schedules stable where the rules still allow it.
- Explain configuration and solve problems in the language of residents, rotations, clinic,
  training levels, weeks, and half-days.
- Keep dense schedules readable and the editing workflow predictable.

## Product behavior to preserve

### Documents and persistence

- Accepted edits are persisted to the current workspace immediately. Some complex editors
  intentionally hold a local draft until their own **Save** action; guard that draft when the
  user closes the editor or changes selection.
- The application-level **Save** action writes the current workspace to the user's `.rbsc`
  file. It is not what commits ordinary form edits to the workspace.
- In the browser workspace, opening a file adds it to the desk. Closing a workspace deletes
  the server's copy, so unsaved work receives the existing close confirmation and delay.
- In the native desktop app, one window owns one document. Open, New, Close, Save, Save As,
  crash recovery, and the native dirty state must continue to agree about which document is
  owned and whether the file is current.
- Sample data is disposable and requires Save As before it becomes the user's document.
- Portable `.rbsc` data intentionally omits application-owned preferences such as display
  colors, solver tuning, and automatic locking state. Keep that ownership split deliberate.

### Editable state, solve readiness, and schedules

There are three different validity boundaries. Do not collapse them:

1. Pydantic models enforce structural and referential integrity. Persisted objects must never
   contain dangling IDs, malformed ranges, duplicate identities, or contradictory field
   shapes.
2. A workspace may still be incomplete while the user configures it. `rbs.solver.readiness`
   reports necessary, actionable reasons a solve cannot start. A readiness check must never
   reject a problem that could be feasible.
3. A solved schedule is validated against the complete problem before it becomes the current
   solution. A failed or invalid solve must not displace the last useful draft.

An input edit that changes the mathematical problem normally makes the solved schedule stale.
A presentation-only or application-preference edit should preserve it. Direct block or clinic
schedule edits may create an explicitly marked working draft; keep it tied to the current
academic year and make the affected placement a hard input for the next solve where the
workflow promises that behavior.

The prior schedule is also a solver reference. Compatible assignments are warm-start hints and
stability is the leading clinic objective before the configured quality goals. Do not turn a
hint into an accidental hard constraint, and do not reorder objective priorities casually.

All academic weeks in domain models are one-based. Calendar dates and configurable training
level names/codes are user-facing; do not expose zero-based indexes or hardcode labels such as
`PGY1` when a configured label is available.

## Domain and data model

The important projections live in `src/rbs/models/instance.py`:

- `SchedulingCase` contains workspace-specific facts and workflow/application settings.
- `ConstraintCatalog` contains reusable program constraints: rotations, curricula, elective
  rules, rotation groups, and clinic policy.
- `SchedulerInput` is the combined model used by the application and persistence layer.
- `SolverProblem` is the self-contained, presentation-free projection that crosses the solver
  boundary.
- `Schedule` is derived output, including assignments, clinic slots, metrics, diagnostics, and
  solver metadata.
- `RBSCState` in `src/rbs/models/rbsc.py` is the portable document contract.

All external models inherit `StrictModel`, which rejects unknown fields. After a cross-field
edit, fully revalidate the resulting object with `revised(...)`, `model_validate(...)`, or the
appropriate operation helper. Pydantic's `model_copy(update=...)` does not validate updates by
itself. Stable resident, rotation, clinic, and event IDs carry relationships; never replace
them with display names or list positions.

Serialized shapes are compatibility contracts. For a change to `.rbsc`, catalog, settings,
solver protocol, schedule JSON, or hosted configuration:

- decide explicitly whether the enclosing versioned contract must change, and update its
  version constant when it does;
- add a narrowly scoped migration for a compatible prior shape or reject it clearly;
- test both accepted and rejected versions, including round trips;
- update samples, schema commands, README text, and changelog notes that describe the format.

Use the constants in the implementation as the source of truth for current schema and protocol
versions; do not copy a version number into new guidance unless that document must describe a
specific release.

## Architecture and dependency direction

The shared application path is:

```text
NiceGUI components -> WorkspaceSession / WorkspaceController
                   -> WorkspaceRepository
                   -> SQLite Store (local/desktop) or per-user Store proxy (hosted)

UI or CLI -> public rbs.solver contract/client -> versioned JSON child process
                                               -> private rbs.solver.core -> Schedule
```

Honor these boundaries; `tests/test_packaging.py` enforces them:

- Shared code never imports `rbs.cloud` or cloud-only dependencies. Cloud code adapts shared
  code, not the reverse.
- UI components depend on `WorkspaceRepository`, never the concrete SQLite `Store`. Only a
  composition root may construct the store.
- UI leaf modules flow into `rbs.ui.app`; they do not import the application entry point back.
- Application code uses the public `rbs.solver` API. Only the solver service enters
  `rbs.solver.core`, and importing the public solver API must not load OR-Tools.
- Solver code stays independent of NiceGUI, ReportLab, storage, desktop, and cloud packages.
- PyObjC/AppKit code stays under `rbs.desktop.macos`, selected only by the desktop composition
  root.
- Packaging-specific file ownership, identity, retention, and solve capacity enter through
  the protocols in `rbs.ui.host`.

Every workspace mutation uses the `workspace_revision` of the snapshot the caller read. A
schedule write additionally uses `instance_revision`. Keep those checks in the same
transaction as the write so a slow solve, stale dialog, or second browser session cannot
silently overwrite newer work. Reload and present conflicts; do not bypass optimistic
concurrency by fetching a newer revision unless the operation is intentionally safe and
idempotent, as `rename_live` is.

The hosted adapter trusts a proxy-authenticated `Principal`; RBS does not authenticate users.
Resolve identity again for HTTP downloads and exports rather than trusting a long-lived UI
socket. Preserve per-user store isolation, bounded solve capacity, retention tombstones, and
the rule that only mutations advance retention activity.

Logging is structured and privacy-preserving. Use stable event codes and the allowlisted scalar
fields in `rbs.logging`. Never log resident names, email addresses, document contents, file
paths, tokens, URLs, solver payloads, or arbitrary exception text outside the existing
sanitization pipeline.

## UI implementation

Keep domain transformations in testable operation/helper modules and rendering in the NiceGUI
modules. Pass updated `SchedulerInput` values through the existing save callbacks and session
methods instead of reaching into persistence from a component. Refresh the affected tab or
panel; avoid remounting the whole application shell for a local edit.

Follow both UI contracts before adding styles or copy:

- `docs/ui-glossary.md` owns user-facing terminology and capitalization.
- `docs/visual-system.md` owns visual roles and points to their implementation sources.

In particular:

- Use the shared page shells in `rbs.ui.page_shells` and button variants in `rbs.ui.buttons`.
- Use semantic classes and the tokens in `src/rbs/ui/static/tokens.css`. Feature stylesheets do
  not introduce interface color literals, font sizes, numeric font weights, radii, shadow
  recipes, or motion timings. Rotation and clinic colors are workspace data passed through
  scoped custom properties.
- Keep visible buttons and notifications in sentence case. Give icon-only controls a specific
  accessible name and a tooltip when nearby text does not make the action clear.
- Treat narrow layouts, long names, configured training-level labels, empty workspaces, stale
  schedules, and tall dialogs as normal states.
- PDF output has a separate point-based print system in `rbs.ui.print_tokens`; share semantic
  colors through `visual_tokens.py` and verify the rendered pages when changing reports.

Avoid exposing SQLite, CP-SAT, process protocols, revisions, or packaging details in ordinary
product copy. Diagnostics may be precise, but they should lead with what the user can change.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/rbs/models/` | Strict domain, document, and schedule contracts |
| `src/rbs/solver/` | Public solver contract, readiness, validation, process client/service |
| `src/rbs/solver/core/` | Private CP-SAT compilation, objectives, solving, and decoding |
| `src/rbs/repository.py`, `src/rbs/workspaces.py` | Storage-neutral repository and command policy |
| `src/rbs/store*.py` | SQLite schema, migrations, workspace/catalog persistence, exchange |
| `src/rbs/ui/` | Shared NiceGUI application and packaging-neutral product UI |
| `src/rbs/ui/*/ops.py` | Pure or mostly pure domain transformations for editors |
| `src/rbs/desktop/` | Native document lifecycle, recovery, settings, diagnostics, packaging |
| `src/rbs/cloud/` | Proxy identity, per-user datasets, retention, and solve pool |
| `data/catalog.json` | Versioned default program catalog shipped with the application |
| `tests/` | Tests arranged to mirror core, UI, solver, desktop, and cloud concerns |
| `tools/`, `packaging/` | Release, desktop bundle, signing, notarization, and disk image tooling |

## Working and verification

The project supports Python 3.11 and newer; CI and desktop packaging currently exercise Python
3.14. Use the checked-in `uv.lock`. Prefer a focused test while iterating, then run the gates
appropriate to the change.

```bash
uv lock --check
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev --extra desktop pytest -m "not solve"
```

Tests that start a real CP-SAT search must carry the `solve` marker. They judge an anytime
solver inside a wall-clock budget and need a capable local machine; hosted CI deliberately
excludes them. Run the full suite locally for solver behavior changes:

```bash
uv run --frozen --extra dev --extra desktop pytest
# or only the real search tests
uv run --frozen --extra dev --extra desktop pytest -m solve
```

Keep deterministic compilation, validation, planning, golden-proto, clinic allocation, and
stub-process tests outside the `solve` group. If objective or constraint structure changes,
inspect the golden objective test and prove that hard constraints, soft goals, final validation,
and multi-attempt result selection still agree.

For manual UI work, use a disposable database rather than the default desk:

```bash
uv run --frozen --extra dev rbs ui --db /tmp/rbs-agent.sqlite
uv run --frozen --extra dev rbs ui --desktop --db /tmp/rbs-agent-desktop.sqlite
uv run --frozen --extra dev rbs ui --cloud --db /tmp/rbs-agent-cloud.sqlite
```

The desktop and cloud flags preview chrome only; they do not exercise native file APIs or real
hosted identity/retention infrastructure. Run the relevant adapter tests for those behaviors.
Build the app with `tools/build_desktop.sh` only when packaging or native integration warrants
it.

Do not commit local databases, solve output, rendered reports, build directories, virtual
environments, or other ignored artifacts. Preserve unrelated work already present in the
worktree.

## User-facing changes and releases

Add one user-focused bullet under `CHANGELOG.md`'s `Unreleased` section for a notable behavior
change. Describe the result, not the implementation. Skip entries for tests and internal
refactors unless users or operators are affected. Follow `docs/releases.md` for surface labels,
compatibility disclosures, and the release workflow.

Do not bump the package version, roll the changelog, tag, sign, notarize, or publish a release
as part of ordinary feature work. Those are explicit release operations.

Before handing off a change, check that the implementation, focused tests, user-visible copy,
compatibility behavior, and changelog all tell the same story. The strongest proof is an
end-to-end domain example: the user makes a realistic edit, RBS preserves the right prior work,
the solve boundary behaves correctly, and the resulting screen or export explains the state
without implementation jargon.
