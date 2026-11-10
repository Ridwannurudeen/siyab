---
name: "Siyab"
description: "A compact teal analytics workspace with evidence beside every investigation."
colors:
  ground: "rgb(6 16 26)"
  ground-alt: "rgb(12 23 34)"
  panel: "rgb(18 28 38)"
  ink: "rgb(242 244 245)"
  secondary: "rgb(148 159 168)"
  hairline: "rgb(43 53 64)"
  hairline-strong: "rgb(99 119 137)"
  edge: "rgb(121 142 159)"
  accent: "rgb(0 255 167)"
  accent-fill: "rgb(0 255 167)"
  primary-ink: "rgb(6 16 26)"
  focus: "rgb(0 255 167)"
  allow: "rgb(52 207 130)"
  allow-text: "rgb(100 220 157)"
  block: "rgb(255 127 123)"
  block-text: "rgb(255 127 123)"
  unknown: "rgb(236 201 128)"
  light-ground: "rgb(244 249 247)"
  light-ground-alt: "rgb(234 243 239)"
  light-panel: "rgb(255 255 255)"
  light-ink: "rgb(6 16 26)"
  light-secondary: "rgb(69 91 82)"
  light-hairline: "rgb(210 224 218)"
  light-hairline-strong: "rgb(105 129 117)"
  light-edge: "rgb(91 116 103)"
  light-accent: "rgb(0 117 78)"
  light-accent-fill: "rgb(0 255 167)"
  light-primary-ink: "rgb(6 16 26)"
  light-focus: "rgb(0 117 78)"
  light-allow: "rgb(33 131 92)"
  light-allow-text: "rgb(20 108 73)"
  light-block: "rgb(180 50 73)"
  light-block-text: "rgb(155 35 59)"
  light-unknown: "rgb(123 82 13)"
typography:
  headline:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: "28px"
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.01em"
  panel-title:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0.15px"
  label:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
  navigation:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "22px"
  brand:
    fontFamily: "Manrope, Inter, Arial, Helvetica, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    letterSpacing: "-0.035em"
  numeric:
    fontFamily: "IBM Plex Mono, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0.15px"
  identifier:
    fontFamily: "IBM Plex Mono, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    letterSpacing: "0.15px"
  body-mobile:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.6
  input-mobile:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "16px"
    fontWeight: 400
  workspace-heading-mobile:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: "28px"
    letterSpacing: "-0.01em"
  brand-mobile:
    fontFamily: "Manrope, Inter, Arial, Helvetica, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.035em"
  brand-phone:
    fontFamily: "Manrope, Inter, Arial, Helvetica, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    letterSpacing: "-0.035em"
  source-note:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.6
  graph-label:
    fontFamily: "IBM Plex Mono, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 400
    letterSpacing: "0.15px"
  graph-detail:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    letterSpacing: "0.15px"
  inspector-data:
    fontFamily: "IBM Plex Mono, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0.15px"
  inline-value:
    fontFamily: "Inter, Arial, Helvetica, sans-serif"
    fontSize: "19px"
    fontWeight: 600
rounded:
  bar: "2px"
  chip: "3px"
  summary: "4px"
  control: "5px"
  surface: "6px"
  popover: "7px"
  panel: "8px"
  dialog: "10px"
spacing:
  control-block: "7px"
  control-inline: "12px"
  panel-inset: "16px"
  panel-gap: "16px"
  page-inline: "20px"
  page-wide-inline: "32px"
  mobile-inline: "16px"
  phone-inline: "14px"
components:
  button-primary:
    backgroundColor: "{colors.accent-fill}"
    textColor: "{colors.primary-ink}"
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
  sidebar:
    backgroundColor: "{colors.ground}"
    width: "248px"
  topbar:
    backgroundColor: "{colors.ground}"
    height: "64px"
  data-panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "16px"
---
# Design System: Siyab

## Overview

**Creative North Star: "The evidence workspace"**

Dark navy ground and charcoal panels carry bright teal actions and selection. Manrope gives only the Siyab wordmark its geometric character. Inter provides compact interface hierarchy; IBM Plex Mono separates identifiers and numeric data. A persistent desktop rail and a compact context header frame the work without displacing the evidence.

The light theme keeps the same structure with pale green-neutral surfaces and darker teal links. Written states, source detail and recording limits carry meaning alongside color. This document records the built visual system in intel/ui/site.css; it does not certify that every page or interaction has passed final review.

**Key Characteristics:**
- Persistent desktop navigation with a compact context header.
- Aligned chart and table panels with readable source detail.
- Teal actions, written semantic states and preserved bounded motion.

## Colors

A bright teal action color sits against dark navy ground and charcoal panels. Frontmatter values are extracted from the current stylesheet; light-prefixed entries correspond to its light-theme overrides. There are no synthesized tonal ramps.

### Primary

Teal marks links, navigation selection, graph focus and count bars. Primary controls retain the bright accent fill in both themes and use the dark primary ink for their text, including hover. Dark-theme secondary text remains distinct from primary content; light-theme links and focus use the darker teal accent.

### Neutral

Ground frames the application, panel holds working content, and alternate ground separates inspectors and secondary areas. Hairline is the quiet structural divider; the stronger hairline and edge tokens identify actionable boundaries. Green, coral and amber reinforce written ALLOW, DENY and ASK/unknown states. They are not substitutes for outcome text.

## Typography

**Wordmark Font:** Manrope, with the interface stack as fallback.
**Interface Font:** Inter, Arial, Helvetica, sans-serif.
**Numeric and Identifier Font:** IBM Plex Mono, Consolas, monospace.

The three families have separate jobs: Manrope appears only in the brand, Inter carries interface text, and IBM Plex Mono carries data and identifiers. This keeps the geometric display treatment out of dense tables and controls. Manrope is an independently licensed open font, not a reproduction of the reference's proprietary face.

All three families are self-hosted. Manrope has a semibold file; Inter has regular and semibold files; IBM Plex Mono has regular and medium files. Font licenses and provenance live with the assets in intel/ui/fonts. Font display is swap; synthetic font styling is disabled.

Page, workspace and profile headings are 20px semibold with a 28px line height and -0.01em tracking, equivalent to -0.2px at that size. Ordinary section headings are 18px with -0.01em tracking; panel headings are 16px. Body text is 14px with 1.6 leading and +0.15px tracking; prose is capped at 85ch. Navigation remains 14px with a 22px line height. Tabular numeric columns use 12px regular monospace, an 18px line height, +0.15px tracking and right alignment. Generic identifiers retain 12px regular monospace with inherited leading; inspector identifiers and counts explicitly use the same 12px/18px regular data role. Inline provenance totals retain their 19px semibold interface role.

The Siyab wordmark uses 24px Manrope semibold with -0.035em tracking. At 760px and below it becomes 20px, then 18px at 480px and below. Those brand sizes do not change the interface font. Body text becomes 15px and form inputs 16px at 760px and below; the workspace heading stays 20px/28px. Stacked account cells use 14px values and 12px labels. Small source notes retain their observed 11px role; do not substitute that size for ordinary body text.

Graph account labels use 14px regular IBM Plex Mono; graph detail lines use 13px regular Inter. Inspector account IDs and numeric counts use 12px/18px regular IBM Plex Mono with +0.15px tracking. These roles distinguish readable identifiers and figures from surrounding interface descriptions without introducing another display face.

## Layout

At desktop widths the fixed left rail is 248px and the sticky topbar is 64px. Content and footer share the rail offset and 20px inline padding, with no previous 1600px page cap. At 1700px and above the inline padding becomes 32px. The rail has 12px side padding, 36px minimum navigation rows and 4px gaps; the topbar carries route context, search and theme.

At 1099px and below the rail disappears, its offset becomes zero and native mobile navigation becomes available. At 760px and below the context label is hidden, header search uses its short label, and inline padding becomes 16px; at 480px and below it becomes 14px. The mobile navigation panel opens beneath the 64px topbar and scrolls within the remaining viewport.

Analytics use two equal columns with 16px gaps and stack below 761px. The recorded flow distribution/activity pair stacks earlier, at 1399px and below, to retain readable evidence columns. Working panel headings use 14px by 16px padding; body and chart insets are 16px. Retained proof, report and tool sections keep their existing content and responsive layouts.

The graph uses a flexible canvas and a 300px inspector at 1280px and above, with a 460px stage. Between 761px and 1279px the inspector stacks underneath with a 340px scrollable height. At 760px and below the canvas is 330px and the inspector has natural height. Phone graph layout retains readable account labels rather than shrinking the entire desktop graph. Account assertion and hypothesis tables use explicit desktop column allocation; at 900px and below their rows become labelled records, with long note/source cells spanning the row.

Distribution rows use four columns: a flexible counterparty, 128px USD, 56px row count and 64px share, with 8px column gaps and a 4px bar spanning the row. At 760px and below the counterparty spans the width above three equal metric cells; each metric has its own written label. The transfer timeline reserves 32px for its count axis, an 8px gap and a 180px chart height. Count and UTC labels are HTML outside the SVG, whose viewBox height is 100; the plotted geometry scales independently of the text.

In script-free record content, record-path values wrap anywhere, definition descriptions have a 16px inline-start margin and ordered lists have 20px left padding. Nested definition lists retain 8px top and 12px bottom margins. These rules are scoped to record content rather than changing every definition list.

Reflow without page-wide horizontal scrolling is a required acceptance condition, not a claim that this document resolves open page defects. Preserve script-free evidence navigation and full data when changing layout.

## Elevation & Depth

Panels are flat at rest; tonal surfaces and thin boundaries establish hierarchy. Shadows are reserved for overlays: search uses 0 24px 80px rgb(0 0 0/.35), graph view controls use 0 12px 30px rgb(0 0 0/.25), and mobile navigation uses 0 16px 32px rgb(0 0 0/.25). The search backdrop is rgb(2 5 12/.72). Focus uses a two-pixel accent outline with a three-pixel offset.

## Shapes

Controls and ordinary data cards have 5px corners; navigation rows, lookup surfaces and graph nodes use 6px; graph view-control popovers use 7px; working panels and header search use 8px; search dialogs use 10px. Outcome chips use 3px corners and numeric bar tracks 2px. The rail/topbar are structural planes, not floating cards.

## Components

### Buttons and inputs

Primary buttons combine bright teal fill and dark primary ink. Secondary buttons use the panel, primary text and a stronger boundary; hover switches their surface to alternate ground and boundary to edge. Controls have 36px minimum height and 7px by 12px padding, with 140ms background/border transitions. Disabled buttons use .5 opacity and a not-allowed cursor. Phone buttons and header controls reach 44px. Inputs use the ground surface, edge border, 5px corners and 40px minimum height; the workspace search has 46px controls.

### Navigation and search

Desktop navigation is an aligned vertical list. Its active row uses ten-percent accent fill, accent text and semibold weight; hover uses the panel surface. The compact topbar remains the search/theme context. Mobile navigation is native details/summary; records remain script-free with normal search links. Global search uses a native dialog with keyboard navigation and focus restoration. Theme persistence remains conditional on available browser storage.

### Data panels, tables and graphs

Panels have charcoal surfaces, 8px corners, 16px body padding and thin structural borders. Table headers recede while values and source quotations remain readable. Graph nodes use written identifiers and depth, with selected paths and nodes in teal; dashed relationships retain attribution-hypothesis meaning. Full evidence, filtered display counts and acquisition limits remain distinct. Numeric values do not become fictional portfolio or loss measures.

Standalone graph pages place the page heading and subject context before the canvas. Retained section labels follow their headings as ordinary muted prose; they do not form a separate eyebrow style.

Direct outbound distribution keeps the full recorded dataset separate from graph filters. A compact identifier may represent a 64-character account in the row, while native Full identifier disclosure retains the complete linked or quoted value without JavaScript. Its phone summary has a 44px minimum height. USD, row count and share remain written values; the bar supplements them. Compact header and phone metric labels reuse the 11px interface role, identifiers use 12px monospace, and values use the existing numeric role.

Recorded transfer activity exposes count ticks and three UTC time labels alongside the histogram. Native disclosure below it retains exact time bounds and transfer counts. Unknown USD and deeper-hop exclusions remain explicit; chart geometry does not replace the underlying records.

### Chips and status

Outcome chips use written states, compact monospace text, thin semantic borders and 3px corners. Feed freshness, pending events and unknown results remain explicit. Reader-controlled updates preserve the existing Pause control; unavailable data does not become a favorable result.

### Preserved motion

The four existing keyframes are recorded verbatim below. Their selector timings are search-enter 160ms ease-out both, inspector-enter 180ms ease-out 1, event-arrival 1100ms ease-out 1 and path-select 900ms ease-out 1. These are stylesheet definitions: inspector entry requires data-changed="true", and event arrival requires the feed-event-new class. Documentation does not assert that those hooks are currently attached by JavaScript.

```css
@keyframes search-enter{from{opacity:0;transform:translateY(6px) scale(.99)}to{opacity:1;transform:none}}
@keyframes inspector-enter{from{opacity:.65;transform:translateX(6px)}to{opacity:1;transform:none}}
@keyframes event-arrival{from{background:rgb(var(--accent)/.14)}to{background:transparent}}
@keyframes path-select{from{stroke-dasharray:4 4;stroke-dashoffset:32}to{stroke-dasharray:4 4;stroke-dashoffset:0}}
```

Reduced motion preserves the existing .01ms animation/transition override, one iteration and automatic scrolling. Do not shorten, remove or redesign any existing motion as part of a style update.

## Do's and Don'ts

### Do:

- Do keep source detail and recording coverage beside conclusions.
- Do distinguish selection from payment outcomes with written states.
- Do preserve every existing motion definition, duration, Pause control and reduced-motion alternative.
- Do retain full evidence and accessible textual alternatives when layouts reflow.

### Don't:

- Don't turn missing USD values into zero or imply a truncated recording is complete.
- Don't use white text on the bright teal primary fill.
- Don't add unsupported portfolios, trading controls or conversational AI claims.
- Don't treat a documented motion selector as proof that its runtime trigger is attached.
