// Variant A — "Robinhood / Schwab" — Hero P&L, sparkline, pinned action row,
// then positions, watching, compact feed. Reads like a retail brokerage app.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import {
  Play, FlaskConical, Zap, ScanSearch, RefreshCw,
  AlertCircle, ChevronRight, Newspaper,
} from "lucide-react";
import { api } from "../../lib/api";
import { cn, fmtUSD, fmtPct, shortTime } from "../../lib/cn";
import { haptic } from "../../lib/native";
import type { VariantProps } from "./shared";

export function TodayA({ acct, history, cards, nextActions, feed, orders, portfolio }: VariantProps) {
  const qc = useQueryClient();
  const startAgent  = useMutation({ mutationFn: api.agentStart,    onSuccess: () => { haptic.success(); qc.invalidateQueries(); } });
  const research    = useMutation({ mutationFn: api.researchCycle, onSuccess: () => qc.invalidateQueries() });
  const agentRunning = cards?.agent.running ?? false;

  if (!acct) return <Skeleton />;
  if (!acct.connected) return <Disconnected />;

  const pl = acct.todays_pl;
  const plColor = pl >= 0 ? "text-buy" : "text-sell";
  const plStroke = pl >= 0 ? "#1a8348" : "#ef4444";
  const series = history?.equity_series.map((v, i) => ({ i, v })) ?? [];

  return (
    <div className="space-y-3">
      {/* ─── Hero card: massive P&L + sparkline ─────────────────────── */}
      <div className="card p-5 relative overflow-hidden">
        <div className="relative z-10">
          <div className={cn("text-5xl sm:text-6xl font-bold font-mono leading-none tracking-tight", plColor)}>
            {pl >= 0 ? "+" : ""}{fmtUSD(pl)}
          </div>
          <div className={cn("text-sm font-medium mt-1 font-mono", plColor)}>
            {fmtPct(acct.todays_pl_pct)} today
            <span className="opacity-50 ml-2">· {fmtUSD(acct.equity)} equity</span>
          </div>
        </div>
        {series.length > 1 && (
          <div className="absolute inset-x-0 bottom-0 h-24 opacity-60">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
                <YAxis hide domain={["dataMin", "dataMax"]} />
                <Line type="monotone" dataKey="v" stroke={plStroke} strokeWidth={2}
                      dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
        {history && (
          <div className="absolute top-3 right-4 text-xxs font-mono opacity-50">
            30d · {history.return_pct >= 0 ? "+" : ""}{history.return_pct.toFixed(2)}%
          </div>
        )}
      </div>

      {/* ─── Pinned action row ─────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <ActionTile
          icon={Play} label={agentRunning ? "Agent Running" : "Start Agent"}
          tone={agentRunning ? "ok" : "primary"}
          loading={startAgent.isPending}
          disabled={agentRunning}
          onClick={() => { haptic.medium(); startAgent.mutate(); }}
        />
        <ActionTile
          icon={FlaskConical} label="Run Research"
          loading={research.isPending}
          onClick={() => { haptic.medium(); research.mutate(); }}
        />
        <ActionTile
          icon={Zap} label="Quick Trade"
          onClick={() => { haptic.tap(); document.querySelector<HTMLButtonElement>("[aria-label='Quick Trade']")?.click(); }}
        />
        <ActionTile
          icon={ScanSearch} label="Scan Market"
          onClick={() => {
            haptic.tap();
            localStorage.setItem("nuroq.view", "scanner");
            window.location.reload();
          }}
        />
      </div>

      {/* ─── Positions (compact) ──────────────────────────────────── */}
      <div className="card overflow-hidden">
        <SectionHeader title={`My Positions (${portfolio?.length ?? 0})`} onRefresh={() => qc.invalidateQueries({ queryKey: ["portfolio"] })} />
        {!portfolio || portfolio.length === 0 ? (
          <div className="px-3 py-4 text-xs opacity-60 text-center">No open positions.</div>
        ) : (
          portfolio.slice(0, 5).map((p) => (
            <div key={p.ticker}
                 className="px-3 py-2 flex items-center gap-3 border-t border-zinc-100 dark:border-zinc-800/50 first:border-t-0">
              <span className="font-mono font-bold w-16">{p.ticker}</span>
              <span className="text-xs opacity-60">{p.shares} @ {fmtUSD(p.avg_price)}</span>
              <div className="flex-1" />
              <span className={cn("font-mono font-medium text-sm",
                p.pnl_pct >= 0 ? "text-buy" : "text-sell")}>
                {fmtPct(p.pnl_pct)}
              </span>
              <span className="font-mono text-sm w-20 text-right">{fmtUSD(p.total_value)}</span>
            </div>
          ))
        )}
      </div>

      {/* ─── Watching + Pending side-by-side ──────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="card card-tight">
          <div className="text-xxs uppercase opacity-50 mb-1">Watching for crossings</div>
          {cards ? (
            <>
              <div className="text-2xl font-bold font-mono">
                <span className="text-buy">{cards.watchlist.buys}</span>
                <span className="opacity-30 mx-1">/</span>
                <span className="opacity-70">{cards.watchlist.buys + cards.watchlist.holds}</span>
              </div>
              <div className="text-xxs opacity-60 mt-1">
                <b className="text-buy">{cards.watchlist.buys}</b> BUYs ready · {cards.watchlist.holds} HOLDs near threshold
              </div>
            </>
          ) : <div className="h-10 animate-pulse bg-zinc-100 dark:bg-zinc-900 rounded" />}
        </div>

        <div className="card card-tight">
          <div className="text-xxs uppercase opacity-50 mb-1">Pending orders</div>
          <div className="text-2xl font-bold font-mono">{orders?.length ?? 0}</div>
          <div className="text-xxs opacity-60 mt-1">
            {orders?.length ? "queued · will route at market open" : "none queued"}
          </div>
        </div>
      </div>

      {/* ─── Next Actions (compact, if any) ───────────────────────── */}
      {nextActions && nextActions.length > 0 && (
        <div className="card card-tight">
          <div className="text-xxs uppercase opacity-50 mb-2">Next Actions</div>
          <ul className="space-y-1.5">
            {nextActions.slice(0, 3).map((s, i) => (
              <li key={i} className="flex items-start gap-2 text-xs">
                <span className={cn(
                  "mt-1 w-1.5 h-1.5 rounded-full shrink-0",
                  s.level === "ok" && "bg-buy",
                  s.level === "warn" && "bg-amber-500",
                  s.level === "err" && "bg-sell",
                )} />
                <span dangerouslySetInnerHTML={{ __html: s.text.replace(/\*\*(.*?)\*\*/g, "<b>$1</b>") }} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ─── Recent activity (last 6) ─────────────────────────────── */}
      <div className="card overflow-hidden">
        <SectionHeader title="Recent activity" />
        {!feed || feed.length === 0 ? (
          <div className="px-3 py-4 text-xs opacity-60 text-center">
            No agent decisions or news shocks yet.
          </div>
        ) : (
          feed.slice(0, 6).map((e, i) => (
            <FeedRowCompact key={i} e={e} />
          ))
        )}
      </div>
    </div>
  );
}

function ActionTile({
  icon: Icon, label, onClick, tone, loading, disabled,
}: {
  icon: typeof Play; label: string; onClick: () => void;
  tone?: "primary" | "ok"; loading?: boolean; disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "card card-tight flex flex-col items-center justify-center gap-1.5 py-3 transition-colors",
        !disabled && "hover:border-accent active:scale-95",
        tone === "ok" && "border-buy/30 bg-buy/5",
        tone === "primary" && "border-accent/30 bg-accent/5",
        disabled && "opacity-60 cursor-default",
        loading && "animate-pulse",
      )}
    >
      <Icon className={cn(
        "w-5 h-5",
        tone === "ok" && "text-buy",
        tone === "primary" && "text-accent",
      )} />
      <span className="text-xxs font-medium text-center">{label}</span>
    </button>
  );
}

function SectionHeader({ title, onRefresh }: { title: string; onRefresh?: () => void }) {
  return (
    <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-center gap-2">
      <span className="text-xxs uppercase font-semibold opacity-70">{title}</span>
      <div className="flex-1" />
      {onRefresh && (
        <button onClick={() => { haptic.tap(); onRefresh(); }}
                className="p-0.5 rounded opacity-60 hover:opacity-100">
          <RefreshCw className="w-3 h-3" />
        </button>
      )}
      <ChevronRight className="w-3 h-3 opacity-30" />
    </div>
  );
}

function FeedRowCompact({ e }: { e: VariantProps["feed"] extends (infer R)[] | undefined ? R : never }) {
  const isNews = e.kind === "news";
  const sym = isNews
    ? (e.classification === "POSITIVE_BOOST" ? "📈" : e.classification === "NEGATIVE_WARNING" ? "⚠️" : "🛑")
    : (e.action === "FIRED_BUY" ? "🎯" : e.action === "FIRED_SELL" ? "📉" : "•");
  const desc = isNews
    ? `${e.classification?.replace(/_/g, " ")}: ${e.headline}`
    : `${e.direction} ${e.action?.replace(/_/g, " ").toLowerCase()}` +
      (e.price ? ` @ ${fmtUSD(e.price)}` : "");
  return (
    <div className="px-3 py-2 flex items-baseline gap-2 text-xxs border-t border-zinc-100 dark:border-zinc-800/50 first:border-t-0">
      <span className="font-mono opacity-50 w-10">{shortTime(e.ts)}</span>
      <span>{sym}</span>
      <span className="font-mono font-bold w-12">{e.ticker}</span>
      <span className="opacity-80 truncate flex-1">{desc}</span>
    </div>
  );
}

function Skeleton() {
  return <div className="card card-tight h-40 animate-pulse" />;
}

function Disconnected() {
  return (
    <div className="card card-tight border-sell/30">
      <div className="flex items-center gap-2 text-sell">
        <AlertCircle className="w-4 h-4" />
        <span className="text-xs font-medium">Alpaca not connected — check .env keys</span>
      </div>
    </div>
  );
}

// Suppress unused-imports if your linter is strict
void Newspaper;
