# Visual Design System Research
## Developer Tool Design Language Analysis for Terminal UI

**Date:** 2026-06-13
**Purpose:** Research and codify design patterns from leading developer tools into a terminal-compatible design system for agenthicc's TUI.

---

## 1. Executive Summary

This document analyzes the visual design languages of ten industry-leading developer tools — Linear, Cursor IDE, Raycast, Warp Terminal, Vercel, Arc Browser, GitHub, Notion, VS Code, and Figma — and translates their findings into a coherent, actionable terminal UI design system.

### Key Findings

The dominant design trend across all ten tools is **aggressive restraint**: fewer colors, higher intentionality, and density calibrated to context. Every tool studied has converged on a shared philosophy: color is used sparingly, for semantic signaling only — not decoration. Typography weight and contrast carry most of the visual hierarchy burden. Dark-first design is now the standard for developer-facing tools, with near-black backgrounds (not pure black) preferred for reduced eye strain.

For terminal UI applications, these findings translate to a precise set of rules:

1. **Semantic color over decorative color** — ANSI colors should map to meanings (error, success, warning, info), not aesthetics.
2. **Bold/dim carry hierarchy** — Use ANSI bold and dim modifiers as the primary hierarchy mechanism, not multiple colors.
3. **Negative space is active design** — Spacing between components communicates grouping and priority just as much as color.
4. **Status indicators must be self-explanatory** — A single symbol + color should convey state without prose.
5. **Typography rhythm matters even in monospace** — Consistent indentation (2-space), separator alignment, and line-group whitespace create readability.

The terminal design tokens defined in Section 10 are the primary actionable output of this research.

---

## 2. Analysis of Reference Tools

### 2.1 Linear — Minimal Precision

**Category:** Project management / issue tracker

#### Color Palette Philosophy
Linear's palette underwent a significant shift from 2024 to 2025: it moved from "dull monochrome blue" to "monochrome black/white with even fewer bold colors." The primary accent is indigo (`#5e6ad2`) for interactive elements and emphasis, with lime (`#e4f222`) reserved exclusively for the primary CTA. The system is built on restraint — the fewer colors used, the more weight each carries.

Linear's dark background is near-black (`#0f0f0f`), not pure black, which reduces the harshness of white text contrast while maintaining a premium feel. Neutrals are pure grays with no blue or warm undertones.

#### Typography Choices
- Primary typeface: **Inter Variable**, weight capped at 590
- Monospace/code: **Berkeley Mono** — used for IDs, keyboard shortcuts, and code references
- The monospace choice is significant: it communicates precision and developer identity

#### Spacing / Density Decisions
- Vertical rhythm: 80–120px between major sections
- Intra-component gaps: 8–12px
- Uses an 8px base grid throughout
- "High density" view: 28–32px row heights with minimal padding

#### Visual Hierarchy Approaches
Linear uses a strict single-axis layout — content flows in one direction. This "linearity" principle reduces cognitive load by eliminating zigzag eye movement. Headers are bold + slightly larger; subtext is dimmed. Active items get the accent indigo.

#### Status Indicator Design
Linear uses colored circles for issue status: unstarted (gray), in-progress (yellow), done (green), cancelled (red-orange). Each state maps to a distinct hue — never relying on color alone (shape/fill also varies).

#### Interactive Element Design
Buttons have very low border-radius (4–6px) with subtle hover states. No shadows — elevation comes from background color differences. Focus states use the indigo accent outline.

#### Animation Philosophy
Micro-animations only: 150–200ms easing for state transitions. No gratuitous animations. The system avoids motion that doesn't communicate information.

**Terminal Translation:**
- Near-black bg → `\033[40m` or default terminal bg; prefer 256-color `\033[48;5;232m`
- Indigo accent → `\033[34m` (blue) or `\033[38;5;99m` (256-color indigo)
- Berkeley Mono sensibility → Use monospace spacing conventions; never mix symbol widths

---

### 2.2 Cursor IDE — AI-Native Integration

**Category:** AI-native code editor

#### Color Palette Philosophy
Cursor builds on VS Code's color architecture but introduces AI-specific visual vocabulary. The editor uses dark surfaces with distinct "AI panel" regions differentiated by a slightly elevated surface color. AI-generated suggestions use a subtle purple-toned highlight to distinguish them from user code without disrupting reading flow.

The key innovation is **contextual color** — the same semantic meaning (suggested, accepted, rejected) maps to a consistent color regardless of what is being shown. This makes the AI interaction model instantly legible.

#### Typography Choices
Inherits VS Code's monospace defaults but adds proportional sans-serif in the AI chat panel. The typography contrast between "code area" (monospace) and "conversation area" (sans-serif) creates immediate spatial orientation — you know where you are by the font.

#### Spacing / Density Decisions
High-density code editor pane (standard editor spacing), lower-density chat panel with more breathing room. The density gradient communicates "code here, conversation there" without any explicit labels.

#### Visual Hierarchy Approaches
Three-tier visual hierarchy:
1. Current cursor position / active line: highest contrast + background highlight
2. Code content: default contrast
3. Gutter, line numbers, suggestions: dimmed / reduced opacity

#### Status Indicator Design
- Inline AI spinner: animated dots (not spinners) during generation
- Acceptance state: green checkmark / line glow
- Rejection: neutral (no animation, state simply disappears)
- Error: red underlines (maintained from VS Code convention)

#### Interactive Element Design
"Point and prompt" paradigm — click any element, describe change. Interactive elements have hover states that reveal contextual menus. No permanent chrome; UI surfaces only when needed.

#### Animation Philosophy
Generation animations use a progressive reveal (text appears character-by-character), mimicking typewriter output. This is deliberate: it communicates "AI is generating" vs "AI result is complete."

**Terminal Translation:**
- AI/agent activity → `\033[35m` (magenta) or `\033[38;5;135m` (purple-256)
- Active panel → dim border characters; elevated surface → brighter border
- Progressive reveal → spinner frames, then stable output

---

### 2.3 Raycast — Command Palette Precision

**Category:** macOS app launcher / command palette

#### Color Palette Philosophy
Raycast is a dark-only system. The surface hierarchy is built entirely from background color steps — no shadows, no borders except hairlines:

| Surface | Hex | Purpose |
|---------|-----|---------|
| Canvas | `#07080a` | Page background |
| Surface | `#0d0d0d` | Cards, panels |
| Surface Elevated | `#101111` | Buttons, inputs |
| Surface Card | `#121212` | Keycaps, icon tiles |

Accent colors appear only as illustration accents, never as functional UI colors:
- Yellow: `#ffc533`
- Red: `#ff6161`
- Green: `#59d499`
- Blue: `#57c1ff`

The primary interactive element is a **white pill button** (`#ffffff` background, black text) — the one high-contrast element on any screen.

#### Typography Choices
All text: **Inter** with OpenType features `calt`, `kern`, `liga`, `ss03` enabled. The `ss03` alternate `g` is Raycast's typographic signature. Weight distribution:

| Context | Weight | Size |
|---------|--------|------|
| Foreground text | 400 | 16px |
| Subtext/metadata | 400 muted | 14px |
| Heading | 500 | 24px |
| Display | 600 | 56–64px |
| Button label | 500 | 14px |

#### Spacing / Density Decisions
- Base unit: 8px
- Section rhythm: 96px vertical
- Card padding: 16–24px
- Command palette row height: ~36px (compact but not cramped)

#### Visual Hierarchy Approaches
The command palette uses a strict single-column list. Active item gets a surface-elevated background. Secondary text (descriptions, metadata) appears in muted text color (`#9c9c9d`). Keyboard shortcuts use keycap glyphs with subtle gradient backgrounds.

#### Status Indicator Design
Raycast uses colored badges (small circles or pills) for extension/plugin states. The badge approach: icon + label + optional status pill — all in one horizontal row.

#### Interactive Element Design
The command palette input has a prominent, always-visible cursor. Items have hover states that change background without animation delay. Keyboard shortcut hints are secondary but always present.

#### Animation Philosophy
Near-zero: command palette opens instantly (no transition). Item hover has sub-100ms color change. The philosophy is "keyboard-first, no animation tax."

**Terminal Translation:**
- Surface ladder → Dim background characters or box-drawing borders of varying thickness
- Muted text → `\033[2m` (dim) or `\033[38;5;240m` (gray-256)
- Active item → `\033[7m` (reverse video) or `\033[48;5;235m` (dark gray bg)
- Keyboard hints → brackets: `[Ctrl+C]` in dim text

---

### 2.4 Warp Terminal — Native Terminal Design System

**Category:** Modern terminal application

#### Color Palette Philosophy
Warp uniquely combines ANSI compatibility with a full custom UI system. The key insight: the 16 ANSI colors serve as the **semantic layer** (standard terminal output), while a custom **accent color** serves as the UI interaction layer. This separation prevents UI chrome from clashing with program output.

The default Warp dark theme uses:
- Background: `#01010a` (near-black with very subtle blue warmth)
- Foreground: `#f2f2f7` (off-white, not pure white)
- Accent: `#0066ff` (bright blue)
- UI surfaces (overlays): white at low opacity on dark backgrounds

The gradient background feature shows that Warp treats the terminal canvas as a design surface, not just a text area. This is philosophically significant: visual richness can coexist with functional output if properly layered.

#### Typography Choices
Warp uses semantic syntax coloring on commands as you type — distinguishing subcommands, options, and variables through color. This extends the concept of "typography" to include semantic coloring as a type signal.

The default monospace font follows JetBrains Mono conventions: a humanized monospace with large x-height and clear descenders.

#### Spacing / Density Decisions
Warp introduces the "block" concept: each command + output forms a visually grouped block with a subtle background and top/bottom separation. This creates natural information density — blocks can be expanded/collapsed.

#### Visual Hierarchy Approaches
- Command prompt: highest contrast, with user-controlled accent color
- Output text: default foreground
- Error output (stderr): red ANSI (`\033[31m`)
- Timestamps / metadata: dimmed, right-aligned
- Block headers: slightly elevated background

#### Status Indicator Design
- Running commands: animated spinner in the prompt
- Exit code 0: no indicator (success is silence)
- Exit code non-zero: red status badge with exit code number
- AI suggestions: purple/violet accent region

#### Interactive Element Design
Command palette overlay uses a translucent dark surface with blur effect. Buttons use the accent color. Hoverable regions show a subtle highlight.

#### Animation Philosophy
The block model enables smooth expand/collapse animations. Spinners during command execution. But the philosophy aligns with terminal conventions: when in doubt, don't animate.

**Terminal Translation:**
- Block concept → Use separator lines above/below command output groups: `─────────────`
- Block accent → Left border indicator: `│` in color
- Spinner → `⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏` (Braille dot spinners)
- Exit status → `✓` in green or `✗` in red after command

---

### 2.5 Vercel — Developer Dashboard Clarity

**Category:** Deployment platform / developer dashboard

#### Color Palette Philosophy
Vercel's Geist design system is perhaps the most studied system for developer tools. Its philosophy: "a design system doesn't need complexity to feel premium." The palette is brutally minimal:

| Token | Value | Use |
|-------|-------|-----|
| Background | `#000000` | Dark surfaces |
| Foreground | `#FFFFFF` | Primary text |
| Accent Blue | `#0070F3` | Links, CTAs, active states only |
| Error | `#EE0000` | Error states |
| Warning | `#F5A623` | Warning states |
| Gray-900 | `#171717` | Near-black surfaces |
| Gray-800 | `#262626` | Elevated surfaces |
| Gray-700 | `#404040` | Secondary surfaces |
| Gray-500 | `#737373` | Secondary text |
| Gray-400 | `#A3A3A3` | Placeholder / disabled |
| Gray-200 | `#E5E5E5` | Borders (light mode) |

The gray ramp is pure neutral — zero blue or warm undertones. This keeps the system feeling technical and precise.

#### Typography Choices
Geist was purpose-built for developer interfaces:
- **Geist Sans**: UI text, labels, documentation
- **Geist Mono**: Code blocks, terminal output, technical labels (12–14px optimized)

Scale: XS 12px / SM 14px / Base 16px / LG 18px / XL 24px / 2XL 32px / 3XL 48px / Display 64px

Letter spacing: `-0.01em` default ("tighter than Inter" for a more designed feel)

#### Spacing / Density Decisions
8px base grid: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64 / 96 / 128px. Dashboard rows are compact (~36–40px) with clear section headers. Marketing uses aggressive 96–128px section padding; product UI uses 16–24px.

#### Visual Hierarchy Approaches
Restrained hierarchy: bold weight and `#FFFFFF` vs `#737373` (gray-500) carry all differentiation. Active/selected states use the accent blue — the only non-neutral color in most screens.

#### Status Indicator Design
Deployment states are a key Vercel design contribution:
- **Queued**: gray circle `○` + "Queued" label
- **Building**: animated progress indicator + "Building" in blue
- **Ready/Success**: green dot `●` + "Ready"
- **Error**: red dot `●` + "Error" + exit reason
- **Cancelled**: gray `×` + "Cancelled"

Status appears in browser tab icons, dashboards, and inline — always the same symbol set.

#### Interactive Element Design
Sharp to near-sharp edges (border-radius 0–6px). Borders at 8% opacity — visible but never heavy. Hover states: background shifts one step on the gray ramp.

#### Animation Philosophy
Performance is an aesthetic: the redesign targeted reducing First Meaningful Paint by 1.2s. No decorative animations. Real-time updates via SWR appear instantly — the product communicates speed by being fast.

**Terminal Translation:**
- Neutral gray scale → `\033[38;5;240m` through `\033[38;5;255m`
- Status dots: `●` (green/red) or `○` (gray) for deployment states
- Accent blue → `\033[34m` or `\033[94m` (bright blue)
- Geist Mono sensibility → Tight label alignment; numbers right-aligned

---

### 2.6 Arc Browser — Spatial Minimalism

**Category:** Modern web browser

#### Color Palette Philosophy
Arc treats browser chrome as furniture — it should disappear into the background so web content takes center stage. The sidebar (the core UI element) uses a customizable color space — users can assign any color to a "Space." However, Arc's defaults use muted, desaturated tones: dusty blues, warm grays, and soft purples.

The philosophy of "figure-ground" is central: the browser UI is ground; the web page is figure. Minimal chrome = maximum focus on content.

#### Typography Choices
Arc uses a mix of serif and sans-serif in unexpected places — breaking the "pure sans-serif" convention of most tools. The result feels editorial and personal. System fonts where possible to maximize performance and native feel.

#### Spacing / Density Decisions
The sidebar is deliberately narrow and uses high density for tab organization. However, the main content area gets maximum space — the density asymmetry creates a strong figure-ground relationship.

#### Visual Hierarchy Approaches
Arc uses **spatial organization** as hierarchy: the sidebar organizes by Space (workspace), then folder, then tab. Position communicates grouping without color. Indentation and icons carry the hierarchy signal.

#### Status Indicator Design
Favicons serve as the primary status indicator for tab state — open, loading (spinner overlay on favicon), and errored (broken image icon). Browser loading state: progress bar that fades out on completion.

#### Interactive Element Design
Soft rounded corners (8–16px on main surfaces). Smooth transitions at 200–300ms. Hover states reveal additional actions in-context rather than displacing content.

#### Animation Philosophy
Arc's animations are more generous than other tools: tab switching, space transitions, and command bar opening all use easing curves. The philosophy is that animation communicates spatial navigation — "I moved here from there."

**Terminal Translation:**
- Spatial hierarchy → Indentation + box-drawing hierarchy: `├──` / `└──`
- Sidebar/main contrast → Left panel in dim, right content in bright
- Arc's "sidebar" → Narrow status column left; wide content area right

---

### 2.7 GitHub — Code Review and Diff Visualization

**Category:** Version control platform / code review

#### Color Palette Philosophy
GitHub's Primer design system supports nine themes and two color modes (light/dark). Semantic colors are the core innovation: each color is tied to a role, not an appearance.

Key semantic roles:

| Role | Light hex (approx) | Dark hex (approx) | Meaning |
|------|--------------------|-------------------|---------|
| accent | `#0969da` | `#2f81f7` | Links, selection, focus |
| success | `#1a7f37` | `#3fb950` | Positive states, merges |
| danger | `#d1242f` | `#f85149` | Errors, destructive actions |
| attention | `#9a6700` | `#d29922` | Warnings, in-progress |
| open | `#1a7f37` (green) | `#3fb950` | Open PR/issue |
| closed | `#d1242f` (red) | `#f85149` | Closed PR/issue |
| done | `#8250df` (purple) | `#a371f7` | Completed/merged |

The neutral scale goes 0–13 with distinct ranges: 0–5 for backgrounds, 7–8 for borders, 9–10 for text. This provides deterministic contrast.

#### Typography Choices
- UI text: system-ui (platform native)
- Code: `SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace`
- Base size: 14px for dense UI, 16px for documentation
- Line height: 1.5 for readability

#### Spacing / Density Decisions
Primer uses a base-8 grid. The "density" principle varies by context:
- Dense: issue lists, file trees (28–32px rows)
- Comfortable: PR review, comments (full-width, generous padding)
- Spacious: documentation, landing pages

#### Visual Hierarchy Approaches
Four content levels:
1. **Page title** — largest, always H1
2. **Section headers** — H2/H3 with dividers
3. **Component labels** — bold inline text
4. **Metadata** — muted/dimmed text

#### Status Indicator Design
GitHub's status system is a design pattern worth studying:
- **PR states**: Open (green circle), Closed (red circle), Merged (purple merge icon), Draft (gray circle with dashes)
- **Check states**: Pending (yellow ●), Success (green ✓), Failure (red ✗), Skipped (gray ―)
- **Diff visualization**: Green (+) for additions, red (-) for deletions, with full-line background color tinting

#### Interactive Element Design
Diff view uses side-by-side or unified mode. Addition lines: light green background (`#e6ffec`). Deletion lines: light red background (`#ffebe9`). Line numbers in gutter, muted.

#### Animation Philosophy
GitHub uses minimal animations: dropdown open/close fade, notification toast slide-in. Code and data never animate — instant render is the convention for technical content.

**Terminal Translation:**
- Diff: `+` lines in `\033[32m` (green), `-` lines in `\033[31m` (red), `@` in `\033[36m` (cyan)
- PR state: `●` open in green, `◉` merged in magenta, `○` closed in red
- Check states: `✓` in green, `✗` in red, `●` in yellow for pending
- Dividers: `═══` or `───` to separate sections

---

### 2.8 Notion — Information Hierarchy

**Category:** Productivity / knowledge management

#### Color Palette Philosophy
Notion uses a cream-white background (`#FFFFFF` / `#FFFEF9` with subtle warmth) rather than pure white. Text is `#37352F` (near-black, warm undertone) rather than pure black. This warm tone-on-tone approach reduces eye strain significantly.

For callout blocks and highlights, Notion uses the full spectrum of soft, pastel-tinted backgrounds: yellow, blue, green, red, purple — all at low saturation to avoid visual noise.

#### Typography Choices
- Primary: **NotionInter** (custom variant of Inter with tighter spacing)
- Editorial pull quotes: **Lyon Text** (serif) — used sparingly for variety
- Weight hierarchy: 600 (headings), 500 (subheadings), 400 (body/UI)
- Body: 16px, line-height 1.5
- Headings: 20px+ at 500–600 weight

#### Spacing / Density Decisions
8px base grid: 4, 8, 12, 16, 20, 24, 32, 40, 48, 64px. Content is intentionally spacious — generous line height and paragraph spacing to support extended reading sessions.

#### Visual Hierarchy Approaches
Notion's information hierarchy is block-based:
- H1: largest, heavy weight, with more top margin than bottom
- H2: mid-weight, secondary section marker
- H3: small-cap or italic variation
- Body: normal weight, generous line-height
- Callout: indented with left border in accent color
- Code: monospace on a tinted background

The sidebar uses a tree hierarchy with indentation: each level adds 20px left padding. Parent items can be bold to indicate they contain children.

#### Status Indicator Design
Notion database views use colored circles for status properties — custom status with custom colors. Default: not started (gray), in progress (blue), done (green).

#### Interactive Element Design
Hover states reveal action buttons inline. Drag handles appear on hover. Selection state is light blue highlight. Every interactive element appears contextually — no permanent toolbar clutter.

#### Animation Philosophy
Notion uses subtle slide and fade for page transitions (100–200ms). Block rearrangement uses smooth drag feedback. Generally conservative — editing feels instant.

**Terminal Translation:**
- Notion's warm tones → Use `\033[33m` (yellow) sparingly for callout content
- Block hierarchy → Indented box-drawing: `│  ` prefix for nested content
- Tree sidebar → `├── item` / `└── last item` pattern
- Spacious reading → Double newlines between paragraph groups

---

### 2.9 VS Code — Editor UI Patterns

**Category:** Code editor

#### Color Palette Philosophy
VS Code's dark default ("Dark Modern") uses:
- Background: `#1e1e1e`
- Surface (sidebar): `#252526`
- Surface (status bar): `#007acc` (blue) — uniquely, the status bar uses a full accent color
- Editor background: `#1e1e1e`
- Text foreground: `#d4d4d4` (light gray, not white)
- Comments: `#6a9955` (muted green)
- Strings: `#ce9178` (warm orange)
- Keywords: `#569cd6` (blue)
- Functions: `#dcdcaa` (yellow)
- Types: `#4ec9b0` (teal)
- Numbers: `#b5cea8` (light green)

The syntax highlighting palette is carefully balanced: no two adjacent language constructs use highly similar colors. The system is designed for long reading sessions at small font sizes.

#### Typography Choices
VS Code defaults to `Consolas` (Windows) / `Menlo` (macOS) / `monospace`. The ecosystem standard is now JetBrains Mono or Fira Code with ligatures. Tab size is 4 spaces by default but 2 is common for web development.

#### Spacing / Density Decisions
Line height: 1.5 default for code. Character width is tight — monospace at 14px. The status bar is 22px tall — the thinnest chrome possible while remaining clickable. Sidebar icons are 22px with 4px padding.

#### Visual Hierarchy Approaches
VS Code establishes zones:
- **Activity bar** (leftmost): icons only, no text, all same visual weight
- **Sidebar**: file tree with icon + name at 14px
- **Editor**: content at center, max priority
- **Status bar** (bottom): small, persistent info in colored accent
- **Panel** (bottom): terminal/problems in lower surface

Each zone has a distinct background, creating spatial separation without explicit borders.

#### Status Indicator Design
VS Code's Diagnostics system uses colored squiggles (error = red, warning = yellow, info = blue, hint = green). The Problems panel uses the same color set in a list. The status bar summarizes: `✗ 2  ⚠ 4` with count badges.

#### Interactive Element Design
Activity bar icons are always visible; sidebar can be toggled. Tabs at the top of the editor show file name + close button (×) on hover. Breadcrumb shows path hierarchy at the top of each file.

#### Animation Philosophy
Near-zero. Even the command palette opens instantly. The only animations are progress indicators in the activity bar and sidebar skeleton loaders. Speed is the primary aesthetic.

**Terminal Translation:**
- Zone concept → Persistent header bar + scrolling content + footer status bar
- Status bar bottom → Last line of TUI with always-visible status
- Diagnostics → `\033[31m✗\033[0m` for errors, `\033[33m⚠\033[0m` for warnings
- Activity zones → Named section headers with consistent format

---

### 2.10 Figma — Tool UI Patterns

**Category:** Design tool

#### Color Palette Philosophy
Figma's dark mode interface:
- Canvas background: `#1e1e1e` (off-black)
- Panel background: `#2c2c2c`
- Toolbar: `#2c2c2c` with `#ffffff` icons
- Properties panel: `#232323`
- Accent: `#8b5cf6` (violet — Figma's brand purple)
- Selection highlight: `#1abcfe` (bright blue)

The tool panel itself is deeply dark to contrast with the bright canvas content (designs). The UI disappears so the design can breathe.

#### Typography Choices
Figma uses inter at 11–12px for panel text — extremely dense. The small font size is acceptable because Figma users are experts who scan rather than read panel labels. Labels are sentence case, not ALL CAPS (which was historically common in design tools).

#### Spacing / Density Decisions
Panels are extremely dense: 24–28px row heights in properties panels, 32px in the layers panel. The toolbar icons are 32×32px with minimal padding. Every pixel is valuable in the editor chrome.

#### Visual Hierarchy Approaches
Figma uses **group-by-similarity** hierarchy: all fill properties together, all stroke properties together, with labeled section headers. Collapsed groups are indicated by a chevron `›`. The layers panel uses indentation + thumbnail for visual scanning.

#### Status Indicator Design
Multiplayer cursors: each collaborator gets a unique color cursor with a name tooltip. Comments appear as orange speech bubbles on the canvas. Prototype links appear as blue arrows. Each status type has a distinct visual treatment.

#### Interactive Element Design
Properties panel controls are extremely compact: color swatches (16×16px), number inputs with up/down arrows, toggle switches (24px wide). Every control is dense but distinct.

#### Animation Philosophy
Figma invests in animation for collaboration features: cursor movement, comment dropping, and multiplayer presence all animate smoothly. For non-collaborative interactions (panel toggles, selection), animations are minimal. The philosophy: animate to communicate the presence of others, not to decorate.

**Terminal Translation:**
- Layers panel → Indented tree with `├──`, `│  `, `└──`
- Properties → Aligned key: value pairs with padding
- Dense toolbar → Single-line command strip below input or as top header
- Multiplayer → Show agent names in distinct colors

---

## 3. Terminal Color System Design

### 3.1 Semantic Color Palette

This semantic palette maps meaning to terminal color codes. Every color choice follows a single rule: **color communicates meaning, not decoration.**

#### Primary Semantic Colors

```
SUCCESS     → \033[32m  (green)        hex ref: #3fb950
ERROR       → \033[31m  (red)          hex ref: #f85149
WARNING     → \033[33m  (yellow)       hex ref: #d29922
INFO        → \033[34m  (blue)         hex ref: #2f81f7
ACCENT      → \033[35m  (magenta)      hex ref: #bc8cff  [AI/agent actions]
NEUTRAL     → \033[0m   (default fg)   hex ref: #d4d4d4
MUTED       → \033[2m   (dim)          hex ref: #6e7681
EMPHASIS    → \033[1m   (bold)         hex ref: #ffffff
```

#### 256-Color Extended Palette (for true-color terminals)

```
# Backgrounds (surface ladder, inspired by Raycast/Vercel)
BG_DEFAULT  → \033[48;5;232m   hex ref: #080808  (base surface)
BG_ELEVATED → \033[48;5;234m   hex ref: #1c1c1c  (panels, cards)
BG_ACTIVE   → \033[48;5;236m   hex ref: #303030  (selected row, active item)
BG_HOVER    → \033[48;5;237m   hex ref: #3a3a3a  (hover state)

# Foregrounds
FG_PRIMARY  → \033[38;5;252m   hex ref: #d0d0d0  (main content)
FG_SECONDARY→ \033[38;5;245m   hex ref: #8a8a8a  (secondary text)
FG_MUTED    → \033[38;5;240m   hex ref: #585858  (disabled/placeholder)
FG_BRIGHT   → \033[38;5;255m   hex ref: #eeeeee  (headers, emphasis)

# Semantic accents (extended)
SUCCESS_BRIGHT  → \033[38;5;77m    hex ref: #5faf5f
SUCCESS_MUTED   → \033[38;5;22m    hex ref: #005f00
ERROR_BRIGHT    → \033[38;5;196m   hex ref: #ff0000
ERROR_MUTED     → \033[38;5;88m    hex ref: #870000
WARNING_BRIGHT  → \033[38;5;214m   hex ref: #ffaf00
WARNING_MUTED   → \033[38;5;58m    hex ref: #5f5f00
INFO_BRIGHT     → \033[38;5;75m    hex ref: #5fafff
INFO_MUTED      → \033[38;5;24m    hex ref: #005f87
AGENT_COLOR     → \033[38;5;135m   hex ref: #af5fff  (AI/agent activity)
AGENT_MUTED     → \033[38;5;53m    hex ref: #5f005f
```

### 3.2 Foreground / Background Contrast Ratios

Following GitHub Primer's WCAG AA compliance approach, the design system maintains minimum contrast ratios:

| Use Case | Foreground | Background | Contrast |
|----------|------------|------------|----------|
| Primary text | `#d0d0d0` | `#080808` | ~13:1 (AAA) |
| Secondary text | `#8a8a8a` | `#080808` | ~7:1 (AA) |
| Muted text | `#585858` | `#080808` | ~4.5:1 (AA) |
| Error red on dark | `#f85149` | `#080808` | ~5:1 (AA) |
| Success green on dark | `#3fb950` | `#080808` | ~5.5:1 (AA) |
| Warning yellow on dark | `#d29922` | `#080808` | ~6:1 (AA) |

**Rule**: Never place muted text on an elevated background — the contrast may fall below WCAG AA. Always test status indicators on both `BG_DEFAULT` and `BG_ELEVATED`.

### 3.3 Dark / Light Terminal Compatibility

Terminal UIs must degrade gracefully across:
- **True color** (24-bit): Use hex-derived `\033[38;2;R;G;Bm` values
- **256-color**: Use `\033[38;5;Nm` palette as documented above
- **16-color ANSI**: Fall back to basic 8 + bright variants
- **No color**: Use bold/dim/underline for all hierarchy

**Detection strategy:**
```python
import os
color_support = os.environ.get("COLORTERM", "")
if color_support in ("truecolor", "24bit"):
    # use \033[38;2;R;G;Bm
elif os.environ.get("TERM_PROGRAM") or "256color" in os.environ.get("TERM", ""):
    # use \033[38;5;Nm
else:
    # use 8-color ANSI only
```

**16-color fallback mapping:**

```
SUCCESS  → \033[32m   (green)
ERROR    → \033[31m   (red)
WARNING  → \033[33m   (yellow)
INFO     → \033[34m   (blue)
AGENT    → \033[35m   (magenta)
MUTED    → \033[2m    (dim)
EMPHASIS → \033[1m    (bold)
HEADER   → \033[1;37m (bold white)
```

---

## 4. Typography in Terminals

### 4.1 Bold, Italic, Underline Usage

Terminal "typography" is limited to ANSI modifiers. Following the research:

| Modifier | ANSI Code | Use | Inspired By |
|----------|-----------|-----|-------------|
| Bold | `\033[1m` | Headers, labels, emphasis, key data | Linear, VS Code, GitHub |
| Dim | `\033[2m` | Secondary text, metadata, timestamps | Raycast, Vercel |
| Italic | `\033[3m` | Quoted text, descriptions, editorial | Notion |
| Underline | `\033[4m` | Links, clickable references | GitHub, VS Code |
| Reverse | `\033[7m` | Active selection, cursor position | Raycast, VS Code |
| Strikethrough | `\033[9m` | Deprecated, cancelled items | GitHub (cancelled PR) |

**Reset:** Always reset after applying: `\033[0m`

**Compound modifiers:**
```
\033[1;32m   = bold + green (success headers)
\033[2;33m   = dim + yellow (muted warning context)
\033[1;37m   = bold + white (section headers)
\033[4;34m   = underline + blue (hyperlinks)
```

### 4.2 Monospace Font Optimization

Since all terminal output is monospace, these conventions maximize readability:

**Alignment principles** (inspired by Vercel/GitHub):
- Numbers: always right-align in columns
- Status indicators: left-align at fixed column position
- Key-value pairs: align `:` separator at a consistent column within a block
- File paths: left-align; truncate from the left with `…` if needed

**Line length guidelines:**
- Maximum content width: 100 chars (avoids wrapping in most terminals)
- Status bar / header: full terminal width
- Prose/description: max 80 chars (one measure of comfortable reading)
- Code/output: unrestricted (preserve original formatting)

**Whitespace as typography:**
```
# Good: blank lines create section rhythm (Notion principle)
Section Header
  item one
  item two

Next Section
  item three

# Bad: wall of text with no breathing room
Section Header
  item one
  item two
Next Section
  item three
```

---

## 5. Spacing System

### 5.1 Padding Conventions

Following the 8px base grid (Raycast, Vercel, Notion, Linear) translated to terminal columns/rows:

| Token | Terminal Units | Use Case |
|-------|---------------|----------|
| `space-1` | 1 char / 1 line | Tight component internal padding |
| `space-2` | 2 chars / 2 lines | Default component padding |
| `space-3` | 4 chars | Section indent, nested content |
| `space-4` | 8 chars | Major section offset |
| `space-section` | 1 blank line | Between related items |
| `space-group` | 2 blank lines | Between major sections |

**Horizontal padding convention:**
```
# Component border box (space-2 = 2 chars each side)
┌──────────────────────────┐
│  content starts here     │
│  aligned content         │
└──────────────────────────┘

# Indented child content (space-3 = 4 chars)
Parent Label
    child item one
    child item two
        grandchild item
```

### 5.2 Visual Breathing Room

Inspired by Linear's "80–120px section rhythm" and Notion's generous line heights:

**Rule 1: Never stack two bold/colored lines without a visual break**
```
# Wrong — visually dense
\033[1mSECTION ONE\033[0m
\033[1mSECTION TWO\033[0m

# Correct — room to breathe
\033[1mSECTION ONE\033[0m
(blank line)
\033[1mSECTION TWO\033[0m
```

**Rule 2: Group related items; separate groups**
```
Agent: planner-1
Task: Analyze requirements
Status: running

Agent: executor-1
Task: Write implementation
Status: queued
```

**Rule 3: Status columns use fixed width**
```
[ SUCCESS ]  Task completed in 1.23s
[  ERROR  ]  Permission denied: /etc/passwd
[ WARNING ]  Retry attempt 2/3
```

---

## 6. Visual Hierarchy

### 6.1 Primary / Secondary / Tertiary Text

**Four-level hierarchy** (inspired by GitHub Primer's 4-tier text levels):

```python
# Level 1 — Primary (headers, action labels)
"\033[1;37m{text}\033[0m"          # bold white

# Level 2 — Secondary (content, descriptions)
"\033[0m{text}\033[0m"             # default fg

# Level 3 — Tertiary (metadata, timestamps, counts)
"\033[2m{text}\033[0m"             # dim

# Level 4 — Ghost (disabled, placeholder)
"\033[38;5;240m{text}\033[0m"      # dark gray (256-color)
# fallback: "\033[2m{text}\033[0m" in 16-color
```

**Usage example:**
```
\033[1;37mAgent Output\033[0m                   ← Level 1 header
Task completed successfully.               ← Level 2 content
Duration: 1.4s · Memory: 128MB             ← Level 3 metadata
(press q to dismiss)                       ← Level 4 ghost
```

### 6.2 Prominence Indicators

| Signal | Terminal Method | Example |
|--------|----------------|---------|
| Most important | Bold + accent color | `\033[1;34m❯ Critical action\033[0m` |
| Important | Bold default fg | `\033[1mKey information\033[0m` |
| Normal | Default | `Regular content` |
| Supporting | Dim | `\033[2mTimestamp: 12:34\033[0m` |
| Disabled | Dim + strikethrough | `\033[2;9mOld option\033[0m` |

---

## 7. Component Styling Guide

### 7.1 Headers

**Page-level header** (inspired by VS Code's zone headers + Linear's bold hierarchy):
```
\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m
\033[1;37m  AGENTHICC  ·  Session #42\033[0m
\033[1;37m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m
```

**Section header** (inspired by Notion H2 + GitHub dividers):
```
\033[1;37m▶ Planning Phase\033[0m
\033[38;5;240m──────────────────────────────\033[0m
```

**Subsection header** (inspired by Raycast command groups):
```
\033[1mTool Results\033[0m
```

**Inline header** (label + value):
```
\033[1mAgent:\033[0m  planner-1
\033[1mTask:\033[0m   Analyze codebase
\033[1mStatus:\033[0m \033[33m⏳ running\033[0m
```

### 7.2 Code Blocks

Inspired by VS Code syntax highlighting + GitHub code review + Notion code blocks:

```
\033[48;5;234m\033[38;5;252m  # code block with elevated background     \033[0m
\033[48;5;234m\033[38;5;252m  def my_function(x: int) -> str:           \033[0m
\033[48;5;234m\033[38;5;252m      return str(x)                         \033[0m
```

For inline code within prose:
```
Use the \033[48;5;234m\033[38;5;252m `run_bash` \033[0m tool to execute shell commands.
```

Language label (top-right of code block):
```
\033[48;5;234m\033[38;5;245m  python                                    \033[0m
```

### 7.3 Diffs

Directly translated from GitHub's diff visualization:

```
\033[38;5;245m@@ -12,7 +12,7 @@\033[0m
\033[38;5;240m───────────────────────────────────────────\033[0m
\033[31m- old_function_name(arg1, arg2)\033[0m
\033[32m+ new_function_name(arg1, arg2, arg3)\033[0m
  unchanged_line_here()
  another_unchanged_line()
\033[31m- removed_line()\033[0m
\033[32m+ added_replacement_line()\033[0m
```

**Diff summary header** (hunk metadata):
```
\033[1;36m  src/main.py  +3 -1\033[0m
```

**Binary / too large notice:**
```
\033[2m  (binary file or diff too large to display)\033[0m
```

### 7.4 Status Indicators

Inspired by Vercel deployment states + GitHub check states + Warp exit codes:

```python
STATUS_SYMBOLS = {
    "pending":   "\033[33m○\033[0m",    # yellow empty circle
    "running":   "\033[34m●\033[0m",    # blue solid circle (+ spinner anim)
    "success":   "\033[32m✓\033[0m",    # green checkmark
    "error":     "\033[31m✗\033[0m",    # red cross
    "warning":   "\033[33m⚠\033[0m",   # yellow warning
    "cancelled": "\033[2m○\033[0m",     # dim empty circle
    "skipped":   "\033[2m─\033[0m",     # dim dash
    "blocked":   "\033[31m⊘\033[0m",   # red prohibited
}

# Usage in status line
f"{STATUS_SYMBOLS['running']} Task: Analyzing codebase..."
f"{STATUS_SYMBOLS['success']} Task: Completed in 2.3s"
f"{STATUS_SYMBOLS['error']} Task: Failed — permission denied"
```

**Status badge format** (fixed-width, Vercel-inspired):
```
\033[42m\033[30m SUCCESS \033[0m  Built in 3.2s
\033[41m\033[37m  ERROR  \033[0m  Exit code 1
\033[43m\033[30m WARNING \033[0m  Deprecated API used
\033[44m\033[37m  INFO   \033[0m  2 files modified
\033[2m[PENDING]\033[0m  Waiting for dependency
```

### 7.5 Progress Bars

Inspired by VS Code's progress indicators + Warp's task display:

**Determinate progress:**
```python
def progress_bar(pct: float, width: int = 30) -> str:
    filled = int(width * pct)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"\033[34m[{bar}]\033[0m {int(pct*100):3d}%"

# Example output:
# \033[34m[████████████░░░░░░░░░░░░░░░░░░]\033[0m  40%
```

**Indeterminate (spinner):**
```python
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
# Usage: f"\033[34m{SPINNER_FRAMES[frame % 10]}\033[0m Building..."
```

**Task progress summary:**
```
\033[1mWorkflow Progress\033[0m
█████████░░░░░░░░  3 / 5 tasks complete
\033[32m✓\033[0m plan        1.2s
\033[32m✓\033[0m implement   8.4s
\033[32m✓\033[0m test        3.1s
\033[34m●\033[0m review      running...
\033[2m○\033[0m deploy      waiting
```

### 7.6 Dividers

Inspired by Linear's single-line separators + GitHub's section breaks + VS Code's zone boundaries:

```python
# Heavy divider — major section separator
DIVIDER_HEAVY = "\033[38;5;240m" + "═" * width + "\033[0m"

# Light divider — sub-section separator
DIVIDER_LIGHT = "\033[38;5;237m" + "─" * width + "\033[0m"

# Dotted divider — between list items (Notion-inspired)
DIVIDER_DOT   = "\033[38;5;236m" + "·" * width + "\033[0m"

# Named section divider (Raycast-inspired group headers)
def section_divider(label: str, width: int = 60) -> str:
    label_str = f"  {label}  "
    line_len = (width - len(label_str)) // 2
    line = "─" * line_len
    return f"\033[2m{line}{label_str}{line}\033[0m"
```

### 7.7 Agent Messages

The agent message format creates clear author identity (inspired by Cursor's AI/code zone contrast + Figma's multiplayer cursors):

```
# Agent turn header
\033[1;35m◆ agent:planner-1\033[0m  \033[2m12:34:52\033[0m
\033[38;5;240m──────────────────────────────────────\033[0m

Content of agent response appears here.
Multiple lines of content if needed.

# User/system turn
\033[1;37m◇ system\033[0m  \033[2m12:34:51\033[0m
\033[38;5;240m──────────────────────────────────────\033[0m

System message content.
```

**Agent role colors:**
```python
AGENT_COLORS = {
    "planner":   "\033[35m",  # magenta
    "executor":  "\033[34m",  # blue
    "reviewer":  "\033[36m",  # cyan
    "system":    "\033[37m",  # white
    "user":      "\033[32m",  # green
    "error":     "\033[31m",  # red
}
```

### 7.8 Tool Output

Inspired by Warp's "block" concept — each tool invocation is a visually distinct unit:

```
\033[2m┌─ tool:run_bash ─────────────────────────\033[0m
\033[2m│\033[0m \033[38;5;245m$ git status\033[0m
\033[2m│\033[0m On branch main
\033[2m│\033[0m nothing to commit, working tree clean
\033[2m└─\033[0m \033[32m✓\033[0m \033[2mexited 0 · 0.12s\033[0m
```

**Tool call header variants:**
```python
def tool_header(name: str, args_preview: str = "") -> str:
    preview = f" {args_preview}" if args_preview else ""
    return f"\033[2m┌─ tool:{name}{preview} {'─'*max(0, 40-len(name)-len(args_preview))}┐\033[0m"

def tool_footer(exit_code: int, duration: float) -> str:
    status = "\033[32m✓\033[0m" if exit_code == 0 else "\033[31m✗\033[0m"
    return f"\033[2m└─\033[0m {status} \033[2mexited {exit_code} · {duration:.2f}s\033[0m"
```

---

## 8. Icon and Symbol System

All symbols use Unicode characters from the Basic Multilingual Plane (BMP) for broad terminal compatibility. Composed from three tiers:

### 8.1 Tier 1 — Universal (ASCII + Basic Latin)

Available in every terminal, every font:

| Symbol | Use |
|--------|-----|
| `>` / `<` | Direction, navigation |
| `*` | Active, asterisk, required |
| `-` | Dash, bullet, separator |
| `=` | Equals, double separator |
| `+` | Add, success delta |
| `#` | Number, channel, tag |
| `!` | Alert, warning |
| `?` | Unknown, help |
| `[x]` | Checkbox checked |
| `[ ]` | Checkbox unchecked |

### 8.2 Tier 2 — Box Drawing and Blocks (widely supported)

```
Borders:   ─  │  ┌  ┐  └  ┘  ├  ┤  ┬  ┴  ┼
Double:    ═  ║  ╔  ╗  ╚  ╝  ╠  ╣  ╦  ╩  ╬
Heavy:     ━  ┃  ┏  ┓  ┗  ┛  ┣  ┫  ┳  ┻  ╋
Rounded:   ╭  ╮  ╰  ╯
Blocks:    █  ▓  ▒  ░
Arrows:    ▶  ◀  ▲  ▼  ►  ◄  ↑  ↓  ←  →
Triangles: ◆  ◇  ◉  ○  ●  ◎
```

### 8.3 Tier 3 — Extended Unicode (modern terminals, Nerd Fonts optional)

Use with fallback: if Nerd Fonts unavailable, substitute Tier 2 equivalents.

| Symbol | Meaning | ASCII fallback |
|--------|---------|---------------|
| `✓` | Success / done | `[ok]` |
| `✗` | Error / fail | `[!!]` |
| `⚠` | Warning | `[!]` |
| `ℹ` | Information | `[i]` |
| `⏳` | In progress | `...` |
| `🔒` | Locked / secure | `[lock]` |
| `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` | Spinner frames | `\|/-\\` |
| `❯` | Prompt / chevron | `>` |
| `◆` | Agent turn marker | `*` |
| `◇` | System turn marker | `-` |

### 8.4 Status Symbol Combinations

```
Running:   \033[34m●\033[0m  or  \033[34m⠙\033[0m (spinner)
Done:      \033[32m✓\033[0m
Error:     \033[31m✗\033[0m
Warning:   \033[33m⚠\033[0m
Pending:   \033[2m○\033[0m
Blocked:   \033[31m⊘\033[0m
Skipped:   \033[2m─\033[0m
Info:      \033[34mℹ\033[0m
Merged:    \033[35m◉\033[0m
Cancelled: \033[2m✗\033[0m
```

---

## 9. Animation and Motion Philosophy

### 9.1 Core Principles

Derived from synthesizing all ten tools studied:

1. **Animation communicates state change, not aesthetics.** If an animation doesn't tell the user something changed, remove it.
2. **Input lag is the enemy.** Never delay user input for animation completion. Raycast's "instant open" philosophy applies universally.
3. **Terminal animations must be opt-in aware.** Check `NO_COLOR` and `TERM=dumb` before any color/animation output.
4. **Frame rate is dictated by refresh budget, not ambition.** 10fps (100ms) is sufficient for spinners; 4fps (250ms) is acceptable for progress bars.

### 9.2 When to Animate

| Scenario | Animation | Duration | Inspired By |
|----------|-----------|----------|-------------|
| Command executing | Spinner on prompt | Continuous | Warp |
| Task starting | One-shot spinner advance | On state change | VS Code progress |
| Task completing | Status symbol replace | Instant | Vercel dashboard |
| Agent responding | Streaming text reveal | Real-time | Cursor IDE |
| Error appearing | Static (no animation) | Instant | All tools |
| Progress bar updating | Smooth fill | Per tick | VS Code |

### 9.3 Implementation Pattern

```python
import sys
import time

def with_spinner(message: str, task_fn) -> None:
    """Run task_fn while displaying a spinner."""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    # Check for animation support
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        print(f"... {message}")
        result = task_fn()
        print(f"done")
        return result

    try:
        while task_running:
            frame = frames[i % len(frames)]
            sys.stdout.write(f"\r\033[34m{frame}\033[0m {message}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)
    finally:
        sys.stdout.write(f"\r\033[32m✓\033[0m {message}\n")
        sys.stdout.flush()
```

### 9.4 Respecting User Preferences

```python
def should_animate() -> bool:
    """Determine if animation is appropriate for this terminal."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") in ("dumb", "unknown"):
        return False
    if not sys.stdout.isatty():
        return False
    return True
```

---

## 10. Complete Design Token Specification for Terminal Use

These tokens form the complete terminal design vocabulary for agenthicc. Import and use these as constants; never hardcode ANSI sequences inline.

```python
# =============================================================================
# AGENTHICC TERMINAL DESIGN TOKENS
# Derived from: Linear, Cursor, Raycast, Warp, Vercel, Arc, GitHub, Notion,
#               VS Code, Figma design system research
# =============================================================================

# ── Reset ────────────────────────────────────────────────────────────────────
RESET = "\033[0m"

# ── Modifiers ────────────────────────────────────────────────────────────────
BOLD         = "\033[1m"
DIM          = "\033[2m"
ITALIC       = "\033[3m"
UNDERLINE    = "\033[4m"
BLINK        = "\033[5m"   # use sparingly / avoid
REVERSE      = "\033[7m"   # active selection
STRIKETHROUGH = "\033[9m"

# ── 16-Color Foregrounds (tier 1) ────────────────────────────────────────────
FG_BLACK    = "\033[30m"
FG_RED      = "\033[31m"   # ERROR
FG_GREEN    = "\033[32m"   # SUCCESS
FG_YELLOW   = "\033[33m"   # WARNING / ATTENTION
FG_BLUE     = "\033[34m"   # INFO / RUNNING
FG_MAGENTA  = "\033[35m"   # AGENT / AI ACTIONS
FG_CYAN     = "\033[36m"   # REVIEWER / DIFF HEADER
FG_WHITE    = "\033[37m"   # HEADER / EMPHASIS
FG_DEFAULT  = "\033[39m"   # reset to terminal default

# ── 16-Color Bright Foregrounds ───────────────────────────────────────────────
FG_BRIGHT_BLACK   = "\033[90m"  # dark gray (muted)
FG_BRIGHT_RED     = "\033[91m"  # bright error
FG_BRIGHT_GREEN   = "\033[92m"  # bright success
FG_BRIGHT_YELLOW  = "\033[93m"  # bright warning
FG_BRIGHT_BLUE    = "\033[94m"  # bright info
FG_BRIGHT_MAGENTA = "\033[95m"  # bright agent
FG_BRIGHT_CYAN    = "\033[96m"  # bright reviewer
FG_BRIGHT_WHITE   = "\033[97m"  # maximum emphasis

# ── 16-Color Backgrounds ─────────────────────────────────────────────────────
BG_BLACK    = "\033[40m"
BG_RED      = "\033[41m"   # error badge background
BG_GREEN    = "\033[42m"   # success badge background
BG_YELLOW   = "\033[43m"   # warning badge background
BG_BLUE     = "\033[44m"   # info badge background
BG_MAGENTA  = "\033[45m"   # agent badge background
BG_CYAN     = "\033[46m"
BG_WHITE    = "\033[47m"
BG_DEFAULT  = "\033[49m"   # reset to terminal default

# ── 256-Color Foregrounds (tier 2) ───────────────────────────────────────────
# Semantic colors with precise hex refs
FG_PRIMARY        = "\033[38;5;252m"  # #d0d0d0  main content
FG_SECONDARY      = "\033[38;5;245m"  # #8a8a8a  secondary text
FG_MUTED          = "\033[38;5;240m"  # #585858  disabled/placeholder
FG_BRIGHT_WHITE_256 = "\033[38;5;255m"  # #eeeeee headers

FG_SUCCESS_BRIGHT = "\033[38;5;77m"   # #5faf5f
FG_SUCCESS_MUTED  = "\033[38;5;22m"   # #005f00
FG_ERROR_BRIGHT   = "\033[38;5;196m"  # #ff0000
FG_ERROR_MUTED    = "\033[38;5;88m"   # #870000
FG_WARNING_BRIGHT = "\033[38;5;214m"  # #ffaf00
FG_WARNING_MUTED  = "\033[38;5;58m"   # #5f5f00
FG_INFO_BRIGHT    = "\033[38;5;75m"   # #5fafff
FG_INFO_MUTED     = "\033[38;5;24m"   # #005f87
FG_AGENT          = "\033[38;5;135m"  # #af5fff  AI/agent activity
FG_AGENT_MUTED    = "\033[38;5;53m"   # #5f005f
FG_REVIEWER       = "\033[38;5;80m"   # #5fd7d7  review/code analysis
FG_DIFF_ADD       = "\033[38;5;71m"   # #5faf5f  diff additions
FG_DIFF_DEL       = "\033[38;5;167m"  # #d75f5f  diff deletions
FG_DIFF_HEADER    = "\033[38;5;74m"   # #5fafd7  diff hunk headers
FG_DIFF_META      = "\033[38;5;241m"  # #626262  diff metadata/line numbers

# ── 256-Color Backgrounds (tier 2) ───────────────────────────────────────────
BG_SURFACE         = "\033[48;5;232m"  # #080808  base surface (deepest)
BG_SURFACE_RAISED  = "\033[48;5;234m"  # #1c1c1c  panels, code blocks
BG_SURFACE_ACTIVE  = "\033[48;5;236m"  # #303030  selected row
BG_SURFACE_HOVER   = "\033[48;5;237m"  # #3a3a3a  hover state
BG_DIFF_ADD        = "\033[48;5;22m"   # #005f00  diff addition lines
BG_DIFF_DEL        = "\033[48;5;88m"   # #870000  diff deletion lines

# ── Compound Semantic Tokens (ready-to-use) ───────────────────────────────────
# Text roles — inspired by GitHub Primer's semantic color roles
TEXT_HEADER    = f"{BOLD}{FG_BRIGHT_WHITE_256}"  # section headers
TEXT_PRIMARY   = f"{FG_PRIMARY}"                  # main content
TEXT_SECONDARY = f"{FG_SECONDARY}"                # supporting text
TEXT_MUTED     = f"{DIM}"                         # metadata, timestamps
TEXT_GHOST     = f"{FG_MUTED}"                    # disabled, placeholder
TEXT_LINK      = f"{UNDERLINE}{FG_INFO_BRIGHT}"   # hyperlinks, references
TEXT_CODE      = f"{BG_SURFACE_RAISED}{FG_PRIMARY}" # inline code

# Status tokens — inspired by Vercel deployment states
STATUS_SUCCESS  = f"{FG_GREEN}✓{RESET}"
STATUS_ERROR    = f"{FG_RED}✗{RESET}"
STATUS_WARNING  = f"{FG_YELLOW}⚠{RESET}"
STATUS_INFO     = f"{FG_BLUE}ℹ{RESET}"
STATUS_RUNNING  = f"{FG_BLUE}●{RESET}"
STATUS_PENDING  = f"{DIM}○{RESET}"
STATUS_BLOCKED  = f"{FG_RED}⊘{RESET}"
STATUS_SKIPPED  = f"{DIM}─{RESET}"
STATUS_MERGED   = f"{FG_MAGENTA}◉{RESET}"
STATUS_CANCELLED = f"{DIM}✗{RESET}"

# Agent tokens — inspired by Cursor's AI zone identity
AGENT_PLANNER   = f"{BOLD}{FG_MAGENTA}◆ agent:planner{RESET}"
AGENT_EXECUTOR  = f"{BOLD}{FG_BLUE}◆ agent:executor{RESET}"
AGENT_REVIEWER  = f"{BOLD}{FG_CYAN}◆ agent:reviewer{RESET}"
AGENT_SYSTEM    = f"{BOLD}{FG_WHITE}◇ system{RESET}"

# Diff tokens — directly from GitHub diff visualization
DIFF_ADD_LINE     = f"{FG_GREEN}+{RESET}"
DIFF_DEL_LINE     = f"{FG_RED}-{RESET}"
DIFF_CONTEXT_LINE = f"{FG_MUTED} {RESET}"
DIFF_HUNK_HEADER  = f"{FG_CYAN}"
DIFF_FILE_HEADER  = f"{BOLD}{FG_WHITE}"

# ── Borders and Dividers ──────────────────────────────────────────────────────
BORDER_HEAVY  = "\033[38;5;240m" + "═"   # use * terminal_width
BORDER_LIGHT  = "\033[38;5;237m" + "─"   # use * terminal_width
BORDER_DOT    = "\033[38;5;236m" + "·"   # between items

# Box drawing characters
BOX_TL, BOX_TR, BOX_BL, BOX_BR = "┌", "┐", "└", "┘"
BOX_H, BOX_V = "─", "│"
BOX_LT, BOX_RT, BOX_TB, BOX_BB = "├", "┤", "┬", "┴"
BOX_CROSS = "┼"
PIPE_L, PIPE_R = "│", "│"

# Hierarchy indicators (tree view, Notion-inspired indentation)
TREE_BRANCH = "├── "
TREE_LAST   = "└── "
TREE_PIPE   = "│   "
TREE_SPACE  = "    "

# ── Spinner Frames ────────────────────────────────────────────────────────────
# Braille pattern (Warp, VS Code activity bar)
SPINNER_BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
# Classic (16-color fallback)
SPINNER_CLASSIC = ["|", "/", "-", "\\"]
# Dots (compact)
SPINNER_DOTS    = ["   ", ".  ", ".. ", "..."]
# Growing block (progress-like)
SPINNER_BLOCKS  = ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]

# ── Progress Bar Components ───────────────────────────────────────────────────
PROGRESS_FILLED  = "█"
PROGRESS_PARTIAL = "▓"
PROGRESS_EMPTY   = "░"

# ── Layout Constants ──────────────────────────────────────────────────────────
INDENT_1 = " " * 2   # 2-space indent (space-1)
INDENT_2 = " " * 4   # 4-space indent (space-3)
INDENT_3 = " " * 8   # 8-space indent (space-4)
MAX_CONTENT_WIDTH = 100   # max line length
PROSE_WIDTH = 80          # comfortable reading width
STATUS_WIDTH = 9          # fixed-width status badge "[SUCCESS]"

# ── Spacing Patterns ──────────────────────────────────────────────────────────
LINE_BREAK      = "\n"    # single line break (space-1)
SECTION_BREAK   = "\n\n"  # double line break (space-group, Notion principle)

# ── ANSI Control ─────────────────────────────────────────────────────────────
CURSOR_UP    = "\033[A"
CURSOR_DOWN  = "\033[B"
CURSOR_RIGHT = "\033[C"
CURSOR_LEFT  = "\033[D"
CLEAR_LINE   = "\033[2K"
CLEAR_TO_EOL = "\033[0K"
SAVE_CURSOR  = "\033[s"
RESTORE_CURSOR = "\033[u"
HIDE_CURSOR  = "\033[?25l"
SHOW_CURSOR  = "\033[?25h"

# ── Utility Functions ─────────────────────────────────────────────────────────

def styled(text: str, *codes: str) -> str:
    """Apply ANSI style codes to text and reset."""
    return "".join(codes) + text + RESET


def header(text: str, width: int = 60, level: int = 1) -> str:
    """Render a section header at the given level."""
    if level == 1:
        line = BORDER_HEAVY + "═" * (width - 1)
        return f"{line}\n{BOLD}{FG_BRIGHT_WHITE_256}  {text}{RESET}\n{line}"
    elif level == 2:
        return f"{BOLD}{FG_BRIGHT_WHITE_256}{text}{RESET}\n{DIM}{'─' * min(len(text) + 2, width)}{RESET}"
    else:
        return f"{BOLD}{text}{RESET}"


def status_badge(status: str) -> str:
    """Render a fixed-width colored status badge."""
    BADGES = {
        "success":  f"{BG_GREEN}\033[30m SUCCESS {RESET}",
        "error":    f"{BG_RED}{FG_WHITE}  ERROR  {RESET}",
        "warning":  f"{BG_YELLOW}\033[30m WARNING {RESET}",
        "info":     f"{BG_BLUE}{FG_WHITE}  INFO   {RESET}",
        "pending":  f"{DIM}[PENDING]{RESET}",
        "running":  f"{BG_BLUE}{FG_WHITE} RUNNING {RESET}",
        "cancelled":f"{DIM}[CANCELL]{RESET}",
    }
    return BADGES.get(status, f"[{status.upper()[:7]:^7}]")


def progress_bar(pct: float, width: int = 30, color: str = FG_BLUE) -> str:
    """Render a horizontal progress bar."""
    filled = int(width * pct)
    empty = width - filled
    bar = PROGRESS_FILLED * filled + PROGRESS_EMPTY * empty
    return f"{color}[{bar}]{RESET} {int(pct * 100):3d}%"


def diff_line(line: str) -> str:
    """Render a single diff line with appropriate color."""
    if line.startswith("+++") or line.startswith("---"):
        return f"{DIFF_FILE_HEADER}{line}{RESET}"
    elif line.startswith("@@"):
        return f"{DIFF_HUNK_HEADER}{line}{RESET}"
    elif line.startswith("+"):
        return f"{FG_DIFF_ADD}{line}{RESET}"
    elif line.startswith("-"):
        return f"{FG_DIFF_DEL}{line}{RESET}"
    else:
        return f"{FG_DIFF_META}{line}{RESET}"


def agent_header(name: str, role: str = "executor", timestamp: str = "") -> str:
    """Render an agent turn header."""
    ROLE_COLORS = {
        "planner":  FG_MAGENTA,
        "executor": FG_BLUE,
        "reviewer": FG_CYAN,
        "system":   FG_WHITE,
    }
    color = ROLE_COLORS.get(role, FG_WHITE)
    ts = f"  {DIM}{timestamp}{RESET}" if timestamp else ""
    return (
        f"{BOLD}{color}◆ {name}{RESET}{ts}\n"
        f"{FG_MUTED}{'─' * 40}{RESET}"
    )


def tool_block(tool_name: str, command: str, output: str,
               exit_code: int = 0, duration: float = 0.0) -> str:
    """Render a Warp-inspired tool output block."""
    status = STATUS_SUCCESS if exit_code == 0 else STATUS_ERROR
    header_line = f"{DIM}┌─ tool:{tool_name} {'─' * max(0, 38 - len(tool_name))}┐{RESET}"
    cmd_line    = f"{DIM}│{RESET} {FG_SECONDARY}{command}{RESET}"
    out_lines   = "\n".join(f"{DIM}│{RESET} {line}" for line in output.splitlines())
    footer_line = f"{DIM}└─{RESET} {status} {DIM}exited {exit_code} · {duration:.2f}s{RESET}"
    return "\n".join([header_line, cmd_line, out_lines, footer_line])
```

---

## Appendix: Quick Reference Card

```
╔══════════════════════════════════════════════════════════════════╗
║           AGENTHICC TERMINAL DESIGN QUICK REFERENCE              ║
╠══════════════════════════════════════════════════════════════════╣
║  SEMANTIC COLORS (16-color safe)                                  ║
║  Success   \033[32m      Error    \033[31m      Warning  \033[33m      ║
║  Info      \033[34m      Agent    \033[35m      Review   \033[36m      ║
║  Header    \033[1;37m   Muted    \033[2m       Reset    \033[0m       ║
╠══════════════════════════════════════════════════════════════════╣
║  STATUS SYMBOLS                                                    ║
║  ✓  success   ✗  error    ⚠  warning   ●  running                ║
║  ○  pending   ─  skipped  ⊘  blocked   ◉  merged                 ║
╠══════════════════════════════════════════════════════════════════╣
║  SPINNER FRAMES (Braille)                                          ║
║  ⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏  (10-frame, 100ms each)                ║
╠══════════════════════════════════════════════════════════════════╣
║  TYPOGRAPHY HIERARCHY                                              ║
║  L1 Header:   \033[1;37m Bold White                                ║
║  L2 Content:  Default foreground                                   ║
║  L3 Meta:     \033[2m Dim                                          ║
║  L4 Ghost:    \033[38;5;240m Dark gray                             ║
╠══════════════════════════════════════════════════════════════════╣
║  SPACING                                                           ║
║  Item gap:    1 blank line    Section gap: 2 blank lines           ║
║  Indent-1:    2 spaces        Indent-2:    4 spaces                ║
║  Max width:   100 chars       Prose width: 80 chars                ║
╠══════════════════════════════════════════════════════════════════╣
║  BOX DRAWING                                                       ║
║  ┌─────┐    ├── child     ╔═════╗  heavy borders                  ║
║  │     │    │   ├── sub   ║     ║                                  ║
║  └─────┘    │   └── last  ╚═════╝                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

---

*Research sources: Linear.app design analysis (LogRocket, DesignMD); Raycast design documentation (VoltAgent awesome-design-md); Vercel Geist design system (seedflip.co, vercel.com/geist); GitHub Primer design system (primer.style); VS Code theming documentation (code.visualstudio.com); Warp terminal theme design blog (warp.dev/blog); Arc browser UX analysis (LogRocket, Medium); Notion design system (DesignMD, Medium); Figma dark mode and workspace design (Figma blog, Design+Code); TUI design best practices (blog.tng.sh, tui.studio).*
