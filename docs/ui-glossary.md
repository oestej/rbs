# UI glossary

Use these forms in visible application copy, notifications, and accessibility
descriptions.

| Concept | Preferred form | Avoid |
| --- | --- | --- |
| One-off time away | individual day(s) off | single day(s) off |
| Ranked elective choices | elective preferences | Elective Preference |
| Dates when a clinic is closed | closure days; Add closure day | Holidays/Closure Days; Add closure |
| Weekly half-day frequency | half-day(s) per week | half-day/week; half-day/wk |
| Compact training-level identifier | configured short code, such as `PGY1` or `SMF` | a hardcoded `PGY<n>` label |
| Descriptive training-level name | configured full name, such as `PGY 1` or `Sports Medicine Fellow` | a compact code in headings or prose |

Buttons and notifications use sentence case. Preserve uppercase only for proper
names and initialisms such as CSV and PDF. Icon-only controls need a specific
accessible name, and a tooltip when the action is not already obvious from
nearby text.

Full pages use one of the shared schedule-canvas, master/detail, or
configuration shells from `rbs.ui.page_shells`. Button hierarchy comes from
the variants in `rbs.ui.buttons`. Visual roles and implementation constraints
are documented in [the RBS visual system](visual-system.md).
