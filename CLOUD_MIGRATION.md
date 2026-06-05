# NuroQ — Cloud Migration Plan

> Comprehensive plan to move NuroQ from a single Apple-Silicon Mac (launchd +
> MLX-Gemma + local SQLite) to an always-on cloud deployment for **personal use**.
> Written 2026-06-03. Pairs with `ARCHITECTURE.md` (system design) and `HANDOFF.md`.

---

## 0. Executive summary

NuroQ is ~90% cloud-ready already, because of one lucky design fact: **every AI
inference is routed through a single chokepoint** (`analyst.analyze()` /
`EnsembleAnalyst`, dashboard.py). The compute layer (`scoring.py`, `live_agent.py`,
`data_fetcher.py`, `alpaca_executor.py`, etc.) is plain Python that runs anywhere.

There is exactly **one hard blocker**: the AI tiebreaker runs on **MLX, which is
Apple-Silicon-only**. The current `Dockerfile` even tries to `uv pip install mlx-lm`,
which fails on Linux. So the migration is really one big decision (what replaces
MLX) plus a handful of standard 12-factor cleanups (config, secrets, persistence,
scheduler, auth).

**Recommended personal-use stack:**

| Layer | Choice | ~$/mo |
|---|---|---|
| Always-on app (API + live agent + websocket + Telegram) | **Fly.io** 1× shared-cpu, 1 GB | $4–7 |
| Persistence | **SQLite on a Fly Volume** (3 GB) + **Litestream** → S3/R2 backup | $1 + ~$0 |
| AI tiebreaker | **Gemini 2.5 Flash/Flash-Lite API** behind `analyst.analyze()` | $8–15 |
| Scheduler (research + premarket) | Fly **scheduled Machines** (or in-proc APScheduler) | $0 |
| Frontend (React static) | **Cloudflare Pages / Vercel** free tier | $0 |
| Market data | **Polygon** (you already pay) + **Alpaca IEX** stream (free) | $0 incremental |
| **Incremental total** | | **≈ $15–25/mo** |

If preserving the **DPO-fine-tuned Gemma** matters (you have `train_dpo.py` +
`TRAINING_GUIDE.md` invested), swap the AI line for **Modal serverless GPU**
hosting your own Gemma (~$25/mo, often $0 after Modal's $30/mo free credit at
personal volume). See §2.

> Bottom line: **personal-use cloud NuroQ is roughly the price of two coffees a
> month** on top of your existing Polygon subscription. Compute is cheap; the only
> real lever is the AI backend.

---

## 1. Current vs. cloud — gap analysis

| Concern | Today (Mac) | Cloud target | Effort |
|---|---|---|---|
| **AI inference** | Gemma-4B via **MLX (Apple-only)**, `_gemma_lock` for Metal | Hosted LLM API **or** Modal-hosted Gemma, behind same `analyze()` | **High (the decision)** |
| Always-on process | launchd `com.nuroq.backend` (uv run uvicorn) | 1 always-on container | Low |
| Cron | launchd plists (research 03:30 CT, premarket 08:00 CT) | Platform scheduler / APScheduler | Low |
| DB | local `nuroq.db` SQLite (WAL) | SQLite on volume **or** managed Postgres | Low–Med |
| Secrets | `.env` file | Platform secrets / env | Low |
| Filesystem perms | macOS **TCC/Full-Disk-Access** pain (exit 78!) | Gone — Linux container | Negative (a relief) |
| API auth | **none** (LAN-only, unauthenticated) | **API key/JWT + HTTPS — mandatory** | **Med (do not skip)** |
| Telegram bot | single poller (getUpdates conflict gotcha) | exactly **one** instance polls | Low (already handled) |
| Market-hours clock | `is_market_hours()` uses **naive local time** (assumes ET host) | Set container TZ=America/New_York or make tz-aware | Trivial |
| Embeddings (RAG) | `sentence-transformers` (CPU, works on Linux but heavy) | Keep on CPU, or hosted embedding API | Low |
| Frontend | Vite build served for LAN/iOS | Static build on CDN, HTTPS base URL | Low |
| iOS app | Capacitor → LAN IP | Re-point to cloud HTTPS, resubmit TestFlight | Low |

**Net:** one big swap (MLX) + auth + config plumbing. The 14 launchd/TCC headaches
documented in `HANDOFF.md` all *disappear* in a Linux container.

---

## 2. The one real decision — what replaces MLX-Gemma

The AI is a **gated 10-point tiebreaker** (only applied when the quant base ≥ 40,
`scoring.py:163`). It is NOT the core signal — the deterministic 100-pt rubric is.
That means you have wide latitude; the AI swap is low-risk to the system's edge.

All three options keep the **exact same `analyst.analyze(ticker, prompt) → {score,
reasoning, …}` signature**, so the blast radius is one class. The `_gemma_lock`
(Metal serialization) is simply deleted — replaced by an async concurrency limit.

| Option | What it is | Pros | Cons | ~$/mo (personal) |
|---|---|---|---|---|
| **A. Hosted LLM API** *(recommended)* | Gemini 2.5 Flash-Lite/Flash or Claude Haiku behind `analyze()` | Zero GPU ops; cheap; scales; better reasoning than 4B | **Loses your DPO fine-tune**; per-call cost; external dependency | **$8–34** (Flash-Lite cheapest, Haiku priciest) |
| **B. Self-host your Gemma on serverless GPU** | Modal/Replicate runs your DPO MLX→safetensors Gemma | **Keeps the DPO model**; private; pay-per-second | GPU cold starts (~secs); convert MLX→HF/vLLM; more ops | **~$25** (often **$0** under Modal's $30 credit) |
| **C. Hybrid — Mac as inference appliance** | Cloud runs everything; calls home Mac's Gemma over Tailscale/Cloudflare Tunnel | Cheapest; keeps DPO model as-is | Mac must stay on/online; not "true cloud"; home-uptime risk | **~$0** extra |

**Recommendation:** Start with **A (Gemini 2.5 Flash-Lite)** — simplest, and since
the AI is only a 10% tiebreaker, you lose little by dropping the 4B fine-tune. If a
later A/B shows the DPO Gemma materially beats Flash on your tickers, move to **B
(Modal)**. Build the interface so A and B are a one-line config swap.

```python
# analyst_backends.py  — the ONLY new abstraction needed
class Analyst(Protocol):
    def analyze(self, ticker: str, prompt: str) -> dict: ...

class HostedLLMAnalyst:      # Gemini / Anthropic — Option A
    def __init__(self, model, api_key, max_concurrency=4): ...
    def analyze(self, ticker, prompt) -> dict: ...   # same return shape as today

class RemoteGemmaAnalyst:    # Modal endpoint — Option B
    def analyze(self, ticker, prompt) -> dict: ...

# wiring: chosen by env, returns the same object dashboard.py already uses
ANALYST = make_analyst(os.getenv("NUROQ_AI_BACKEND", "gemini"))
```

---

## 3. Target architecture (process topology)

```
                       ┌────────────────────────── Cloudflare Pages / Vercel (free)
   iPhone / browser ───┤  React static bundle (HTTPS)
                       └──────────────┬───────────  calls  ─────────────┐
                                      │  HTTPS + API key                  ▼
        ┌─────────────────────────────────────────────────────────────────────┐
        │  Fly.io — 1 ALWAYS-ON Machine (shared-cpu-1x, 1 GB)                   │
        │  • FastAPI (backend/api.py)        ← + auth middleware                │
        │  • LiveAgent  → Alpaca **IEX websocket** (bars, market hours)         │
        │  • Background services: NewsPoller(30m), LLM queue, position monitor, │
        │    Telegram bot (single poller)                                       │
        │  • analyst.analyze() ─────────► Gemini API  (or Modal Gemma)          │
        │  • SQLite (nuroq.db) on attached Volume  ──► Litestream → R2/S3       │
        └───────────────┬───────────────────────────────┬─────────────────────┘
                        │ scheduled Machines             │ REST
        ┌───────────────▼──────────────┐        ┌────────▼─────────┐
        │ research_cycle.py  (nightly)  │        │ Polygon  (data)  │
        │ premarket_refresh.py (AM)     │        │ Alpaca   (orders)│
        │  NUROQ_BACKGROUND_SERVICES=0  │        └──────────────────┘
        └───────────────────────────────┘
```

**Key rules carried over from today (already in `CLAUDE.md`):**
- **Exactly one** process runs the Telegram poller and the Alpaca websocket
  (getUpdates / stream conflict). The scheduled jobs MUST set
  `NUROQ_BACKGROUND_SERVICES=0` — same flag, same reason as the launchd crons.
- Live trading still gated behind `NUROQ_LIVE_TRADING=1` **and**
  (`NUROQ_WASH_SALE_AWARE=1` **or** `NUROQ_SECTION_475=1`).

---

## 4. Code restructuring — concrete checklist

1. **Analyst abstraction (§2).** New `analyst_backends.py`; make `EnsembleAnalyst`
   delegate to the configured backend. Delete `_gemma_lock`; add async semaphore.
   *This is the only architecturally significant change.*
2. **Two Docker images / one build arg.** The current `Dockerfile` installs
   `mlx-lm` (fails on Linux). Split deps: move `mlx-lm`, `mlx-lm-lora` to an
   **optional `[mac]` extra** in `pyproject.toml`; the cloud image installs the
   base set only. Change `CMD` from `python dashboard.py` (Gradio) to
   `uvicorn backend.api:app`. Keep `sentence-transformers` (CPU RAG) unless you
   move embeddings to an API.
3. **12-factor config.** Everything via env: `DB_PATH`/`DATABASE_URL`,
   `POLYGON_API_KEY`, `ALPACA_*`, `TELEGRAM_*`, `NUROQ_AI_BACKEND`, model + key,
   `NUROQ_API_KEY` (new). No hardcoded `nuroq.db` / paths (a few `sqlite3.connect("nuroq.db")`
   calls in dashboard.py must read `DB_PATH`).
4. **Auth middleware.** FastAPI dependency that checks `X-NuroQ-Key` against
   `NUROQ_API_KEY`; reject otherwise. The app is currently wide open — this is
   non-negotiable once it has a public URL. (Frontend sends the key; for a
   personal app a single static key + HTTPS is fine.)
5. **Persistence.** Simplest: SQLite on a Fly Volume + **Litestream** for
   continuous backup to Cloudflare R2/S3 (point-in-time restore, ~free). Only go
   **Postgres** (Neon/Supabase free tier) if you later want multi-instance — then
   abstract the handful of raw `sqlite3` calls (WAL pragma etc. become conditional).
   For single-user, **SQLite-on-volume is the right call.**
6. **Scheduler.** Replace `scripts/install-schedules.sh` (launchd) with either
   (a) Fly **scheduled Machines** running `research_cycle.py` / `premarket_refresh.py`,
   or (b) in-process **APScheduler** in the always-on app. Drop the `pmset` wake
   hack (`HANDOFF.md`) — cloud is always on.
7. **Timezone.** Set container `TZ=America/New_York` (or make `is_market_hours()`
   tz-aware with `zoneinfo`). Removes the "assumes ET host" footgun.
8. **Frontend.** `VITE_API_BASE=https://nuroq.fly.dev`; build → Cloudflare Pages.
   iOS: re-point, rebuild, resubmit (see `PUBLISHING.md`).
9. **Observability.** Healthcheck endpoint (you have status helpers already),
   Fly metrics/log drain, and route fatal errors to the existing Telegram channel.

**Migration phases (each independently shippable):**
- **P1 – AI swap (local):** add hosted backend, A/B vs Gemma on a sample, keep tests green.
- **P2 – Containerize:** cloud Dockerfile, env config, auth, SQLite-on-volume; deploy to Fly; verify `/api/*` over HTTPS.
- **P3 – Scheduler:** port research + premarket to scheduled Machines.
- **P4 – Frontend/iOS:** CDN + re-point.
- **P5 – Backups + observability:** Litestream, healthcheck, error alerts.
- **P6 – Go-live hardening (later):** secrets rotation, kill-switch, the live-trading belts before flipping `NUROQ_LIVE_TRADING`.

---

## 5. How the API calls are made — call-volume model

Three external dependencies. Volumes derived from the actual code
(`research_cycle.py --top-n 150`, `news_engine.py` 30-min poll, gated AI).

### 5a. Market data — **Polygon** (REST, you already subscribe)
- **Research cycle (nightly):** ~150 tickers × {OHLCV history + fundamentals + news + SEC RAG} ≈ **300–600 calls/night**, rate-limited.
- **Premarket refresh (AM):** 1 grouped-daily snapshot (all tickers, 1 call) + news for the actionable subset (cap 30) ≈ **~31 calls**.
- **NewsPoller:** every 30 min during the day, held + borderline names ≈ **~150–300 calls/day**.
- **Total Polygon: ~600–900 REST calls/day**, all comfortably inside a paid tier's rate limits. **Cloud does not change this** — same calls, just from a Linux box.

### 5b. Brokerage — **Alpaca** (free)
- **Websocket:** 1 persistent **IEX** stream (`wss://stream.data.alpaca.markets/v2/iex`), ~150 ticker bar subscriptions during market hours. **Streaming, not per-call.** (Free IEX feed; SIP feed is +$99/mo and not needed.)
- **REST:** positions/orders/fills on demand — reconcile each refresh, trade submits, `get_recent_fills` (wash-sale). ~**hundreds/day**, free.

### 5c. AI tiebreaker — **the only NEW metered call** (replaces local Gemma)
Routed through `analyst.analyze()`:
- **Nightly research:** ~150 deep analyses → **150 calls**.
- **Intraday event-driven** (`event_stream` notable events): **~20–100/day**.
- **Ask-AI (on demand):** **~10–30/day**.
- **≈ 200–280 LLM calls/day ≈ 6,000–8,500/month.**

**Token math (per call):** prompt ≈ price-action + fundamentals + news + bounded
SEC-RAG ≈ **~2,000 input tokens**; output ≈ **~400–500 tokens**.
- ≈ **15 M input + ~3.5 M output tokens / month** (at ~250 calls/day).
- Prompt caching (system prompt + RAG context are reused) and **batching the
  nightly 150** (it's offline) cut this materially.

---

## 6. Cost estimate — personal use (monthly, incremental over Polygon)

Assumes ~250 AI calls/day → **15 M input + 3.5 M output tokens/mo**.

| Item | Detail | Low | Typical | High |
|---|---|---|---|---|
| **Compute** (Fly always-on) | shared-cpu-1x, 1 GB, 24/7 | $4 | $6 | $10 |
| **Volume + IPv4** | 3 GB vol ($0.45) + dedicated IPv4 ($2) | $2 | $3 | $3 |
| **Backups** (Litestream → R2) | a few GB egress/storage | $0 | $0 | $1 |
| **AI — Option A: Gemini Flash-Lite** | cheapest hosted | $3 | $5 | $8 |
| **AI — Option A: Gemini 2.5 Flash** | $0.30/M in, $2.50/M out → 15M·0.30 + 3.5M·2.50 ≈ **$4.5+$8.8** | — | **$13** | $18 |
| **AI — Option A: Claude Haiku 4.5** | $1/M in, $5/M out ≈ $15+$17.5 (− caching/batch) | — | **$20–34** | $34 |
| **AI — Option B: Modal Gemma (DPO)** | A10G $1.10/hr, ~0.75 hr/day ≈ $25; **−$30 credit** | **$0** | $0–10 | $25 |
| **Frontend CDN** | Cloudflare Pages / Vercel | $0 | $0 | $0 |
| **Alpaca** | paper + live, IEX feed | $0 | $0 | $0 |

**Realistic personal-use totals (incremental, excl. your existing Polygon):**

| Scenario | Stack | **~$/mo** |
|---|---|---|
| **Cheapest** | Fly + SQLite-vol + **Modal Gemma under free credit** (keeps DPO model) | **~$10** |
| **Recommended** | Fly + SQLite-vol + **Gemini 2.5 Flash-Lite/Flash** | **~$15–25** |
| **Premium reasoning** | Fly + **Claude Haiku 4.5** (no caching) | **~$30–45** |

> Plus your current **Polygon** subscription (unchanged: Starter ~$29 → Advanced
> ~$199 depending on tier) and **$0** Alpaca. If you ever add the SIP data feed,
> that's +$99/mo — not needed for this strategy.

**Sensitivity:** AI cost scales linearly with call volume × tokens. Halving the
RAG context or batching the nightly run roughly halves the AI line. Compute is
fixed and tiny. So your monthly bill is **dominated by the AI backend choice**,
and even the premium path is < $50/mo for personal use.

---

## 7. Risks & carry-over gotchas

- **Single-writer Telegram/websocket:** never run two app instances polling
  Telegram or streaming bars — the `getUpdates` conflict from `CLAUDE.md` applies
  in cloud too. Scheduled jobs use `NUROQ_BACKGROUND_SERVICES=0`.
- **Public API = attack surface:** auth + HTTPS before exposing. Don't ship the
  current unauthenticated backend to a public URL.
- **DPO model loss (Option A):** going hosted abandons your fine-tune. Keep the
  interface swappable; A/B before committing.
- **Market-data licensing:** Polygon/Alpaca personal API terms generally fine for
  personal use; re-confirm if you ever expose data to others.
- **Live trading:** unchanged safety belts — `NUROQ_LIVE_TRADING` +
  `NUROQ_WASH_SALE_AWARE`/`NUROQ_SECTION_475`. Add a cloud **kill-switch** (env
  flag + Telegram command) before going live.
- **Cold starts (Option B):** Modal GPU cold start adds a few seconds to the
  *first* intraday call; fine for a 10% tiebreaker, batch the nightly run.

---

## 8. Pricing sources (verified 2026-06)
- Fly.io pricing — https://fly.io/docs/about/pricing/ , https://fly.io/pricing/
- Gemini API pricing — https://ai.google.dev/gemini-api/docs/pricing
- Claude API pricing — https://platform.claude.com/docs/en/about-claude/pricing
- Modal pricing — https://modal.com/pricing

> Prices move; re-check the four links above before committing. All figures here
> are personal-use scale (single user, ~250 AI calls/day).
