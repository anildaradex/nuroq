// Variant D — "Trade Ideas / Channel Bar" — live feed is the primary surface,
// sidebar shows account + agent + actions. Optimized for active sessions
// where you watch the feed during market hours.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Radio, Play, Square, Zap, RefreshCw, AlertCircle } from "lucide-react";
import { api } from "../../lib/api";
import { cn, fmtUSD, fmtPct, shortTime, relTime } from "../../lib/cn";
import { haptic } from "../../lib/native";
import type { VariantProps } from "./shared";

export function TodayD({ acct, cards, feed, orders, portfolio }: VariantProps) {
  const qc = useQueryClient();
  const startAgent = useMutation({ mutationFn: api.agentStart,  onSuccess: () => { haptic.success(); qc.invalidateQueries(); } });
  const stopAgent  = useMutation({ mutationFn: api.agentStop,   onSuccess: () => { haptic.medium();  qc.invalidateQueries(); } });
  const agentRunning = cards?.agent.running ?? false;

  if (!acct?.connected) {
    return (
      <div className="card card-tight border-sell/30 text-sell text-xs flex items-center gap-2">
        <AlertCircle className="w-4 h-4" /> Alpaca not connected
      </div>
    );
  }

  const pl = acct.todays_pl;
  const plColor = pl >= 0 ? "text-buy" : "text-sell";

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      {/* ─── PRIMARY: Live Feed (left 2/3 on lg) ──────────────────── */}
      <div className="lg:col-span-2 order-2 lg:order-1">
        <div className="card overflow-hidden">
          <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-center gap-2 bg-zinc-50 dark:bg-zinc-900/50">
            <Radio className="w-3.5 h-3.5 text-buy" />
            <span className="text-xs font-semibold">Live Feed</span>
            <span className="nuroq-live-dot" />
            <span className="text-xxs opacity-60">last 24h</span>
            <div className="flex-1" />
            <button
              onClick={() => { haptic.tap(); qc.invalidateQueries({ queryKey: ["feed"] }); }}
              className="p-1 rounded opacity-60 hover:opacity-100"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          </div>

          {!feed || feed.length === 0 ? (
            <div className="px-3 py-12 text-xs opacity-50 text-center">
              No events yet. The feed populates during market hours as the
              live agent fires crossings and the news poller ingests headlines.
            </div>
          ) : (
            <div className="max-h-[70vh] overflow-y-auto">
              {feed.map((e, i) => (
                <FeedRowRich key={i} e={e} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ─── SIDEBAR (right 1/3 on lg, top on mobile) ─────────────── */}
      <aside className="space-y-3 order-1 lg:order-2">
        {/* P&L hero — quieter than Variant A */}
        <div className="card card-tight">
          <div className="text-xxs uppercase opacity-50 mb-1">Today</div>
          <div className={cn("text-3xl font-bold font-mono leading-none", plColor)}>
            {pl >= 0 ? "+" : ""}{fmtUSD(pl)}
          </div>
          <div className={cn("text-xs font-medium mt-1 font-mono", plColor)}>
            {fmtPct(acct.todays_pl_pct)}
            <span className="opacity-50 ml-2 text-xxs">{fmtUSD(acct.equity)} equity</span>
          </div>
        </div>

        {/* Agent control */}
        <div className="card card-tight">
          <div className="flex items-center gap-2 mb-2">
            <span className={cn("w-2 h-2 rounded-full", agentRunning ? "bg-buy animate-pulse" : "bg-zinc-400")} />
            <span className="text-xs font-medium">
              Agent · {agentRunning ? "Running" : "Stopped"}
            </span>
          </div>
          {agentRunning && cards && (
            <div className="text-xxs opacity-70 font-mono mb-2 space-y-0.5">
              <div>{cards.agent.subscribed_tickers} tickers subscribed</div>
              <div>BUYs {cards.agent.buys_fired_today}/{cards.agent.buys_cap} · SELLs {cards.agent.sells_fired_today}</div>
              <div>{cards.agent.bars_processed.toLocaleString()} bars · last {cards.agent.latest_bar_ts ? shortTime(cards.agent.latest_bar_ts) : "—"}</div>
            </div>
          )}
          {agentRunning ? (
            <button onClick={() => stopAgent.mutate()} className="btn btn-sell w-full">
              <Square className="w-3.5 h-3.5" /> Stop
            </button>
          ) : (
            <button onClick={() => startAgent.mutate()} className="btn btn-primary w-full">
              <Play className="w-3.5 h-3.5" /> Start
            </button>
          )}
        </div>

        {/* Positions count + Pending */}
        <div className="grid grid-cols-2 gap-2">
          <div className="card card-tight text-center">
            <div className="text-2xl font-bold font-mono">{portfolio?.length ?? 0}</div>
            <div className="text-xxs opacity-60 mt-0.5">positions</div>
            {portfolio && portfolio.length > 0 && (
              <div className="text-xxs font-mono opacity-50 mt-0.5">{fmtUSD(acct.positions_value, 0)}</div>
            )}
          </div>
          <div className="card card-tight text-center">
            <div className="text-2xl font-bold font-mono">{orders?.length ?? 0}</div>
            <div className="text-xxs opacity-60 mt-0.5">pending</div>
            <div className="text-xxs opacity-50 mt-0.5 font-mono">queued</div>
          </div>
        </div>

        {/* Watching summary */}
        {cards && (cards.watchlist.buys + cards.watchlist.holds) > 0 && (
          <div className="card card-tight">
            <div className="text-xxs uppercase opacity-50 mb-1">Watching</div>
            <div className="text-xs">
              <span className="text-buy font-bold font-mono">{cards.watchlist.buys}</span>
              <span className="opacity-60"> BUY · </span>
              <span className="font-bold font-mono">{cards.watchlist.holds}</span>
              <span className="opacity-60"> HOLD</span>
              {cards.watchlist.sells > 0 && (
                <>
                  <span className="opacity-60"> · </span>
                  <span className="text-sell font-bold font-mono">{cards.watchlist.sells}</span>
                  <span className="opacity-60"> SELL</span>
                </>
              )}
            </div>
            {cards.watchlist.generated_at && (
              <div className="text-xxs opacity-40 mt-1 font-mono">
                generated {relTime(cards.watchlist.generated_at)}
              </div>
            )}
          </div>
        )}

        {/* Quick trade FAB hint on mobile */}
        <button
          onClick={() => { haptic.tap(); document.querySelector<HTMLButtonElement>("[aria-label='Quick Trade']")?.click(); }}
          className="btn btn-ghost w-full"
        >
          <Zap className="w-3.5 h-3.5 text-accent" /> Open Quick Trade
        </button>
      </aside>
    </div>
  );
}

function FeedRowRich({ e }: { e: VariantProps["feed"] extends (infer R)[] | undefined ? R : never }) {
  const isNews = e.kind === "news";
  const accent =
    isNews ? (
      e.classification === "POSITIVE_BOOST"   ? "border-l-buy bg-buy/5" :
      e.classification === "NEGATIVE_WARNING" ? "border-l-amber-500 bg-amber-500/5" :
      e.classification === "NEGATIVE_BLOCK"   ? "border-l-sell bg-sell/5" :
      "border-l-zinc-400"
    ) : (
      e.action === "FIRED_BUY"         ? "border-l-buy bg-buy/5" :
      e.action === "FIRED_SELL"        ? "border-l-sell bg-sell/5" :
      e.action?.startsWith("SUPPRESSED") ? "border-l-amber-500 bg-amber-500/5" :
      "border-l-zinc-400"
    );
  const sym =
    isNews ? (e.classification === "POSITIVE_BOOST" ? "📈" : e.classification === "NEGATIVE_WARNING" ? "⚠️" : "🛑")
           : (e.action === "FIRED_BUY" ? "🎯" : e.action === "FIRED_SELL" ? "📉" : e.action?.startsWith("SUPPRESSED") ? "🚫" : "•");

  return (
    <div className={cn(
      "px-3 py-2 border-l-2 border-t border-zinc-100 dark:border-zinc-800/50 first:border-t-0",
      accent,
    )}>
      <div className="flex items-baseline gap-2 text-xs">
        <span className="text-base leading-none">{sym}</span>
        <span className="font-mono font-bold">{e.ticker}</span>
        <span className="opacity-50 text-xxs font-mono">{shortTime(e.ts)}</span>
        <div className="flex-1" />
        {!isNews && e.score_before != null && e.score_after != null && (
          <span className="font-mono text-xxs opacity-70">
            {e.score_before}<span className="opacity-50">→</span>{e.score_after}
          </span>
        )}
        {!isNews && e.price && (
          <span className="font-mono text-xxs">{fmtUSD(e.price)}</span>
        )}
      </div>
      <div className="text-xxs opacity-80 mt-1 leading-snug">
        {isNews
          ? `${e.classification?.replace(/_/g, " ")}: ${e.headline}`
          : `${e.direction} ${e.action?.replace(/_/g, " ").toLowerCase()}`}
      </div>
    </div>
  );
}
