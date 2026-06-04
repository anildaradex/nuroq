// OrderReviewModal — proper "review before you submit" surface that opens
// when you tap ⚡ on a Watchlist row (or "Send to Quick Trade" in Analyze).
// Replaces the cramped bottom-strip prefill UX. Big touch targets, full
// summary (cost / risk / reward / R:R), prominent Cancel + Submit buttons.
//
// On desktop: centered modal, max-w-md.
// On mobile: full-screen-from-bottom sheet, safe-area respecting.

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { X, Zap, Loader2, AlertCircle, CheckCircle2, TrendingUp, TrendingDown, ShieldAlert } from "lucide-react";
import { api } from "../lib/api";
import { cn, fmtUSD, fmtPct } from "../lib/cn";
import { haptic } from "../lib/native";

export interface OrderReview {
  ticker: string;
  side: "buy" | "sell";
  shares: number;
  price: number;         // reference price for display + risk math
  sl?: number;
  tp?: number;
  company_name?: string;
  source?: "watchlist" | "analyze" | "manual";  // where the trigger came from
}

interface Props {
  order: OrderReview | null;
  onClose: () => void;
}

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

const DEBOUNCE_MS = 1500;

export function OrderReviewModal({ order, onClose }: Props) {
  // Local editable copies — user can adjust shares/SL/TP without
  // mutating the caller's state.
  //
  // Defaults to LIMIT entry (with the current price as the limit) rather than
  // MARKET because most NuroQ users want price discipline. Switch the dropdown
  // to Market for immediate fills. Both order types support the SL/TP bracket.
  const [shares, setShares] = useState<number>(0);
  const [sl, setSl] = useState<number | "">("");
  const [tp, setTp] = useState<number | "">("");
  const [orderType, setOrderType] = useState<"Market" | "Limit">("Limit");
  const [limitPrice, setLimitPrice] = useState<number | "">("");
  const [tif, setTif] = useState("GTC");

  // Debounce + idempotency key — same triple-layer protection as QuickTrade
  const lastClickAtRef = useRef<number>(0);
  const inFlightKeyRef = useRef<string | null>(null);

  // Wash-sale risk for this ticker. Fetched as soon as the modal opens.
  // If risk=true, the first Submit tap shows the warning + requires a second
  // tap with `acknowledged=true` to actually fire (with wash_sale_override).
  const washSale = useQuery({
    queryKey: ["wash-sale", order?.ticker ?? ""],
    queryFn: () => api.washSale(order!.ticker),
    enabled: !!order,
    staleTime: 60_000,
  });
  const [washAcknowledged, setWashAcknowledged] = useState(false);

  // Reset wash-sale acknowledgment whenever a new order opens
  useEffect(() => { setWashAcknowledged(false); }, [order]);

  // Refresh local state whenever a new order arrives. Default to a Limit
  // bracket pre-filled at the current price — user can adjust the limit
  // price down (better fill) or up (more aggressive), or flip to Market.
  useEffect(() => {
    if (!order) return;
    setShares(order.shares);
    setSl(order.sl ?? "");
    setTp(order.tp ?? "");
    setOrderType("Limit");
    setLimitPrice(order.price);
    setTif("GTC");
    haptic.tap();
  }, [order]);

  const trade = useMutation({
    mutationFn: api.trade,
    onSuccess: (resp) => {
      if (resp.ok) {
        haptic.success();
        // Auto-close on success after a brief moment so user sees the confirmation
        setTimeout(onClose, 1400);
      } else {
        haptic.error();
      }
    },
    onError: () => haptic.error(),
  });

  // ESC to close
  useEffect(() => {
    if (!order) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !trade.isPending) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [order, onClose, trade.isPending]);

  if (!order) return null;

  const sideLabel = order.side === "buy" ? "BUY" : "SELL";
  const sideBg    = order.side === "buy" ? "bg-buy"   : "bg-sell";
  const sideRing  = order.side === "buy" ? "ring-buy/30" : "ring-sell/30";

  // Order math
  const refPrice = orderType === "Limit" && limitPrice !== "" ? Number(limitPrice) : order.price;
  const sharesN  = Number(shares) || 0;
  const slN      = typeof sl === "number" ? sl : (sl === "" ? null : Number(sl));
  const tpN      = typeof tp === "number" ? tp : (tp === "" ? null : Number(tp));
  const cost     = sharesN * refPrice;

  // Risk = (entry - SL) × shares (for long); Reward = (TP - entry) × shares
  const risk    = (slN != null && order.side === "buy") ? Math.max(0, (refPrice - slN) * sharesN) : null;
  const reward  = (tpN != null && order.side === "buy") ? Math.max(0, (tpN - refPrice) * sharesN) : null;
  const rrRatio = (risk != null && reward != null && risk > 0) ? reward / risk : null;

  const valid = sharesN > 0 && refPrice > 0;

  const washRisk = order.side === "buy" && (washSale.data?.risk ?? false);
  const washNeedsAck = washRisk && !washAcknowledged;

  const submit = () => {
    if (!valid || trade.isPending) return;
    // First-tap on a wash-sale ticker: don't submit yet — flip the
    // acknowledgment flag, which switches the warning banner to "ACK'd"
    // state and changes the Submit button label to EXECUTE ANYWAY.
    if (washNeedsAck) {
      haptic.warning();
      setWashAcknowledged(true);
      return;
    }
    const now = Date.now();
    if (now - lastClickAtRef.current < DEBOUNCE_MS) {
      haptic.warning();
      return;
    }
    lastClickAtRef.current = now;
    const key = inFlightKeyRef.current ?? uuid();
    inFlightKeyRef.current = key;

    haptic.medium();
    trade.mutate(
      {
        ticker: order.ticker,
        shares: sharesN,
        action: order.side,
        order_type: orderType,
        tif,
        sl: slN ?? undefined,
        tp: tpN ?? undefined,
        limit_price: orderType === "Limit" && limitPrice !== "" ? Number(limitPrice) : undefined,
        idempotency_key: key,
        wash_sale_override: washAcknowledged,
      },
      { onSettled: () => { inFlightKeyRef.current = null; } }
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm
                 flex items-end sm:items-center justify-center
                 animate-in fade-in duration-150"
      onClick={() => !trade.isPending && onClose()}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className={cn(
          // Mobile: bottom sheet with safe-area padding
          // Desktop (sm+): centered card
          "w-full sm:max-w-md bg-white dark:bg-zinc-950",
          "border-t sm:border border-zinc-200 dark:border-zinc-800",
          "rounded-t-2xl sm:rounded-xl shadow-2xl",
          "pb-safe sm:pb-0",
          "animate-in slide-in-from-bottom sm:slide-in-from-bottom-0 sm:zoom-in-95 duration-200",
          `ring-2 ${sideRing}`,
        )}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-5 py-4 border-b border-zinc-200 dark:border-zinc-800">
          <Zap className="w-5 h-5 text-accent" />
          <div className="flex-1">
            <div className="text-sm font-bold">Review Order</div>
            {order.source && (
              <div className="text-xxs opacity-50 capitalize">
                from {order.source}
                {order.source === "watchlist" && " • ATR-sized"}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            disabled={trade.isPending}
            className="p-2 -mr-2 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-900
                       disabled:opacity-30"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Ticker + side banner */}
        <div className="px-5 py-4 flex items-baseline gap-3 border-b border-zinc-200 dark:border-zinc-800">
          <span className={cn("px-2.5 py-1 rounded text-sm font-bold text-white", sideBg)}>
            {sideLabel}
          </span>
          <span className="text-2xl font-mono font-bold">{order.ticker}</span>
          {order.company_name && (
            <span className="text-xs opacity-60 truncate">{order.company_name}</span>
          )}
          <div className="flex-1" />
          <span className="text-xs opacity-50 font-mono">@ {fmtUSD(order.price)}</span>
        </div>

        {/* Form */}
        <div className="p-5 space-y-4">
          {/* Shares */}
          <div>
            <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block">Shares</label>
            <input
              type="number"
              value={shares || ""}
              onChange={(e) => setShares(Number(e.target.value) || 0)}
              className="input w-full text-lg font-mono font-bold !py-2.5"
              min={1}
              step={1}
              autoFocus
            />
          </div>

          {/* Order type + TIF */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block">Type</label>
              <select
                value={orderType}
                onChange={(e) => setOrderType(e.target.value as "Market" | "Limit")}
                className="input w-full !py-2.5"
              >
                <option value="Market">Market</option>
                <option value="Limit">Limit</option>
              </select>
            </div>
            <div>
              <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block">TIF</label>
              <select
                value={tif}
                onChange={(e) => setTif(e.target.value)}
                className="input w-full !py-2.5"
              >
                {["Day", "GTC", "OPG", "IOC", "FOK"].map((t) => <option key={t}>{t}</option>)}
              </select>
            </div>
          </div>

          {/* Limit price (only when Limit) */}
          {orderType === "Limit" && (
            <div>
              <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block">Limit Price ($)</label>
              <input
                type="number"
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value === "" ? "" : Number(e.target.value))}
                className="input w-full !py-2.5"
                step={0.01}
              />
            </div>
          )}

          {/* SL/TP bracket — available for BOTH Market AND Limit entries.
              Backend routes either to alpaca_executor.submit_bracket_order
              with the right entry type. Hidden only for non-bracket-capable
              types (Stop, Stop Limit, Trailing Stop — not used in modal). */}
          {order.side === "buy" && (orderType === "Market" || orderType === "Limit") && (
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block flex items-center gap-1">
                  <TrendingDown className="w-3 h-3 text-sell" />
                  Stop Loss
                </label>
                <input
                  type="number"
                  value={sl}
                  onChange={(e) => setSl(e.target.value === "" ? "" : Number(e.target.value))}
                  className="input w-full !py-2.5 font-mono"
                  step={0.01}
                  placeholder="optional"
                />
              </div>
              <div>
                <label className="text-xxs uppercase opacity-60 tracking-wider mb-1 block flex items-center gap-1">
                  <TrendingUp className="w-3 h-3 text-buy" />
                  Take Profit
                </label>
                <input
                  type="number"
                  value={tp}
                  onChange={(e) => setTp(e.target.value === "" ? "" : Number(e.target.value))}
                  className="input w-full !py-2.5 font-mono"
                  step={0.01}
                  placeholder="optional"
                />
              </div>
            </div>
          )}

          {/* Helpful validation hint for the limit bracket — Alpaca rejects
              if the limit price isn't sandwiched between SL and TP. */}
          {orderType === "Limit" && order.side === "buy" && limitPrice !== "" && sl !== "" && tp !== "" && (
            (() => {
              const lp = Number(limitPrice), slv = Number(sl), tpv = Number(tp);
              if (!(slv < lp && lp < tp)) {
                return (
                  <div className="text-xxs text-amber-500 bg-amber-500/10 border border-amber-500/30 rounded px-2 py-1.5">
                    ⚠️ For a LIMIT BUY bracket, the limit (${lp}) must be between SL (${slv}) and TP (${tpv}).
                  </div>
                );
              }
              return null;
            })()
          )}

          {/* Wash-sale risk banner — only on BUY side, only when risk detected */}
          {washRisk && (
            <div className={cn(
              "flex items-start gap-2 px-3 py-2.5 rounded-md text-xs border",
              washAcknowledged
                ? "bg-amber-500/10 border-amber-500/40 text-amber-600 dark:text-amber-400"
                : "bg-sell/10 border-sell/40 text-sell"
            )}>
              <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
              <div className="leading-relaxed">
                <div className="font-semibold mb-0.5">
                  {washAcknowledged ? "Wash-sale risk acknowledged" : "🛑 Wash-sale risk"}
                </div>
                <div className="text-xxs opacity-90">{washSale.data?.hint}</div>
                {!washAcknowledged && (
                  <div className="text-xxs mt-1 opacity-75">
                    Tap Submit once to acknowledge, then again to execute anyway.
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Order summary */}
          <div className="bg-zinc-50 dark:bg-zinc-900 rounded-md p-3 space-y-1.5 border border-zinc-200 dark:border-zinc-800">
            <SumRow label="Estimated cost" value={fmtUSD(cost)} bold />
            {risk != null && (
              <SumRow
                label="Risk (entry → SL)"
                value={`-${fmtUSD(risk)}  (${fmtPct(-(risk / cost) * 100)})`}
                tone="sell"
              />
            )}
            {reward != null && (
              <SumRow
                label="Reward (entry → TP)"
                value={`+${fmtUSD(reward)}  (${fmtPct((reward / cost) * 100)})`}
                tone="buy"
              />
            )}
            {rrRatio != null && (
              <SumRow
                label="Risk/Reward"
                value={`1 : ${rrRatio.toFixed(2)}`}
                tone={rrRatio >= 2 ? "buy" : rrRatio >= 1 ? undefined : "warn"}
                bold
              />
            )}
          </div>

          {/* Status banner */}
          {trade.data && (
            <div className={cn(
              "flex items-start gap-2 px-3 py-2 rounded-md text-xs",
              trade.data.ok
                ? "bg-buy/10 text-buy border border-buy/30"
                : "bg-sell/10 text-sell border border-sell/30",
            )}>
              {trade.data.ok ? <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" /> : <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />}
              <span className="leading-relaxed">{trade.data.message}</span>
            </div>
          )}
          {trade.isError && (
            <div className="flex items-start gap-2 px-3 py-2 rounded-md text-xs bg-sell/10 text-sell border border-sell/30">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span className="leading-relaxed">{(trade.error as Error)?.message ?? "Submission failed"}</span>
            </div>
          )}
        </div>

        {/* Footer — big touch-friendly Cancel + Submit */}
        <div className="grid grid-cols-2 gap-3 p-4 border-t border-zinc-200 dark:border-zinc-800">
          <button
            onClick={onClose}
            disabled={trade.isPending}
            className="btn btn-ghost !py-3 !text-sm"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!valid || trade.isPending || (trade.data?.ok ?? false)}
            className={cn(
              "btn !py-3 !text-sm font-bold",
              order.side === "buy" ? "btn-buy" : "btn-sell",
              washNeedsAck && "!bg-sell",  // visually loud on first wash-sale tap
            )}
          >
            {trade.isPending
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : trade.data?.ok ? "Submitted ✓"
              : washNeedsAck ? `Acknowledge wash-sale risk`
              : washAcknowledged ? `⚠️ Execute Anyway`
              : `Submit ${sideLabel}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function SumRow({
  label, value, tone, bold,
}: { label: string; value: string; tone?: "buy" | "sell" | "warn"; bold?: boolean }) {
  return (
    <div className="flex justify-between items-baseline text-xs">
      <span className="opacity-60">{label}</span>
      <span className={cn(
        "font-mono",
        bold && "font-bold",
        tone === "buy"  && "text-buy",
        tone === "sell" && "text-sell",
        tone === "warn" && "text-amber-500",
      )}>{value}</span>
    </div>
  );
}
