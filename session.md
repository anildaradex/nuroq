# NuroQ — Session Log

> **Running, append-only log of what each working session did.** Newest entry on
> top. This is the FAST context file — read it first on every new session, then
> `HANDOFF.md` for the deeper architectural picture.
>
> **Maintenance convention** (also recorded in `CLAUDE.md`):
> - At the START of a session: read this file + `HANDOFF.md`.
> - DURING a session: append a dated bullet under "Current session" each time a
>   meaningful action is taken (feature shipped, bug fixed, file created,
>   config changed, service restarted). Not every tool call — meaningful steps.
> - At the END / when asked to "update handoff": promote "Current session" notes
>   into a numbered, dated session block here AND update `HANDOFF.md`.
>
> Raw, complete transcripts are also auto-saved by Claude Code under
> `~/.claude/projects/.../*.jsonl` — but those are uncurated. This file is the
> curated trail.

---

## Current session — 2026-06-07

**Replaced API-key auth with password login (single-user).** The 48-char
`X-NuroQ-Key` was strong but the UX was bad: cookies expired daily, scoped per
origin (cloud URL vs SSH-tunnel localhost = separate cookie jars = re-prompts),
flaky in cross-origin Capacitor WebView. Swapped to familiar password login.
- **New `backend/auth.py`** — PBKDF2-HMAC-SHA256 (600k iters, 16-byte salt),
  HMAC-SHA256-signed session token. Stdlib only, no new deps. Storage: new
  `auth_settings` row in the existing SQLite DB (NUROQ_DB_PATH). Seeded on
  first access with `INITIAL_PASSWORD = "nuroq"` (intentionally weak — user
  changes via the in-app form). Per-box `session_secret` survives restarts.
- **`backend/api.py`** — replaced `X-NuroQ-Key` middleware with session-cookie
  middleware (`_session_guard`). New endpoints:
  - `GET /api/auth/status` → `{authenticated, must_change_password}`
  - `POST /api/auth/login {password}` → sets httponly `nuroq_session` cookie (30d)
  - `POST /api/auth/logout` → clears cookie
  - `POST /api/auth/change-password {current_password, new_password}` — min 6
  Exempt paths: SPA shell + assets, /health, /docs, /api/auth/status, /api/auth/login.
- **Peer-compare auth flipped to password.** `NUROQ_COMPARE_KEY` gone; new
  `NUROQ_PEER_PASSWORD`. Server posts to peer's `/api/auth/login`, caches the
  session cookie in-process, re-logs in on 401. (Set this env var on the local
  box after rotating the cloud password to re-enable cloud-Gemini compare.)
- **`frontend/src/lib/api.ts`** — dropped `?api_key=` capture, `X-NuroQ-Key`
  header injection, `nuroq.api_key` localStorage. Added `credentials: "include"`
  on every fetch + new `UnauthorizedError`. New client methods: `authStatus`,
  `login`, `logout`, `changePassword`.
- **`frontend/src/App.tsx`** — outer `App` now polls `/api/auth/status` and
  renders `LoginScreen` until authenticated. Real app moved into inner
  `AuthenticatedApp`. Shows a centered backdrop-blur `ChangePasswordPanel`
  modal automatically while `must_change_password: true`; dismissible.
- **New `frontend/src/components/LoginScreen.tsx`** — password field, sign-in
  button, seeded-password hint. Exports a `ChangePasswordPanel` with current /
  new / confirm fields + client-side validation (length + match).
- **Verified end-to-end:** wrong pw → 401, `"nuroq"` → 200 + cookie, protected
  endpoint with cookie → 200, in-app password rotation (`"nuroq"` → `"TestPass123!"`)
  → server-confirmed (old pw 401, new pw 200, must_change_password now false),
  reset via `DELETE FROM auth_settings` re-seeds `"nuroq"`. Browser drove the
  full flow: LoginScreen rendered → typed pw → app loaded → modal auto-opened.
  No console errors. 110+ existing tests still pass.

**⚠️ Cloud deploy required to take effect on https://nuroq.nuroquant.com.** The
cloud is still running yesterday's bundle with the API-key gate. Push to `main`
(GitHub Actions auto-deploys) or `PROJECT_ID=nuroq-prod-anildara ./deploy/deploy_gce.sh`.
After deploy, open the cloud URL once → LoginScreen → password `"nuroq"` →
change-password modal appears → set a real password (and put it in
`NUROQ_PEER_PASSWORD` on the local box if you want compare to keep working).

**Why password auth is fine for this app (recorded for future-me):** single
user, paper trading, second factor is Telegram approve-buttons on real trades,
no PII besides positions. Password keeps the gate but trades the URL-paste UX
for a familiar form. Doesn't change the real risk model.

**Auth UX fix: paste `?api_key=…` ONCE per browser, never again.** User kept
hitting 401 on `/api/analyze/NVDA`; root cause was the SPA relied entirely on
the 24h httponly `nuroq_key` cookie, which (a) expires daily, (b) is per-origin
(cloud URL vs `localhost:8080` SSH tunnel = separate cookie jars = re-auth on
each), (c) is unreliable in cross-origin Capacitor WebView calls.
- **`frontend/src/lib/api.ts`** — on module load, capture `?api_key=` from URL
  → `localStorage["nuroq.api_key"]` + scrub the key out of the address bar via
  `history.replaceState` (so it doesn't sit in browser history). Wrap `get()` /
  `post()` to inject `X-NuroQ-Key` from storage on every call. Server still
  enforces auth (verified bare fetch → 401), but the SPA now self-authenticates
  forever on each browser. Same key in `.env` and Secret Manager, so the
  *same* paste authenticates against both local Gemma and cloud Gemini boxes.
- **`backend/api.py`** — cookie `max_age` 24h → 30d (belt-and-suspenders for
  curl / non-SPA clients / iOS WebView).
- **Verified end-to-end:** wiped cookie + reloaded, drove the rebuilt SPA
  through Analyze → NVDA, got `HOLD 56/100` with `cookieStillAbsent: true`,
  `storedKeyStillThere: 48`, no console errors.

**Explained the SSH-tunnel + Cloudflare reality:** SSH tunnel (`gcloud compute
ssh nuroq-backend -- -N -L 8080:localhost:8000`) reaches GCP uvicorn directly
bypassing Cloudflare. Verified `localhost:8080/health → 200 (ai_backend:gemini)`.
Tunnel does NOT bypass NuroQ's own auth (lives in uvicorn middleware). The
`nuroq-api` firewall rule has a stale source CIDR (`47.187.113.254/32`); SSH on
:22 is open so the tunnel works without touching it. Direct `:8000` from
browser would require updating the firewall to current IP.

**Local-Gemma vs Cloud-Gemini health check + in-app A/B compare (shipped):**
- **Verified local works.** Ran the exact `/api/analyze` path (`analyze_single_ticker_data`
  → `analyst.analyze()`) locally on NVDA with MLX-Gemma: **56/HOLD** (ai 68),
  ~44s cold model load + ~22s analyze. Cloud Gemini (Vertex) on the same ticker:
  **57/HOLD** (ai 75), ~8s warm. Both cite the same signals (weak downtrend,
  bullish BB lower-band reversal, P/E 31.46, ~85% rev growth) → backends agree
  within 1 pt on final score. Validates the swappable-backend design.
- **New A/B compare panel** (user picked "in-app A/B panel"):
  - Backend `GET /api/analyze/peer/{ticker}` (`backend/api.py`) — returns THIS
    instance's backend label + a *peer* instance's analysis fetched over HTTP.
    Config `NUROQ_COMPARE_URL` (default `https://nuroq.nuroquant.com`) +
    `NUROQ_COMPARE_KEY`. **Zero-config locally:** when unset AND local backend is
    `gemma`, it reuses `NUROQ_API_KEY` (the deploy script persists the SAME
    Secret-Manager key into local `.env`, so it authenticates against the cloud).
    Peer side is frontend-supplied local + only the peer is fetched → fast (~8s,
    no local Gemma re-run). Fails closed (peer=null + note).
  - **Gotcha hit & fixed:** the cloud sits behind Cloudflare, which **403s the
    default `Python-urllib/x` User-Agent** as a bot (curl's UA passes). Added a
    `User-Agent: NuroQ-Compare/2.0` header to `_peer_get`. Verified 403→200.
  - Frontend: AnalyzeView "AI Reasoning" tab gets a **Compare with cloud (Gemini)**
    button → two-column view (local `Gemma · local MLX` vs `Gemini · cloud Vertex`)
    with AI-score chips, ratings, reasoning, and **Δ AI score / Ratings agree**
    badges (`api.ts` PeerCompare type + `analyzePeer`; `CompareCol` component).
  - **Verified in-browser:** NVDA → Δ AI score 7, Ratings agree (both HOLD),
    Gemma 68 vs Gemini 75, cloud 9s. No console errors. (Local backend has auth on
    — open `localhost:8000/?api_key=<KEY>` once to set the cookie, like the cloud.)

**UI readability pass (WhatsApp-style) + US-dollar-green palette:**
- **Native font stack** — swapped the `Inter` webfont for a system-UI stack
  (`-apple-system` → SF Pro on Mac/iPhone, Segoe UI / Roboto elsewhere) in
  `tailwind.config.js`; removed the rsms.me Inter `<link>` + Inter-only
  `font-feature-settings` (kept JetBrains Mono for tickers/numbers, kept
  tabular-nums). Same clean, no-download approach WhatsApp uses.
- **Type scale nudged for readability** — `sm` 14→14.5px (WhatsApp body ~14.2),
  `base` 15→15.5px, `xs` 12.5→13px, comfortable line-heights; body fallback
  14→15px in `index.css`.
- **Light mode is now the DEFAULT** — `index.html` no longer hard-codes
  `class="dark"`; added a pre-paint boot `<script>` that applies the saved theme
  (default light). This also **fixed a latent persistence bug**: TopBar wrote
  `localStorage["nuroq.theme"]` but nothing read it on boot, so the choice never
  survived a reload. Now it does.
- **"Greenback" palette** — replaced the emerald/teal `buy`/`accent` tokens with
  deep US-dollar money-greens: `accent` `#147a45` (brand/buttons/focus/logo),
  `buy` `#1a8348` (gains/up). Both WCAG-AA on white text + as text on white.
  Updated the 4 hardcoded chart hexes (AnalyzeView BB bands + close line,
  PortfolioView equity curve, TodayA P&L sparkline).
- **Verified in-browser** (preview at :8000, built dist): light default + system
  font (15px) + exact greens confirmed via computed styles (accent rgb(20,122,69),
  buy rgb(26,131,72)); dark-mode toggle still works + persists; no console errors.
  Note: Today Hero + Watchlist render empty because `watchlist_today` is stale
  (data freshness, not styling).
- **`.claude/launch.json`** — pointed the preview server at `uv run uvicorn`
  (the TCC-safe path; the old `./.venv/bin/uvicorn` is blocked under `~/Documents`),
  with `NUROQ_BACKGROUND_SERVICES=0`/`NUROQ_AUTOSTART_AGENT=0` so previews don't
  fire Telegram/agent side effects.

---

## Session 6 — 2026-06-03 → 06-06 (COMPLETE)

> **TL;DR for next session:** NuroQ is LIVE in the cloud at
> **https://nuroq.nuroquant.com** (HTTPS via a named Cloudflare tunnel). GCE
> e2-medium VM `nuroq-backend` @ static IP **34.9.20.141** (us-central1-a, project
> `nuroq-prod-anildara`, 80GB disk). Gemini via **Vertex** (no key). **Auto-deploys
> on push to `main`** (GitHub Actions + Workload Identity Federation). Scheduler +
> live agent running; **paper trading only**. `main` == feature branch (PR #7 merged).
> Access the UI: open `https://nuroq.nuroquant.com/?api_key=<KEY>` once (sets a
> cookie). Key: `gcloud secrets versions access latest --secret=NUROQ_API_KEY --project=nuroq-prod-anildara`.
> Redeploy manually: `PROJECT_ID=nuroq-prod-anildara ./deploy/deploy_gce.sh`.

**Cloud migration — code made GCP-ready (2026-06-04):** Decided **Compute Engine
VM + Gemini**. Did the real restructuring so NuroQ can run on Linux:
- **MLX unblocked:** `pyproject.toml` gates `mlx-lm`/`mlx-lm-lora` behind
  `sys_platform=='darwin'` (Linux skips them); `dashboard.py` lazy-imports MLX
  (no top-level `from mlx_lm import`). Verified `import dashboard` + `backend.api`
  on a simulated no-MLX Linux env.
- **Swappable AI backend:** new `analyst_backends.py` (`GeminiBackend` via
  `google-genai`); `EnsembleAnalyst` routes through `NUROQ_AI_BACKEND` (gemma=local
  MLX / gemini=cloud) at the single `analyze()` chokepoint — same parse/consensus.
- **12-factor:** all DB access via `NUROQ_DB_PATH` (dashboard.py + backend/api.py
  hardcoded `nuroq.db` removed). **API auth:** `X-NuroQ-Key` middleware + open
  `/health`. Hardened `.dockerignore`.
- **Deploy scaffolding:** `deploy/Dockerfile.cloud` (no MLX, Gemini, TZ=ET),
  `deploy/deploy_gce.sh` (idempotent build→Secret Manager→disk→VM→firewall),
  `deploy/README.md`, `.env.cloud.example`.
- Tests still **104/104 green**. gcloud authed (anil.dara@gmail.com), billing open.
**🚀 DEPLOYED TO GCP (2026-06-04, LIVE):** Project **`nuroq-prod-anildara`**,
e2-medium VM **`nuroq-backend`** @ static IP **34.9.20.141** (`nuroq-ip`), us-central1-a.
(`update-container` stops/starts the VM → reassigns ephemeral IPs, so a reserved
static IP was attached; baked into the script.)
- `/health` → 200 (`ai_backend: gemini`); auth blocks w/o key (401), passes w/ key
  (200); `/api/propose-sells` → 200. Cloud Build image (mlx skipped, google-genai
  2.8.0 + torch). Gemini via **Vertex** (VM service account, no key). Secrets in
  Secret Manager. SQLite on host-path `/var/lib/nuroq` (mounted /data).
- **Gotchas hit & fixed:** (1) `gcloud builds submit --tag -f` invalid → added
  `deploy/cloudbuild.yaml` pointing at `Dockerfile.cloud`. (2) e2-small (2GB) +
  separate-disk `--container-mount-disk` → OOM + konlet fsck-on-restart race
  (crash loop). Fixed: **e2-medium (4GB)** + **host-path mount** on a 30GB boot
  disk. (3) Granted compute SA `artifactregistry.reader` + `aiplatform.user`.
  (4) Key regenerated each run → script now reuses Secret Manager key + persists
  to .env, never echoes it.
- Retrieve API key: `gcloud secrets versions access latest --secret=NUROQ_API_KEY --project=nuroq-prod-anildara`
- **Vertex VERIFIED:** `GET /api/analyze/NVDA` → 200 with a real, NVDA-specific
  Gemini analysis (Vertex via VM SA works end-to-end). Live agent confirmed
  running on the VM (`/api/agent/status` running:true, 9 tickers). Triggered
  `POST /api/research-cycle` to populate the cloud DB.
- **⚠️ Gemini parsing caveat (FOLLOW-UP):** Gemini's output format differs from
  the DPO-Gemma format `get_structured_data` expects → `ai_score` defaults to 50
  and `ai_reasoning` holds the raw JSON blob. Quant rubric is unaffected (real
  signal), but the AI tiebreaker contributes nothing until the parser/prompt is
  tuned for Gemini. Worth fixing next.
- **In-process scheduler added** (`scheduler.py`, `NUROQ_INPROC_SCHEDULER=1`):
  daemon threads run research @ 03:30 ET + sell-proposals @ 08:00 ET (weekdays) —
  replaces launchd cron in the container. `/health` now reports scheduler state.
  Tests 104→108. Deploy uses **unique image tags** (vYYYYMMDD-HHMMSS) so
  `update-container` reliably re-pulls (preserves VM IP + /data).
- **Gemini parsing FIXED (2026-06-05):** scoring path now uses Gemini structured
  output (JSON schema + 2048-tok cap) so `ai_score` parses (was defaulting to 50
  — verbose output truncated the JSON before the score key). Threaded a
  `structured` flag through `analyst.analyze()` → only scoring paths (get_consensus,
  analyze_single_ticker_data, analyze_stock) get JSON mode; Ask-AI stays free-text.
  Tests 108→110 (`TestGeminiStructuredOutput`).
- **GitHub Actions auto-deploy ADDED (2026-06-05):** `.github/workflows/deploy.yml`
  deploys to the VM on push (build Dockerfile.cloud → push AR → update-container).
  Auth via **Workload Identity Federation — no stored key.** Set up in GCP:
  WIF pool `github-pool` + provider `github-provider` (owner-restricted),
  SA `gh-deployer` (artifactregistry.writer + compute.instanceAdmin.v1 +
  serviceAccountUser), repo bound via workloadIdentityUser. See deploy/README.md.
- First WIF Actions run verified GREEN (build → push → update-container).

**React UI + Cloudflare Tunnel — HTTPS from anywhere (2026-06-05/06):**
- `Dockerfile.cloud` is now **multi-stage**: stage 1 builds the React SPA with
  `VITE_API_BASE=""` (same-origin `/api` calls), baked into the image so the
  backend serves the full UI at `/`. cloudflared installed in the image.
- `deploy/entrypoint.sh` runs **cloudflared + uvicorn**: QUICK tunnel by default
  (ephemeral `*.trycloudflare.com`, surfaced at `GET /tunnel-url`); NAMED tunnel
  when `CLOUDFLARED_TOKEN` is set.
- **Cookie auth** so the served SPA works without a key in every URL: the auth
  middleware drops a `nuroq_key` cookie when a valid `?api_key=` is seen, and
  accepts it on `/api/*`. SPA shell + `/assets/*` exempt. Visit
  `https://<host>/?api_key=KEY` once → whole UI authenticates.
- **PERMANENT named tunnel LIVE:** domain **nuroquant.com** (registered on
  Cloudflare), tunnel `nuroq`, public hostname **nuroq.nuroquant.com → HTTP
  localhost:8000** (gotcha: the route service must be **http**, not https, or
  cloudflared 502s). `CLOUDFLARED_TOKEN` in Secret Manager + injected; the deploy
  script resolves it from env/.env/Secret-Manager. URL: **https://nuroq.nuroquant.com**.
- **'Gemma' → 'AI' labels:** renamed user-facing/log strings (Ask-AI UI spinner,
  `get_consensus` logs, `/docs` docstrings) to backend-agnostic "AI" (cloud runs
  Gemini, not Gemma). Internal identifiers (`_gemma_lock`, `"gemma"` backend) kept.

**Bug fixes for cloud usability (2026-06-06):**
- **Async market scan (fixes Cloudflare 524):** the scan ran past Cloudflare's
  100s proxy timeout → 524. Now `start_scan_async()`/`scan_status()` (daemon
  thread) in dashboard.py; `POST /api/scan` returns immediately; `GET
  /api/scan/status` is polled by the UI. ScannerView rewritten to be server-backed.
- **CPU-only torch image:** the default torch wheel pulled the ~3GB **NVIDIA CUDA
  stack** → ~5GB image **filled the 30GB boot disk** (container crash-loop "no
  space left"). `Dockerfile.cloud` now installs **CPU torch first**
  (`--index-url .../whl/cpu`) → image ~1.5GB. Boot disk also resized **30→80GB**.
- **Persist Analyze/Scan results across navigation:** views unmount on nav
  (App.tsx conditional render) so component-local results were lost. Moved into the
  **global react-query cache** — AnalyzeView keeps `["analyze-result"]`
  (staleTime/gcTime Infinity); Scanner reads server-backed `/api/scan/status`.

**Merge to main (2026-06-06):** PR #6's API squash-merge was INCOMPLETE and
stripped `.github/workflows/deploy.yml` (the credential-helper token lacks
`workflow` scope). Recovered `main` non-destructively (forward commit, no force).
Final: **PR #7 created and merged via a real merge commit (`52a1598`)**; verified
`origin/main == feat` exactly. `deploy.yml` triggers on push to `main` only.

**End state:** tests **110/110 green**; NuroQ live at https://nuroq.nuroquant.com;
`main` == feature branch; auto-deploy on merge to main.

**Open / next-session candidates:**
- Confirm the merge-to-main auto-deploy Action finished green.
- **Today "Design lab" cleanup** — pick one of the 4 layout variants (TodayA-D),
  delete the others + the switcher.
- **Backups:** snapshot the 80GB boot disk on a schedule (SQLite + cloudflared
  log live on the host-path `/var/lib/nuroq`). `gcloud compute disks snapshot`.
- Optional: **Tailscale** as a more-private alternative to the public tunnel.
- iOS app → re-point to https://nuroq.nuroquant.com + resubmit (`PUBLISHING.md`).
- Live trading still **OFF** (needs `NUROQ_LIVE_TRADING=1` + `WASH_SALE_AWARE`/`§475`).
- Tune sell-proposal thresholds + Today design once data accrues.

---

## Session 6 — 2026-06-03 (earlier)

**§475(f) mode + PDT-rule context (tax/regulatory):**
- Reviewed two user claims for accuracy (web-verified): (1) **PDT rule abolished**
  — SEC approved FINRA's proposal 2026-04-14, effective ~2026-06-04; $25k min &
  "Pattern Day Trader" label gone, replaced by risk-based intraday margin (Rule
  4210, $2k margin min), 18-mo broker phase-in to ~Oct 2027. NuroQ had NO PDT
  logic, so nothing to remove. (2) **§475(f) MTM election** makes wash-sale
  (§1091) + $3k cap-loss limit inapplicable (ordinary gains/losses, Form 4797) —
  it's an election statement + Form 3115, NOT a "Form 475." **2026 window CLOSED
  for existing individuals** (was due Apr 15, 2026); open for a new entity (~75-day
  Rev. Proc. 99-17) or 2027 (Apr 15, 2027).
- **Implemented `NUROQ_SECTION_475` flag (default OFF).** Built on the agreed
  basis: ready for a future entity / 2027 election, never assumes the election.
  - `dashboard.section_475_active()` helper (reads env live; no restart needed).
  - `wash_sale_check()` short-circuits at the top when the flag is on → returns
    `risk=False` + explanatory hint BEFORE any Alpaca call or cache write. Single
    chokepoint neutralizes ALL three BUY gates (live_agent, handle_quick_trade,
    iOS OrderReviewModal) since they all key off `risk`. Not cached → toggling is
    instant.
  - `alpaca_executor._connect` safety belt now accepts `NUROQ_SECTION_475=1` OR
    `NUROQ_WASH_SALE_AWARE=1` to satisfy the live-trading wash-sale acknowledgment.
- **Tests: 91 → 96** (`TestSection475`): helper default-off, short-circuit
  returns-before-Alpaca, default-path guard still flags loss re-entry (regression),
  §475 satisfies the live belt, belt still blocks with neither ack. `master_test_suite.py` green (4.2s).
- **Key principle reaffirmed:** the wash-sale code is *advisory only* — removing/
  disabling it does NOT change real tax treatment (set by the filed election +
  1099-B), and NuroQ is still paper-only (no live taxable trades today).

**Option B — core quant layer now PROPOSES sells (shipped):**
- `dashboard.propose_sells()` — PURE, fails-closed. Deliberate pass over held
  Alpaca positions (via `list_positions`) × current watchlist scores → ranked
  proposals of three kinds:
  - **TAX_LOSS_HARVEST** — §475-GATED (suppressed unless `NUROQ_SECTION_475=1`,
    since otherwise it's a wash-sale trap). Material paper loss (≥2%) on a
    non-strong holding (score < 55 or off-list) → realize the now-deductible loss;
    re-entry unrestricted under §475.
  - **ROTATE** — weak holding (score ≤ 45) + a non-held BUY candidate out-scoring
    it by ≥20 → rotate capital into the stronger name (different ticker = never a
    wash sale; PDT-free = same-session fine).
  - **EXIT_WEAK** — weak holding, no stronger candidate → trim/close.
  Tunables: `SELL_PROPOSE_WEAK_SCORE=45`, `HARVEST_SCORE_CEILING=55`,
  `HARVEST_MIN_LOSS_PCT=0.02`, `ROTATE_SCORE_EDGE=20`.
- `dashboard.log_sell_proposals()` — writes PROPOSE_SELL rows to live_triggers
  (auto-surface in the React Recent Activity feed); deduped per (date,ticker,kind).
- Surfaced 3 ways: **Next Actions** card (`render_next_actions`, top 4),
  **`GET /api/propose-sells`** (new backend endpoint, returns proposals + §475
  flag), and the **premarket cron** (`premarket_refresh.main` now calls
  `log_sell_proposals()` each morning → daily feed entries).
- ADVISORY ONLY — nothing auto-executes; user acts via Watchlist ⚡ / Telegram /
  close_position. Verified live: ran against the real paper account → coherent
  ROTATE proposals (SPY/AAPL scores decayed below the live SELL threshold).
- Tests 96 → **104** (`TestSellProposals`): harvest suppressed w/o §475, harvest
  under §475, strong-loser-not-harvested, rotate vs exit-weak, healthy=none,
  fails-closed, dedup. Suite green (~4.2s).

✅ **Backend started 2026-06-03 23:38, now serving new code.** `/api/propose-sells`
→ 200 with live ROTATE proposals (SPY/AAPL/MU scores decayed to 15-25). LiveAgent
autostarted (154 tickers, 9 held), Telegram digest sent.
⚠️ Started MANUALLY (`uv run uvicorn backend.api:app`), NOT via launchd — the
`com.nuroq.backend` job fails with **exit 78 / TCC** (no logs on kickstart). Manual
process won't survive reboot. Fix: re-grant Full Disk Access to `/opt/homebrew/bin/uv`
+ `/bin/sh`, then `launchctl kickstart -k gui/$(id -u)/com.nuroq.backend`.

**Stated north-star (recorded):** evolve NuroQ into a fully autonomous live-trading
app — places autotrades without intervention, backtests, measures its own efficacy,
learns from mistakes, self-improves. Build every feature toward that trajectory.

**Open / next-session candidates:**
- Restart the backend to serve `/api/propose-sells`; build the React UI for sell
  proposals (feed already shows PROPOSE_SELL rows; a dedicated card w/ SELL buttons
  would close the loop).
- Option C: first-class Investor vs Trader-§475 regime surfaced in the UI.
- Tune proposer thresholds against real watchlist behavior (initial live run
  flagged several ROTATEs — confirm the score-decay band feels right).

---

## Session 5 — 2026-05-31 → 2026-06-01 (COMPLETE)

**AI & analysis**
- Built **Ask AI** free-form Q&A under the Analyze chart (`AskBar` in
  `AnalyzeView.tsx`, `POST /api/ask`, `dashboard.ask_about_ticker`). Grounds
  Gemma with price action (month-by-month) + cached news (cache-first, 8s live
  fallback) + SEC RAG (10s bounded) + fundamentals. Reuses drift detector.
  Verified live (NVDA 7.6s, RKLB "why did it increase in May" → correct 78.7%).

**Trading correctness & UX**
- **Wash-sale Layer-1 guard** (IRS §1091) on ALL BUY paths: `wash_sale_check()`
  via `alpaca_api.get_recent_fills()`, 5-min memoized, fails-open. Gated in
  `handle_quick_trade`, `live_agent._handle_buy_crossing` (suppress + info
  Telegram), Telegram REFEX (→ "EXECUTE ANYWAY" REFEXOK override). `/api/wash-sale`
  + OrderReviewModal red-banner two-tap override. Safety belt in
  `alpaca_executor._connect` (live trading needs NUROQ_WASH_SALE_AWARE=1).
- **Limit-bracket orders** (`submit_bracket_order(limit_price=...)`); modal
  defaults to Limit, SL/TP visible for both types (fixed hide-on-Limit bug).
- **OrderReviewModal** — spacious review surface (Watchlist ⚡), editable
  shares/SL/TP, cost/risk/reward/R:R, big Cancel+Submit.
- **Double-submit prevention** — React 1.5s debounce + idempotency UUID
  (backend 10s replay cache) + isPending. (Fixed the SAN-bought-twice bug.)
- **Watchlist ⚡ Trade button** moved to 2nd column (was off-screen mobile);
  refresh uses `isFetching` so spinner shows every refetch.

**Telegram**
- **Refresh-BUY alerts** at session open: 1 msg/BUY ticker w/ sizing +
  ✅ EXECUTE / ⏭️ Dismiss buttons. Idempotent per-ticker/day. Cap 12.
- **Session-open digest** table once/day.
- **Score-shift detector** → LOG-ONLY (feed via SCORE_SHIFT_UP/DOWN rows in
  live_triggers, NO Telegram — momentum pings were too noisy).
- **SL/TP exit alerts rebuilt**: deduped (`_alert_state` re-arms on zone exit —
  fixed the every-60s MSFT spam) + rich (shares, value, cost basis, unrealized
  P&L $/%) + actionable (💰 SELL ALL / ✊ Hold buttons → SELLEX/SELLHOLD
  callbacks → `close_position`).

**Portfolio / Alpaca sync (big one)**
- **Two-way reconcile** (`_reconcile_with_alpaca`, runs every refresh_prices):
  removes phantom local rows (killed MSFT "position not found"), imports Alpaca
  positions missing locally (avg cost from position, SL/TP from open brackets),
  backfills advisory ATR SL/TP on bare rows. New executor helpers:
  `list_positions`, `list_position_symbols`, `get_bracket_levels` (nested-leg
  aware). Local tracker went 2→11 positions matching Alpaca.
- **Real protective OCO orders** (`submit_protective_oco`, OCO order class):
  placed live SL+TP on the 7 bare positions (AAPL, AMD, GOOG, MU, NVDA, SPY, V)
  so they auto-exit even if NuroQ is offline. `/api/protect` resolves shares
  from LIVE position (not stale tracker). Telegram PROTECT callback. NOTE:
  META/NVO/SAN/SONY still TP-only (no stop) — user chose "bare 7 only".
- **Portfolio page redesign**: 4 stat cards (cost basis, market value,
  unrealized P&L, N-day return), beautiful equity-curve area chart (7/30/90D
  toggle, green/red by direction, baseline ref line), holdings table with
  cost-basis + P&L$ columns + TOTAL footer.

**Design / infra**
- **Global type scale bump** (Google Finance-inspired): overrode Tailwind
  `fontSize` scale + base body 13→14px + comfortable line-heights + table cell
  padding (`px-3 py-2.5`). One CSS change, all views inherit. Fixed the cramped
  Bloomberg-density feel.
- Cron: premarket 08:15→08:00 CT, research 01:00→03:30 CT, Memorial-Day holiday
  rollback in `run_research_cycle`, premarket news scope trimmed to actionable
  subset (~83min → ~5-7min).
- launchd backend autostart via `uv run` (TCC-safe, uv in /opt/homebrew);
  `restart.sh` launchd-aware (`scripts/install-backend.sh`).
- Logging convention established: `session.md` + `CLAUDE.md` (auto-read on
  session start; this is that file).
- Fixed live-agent AttributeError crash (`_check_score_shift` missing from a
  prior garbled-edit; re-added).

**End state:** 91/91 tests pass · backend running via launchd on :8000 ·
iOS bundle synced · 11 Alpaca positions, 7 with full OCO protection · branch
`feat/algo-claude-improvements` (substantial uncommitted work).

**Open / next-session candidates:**
- Protect the 4 TP-only positions (META/NVO/SAN/SONY) with full OCO if wanted
  (needs cancel + replace of existing TP order).
- Commit the working tree (lots uncommitted on `feat/algo-claude-improvements`).
- Equity history is thin (30/90d show +0.00%, only 7d has movement) — fills in
  as the paper account accrues daily snapshots.
- Verify the new type scale on phone; tune any spot that wraps/oversizes.

---

## Session 4 — 2026-05-25 (evening)

- Today **design lab**: 4 layout variants (`TodayA-D.tsx`) behind a switcher.
- **launchd cron** installed (research + premarket) via `scripts/install-schedules.sh`.
- FastAPI **auto-starts live agent** on boot (`NUROQ_AUTOSTART_AGENT=1`).
- See `HANDOFF.md` "Session 4" for detail.

## Session 3 — 2026-05-25 (earlier)

- All 8 React views wired (Analyze, Watchlist, Portfolio, Scanner, Agent,
  Signals, System, Logs). Quick Trade actually trades via `/api/trade`.
- Command palette analyzes tickers. `PUBLISHING.md` (TestFlight + App Store).

## Session 2 — 2026-05-24 (evening)

- **React + FastAPI + Capacitor iOS** stack stood up alongside Gradio.
- Today view full fidelity; status pills; iOS responsive + native app.
- See `HANDOFF.md` for the migration detail.

## Session 1 — 2026-05-24

- Gradio UI redesign (Phases A–D): density pass, Today tab, drill-down,
  ⌘K palette, command bar. Test-suite watchlist-wipe bug fixed.

---

_Older history predates this log — see `HANDOFF.md`, `CAPABILITIES.md`,
`ARCHITECTURE.md`._
