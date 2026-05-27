# WebUI Styling Tips

Practical notes collected during the 2026-05-19 brand-identity sweep
(porting the [racelink.dev](https://racelink.dev) visual language into
the Host WebUI). Read this before making the next round of theme,
colour, font, or component-styling changes — most of the time you'll
save a debugging round-trip.

Scope: the Vue 3 + Vite + Tailwind v4 + reka-ui / shadcn-vue stack
under [frontend/](../frontend/).

---

## 1. Where styling lives

| Concern                               | File                                                                       |
|---------------------------------------|----------------------------------------------------------------------------|
| Design tokens (colours, fonts, radii) | [frontend/src/styles/tailwind.css](../frontend/src/styles/tailwind.css) — inside `@theme { … }` |
| Custom utilities (e.g. `btn-run-bg`)  | Same file — `@utility name { … }` blocks                                   |
| `@font-face` declarations             | Same file — above `@theme`                                                 |
| Body background + layered effects     | [frontend/src/styles/racelink.css](../frontend/src/styles/racelink.css) — inside `@layer base` |
| Reusable component-class styles       | Same file (`.rl-brand`, `.btn-brand`, `.mono`, …)                          |
| Self-hosted fonts                     | [frontend/public/fonts/](../frontend/public/fonts/)                        |
| Favicon                               | [frontend/public/favicon.svg](../frontend/public/favicon.svg)              |
| shadcn-vue Button variants            | [frontend/src/components/ui/button/Button.vue](../frontend/src/components/ui/button/Button.vue) (`cva()` config) |

When in doubt: **tokens go in `@theme`**, **utilities go in `@utility`
or `@layer utilities`**, **everything else into `@layer base` in
racelink.css**.

---

## 2. Tailwind v4 idioms used here

### `@theme` auto-generates utilities

Every CSS variable declared inside `@theme { … }` with a recognised
prefix produces matching utility classes. Examples:

| Token in `@theme`                   | Utility classes generated                          |
|-------------------------------------|----------------------------------------------------|
| `--color-brand-cyan: #1fe6d6`       | `bg-brand-cyan`, `text-brand-cyan`, `border-brand-cyan`, with `/<opacity>` modifiers |
| `--font-display: 'Chakra Petch', …` | `font-display`                                     |
| `--shadow-brand-glow: 0 0 14px …`   | `shadow-brand-glow`                                |
| `--gradient-run: linear-gradient(…)`| (no auto utility — gradients aren't a Tailwind property; consume via `@utility` or arbitrary value) |

Use `bg-brand-cyan/45` for 45% opacity — the modifier works on any
auto-generated colour utility. **No need to define `--color-brand-cyan-45`**.

### Preflight is **off** in this repo

[tailwind.css](../frontend/src/styles/tailwind.css) imports only
`tailwindcss/theme.css` and `tailwindcss/utilities.css` — not
`tailwindcss/preflight.css`. The intentional consequence is that
existing legacy CSS in [racelink.css](../frontend/src/styles/racelink.css)
keeps painting unchanged.

**Side-effect that will bite you again:**

* Form controls (`<button>`, `<input>`, `<select>`, `<textarea>`) do
  **not** inherit `font-family` from `<body>`. The UA stylesheet pins
  them to Arial/Helvetica. Preflight would normally reset them with
  `font: inherit`, but Preflight is off here. The current fix lives in
  [racelink.css](../frontend/src/styles/racelink.css):
  ```css
  button, input, select, textarea, optgroup { font-family: inherit; }
  ```
  When you add a new element kind that needs the body font, add it to
  that selector list.

### `@utility` for layer-correct custom classes

Tailwind class utilities live in `@layer utilities`, which beats
`@layer base`. If you write a class in racelink.css (`@layer base`),
**it cannot override a shadcn `bg-primary` on the same element** —
utility wins.

Solution: define custom utility classes via `@utility`:

```css
@utility btn-run-bg {
  background-image: var(--gradient-run);
  transition: filter 0.18s ease, border-color 0.18s ease;
  &:hover:not(:disabled) {
    filter: brightness(1.3) saturate(1.15);
  }
}
```

These end up in `@layer utilities` automatically and beat shadcn-vue
defaults. The Button.vue `run` variant uses this.

`@utility` supports CSS nesting (`&:hover`), `:not()`, pseudo-elements,
the full CSS toolbox.

---

## 3. Variant strategy for buttons

Variants in [Button.vue](../frontend/src/components/ui/button/Button.vue):

| Variant       | Purpose                                                  | Style language                              |
|---------------|----------------------------------------------------------|---------------------------------------------|
| `default`     | Generic primary (rarely used now)                        | Solid `bg-primary` + dark foreground        |
| `secondary`   | Cancel / Close / non-primary toolbar                     | Solid `bg-secondary`                        |
| `ghost`       | In-row icon buttons, tertiary                            | Transparent → `bg-secondary` on hover       |
| `outline`     | Bordered neutral                                         | Border + transparent fill                   |
| `brand`       | **Save / Apply / Create / Confirm** (commit state)       | Cyan outline + faint cyan fill + cyan text  |
| `run`         | **Start / Re-sync / Send / OTA-Start** (execute action)  | Pink→cyan gradient fill + cyan border + light text |
| `destructive` | **Delete + destructive confirm dialogs**                 | Pink outline + faint pink fill + pink text  |

**Picking the variant** for a new button:

1. **Does it commit edits to a record?** → `brand` (Save vocabulary).
2. **Does it execute a transient operation that runs over time?** → `run`.
3. **Does it remove or reset destructively?** → `destructive`.
4. **Is it a cancel/close/secondary action?** → `secondary` or `ghost`.

The Save/Run/Delete colour split is intentional contrast — operators
should be able to recognise the action class at a glance:

* **Cyan = safe commit** (`brand`, `run` also cyan-bordered)
* **Pink = caution** (`destructive`, plus the Run gradient leans pink at the left edge)
* **Gradient fill = execute** (`run` is the only filled brand variant)

### Conditional variant — see SpecialsActionRow

[SpecialsActionRow.vue](../frontend/src/components/modals/SpecialsActionRow.vue)
renders a generic Send button from a data-driven action schema. One
action ("Reset to RaceLink defaults", key `wled_reset_overrides`) is
visually a commit-state operation, not a generic Send. Handled with a
computed prop, not by inventing a new variant:

```ts
const sendVariant = computed<'brand' | 'run'>(() =>
  props.fn.key === 'wled_reset_overrides' ? 'brand' : 'run',
)
// then <Button :variant="sendVariant" …>
```

Pattern to repeat: pick the variant in script-land based on
**data**, not by hard-coding it in the template.

### When you add a new variant

1. Add the cva entry in [Button.vue](../frontend/src/components/ui/button/Button.vue).
2. If it needs a non-trivial fill (gradient, multi-layer bg, animated
   filter), put that in an `@utility` in tailwind.css and reference the
   utility class from the cva entry.
3. Document the variant in this table.
4. Update the English docs at
   [RaceLink_Docs/docs/RaceLink_Host/ui-conventions.md](../../RaceLink_Docs/docs/RaceLink_Host/ui-conventions.md)
   if it represents a new verb category.

---

## 4. Gradient hover transitions — use `filter`, not background-image

Browsers **cannot interpolate** between two `linear-gradient()`
functions. Setting `transition: background-image 0.18s` on a gradient
button produces an abrupt switch, no animation.

Three solutions, in order of preference:

| Approach                              | When to use                                       |
|---------------------------------------|---------------------------------------------------|
| `transition: filter` + `:hover { filter: brightness(1.3) }` | Smooth, simple, no extra DOM. **Use this for gradient fills.** |
| Two-layer bg: `background-color` (animated) under `background-image` (static) | When you want the gradient *shape* to stay but tint to shift |
| `::before` pseudo-element with the alternate gradient + `opacity` transition | Heavy — only when the hover state needs an entirely different gradient |

The `btn-run-bg` utility uses option 1. The base cva applies
`transition-colors` which doesn't cover `filter`, so the utility
declares its own `transition`.

---

## 5. Native form-control colours

Browser-default accents (checkbox tick, radio dot, slider track/thumb,
progress fill) ignore CSS-variable theming unless you set
**`accent-color`** explicitly. The rule lives in
[racelink.css](../frontend/src/styles/racelink.css):

```css
input[type="checkbox"],
input[type="radio"],
input[type="range"],
progress,
meter,
select {
  accent-color: var(--color-accent);
}
```

When `--color-accent` changes (it cascaded from `#4c8bf5` → `#1fe6d6` →
`#2aa599` during this sweep), every native control updates
automatically. **Don't try to style native control internals with
`::-webkit-slider-thumb` etc.** unless `accent-color` proves
insufficient — it's a maintenance trap and Firefox / Safari diverge.

---

## 6. The `--color-card` / `--color-popover` split

`--color-card` is allowed to have an alpha channel (currently
`#07080d1f` — 12% opacity, glassmorph effect). Cards float over the
body's pink/cyan radial-glow atmosphere.

**`--color-popover` is intentionally decoupled and kept opaque
(`#07080D`)**. Dialogs and popovers need readable solid surfaces — if
they inherit the transparent card colour, the dimmed page bleeds
through and content becomes hard to read.

Rule of thumb when adding a surface:

| Surface type                    | Token        |
|---------------------------------|--------------|
| In-page panel / card / sidebar / sticky header | `bg-card` (transparent OK) |
| Modal dialog / popover / dropdown menu / tooltip | `bg-popover` (solid required) |

If you ever need a "card-but-solid" or "panel-with-its-own-tint",
introduce a new token rather than overloading these two.

---

## 7. Self-hosting fonts

The Chakra Petch + Sora WOFF2 files in
[frontend/public/fonts/](../frontend/public/fonts/) are GDPR-friendly
self-hosted copies (mirrored from racelink.dev). Adding a new font:

1. Drop the WOFF2 file(s) into `public/fonts/`. Vite picks them up
   without config.
2. Declare `@font-face` blocks in
   [tailwind.css](../frontend/src/styles/tailwind.css) — **above** the
   `@theme` block — with `font-display: swap` so the system fallback
   paints immediately.
3. Reference the family from a CSS variable in `@theme`
   (`--font-foo: 'Foo', system-ui, …`).
4. Use as `font-foo` Tailwind utility, or `var(--font-foo)` directly.

Path used in `src: url(...)` must be `/fonts/<file>.woff2` (Vite serves
`public/` at the root).

---

## 8. Favicon under Flask

The browser auto-requests `/favicon.ico` if no `<link rel="icon">` is
declared. Flask serves the WebUI at the blueprint prefix root, not at
the OS root, so `/favicon.ico` is a guaranteed 404.

The fix lives in [frontend/index.html](../frontend/index.html):

```html
<link rel="icon" type="image/svg+xml"
      href="{{ rl_static_path }}/dist/favicon.svg" />
```

`{{ rl_static_path }}` is a Jinja placeholder. Vite passes it through
untouched (it doesn't recognise `{{` as an asset path). Flask's
`render_template_string` substitutes it at request time
([blueprint.py:301](../racelink/web/blueprint.py#L301)), so the same
build works under any prefix.

**Pattern**: anywhere the HTML needs a deployment-prefix-aware path,
use the Jinja substitution rather than hard-coding `/racelink/...`.

---

## 9. Layered atmosphere — body background

[racelink.css](../frontend/src/styles/racelink.css) layers three
background effects on `<body>`:

1. **Radial glow top-left**: pink at ~13% opacity.
2. **Radial glow top-right**: cyan at ~11% opacity.
3. **64 px speed-grid** on `body::before`, masked to fade out toward
   the bottom.

`background-attachment: fixed` anchors the glows to the viewport so
they don't scroll with the page. The grid is `position: fixed` for the
same reason, plus `pointer-events: none` so it never intercepts clicks.

If a page-level element ends up below the grid because of stacking
contexts, set `position: relative; z-index: 1` on the app root (`#app`
in [racelink.css](../frontend/src/styles/racelink.css) does this).

---

## 10. Anti-patterns to avoid

* **Don't reach for `!important`** to win against a Tailwind utility —
  use `@utility` or restructure the cva entry instead.
* **Don't hard-code colours in component templates** (`bg-[#1fe6d6]`)
  — every such usage breaks the colour-iteration that drove this sweep.
  Use tokens.
* **Don't add a `tailwind.config.js`** file — Tailwind v4 is config-in-CSS.
  A JS config will conflict with the `@theme` block.
* **Don't mix `font: inherit` and explicit `font-size`** — the
  shorthand resets all font properties, including size, weight, style.
  Use `font-family: inherit` if you only mean the family.
* **Don't gate visual changes behind feature flags** — the design
  system is one global skin. If you need A/B-like styling, fork the
  token block, don't fragment classes.
* **Don't put new tokens in racelink.css**. They belong in
  `tailwind.css` `@theme` so Tailwind exports the matching utilities.

---

## 11. Iteration playbook

The 2026-05-19 sweep iterated colour values five+ times across two
chat turns. Things that made the back-and-forth fast:

1. **Edit one token at a time.** Each iteration changed a single
   `@theme` variable; the diff for "darker cards" was literally
   `--color-card: #07080D` → `#07080d1f`.
2. **Run `npm run build` after each visual change.** vue-tsc + vite
   typecheck + bundle in ~4 s. Catches Vue-template errors before the
   browser does.
3. **Trust the production build for layout regressions.** The dev
   server uses Vite's on-demand compile which can mask cascade order
   issues. The prod build's single bundled stylesheet exercises layer
   precedence the way users see it.
4. **Treat each variant as a unit.** Updating `--gradient-run`
   propagates to all `variant="run"` buttons in one shot; no per-file
   sweep.
5. **Iterate visually first, then formalise.** The Run / Save / Delete
   distinction emerged after the user saw the buttons next to each
   other. Don't try to anticipate the taxonomy.

When iterating in another session: pull this file up first, scan the
"Where styling lives" table, then jump to the relevant section.
