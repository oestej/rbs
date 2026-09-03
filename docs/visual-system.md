# RBS visual system

This document is the implementation contract for visual consistency across the
browser, desktop, loading/reconnect, dialog, schedule, and printable-report
surfaces.

## Sources of truth

- `src/rbs/ui/static/tokens.css` owns screen typography, spacing, shape, color,
  elevation, state effects, and motion.
- `src/rbs/ui/visual_tokens.py` mirrors fixed colors needed by Python-rendered
  surfaces and PDF exports. A parity test keeps the CSS and Python values in sync.
- `src/rbs/ui/print_tokens.py` owns the smaller point-based type scale used by
  ReportLab. PDF-safe Helvetica is intentional and separate from screen type.
- `src/rbs/ui/buttons.py`, `src/rbs/ui/page_shells.py`, and the shared branded
  dialog classes in `app.css` define recurring component hierarchy.

Feature stylesheets consume tokens. They do not add interface hex colors,
font sizes, numeric font weights, corner radii, shadow recipes, or motion
durations. Workspace-assigned rotation and clinic colors remain data and are
passed through scoped custom properties.

## Typography

Roboto is the UI family supplied with NiceGUI. Monospace content uses the
platform monospace stack. Feature code chooses semantic classes rather than
Quasar's visual size utilities.

| Intent | Class | Size / line height | Weight |
| --- | --- | --- | --- |
| Product brand | `.rbs-type-brand` | 24 / 32 px | 700 |
| Page title | `.rbs-type-page-title` | 24 / 32 px | 500 |
| Dialog title | `.rbs-type-dialog-title` | 20 / 28 px | 600 |
| Section title | `.rbs-type-section-title` | 16 / 24 px | 600 |
| Control label | `.rbs-type-control-label` | 14 / 20 px | 600 |
| Body | `.rbs-type-body` | 14 / 20 px | 400 |
| Secondary | `.rbs-type-secondary` | 13 / 18 px | 400 |
| Caption | `.rbs-type-caption` | 12 / 16 px | 400 |
| Dense schedule | `.rbs-type-schedule` | 11 / 14 px | 600 |
| Micro label | `.rbs-type-micro` | 10 / 12 px | 700 |

The vector wordmark contains outlined letters, so it is independent of locally
installed fonts. It is artwork, not a substitute for a page title.

## Color

Color has three ownership layers:

1. Fixed interface neutrals and feedback colors come from `tokens.css`.
2. Workspace primary, secondary, and accent roles enter through NiceGUI's theme.
   Their foregrounds are computed to meet WCAG AA contrast for normal text.
3. Rotation and clinic colors belong to schedule data. Components expose those
   values through local custom properties and consume shared formulas for borders,
   tints, and selection rings.

Administrative time, academic time, vacation, conference, special events, and
closures use fixed state colors and distinct patterns. They do not borrow a
workspace palette slot, because their meaning must survive palette changes.

## Spacing, shape, and elevation

General layout follows a 4 px rhythm: 4, 8, 12, 16, 20, 24, 32, 40, and 48 px.
One- to three-pixel offsets are allowed for borders, optical alignment, and dense
schedule geometry. Shared chrome and dialogs use the scale directly.

The five radii are 4 px, 8 px, 12 px, 20 px for dialogs, and fully rounded.
The four elevation levels are low, raised, modal, and sticky. Selection rails,
focus/state outlines, and swatch rings are named effects rather than ad-hoc
shadows.

Motion is limited to none, fast (120 ms), normal (180 ms), and the shared spinner
duration. Reduced-motion preferences slow continuous spinners and remove the
loading-screen transition.

## Component hierarchy

- Primary buttons are filled and reserved for the main action in a context.
- Secondary buttons are outlined. Destructive buttons use the danger role.
- Icon-only actions use the shared dense icon-button variant and require an
  accessible name; a tooltip is used unless nearby text makes the action obvious.
- Page surfaces use the shared schedule-canvas, master/detail, or configuration
  shell.
- Branded dialogs use the shared wordmark, title hierarchy, close treatment,
  dialog radius, and modal elevation. Long-form viewers use the shared header,
  divider, scroll region, and content spacing.
- Status pills use fixed semantic variants. Badges which are merely metadata use
  the neutral badge role.

## Responsive behavior

The supported layout thresholds are compact (520 px), phone (600 px), tablet
(760 px), and desktop (1024 px). Media-query custom properties are not supported
reliably by browsers, so these four values remain literal CSS exceptions.

Desktop native file dialogs and operating-system menus deliberately retain the
host platform's styling. The web content inside the desktop shell uses this same
visual system as browser mode.

## Verification

`tests/ui/test_ui_assets.py` rejects new feature-level colors, type values,
numeric weights, radii, shadow recipes, motion timings, and legacy Quasar visual
utility classes. It also checks CSS/Python color parity and deterministic vector
branding. UI behavior tests and PDF tests cover their respective rendered
surfaces; visual review should include empty and loaded workspaces, all shared
dialog types, loading/reconnect states, narrow widths, and both schedule views.
