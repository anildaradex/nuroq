// Variant A — "Robinhood / Schwab" — Hero P&L, sparkline, pinned action row,
// then positions, watching, compact feed. Reads like a retail brokerage app.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import {
  Play, FlaskConical, Zap, ScanSearch, RefreshCw,
  AlertCircle, ChevronRight, Newspaper,
} from "lucide-react";
import { api, type ResearchStatus } from "../../lib/api";
import { cn, fmtUSD, fmtPct, shortTime } from "../../lib/cn";
import { haptic } from "../../lib/native";
import type { VariantProps } from "./shared";

export function TodayA({ acct, history, cards, nextActions, feed, orders, portfolio }: VariantProps) {
  const qc = useQueryClient();
  const startAgent  = useMutation({ mutationFn: api.agentStart,    onSuccess: () => { haptic.success(); qc.invalidateQueries(); } });

  // Research cycle: poll status so the tile + banner can show live progress.
  // Fast poll (3s) only when active; slow (60s) when idle. This is how the
  // user gets "yes it actually started" feedback — the tile transitions to
  // a running state within one tick of clicking.
  const researchSt = useQuery<ResearchStatus>({
    queryKey: ["research-status"], queryFn: api.researchStatus,
    refetchInterval: (q) => (q.state.data?.active ? 3_000 : 60_000),
    staleTime: 2_000,
  });
  const research = useMutation({
    mutationFn: api.researchCycle,
    onSuccess: () => {
      haptic.success();
      // Force an IMMEDIATE status refetch so the tile flips to "Running" within
      // the network roundtrip, not on the next 60s tick.
      qc.invalidateQueries({ queryKey: ["research-status"] });
    },
    onError: () => haptic.error(),
  });

  const rs = researchSt.data;
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

      {/* ─── Research-cycle status banner ───────────────────────────
          Surfaces immediate feedback after the user clicks Run Research:
          (a) the backend's success message while the mutation just settled,
          then (b) live progress while the cycle is running. Replaces the
          previous "click → silence" UX. */}
      {(rs?.active || research.isSuccess || research.isError) && (
        <ResearchBanner
          status={rs}
          justStartedMessage={research.isSuccess ? research.data?.message : undefined}
          errorMessage={research.isError ? "Failed to start — check Logs." : undefined}
          onDismissJustStarted={() => research.reset()}
        />
      )}

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
          icon={FlaskConical}
          label={
            rs?.active
              ? `Research ${rs.progress}/${rs.total || "…"} · ${Math.floor(rs.elapsed_sec / 60)}m`
              : research.isPending ? "Starting…" : "Run Research"
          }
          tone={rs?.active ? "ok" : undefined}
          loading={research.isPending || rs?.active}
          disabled={rs?.active}
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

function ResearchBanner({
  status, justStartedMessage, errorMessage, onDismissJustStarted,
}: {
  status: ResearchStatus | undefined;
  justStartedMessage?: string;
  errorMessage?: string;
  onDismissJustStarted: () => void;
}) {
  // Error wins (fastest signal something's wrong).
  if (errorMessage) {
    return (
      <div className="card card-tight border-sell/40 bg-sell/5 flex items-start gap-2 text-xs">
        <AlertCircle className="w-3.5 h-3.5 text-sell shrink-0 mt-0.5" />
        <div className="opacity-90">{errorMessage}</div>
      </div>
    );
  }

  // Cycle running: show live progress prominently. Buyers also see this if
  // someone else (or the cron) started a cycle — it's the canonical UI signal.
  if (status?.active) {
    const pct = status.percent || 0;
    const elapsed = `${Math.floor(status.elapsed_sec / 60)}m ${status.elapsed_sec % 60}s`;
    const eta = status.eta_sec != null
      ? `~${Math.max(1, Math.ceil(status.eta_sec / 60))}m left`
      : "warming up";
    return (
      <div className="card card-tight border-accent/40 bg-accent/5 space-y-2 text-xs">
        <div className="flex items-center gap-2">
          <FlaskConical className="w-3.5 h-3.5 text-accent shrink-0" />
          <span className="font-semibold">Research cycle running</span>
          <span className="opacity-70 font-mono">
            {status.progress}/{status.total || "…"} · {elapsed} · {eta}
          </span>
        </div>
        {/* Animated progress bar — gives a moving signal even when progress is
            stuck at 0 (the pre-fetch phase, before per-ticker work starts). */}
        <div className="h-1.5 rounded-full bg-zinc-200 dark:bg-zinc-800 overflow-hidden">
          <div
            className="h-full bg-accent transition-[width] duration-700"
            style={{ width: `${pct || 4}%` }}
          />
        </div>
      </div>
    );
  }

  // Mutation just succeeded but status hasn't ticked back yet (the 1-second
  // gap between POST returning and the next status poll). Show the backend's
  // start message so the user gets immediate confirmation. Auto-dismisses when
  // the status query catches up (the parent removes us once `active: true`).
  if (justStartedMessage) {
    return (
      <div className="card card-tight border-accent/40 bg-accent/5 flex items-start gap-2 text-xs">
        <FlaskConical className="w-3.5 h-3.5 text-accent shrink-0 mt-0.5 animate-pulse" />
        <div className="flex-1 opacity-90">{justStartedMessage}</div>
        <button
          onClick={onDismissJustStarted}
          className="text-xxs opacity-50 hover:opacity-100"
        >dismiss</button>
      </div>
    );
  }

  return null;
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
