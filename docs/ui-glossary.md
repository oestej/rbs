# UI glossary

Use these forms in visible application copy, notifications, and accessibility
descriptions.

| Concept | Preferred form | Avoid |
| --- | --- | --- |
| One-off time away | individual day(s) off | single day(s) off |
| Attending working period | schedule dates; schedule start date; schedule end date | employment dates; contract dates |
| Missing attending schedule boundary | academic year start; academic year end; Full academic year | unlimited; blank |
| Attending time away | vacation; vacation range(s) | vacation weeks |
| Actual attending work by academic week | week-by-week schedule; academic week; configured week(s) | recurring schedule; default schedule |
| Reusable attending pattern applier | schedule template; Apply template | base schedule; live template |
| Dated attending work replacing one scheduled slot | ad hoc work; ad hoc half-day(s) | special availability; extra shift(s) |
| Attending half-day categories | Inpatient Service; Attending Clinic; Precepting Clinic; Admin Time; Special/Other | generic clinic time |
| Attending category shift goal | weekly category target; shifts per week; Fixed; Flexible; No target | quota; limit |
| Source of resident clinic capacity | Capacity-managed; Attending-managed | automatic capacity; manual capacity |
| Ranked elective choices | elective preferences | Elective Preference |
| Dates when a clinic is closed | closure days; Add closure day | Holidays/Closure Days; Add closure |
| Weekly half-day frequency | half-day(s) per week | half-day/week; half-day/wk |
| Resident-specific named elective placement | Elective Slot(s) | extra elective; elective take |
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
