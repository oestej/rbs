# Changelog

## [Unreleased]

### Added

- **Scheduling:** Solving now schedules attendings. Each attending's open
  half-days are filled from their weekly half-day total, Fixed and Flexible
  category ranges, Attending Clinic day minimum, and preferred weekly schedule,
  around hand-entered work and the academic half-day's Admin Time. Every
  attending-managed clinic gets the Precepting Clinic coverage its residents
  need, is open whenever residents with a target share there are in clinic, and
  keeps its allocation targets. Attendings are never booked to precept without
  residents unless a Fixed range requires it. On the next solve, locked
  attending work and work through today (when that option is on) is kept, and
  other work stays put where the rules still allow it.
- **Scheduling:** Failed solves now explain each blocking conflict with ways to resolve it, both in the workspace dialog and in `rbs schedule` output. When no single conflict can be isolated, the report says so in plain language instead of repeating internal solver notes.
- **Clinic schedule:** Switch between Residents and Attendings in the calendar.
  The attending view shows effective week-by-week work with category labels;
  site and date filters and CSV/PDF exports follow the selected view.
- **Attendings:** A new Attendings tab starts each attending with a lightweight
  form for their name, weekly half-day total, and optional schedule dates. The
  full editor separates details, weekly category targets, preferences, and
  vacation into tabs. Each recurring work category can have no target, a
  Fixed required minimum/maximum range, or a Flexible preferred minimum/maximum
  range. Special/Other work is placed manually and can include a free-text
  description. The weekly total defaults to 10, missing schedule dates are shown
  distinctly and use the academic year's first or last day, and unsaved
  changes are protected when leaving the editor. Every academic week has an
  independently editable AM/PM schedule whose blocks can be clicked to edit or
  dragged to move and swap. An attending with a zero-half-day default can be
  assigned work in a particular week and then use Override to make the current
  assignment count that week's total. Schedule shifts can
  be assigned as Inpatient Service, Attending Clinic, Precepting Clinic, Admin
  Time, or Special/Other; Precepting Clinic work selects a clinic site.
  Attendings can also set a soft preferred weekly schedule and a minimum number
  of distinct Attending Clinic days for non-vacation weeks. The program's
  effective academic half-day is reserved automatically as Admin Time for
  active attendings, with an option to disable that rule in Academic settings.
  Schedule checks identify missing weekly totals, Fixed ranges, and Attending
  Clinic day minimums as errors while showing Flexible ranges and preferred
  placements as warnings; vacation and partial boundary weeks may fall below
  their usual minimums.
- **Clinic staffing:** Each clinic can remain Capacity-managed or switch to
  Attending-managed staffing. An attending-managed clinic's resident capacity
  follows the attendings precepting there, after schedule dates, vacation, and
  closures, times its capacity per attending. Its weekly schedule sets Max
  attendings for each half-day, and exceptions can change that maximum or stop
  precepting on specific dates. The solve never schedules more preceptors than
  a clinic's maximum, and scheduled precepting at a capacity-managed clinic
  stays within that half-day's attendings.
- **Attendings:** Schedule edits can put more attendings at a clinic half-day
  than the clinic allows. The edit is saved as a manual override with a message
  explaining the limit, whether the next solve will keep the work, and how to
  allow it with a clinic exception, and the half-day stays marked in the
  calendar. Solving keeps hand-entered and locked overrides and adds no
  preceptors beside them.

### Changed

- **Sample data:** The sample workspace has eight attendings, each precepting
  three half-days a week when the sample clinics run, so solving it shows
  attending scheduling.
- **Attendings:** The full-year schedule and manual editor now match the
  resident schedule's spacing and controls, with clearer work labels and locks,
  a tidier edit toolbar, and dated half-day dialogs that work at narrow widths.
- **Attendings:** Changing an attending's weekly total, category ranges,
  preferences, vacation, schedule dates, or week-by-week work now marks the
  current schedule out of date, because the next solve schedules attendings from
  them. Renaming an attending does not.
- **Scheduling:** Assigning clinic sites after a solve takes about half as long
  on a full academic year.
- **Attendings:** Lock individual work half-days or use Lock all work and Unlock
  all work in Edit schedule. Locks protect placements from edits and future
  solves, and automatic locking through today also covers attending work.
- **Attendings:** Selecting an attending now shows their full academic-year AM/PM
  schedule, including vacation, schedule dates, and automatic Admin Time.
- **Attendings:** Edit schedule now works directly in the full-year calendar,
  like resident schedules. Add, change, remove, or drag work half-days with
  changes saved immediately, then select Return to view when finished. Edit
  attending now focuses on details, targets, preferences, and vacation.
- **Clinic schedule:** Switching between Residents and Attendings is faster,
  preserves each view's scroll position, and uses less HTML for hover details.
- **Data compatibility:** Version 11 `.rbsc` files migrate dated attending work
  into separate, week-specific attending schedules and convert single category
  targets into exact minimum/maximum ranges. Versions 9 and 10 `.rbsc` files
  migrate with an empty attending directory. Version 8 and 9 catalogs migrate
  with capacity-managed staffing and automatic attending Admin Time enabled
  for the academic half-day. Files saved by this version use `.rbsc` schema
  12 and catalog schema 10 and cannot be opened by older builds. Solved
  schedules now include the attending work the solve placed, and solver
  protocol 7 requires matching solver binaries.

### Fixed

- **Clinic schedule:** Attending-managed clinic coverage and resident hover
  details now include preceptors placed by the solve, alongside hand-entered
  work, so displayed capacity matches the schedule's staffing. Locking
  hand-entered precepting work also preserves its placement.
- **Scheduling:** An attending-managed clinic that no attending can precept at
  no longer ends in an unexplained infeasible result. Solve readiness now names
  the rotations that cannot be placed and links to the clinic configuration,
  before any search starts.

## [0.1.14] - 2026-09-29

### Changed

- Rotations and Configuration show the selected section first, then prepare
  unopened sections while you are idle without replacing open editors.
- **Scheduling:** Preparing a solve is faster and uses a smaller model when
  quality goals are disabled, leaving more of the solve budget for scheduling.
- Main pages open faster, and large block schedules resize more smoothly while
  keeping rotation names readable.

### Added

- **Clinic schedule:** Right-click a resident’s clinic block to open their clinic
  schedule at the selected week. Hovering still shows the details card.
- **Block schedule:** Right-click a block to jump to its resident’s schedule or
  rotation configuration.
- **Clinic schedule:** Hover over or keyboard-focus a resident’s name to see their
  rotation, clinic rules, availability restrictions, and manual override details.

## [0.1.13] - 2026-09-19

### Changed

- **Desktop:** Clinic staffing now uses capacity points. Set Capacity per attending
  in the Clinic editor and Capacity per resident for each training level in Clinic
  block rules (default 1). These points apply to clinic attendance on every rotation,
  including allocation, capacity checks, and attending totals.
- **Data compatibility:** Documents and catalogs now use schemas 10 and 9;
  preceding schemas migrate with capacity defaulting to 1. Older builds cannot
  read newly saved files. Solver protocol 6 requires matching solver binaries;
  schedule output and settings formats are unchanged.

## [0.1.12] - 2026-09-18

### Security

- **Desktop:** Imported names remain literal text in action buttons, spreadsheet
  exports protect against embedded formulas, and malformed local access tokens
  are rejected without causing request errors. Saved documents are unchanged.

### Added

- **Rotations:** The Rotations screen now has an Export CSV button that
  downloads every rotation and its configured parameters as a single
  spreadsheet-friendly CSV file.
- **Configuration:** A new Use placeholder electives option, off by default,
  solves every elective slot as a generic gray Placeholder block with no
  clinic hours instead of matching elective preferences or backfilling with
  Clinic.

### Fixed

- **Desktop:** Editing solved schedules and checking save status do less repeated
  work. Clinic sections and edit menus load when opened, switching residents
  retains the directory search, and multi-attempt solves share model preparation
  to leave more of the time budget for finding a schedule. Saved file formats
  and scheduling priorities are unchanged.
- **Residents:** Ranking elective preferences is about an order of magnitude
  faster, and no longer slows down as the list grows. A resident's block and
  clinic schedule reports are now drawn when their tab is opened, and saving
  an edit redraws only the resident being edited instead of the whole tab —
  which also speeds up block, clinic, and lock edits, the more so the larger
  the program.
- **Scheduling:** Solve readiness now identifies when required rotation blocks
  need more resident-weeks than an overall or training-level maximum can hold,
  including resident-specific additions and waivers, instead of starting a
  solve that can only return infeasible.
- **Academic year:** Moving to a new academic year now clears resident rotation
  exceptions and overrides along with the block schedule and other
  year-specific entries.

## [0.1.11] - 2026-09-12

### Fixed

- **Schedules:** A locked clinic session from a previous draft is now kept as
  a one-off extra session when later edits leave it outside the rotation's
  clinic days, with a warning naming the session, instead of failing the
  solve as infeasible. A lock that genuinely cannot be kept — time off, the
  academic half-day, a vacation week, or an Away block — now explains exactly
  which rule blocks it instead of failing unexplained.
- **Editing:** Removing a manual lock is never refused because of unrelated
  workspace state. Loosening a lock always applies; other configuration the
  current rules reject is left for its own editor to repair.

## [0.1.10] - 2026-09-08

### Fixed

- **Academic year:** Moving a workspace to another academic year now asks for
  confirmation, carries reusable setup forward, and starts without schedules,
  time away, dated calendar entries, or manual placements from the prior year.
- **Clinic schedule:** CSV exports now use a native save dialog in RBS Desktop
  and write to the selected file.
- **Editing:** A form opened before another edit now reloads the current
  workspace instead of overwriting newer work.
- **Schedules:** Name, label, and color-only edits no longer mark the current
  schedule as out of date.
- **Rotations:** Standalone electives can now be changed between once per
  resident and repeatable when they are created or edited.

## [0.1.9] - 2026-09-08

### Fixed

- **Residents:** Manually adding a block of a rotation a resident takes more
  than once (such as FMED) stays available until every required block is on
  their schedule, instead of being blocked after the first one.

## [0.1.8] - 2026-09-08

### Changed

- **Block schedule:** The full academic-year schedule can now be exported as a
  landscape PDF, split between complete four-week blocks with resident and date
  headings repeated on every page.
- **Files:** Only the current file format opens. Older `.rbsc` files are
  rejected instead of being upgraded in place.
- **Clinic:** Scheduling a fixed Clinic block for a resident now places it
  on their block schedule immediately, with a solve still required
  afterwards. Removing the block clears that placement again.
- **Rotations:** The roomier shared elective properties editor separates
  general settings, training-level rules, and resident overrides into tabs.
  Training levels collapse for easier scanning, resident exceptions use the
  same focused dialogs and summary rows as other rule editors, and Save stays
  beside Close. Resident overrides can add an Elective Slot, funded by
  unallocated time or a direct elective block, or waive a direct elective
  block. The solver places Elective Slots like other elective blocks.
- **Scheduling:** A failed solve now names locked blocks that exceed a
  rotation's maximum or repeat a non-repeatable elective, instead of
  returning with no explanation.
- **Residents:** Identical duplicate locks are cleaned up automatically when
  a file opens, and saving the same block twice no longer creates another
  copy.
- **Clinic:** A closure day outside the academic year is rejected with an
  explanation instead of being saved.

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

[Unreleased]: https://github.com/oestej/rbs/compare/v0.1.14...HEAD
[0.1.14]: https://github.com/oestej/rbs/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/oestej/rbs/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/oestej/rbs/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/oestej/rbs/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/oestej/rbs/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/oestej/rbs/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/oestej/rbs/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/oestej/rbs/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/oestej/rbs/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/oestej/rbs/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/oestej/rbs/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/oestej/rbs/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/oestej/rbs/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/oestej/rbs/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/oestej/rbs/releases/tag/v0.1.0
