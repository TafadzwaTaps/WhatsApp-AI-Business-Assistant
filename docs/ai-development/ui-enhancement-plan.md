# WaziBot UI Enhancement Plan (ui-ux-pro-max)

Date: 2026-11-16 · Status: **PLAN ONLY — not approved, no UI files edited.**
Scope: existing static dashboard (`static/dashboard.html/.css/.js`, `inbox.html/.css/.js`). Stack stays HTML + CSS + vanilla JS. No framework, no new dependencies, no build step.

## 1. Evidence from the current UI (verified)
- Dark theme: `#0a0f0d` background, WhatsApp-style green `--green:#22c55e`, fonts Syne (UI) and DM Mono (labels, served locally from `static/fonts/`). A light theme already exists (`html[data-theme="light"]`, `dashboard.css:2216`).
- **No `:focus-visible` and no `prefers-reduced-motion` rules** in `dashboard.css` or `inbox.css`.
- **73 font-size declarations of 11px or smaller** in `dashboard.css` (mono uppercase labels); 15 `@media` rules in `dashboard.css` and 7 in `inbox.css` for a 7.7k-line UI.
- **799 inline `style=""` attributes** in `dashboard.html`; ~276 inline `onclick` handlers (earlier audit) — this limits what can be done safely with CSS alone.
- Only 31 `aria-` attributes against ~261 controls (earlier audit); clickable `div` rows are not keyboard-operable.
- `.btn` is `padding:10px 20px; font-size:13px` — below the 44px touch-target guideline on mobile.
- Unpaginated lists and a 15 s full refetch poll cause visible reflow/jank (earlier audit).
- Working tree is clean on branch `phase1c-restore-guards` (HEAD `e34e134`).

## 2. Design direction (decision for you)
The skill's generated system for "WhatsApp AI assistant SaaS dashboard" recommended a *Data-Dense Dashboard* style with a blue/amber palette and Fira Code/Fira Sans. **I recommend NOT adopting its palette or fonts:**
- CLAUDE.md requires keeping the existing visual identity unless a redesign is requested; the green-on-dark WhatsApp feel is the brand.
- Fira would add external web-font loads (CSP and privacy cost); Syne/DM Mono are already bundled.
- I will adopt the skill's *rules* (accessibility, touch, layout, motion, consistency) and the "data-dense" ideas (KPI cards, row highlighting, filters, loading states) inside the current brand.
If you actually want a rebrand, say so and I will produce a separate proposal.

## 3. Proposed phases (each independently shippable, additive, low risk first)
**U1 — Accessibility and interaction (CSS-only, safest).**
- Global `:focus-visible` ring using `--green` (replacing any `outline:none` without substitute); skip-to-content link; visually-hidden utility.
- `@media (prefers-reduced-motion: reduce)` disabling transitions/animations.
- `@media (pointer: coarse)` rules for ≥44px touch targets on buttons, nav items, icon buttons, with ≥8px gaps.
- Raise the tiniest label text (9–11px) to a 12px floor and verify 4.5:1 contrast for `--text-dim` on surfaces in dark and light themes.
- Disabled/loading/active states for `.btn*`; consistent hover transitions (150–200 ms) with no layout shift.

**U2 — Visual polish and consistency (CSS tokens + small markup).**
- Add spacing/radius/shadow/z-index scale as CSS variables (additive; existing variables unchanged).
- KPI card hierarchy, table rows (hover highlight, sticky header, numeric alignment), badges/status chips with consistent colour + icon + text (colour never the only signal).
- Empty states and skeleton loaders for async lists (avoid content jumping); consistent toasts and modals.

**U3 — Responsive pass (375 / 768 / 1024 / 1440).**
- Tables in scroll containers or stacked-card layout on mobile; inbox list/detail switch; sidebar drawer polish; 16px form fields on mobile (prevents iOS zoom); remove horizontal scroll.

**U4 — Icons.** Audit for emoji/unicode used as UI icons and replace with a small inline SVG set (Lucide-style, no dependency, `aria-hidden` + labelled buttons). Needs an audit first (my emoji count was inconclusive on this shell).

**U5 — Inbox and "AI employee" experience (needs small JS, some may need backend).**
AI-handled vs needs-you filter and reason chip, clearer handoff state, undo toast for "return to AI", stronger Needs-Attention emphasis, AI activity summary on the dashboard home.

**U6 — Copy and defaults.** Neutral phone/address placeholders, country-driven timezone/currency, payment-readiness check counting all methods (finding H16). Touches `dashboard.html/js` text and defaults.

**U7 — Password-change UX (from Phase 1 findings).** Show "Password updated — signing you out…" then log out deliberately; normalise pydantic 422 error arrays so users never see `[object Object]`; guard against `null` from `apiFetch`; client checks for 128 characters / 72 bytes.

## 4. Explicitly out of scope (needs separate approval)
Tailwind/React/Vue migration; splitting `dashboard.js`; removing inline handlers or CSP `unsafe-inline` (security refactor with large churn); redesign/rebrand; backend contract changes.

## 5. Method, safety and verification
- New branch `ui-enhancement` from current HEAD; one commit per phase; **no commit, push or deploy without your approval**.
- Order: U1 → U2 → U3 → (U7 small) → U4 → U6 → U5.
- Ownership: single frontend-engineer agent per phase (all phases touch `dashboard.css`/`dashboard.html`, so phases run sequentially, not in parallel). Security and QA review each phase.
- Verification I can do: existing tests (`pytest`, `node` checks), static diff review (no whitespace churn, no removed classes/ids used by JS), and screenshots of public/login pages in the built-in browser at 375/768/1024/1440.
- **Verification I cannot do without your help:** screenshots of the logged-in dashboard/inbox need a running backend with Supabase credentials. Options: you give a staging URL plus a test account (test credentials only), or I build a throwaway static harness with mocked API data (local only, not committed).
- Rollback: revert the phase commit; all changes are additive CSS/markup.
- Constraints honoured: keep element ids/classes/`onclick` names that `dashboard.js` depends on; no new dependencies; fonts stay local.

## 6. Decisions needed
1. Keep the current green-on-dark brand (recommended) or request a rebrand?
2. Which phases to start with? (Recommended: U1 + U2 + U3 first, then U7.)
3. Pages in scope first: dashboard + inbox (recommended), then landing/signup/pricing?
4. How should I verify the logged-in UI: staging + test account, or a local mocked harness?
