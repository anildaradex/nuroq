import { useState, useEffect } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ComposedChart,
} from "recharts";
import {
  Search, TrendingUp, Brain, Zap, AlertCircle, Loader2, Sparkles, SendHorizonal,
} from "lucide-react";
import { api, type AnalyzeResult } from "../lib/api";
import { cn, fmtUSD, fmtPct } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  initialTicker?: string;
  onSendToQuickTrade?: (ticker: string, shares: number, sl: number, tp: number) => void;
}

export function AnalyzeView({ initialTicker, onSendToQuickTrade }: Props) {
  const [ticker, setTicker] = useState(initialTicker ?? "");
  const [active, setActive] = useState<"signal" | "ai" | "setup">("signal");
  const [recent, setRecent] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("nuroq.recent") ?? "[]");
    } catch {
      return [];
    }
  });

  const analyze = useMutation({
    mutationFn: (t: string) => api.analyze(t),
    onSuccess: (r) => {
      const t = r.ticker.toUpperCase();
      setRecent((rec) => {
        const next = [t, ...rec.filter((x) => x !== t)].slice(0, 8);
        localStorage.setItem("nuroq.recent", JSON.stringify(next));
        return next;
      });
      haptic.success();
    },
    onError: () => haptic.error(),
  });

  // Auto-run when a ticker is pushed in via drill-down from another view.
  useEffect(() => {
    if (initialTicker) {
      setTicker(initialTicker);
      analyze.mutate(initialTicker);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTicker]);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = ticker.trim().toUpperCase();
    if (!t) return;
    haptic.tap();
    analyze.mutate(t);
  };

  const r = analyze.data;

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <form onSubmit={onSubmit} className="card card-tight flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 opacity-50" />
          <input
            autoFocus
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="Enter ticker (NVDA, AAPL, …)"
            className="input pl-7 font-mono w-full"
          />
        </div>
        <button type="submit" className="btn btn-primary" disabled={!ticker || analyze.isPending}>
          {analyze.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "Run Analysis"}
        </button>
        {recent.length > 0 && (
          <div className="flex items-center gap-1 ml-auto flex-wrap">
            <span className="text-xxs opacity-50 mr-1">Recent:</span>
            {recent.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => { setTicker(t); analyze.mutate(t); }}
                className="px-1.5 py-0.5 rounded text-xxs font-mono border border-zinc-300
                           dark:border-zinc-700 hover:bg-accent/10 hover:border-accent"
              >
                {t}
              </button>
            ))}
          </div>
        )}
      </form>

      {analyze.isError && (
        <div className="card card-tight border-sell/30 flex items-center gap-2 text-sell">
          <AlertCircle className="w-4 h-4" />
          <span className="text-xs">{(analyze.error as Error)?.message ?? "Analysis failed"}</span>
        </div>
      )}

      {!r && !analyze.isPending && !analyze.isError && (
        <div className="card card-tight nuroq-empty-state">
          <h3 className="text-sm font-semibold">No analysis yet</h3>
          <p className="text-xs mt-1">
            Enter a ticker above or click any row in Watchlist / Portfolio / Scanner /
            Signal History to drill in here.
          </p>
        </div>
      )}

      {r && (
        <>
          <HeaderCard r={r} />

          {/* Chart + Tabs row: chart left 60%, tabbed context right 40% on lg */}
          <div className="grid grid-cols-1 lg:grid-cols-5 gap-3">
            <div className="lg:col-span-3">
              <PriceChart r={r} />
            </div>
            <div className="lg:col-span-2 card card-tight">
              <div className="flex gap-1 mb-2 border-b border-zinc-200 dark:border-zinc-800">
                <TabBtn label="Signal" active={active === "signal"} onClick={() => setActive("signal")} icon={TrendingUp} />
                <TabBtn label="AI Reasoning" active={active === "ai"} onClick={() => setActive("ai")} icon={Brain} />
                <TabBtn label="Trade Setup" active={active === "setup"} onClick={() => setActive("setup")} icon={Zap} />
              </div>
              {active === "signal" && <SignalTab r={r} />}
              {active === "ai" && <AITab r={r} />}
              {active === "setup" && <TradeSetupTab r={r} onSend={onSendToQuickTrade} />}
            </div>
          </div>

          {/* AI mode — free-form Q&A grounded in this ticker's data */}
          <AskBar ticker={r.ticker} />
        </>
      )}
    </div>
  );
}

function HeaderCard({ r }: { r: AnalyzeResult }) {
  const cColor = r.change_pct >= 0 ? "text-buy" : "text-sell";
  const ratingColor =
    r.rating === "BUY" ? "bg-buy text-white" :
    r.rating === "SELL" ? "bg-sell text-white" :
    "bg-zinc-200 dark:bg-zinc-800";
  return (
    <div className="card card-tight">
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="text-2xl font-bold font-mono">{r.ticker}</span>
        {r.company_name && r.company_name !== r.ticker && (
          <span className="text-sm opacity-60 truncate">{r.company_name}</span>
        )}
        {r.industry && <span className="text-xxs opacity-50 font-mono">{r.industry}</span>}
        <div className="flex-1" />
        <span className={cn("px-2 py-0.5 rounded text-xs font-bold", ratingColor)}>
          {r.rating} {r.final_score}/100
        </span>
      </div>
      <div className="mt-1 flex items-baseline gap-3 flex-wrap text-sm font-mono">
        <span className="text-xl font-bold">{fmtUSD(r.price)}</span>
        <span className={cn("font-medium", cColor)}>{fmtPct(r.change_pct)}</span>
        {r.technicals.trend && <span className="text-xxs opacity-60">trend: {r.technicals.trend}</span>}
      </div>
    </div>
  );
}

function PriceChart({ r }: { r: AnalyzeResult }) {
  const data = r.chart.bars.map((b, i) => ({
    t: b.t.slice(5),
    close: b.c,
    sma20: r.chart.sma20[i],
    upper: r.chart.upper_bb[i],
    lower: r.chart.lower_bb[i],
  }));
  return (
    <div className="card card-tight h-[320px] lg:h-[420px]">
      <div className="text-xxs opacity-60 mb-1">
        Last {data.length} bars · SMA20 + Bollinger Bands
      </div>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 16, left: 0 }}>
          <defs>
            <linearGradient id="bb-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#14b8a6" stopOpacity={0.15} />
              <stop offset="100%" stopColor="#14b8a6" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <XAxis dataKey="t" tick={{ fontSize: 10, fill: "currentColor", opacity: 0.6 }}
                 stroke="currentColor" strokeOpacity={0.2} />
          <YAxis tick={{ fontSize: 10, fill: "currentColor", opacity: 0.6 }}
                 stroke="currentColor" strokeOpacity={0.2} width={50} domain={["auto", "auto"]} />
          <Tooltip
            contentStyle={{ background: "#0a0a0a", border: "1px solid #333", borderRadius: 4, fontSize: 11 }}
            labelStyle={{ color: "#9ca3af" }}
            formatter={(v) => (typeof v === "number" ? v.toFixed(2) : v)}
          />
          <Area type="monotone" dataKey="upper" stroke="#14b8a6" strokeOpacity={0.3} fill="url(#bb-fill)" dot={false} />
          <Area type="monotone" dataKey="lower" stroke="#14b8a6" strokeOpacity={0.3} fill="transparent" dot={false} />
          <Line type="monotone" dataKey="sma20" stroke="#a855f7" strokeWidth={1.5} dot={false} />
          <Line type="monotone" dataKey="close" stroke="#10b981" strokeWidth={2} dot={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function TabBtn({
  label, active, onClick, icon: Icon,
}: { label: string; active: boolean; onClick: () => void; icon: typeof TrendingUp }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 px-2 py-1.5 text-xs border-b-2 transition-colors",
        active ? "border-accent text-accent" : "border-transparent opacity-70 hover:opacity-100"
      )}
    >
      <Icon className="w-3.5 h-3.5" />
      {label}
    </button>
  );
}

function SignalTab({ r }: { r: AnalyzeResult }) {
  const t = r.technicals;
  return (
    <div className="space-y-2 text-xs">
      <Row k="Final score" v={`${r.final_score}/100`} bold />
      <Row k="Rating" v={r.rating} />
      <Row k="Trend" v={t.trend ?? "—"} />
      <Row k="RSI" v={t.rsi != null ? `${t.rsi.toFixed(1)} (${t.semantic_rsi})` : "—"} />
      <Row k="%B" v={t.percent_b != null ? `${t.percent_b.toFixed(2)} (${t.semantic_bb})` : "—"} />
      <Row k="ATR" v={t.atr != null ? `$${t.atr.toFixed(2)}` : "—"} />
      <Row k="SMA20" v={t.sma_20 != null ? `$${t.sma_20.toFixed(2)}` : "—"} />
      <Row k="Rel volume" v={t.rel_vol != null ? `${t.rel_vol.toFixed(2)}×` : "—"} />
      <Row k="20-day gain" v={t.gain_20d != null ? fmtPct(t.gain_20d) : "—"} />
      <hr className="border-zinc-200 dark:border-zinc-800 my-2" />
      <Row k="P/E" v={String(r.fundamentals.pe ?? "—")} />
      <Row k="Growth" v={String(r.fundamentals.growth ?? "—")} />
    </div>
  );
}

function AITab({ r }: { r: AnalyzeResult }) {
  if (r.ai_score == null && !r.ai_reasoning) {
    return <div className="text-xs opacity-60">No cached AI reasoning. Run a research cycle.</div>;
  }
  return (
    <div className="space-y-3 text-xs">
      {r.ai_score != null && (
        <div className="flex items-center gap-2">
          <span className="opacity-60">AI score:</span>
          <span className="font-mono font-bold">{r.ai_score}/100</span>
        </div>
      )}
      {r.ai_reasoning && <Section title="Reasoning" body={r.ai_reasoning} />}
      {r.ai_bull_case && <Section title="🐂 Bull case" body={r.ai_bull_case} />}
      {r.ai_bear_case && <Section title="🐻 Bear case" body={r.ai_bear_case} />}
      {r.ai_key_risk && <Section title="⚠️ Key risk" body={r.ai_key_risk} />}
      {r.ai_considerations.length > 0 && (
        <div>
          <div className="font-semibold mb-1">Considerations</div>
          <ul className="list-disc list-inside space-y-0.5 opacity-80">
            {r.ai_considerations.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function TradeSetupTab({
  r, onSend,
}: { r: AnalyzeResult; onSend?: (ticker: string, shares: number, sl: number, tp: number) => void }) {
  const s = r.trade_setup;
  return (
    <div className="space-y-2 text-xs">
      <Row k="Suggested shares" v={s.shares.toLocaleString()} bold />
      <Row k="Position value" v={fmtUSD(s.position_value)} />
      <Row k="Stop loss" v={fmtUSD(s.sl)} valueClass="text-sell" />
      <Row k="Take profit" v={fmtUSD(s.tp)} valueClass="text-buy" />
      <Row k="ATR" v={fmtUSD(s.atr)} />
      <Row k="Days to earnings" v={`${s.earnings_days}${s.earnings_risk ? " ⚠️" : ""}`} />
      {onSend && (
        <button
          onClick={() => { haptic.tap(); onSend(r.ticker, s.shares, s.sl, s.tp); }}
          className="btn btn-primary w-full mt-3"
        >
          <Zap className="w-3.5 h-3.5" />
          Send to Quick Trade
        </button>
      )}
    </div>
  );
}

function Row({ k, v, bold, valueClass }: { k: string; v: string; bold?: boolean; valueClass?: string }) {
  return (
    <div className="flex justify-between">
      <span className="opacity-60">{k}</span>
      <span className={cn("font-mono", bold && "font-bold", valueClass)}>{v}</span>
    </div>
  );
}

function Section({ title, body }: { title: string; body: string }) {
  return (
    <div>
      <div className="font-semibold mb-0.5">{title}</div>
      <div className="opacity-80 leading-relaxed">{body}</div>
    </div>
  );
}

// ─── AskBar — "AI mode" free-form Q&A grounded in this ticker's data ─────────
// Sits under the chart. User types a natural-language question
// ("why did RKLB increase in May?") and gets a Gemma answer grounded in
// price action + news + SEC filings + fundamentals for the loaded ticker.
function AskBar({ ticker }: { ticker: string }) {
  const [q, setQ] = useState("");
  const [history, setHistory] = useState<Array<{ q: string; a: string; sources: string[] }>>([]);

  const ask = useMutation({
    mutationFn: (question: string) => api.ask(ticker, question),
    onSuccess: (res, question) => {
      haptic.success();
      setHistory((h) => [{ q: question, a: res.answer, sources: res.sources }, ...h]);
      setQ("");
    },
    onError: () => haptic.error(),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const question = q.trim();
    if (!question || ask.isPending) return;
    haptic.tap();
    ask.mutate(question);
  };

  const suggestions = [
    `Why did ${ticker} move recently?`,
    `What are the key risks for ${ticker}?`,
    `Summarize the bull and bear case for ${ticker}.`,
  ];

  return (
    <div className="card card-tight">
      <div className="flex items-center gap-2 mb-2">
        <Sparkles className="w-4 h-4 text-accent" />
        <span className="text-sm font-semibold">Ask AI about {ticker}</span>
        <span className="text-xxs opacity-50">· grounded in price, news &amp; filings</span>
      </div>

      <form onSubmit={submit} className="flex items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={`e.g. "Why did ${ticker} increase in May?"`}
          className="input flex-1"
          disabled={ask.isPending}
        />
        <button
          type="submit"
          className="btn btn-primary !px-3"
          disabled={!q.trim() || ask.isPending}
          aria-label="Ask"
        >
          {ask.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <SendHorizonal className="w-4 h-4" />}
        </button>
      </form>

      {history.length === 0 && !ask.isPending && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {suggestions.map((s) => (
            <button
              key={s}
              onClick={() => { setQ(s); haptic.tap(); ask.mutate(s); }}
              className="px-2 py-1 rounded-md text-xxs border border-zinc-300 dark:border-zinc-700
                         hover:bg-accent/10 hover:border-accent transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {ask.isPending && (
        <div className="mt-3 flex items-center gap-2 text-xs opacity-70">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          Thinking… (running AI over {ticker}'s data)
        </div>
      )}

      {ask.isError && (
        <div className="mt-3 flex items-start gap-2 text-xs text-sell">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{(ask.error as Error)?.message ?? "Ask failed"}</span>
        </div>
      )}

      {history.length > 0 && (
        <div className="mt-3 space-y-3">
          {history.map((item, i) => (
            <div key={i} className="border-t border-zinc-200 dark:border-zinc-800 pt-2 first:border-t-0 first:pt-0">
              <div className="text-xs font-medium flex items-start gap-1.5">
                <span className="opacity-50">Q:</span>
                <span>{item.q}</span>
              </div>
              <div className="text-xs mt-1.5 leading-relaxed whitespace-pre-wrap">{item.a}</div>
              {item.sources.length > 0 && (
                <div className="text-xxs opacity-50 mt-1.5">Sources: {item.sources.join(" · ")}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
