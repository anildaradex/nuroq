import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
  ComposedChart,
} from "recharts";
import { Briefcase, RefreshCw, Trash2, TrendingUp, TrendingDown, Wallet, Scale } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { cn, fmtUSD, fmtPct } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  onDrillIn: (ticker: string) => void;
}

type Range = 7 | 30 | 90;

export function PortfolioView({ onDrillIn }: Props) {
  const qc = useQueryClient();
  const [range, setRange] = useState<Range>(30);

  const { data, isFetching } = useQuery({
    queryKey: ["portfolio"], queryFn: api.portfolio, refetchInterval: 60_000,
  });
  const hist = useQuery({
    queryKey: ["alpaca-history", range], queryFn: () => api.alpacaHistory(range),
    refetchInterval: 5 * 60_000,
  });
  // Account-level metrics from Alpaca: equity, cash, positions_value.
  // Needed to surface margin/leverage — summing position MVs gives the GROSS
  // exposure (what NuroQ used to show as "Total Value"), but account equity is
  // gross MV minus the cash deficit when on margin. Casual viewers were reading
  // gross-MV as "my account is worth this" — which is wrong on margin. We now
  // show both, clearly labeled, with a leverage indicator.
  const acct = useQuery({
    queryKey: ["alpaca-summary"], queryFn: api.alpacaSummary, refetchInterval: 60_000,
  });

  const removeMut = useMutation({
    mutationFn: (t: string) => api.removePosition(t),
    onSuccess: () => { haptic.success(); qc.invalidateQueries({ queryKey: ["portfolio"] }); },
    onError: () => haptic.error(),
  });

  const rows = data ?? [];
  // Per-position math (cost = avg×shares; MV = current×shares from API).
  const totalCost  = rows.reduce((s, r) => s + r.avg_price * r.shares, 0);
  const totalValue = rows.reduce((s, r) => s + r.total_value, 0);
  const totalPnl   = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;

  // Account-level (Alpaca-truthed). Prefer Alpaca's `positions_value` to
  // tolerate any per-row rounding drift, but fall back to summed row MVs if
  // the account call hasn't loaded yet.
  const equity         = acct.data?.equity ?? totalValue;
  const cash           = acct.data?.cash ?? 0;
  const positionsValue = acct.data?.positions_value ?? totalValue;
  // On margin when cash is negative — borrowing against equity to hold more
  // stock than the equity alone would allow. Leverage = positions / equity.
  const onMargin       = cash < 0 && equity > 0;
  const leverage       = onMargin ? positionsValue / equity : 1;
  const marginUsed     = onMargin ? -cash : 0;

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-base font-semibold flex items-center gap-2">
          <Briefcase className="w-4 h-4 text-accent" /> Portfolio
        </h2>
        {rows.length > 0 && (
          <span className="text-xxs opacity-60">{rows.length} positions</span>
        )}
        <div className="flex-1" />
        <button
          onClick={() => { haptic.tap(); qc.invalidateQueries(); }}
          className="btn btn-ghost flex items-center gap-1.5 !px-3"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isFetching && "animate-spin")} />
          <span className="text-xxs">{isFetching ? "Refreshing…" : "Refresh"}</span>
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="card card-tight nuroq-empty-state">
          <h3 className="text-sm font-semibold">No positions</h3>
          <p className="text-xs mt-1">
            Open a position via Quick Trade, or wait for the Live Agent's BUY approvals.
          </p>
        </div>
      ) : (
        <>
          {/* Summary stat cards. The PRIMARY number is Account Equity — this
              is what Alpaca's UI shows as "Total Equity" and what gets actually
              liquidated. "Market Value" is the gross positions exposure, which
              exceeds equity when on margin. We surface both with their
              relationship explicit, so the equity vs gross-MV confusion can't
              recur. */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard
              icon={Wallet} label="Account Equity"
              value={fmtUSD(equity)}
              sub={onMargin
                ? `${fmtUSD(marginUsed)} margin used`
                : `${fmtUSD(cash)} cash`}
              tone={onMargin ? "warn" : undefined}
            />
            <StatCard
              icon={Briefcase} label="Positions Value"
              value={fmtUSD(positionsValue)}
              sub={onMargin
                ? `${leverage.toFixed(2)}× leverage · gross`
                : `${rows.length} positions · gross`}
            />
            <StatCard
              icon={totalPnl >= 0 ? TrendingUp : TrendingDown}
              label="Unrealized P&L"
              value={`${totalPnl >= 0 ? "+" : ""}${fmtUSD(totalPnl)}`}
              sub={`${fmtPct(totalPnlPct)} · cost ${fmtUSD(totalCost)}`}
              tone={totalPnl >= 0 ? "buy" : "sell"}
            />
            <StatCard
              icon={hist.data && hist.data.return_pct >= 0 ? TrendingUp : TrendingDown}
              label={`${range}-Day Return`}
              value={hist.data ? fmtPct(hist.data.return_pct) : "—"}
              sub="account equity"
              tone={hist.data ? (hist.data.return_pct >= 0 ? "buy" : "sell") : undefined}
            />
          </div>

          {/* Margin banner: only when on margin, give the equity = MV − debt
              math explicitly. Removes ambiguity for casual viewers. */}
          {onMargin && (
            <div className="card card-tight flex items-start gap-2 text-xxs border-amber-500/40 bg-amber-500/5">
              <Scale className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
              <div className="opacity-90">
                <span className="font-semibold">On margin:</span>{" "}
                <span className="font-mono">{fmtUSD(positionsValue)}</span> positions
                {" − "}<span className="font-mono">{fmtUSD(marginUsed)}</span> borrowed
                {" = "}<span className="font-mono font-semibold">{fmtUSD(equity)}</span> equity
                {" "}<span className="opacity-60">(matches Alpaca's "Total Equity").</span>
              </div>
            </div>
          )}

          {/* Equity curve chart */}
          <EquityChart hist={hist.data} range={range} setRange={setRange} loading={hist.isFetching} />

          {/* Holdings table */}
          <div className="card overflow-hidden">
            <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 text-xs font-semibold opacity-80">
              Holdings ({rows.length})
            </div>
            <div className="overflow-x-auto">
              <table className="w-full table-zebra">
                <thead>
                  <tr className="text-left">
                    <th>Ticker</th>
                    <th className="text-right">Shares</th>
                    <th className="text-right">Avg</th>
                    <th className="text-right">Now</th>
                    <th className="text-right">Cost Basis</th>
                    <th className="text-right">Value</th>
                    <th className="text-right">P&L</th>
                    <th className="text-right">P&L %</th>
                    <th className="text-right hidden md:table-cell">SL / TP</th>
                    <th className="text-right"></th>
                  </tr>
                </thead>
                <tbody>
                  {[...rows]
                    .sort((a, b) => b.total_value - a.total_value)
                    .map((r) => {
                      const cost = r.avg_price * r.shares;
                      const pnl$ = r.total_value - cost;
                      return (
                        <tr key={r.ticker} onClick={() => { haptic.tap(); onDrillIn(r.ticker); }}>
                          <td className="font-mono font-bold">{r.ticker}</td>
                          <td className="text-right font-mono">{r.shares}</td>
                          <td className="text-right font-mono opacity-70">{fmtUSD(r.avg_price)}</td>
                          <td className="text-right font-mono">{fmtUSD(r.current_price)}</td>
                          <td className="text-right font-mono opacity-80">{fmtUSD(cost)}</td>
                          <td className="text-right font-mono font-medium">{fmtUSD(r.total_value)}</td>
                          <td className={cn("text-right font-mono", pnl$ >= 0 ? "text-buy" : "text-sell")}>
                            {pnl$ >= 0 ? "+" : ""}{fmtUSD(pnl$)}
                          </td>
                          <td className={cn("text-right font-mono", r.pnl_pct >= 0 ? "text-buy" : "text-sell")}>
                            {fmtPct(r.pnl_pct)}
                          </td>
                          <td className="text-right font-mono opacity-70 text-xxs hidden md:table-cell">
                            {r.stop_loss ? fmtUSD(r.stop_loss) : "—"} / {r.take_profit ? fmtUSD(r.take_profit) : "—"}
                          </td>
                          <td className="text-right">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                if (confirm(`Remove ${r.ticker} from the local tracker? (does NOT close the Alpaca position)`)) {
                                  removeMut.mutate(r.ticker);
                                }
                              }}
                              className="p-1 rounded text-sell hover:bg-sell/10"
                              title="Remove from local tracker"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
                {/* Totals footer */}
                <tfoot>
                  <tr className="border-t-2 border-zinc-300 dark:border-zinc-700 font-semibold">
                    <td className="font-mono">TOTAL</td>
                    <td></td><td></td><td></td>
                    <td className="text-right font-mono">{fmtUSD(totalCost)}</td>
                    <td className="text-right font-mono">{fmtUSD(totalValue)}</td>
                    <td className={cn("text-right font-mono", totalPnl >= 0 ? "text-buy" : "text-sell")}>
                      {totalPnl >= 0 ? "+" : ""}{fmtUSD(totalPnl)}
                    </td>
                    <td className={cn("text-right font-mono", totalPnl >= 0 ? "text-buy" : "text-sell")}>
                      {fmtPct(totalPnlPct)}
                    </td>
                    <td className="hidden md:table-cell"></td>
                    <td></td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function StatCard({
  icon: Icon, label, value, sub, tone,
}: {
  icon: typeof Wallet; label: string; value: string; sub?: string;
  tone?: "buy" | "sell" | "warn";
}) {
  return (
    <div className="card card-tight">
      <div className="flex items-center gap-1.5 text-xxs uppercase tracking-wider opacity-50 mb-1">
        <Icon className="w-3 h-3" /> {label}
      </div>
      <div className={cn(
        "text-xl font-mono font-bold leading-none",
        tone === "buy" && "text-buy",
        tone === "sell" && "text-sell",
        tone === "warn" && "text-amber-500",
      )}>{value}</div>
      {sub && <div className="text-xxs opacity-50 mt-1 font-mono">{sub}</div>}
    </div>
  );
}

function EquityChart({
  hist, range, setRange, loading,
}: {
  hist: { equity_series: number[]; timestamps: number[]; return_pct: number;
          benchmarks?: Record<string, { closes: number[]; return_pct: number }> } | undefined;
  range: Range; setRange: (r: Range) => void; loading: boolean;
}) {
  // Normalize ALL three lines (you + SPY + VOO) to "% from period start" so the
  // $96k equity and the $737 SPY line are visually comparable.
  const equity = hist?.equity_series ?? [];
  const equityBase = equity.length ? equity[0] : 0;
  const benches = hist?.benchmarks ?? {};
  // Each benchmark gets a normalized series of the same length as the equity.
  // If lengths differ slightly (Alpaca vs yfinance count of trading days), we
  // align to the equity's length by trimming/padding from the trailing side.
  const benchPctBySym: Record<string, number[]> = {};
  for (const [sym, b] of Object.entries(benches)) {
    const closes = b.closes || [];
    if (closes.length < 2) continue;
    const tail = closes.slice(-equity.length);
    const b0 = tail[0] || 1;
    benchPctBySym[sym] = tail.map((c) => (c / b0 - 1) * 100);
  }

  const series = equity.map((v, i) => {
    const row: Record<string, number | Date | null> = {
      i,
      equity_pct: equityBase ? (v / equityBase - 1) * 100 : 0,
      t: hist?.timestamps?.[i] ? new Date(hist.timestamps[i] * 1000) : null,
    };
    for (const sym of Object.keys(benchPctBySym)) {
      row[sym] = benchPctBySym[sym][i] ?? null;
    }
    return row;
  });

  const up = (hist?.return_pct ?? 0) >= 0;
  const stroke = up ? "#1a8348" : "#ef4444";
  // Distinct colors that read against the green/red filled area.
  const benchColors: Record<string, string> = { SPY: "#2563eb", VOO: "#a855f7" };
  const benchSyms = Object.keys(benchPctBySym);

  const fmtAxisDate = (i: number) => {
    const t = series[i]?.t as Date | null;
    return t ? t.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
  };

  return (
    <div className="card card-tight">
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="text-xs font-semibold opacity-80">Portfolio vs Benchmarks</span>
        {/* Legend chips — show return % for each line. The user's first. */}
        <span className={cn("text-xxs font-mono font-bold flex items-center gap-1",
                            up ? "text-buy" : "text-sell")}>
          <span className="w-2 h-2 rounded-full inline-block" style={{ background: stroke }} />
          YOU {fmtPct(hist?.return_pct ?? 0)}
        </span>
        {benchSyms.map((sym) => {
          const ret = benches[sym]?.return_pct ?? 0;
          const color = benchColors[sym] ?? "#64748b";
          const positive = ret >= 0;
          return (
            <span key={sym} className="text-xxs font-mono flex items-center gap-1">
              <span className="w-2 h-2 rounded-full inline-block" style={{ background: color }} />
              <span className="opacity-80">{sym}</span>
              <span className={positive ? "text-buy" : "text-sell"}>{fmtPct(ret)}</span>
            </span>
          );
        })}
        <div className="flex-1" />
        {/* Range selector */}
        <div className="flex border border-zinc-300 dark:border-zinc-700 rounded overflow-hidden">
          {([7, 30, 90] as Range[]).map((r) => (
            <button
              key={r}
              onClick={() => { haptic.tap(); setRange(r); }}
              className={cn(
                "px-2.5 py-1 text-xxs font-medium",
                range === r ? "bg-accent text-white" : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              )}
            >{r}D</button>
          ))}
        </div>
      </div>

      <div className="h-[260px] lg:h-[320px]">
        {loading && !hist ? (
          <div className="h-full animate-pulse bg-zinc-100 dark:bg-zinc-900 rounded" />
        ) : series.length < 2 ? (
          <div className="h-full flex items-center justify-center text-xs opacity-50">
            Not enough history yet for a {range}-day curve.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={series} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
              <defs>
                <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"   stopColor={stroke} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="i"
                tickFormatter={fmtAxisDate}
                tick={{ fontSize: 10, fill: "currentColor", opacity: 0.55 }}
                stroke="currentColor" strokeOpacity={0.15}
                minTickGap={40}
              />
              <YAxis
                domain={["dataMin", "dataMax"]}
                tickFormatter={(v) => `${v >= 0 ? "+" : ""}${(v as number).toFixed(1)}%`}
                tick={{ fontSize: 10, fill: "currentColor", opacity: 0.55 }}
                stroke="currentColor" strokeOpacity={0.15}
                width={52}
              />
              <Tooltip
                contentStyle={{
                  background: "rgba(10,10,10,0.92)", border: "1px solid #333",
                  borderRadius: 6, fontSize: 11, padding: "6px 10px",
                }}
                labelStyle={{ color: "#9ca3af", marginBottom: 2 }}
                labelFormatter={(i) => {
                  const t = series[i as number]?.t as Date | null;
                  return t ? t.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) : "";
                }}
                formatter={(v, name) => {
                  const num = typeof v === "number" ? v : 0;
                  const label = name === "equity_pct" ? "YOU" : String(name);
                  return [`${num >= 0 ? "+" : ""}${num.toFixed(2)}%`, label];
                }}
              />
              {/* Zero line — the period start (everyone starts at 0%). */}
              <ReferenceLine y={0} stroke="currentColor" strokeOpacity={0.30}
                strokeDasharray="4 4" />
              {/* The user's equity — filled area in green/red. */}
              <Area
                type="monotone" dataKey="equity_pct"
                stroke={stroke} strokeWidth={2}
                fill="url(#equity-fill)" dot={false}
                isAnimationActive={true} animationDuration={500}
              />
              {/* Benchmarks — thin lines, no fill, dashed pattern so they don't
                  fight visually with the user's filled curve. */}
              {benchSyms.map((sym) => (
                <Line
                  key={sym}
                  type="monotone" dataKey={sym}
                  stroke={benchColors[sym] ?? "#64748b"}
                  strokeWidth={1.5} strokeDasharray="5 3"
                  dot={false} isAnimationActive={false}
                  connectNulls
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="text-xxs opacity-40 mt-1 text-right">
        {benchSyms.length > 0
          ? `% return over ${range} days · YOU vs ${benchSyms.join(" / ")} · dashed line = period start`
          : `Account equity over ${range} days · dashed line = period start`}
      </div>
    </div>
  );
}
