# PostDelegate: Postiz-inspired UI spec

Status: draft for owner review. Not committed. Written 2026-10-24.
Scope: look, feel and interaction model only. PostDelegate keeps its own name, logo, Orbit theme (brand-500 `#7357FF`, brand-600 `#6545ED`, lime accent `#C8FF4D`), Manrope + Noto Sans Arabic, RTL support and WCAG 2.2 AA token discipline.

Sources read for this spec
- Postiz frontend (Next.js + Tailwind 3 + Mantine/dnd): `/Users/naserkhanjar/Projects/PostDelegate/apps/frontend` (referred to below as `PZ:`). Note: this checkout is the Postiz fork already carrying PostDelegate tokens (`--brand-*` in colors.scss), so colour values quoted below are the dark/light `--new-*` roles as they exist there. Structure, spacing and behaviour are unchanged Postiz.
- PostDelegate Django app: `/Users/naserkhanjar/Projects/postdelegate-studio` (referred to below as `PD:`), CSS source `theme/static_src/src/styles.css`, overrides `theme/static/css/postdelegate.css`.
- Not done: no upstream GitHub/docs screenshots were fetched and no logged-in session of app.postdelegate.com was inspected. PD's current look was taken from its templates and CSS. Visual verification (screenshots of both) should be the first step of WP1.

Important PD facts that shape every work package
- PD is dark-only today (`html[data-theme="postdelegate"] { color-scheme: dark }`, postdelegate.css:19). Many templates still use inherited light classes (`bg-white`, `border-stone-200`, `text-orange-600`); `postdelegate.css` lines 43-88 remap them to Orbit tokens. New work must use Orbit tokens directly (`bg-surface-1`, `border-line`, `text-ink-2`, `text-accent`), never new stone/orange classes. Postiz has dark and light (`.dark` / `.light` in `PZ:src/app/colors.scss`); a light theme in PD is out of scope unless the owner asks, so "dark/light" acceptance below applies only if a light theme is introduced.
- Frameworks: Alpine + HTMX only. SortableJS is already vendored (`static/js/vendor`) and used by the calendar.
- Shared components that must be reused: `{% modal %}` (`templates/components/modal.html`, sizes sm/md/lg, focus trap via `x-pd-dialog`), `{% icon_button %}` (`templates/components/icon_button.html`, sizes sm 28 / md 36 / lg 44), `{% ui_select %}` (`ui_select.html`), `_confirm_dialog.html`, `_icon.html`, `.pd-popover`, `.pd-icon-btn`, `.pd-toast`.

---

## 1. Postiz design system, distilled

### 1.1 Layout grid (desktop)

Source: `PZ:src/components/new-layout/layout.component.tsx`.

```
body (bg newBgColor #080A0F dark)      padding 12px on all sides, gap 8px between panels
+--------+------------------------------------------------------------+
| rail   | main panel  (rounded-[12px], bg newBgLineColor, gap 1px)    |
| 80px   |  +------------------------------------------------------+  |
| rounded|  | top bar  h-[80px] px-[20px]  bg newBgColorInner      |  |
| 12px   |  |  title (24px/600)  ........  icon cluster            |  |
| bg     |  +------------------------------------------------------+  |
| inner  |  | page content, flex gap-[1px] (1px hairlines between   |  |
|        |  | sub-panels: channel list | calendar)                  |  |
+--------+------------------------------------------------------------+
```

- The whole app is a set of floating rounded cards on the darkest background, separated by 8px gutters or 1px hairlines. No full-bleed sidebars, no borders on the cards themselves. This is the single biggest visual difference from PD (PD: flat sidebar on page colour, one bordered main card `.pd-main`, `margin 0.625rem`, `radius-xl`, border 1px, styles.css ~line 955).
- Left rail: 80px wide card, `bg-newBgColorInner`, `rounded-[12px]`, inner fixed column 64px wide (`start-[17px]`). Logo block 60x60 at top, then `TopMenu`.
- Menu item (`PZ:new-layout/menu-item.tsx`): vertical stack, icon (21-23px) over 10px label (9px on short viewports via `custom:` breakpoint), item height 54px (44px on short screens), `rounded-[12px]`, weight 600, `py-[8px] px-[6px]`, gap 4px. Active and hover: `bg-boxFocused text-textItemFocused` (dark: `#F7F7F2` pill with dark text; light: `#ebe8ff` with `#3900b2`). Inactive text `textItemBlur` (`#9299AA`). `transition-colors`, icon `scale-90` on short screens.
- Top bar (80px): left = page title (`Title` component, 24px/600); right = cluster of 20px-gap icons separated by 1px x 20px `bg-blockSeparator` dividers: streak, organization/workspace selector, theme toggle, language, extension, feedback, notifications.
- Calendar page (`PZ:launches/launches.component.tsx`): two sub-panels inside the main card, both `bg-newBgColorInner p-[20px]`.
  - Channel rail: 260px wide (100px when collapsed via the 24x24 chevron button, state in a cookie/`collapseMenu`). Header "Channels" 20px/500. Buttons stack: "Add Channel" (full width), then "Create Post" + "Generate" side by side. Groups by customer, each group collapsible.
  - Calendar area: toolbar (`Filters`, h-[42px] controls) above the grid.
- Mobile: Postiz collapses the rail to a bottom/overlay menu (`mobile.integration.tsx`); the calendar is usable but not a primary mobile target. PD's mobile (drawer sidebar below lg, agenda-style week/month) is already better; keep it.

### 1.2 Spacing, radii, shadows, typography

Spacing is written in explicit pixels (`gap-[8px]`, `p-[20px]`). Scale in use: 2.5, 4, 5, 6, 8, 10, 12, 15, 16, 20, 24, 32, 40. Most frequent: panel padding 20, gutters 8-12, intra-control gap 8-10, section gap 32 (channel groups), modal padding 32.

| Element | Postiz value | File |
|---|---|---|
| Panel / card radius | 12px | layout.component.tsx |
| Post modal outer | 20px, modal wrapper 24px | manage.modal.tsx:473, modal.wrapper.component.tsx:49 |
| Buttons / inputs / tabs | 8px (6px for small chevron buttons, 4px legacy) | manage.modal.tsx, filters.tsx |
| Calendar chip | 10px (top 10px tab bar, bottom 10px body); avatars 8px (not circles) | calendar.tsx:1105-1181 |
| Channel avatar | 36px, `rounded-[8px]`, 18px platform badge at bottom-end (-5px offset) with 1px border | launches.component.tsx:293-310 |
| Selected-channel chip in editor | 40x40, 26px round avatar, 12px badge; selected = 1px `#8D78FF` border | select.current.tsx:100-180 |
| Shadows | Almost none. `--menu-shadow` for popovers (dark `0 8px 30px rgba(0,0,0,.5)`), `--preview-box-shadow` none in dark. Toast has coloured glow (`shadow-greenToast`, `shadow-yellowToast`) | colors.scss, tailwind.config.cjs |
| Font | Plus Jakarta Sans (layout.component.tsx) in the app; PD fork body font Manrope (tailwind.config.cjs `sans`) | |
| Sizes | Page title 24/600; modal title 20/600; section title 20/500; body 14; calendar chip 14 body + 11 tag strip; table header 14/500; small labels 10-12; buttons 15/600 | |
| Density | Airy: 40-44px controls, 62px calendar header cells, 70px min month-cell height, 4px grid gaps | calendar.tsx:375, 849 |

### 1.3 Colour roles and PD token mapping

Postiz roles (Tailwind names from `PZ:tailwind.config.cjs`, values from `colors.scss` `.dark`):

| Role | Postiz token (dark / light) | PD token (Orbit, already in styles.css) | Notes |
|---|---|---|---|
| App background | `newBgColor` `#080A0F` / `#f0f2f4` | `--surface-page` `#080a0f`, `bg-surface-0`... use `var(--surface-page)` | identical dark value |
| Card / panel | `newBgColorInner` `#11141C` / `#fff` | `--surface-0` `#11131b` (`bg-surface-0`) | |
| Hairline between panels | `newBgLineColor` `#2A2F3B` | `--border` `#2a2e3c` (`border-line`) | |
| Raised / input / hover | `newSettings` `#191D27`, `boxHover` `#191D27`, `btnSimple` `#202530` | `--surface-1` `#151823`, `--surface-2` `#1b1f2b`, `--surface-3` `#252b3a` | |
| Calendar header / table | `newTableHeader` `#191D27`, `newTableBorder` `#2A2F3B` | `--surface-1`, `--border` | |
| Chip body | `newColColor` `#202530` | `--surface-2` | |
| Text primary | `newTextColor` `#F7F7F2` | `--text-primary` `#f8f7f2` (`text-ink`) | |
| Text muted / inactive nav | `textItemBlur` / `newTableText` `#9299AA` | `--text-secondary` `#b0b6c7` (`text-ink-2`) or `--text-tertiary` `#929bad` (`text-ink-3`) | use ink-3 for the exact Postiz grey |
| Active nav fill | `boxFocused` `#F7F7F2` + text `#191D27` | `--primary-soft` + `--brand-700` (current) or invert: `bg-[var(--text-primary)] text-[var(--surface-1)]` | owner decision, see 1.4 |
| Primary | `btnPrimary` `#7357FF` | `--primary` `#7357ff` / `--primary-hover` `#6545ed` | same hue; white text on brand-500 is 4.63:1, passes |
| Primary text on dark | `newTableTextFocused` `#8D78FF` | `--text-brand` `#a58aff` (`text-brand`) | do not use raw brand-500 as small text (4.0:1) |
| Accent | `brand.accent` `#C8FF4D` | `--brand-green-500` (focus ring, kicker) | |
| Success | `#72DCA4` (`brand.success`), toast icon `#6CE9A6` | `--success-500/700/50` | |
| Warning | `#F4BF50`, toast icon `#FEC84B` | `--warning-500/700/50` | |
| Error / delete | `#FF5F74`, `red-500`, `#FF3F3F` delete text | `--error-500/700/50` | |
| Info | `#64B5FF` | `--info-500/700/50` | |
| Backdrop | `popup` `rgba(65,64,66,.3)` | `rgba(0,0,0,.6)` (`.pd-backdrop`, `backdrop:bg-black/60`) | keep PD's |
| Per-network preview skins | `bgLinkedin`, `bgFacebook`, `bgInstagram`, `bgYoutube`, etc. | `[data-platform-preview]` scoped skins, exempt from remaps | keep |
| Post status colours | Not colour-coded in Postiz: chip is `newColColor` with tag colour on the top strip, red ring+badge for ERROR, grayscale+strip pattern for past | `.status-*` classes in `calendar.html:126-143` | PD is richer; keep status colours, adopt Postiz chip anatomy |

### 1.4 Decisions for the owner (flag, then proceed with the default)

1. Active nav colour: Postiz inverts (light pill, dark text). Default: keep PD's purple-soft pill (`primary-soft` + `brand-700`), because it is on-brand and already AA. Offer the inverted variant only if the owner wants a closer copy.
2. Rail style: icon-over-label rail (Postiz) vs text sidebar (PD). Default: ship an icon-over-label rail at lg+ as the collapsed form and keep the 15rem text sidebar as the expanded form (see WP1). Both exist in PD already via `sidebarCollapsed`; the new work is restyling the collapsed state, not removing the text one.

### 1.5 Icon set

Postiz uses inline hand-pasted SVGs (mostly Untitled-UI-style outlines, 1.5-1.8 stroke, `currentColor`) in `PZ:src/components/ui/icons/index.tsx` (32 exports) and in `menu-item` definitions in `PZ:layout/top.menu.tsx`, plus react-icons-style glyphs. Network logos are PNGs in `PZ:public/icons/platforms/*.png` (36 files, square, rounded 8px by CSS) and `youtube.svg`.
PD already uses inline Lucide/Feather-style 24px outline SVGs at stroke 2 (`templates/base.html` nav) and an icon sprite helper `templates/components/_icon.html`. Keep this set. Do not import Postiz's icon file; draw/choose the few missing glyphs in PD's style (stroke 1.8-2, `currentColor`, 18-21px).

---

## 2. Screen-by-screen comparison

Format: Postiz behaviour (file) / PD today (file) / gap and recommendation.

### 2.1 App shell and navigation
- Postiz: floating-card shell, 80px icon rail, 80px top bar with title and icon cluster (layout.component.tsx, menu-item.tsx, top.menu.tsx). Items: Launches/Calendar, Agent, Analytics, Media, Plugs, Settings, plus Support and billing.
- PD: `templates/base.html` lines ~112-760. `.pd-shell` flex, `.pd-sidebar` 15rem (3.75rem collapsed) on `--surface-page`, workspace switcher at top (base.html ~142-225), "New" split button (~228), nav list with `.sidebar-nav-item` (styles.css 244-262, 13px/500, radius-md, active = `primary-soft`), section labels, org footer. Mobile drawer with `x-pd-dialog`. No desktop top bar (`.pd-topbar` is `lg:hidden`, base.html:745). Page titles live inside each page body.
- Gap: no persistent top bar with title and global actions; sidebar items are denser (13px, 18px icons) than Postiz's airy rail; main card has a border and 1.5rem padding instead of floating panels with 8px gutters.
- Recommendation: (a) add a desktop `.pd-header` (h-[64px], not 80, to protect vertical space for the composer and grid) rendered by `base.html` above `.pd-main`, with `{% block page_title %}` on the left and a right-hand icon cluster (workspace switcher moves here as a pill, notifications, help, theme n/a, user menu) separated by 1px x 20px `bg-[var(--border)]` dividers. (b) Change `.pd-shell` to `padding: 12px; gap: 8px` at lg+ and render sidebar and main as two rounded-12 cards (`radius-xl` = 0.75rem already equals 12px) with no outer border on the sidebar, `background: var(--surface-0)` for both, page background `--surface-page`. (c) Collapsed sidebar becomes the Postiz-style icon-over-10px-label rail (new class `.sidebar-rail`) at 4.5rem; keep tooltips from `title`.

### 2.2 Channel list and avatars (incl. Add channel, customer selector)
- Postiz: persistent 260px (100px collapsed) column on the calendar page. Avatar 36px `rounded-[8px]` + 18px platform badge; name truncates; hover reveals a 4px accent line at start (`SVGLine`) and `bg-boxHover`; row is draggable onto calendar slots? (the row `useDrag type 'menu'` is for reordering/grouping by customer). Disabled channels at 50% opacity with plan tooltip; "refresh needed" = red `!` dot + purple 60% overlay over the avatar, click to reconnect; per-row `…` menu (preferences, move to group, edit time slots, disable, customer, etc. in `launches/menu/menu.tsx`). Groups by customer with collapsible headers (`MenuGroupComponent`), `select.customer.tsx` + `customer.modal.tsx`. Empty state: illustration + "No channels yet / connect your accounts".
- PD: channels are not on the calendar page. They live in Settings, `templates/social_accounts/list.html` as wide cards (`_account_card.html`: 44px circular avatar, status badge, followers, 4-5 text buttons, `max-w-4xl`). Customer concept maps to PD "account sets" (`templates/account_sets/`) and workspaces. Calendar only has a channel multi-select filter (`publish_calendar_shell.html` `ui_select ... channels`).
- Gap: biggest functional-UX gap. Users cannot see, reconnect, or add channels from the calendar.
- Recommendation: new partial `templates/calendar/partials/_channel_rail.html` rendered left of `#calendar-content` at xl+ (260px, collapsible to 72px with `localStorage` key `channelRailCollapsed`, same pattern as `sidebarCollapsed` in base.html:94-98). Row anatomy: 36px avatar `rounded-lg` + 18px platform badge (reuse `social_accounts/partials/_platform_icon.html` for the badge, not PNG), name, status dot; hover `bg-surface-2` + 3px `bg-[var(--primary)]` leading bar; reconnect state uses `.status-failed` tokens with a red `!` dot; row menu via existing `_account_card.html` actions moved into a `.pd-popover`. Top of rail: "Connect channel" (links to `social_accounts:connect`) and "Create post" buttons. Group by account set if any. Row click toggles the calendar's channel filter (`calendarFilters.channels`) which already exists. Below xl it becomes a horizontally scrollable avatar strip above the toolbar. Keep `social_accounts/list.html` as the management page but restyle `_account_card.html` as compact rows (avatar 36 rounded-lg, 56px high) matching the rail.

### 2.3 Calendar (day / week / month / list)
- Postiz (`PZ:launches/calendar.tsx`, `filters.tsx`, `calendar.context.tsx`):
  - Toolbar (filters.tsx:290-500): prev/next chevrons joined in one 42px-high segmented control (`border-newTableBorder`, `gap-[1px]`, hover = `boxFocused`), centred period label (min-w-200), "Today" button 42px, `Customer` selector, view switcher as a segmented pill group (`p-[4px] border rounded-[8px]`, active item filled), Day/Week/Month/List, plus a preferences menu.
  - Week grid (calendar.tsx:375): `grid-cols-[136px_repeat(7,1fr)] gap-[4px]`, sticky 62px header cells `bg-newTableHeader rounded-[8px]` with weekday 14/500 `newTableText` and a 6px primary dot for today. Hour rows with a 136px time column. Each cell: 1px `newTextColor/5` border radius 8px, hover on an empty future cell shows a centred purple 40x40 rounded square containing "+" (`group-hover:before:content-["+"]`, `bg-btnPrimary`). Past cells: diagonal `.repeated-strip` pattern (global.scss:750), `cursor-not-allowed`, "Date passed" label on hover (`.col-calendar:hover:before`).
  - Month grid: 7 columns, 62px header row, day number top-left 14px, cells min-h 70, each shows up to 3 chips then "+ Show more (n)" link; click empty area = create.
  - Day view: hour rows; hovering an empty slot shows the avatars of every channel at 30% opacity and greyscale (full colour on hover) as a "who will it go to" hint.
  - Chip (calendar.tsx:1100-1210): two-part pill. Top strip 24px high, background = tag colour or primary, shows tag names; on hover the tag text swaps for 16-20px action icons: Duplicate, Preview (opens `/p/<id>?share=true`), Statistics, Delete. Body 14px `bg-newColColor`, 20px avatar + 12px network badge, one-line clipped content prefixed "Draft: ", time at end (HH:mm or hh:mm A for US). ERROR = `ring-2 ring-red-500` + red `!` badge top-start with tooltip of the error; creation-method badge bottom-end. Dragging sets opacity 0 on the source and highlights target `border border-[#7357FF]` (react-dnd `useDrop`, `canDrop`). Loading = `animate-pulse` over the cell with a `bg-newSettings` skeleton.
  - List view (`/posts-list`): flat paginated list, same chips.
- PD (`templates/calendar/partials/*`, `calendar.html`): already has Month/Week/Day/List shells, SortableJS drag-and-drop with `cal-drop-active` outline, keyboard/touch "Reschedule…" dialog, phone agenda and compact month, saved views, bulk select, status legend, best-time chips, channel posting-slot rows (`_grid_slot.html`), per-cell `+` links (`.cal-add-btn`). Toolbar is `bg-white border-b border-stone-200` with a Month/Week/Day dropdown (not a segmented control) and small 12px text controls (publish_calendar_shell.html:14-80). Chips are 11px single-line pills with status colours (calendar.html:113-143).
- Gap and recommendation (visual only, preserve all PD behaviour):
  1. Toolbar: replace the view dropdown with a segmented control Day | Week | Month | List (`role="tablist"` or radio group, 36px high, `p-1 border border-line rounded-lg`, active `bg-surface-3 text-ink`), join prev/next into one 36px segmented button, bump control text from 12px to 14px/500, keep Today and filters. Use tokens, drop `bg-white/stone` classes in these partials.
  2. Week/day grid: header cells as rounded-lg `bg-surface-1` sticky 56px tiles with a 6px primary dot for today (PD today uses a filled circle on the date number; keep that, it is clearer); give hour cells 4px gaps (`gap-1`) instead of border lines, `radius-lg`, `border border-line/50`; widen time column from 72px to 88px.
  3. Empty-slot affordance: hovering an empty future cell shows a centred 36x36 rounded-lg `bg-[var(--primary)]` tile with "+" (replace `.cal-add-btn` small icon; keep the `<a href>` so it stays keyboard-focusable, show on `:focus-visible` too). In day view show channel avatars at 30% grayscale that go to full colour on hover (new, optional).
  4. Past cells: add the diagonal stripe pattern and a "Date passed" hover label (new `.cal-past` class; pattern `repeating-linear-gradient(135deg, var(--surface-0) 0 4px, var(--surface-1) 4px 8px)`), purely decorative with `aria-hidden`; keep `Open` label for past open slots.
  5. Chip anatomy: switch to two-part chip: 20px top strip (tag colour, tag name; falls back to status colour) + body with 20px rounded-md avatar, 12px platform badge, one-line text, time. Hover on the strip reveals icon actions: Duplicate, Preview, Reschedule (existing `.chip-resched` moves here), Delete (opens existing `_delete_post_modal.html`). Failed chips: `ring-2 ring-[var(--error-500)]` plus a 18px red `!` badge with `title` of `pp.error` and an `sr-only` message. Keep `.status-*` colours as the dot/left border (4px `border-inline-start`) so status is still conveyed without relying on fill colour.
  6. Month cells: show max 3 chips, then "+ Show more (n)" as a `<button>` that expands inline; day number top-start 14px.
  7. Loading: swap the HTMX content with `.htmx-request` `animate-pulse` skeleton cells (`bg-surface-1 rounded-lg`) instead of leaving stale content.
- Files: `templates/calendar/partials/publish_calendar_shell.html`, `week_grid.html`, `day_grid.html`, `month_grid.html`, `_grid_chip.html`, `_grid_slot.html`, `list_view.html`, `_select_checkbox.html`; CSS block in `templates/calendar/calendar.html:100-370` (move new chip/cell CSS to `theme/static_src/src/styles.css` under a `/* calendar */` section if it grows).

### 2.4 Post editor / composer
- Postiz (`PZ:new-launch/manage.modal.tsx`, `select.current.tsx`, `picks.socials.component.tsx`, `editor.tsx`):
  - Opens as a large modal (p-[40px] around a `rounded-[20px]` `bg-newBgColorInner` card): left column editor (flex-1), right 580px "Post Preview" column; vertical 1px `border-newBorder` divider; both columns have a 65px `bg-newBgColor` header strip with 20px/600 title ("Create Post" / "Post Preview") and a close X on the right.
  - Channel picker row at the top: 40x40 avatar buttons with network badge; unselected avatars greyscale/dim, selected get purple ring. Selecting channels adds them to a second row of "selected channel" tabs (`select.current.tsx`): first tab "Global" (the shared editor), then one 40x40 tab per channel with a tiny red X to remove and a "custom" indicator (`IsGlobal`) when a channel has diverged; selected tab = 1px `#8D78FF` border. "Edit content" per channel enters custom mode; "Back to global" reverts (confirm "Yes, go back to global mode").
  - Editor (`editor.tsx`): rich text, toolbar with bold/underline/lists, emoji picker (tooltip "Insert Emoji"), AI helper (`editor-ai-bindings.tsx`), mentions (`mention.component.tsx`), signature insert, character counter per network, media attachments strip with drag-to-reorder, drop zone ("Drop your files here to upload"), thread posts (`add.post.button.tsx`, delay between thread parts `delay.component.tsx`), per-provider settings panel (`providers/<network>`), tags (`tags.component.tsx`), repeat (`repeat.component.tsx`), date picker.
  - Footer (84px, border-t): left: date picker, repeat, tags; right: red "Delete Post" text button, "Save as Draft" (btnSimple, h-44, rounded-8, 15/600), primary split button "Add to calendar / Schedule / Update" (`bg-[#7357FF]`, h-44, min-w-180, rounded-8) with a hover dropdown above it containing an orange-brown "Post Now" item (`#B4552D`).
  - Confirm dialogs on close with unsaved changes (`askClose` -> "Are you sure you want to close?" "Yes, close it!").
- PD (`templates/composer/compose.html`, 3750 lines; partials in `templates/composer/partials/`):
  - Full page, not a modal: header row (back button, "Create"/"Edit", mobile Preview toggle), centre editor column (`overflow-y-auto p-4 sm:p-6`), right `.panel-right` preview drawer `bg-stone-50` (compose.html:1844) that becomes a modal dialog below lg. `@keydown.meta.s`/`ctrl.s` save draft. Platforms picker is in the header area, per-account overrides are blocks inside the editor (`override-caption-<accId>`), `thread_editor.html`, `insert_toolbar.html` (emoji, hashtags, mentions), `media_picker.html`, `repeat_panel.html`, `template_picker.html`, `preview_panel.html`/`preview_card.html` (HTMX re-render on `previewUpdate`), `idea_card.html`, AI writer in `templates/ai_writer`. Unsaved-changes guard in `static/js/composer-unsaved.js`.
- Gap: PD has the feature depth, but the layout is one long scroll with per-network override blocks; Postiz's "Global tab + one tab per selected channel" gives a clearer mental model and smaller visual load. Preview in Postiz is wider (580px) and sits in an equal-weight pane with a header.
- Recommendation (restructure UI, do not change form field names or backend):
  1. Page frame: keep a full page (HTMX/Alpine, no new modal route) but use the Postiz two-pane card: wrap in `.pd-composer` = one `rounded-2xl bg-surface-0` card, left pane header 56px `bg-surface-page` with title, right pane 480-560px with header "Post preview". Footer sticks to the card bottom (72px, `border-t border-line`).
  2. Channel strip: top of the left pane, two rows: (a) all connected channels as 40x40 toggle avatars (`aria-pressed`, unselected `grayscale opacity-60`, selected ring `ring-2 ring-[var(--primary)]`), (b) tabs `role="tablist"`: "All channels" (global) + one tab per selected channel with a "customised" dot when an override exists and an `x` remove. Selecting a tab shows only that channel's override fields (re-use existing `override-caption-*` blocks via `x-show="activeTab === accId"`), the global tab shows the main caption. Keyboard: arrow keys move between tabs. This is mostly Alpine state (`activeTab`) added to `composerApp()`.
  3. Footer: left = schedule date/time + repeat + tags; right = destructive text "Delete" (error-700), secondary "Save draft" (`btn-outline`), primary split button "Schedule" with menu item "Post now" (confirm via `_confirm_dialog.html`, use `warning` tokens, not a custom brown). Keep `@keydown.ctrl.s`.
  4. Editor chrome: toolbar row directly under the caption with icon buttons (`{% icon_button size="sm" %}`) for emoji, hashtags, mention, AI, media, link; per-network character counter pills `bg-surface-2` turning `text-bad` when over limit (already computed in `charLimits`).
  5. Media: keep `media_picker.html`; add a visible drop zone with the Postiz copy "Drop your files here to upload" style and drag-reorder of attachments via SortableJS (already vendored).
  6. Preview: sticky pane, one card per selected network (`preview_card.html`), skinned with `[data-platform-preview]`; header chip shows the count of platforms.
  7. AI button: place a purple `bg-[var(--primary)]` "AI" icon button in the toolbar (Postiz `ai` colour role equals primary), opening the existing AI writer popover. Remember product rule: AI is bring-your-own key only (see project memory); show the "connect your key" state when no key is configured.

### 2.5 Post preview modal
- Postiz: calendar chip Preview icon opens `/p/<id>?share=true` in a new tab (`PZ:preview/preview.wrapper.tsx`, `comments.components.tsx`): a standalone, shareable page with the post rendered and a comment thread for client feedback, `border-preview` and `preview-box-shadow` in light mode.
- PD: approvals/client portal already cover this (`templates/approvals/link/`, `templates/client_portal/`, `templates/composer/partials/approver_previews.html`).
- Recommendation: add a lightweight "Preview" modal from the new chip hover action using `{% modal size="lg" %}` that loads `composer/partials/preview_card.html` via `hx-get` (new GET endpoint in `apps/composer` or `apps/calendar`; if too costly, link to the existing approval preview). Ship after WP2; low priority.

### 2.6 Analytics
- Postiz (`PZ:components/platform-analytics/*`, `analytics/*`): channel selector on the left (same avatar rows), right pane with date-range dropdown (7/30/90 days) and one card per metric with a line/area chart and a big number; grid of 2-3 cards, `bg-newBgColorInner`/`newSettings`, 12px radius.
- PD: much richer (`templates/analytics/`: `_kpi_card`, `_hero_chart`, `_delta_chip`, `_range_controls`, `_post_table`, `compare.html`, heatmap tokens `--heat-*`, chart tokens `--chart-1..4` validated for contrast).
- Recommendation: do not rebuild. Restyle only: cards `rounded-xl bg-surface-1` without double borders, 20px padding, KPI number 28/700, range control as the same segmented control as the calendar toolbar (WP2 component). Reuse the new channel rail as the channel selector (WP3).

### 2.7 Settings and team
- Postiz: Settings page uses top tabs (Team, Webhooks, Auto Post, Signatures, Public API, Sets...) inside the main card; Team tab (`settings/teams.component.tsx`) = heading 20px, helper text, invite box `bg-sixth border-fifth rounded-[4px] p-[24px]`, member table.
- PD: left-sidebar settings shell (`templates/layouts/settings.html` swaps the nav; `layouts/workspace_settings.html`), pages under `templates/accounts`, `members/list.html`, `organizations`, `workspaces`, `api_keys`, `webhooks`, `billing`.
- Recommendation: keep PD's sidebar-swap navigation (better for 20+ sections). Adopt only Postiz's content styling: page header (title 20/600 + 14px muted helper), content in `rounded-xl bg-surface-1 p-6` panels, tables with `bg-surface-1` header row and `border-line` row separators, and invite forms as inline panels. Files: `templates/members/list.html`, `templates/workspaces/settings.html`, `templates/api_keys/list.html`, `templates/webhooks/*`.

### 2.8 Media library
- Postiz: a modal (`PZ:media/media.component.tsx`) opened from the editor: search input ("Search by file name"), Upload button, paged grid, multi-select with "Add selected media", footer Previous/Next; plus a dedicated /media page (`src/app/(app)/(site)/media`). Also "Design Media" (Polonto editor, `launches/polonto*`) and AI image/video generators.
- PD: `templates/media_library/` (folders `_folder_tree`, tags, versions, asset detail panel, shared library), already richer.
- Recommendation: restyle only; make `media_picker.html` in the composer behave like Postiz's picker modal (search + upload on one row, selectable tiles with a purple check overlay, sticky footer "Cancel / Add selected media (n)"). No Polonto equivalent (out of scope).

### 2.9 Empty states and onboarding
- Postiz: "No channels yet" empty state with a 125x96 line illustration (`public/no-channels.svg` dark, `no-channels-colors.svg` light), 20/600 title, 14px caption, shown inside the channel rail; first-run `Onboarding` overlay/modal (`onboarding/onboarding.modal.tsx`), a streak counter in the top bar, top tip tooltips (`layout/top.tip.tsx`).
- PD: `social_accounts/partials/_empty.html`, `calendar/partials/_no_channels_empty_state.html`, `analytics/_no_analytics_state.html`, `media_library/_empty_state.html`, `{% onboarding_hint %}` tag, `templates/onboarding/welcome`.
- Recommendation: standardise all empty states on one shape: 64px tinted rounded-2xl icon tile (`bg-[var(--primary-soft)]`), 18/600 title, 14px `text-ink-2` max-w-sm body, one primary action. Create one include `templates/components/_empty_state.html` (arguments: icon, title, body, cta_label, cta_url) and migrate the five templates above. For the calendar-with-no-channels case, show the empty state inside the channel rail and a dimmed grid behind it.

### 2.10 Login and signup
- Postiz: split layout, form column with 40px/500 heading, 14px helper, social login buttons, "or" divider (1px `bg-fifth` with centred label), testimonial carousel on the right over `bg-login.png` (`PZ:auth/login.tsx`, `testimonial.component.tsx`, `public/auth/`).
- PD: already a custom two-column `.pd-auth` card (styles.css ~160-230: 1120px max, radius 28px, story panel with radial brand gradient and lime kicker, `templates/account/login.html`, `signup.html`).
- Recommendation: no change beyond polishing the divider ("or", 1px `--border` line) and ensuring 375px layout stacks (it does at max-width 520px). Do not import Postiz's testimonial component or avatars.

---

## 3. Interaction details worth copying

| Interaction | Postiz behaviour | PD implementation note |
|---|---|---|
| Nav hover | `transition-colors`; bg and text colour swap, icon unchanged | `.sidebar-nav-item` already uses `--dur-fast` 150ms; keep |
| Channel row hover | 4px primary bar slides in at start, row bg `boxHover` | CSS `:hover::before` width/opacity transition 150ms, `inset-inline-start` for RTL |
| Chip hover | Tag text replaced by action icons in the strip | CSS `.group:hover .chip-tags {display:none}`; also reveal on `:focus-within` so keyboard users get the actions |
| Drag | react-dnd; source fades to 0 opacity, target cell gets 1px primary border, past cells reject (`canDrop`) | PD: Sortable `ghostClass: chip-ghost` (display none) + `.cal-drop-active` outline is equivalent; add `cursor: not-allowed` and no outline over past cells; keep Reschedule dialog as the accessible alternative |
| Empty slot "+" | Centred 40px primary tile on hover | see 2.3 item 3; must also appear on `:focus-visible` |
| Past cells | Striped pattern + "Date passed" label | decorative only, `aria-hidden` |
| Toasts | Top-centre, 56px high, `animate-fadeDown` 4s, dark `customColor8` bg, green or amber icon with matching glow (`toaster.tsx`) | PD toast (`.pd-toast`, top-end, error only, styles.css ~1015). Add success variant (`--success-50`/`--success-500`), `role="status"` for success and `role="alert"` for error; auto-dismiss 4s success, sticky error; keep `data-no-error-toast` opt-out |
| Loading | `animate-pulse` skeleton cell; button spinner `animate-spin h-20 w-20 border-4 border-t-transparent` replaces label (`invisible` keeps width) | Use `htmx-indicator` + `.is-loading` on buttons; respect `prefers-reduced-motion` (PD already disables transitions in calendar.html:271) |
| Modals | Stacked modal manager; Esc closes only the top one; "askClose" confirm for dirty forms | `{% modal %}` already traps focus and handles Esc; `composer-unsaved.js` covers dirty guard |
| Keyboard | `react-hotkeys-hook` only for Escape in modals; no calendar shortcuts | PD already has Ctrl/Cmd+S in composer and `inbox-keyboard.js`. Add (optional): `t` today, `[`/`]` prev/next period, `m/w/d/l` view, `c` create post on calendar, each guarded against typing in inputs and listed in a `?` help dialog |
| Tooltips | Global `data-tooltip-id="tooltip"` (react-tooltip) | Use native `title` + `aria-label` (as `icon_button` does); no new library |
| Collapse state | Persisted cookie for channel rail | `localStorage` with try/catch, same as `sidebarCollapsed` |
| Dropdown reveal | Hover-revealed menus (post-now menu uses `group-hover`) | Do not copy hover-only menus; PD uses click + Esc + focus return, which is the accessible pattern |

---

## 4. Prioritised implementation plan

Order is by user impact. Each WP is sized for one engineer-agent (about 1-2 days) and is shippable alone. General rules for all: Tailwind v4 tokens from `styles.css` (`bg-surface-*`, `border-line`, `text-ink*`, `text-accent`, `text-brand`), no new JS frameworks, no raw hex except Orbit-approved values, logical properties (`ms-`, `ps-`, `inset-inline-*`) for RTL, `prefers-reduced-motion` honoured, and build CSS with the project's Tailwind step (`theme/static_src`). Every change must pass the existing accessibility tests (`tests/` a11y suites) and not alter form names or endpoints.

Standard acceptance block (A) used below:
- A1: renders correctly at 1440x900, 1024x768 and 375x812 with no horizontal page scroll at 375.
- A2: all new interactive elements reachable by keyboard in logical order, visible focus ring (existing `--brand-green-500` ring), hit targets >= 24px (>= 44px on touch via `@media (pointer: coarse)`).
- A3: text contrast >= 4.5:1, UI boundaries >= 3:1 (use tokens listed in styles.css contrast table; never `brand-500` as small text).
- A4: RTL (`dir="rtl"`) mirrors correctly.
- A5: light theme: N/A unless a light theme exists; if introduced, all new classes must use tokens only so both themes follow.

### WP1: Shell (floating cards, desktop header, icon rail)
Files: `templates/base.html` (shell markup ~112-130, 745-790), `theme/static_src/src/styles.css` (`.pd-shell`, `.pd-sidebar`, `.pd-main`, `.pd-topbar`, lines ~930-1010, `.sidebar-*` 244-320), `theme/static/css/postdelegate.css` if remaps needed; `templates/layouts/settings.html` (title block).
Create: `.pd-header` (desktop header, new block `{% block page_header %}` already exists and is blanked in `settings.html:8`; reuse that block name), `.sidebar-rail` collapsed variant.
Do: shell padding 12px/gap 8px at lg+, sidebar and main as separate rounded-xl cards on `--surface-page`, remove sidebar's implicit page-colour blending; 64px header with title + icon cluster (workspace pill, notifications link, help, user menu moved from sidebar footer) separated by dividers; collapsed sidebar = icon-over-10px-label rail 4.5rem wide, active = `primary-soft`.
Acceptance: A1-A4; mobile drawer unchanged (still focus-trapped via `x-pd-dialog`, `inert` when closed); `html.sidebar-is-collapsed` pre-paint logic still prevents flash; no layout shift on first paint; pages that set their own header still work.

### WP2: Calendar toolbar, grid and chips
Files: `templates/calendar/partials/publish_calendar_shell.html`, `week_grid.html`, `day_grid.html`, `month_grid.html`, `list_view.html`, `_grid_chip.html`, `_grid_slot.html`, `templates/calendar/calendar.html` (CSS 100-370 and the Sortable block 574-700), `static/js/reschedule-dialog.js` (only if hooks change).
Create: `templates/components/_segmented_control.html` (include with args: name, options, active, url_template or Alpine model; used here and in analytics), `.cal-chip` two-part chip CSS, `.cal-past` stripe pattern, `.cal-add-tile`.
Do: items 2.3.1-2.3.7. Keep every existing data attribute (`data-platform-post-id`, `data-date`, `data-time`, `.cal-drop`, `.chip-row`) because Sortable and tests depend on them.
Acceptance: A1-A4; drag-and-drop still reschedules (existing calendar tests); Reschedule button still reachable by keyboard (now inside the hover actions, visible on `:focus-within`); failed chip announces error via `sr-only`; month view at 375px keeps the compact-month + agenda design; chip actions are not hover-only on touch (`@media (hover: none)` shows them as in calendar.html:290).

### WP3: Channel rail on the calendar
Files: `templates/calendar/partials/publish_calendar_shell.html` (layout wrapper), new `templates/calendar/partials/_channel_rail.html`, `templates/social_accounts/partials/_account_card.html` (compact variant), `templates/social_accounts/partials/_empty.html`, `apps/calendar` view (`social_accounts` already in context per `ui_select ... options=social_accounts`; add reconnect/paused flags if missing, read-only query change), `static_src` CSS.
Create: `_channel_rail.html`, `.channel-row`, `.channel-avatar` (36px rounded-lg + 18px badge).
Do: section 2.2. Row click toggles `calendarFilters.channels` and calls `reloadCalendar()`. "Connect channel" and "Create post" buttons. Collapse persisted to `localStorage('channelRailCollapsed')` wrapped in try/catch.
Acceptance: A1-A4; at <xl the rail becomes a horizontal avatar strip (`overflow-x-auto`, snap); reconnect-needed channels are keyboard-operable buttons with `aria-label="Reconnect <name>"`; rows are `<button aria-pressed>` or links, not div-with-click; empty state when zero channels shows the shared empty state and "Connect channel" CTA; no N+1 queries added (use `select_related`).

### WP4: Composer layout, channel tabs, footer
Files: `templates/composer/compose.html` (top ~296-360 and footer; override blocks at ~1324-1340), `templates/composer/partials/preview_panel.html`, `preview_card.html`, `insert_toolbar.html`, `media_picker.html`, `static/js/composer-unsaved.js` (verify), CSS in compose.html `<style>` (lines ~19-47 `.panel-right`).
Create: Alpine state `activeTab` in `composerApp()`; `.composer-card`, `.channel-tab`, `.channel-toggle` CSS; split-button (reuse `ui_select`/popover pattern, no new library).
Do: section 2.4 items 1-7. Do not rename form fields or HTMX endpoints; `previewUpdate` trigger stays.
Acceptance: A1-A4; at 375px the preview stays a drawer dialog (existing `previewIsDrawer` logic), channel strip scrolls horizontally, footer buttons wrap to two rows without overlap; tab arrow-key navigation; "Post now" requires confirmation; Ctrl/Cmd+S still saves draft; dirty guard still fires; each selected channel's character-limit warning still shown; existing composer tests pass.

### WP5: Shared polish components (toast, empty state, modal sizes, buttons)
Files: `theme/static_src/src/styles.css` (`.pd-toast` ~1015, `.btn-*`), `templates/base.html` (toast markup ~64-90), `templates/components/` (new `_empty_state.html`, `toast`), then migrate `social_accounts/partials/_empty.html`, `calendar/partials/_no_channels_empty_state.html`, `analytics/_no_analytics_state.html`, `analytics/_empty_state.html`, `media_library/_empty_state.html`.
Do: success/error toast variants (section 3), unified empty state, standard button heights (40px default, 44px primary in composer footer, 32px compact), 14px control text, `radius-lg` 8px for controls.
Acceptance: A1-A4; toasts announce via `role=status`/`alert`, don't steal focus, don't overlap the mobile bottom controls; existing HTMX error handler (`data-no-error-toast`, inline error target) unchanged.

### WP6: Settings, team, API keys and analytics restyle
Files: `templates/layouts/settings.html`, `layouts/workspace_settings.html`, `templates/members/list.html`, `templates/workspaces/settings.html`, `templates/api_keys/list.html`, `templates/webhooks/*`, `templates/analytics/_kpi_card.html`, `_stat_card.html`, `_range_controls.html`, `_page_header.html`.
Create: `.pd-panel` (rounded-xl bg-surface-1 p-6) and `.pd-table` utility classes; reuse the segmented control from WP2 for analytics ranges.
Acceptance: A1-A4; tables become stacked cards at 375px; no changes to forms or permissions; remove remaining `stone-*`/`orange-*` classes in touched files (replace with tokens).

### WP7: Media picker and library tiles
Files: `templates/composer/partials/media_picker.html`, `media_list.html`, `templates/media_library/_asset_card.html`, `_asset_grid.html`, `static/js/media-library.js`.
Do: search + upload on one row, tile selection overlay (purple check), sticky footer "Cancel / Add selected (n)", drag-to-reorder attachments.
Acceptance: A1-A4; multi-select via keyboard (Space toggles, Shift+arrows range optional), `aria-checked` on tiles, upload progress uses `role=progressbar`.

### WP8 (optional, last): Calendar keyboard shortcuts and chip preview modal
Files: new `static/js/calendar-shortcuts.js` (vanilla, loaded only on calendar pages), `templates/calendar/partials/_grid_chip.html`, `templates/components/modal.html` usage; one new GET endpoint for preview partial.
Acceptance: shortcuts disabled when focus is in input/textarea/contenteditable or a modal is open; documented in a `?` help dialog; preview modal uses `{% modal size="lg" %}`.

Suggested sequencing: WP1 and WP5 can start in parallel (WP5 has no dependency); WP2 after WP1 (shared tokens/header); WP3 after WP2 (needs the segmented control and tile styles); WP4 independent of WP2/WP3 but benefits from WP5; WP6 and WP7 last.

Process notes from CLAUDE.md/memory: Sonnet builds, Opus reviews every WP before merge; update the Launch HQ artifact after each merge; follow the PR template in `.github/PULL_REQUEST_TEMPLATE.md` if the repo has one.

---

## 5. Assets and legal

Licence position: the Postiz repo (`/Users/naserkhanjar/Projects/PostDelegate/LICENSE`) is AGPL-3.0. Source code and the assets that ship in the repo are covered, which allows reuse of code and original artwork provided the AGPL obligations are met (source offered to network users, licence and notices preserved, changes marked). PD already credits Postiz. Practical caution: AGPL does not give rights to third-party marks embedded in files and does not cover Postiz's trade name or logo.

Safe to reuse or adapt (original Postiz UI artwork, AGPL), prefer redrawing in Orbit style when it is faster than auditing:
- `PZ:public/no-channels.svg` and `PZ:public/no-channels-colors.svg` (generic empty-state illustration; recolour to Orbit `--color-illustration-*` tokens).
- `PZ:public/magic.svg`, `PZ:public/trending.svg` (`public/icons/trending.svg`), `PZ:public/icons/star-circle.svg`, `PZ:public/success.svg`, `PZ:public/peoplemarketplace.svg` (decorative, check each visually before use).
- UI glyph SVGs inside `PZ:src/components/ui/icons/index.tsx` (32 hand-drawn icons) and the inline menu icons in `PZ:src/components/layout/top.menu.tsx`; these are generic shapes. Recommendation: not worth importing; PD's own 24px outline set covers them.
- The CSS patterns described here (diagonal `repeated-strip`, `col-calendar` label, grid proportions) are ideas and trivial code; reimplement rather than copy.

Reusable only as third-party marks (not Postiz's own, so AGPL does not clear them):
- `PZ:public/icons/platforms/*.png` and `youtube.svg` are the logos of Instagram, LinkedIn, X, etc. PD already ships its own network icons via `templates/social_accounts/partials/_platform_icon.html` and `.pi-<platform>` colour classes. Keep using PD's; where official brand guidelines require it, use each network's current official asset.
- `PZ:public/icons/third-party/*`, `generic-oauth.svg`, `github.svg`: same caveat.

Must NOT be used (Postiz brand or personal data):
- The Postiz name, wordmark and logo: `PZ:public/logo.svg`, `logo-text.svg`, `favicon.*`, `apple-touch-icon.png` and `src/components/ui/logo-text.component.tsx` if any variant still resolves to Postiz artwork (in this checkout they have been replaced by PostDelegate files `postdelegate-logo.svg`, `postdelegate-mark.svg`, `postdelegate-og.svg`; verify before copying any file by checking its content, not its name).
- Login visuals and testimonials: `PZ:public/auth/bg-login.png`, `login-box.png`, `public/auth/avatars/*`, `src/components/auth/testimonial*.tsx` (photos/quotes of real third parties).
- Upstream footer/community links and "Peppermint" design credit (see `docs/product/postiz-footer-audit-2026-09-12.md`): no copying of their affiliate, Discord, Canny or hiring links.
- `PZ:public/stripe.svg` (third-party mark) and any file referencing "Postiz" or "gitroom" in text or alt.
- Copy and microcopy that is distinctive Postiz marketing text. UI strings like "Add to calendar", "Post now", "Save as Draft" are generic and fine; do not copy blog/pricing text.

PD brand items to keep: logo `static/img/postdelegate-logo.svg` and mark `postdelegate-mark.svg`, Orbit tokens, purple `#7357FF`/`#6545ED`, lime `#C8FF4D` accent, Manrope/Noto Sans Arabic, the `.pd-auth` story panel, and the copy tone from the public site.

Attribution to preserve: keep the existing Postiz AGPL credit and source link in the app footer/legal page (`templates/partials/_source_credit.html`, `_legal_links.html`); add "Interface inspired by Postiz (AGPL-3.0)" there if the owner wants an explicit line. The BrightBean base also has its own licence file (`LICENSE` at repo root) whose notices must stay.

---

# Addendum (owner request): motion, "not buggy" checklist, mobile

Evidence base: Postiz motion is defined in `PZ:tailwind.config.cjs` (keyframes/animation blocks) and used through Tailwind classes; there is no framer-motion or react-spring in the frontend. Drag and drop is react-dnd with the HTML5 backend (`PZ:launches/helpers/dnd.provider.tsx`), which has no touch support. Overall Postiz motion is modest: fades, a 30px rise, a toast drop, `transition-all`/`transition-colors`, `animate-pulse` and `animate-spin`. "Polish" comes mostly from state coverage (skeletons, spinners in buttons, confirm dialogs), not from heavy animation. Copy that restraint.

## A. Motion

Postiz keyframes (`tailwind.config.cjs`): `normalFadeIn` 0->1 opacity; `fadeOut` (misnamed, a rise) opacity 0 + translateY(30px) -> 1 + 0; `normalFadeDown` opacity 0 + translateY(-30px) -> 1 + 0; `fadeDown` toast (margin-top -30 -> 0 at 10%, hold to 85%, 10px at 90%, out at 100%); `normalFadeOut` 1->0; `overFlow`/`overFlowReverse` (toggle overflow after the animation ends, used for expanding blocks); `newMessages` (background flash then settle). Animations: `fadeIn 0.2s ease-in-out forwards`, `normalFadeIn 0.5s ease-in-out`, `fade 0.5s ease-in-out`, `normalFadeDown 0.5s ease-in-out forwards`, `fadeDown 4s ease-in-out forwards`, `normalFadeOut 0.5s linear 5s forwards`. Default `transition-all` has Tailwind 3's 150ms ease. PD tokens to use: `--dur-fast` 150ms, `--dur-base` 200ms, `--dur-slow` 350ms, `--ease-out` cubic-bezier(.16,1,.3,1), `--ease-spring` (use sparingly) in `styles.css:~85`.

| Interaction | Postiz (duration, easing, implementation) | PD equivalent (Alpine / CSS) |
|---|---|---|
| Modal open | Backdrop `bg-popup` with `animate-fadeIn` 200ms ease-in-out, the panel itself appears without transform (`new-modal.tsx:140,174`). Close = unmount, no exit animation | `{% modal %}` already uses `x-transition` 150ms in / 100ms out opacity (`components/modal.html`). Add panel scale: `x-transition:enter-start="opacity-0 scale-95"` on `.pd-modal`, duration 150ms `--ease-out`. Exit 100ms. Reduced motion: opacity only |
| Mobile drawer / sheet | None (Postiz has no drawer) | `.pd-sidebar` already slides `transform` 200ms `--ease-out` (styles.css ~977). Reuse for bottom sheet: `translateY(100%) -> 0`, 220ms `--ease-out`, backdrop fade 200ms |
| Dropdown / popover | Hover-driven `group-hover:flex` or Mantine popover; effectively instant, no easing. Chevrons rotate via `group-hover:rotate-180` (`manage.modal.tsx:673`) | Alpine `x-transition:enter="transition ease-out duration-100"` `enter-start="opacity-0 -translate-y-1"` (already in `publish_calendar_shell.html:60-70`). Standardise on 100ms in / 75ms out. Chevron: `transition-transform duration-150` + `:class="open && 'rotate-180'"` |
| Toast | Top-centre, `animate-fadeDown` 4s total: 0.4s in, hold ~3.0s, 0.2s settle, 0.4s out. Coloured glow shadow (`toaster.tsx:35`) | `.pd-toast` with `x-transition:enter="transition ease-out duration-200" enter-start="opacity-0 -translate-y-3" enter-end="opacity-100 translate-y-0"` leave 150ms; auto-dismiss `setTimeout(4000)` for success, none for errors; pause on hover/focus |
| Page / tab switch | No route transition. Calendar view switches re-render instantly; SWR keeps old data while loading and shows `animate-pulse` on the cell | PD has `.tab-slide-in` (week/month grids). Keep it but cap at 150ms opacity+4px translate, and add `hx-swap="innerHTML transition:true"` only where View Transitions is supported (progressive; no JS). Never animate the whole page on `hx-boost` |
| Hover (nav, rows, buttons) | `transition-colors` / `transition-all` 150ms default easing, colour or bg change only; channel row accent bar fades via `opacity` (`transition-opacity`) | `transition: background var(--dur-fast), color var(--dur-fast)`, no layout-affecting props (already so in `.sidebar-nav-item`, `.pd-icon-btn`). Do not animate `all` |
| Press / active | No explicit press state (browser default) | Add `:active { transform: scale(.98) }` on primary buttons only, 80ms; disabled under reduced-motion |
| Calendar "+" tile | `group-hover:bg-btnPrimary` with `transition-all`, the "+" is `before:content` (instant) | `.cal-add-tile { opacity:0; transition: opacity var(--dur-fast) } .cal-drop:hover .cal-add-tile, .cal-add-tile:focus-visible { opacity:1 }`. Must be visible on focus and on touch (`@media (hover:none)` always show a faint "+") |
| Drag source / ghost | react-dnd: source `opacity: 0` while dragging (`calendar.tsx:1052`), browser's native drag image follows the pointer; no custom ghost | SortableJS: native-like clone via `fallbackOnBody`; keep `.chip-ghost {display:none}` and a `.sortable-chosen { transform: scale(1.02); box-shadow: var(--shadow-lg) }` for the dragged clone (120ms) |
| Drop target | `canDrop` -> `border border-[#7357FF]` on the cell, instant; drop = SWR mutate, chip appears after server returns | `.cal-drop-active` outline (exists). Add 120ms `transition: box-shadow`. On drop: optimistic move (see B), then pulse the chip once: `@keyframes chip-landed { from { box-shadow: 0 0 0 3px rgba(var(--primary-rgb),.5) } to { box-shadow: 0 0 0 0 transparent } }` 600ms `--ease-out` |
| Skeleton / loading | `animate-pulse` (Tailwind: opacity 1 -> .5 -> 1, 2s cubic-bezier(.4,0,.6,1) infinite) on a `bg-newSettings` rounded block over the cell; full-page `LoadingComponent` spinner; button spinner `animate-spin` 1s linear, `h-[20px] w-[20px] border-4 border-t-transparent`, label `invisible` to keep width | Same animation via Tailwind v4 `animate-pulse`/`animate-spin` (already available). Create `.skeleton { background: var(--surface-2); border-radius: var(--radius-lg); animation: pulse 2s ... }` and `.btn-loading` (label `visibility:hidden`, `::after` spinner) driven by `.htmx-request` so it works with HTMX alone |
| Optimistic updates | SWR `mutate` after actions; drag-drop does not move the chip until the server returns (no true optimistic UI), delete awaits a confirm dialog | We can do better in two safe places: (1) calendar drag: Sortable has already moved the DOM node, keep it, send `hx-post`, revert node and toast on failure (`htmx:responseError`); (2) bulk status toggles/pause: flip the control with Alpine immediately, revert on error. Do not do optimistic UI for publish/delete/billing |
| Expand / collapse | `overFlow` keyframes after height change; group headers toggle `hidden` (instant) | Use `x-collapse` is not available (plugin); use CSS grid trick: `display:grid; grid-template-rows: 0fr -> 1fr; transition: grid-template-rows 200ms var(--ease-out)` with inner `overflow:hidden`. No new plugin |
| Sidebar collapse | Channel rail width swap, `transition-all` | `.pd-sidebar { transition: width var(--dur-base) ease }` exists; also transition the `.channel-rail` width, and set `will-change: width` only during the transition |
| Highlight new item | `newMessages` background flash 1s after 4s | Use `chip-landed` pulse above |

Reduced motion (applies to every row above): add one global block in `styles.css` (currently only `.btn-*` at line 569 and calendar.html:270 handle it):
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
  .skeleton, .animate-pulse { animation: none; opacity: .7; }   /* static placeholder instead of pulsing */
  .animate-spin { animation-duration: 2s !important; animation-iteration-count: infinite !important; } /* keep a spinner, it is status, not decoration */
}
```
Alpine `x-transition` classes are CSS-based, so the global rule covers them. Toast auto-dismiss timers are unaffected.

## B. "Doesn't feel buggy" checklist (Postiz practice vs PD offenders)

Counts from `grep` on `/Users/naserkhanjar/Projects/postdelegate-studio/templates` on 2026-10-24.

1. Loading and disabled state on every posting control. Postiz: every submit swaps the label for a spinner, keeps width (`invisible`), and sets `disabled` (`manage.modal.tsx:618-660`). PD: 131 `hx-post/put/patch/delete` elements have no `hx-disabled-elt`; only 7 uses of `hx-disabled-elt`/`hx-indicator` exist in total (`inbox/partials/_reply_composer.html:100`, `onboarding/welcome/step_post.html:61`, `api_keys/_issue_modal.html:15`, others). Worst offenders:
   - `calendar/partials/_grid.html:28` (`grid_move`), `calendar/queue_detail.html:125,131,138` (entry move/remove, double-click would double-move), `calendar/partials/publish_approvals.html:55,78` (bulk approve/reject), `calendar/partials/_saved_views_menu.html:15,19,25`, `calendar/queues.html:29,104`, `calendar/evergreen.html:70,75`, `calendar/partials/_evergreen_add_form.html:14`, `calendar/partials/_suggested_slots.html:40`, `calendar/posting_slots.html:38`, `calendar/partials/_saved_views.html:30`.
   - AI writer: `ai_writer/partials/composer_panel.html:70,92,103,115,135` (long-running; no spinner, can fire twice and burn the user's own API quota), `ai_writer/partials/link_result.html:20,28`, `ai_writer/org_settings.html:97`.
   - Fix pattern: add globally `hx-disabled-elt="this"` through `<body hx-disabled-elt="find button[type=submit], this">`? Simplest and safest: in `base.html` add `htmx.config` listener `htmx:beforeRequest` that sets `aria-busy=true` and `disabled` on `evt.detail.elt` or the form's submit buttons and clears on `htmx:afterRequest`; plus CSS `.htmx-request.btn-brand, .htmx-request .btn-brand { pointer-events:none; opacity:.7 }`. Then opt-outs by `data-no-lock`.
2. 241 `type="submit"` buttons; plain (non-HTMX) forms have no double-submit guard. Postiz guards with `loading` state on the button. PD: add one delegated `submit` handler in `base.html` (or `static/js/confirm-dialog.js` neighbour, e.g. new `static/js/form-guard.js`) that disables the submitter after first submit (restore on `pageshow`/validation failure). Check `composer/compose.html` (saves via fetch) separately: `compose.html:3333,3337` redirect after save; make sure Save/Schedule are disabled while the request is in flight (`isSaving` flag exists for ideas at `base.html:`"isCreateSaving", extend to composer).
3. Layout shift. Postiz sets explicit `width/height` on avatars (`width={36}`). PD `<img>` tags without width/height or fixed size class: `base.html:153,160,397`, `calendar/partials/publish_sent.html:36,38`, `publish_drafts.html:35,37`, `publish_queue.html:63,65`, `calendar/partials/_grid.html:11`, `composer/partials/media_picker.html:20,24`, `intelligence/_panel_score_packaging.html:57`, `linkinbio/public.html:66,68`. Fix: width/height attrs or `aspect-square` wrappers + `loading="lazy" decoding="async"`; `<video>` thumbnails in `_grid_chip.html` already sized `w-5 h-5`. Also reserve toolbar height while HTMX loads `#calendar-content` (min-height on container).
4. Flash of unpositioned Alpine content. `[x-cloak]{display:none!important}` exists (`styles.css:934`) and `modal.html` uses it, but 147 `x-show=` lines have no `x-cloak` on the same element. Many are inside a cloaked ancestor, but these are direct risks (always-visible-until-Alpine-boots): `base.html:236,313,325,411,412,622,660` (sidebar labels flash in collapsed state; partly covered by `html.sidebar-is-collapsed` pre-paint), `api_keys/_connection_row.html:67,104`, `api_keys/_key_row.html:70,150,175`, `api_keys/_token_reveal.html:13`, `api_keys/_issue_modal.html:12` (modals shown until boot), `ai_writer/org_settings.html`. Fix: add `x-cloak` to every `x-show` that starts hidden; add a lint test `tests/test_templates_xcloak.py` that fails on `x-show` without `x-cloak` for elements whose initial state is false (heuristic acceptable).
5. Consistent focus rings. PD has a ring token (lime `--brand-green-500` focus ring, styles.css contrast table) but 24 elements use `outline-none` without a replacement `focus-visible`/`focus:ring`: `composer/compose.html:2003`, `composer/create_landing.html:618,720,814`, `mfa/totp/activate_form.html:32`, `accounts/settings.html:60,83`, `client_portal/admin/partials/invite_client_modal.html:32`, `api_keys/_issue_modal.html:68` (heading with tabindex -1, acceptable). Fix: global `:focus-visible { outline: 2px solid var(--brand-green-500); outline-offset: 2px }` in `styles.css` and replace per-element `outline-none` with `focus-visible:outline-2`.
6. Scroll restoration and position. Postiz relies on SWR cache and Next's scroll restoration. PD: calendar swaps `#calendar-content` via HTMX; the week grid auto-scrolls to the first busy hour (`week_grid.html:~41`) but after a drag/reschedule refresh the scroll resets. Fix: capture `scrollTop` of `#calendar-content` and the week container on `htmx:beforeSwap`, restore on `htmx:afterSwap` (≈10 lines in `calendar.html`'s Alpine `calendarApp()`); set `hx-swap="innerHTML show:none"` so HTMX does not scroll. Full-page redirects after composer save (`compose.html:2208,3333,3337`) should go through a toast + redirect with `?saved=1`, not silently.
7. Page reloads where a toast/partial would do: `intelligence/_status_active_oob.html:3` (`location.reload()` from an OOB script), `composer/compose.html:2235` (`setTimeout(reload, 1100)`), `organizations/cross_calendar.html:169,181,193` (`window.location.href` on `@change` of filters: whole page reload, no state), `intelligence/_finalizing_completed.html:6`. Replace with `hx-get` + `hx-push-url="true"` on the target container, or `HX-Refresh`-less partial swap.
8. Viewport units. `100vh` breaks on mobile browser chrome: `calendar/partials/week_grid.html:44`, `calendar/partials/day_grid.html:6`, `organizations/partials/day_grid.html:6`, `week_grid.html:6`, `composer/create_landing.html:34,130,522,528`, `accounts/dashboard.html:6`, `oauth2_provider/base.html:34`. `.pd-shell` already uses `100dvh`; use `dvh` everywhere (or flex-fill the shell instead of calc).
9. Errors and empty states. Postiz shows toast on every failure and an empty state in the rail. PD: toast exists only for errors (`.pd-toast` is error-styled), success feedback is mixed (reload, banners). Add success toast (WP5), a consistent inline error slot `data-inline-error` (exists), and use the shared empty state include for all list pages.
10. Reduced motion: only `.btn-*` and the calendar block respect it (`styles.css:569`, `calendar.html:270`); the 96 `animate-*`/`transition` occurrences elsewhere do not. Fix with the global block in section A.
11. Touch target size: 142 elements use 24-32px controls (`size-8`, `w-7 h-7`, `min-h-8`), e.g. `calendar/partials/publish_calendar_shell.html:19,25,40,58` (32px arrows and view button). WCAG 2.2 minimum 24px is met, but the owner's bar is 44px on touch: add `@media (pointer: coarse) { .touch-44 { min-height:44px; min-width:44px } }` and apply to calendar toolbar, chip actions, checkbox hit-areas (`_select_checkbox.html`), `icon_button` sm size.
12. Network/offline: no service worker or offline fallback (`grep serviceWorker` finds nothing). Add in WP9 (PWA).
13. Keep: skip link, `inert` background when dialogs open, `aria-live` on period label, `tabular-nums`. These are ahead of Postiz.

Add a CI guard: extend the template test suite with 3 checks (x-cloak, `hx-post` has `hx-disabled-elt` or `data-no-lock`, `<img>` has dimensions) so regressions fail the build.

## C. Mobile

How Postiz handles mobile: weakly. The shell is a desktop layout (80px rail + 260px channel column + grid with `[136px_repeat(7,1fr)]`); breakpoints are custom `mobile` (max 1025px) and `xs` (max 401px) in `tailwind.config.cjs`, used rarely (rail icon sizes by viewport height, not width). `mobile.integration.tsx` is only a flag on the add-channel flow (`isMobile={true}`), not a mobile layout. There is no bottom nav, no agenda view, no touch drag (react-dnd HTML5 backend does not work on touch), modals are large fixed-width containers (`max-w-[1400px]`, composer `p-[40px]` outer padding with a fixed 580px preview column), and no PWA manifest or service worker in `PZ:public`. Treat Postiz as a desktop reference only; do not copy its mobile behaviour.

PD today: good foundations. Off-canvas drawer with focus trap (`base.html:117-130`), 44px min nav items on mobile (`styles.css:~316`), week agenda and compact month on phones (`week_grid.html:9-37`, `month_grid.html:10-30`), preview as dialog drawer (`compose.html:1844`), `100dvh` shell, a web manifest (`static/favicon/postdelegate.webmanifest`, but with only a 180px icon, missing `start_url`, `scope`, `id`, 192/512 icons even though `web-app-manifest-192x192.png` and `512x512` exist in `static/favicon/`), no `viewport-fit=cover` and only 2 `safe-area` mentions in total, no service worker, no bottom navigation.

Plan:
1. Navigation: below lg replace the hamburger-only access with a fixed bottom tab bar (56px + `env(safe-area-inset-bottom)`): Publish (calendar), Create (centre primary "+" opening the existing New menu as a bottom sheet), Inbox, Analytics, More (opens the existing drawer). Markup in `base.html` next to `.pd-shell` as `<nav class="pd-tabbar lg:hidden" aria-label="Primary">`; `.pd-main` gets `padding-bottom: calc(56px + env(safe-area-inset-bottom))`. Active state same as sidebar (`primary-soft` + `text-brand`), icons 22px, labels 11px. Hide when the on-screen keyboard is open (focus in input) via `visualViewport` resize listener (8 lines Alpine) and on the composer page (it has its own footer).
2. Safe areas: change viewport meta to `width=device-width, initial-scale=1, viewport-fit=cover`; add `padding-inline: max(var(--gutter), env(safe-area-inset-left/right))` on `.pd-main`, `.pd-topbar` top inset, bottom inset on tab bar, sheets and sticky composer footer.
3. Calendar on phones: keep week agenda and compact month (already good); add a "List" default on phones? Make the phone toolbar a single row: period label with prev/next, view segmented control (Day/Week/Month/List) in a horizontally scrollable strip, filters collapse into one "Filters" button opening a bottom sheet (`{% modal %}` variant `pd-sheet`). Channel rail becomes the avatar strip (WP3). Tap on empty hour row = create post with prefilled time (already a "Create post" pill; also make the whole empty row tappable, min 44px). Reschedule remains via dialog (touch drag is unreliable; SortableJS supports touch with `delay: 250` + `delayOnTouchOnly: true`, enable it so long-press drag works, and keep the dialog as fallback).
4. Composer on phones: full-screen sheet. Header sticky (back, title, Preview button), channel strip horizontally scrollable, sticky bottom action bar (Save draft, Schedule) above the safe area, preview as a full-height bottom sheet (existing drawer restyled; swipe-down to close is optional), editor font-size 16px (prevents iOS zoom), media picker as full-screen sheet with 3-column grid. `.panel-right` already switches to dialog below lg; confirm width 100% and height `100dvh`.
5. Touch targets and no horizontal scroll: all interactive controls >= 44x44 under `@media (pointer: coarse)` (chip actions, toolbar buttons, checkboxes, `icon_button` sm/md auto-bumped to lg), body `overflow-x: clip`, tables become stacked cards, long words `overflow-wrap:anywhere`, wide grids wrapped in `overflow-x-auto` with `role=region` and `tabindex=0`. Add a Playwright/pytest smoke that loads key pages at 375x812 and asserts `document.documentElement.scrollWidth <= innerWidth`.
6. Sheets: new `.pd-sheet` variant of `{% modal %}` (`size="sheet"`): at <640px anchored bottom, `border-radius: 20px 20px 0 0`, max-height `88dvh`, grab handle, safe-area bottom padding, slide-up 220ms `--ease-out`; same focus trap and Escape. Today `{% modal %}` goes full-screen below 640px, which is fine for forms; use the sheet only for menus/filters/New menu.
7. PWA install: complete `static/favicon/postdelegate.webmanifest` (`id`, `start_url:"/?source=pwa"`, `scope:"/"`, `display:"standalone"`, `orientation:"any"`, `theme_color:"#080A0F"`, `background_color:"#080A0F"`, icons 192, 512 and maskable 512, `shortcuts` for Create post and Calendar). Add a minimal service worker `static/sw.js` served from `/sw.js` (cache-first for static assets with versioned names, network-only for HTML/HTMX/API, offline fallback page `templates/offline.html`); register it in `base.html` with try/catch. Optional "Install" prompt row in the More sheet using `beforeinstallprompt` (hidden when unavailable; iOS shows an "Add to Home Screen" hint). Do not cache authenticated HTML.
8. Perf feel: `loading="lazy"` on grid images, `content-visibility: auto` on off-screen month rows, `touch-action: manipulation` on buttons (removes 300ms delay), `overscroll-behavior: contain` on drawers/sheets/scroll panes.

### Additional work packages (add to section 4)

- WP9 Mobile shell: bottom tab bar, safe areas, viewport-fit, `.pd-sheet`, filter sheet, touch 44px utility, `dvh` replacements (list in B.8), 375px overflow smoke test. Files: `templates/base.html`, `theme/static_src/src/styles.css`, `templates/components/modal.html`, `templates/calendar/partials/publish_calendar_shell.html`, `week_grid.html`, `day_grid.html`, `month_grid.html`. Depends on WP1 (shell). Acceptance at 375x812 and 768x1024: tab bar visible below lg only, never overlaps content or the keyboard; safe-area insets honoured in iOS Simulator (notch and home indicator) and landscape; every control >= 44px (measured by test); no horizontal scroll; focus trap in sheet; contrast A3; at 768x1024 the tab bar is replaced by the collapsed rail (lg is 1024, so at exactly 768 the tab bar shows; verify the layout is not stretched, grid uses the week hourly view from md=768).
- WP10 Motion and stability pass: global reduced-motion block, modal/dropdown/toast transitions (table A), `.skeleton` and `.btn-loading`, htmx lock handler and `form-guard.js`, x-cloak/image-dimension/focus-ring/`100vh` fixes from B, calendar scroll restoration, optimistic drag, template lint tests. Files: `theme/static_src/src/styles.css`, `templates/base.html`, `static/js/form-guard.js` (new), every file listed in B. Acceptance: with `prefers-reduced-motion` no animation exceeds 0.01ms except spinners; no CLS > 0.05 on calendar load (Lighthouse/Chrome trace at 375 and 1440); every `hx-post` element is disabled during flight (lint test passes); double-click on any submit sends one request; drag failure reverts and toasts; scroll position kept after reschedule.
- WP11 PWA: manifest completion, service worker, offline page, install row. Files: `static/favicon/postdelegate.webmanifest`, `static/sw.js`, `templates/offline.html`, `templates/base.html`, Django URL for `/sw.js` at root scope. Acceptance: Lighthouse PWA installable checks pass; installs on Android Chrome and shows standalone; offline load shows branded offline page; authenticated HTML is never served from cache; SW updates without stale assets (versioned cache).

Revised ordering: WP1 + WP5 + WP10 (parallel start), WP2, WP9, WP3, WP4, WP11, WP6, WP7, WP8. Mobile acceptance in every earlier WP must be tested at 375x812 and 768x1024 (extend block A1 with the 768x1024 viewport).

### Revised top-5 for "feels like Postiz and production-grade"
1. Floating-card shell with desktop header (WP1).
2. Persistent channel rail on the calendar plus two-part post chips with hover actions and the "+" tile (WP3, WP2).
3. Composer as two-pane card with Global + per-channel tabs and a sticky footer (WP4).
4. Uniform loading/disabled/skeleton/toast behaviour and reduced-motion handling, fixing the 131 unguarded HTMX posts (WP10, WP5).
5. Phone-first shell: bottom tab bar, sheets, safe areas, 44px targets, PWA (WP9, WP11). Postiz is weak here, so this is where PD can exceed it.
