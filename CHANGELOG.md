# Changelog

## [Unreleased]

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
- **Desktop:** Install a release signed with an Apple Developer ID and notarized
  by Apple, so macOS opens it without the warning an unidentified application
  gets. Builds produced before the signing credentials were configured still
  need the one-time right-click and **Open**.
