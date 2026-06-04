import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronUp, ChevronDown, Zap, X, Loader2 } from "lucide-react";
import { haptic } from "../lib/native";
import { api } from "../lib/api";
import { cn } from "../lib/cn";

// Smallest possible UUID v4 (no crypto dep needed; falls back if unavailable).
function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

// Minimum time between two BUY/SELL clicks. Anything faster is treated as a
// double-tap and dropped. Combined with backend idempotency (10s TTL on the
// same UUID), this provides triple-layer protection:
//   1. React debounce  → ignore rapid retaps
//   2. TanStack isPending → disable button while in flight
//   3. Backend idempotency → if both somehow reach the server, dedup by UUID
const CLICK_DEBOUNCE_MS = 1500;

export interface QuickTradePrefill {
  ticker: string;
  shares: number;
  sl: number;
  tp: number;
  _ts: number;  // bump to force re-prefill on repeat clicks
}

interface Props {
  prefill?: QuickTradePrefill;
}

// Desktop: always-pinned bottom strip (≥ lg).
// Mobile  (< lg): collapsed to a FAB; tapping opens a full-screen sheet.
export function QuickTrade({ prefill }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const [ticker, setTicker] = useState("");
  const [shares, setShares] = useState<number | "">(1);
  const [orderType, setOrderType] = useState("Market");
  const [tif, setTif] = useState("GTC");
  const [sl, setSl] = useState<number | "">("");
  const [tp, setTp] = useState<number | "">("");
  const [limit, setLimit] = useState<number | "">("");
  const [stop, setStop] = useState<number | "">("");

  const qc = useQueryClient();
  const trade = useMutation({
    mutationFn: api.trade,
    onSuccess: () => { haptic.success(); qc.invalidateQueries(); },
    onError:   () => haptic.error(),
  });

  // Debounce ref — survives re-renders without triggering them.
  const lastClickAtRef = useRef<number>(0);
  // Track the in-flight idempotency key so retries (e.g. React StrictMode
  // double-render in dev, network retry) collapse to one backend submission.
  const inFlightKeyRef = useRef<string | null>(null);

  // Receive prefill from AnalyzeView's Send-to-Quick-Trade button.
  useEffect(() => {
    if (!prefill) return;
    setTicker(prefill.ticker);
    setShares(prefill.shares);
    setSl(prefill.sl);
    setTp(prefill.tp);
    setOrderType("Market");
    setExpanded(true);
    setMobileOpen(true);  // surface on mobile
  }, [prefill]);

  const placeOrder = (side: "buy" | "sell") => {
    if (!ticker || !shares || Number(shares) <= 0) return;

    // Layer 1 — client debounce. Reject rapid retaps.
    const now = Date.now();
    if (now - lastClickAtRef.current < CLICK_DEBOUNCE_MS) {
      haptic.warning();
      return;
    }
    lastClickAtRef.current = now;

    // Layer 2 — idempotency UUID. Backend dedups within 10s on this key.
    const key = inFlightKeyRef.current ?? uuid();
    inFlightKeyRef.current = key;

    haptic.medium();
    trade.mutate(
      {
        ticker: ticker.trim().toUpperCase(),
        shares: Number(shares),
        action: side,
        order_type: orderType,
        tif,
        sl: sl === "" ? undefined : Number(sl),
        tp: tp === "" ? undefined : Number(tp),
        limit_price: limit === "" ? undefined : Number(limit),
        stop_price:  stop  === "" ? undefined : Number(stop),
        idempotency_key: key,
      },
      {
        onSettled: () => {
          // Release the in-flight key once the request resolves so the next
          // CLICK (after debounce) gets a fresh UUID. If a "real" second
          // order is intended, the user waits >1.5s and gets a new key.
          inFlightKeyRef.current = null;
        },
      }
    );
  };

  const statusLine = trade.data?.message ??
    (trade.isError ? `❌ ${(trade.error as Error).message}` :
     trade.isPending ? "Submitting…" :
     "Fill SL + TP on a Market order for a bracket; blank = naked single order.");
  const statusClass = trade.data?.ok ? "text-buy" :
                      trade.isError ? "text-sell" :
                      trade.isPending ? "text-accent" :
                      "opacity-60";

  const fields = (
    <>
      <input
        value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())}
        placeholder="TICKER" className="input w-24 font-mono"
      />
      <input
        type="number" value={shares}
        onChange={(e) => setShares(e.target.value === "" ? "" : Number(e.target.value))}
        placeholder="Shares" className="input w-20"
      />
      <select value={orderType} onChange={(e) => setOrderType(e.target.value)} className="input w-28">
        {["Market", "Limit", "Stop", "Stop Limit", "Trailing Stop"].map((t) => (
          <option key={t}>{t}</option>
        ))}
      </select>
      <select value={tif} onChange={(e) => setTif(e.target.value)} className="input w-20">
        {["Day", "GTC", "OPG", "IOC", "FOK"].map((t) => <option key={t}>{t}</option>)}
      </select>

      {expanded && (
        <>
          <input
            type="number" value={sl}
            onChange={(e) => setSl(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="SL ($)" className="input w-24"
          />
          <input
            type="number" value={tp}
            onChange={(e) => setTp(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="TP ($)" className="input w-24"
          />
          {(orderType === "Limit" || orderType === "Stop Limit") && (
            <input
              type="number" value={limit}
              onChange={(e) => setLimit(e.target.value === "" ? "" : Number(e.target.value))}
              placeholder="Limit ($)" className="input w-24"
            />
          )}
          {(orderType === "Stop" || orderType === "Stop Limit" || orderType === "Trailing Stop") && (
            <input
              type="number" value={stop}
              onChange={(e) => setStop(e.target.value === "" ? "" : Number(e.target.value))}
              placeholder="Stop ($)" className="input w-24"
            />
          )}
        </>
      )}
    </>
  );

  return (
    <>
      {/* Desktop pinned strip */}
      <footer
        className="hidden lg:block sticky bottom-0 z-20 border-t border-zinc-200 dark:border-zinc-800
                   bg-white/95 dark:bg-zinc-950/95 backdrop-blur px-4 py-2"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1.5 text-xs font-medium opacity-70 mr-1">
            <Zap className="w-3.5 h-3.5 text-accent" />
            Quick Trade
          </div>
          {fields}
          <button
            onClick={() => setExpanded((e) => !e)}
            className="btn btn-ghost !px-2"
            title={expanded ? "Hide SL/TP" : "Show SL/TP and stops"}
          >
            {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronUp className="w-3.5 h-3.5" />}
          </button>
          <div className="flex-1" />
          <button onClick={() => placeOrder("buy")}  disabled={trade.isPending} className="btn btn-buy">
            {trade.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "BUY"}
          </button>
          <button onClick={() => placeOrder("sell")} disabled={trade.isPending} className="btn btn-sell">
            {trade.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "SELL"}
          </button>
        </div>
        <div className={cn("text-xxs font-mono mt-1", statusClass)}>{statusLine}</div>
      </footer>

      {/* Mobile FAB */}
      <button
        onClick={() => { setExpanded(true); setMobileOpen(true); haptic.tap(); }}
        className="lg:hidden fixed right-4 z-30 w-14 h-14 rounded-full
                   bg-accent text-white shadow-lg flex items-center justify-center
                   active:scale-95 transition-transform"
        style={{ bottom: "calc(env(safe-area-inset-bottom) + 4rem)" }}
        aria-label="Quick Trade"
      >
        <Zap className="w-6 h-6" />
      </button>

      {/* Mobile sheet */}
      {mobileOpen && (
        <div
          className="lg:hidden fixed inset-0 z-40 bg-black/50 backdrop-blur-sm flex items-end"
          onClick={() => setMobileOpen(false)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full bg-white dark:bg-zinc-950 rounded-t-xl
                       border-t border-zinc-200 dark:border-zinc-800 pb-safe"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-200 dark:border-zinc-800">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Zap className="w-4 h-4 text-accent" /> Quick Trade
              </div>
              <button
                onClick={() => setMobileOpen(false)}
                className="p-2 -mr-2 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-900"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 flex flex-col gap-3">
              <div className="grid grid-cols-2 gap-2">
                <label className="text-xxs opacity-60 col-span-2">Ticker
                  <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())}
                         placeholder="AAPL" className="input mt-1 font-mono" />
                </label>
                <label className="text-xxs opacity-60">Shares
                  <input type="number" value={shares}
                         onChange={(e) => setShares(e.target.value === "" ? "" : Number(e.target.value))}
                         className="input mt-1" />
                </label>
                <label className="text-xxs opacity-60">Type
                  <select value={orderType} onChange={(e) => setOrderType(e.target.value)} className="input mt-1">
                    {["Market", "Limit", "Stop", "Stop Limit", "Trailing Stop"].map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                </label>
                <label className="text-xxs opacity-60">TIF
                  <select value={tif} onChange={(e) => setTif(e.target.value)} className="input mt-1">
                    {["Day", "GTC", "OPG", "IOC", "FOK"].map((t) => <option key={t}>{t}</option>)}
                  </select>
                </label>
                <label className="text-xxs opacity-60">Stop Loss ($)
                  <input type="number" value={sl}
                         onChange={(e) => setSl(e.target.value === "" ? "" : Number(e.target.value))}
                         className="input mt-1" />
                </label>
                <label className="text-xxs opacity-60">Take Profit ($)
                  <input type="number" value={tp}
                         onChange={(e) => setTp(e.target.value === "" ? "" : Number(e.target.value))}
                         className="input mt-1" />
                </label>
                {(orderType === "Limit" || orderType === "Stop Limit") && (
                  <label className="text-xxs opacity-60">Limit ($)
                    <input type="number" value={limit}
                           onChange={(e) => setLimit(e.target.value === "" ? "" : Number(e.target.value))}
                           className="input mt-1" />
                  </label>
                )}
                {(orderType === "Stop" || orderType === "Stop Limit" || orderType === "Trailing Stop") && (
                  <label className="text-xxs opacity-60">Stop ($)
                    <input type="number" value={stop}
                           onChange={(e) => setStop(e.target.value === "" ? "" : Number(e.target.value))}
                           className="input mt-1" />
                  </label>
                )}
              </div>

              <div className={cn("text-xxs font-mono", statusClass)}>{statusLine}</div>

              <div className="grid grid-cols-2 gap-2 mt-1">
                <button onClick={() => placeOrder("buy")}  disabled={trade.isPending} className="btn btn-buy !py-3 !text-sm">
                  {trade.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : "BUY"}
                </button>
                <button onClick={() => placeOrder("sell")} disabled={trade.isPending} className="btn btn-sell !py-3 !text-sm">
                  {trade.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : "SELL"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
