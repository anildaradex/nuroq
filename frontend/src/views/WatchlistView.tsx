import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ChevronRight, Target, Zap, Loader2 } from "lucide-react";
import { api, type WatchlistRow } from "../lib/api";
import { cn, fmtUSD, fmtPct, relTime } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  /** Click anywhere on a row (except action buttons) → switch to Analyze with ticker preloaded. */
  onDrillIn: (ticker: string) => void;
  /**
   * Click the per-row ⚡ Trade button → fetches ATR-sized SL/TP and opens
   * the OrderReviewModal with the full sized order ready to review/submit.
   */
  onReviewOrder?: (order: {
    ticker: string; side: "buy" | "sell"; shares: number;
    price: number; sl?: number; tp?: number;
    source: "watchlist";
  }) => void;
}

export function WatchlistView({ onDrillIn, onReviewOrder }: Props) {
  const qc = useQueryClient();
  // isFetching = true on EVERY fetch (initial + refetches). isLoading = true
  // only on the initial fetch. Use isFetching so the spinner spins every time
  // the user taps refresh, not just the very first time the page loads.
  const { data, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ["watchlist"], queryFn: api.watchlist, refetchInterval: 60_000,
  });

  const [showHolds, setShowHolds] = useState(true);

  const buys = (data ?? []).filter((r) => r.recommendation === "BUY");
  const holds = (data ?? []).filter((r) => r.recommendation !== "BUY");
  const meta = data?.[0]?.generated_at;

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-base font-semibold flex items-center gap-2">
          <Target className="w-4 h-4 text-accent" /> Today's Watchlist
        </h2>
        {meta && <span className="text-xxs opacity-50">generated {relTime(meta)}</span>}
        <span className="text-xxs opacity-60">
          • <b className="text-buy">{buys.length}</b> BUY
          • <b>{holds.length}</b> HOLD
        </span>
        <div className="flex-1" />
        <button
          onClick={() => { haptic.tap(); qc.invalidateQueries({ queryKey: ["watchlist"] }); }}
          className="btn btn-ghost flex items-center gap-1.5 !px-3"
          title="Refresh watchlist"
          aria-label="Refresh watchlist"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isFetching && "animate-spin")} />
          <span className="text-xxs">{isFetching ? "Refreshing…" : "Refresh"}</span>
        </button>
      </div>

      {!data && isFetching && <div className="card card-tight h-40 animate-pulse" />}

      {data && data.length === 0 && (
        <div className="card card-tight nuroq-empty-state">
          <h3 className="text-sm font-semibold">No watchlist yet</h3>
          <p className="text-xs mt-1">
            Run a research cycle from the Today tab or via the command palette
            to generate today's ranked candidates.
          </p>
        </div>
      )}

      {buys.length > 0 && (
        <div className="card overflow-hidden">
          <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 text-xs font-semibold text-buy flex items-center gap-2">
            🟢 BUY signals ({buys.length})
            {onReviewOrder && (
              <span className="ml-auto text-xxs font-normal opacity-60">
                Tap ⚡ to review a sized order
              </span>
            )}
          </div>
          <Table rows={buys} onDrillIn={onDrillIn} onReviewOrder={onReviewOrder} />
        </div>
      )}

      {holds.length > 0 && (
        <div className="card overflow-hidden">
          <button
            onClick={() => setShowHolds((s) => !s)}
            className="w-full px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 text-xs font-semibold opacity-80 flex items-center gap-2"
          >
            <ChevronRight className={cn("w-3.5 h-3.5 transition-transform", showHolds && "rotate-90")} />
            👀 HOLDs ({holds.length}) — Live Agent watches these for HOLD→BUY crossings
          </button>
          {showHolds && <Table rows={holds} onDrillIn={onDrillIn} onReviewOrder={onReviewOrder} />}
        </div>
      )}

      <div className="text-xxs opacity-40 text-right">
        {isFetching
          ? "Refreshing…"
          : `Last refreshed ${dataUpdatedAt ? relTime(dataUpdatedAt / 1000) : "—"}`}
      </div>
    </div>
  );
}

function Table({
  rows, onDrillIn, onReviewOrder,
}: {
  rows: WatchlistRow[];
  onDrillIn: (t: string) => void;
  onReviewOrder?: (order: {
    ticker: string; side: "buy" | "sell"; shares: number;
    price: number; sl?: number; tp?: number; source: "watchlist";
  }) => void;
}) {
  // Track which row's "Trade" button is currently fetching its sized setup
  // so we can show a spinner. Cleared when the modal opens or the fetch fails.
  const [loadingTicker, setLoadingTicker] = useState<string | null>(null);

  async function onTradeClick(e: React.MouseEvent, ticker: string) {
    e.stopPropagation();  // don't drill into Analyze
    if (!onReviewOrder) return;
    haptic.tap();
    setLoadingTicker(ticker);
    try {
      const setup = await api.tradeSetup(ticker);
      onReviewOrder({
        ticker: setup.ticker,
        side: "buy",          // Watchlist BUY signals → open BUY review
        shares: setup.shares,
        price: setup.price,
        sl: setup.sl,
        tp: setup.tp,
        source: "watchlist",
      });
    } catch (err) {
      console.error("trade-setup failed:", err);
      haptic.error();
    } finally {
      setLoadingTicker(null);
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full table-zebra">
        {/*
          Column order: Trade button is now SECOND from the left (right after
          ticker) so it's always visible on a phone-narrow viewport without
          needing to swipe-scroll. Less-essential columns (rank #, AI score,
          technicals_summary) hide on mobile via `hidden sm:table-cell`.
          Mobile shows: TICK | ⚡ | Rating | Quant | Price | Δ%
          Desktop adds: # (left of Tick), AI (after Quant), Technicals (right)
        */}
        <thead>
          <tr className="text-left">
            <th className="hidden sm:table-cell">#</th>
            <th>Ticker</th>
            {onReviewOrder && <th className="text-center">Trade</th>}
            <th>Rating</th>
            <th className="text-right">Quant</th>
            <th className="text-right hidden sm:table-cell">AI</th>
            <th className="text-right">Price</th>
            <th className="text-right">Δ%</th>
            <th className="hidden md:table-cell">Technicals</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isRowLoading = loadingTicker === r.ticker;
            return (
              <tr key={r.ticker} onClick={() => { haptic.tap(); onDrillIn(r.ticker); }}>
                <td className="opacity-60 hidden sm:table-cell">{r.rank}</td>
                <td className="font-mono font-bold">{r.ticker}</td>
                {onReviewOrder && (
                  <td className="text-center" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={(e) => onTradeClick(e, r.ticker)}
                      disabled={isRowLoading}
                      className={cn(
                        "inline-flex items-center justify-center",
                        "w-9 h-9 rounded-md transition-colors",  // 36px square — easy tap target
                        "bg-accent/10 text-accent hover:bg-accent/25",
                        "active:scale-95",
                        "disabled:opacity-40 disabled:cursor-wait",
                      )}
                      title={`Open BUY order review for ${r.ticker}`}
                      aria-label={`Trade ${r.ticker}`}
                    >
                      {isRowLoading
                        ? <Loader2 className="w-4 h-4 animate-spin" />
                        : <Zap className="w-4 h-4" />}
                    </button>
                  </td>
                )}
                <td>
                  <span className={cn(
                    "px-1.5 py-0.5 rounded text-xxs font-bold",
                    r.recommendation === "BUY"  && "bg-buy/15 text-buy",
                    r.recommendation === "HOLD" && "bg-zinc-200 dark:bg-zinc-800",
                    r.recommendation === "SELL" && "bg-sell/15 text-sell",
                  )}>{r.recommendation}</span>
                </td>
                <td className="text-right font-mono">{r.quant_score ?? "—"}</td>
                <td className="text-right font-mono opacity-70 hidden sm:table-cell">{r.ai_score ?? "—"}</td>
                <td className="text-right font-mono">{fmtUSD(r.price)}</td>
                <td className={cn("text-right font-mono", r.change_pct >= 0 ? "text-buy" : "text-sell")}>
                  {fmtPct(r.change_pct)}
                </td>
                <td className="hidden md:table-cell opacity-70 text-xxs truncate max-w-xs">
                  {r.technicals_summary}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
