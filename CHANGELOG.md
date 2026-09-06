# Changelog

## [Unreleased]

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

[Unreleased]: https://github.com/oestej/rbs/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/oestej/rbs/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/oestej/rbs/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/oestej/rbs/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/oestej/rbs/releases/tag/v0.1.0
