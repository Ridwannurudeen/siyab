---
name: "Siyab"
description: "A dark evidence workspace with Manrope titles, uppercase mono labels, quiet chrome, compact tables and explicit uncertainty."
colors:
  ground: "#0A0D16"
  panel: "#10131C"
  ground-alt: "#171C27"
  hover: "#202735"
  ink: "#EDEDED"
  secondary: "#ABB2CA"
  muted: "#969EB3"
  link: "#79AEF2"
  accent: "#60D8BA"
  allow-text: "#6BC498"
  block-text: "#F9809A"
  unknown: "#E6C383"
  hairline: "#293140"
  hairline-strong: "#727D94"
  light-ground: "#F5F7FA"
  light-panel: "#FFFFFF"
  light-ground-alt: "#EDF1F6"
  light-hover: "#E4EAF2"
  light-ink: "#18212F"
  light-secondary: "#48566B"
  light-muted: "#56647A"
  light-link: "#245BA5"
  light-accent: "#006E59"
  light-allow-text: "#246B46"
  light-block-text: "#A82D49"
  light-unknown: "#765309"
  light-hairline: "#D3DBE6"
  light-hairline-strong: "#78849A"
typography:
  display:
    fontFamily: "Manrope"
    fontSize: "clamp(38px, 4.7vw, 68px)"
    fontWeight: 600
    lineHeight: "1.04"
    letterSpacing: "-0.035em"
    textWrap: "balance"
  panel-title-landing:
    fontFamily: "Manrope"
    fontSize: "32px"
    fontWeight: 600
    lineHeight: "38px"
    letterSpacing: "-0.025em"
  card-title-landing:
    fontFamily: "Manrope"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: "24px"
    letterSpacing: "-0.01em"
  page-title:
    fontFamily: "Manrope"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: "30px"
    letterSpacing: "-0.02em"
  incident-title:
    fontFamily: "Manrope"
    fontSize: "28px"
    fontWeight: 600
    lineHeight: "34px"
    letterSpacing: "-0.02em"
  panel-title:
    fontFamily: "Manrope"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: "22px"
    letterSpacing: "0"
  label:
    fontFamily: "IBM Plex Mono"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0.24px"
    textTransform: "uppercase"
  navigation:
    fontFamily: "Public Sans"
    fontSize: "13px"
    fontWeight: 300
    lineHeight: "18px"
    letterSpacing: "0"
  body:
    fontFamily: "Public Sans"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "21px"
    letterSpacing: "0"
  control:
    fontFamily: "Public Sans"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0"
  source-note:
    fontFamily: "Public Sans"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0"
  table-heading:
    fontFamily: "IBM Plex Mono"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0.24px"
  table-name:
    fontFamily: "Public Sans"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0"
  numeric:
    fontFamily: "IBM Plex Mono"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "18px"
    letterSpacing: "0"
  prominent-figure:
    fontFamily: "IBM Plex Mono"
    fontSize: "24px"
    fontWeight: 300
    lineHeight: "30px"
    letterSpacing: "0"
  primary-identifier:
    fontFamily: "IBM Plex Mono"
    fontSize: "22px"
    fontWeight: 400
    lineHeight: "28px"
    letterSpacing: "0"
  control-mobile:
    fontFamily: "Public Sans"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: "22px"
    letterSpacing: "0"
  input-mobile:
    fontFamily: "Public Sans"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: "24px"
    letterSpacing: "0"
  brand:
    fontFamily: "Manrope"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.035em"
rounded:
  bar: "2px"
  chip: "4px"
  command: "4px"
  panel: "6px"
  input: "6px"
  dialog: "12px"
spacing:
  panel-inset: "12px"
  panel-gap: "12px"
  section-separation: "16px"
  page-inline: "24px"
  tablet-inline: "16px"
  phone-inline: "12px"
  narrow-phone-inline: "10px"
components:
  app-header:
    height: "56px"
    navigationGap: "4px"
    collapseBelow: "1240px"
  menu-panel:
    minWidth: "280px"
    rounded: "{rounded.panel}"
    padding: "8px"
  data-panel:
    rounded: "{rounded.panel}"
    padding: "12px"
  button-secondary:
    minHeight: "32px"
    rounded: "{rounded.command}"
  search-field:
    minHeight: "40px"
    rounded: "{rounded.input}"
---
# Design System: Siyab

## Scope

Phases 1 and 2 apply typography, colour, theme policy and shared component dimensions. Phase 3 replaces the desktop sidebar and context topbar with a 56px application header and recomposes four reference pages: Explore (the recorded investigation workspace), recorded account pages, Issuer actions and incident pages. Other page families keep their phase 2 structure until phase 4. Factual text, links, images and controls are retained.

The interface is an evidence workspace. Manrope SemiBold carries the wordmark, landing headings, page titles and panel titles; Public Sans carries explanation and controls; IBM Plex Mono carries identifiers, timestamps, comparable values, table headings and labels. Teal identifies actions and selection, blue identifies navigable references, and written semantic states retain their own colours. Unknown values, recording limits, source dates and acquisition failures remain explicit.

## Colour and themes

| Role | Dark default | Light alternative |
|---|---|---|
| Canvas | #0A0D16 | #F5F7FA |
| Panel | #10131C | #FFFFFF |
| Alternate row / raised surface | #171C27 | #EDF1F6 |
| Hover / selected surface | #202735 | #E4EAF2 |
| Primary text | #EDEDED | #18212F |
| Secondary text | #ABB2CA | #48566B |
| Muted text | #969EB3 | #56647A |
| Link | #79AEF2 | #245BA5 |
| Teal action / selection | #60D8BA | #006E59 |
| ALLOW text | #6BC498 | #246B46 |
| DENY text | #F9809A | #A82D49 |
| Unknown / caution | #E6C383 | #765309 |
| Decorative separator | #293140 | #D3DBE6 |
| Necessary control boundary | #727D94 | #78849A |

Primary buttons use canvas-coloured ink on dark-theme teal and white ink on light-theme teal. Inputs, selects, text areas and the search field keep the stronger boundary token, because the boundary is what identifies them. Labelled buttons, tabs, chips, menu and disclosure buttons and pagination links use the decorative hairline on their surface fill, because their text label identifies them; hover and focus states are unchanged. Decorative panel separators do not establish an actionable boundary. Semantic chips retain their written state. A selected account does not acquire ALLOW styling.

A new visit opens dark regardless of system preference. An explicit browser-local light preference remains honoured, and the existing toggle persists explicit choices when storage is available. The theme head applies an explicit saved choice before styles load. Without a saved choice, dark CSS and dark browser metadata apply.

Records have zero script elements. Their native Theme disclosure contains Dark and Light radio controls, with Dark checked initially. CSS applies the selected theme to that document only; these controls do not read or write browser storage. Native focus and forced-colour support remain available. Print uses readable light surfaces.

Every small text role must meet 4.5:1 contrast on its actual background in either theme. Necessary control boundaries and meaningful graphical marks must meet 3:1. Check hover, selected, tinted and disabled states as rendered, rather than inferring contrast from the palette alone.

## Watermark

The S glyph from the header wordmark marks a few working surfaces as a faint, decorative background image: white at 6 percent opacity in dark and ink at 4 percent in light, supplied by the --mark theme token as an inline SVG data URI. It appears once per surface, centred and never repeated, about 30 percent of the surface width and at most 320px, and never taller than the surface, so a short chart holds a smaller whole glyph instead of a cropped one. Marked surfaces are the graph canvas (Explore, flow, trace and relationship graphs and the loaded account lookup neighbourhood) and the bar and timeline chart bodies of the analytics panels. The columnar distribution list reads as a table and stays unmarked. Empty and unavailable states (feed, workspace, account lookup, portfolio) stay unmarked for now: they are one to three lines tall, so a whole glyph there shrinks to an icon behind the sentence. Tables, records, the landing page and the header carry no mark. It adds no markup, script, motion or layout, and print removes it.

## Typography and font delivery

All fonts are local static weights with one font-face declaration per file, font-display:swap and font-synthesis:none. Public Sans supplies 200, 300, 400 and 500; IBM Plex Mono supplies 300, 400 and 500; Manrope supplies 600. Requested light weights use actual light files. Licences and measured font provenance are in intel/ui/fonts/PROVENANCE.json, including bytes, hashes, version, weight, glyph count and empty variable-axis metadata for these static files.

| Role | Family | Desktop size / leading | Weight | Tracking / case |
|---|---|---|---|---|
| Landing display | Manrope | clamp(38px, 4.7vw, 68px) / 1.04 | 600 | -0.035em; balanced wrap |
| Landing section title | Manrope | 32 / 38px | 600 | -0.025em |
| Landing card title | Manrope | 18 / 24px | 600 | -0.01em |
| Page title | Manrope | 24 / 30px | 600 | -0.02em; sentence case |
| Long incident title | Manrope | 28 / 34px | 600 | -0.02em; sentence case |
| Panel / section title | Manrope | 16 / 22px | 600 | 0 |
| Subsection heading (h4) / section disclosure summary | Manrope | 16 / 22px | 600 | 0; panel title role |
| Navigation | Public Sans | 13 / 18px | 300; active 500 | 0 |
| Buttons / filters | Public Sans | 13 / 18px | 400 | 0; sentence case |
| Explanation | Public Sans | 14 / 21px | 400 | 0 |
| Scope / source note | Public Sans | 12 / 18px | 400 | 0 |
| Table headings / tabs | IBM Plex Mono | 12 / 18px | 400 | +0.24px; uppercase presentation |
| Labels: stat, field, landing eyebrow, Account | IBM Plex Mono | 12 / 18px | 400 | +0.24px; uppercase presentation |
| Table names / descriptions | Public Sans | 13 / 18px | 400 | 0 |
| Numeric cells / timestamps | IBM Plex Mono | 12 / 18px | 400 | 0 |
| Existing prominent figures | IBM Plex Mono | 24 / 30px | 300 | 0 |
| Primary account identifier | IBM Plex Mono | 22 / 28px | 400 | 0; original case, no quote marks; also the Account lookup title while it holds an account |

Numeric text uses font-variant-numeric:lining-nums tabular-nums slashed-zero and font-feature-settings:"lnum" 1,"tnum" 1,"zero" 1. Identifiers keep original case, zero tracking and no discretionary ligatures. Account identifiers in the page head, account links and identifier cells drop the curly quote presentation; quoted source text keeps its marks. Labels are field labels that wrap an input, select or text area, stat and fact labels, landing eyebrows and the Account label above a primary identifier; checkbox and radio labels and record field names keep the text role. Weight 600 is only ever Manrope, so no title uses synthetic bold. Comparable numeric columns remain right aligned with their existing precision. Plex supplies equal-width digits intrinsically. Ordinary explanation keeps its existing 35rem measure; full identifiers and evidence remain allowed to wrap.

At 390 and 320px, explanation is 14/22px, controls 15/22px, inputs 16/24px, primary record values 14/20px, secondary data 12/18px and page titles 22/28px. Small navigation uses 400. No meaningful text is smaller than 12px. The wordmark retains its existing 18px narrow-phone treatment.

## Shared components

Panels are flat with 6px corners, a 1px decorative boundary and a 12px inset. Panel headers have a 40px minimum height, 12px horizontal inset and wrapping content. Related panel gaps are 12px and section separation is 16px. Existing grids, column allocation and stacking breakpoints remain in place.

Ordinary desktop table rows are at least 40px tall, with 18px leading and 5px vertical / 10px horizontal cell padding. Multiline evidence grows naturally without clipping. Header rows are at least 32px with muted mono labels. Alternate rows use the alternate-row surface, and hover uses the hover surface. Existing sorting, filtering, static pagination, exports, disclosures and exact-data alternatives remain available. Existing phone card conversions retain their complete labels and values.

Passive chips have a 24px minimum height, 2px / 8px padding, 4px corners, 12/18px type and a subdued solid fill. Interactive filters and secondary commands have 32px desktop targets and 44px phone targets, 4px corners and 8px gaps. Tabs have 40px desktop and 44px phone targets, 12px side padding, a 2px active underline and selected weight. Inputs have 6px corners and a 40px desktop / 44px phone minimum height. Global search retains the keyboard shortcut, native dialog, results, focus restoration and a 280 to 320px desktop width.

Focus uses a 2px high-contrast outline with a 3px offset. Panel content must not clip that outline. Existing icon controls retain their names and 16px icon box. Overlays retain their existing shadows; ordinary panels remain flat.

## Application header

Every workspace page shares one 56px header: the Siyab wordmark, the primary destinations (Home, Explore, Account lookup, Trace, Feed, Workspace), an Explore menu holding the collection destinations (Incidents, the four entity collections, Incident records, Issuers), a Tools menu holding Payment checks and Verify, global search and the theme toggle. Menus are native disclosures; on interactive pages Escape, a link choice, an outside click or opening the other menu closes them, and Escape returns focus to the menu button. Below 1240px the header shows the native Menu, which lists every destination in its original order. Records keep the same header without scripts, so their menus close natively. Content uses the full width with 24px gutters.

Content gutters are 24px desktop, 16px at tablet sizes, 12px on phones and 10px at 320px. The graph inspector uses 280px at full desktop width and retains its existing stacking breakpoint. The graph algorithm, recorded evidence, filters and command arrangement remain unchanged. On Explore at 1280px and wider, the display count, recording scope and time range share one compact strip and the legend sits directly above the canvas, bringing the canvas to about y=352 at 1440x900; the loading reserve there is 813px.

## Reference pages

Recorded account pages lead with the exact identifier in the primary identifier role, with Account above it as context. Above 980px the evidence column (facts, actions and sourced labels) pairs about 2:1 with the current mainnet check panel. Issuer actions place section anchors beside the title, keep the qualification and four figures, and put each table's search beside its heading above 760px. Incident pages at 1100px and wider pair the case brief (5 of 12 columns) with the evidence timeline (7 of 12); the no-label replay, recorded transfer route and full evidence follow at full width.

## Page families

The remaining families reuse the reference grammar: a compact page head, flat panels with a 40px header strip and 12px inset, and full-width evidence. Incident records place local section anchors beside the title; each section is a panel of field and value rows in two columns, stacked on phones, and the page stays script-free. The record index keeps the synthetic-example and supersession notes as compact notes directly above the ledger. Collections put the title and assertion count in one head and hold the search, filter and sort toolbar, status and table in one panel. Registry entities keep the identity head, a compact strip for the registry read, observation and save action, and a full-width member table with the original source below. Trace and relationship pages share the Explore graph strip at 1280px and wider; chain edges and attribution hypotheses are separate named panels, the hypothesis panel with a dashed boundary. Account lookup pairs finalized state with token holdings at 1100px and wider, and pairs current valuation with retained observations while archived balances stay a separate block. Workspace keeps the shared and browser boundaries; in the browser section, investigations take the main column with watchlist and alerts beside them at 1100px and wider. Feed, verification and top receivers use the same head and panel treatment, with the unknown causes and the denied split as paired panels.

## Evidence and persistence

Source assertions and attribution hypotheses remain distinct. Registry membership does not establish ownership. Recorded gross outbound transfers do not establish loss, and missing valuations do not become zero. Source dates, exact records, acquisition limits and accessible textual alternatives stay beside the views they qualify.

Collection pagination preserves script-free access to every assertion. Content-addressed scripts and graph data remain local. Record paths, scalar values, arrays, hashes, quotations, citations and re-check commands remain complete. Native record theme selection changes only presentation.

Browser investigations, notes, watchlists and view settings remain browser-local by default. Authentication does not upload them. Shared imports and saves retain their explicit choices, and private workspace data stays out of public account archives. Existing worker-health, coverage, delivery and retained-history qualifications remain visible.

## Landing page

The landing keeps its own slim top bar and navigation. Its content is at most 1200px wide, with 64px section padding on desktop and 36px on phones, derived from the landing section spacing token. The display role is Manrope 600 at clamp(38px, 4.7vw, 68px), 1.04 leading, -0.035em tracking and balanced wrapping, which keeps the headline to three lines and the recorded evidence strip above the fold at 1440 by 900. Its second line uses secondary text. Section titles are Manrope 600 at 32/38px and step and capability titles Manrope 600 at 18/24px. Eyebrows keep their numbers and words in the label role. Principle quotations keep Public Sans 300 at 32/38px. The problem statement scales from 32 to 40px. The recorded transfer counter uses Plex Mono 300 from 40 to 56px with tabular figures.

The hero graph is a deterministic seeded excerpt of the same recorded Rhea graph used by Explore, capped at the root plus 40 nodes and 60 edges. Accent paths identify the two cited transfers, never a payment verdict. The counter is the recording's transfer total and never counts through invented values. The coverage statement remains adjacent. Evidence disclosures retain full identifiers, transactions and receipts.

One focal sequence draws the recorded paths. Two supporting pieces connect the payment-detail pipeline and the incident timeline. All start once on page load, with delayed supporting strokes; text and numbers remain visible. No sequence depends on scrolling or replays. The motion tokens in site.css are:

| Token | Value | Job |
|---|---|---|
| --landing-ease-out | cubic-bezier(.22,1,.36,1) | Settle recorded paths |
| --landing-ease-trace | cubic-bezier(.65,0,.35,1) | Connect evidence |
| --landing-draw-duration | 2200ms | Recorded graph paths |
| --landing-highlight-duration | 1200ms | Cited transfer paths |
| --landing-highlight-delay | 900ms | Cited paths follow the context |
| --landing-pipeline-duration | 700ms | Connect the four payment risks |
| --landing-pipeline-delay | 1800ms | Supporting relationship |
| --landing-timeline-duration | 1000ms | Connect the recorded timestamps |
| --landing-timeline-delay | 2900ms | Supporting chronology |

Only stroke-dashoffset and transform animate. Starting states require the landing script's motion-ready class. Disabled scripts, reduced motion, missing observation support and a hidden tab produce the final state immediately. Every sequence completes within 3900ms of its start, removes its running class, and releases will-change. Existing motion elsewhere retains its timings and Pause controls.

## Preserved motion

Existing keyframes, animation declarations, transition declarations, reduced-motion alternatives, triggers, loops and Pause/Resume controls retain their behaviour. Search entry stays 160ms; inspector entry, including portfolio use, stays 180ms; selected paths stay 900ms; feed arrivals stay 1100ms; button transitions stay 140ms.

```css
@keyframes search-enter{from{opacity:0;transform:translateY(6px) scale(.99)}to{opacity:1;transform:none}}
@keyframes inspector-enter{from{opacity:.65;transform:translateX(6px)}to{opacity:1;transform:none}}
@keyframes event-arrival{from{background:rgb(var(--accent)/.14)}to{background:transparent}}
@keyframes path-select{from{stroke-dasharray:4 4;stroke-dashoffset:32}to{stroke-dasharray:4 4;stroke-dashoffset:0}}
```

The existing reduced-motion rule keeps .01ms animation and transition durations, one iteration and automatic scrolling. Feed pause retains displayed results and the distinction between pausing display updates and monitoring. Unavailable data never becomes a favourable verdict.
