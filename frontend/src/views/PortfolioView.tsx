import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Area, AreaChart, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { Briefcase, RefreshCw, Trash2, TrendingUp, TrendingDown, Wallet } from "lucide-react";
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

  const removeMut = useMutation({
    mutationFn: (t: string) => api.removePosition(t),
    onSuccess: () => { haptic.success(); qc.invalidateQueries({ queryKey: ["portfolio"] }); },
    onError: () => haptic.error(),
  });

  const rows = data ?? [];
  // Cost basis = avg cost × shares; value = current × shares (total_value from API).
  const totalCost  = rows.reduce((s, r) => s + r.avg_price * r.shares, 0);
  const totalValue = rows.reduce((s, r) => s + r.total_value, 0);
  const totalPnl   = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;

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
          {/* Summary stat cards */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard
              icon={Wallet} label="Cost Basis"
              value={fmtUSD(totalCost)}
              sub={`${rows.length} positions`}
            />
            <StatCard
              icon={Briefcase} label="Market Value"
              value={fmtUSD(totalValue)}
              sub="current"
            />
            <StatCard
              icon={totalPnl >= 0 ? TrendingUp : TrendingDown}
              label="Unrealized P&L"
              value={`${totalPnl >= 0 ? "+" : ""}${fmtUSD(totalPnl)}`}
              sub={fmtPct(totalPnlPct)}
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
  tone?: "buy" | "sell";
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
      )}>{value}</div>
      {sub && <div className="text-xxs opacity-50 mt-1 font-mono">{sub}</div>}
    </div>
  );
}

function EquityChart({
  hist, range, setRange, loading,
}: {
  hist: { equity_series: number[]; timestamps: number[]; return_pct: number } | undefined;
  range: Range; setRange: (r: Range) => void; loading: boolean;
}) {
  const series = (hist?.equity_series ?? []).map((v, i) => ({
    i,
    equity: v,
    t: hist?.timestamps?.[i] ? new Date(hist.timestamps[i] * 1000) : null,
  }));

  // Color the curve by whether the period is up or down overall.
  const up = (hist?.return_pct ?? 0) >= 0;
  const stroke = up ? "#10b981" : "#ef4444";
  const baseline = series.length ? series[0].equity : 0;

  const fmtAxisDate = (i: number) => {
    const t = series[i]?.t;
    return t ? t.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
  };

  return (
    <div className="card card-tight">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs font-semibold opacity-80">Portfolio Performance</span>
        {hist && (
          <span className={cn("text-xs font-mono font-bold", up ? "text-buy" : "text-sell")}>
            {fmtPct(hist.return_pct)}
          </span>
        )}
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
            <AreaChart data={series} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
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
                tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
                tick={{ fontSize: 10, fill: "currentColor", opacity: 0.55 }}
                stroke="currentColor" strokeOpacity={0.15}
                width={48}
              />
              <Tooltip
                contentStyle={{
                  background: "rgba(10,10,10,0.92)", border: "1px solid #333",
                  borderRadius: 6, fontSize: 11, padding: "6px 10px",
                }}
                labelStyle={{ color: "#9ca3af", marginBottom: 2 }}
                labelFormatter={(i) => {
                  const t = series[i as number]?.t;
                  return t ? t.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) : "";
                }}
                formatter={(v) => [typeof v === "number" ? fmtUSD(v) : String(v), "Equity"]}
              />
              {baseline > 0 && (
                <ReferenceLine y={baseline} stroke="currentColor" strokeOpacity={0.25}
                  strokeDasharray="4 4" />
              )}
              <Area
                type="monotone" dataKey="equity"
                stroke={stroke} strokeWidth={2}
                fill="url(#equity-fill)" dot={false}
                isAnimationActive={true} animationDuration={500}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="text-xxs opacity-40 mt-1 text-right">
        Account equity over {range} days · dashed line = period start
      </div>
    </div>
  );
}
