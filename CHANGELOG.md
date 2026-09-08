# Changelog

## [Unreleased]

### Changed

- **Files:** Only the current file format opens. Older `.rbsc` files are
  rejected instead of being upgraded in place.

## [0.1.7] - 2026-09-07

### Changed

- **Rotations:** Mandatory and available Elective services can now be kept
  contiguous with Clinic, FMED/Inpatient, or both for each training level.
  This grouping is one-way: every configured service block receives its
  selected companion blocks, while additional Clinic and FMED blocks remain
  free to schedule elsewhere.
- **Rotations:** Dense configuration screens now start from compact summaries:
  elective availability opens one four-week block at a time, multi-level rule
  editors collapse when several levels are configured, and repeated detail
  metadata and completion badges have been simplified.
- **Rotations:** The FMED rule editor now uses a large, near-full-window dialog
  with horizontal tabs for general, training-level, clinic, and named-resident
  override settings. FMED elective takes are always available year-round rather
  than carrying separate blackout rules.
- **Rotations:** All named-resident rotation additions, including fixed Clinic
  blocks and Mandatory or FMED services, prefer that resident's unallocated
  time before replacing a same-length Elective block. The Clinic resident
  exceptions screen also supports exempting a named resident from a direct
  Clinic requirement.
- **Rotations:** The Mandatory and standalone Elective editors now keep
  Save in the header next to the close button instead of Cancel and Save
  actions at the bottom. Closing, or picking another rotation, with
  unsaved changes asks whether to keep editing, discard the changes, or
  save them. Each section tracks its own edits, so simultaneous edits in
  Mandatory and Electives are each confirmed, and switching sections
  leaves unsaved edits in place.
- **Residents:** The resident editor now keeps Save changes (or Add
  resident) in the header next to the close button instead of Save and
  Cancel actions at the bottom.

### Fixed

- **Residents:** Adding or changing a block now saves it as an immediate
  calendar assignment with clinic half-days left open, while marking the case
  as needing a solve. The editor no longer falls back to a pending-only lock
  when the working schedule cannot be saved with it.
- **Residents:** The rotation menu in the block editor now clears its existing
  text when typing ahead, so a new search starts from an empty box instead of
  appending to the current selection.
- **Residents:** The rotation menu in the block editor now greys out Mandatory
  rotations the resident already has on their schedule, so they cannot be added
  a second time. Blocks pending solve count as scheduled, while Elective
  options may repeat and stay enabled. A new block also opens on the first
  still-available rotation instead of a greyed-out default, and saving a
  greyed-out rotation is rejected.
- **Residents:** The block editor now disables weeks that overlap another
  block or manual pin on the resident's schedule instead of offering them,
  and saving an overlapping range is rejected instead of displacing the
  existing block.
- **Residents:** Manually locked schedule blocks now show a "Locked" status
  instead of "Manual".
- **Residents:** The clinic schedule header now uses the same Show completed
  / Edit schedule order as the block schedule, and both headers pin those
  controls to the right so they no longer jump left when the row wraps.
- **Residents:** The block and clinic schedule editors now say "Return to
  view" instead of "Done editing". Every change already saves the moment it
  is made, so returning to view is navigation only and never required to
  save.
- **Residents:** The block schedule editor has a "Delete all unlocked"
  button with a confirmation step that deletes unlocked blocks together
  while keeping locked blocks. A new Solve is required afterwards. The
  button is hidden when the resident has no unlocked blocks.
- **Residents:** Elective preferences no longer need Add or Save buttons.
  Choosing a service in the (now clearly marked) add menu appends it at
  once, and reordering or removing requests saves automatically.
- **Residents:** The New resident form no longer shows the continuity
  clinic half-days section; like vacation and days off, it is configured
  when editing the resident instead.
- **Residents and Clinic schedule:** PDF exports now open in a new browser
  window instead of downloading. In the desktop app they open in the
  user's preferred PDF viewer rather than inside the app window.
- **PDF exports:** Every page now shows the export date and time in the top
  right, with a readable month, AM/PM time, and local time zone.

## [0.1.6] - 2026-09-07

### Changed

- **Residents:** Block schedule editing now presents assignments, manual pins,
  and unscheduled gaps in one chronological view, with calendar dates alongside
  week numbers. Adding or editing an exact block places it on the working
  schedule immediately and pins it for the next solve instead of leaving it in
  a separate pending list.

### Fixed

- **Residents:** Resident lists and selection menus now sort alphabetically by
  last name, using the first name for residents with a single-name record.
- **Rotations:** Mandatory and Elective selection menus, including eligible
  rotation choices, now sort alphabetically by code, with the rotation name used
  when a code is unavailable.
- **Scheduling:** Clinic half-day capacity is now a rule the solver plans around instead
  of a check applied to the finished schedule. A half-day can no longer be given more
  residents than every open clinic can seat between them, which is what produced
  schedules that were generated and then rejected.
- **Scheduling:** A solve no longer reports a clinic placement failure when one of its
  concurrent attempts produced a usable schedule. Attempts were compared on objective
  alone, so a rejected result could displace a valid one from the same solve.

## [0.1.5] - 2026-09-06

### Added

- **Clinic:** Closure days can now be copied from one clinic to another without
  replacing destination-only dates.

### Changed

- **Rotations and Clinic:** Minimum concurrent staffing fields and guidance now
  make clear that a configured minimum applies in every academic week, including
  weeks when no block would otherwise be placed.

### Fixed

- **Scheduling:** Solve now stops before search for provable configuration conflicts,
  marks the workspace as `Cannot solve`, and links each issue to its editor. Checks
  identify missing Clinic fallbacks for Elective block shapes, impossible weekly
  staffing minimums, and required blocks longer than their consecutive-week limit.
- **Scheduling:** Solve results now distinguish block infeasibility, search timeouts,
  model-build failures, and a feasible block schedule whose clinic placement failed;
  diagnostic actions can open the relevant Clinic, resident, rotation, or Special
  event configuration.
- **Scheduling:** Invalid post-solve clinic placements are no longer saved. Existing
  drafts are retained, and the result explicitly says when a generated schedule was
  rejected.
- **Clinic:** Capacity validation now reports each affected half-day once using its
  final headcount, then groups failures by clinic with the peak overflow and suggested
  resolutions instead of repeating an error as every resident is assigned.
- **Scheduling:** Exact compile-time configuration errors remain consolidated instead
  of expanding into a misleading error for every resident, while genuine resident
  vacation-coverage failures retain their focused explanation.
- **Scheduling:** Solver diagnostics that are taller than the screen now scroll
  inside their dialog so every conflict and suggested resolution remains reachable.
- **Rotations:** Changing a mandatory block configuration's length now persists
  when the rotation is saved instead of silently reverting to its previous length.

## [0.1.4] - 2026-09-06

### Added

- **Rotations:** Each block shape in a mandatory rotation's training-level
  rules now has its own Mandatory / Elective / Both switch, so one service
  can be required in some shapes and offered as an elective in others. New
  shapes start as Mandatory.
- **Rotations:** Resident exceptions can now waive a resident out of a
  mandatory block, freeing those weeks as unscheduled time, and an extra
  mandatory placement can be funded from a training level's unallocated
  weeks instead of replacing an elective block.
- **Rotations:** Adding a mandatory or standalone elective rotation now uses
  the same full-screen editor as editing, with every option available up
  front.

### Changed

- **Rotations:** Elective availability for a mandatory service is derived
  from its per-shape switches and the Elective curriculum when saving,
  replacing the separate "available as elective" checkboxes and block-size
  picker. Repeatable-as-elective is still configurable per service.
- **Rotations:** Growing a mandatory requirement spends a training level's
  unscheduled weeks first and Elective time second, and saving is rejected
  when the requirement would exceed that level's maximum total weeks.
- **Clinic:** Updating clinic block rules now reports how many locked
  placements were released.
- **Data compatibility:** Existing workspaces open unchanged with no
  migration. Workspaces that use waivers or unallocated-funded overrides
  cannot be opened by older builds.

### Fixed

- **Rotations:** Adding a training year to a standalone elective after it
  was created now extends that elective's eligible years instead of leaving
  it on the previous set.

## [0.1.3] - 2026-09-06

### Added

- **Rotations:** Elective time is authored directly from Shared elective
  properties: each training level's Elective block lengths and block counts are
  set against that level's unscheduled-week budget, which an over-allocation
  blocks until it is corrected. Elective blocks previously appeared only as a
  side effect of adding a Mandatory requirement or enabling an elective option.

### Changed

- **Rotations:** Shared elective properties and the FMED/Inpatient card report
  their block schedule color instead of offering a palette inline; the color is
  chosen in the same pop-out editor as the rest of those rules. Changing only a
  color still leaves a solved schedule in place.

## [0.1.2] - 2026-09-05

### Added

- **Scheduling:** Programs with no protected teaching time can turn the recurring
  academic half-day off entirely, and individual weeks can cancel theirs to
  record conference or holiday weeks that displace teaching.
- **Scheduling:** Training levels show how many of their weeks are allocated and
  how many are still unscheduled, and solving stays blocked with a per-level
  message until every enrolled level's weeks are allocated.

### Changed

- **Clinic:** Allocation targets are stored as whole-number percents (0–100);
  fractions of a percent are rejected and existing files convert automatically.
- **Rotations:** New requirements spend each training level's unscheduled weeks
  first, drawing on Elective time only for the remainder, with live budget
  feedback in the editor.
- **Clinic:** Every Clinic Block week grid now starts on Monday, matching the
  capacity grid and schedule views.
- **Scheduling:** Curricula may be partially built while editing; only
  over-allocating past the calendar length is rejected.

### Fixed

- **Workspaces:** Renaming no longer fails with a revision conflict after
  another save lands first, and repeat submits are harmless no-ops.
- **Residents:** The New resident form focuses the Full name field on open.
- **Clinic:** "Add closure day" opens the clinic editor on the Exceptions tab.

## [0.1.1] - 2026-09-05

### Changed

- **Desktop:** Opening the macOS disk image now shows a drag-to-Applications installer window, with the application on the left and Applications on the right.
- **Rotations:** New workspaces now include default FMED/Inpatient and Clinic block rotations, which cannot be added from the Mandatory editor.
- **Application:** Added a variety of early usability enhancements.

## [0.1.0] - 2026-09-03

### Added

- **Scheduling:** Build full-year residency block schedules with configurable
  rotations, curricula, clinic rules, electives, vacations, locks, and academic
  events.
- **Solver:** Run CP-SAT solves through a versioned standalone process boundary,
  retain stable assignments where possible, and report actionable diagnostics
  when a schedule is infeasible.
- **Desktop:** Edit durable `.rbsc` workspace documents with save-state tracking,
  crash recovery, application settings, and clinic or resident report exports.
- **Cloud:** Run the shared workspace UI for multiple proxy-authenticated users
  with per-user data isolation, retention controls, and bounded solve capacity.
- **Release notes:** Read the product changelog from the About dialog in local,
  desktop, and hosted interfaces.
- **Releases:** Download a macOS disk image built from the tagged commit, with
  that release's changelog section as its published description.

[Unreleased]: https://github.com/oestej/rbs/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/oestej/rbs/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/oestej/rbs/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/oestej/rbs/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/oestej/rbs/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/oestej/rbs/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/oestej/rbs/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/oestej/rbs/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/oestej/rbs/releases/tag/v0.1.0
