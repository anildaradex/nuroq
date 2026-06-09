// Typed API client matching backend/api.py routes. Vite proxies /api → :8000
// in dev; in prod the FastAPI server serves both frontend and API on the same
// origin, so relative paths just work.
//
// Native (Capacitor) builds set VITE_API_BASE to an absolute URL since the
// WebView has no server inside it.

export interface StatusPills {
  agent: PillState;
  stream: PillState;
  telegram: PillState;
  alpaca: PillState;
  ts: number;
}
export type PillState = "ok" | "warn" | "err" | "off";

export interface AlpacaSummary {
  connected: boolean;
  status: string;
  equity: number;
  cash: number;
  buying_power: number;
  positions_value: number;
  todays_pl: number;
  todays_pl_pct: number;
  thirty_day_return_pct: number | null;
}

export interface BenchmarkSeries {
  closes: number[];
  return_pct: number;
}
export interface EquityHistory {
  equity_series: number[];
  timestamps: number[];
  return_pct: number;
  period_days: number;
  /** Overlay benchmarks (default SPY + VOO). Frontend normalizes to % so the
   *  user's equity line and a ~$700 ETF line are visually comparable. */
  benchmarks: Record<string, BenchmarkSeries>;
}

export interface PendingOrder {
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  order_type: string;
  is_bracket: boolean;
  limit_price: number | null;
  stop_price: number | null;
  status: string;
  submitted_at: string | null;
}

export interface TodayCards {
  watchlist: { buys: number; holds: number; sells: number; generated_at: number | null };
  agent: {
    running: boolean;
    subscribed_tickers: number;
    bars_processed: number;
    buys_fired_today: number;
    buys_cap: number;
    sells_fired_today: number;
    started_at: string | null;
    latest_bar_ts: number | null;
  };
  news_24h: Record<string, number>;
}

export interface NextAction {
  level: "ok" | "warn" | "err";
  text: string;
}

export interface FeedEvent {
  ts: number;
  kind: "trigger" | "news";
  ticker: string;
  classification?: string;
  action?: string;
  direction?: string;
  score_before?: number | null;
  score_after?: number | null;
  price?: number | null;
  headline?: string;
}

export interface WatchlistRow {
  ticker: string;
  rank: number;
  ai_score: number | null;
  quant_score: number | null;
  recommendation: string;
  price: number;
  change_pct: number;
  technicals_summary: string;
  fundamentals_summary: string;
  generated_at: number;
}

export interface PortfolioRow {
  ticker: string;
  shares: number;
  avg_price: number;
  current_price: number;
  total_value: number;
  pnl_pct: number;
  stop_loss: number | null;
  take_profit: number | null;
  ai_score: number | null;
  ai_rating: string | null;
  entry_date: string | null;
}

export interface AnalyzeResult {
  ticker: string;
  company_name: string | null;
  industry: string | null;
  price: number;
  change_pct: number;
  final_score: number;
  rating: string;
  technicals: {
    rsi: number | null; percent_b: number | null; atr: number | null;
    trend: string | null; rel_vol: number | null; sma_20: number | null;
    gain_20d: number | null; semantic_rsi: string | null;
    semantic_bb: string | null; volatility: number | null;
  };
  fundamentals: {
    pe: number | string | null; growth: number | string | null;
    name: string | null; industry: string | null;
  };
  ai_score: number | null;
  ai_reasoning: string | null;
  ai_bull_case: string | null;
  ai_bear_case: string | null;
  ai_key_risk: string | null;
  ai_considerations: string[];
  trade_setup: {
    shares: number; sl: number; tp: number; atr: number;
    position_value: number; earnings_days: number; earnings_risk: boolean;
  };
  chart: {
    bars: Array<{ t: string; o: number; h: number; l: number; c: number; v: number }>;
    sma20: Array<number | null>;
    upper_bb: Array<number | null>;
    lower_bb: Array<number | null>;
  };
}

/** One side of the backend A/B comparison (the cloud Gemini peer). */
export interface PeerSide {
  backend: string;
  ok: boolean;
  final_score: number | null;
  rating: string | null;
  ai_score: number | null;
  ai_reasoning: string | null;
  ai_key_risk: string | null;
  price: number | null;
  elapsed_s: number | null;
  error: string | null;
}
export interface PeerCompare {
  ticker: string;
  local_backend: string;
  peer: PeerSide | null;
  note: string | null;
}

export interface SignalRow {
  timestamp: string;
  ticker: string;
  name: string | null;
  industry: string | null;
  price: number;
  technicals: string | null;
  fundamentals: string | null;
  signal: string;
  ai_score: number | null;
  quant_score: number | null;
}

export interface AgentLogRow {
  ts: number;
  ticker: string;
  direction: string;
  score_before: number | null;
  score_after: number | null;
  price: number | null;
  action: string;
  notes: string | null;
}

export interface HealthComponent {
  state: PillState;
  name: string;
  status: string;
}

export interface TradeReq {
  ticker: string;
  shares: number;
  action: "buy" | "sell";
  order_type?: string;
  tif?: string;
  sl?: number;
  tp?: number;
  limit_price?: number;
  stop_price?: number;
  /** Client-supplied UUID for double-submit prevention (TTL 10s on backend). */
  idempotency_key?: string;
  /** Set true after explicit user acknowledgment of wash-sale risk. */
  wash_sale_override?: boolean;
}

export interface AskResult {
  ticker: string;
  question: string;
  answer: string;
  sources: string[];
  grounded: boolean;
}

export interface WashSaleRisk {
  ticker: string;
  risk: boolean;
  recent_sells: Array<{ ts: number; qty: number; sell_price: number; days_ago: number }>;
  likely_loss_sells: Array<{
    ts: number; qty: number; sell_price: number; basis_price: number;
    days_ago: number; approx_loss_per_share: number; approx_total_loss: number;
  }>;
  hint: string;
  days_until_safe: number;
}

export interface TradeSetup {
  ticker: string;
  price: number;
  shares: number;
  sl: number;
  tp: number;
  atr: number;
}

const API_BASE = (import.meta as ImportMeta & { env: { VITE_API_BASE?: string } })
  .env.VITE_API_BASE ?? "";

// Auth is now password-login via /api/auth/login. The server sets an httponly
// `nuroq_session` cookie which the browser sends automatically on every same-
// origin /api/* call. `credentials: "include"` lets the cookie travel cross-
// origin too (Capacitor WebView calling the cloud) when the server CORS allows.
//
// 401 = "log in again" — the App-level AuthGate watches /api/auth/status and
// renders the LoginScreen, so a 401 here just bubbles up as a normal query
// error and the user is moved to the login form on the next status check.

/** A 401 from any endpoint — let callers distinguish "not authed" from network errors. */
export class UnauthorizedError extends Error {
  path: string;
  constructor(path: string) {
    super(`401: ${path}`);
    this.path = path;
  }
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(API_BASE + url, { credentials: "include" });
  if (r.status === 401) throw new UnauthorizedError(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${url}`);
  return r.json();
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(API_BASE + url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) throw new UnauthorizedError(url);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${r.status} ${r.statusText}: ${url} ${text}`);
  }
  return r.json();
}

export interface AgentConfig {
  budget: number;
  max_concurrent: number;
  risk_per_trade_pct: number;
  daily_loss_limit_pct: number;
  entry_window_start: string;    // "HH:MM" ET
  entry_window_end: string;
  eod_flatten_time: string;
  margin_allowed: boolean;
  auto_trade_enabled: boolean;
  notify_on_trade: boolean;
  halted_at: number | null;
  halt_reason: string | null;
  pending_open_flatten: number | null;
  updated_at: number;
}

export type AgentConfigUpdate = Partial<Omit<AgentConfig, "halted_at" | "halt_reason" | "updated_at">>;

export interface FlattenResp {
  closed_count: number;
  cancelled_orders: number;
  queued_for_open: number;
  errors: string[];
}

export interface AutoTradeStatus {
  enabled: boolean;
  halted: boolean;
  halt_reason: string | null;
  today_buys: number;
  today_sells: number;
  open_positions: number;
  todays_pl: number;
  todays_pl_pct: number;
  equity: number;
  cash: number;
  on_margin: boolean;
}

export interface PortfolioContribution {
  ticker: string;
  intraday_pl: number;
  change_pct: number;
  market_value: number;
}

/** AI-generated "why am I up/down today" insight. */
export interface Insight {
  summary: string;
  pnl_dollars: number;
  pnl_pct: number;
  equity: number;
  top_contributors: PortfolioContribution[];
  top_detractors: PortfolioContribution[];
  sources: string[];
  grounded: boolean;
  generated_at: number;
}

/** Free-form Q&A answer about the user's portfolio (not a single ticker). */
export interface PortfolioAsk {
  question: string;
  answer: string;
  sources: string[];
  grounded: boolean;
}

export interface ResearchStatus {
  active: boolean;
  progress: number;
  total: number;
  percent: number;
  elapsed_sec: number;
  eta_sec: number | null;
  started_at: number | null;
  last_completed_at: number | null;   // unix ts of most recent finished cycle
  last_count: number;                  // # candidates in current watchlist
}

export interface AuthStatus {
  authenticated: boolean;
  must_change_password: boolean;
  /** "gemma" on the local Mac (MLX), "gemini" on the cloud (Vertex). The A/B
   *  compare button only shows when this is "gemma" — the cloud can't reach
   *  back to your Mac, and "compare cloud against itself" is meaningless. */
  ai_backend: string;
}

export const api = {
  // Today + status
  statusPills:    () => get<StatusPills>("/api/status/pills"),
  alpacaSummary:  () => get<AlpacaSummary>("/api/alpaca/summary"),
  alpacaHistory:  (days = 30) => get<EquityHistory>(`/api/alpaca/history?days=${days}`),
  pendingOrders:  () => get<PendingOrder[]>("/api/alpaca/orders"),
  todayCards:     () => get<TodayCards>("/api/today/cards"),
  nextActions:    () => get<NextAction[]>("/api/today/next-actions"),
  feed:           () => get<FeedEvent[]>("/api/today/feed"),
  // Data tables
  watchlist:      () => get<WatchlistRow[]>("/api/watchlist"),
  portfolio:      () => get<PortfolioRow[]>("/api/portfolio"),
  signals:        () => get<SignalRow[]>("/api/signals"),
  agentLog:       (limit = 100) => get<AgentLogRow[]>(`/api/agent/log?limit=${limit}`),
  logs:           (lines = 200) => get<{ lines: string[]; path: string }>(`/api/logs?lines=${lines}`),
  systemHealth:   () => get<HealthComponent[]>("/api/system/health"),
  // Deep analysis
  analyze:        (ticker: string) => get<AnalyzeResult>(`/api/analyze/${ticker.toUpperCase()}`),
  analyzePeer:    (ticker: string) => get<PeerCompare>(`/api/analyze/peer/${ticker.toUpperCase()}`),
  // Auth
  authStatus:     () => get<AuthStatus>("/api/auth/status"),
  login:          (password: string) =>
                    post<{ ok: boolean; detail?: string }>("/api/auth/login", { password }),
  logout:         () => post<{ ok: boolean }>("/api/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
                    post<{ ok: boolean; detail?: string }>(
                      "/api/auth/change-password",
                      { current_password, new_password }),
  tradeSetup:     (ticker: string) => get<TradeSetup>(`/api/trade-setup/${ticker.toUpperCase()}`),
  washSale:       (ticker: string) => get<WashSaleRisk>(`/api/wash-sale/${ticker.toUpperCase()}`),
  ask:            (ticker: string, question: string) =>
                    post<AskResult>("/api/ask", { ticker: ticker.toUpperCase(), question }),
  // Mutations
  trade:          (req: TradeReq) => post<{ ok: boolean; message: string }>("/api/trade", req),
  removePosition: (ticker: string) => post<{ ok: boolean; message: string }>("/api/portfolio/remove", { ticker }),
  agentStart:     () => post<{ ok: boolean; message: string }>("/api/agent/start"),
  agentStop:      () => post<{ ok: boolean; message: string }>("/api/agent/stop"),
  researchCycle:  () => post<{ ok: boolean; message: string }>("/api/research-cycle"),
  researchStatus: () => get<ResearchStatus>("/api/research-cycle/status"),
  insightToday:   (force = false) => get<Insight>(`/api/insight/today${force ? "?force=true" : ""}`),
  askPortfolio:   (question: string) =>
                    post<PortfolioAsk>("/api/ask-portfolio", { question }),
  // Auto-trade configuration + control
  getConfig:        () => get<AgentConfig>("/api/config"),
  updateConfig:     (patch: AgentConfigUpdate) => post<AgentConfig>("/api/config", patch),
  autoTradeStatus:  () => get<AutoTradeStatus>("/api/auto-trade/status"),
  autoTradeHalt:    (reason = "manual") =>
                      post<AgentConfig>("/api/auto-trade/halt", { reason }),
  autoTradeResume: () => post<AgentConfig>("/api/auto-trade/resume"),
  flattenAll:      () => post<FlattenResp>("/api/flatten-all"),
  // Scan is async (long-running): start it, then poll scanStatus until !running.
  scan: (mode: "top20" | "global") =>
    post<{ started: boolean; running: boolean; message: string }>("/api/scan", { mode }),
  scanStatus: () =>
    get<{ running: boolean; rows: unknown[]; summary: string; error: string | null; mode: string | null }>(
      "/api/scan/status"),
};
