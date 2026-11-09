---
name: Siyab
description: Sourced investigation views with restrained operational chrome.
colors:
  ground: "rgb(11 15 23)"
  ground-alt: "rgb(15 21 32)"
  panel: "rgb(20 28 42)"
  ink: "rgb(242 246 250)"
  secondary: "rgb(166 180 199)"
  hairline: "rgb(38 49 66)"
  hairline-strong: "rgb(65 80 104)"
  edge: "rgb(121 139 164)"
  accent: "rgb(159 184 255)"
  accent-fill: "rgb(56 79 167)"
  focus: "rgb(159 184 255)"
  allow: "rgb(66 164 127)"
  allow-text: "rgb(125 218 179)"
  block: "rgb(194 68 88)"
  block-text: "rgb(255 157 170)"
  unknown: "rgb(236 201 128)"
  light-ground: "rgb(245 247 251)"
  light-ground-alt: "rgb(235 240 247)"
  light-panel: "rgb(255 255 255)"
  light-ink: "rgb(23 33 49)"
  light-secondary: "rgb(76 93 116)"
  light-hairline: "rgb(217 225 237)"
  light-hairline-strong: "rgb(167 182 203)"
  light-edge: "rgb(109 127 151)"
  light-accent: "rgb(42 68 147)"
  light-accent-fill: "rgb(56 79 167)"
  light-focus: "rgb(42 68 147)"
  light-allow: "rgb(33 131 92)"
  light-allow-text: "rgb(20 108 73)"
  light-block: "rgb(180 50 73)"
  light-block-text: "rgb(155 35 59)"
  light-unknown: "rgb(123 82 13)"
  primary-text: "white"
typography:
  headline:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "30px"
    fontWeight: 600
    lineHeight: 1.22
  title:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.35
  body:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
  navigation:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  brand:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "19px"
    fontWeight: 600
  workspace-heading:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "25px"
    fontWeight: 600
  body-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "15px"
    fontWeight: 400
  workspace-heading-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "23px"
    fontWeight: 600
  brand-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  retained-hero-heading-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "22px"
    fontWeight: 600
  page-heading-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "26px"
    fontWeight: 600
  input-mobile:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "16px"
    fontWeight: 400
  profile-heading:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "27px"
    fontWeight: 600
  source-note:
    fontFamily: "Public Sans, Arial, Helvetica, sans-serif"
    fontSize: "11px"
    fontWeight: 400
rounded:
  chip: "3px"
  surface: "6px"
  popover: "7px"
  control: "5px"
  panel: "8px"
  dialog: "10px"
spacing:
  control-block: "7px"
  control-inline: "12px"
  panel-inset: "18px"
  page-inline: "28px"
  phone-inline: "14px"
components:
  button-primary:
    backgroundColor: "{colors.accent-fill}"
    textColor: "{colors.primary-text}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  search-field:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 11px"
---
# Design System: Siyab

## Overview

**Creative North Star: "The evidence workspace"**

Deep navy chrome and cool layered surfaces hold precise sans text, tabular values and restrained blue selection. Written states and adjacent source detail carry meaning; color reinforces them. The light theme preserves the same hierarchy with pale surfaces and darker text.

The system uses density to support inspection rather than display a decorative dashboard. Source-backed views remain explicit about their bounds. The approved visual authority is recorded in the surface brief; this document captures the implemented system, not a new direction.

**Key Characteristics:**
- Compact operational controls and consistent thin boundaries.
- Readable identifiers, numeric columns and source panels.
- Bounded selection motion with a reduced-motion alternative.

## Colors

The palette uses a cool primary selection color, layered neutral surfaces and distinct written semantic states. Frontmatter values are extracted from `intel/ui/site.css`; its light-prefixed entries correspond to the light-theme overrides.

### Primary

The accent marks links, selection, graph focus and count bars. The deeper accent fill supports primary controls with white text, including hover. A focus outline remains visible in both themes.

### Neutral

Ground, alternate ground and panel establish depth; ink and secondary text establish reading hierarchy. Hairline, stronger hairline and edge separate surfaces and identify actionable boundaries. Green, red and amber reinforce written outcomes, rather than substituting for them.

## Typography

Public Sans is self-hosted in regular and semibold WOFF2 files with its license. The same family carries headings, labels and body copy. System monospace renders identifiers and tabular numeric columns. Body leading remains generous relative to the compact controls; prose is capped at 85 characters.

The workspace heading uses 25px on desktop and 23px on smaller screens. Body text becomes 15px below 760px; form inputs become 16px at that breakpoint. Numeric columns align right. No external runtime font service is used. Navigation, brand, workspace, source-note and responsive roles are recorded separately in the frontmatter. The retained mobile hero heading role describes existing stylesheet rules; the preserved original introduction now renders as prose and does not use that heading rule.

## Layout

The shared container stops at 1600px. Desktop inline padding is 28px, rising to 40px above 1700px; it becomes 18px below 760px and 14px below 480px. Analytics use two equal columns, stacking below 760px. The graph has a canvas and evidence inspector, with a 460px stage on desktop and a 330px canvas on smaller screens; the inspector stacks below it. Header navigation wraps below 980px and becomes sticky above 760px. Account assertion tables use fixed column allocation on desktop and stack into labelled cells below 900px.

Recorded evidence and source tables follow the working panels on the same page. Phone layouts wrap identifiers and retain the full evidence rather than clipping columns away.

## Elevation & Depth

Most depth comes from tonal surfaces and thin boundaries. The search dialog uses a darkened backdrop and a soft shadow; the graph view-control popover has a smaller shadow. Ordinary data cards remain flat. Focus outlines sit outside the component boundary.

## Shapes

Controls use compact curved corners. Data cards use the control radius, working panels use the panel radius, and the search dialog uses the dialog radius. Compact outcome chips use the chip radius; lookup surfaces and graph nodes use the surface radius, and the view-control popover uses the popover radius. Thin borders provide stable edges; rounding does not replace grouping or spacing.

## Components

### Buttons and inputs

Primary controls keep white text on the deeper accent fill through hover. Secondary controls use panel surfaces and a stronger boundary. All controls expose a two-pixel focus outline with a three-pixel offset. Phone buttons grow to at least 44px high. Inputs use the ground surface with a visible edge and subdued placeholder text.

### Navigation and search

The brand, Home, Trace, Feed, payment checks and verifier form a compact shared header. Global search uses a native dialog with keyboard navigation and focus restoration. Script-free record pages expose a normal search link. Theme choice persists when browser storage permits it.

### Data panels and graph

Panels use boundaries and internal padding to separate headings, values and evidence. Graph account and relationship selection updates an adjacent inspector; reciprocal and parallel relationships remain distinguishable. Pagination and native view controls keep the recorded dataset usable at small widths. Source and recording limits remain visible alongside graph counts.

### Analytics and feed

Distribution bars preserve the recorded direct-outbound denominator and separate unknown or subthreshold values. Histograms show transfer counts over recorded time bounds, with exact values available in native details and HTML tables. Feed updates queue for the reader, can be paused, and stop while the page is hidden. Added motion is bounded: search entry 160ms, inspector entry 180ms and selected-path animation 900ms; reduced-motion media rules remove movement.

## Do's and Don'ts

### Do:
- **Do** keep source detail and recorded coverage visible beside conclusions.
- **Do** use written semantic states alongside color.
- **Do** preserve keyboard focus, reduced motion and reader-controlled feed updates.

### Don't:
- **Don't** turn missing USD values into zero.
- **Don't** imply a filtered graph or truncated recording is complete.
- **Don't** add decorative neon or generic hero tiles.
