# Architecture Decision Records (ADR)

## ADR 001: Local Inference Strategy
- **Decision:** Use `mlx-lm` with 4-bit quantized 7B models.
- **Rationale:** The M3 Pro (36GB RAM) can fit two 7B models (approx 4.5GB each) simultaneously. This allows for local "Dual-Model Consensus" without hitting cloud costs or privacy issues.
- **Status:** Accepted.

## ADR 002: API Rate Limiting (Polygon Free Tier)
- **Decision:** Implement a global `PolygonRateLimiter` with a 20-second sleep between analysis calls.
- **Rationale:** Polygon Free Tier allows only 5 requests per minute. Background agents must be throttled to prevent API key blacklisting.
- **Status:** Accepted.

## ADR 003: Human-in-the-Loop (HITL) via Telegram
- **Decision:** All "BUY" signals require an explicit "EXECUTE" callback from Telegram.
- **Rationale:** Provides a safety gate for an autonomous trading system while allowing for remote operation from a phone.
- **Status:** Accepted.

## ADR 004: Gradio Theme Consistency
- **Decision:** Force "Light Mode" via custom JavaScript.
- **Rationale:** Prevents "Theming Chaos" where agents add conflicting CSS styles; ensures a premium, clean aesthetic for the dashboard.
- **Status:** Accepted.
