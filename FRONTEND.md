# NuroQ — React Frontend Guide

> The React + Vite + Tailwind app at `frontend/`. Backed by FastAPI at `backend/api.py`.
> For the legacy Gradio dashboard see `dashboard.py` (still works in parallel).
>
> **Last updated:** 2026-05-24

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Build | Vite 8 | Fast HMR, ESM-native, no webpack config |
| Language | TypeScript 6 (strict) | Type safety against backend Pydantic models |
| UI | React 19 | Concurrent rendering, suspense, mature ecosystem |
| Styles | Tailwind CSS 3 | Utility-first, dark mode via class, design tokens |
| Icons | lucide-react | Clean, consistent, ~1KB per icon tree-shaken |
| Data | @tanstack/react-query | Cache + polling + retry + stale-while-revalidate |
| Charts | recharts (installed, not yet used) | For Analyze view in next session |
| Command palette | cmdk | Real modal with fuzzy search |
| Native shell | Capacitor 8 (iOS, Android-ready) | See `MOBILE.md` |

Total bundle: **~99 KB gzipped JS + ~5 KB gzipped CSS.**

---

## File layout

```
frontend/
├── index.html               # iOS PWA meta tags + font CDN
├── package.json             # all deps
├── vite.config.ts           # dev proxy /api → :8000, ws → :8000
├── tailwind.config.js       # custom buy/sell/accent palette + Inter/JetBrains Mono
├── postcss.config.js        # Tailwind + autoprefixer (default)
├── capacitor.config.ts      # iOS app id + dev/prod URL switch
├── public/
│   ├── manifest.json        # PWA manifest
│   ├── apple-touch-icon.png # 180×180
│   ├── icon-192.png         # PWA manifest icon
│   ├── icon-512.png         # PWA manifest icon
│   └── favicon.png          # 32×32 browser tab
├── dist/                    # built bundle (FastAPI serves from here)
├── ios/                     # Capacitor Xcode project (see MOBILE.md)
└── src/
    ├── main.tsx             # mount + QueryClientProvider
    ├── App.tsx              # layout shell + view router + Capacitor bootstrap
    ├── index.css            # Tailwind + design tokens + safe-area + components
    ├── lib/
    │   ├── api.ts           # typed fetch client (VITE_API_BASE for native)
    │   ├── cn.ts            # className combiner + fmtUSD/fmtPct/relTime
    │   └── native.ts        # Capacitor bridge (no-ops on web)
    ├── components/
    │   ├── TopBar.tsx       # brand + search trigger + status pills + theme
    │   ├── LeftRailNav.tsx  # 9-item desktop rail (≥ lg)
    │   ├── MobileNav.tsx    # 5-tab iOS bottom bar (< lg) + More sheet
    │   ├── StatusPills.tsx  # 4 health pills (dots-only on mobile)
    │   ├── CommandPalette.tsx  # cmdk modal triggered by ⌘K
    │   └── QuickTrade.tsx   # pinned strip desktop / FAB+sheet mobile
    └── views/
        ├── TodayView.tsx        # ✅ full fidelity
        └── PlaceholderView.tsx  # ⏳ 8 other views are placeholders
```

---

## Design tokens

### Color semantic split

Distinct from Gradio's mixed-use green:

- **Signal colors** (data): `buy` (#10b981 green), `sell` (#ef4444 red), `amber-500` (warning)
- **Action color** (buttons): `accent` (#14b8a6 teal) — NOT green, so action buttons don't confuse with BUY signals
- **Neutral**: zinc-50 → zinc-950 (light/dark mode swap)

Defined in `tailwind.config.js`. Use as `bg-buy`, `text-sell`, `border-accent` etc.

### Typography

- **Sans:** Inter (loaded from rsms.me CDN)
- **Mono:** JetBrains Mono (loaded from jsDelivr CDN)
- Tabular-nums on `html` → all numbers align in tables and the hero P&L

### Spacing

- Base font 13px (configured via Tailwind's prose). Compact, financial-app density.
- Default Tailwind spacing scale (0.5/1/1.5/2/2.5/3 ↔ 2/4/6/8/10/12 px)
- Cards: `card` + `card-tight` (12px padding) classes in `index.css`

### Components defined in `index.css` as `@layer components`

- `.btn` + `.btn-primary` / `.btn-buy` / `.btn-sell` / `.btn-ghost`
- `.pill` + `.pill-ok` / `.pill-warn` / `.pill-err` / `.pill-off`
- `.card` + `.card-tight`
- `.input` — bordered, focus-ring
- `.kbd` — keyboard hint chip
- `.table-zebra` — alternating rows + hover

---

## Dev workflow

### Option 1 — Dual-port HMR (recommended)

```bash
./scripts/dev.sh
# FastAPI on :8000 with --reload
# Vite on :5173 with HMR
# Vite proxies /api → :8000 so React code uses relative URLs
# Open http://127.0.0.1:5173
```

Changes to:
- React/TS/CSS → instant HMR
- Python (`backend/`, `dashboard.py`) → auto-reload via uvicorn

### Option 2 — Single-port production-like

```bash
./scripts/prod.sh
# Builds dist/, FastAPI serves it + API on :8000
# Open http://127.0.0.1:8000
```

Use for verifying the production bundle and for sharing on LAN.

### Option 3 — Manual

```bash
# Backend
NUROQ_BACKGROUND_SERVICES=0 ./.venv/bin/uvicorn backend.api:app --port 8000 --reload

# Frontend (separate terminal)
cd frontend && npm run dev
```

---

## Adding a new view

1. Create `src/views/MyView.tsx`. Pattern:
   ```tsx
   import { useQuery } from "@tanstack/react-query";
   import { api } from "../lib/api";

   export function MyView() {
     const { data, isLoading } = useQuery({
       queryKey: ["my-data"],
       queryFn: api.myEndpoint,
       refetchInterval: 30_000,
     });
     if (isLoading) return <div className="card card-tight animate-pulse h-40" />;
     return <div className="card card-tight">…</div>;
   }
   ```

2. Add a backend endpoint in `backend/api.py`:
   ```python
   @app.get("/api/my-endpoint")
   def my_endpoint(): ...
   ```

3. Add a typed wrapper in `src/lib/api.ts`:
   ```ts
   myEndpoint: () => get<MyResponse>("/api/my-endpoint"),
   ```

4. Register the view in `src/App.tsx`'s view router and add a `ViewId` in `src/components/LeftRailNav.tsx`.

---

## Adding a new API endpoint

```python
# backend/api.py
@app.post("/api/something", response_model=SomethingResp)
def something(req: SomethingReq):
    result = dash.some_helper(req.field)  # reuse existing dashboard logic
    return SomethingResp(...)
```

CORS regex already allows LAN + Capacitor origins. No middleware changes needed.

---

## Common tasks

| Task | Command |
|---|---|
| Build for production | `cd frontend && npm run build` |
| Type-check only | `cd frontend && npx tsc -b` |
| Update a dep | `cd frontend && npm install <pkg>@latest` |
| Run all tests | `./.venv/bin/python master_test_suite.py` (Python only) |
| Smoke-test backend | `curl -sS http://127.0.0.1:8000/api/today/cards` |

---

## Conventions

- **Tailwind utilities first.** Drop into `index.css` `@layer components` only when 3+ places need the same pattern.
- **`cn()` from `lib/cn.ts`** for conditional classes; avoid `clsx` directly.
- **`useQuery` for data, `useMutation` for actions.** Don't write `fetch()` in components.
- **`gr.Tabs(selected=...)` equivalent in React:** lift active view to App state, pass setter down.
- **Mobile-first responsive.** Default Tailwind classes for phone; add `sm:` / `lg:` for larger.
- **Touch targets ≥ 44px** on `pointer: coarse` (enforced by base CSS).
- **No inline styles** except for dynamic values (e.g. `style={{ color: plColor }}`).
- **Format helpers** (`fmtUSD`, `fmtPct`, `relTime`, `shortTime`) live in `lib/cn.ts` — use them, don't reformat in components.

---

## Troubleshooting

**Vite HMR not picking up changes:** restart Vite. Sometimes the cache gets confused after big edits.

**TypeScript error on `cn` not found:** `import { cn } from "../lib/cn"` — relative path matters because there's no path alias configured.

**`Cannot read property 'env' of undefined` (Vite env vars):** the cast in `api.ts` (`import.meta as ImportMeta & { env: ... }`) is the workaround for Vite 8 + TS strict. Don't remove it.

**CORS error in browser console:** check `backend/api.py` `allow_origin_regex`. Add your origin if on an unusual network (e.g. 172.x).

**`Cannot find module '@capacitor/...'`:** ran `npm install` but didn't restart Vite. Restart.

**Bundle bigger than expected:** check tree-shake — lucide imports must be named (`import { Brain } from "lucide-react"`), not default.
