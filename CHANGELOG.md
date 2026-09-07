# Changelog

## [Unreleased]

### Fixed

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

[Unreleased]: https://github.com/oestej/rbs/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/oestej/rbs/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/oestej/rbs/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/oestej/rbs/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/oestej/rbs/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/oestej/rbs/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/oestej/rbs/releases/tag/v0.1.0
