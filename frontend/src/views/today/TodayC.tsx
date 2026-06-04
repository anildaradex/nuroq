// Variant C — "Linear / Notion" — quiet, hierarchical, the system tells you
// what to do. Big Next Action card on top. Then a single P&L line. Then
// a small stat strip. Then a focused activity card. Lots of breathing room.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Sparkles, ArrowRight, CheckCircle2, AlertTriangle, AlertCircle } from "lucide-react";
import { api } from "../../lib/api";
import { cn, fmtUSD, fmtPct, shortTime } from "../../lib/cn";
import { haptic } from "../../lib/native";
import type { VariantProps } from "./shared";

export function TodayC({ acct, cards, nextActions, feed, orders, portfolio }: VariantProps) {
  const qc = useQueryClient();
  const startAgent = useMutation({
    mutationFn: api.agentStart,
    onSuccess: () => { haptic.success(); qc.invalidateQueries(); },
  });

  if (!acct) return <div className="card card-tight h-40 animate-pulse" />;
  if (!acct.connected) {
    return <div className="card card-tight border-sell/30 text-sell text-xs">Alpaca disconnected.</div>;
  }

  const pl = acct.todays_pl;
  const plColor = pl >= 0 ? "text-buy" : "text-sell";

  // Pick the top-priority next action for hero
  const heroAction = nextActions?.[0];
  const agentRunning = cards?.agent.running ?? false;

  // Suggest "Start Agent" as a hero CTA when there's a watchlist but no agent
  const showStartAgentCTA = !agentRunning && (cards?.watchlist.buys ?? 0) > 0;

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      {/* ─── Hero: Next Action (or "Start Agent" if compelling) ───── */}
      {showStartAgentCTA ? (
        <div className="card p-5 border-accent/40 bg-gradient-to-br from-accent/5 to-transparent">
          <div className="flex items-start gap-3">
            <Sparkles className="w-5 h-5 text-accent shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold mb-1">Start the live agent</div>
              <div className="text-xs opacity-70 leading-relaxed">
                {cards?.watchlist.buys} BUY signals are ready on today's watchlist.
                The agent will react to live bars and ping you for approval on each.
              </div>
            </div>
            <button
              onClick={() => { haptic.medium(); startAgent.mutate(); }}
              disabled={startAgent.isPending}
              className="btn btn-primary shrink-0"
            >
              Start Agent <ArrowRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      ) : heroAction ? (
        <div className={cn(
          "card p-5",
          heroAction.level === "err"  && "border-sell/40 bg-sell/5",
          heroAction.level === "warn" && "border-amber-500/40 bg-amber-500/5",
          heroAction.level === "ok"   && "border-buy/40 bg-buy/5",
        )}>
          <div className="flex items-start gap-3">
            {heroAction.level === "err"  && <AlertCircle className="w-5 h-5 text-sell shrink-0 mt-0.5" />}
            {heroAction.level === "warn" && <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />}
            {heroAction.level === "ok"   && <CheckCircle2 className="w-5 h-5 text-buy shrink-0 mt-0.5" />}
            <div className="flex-1 min-w-0 text-xs leading-relaxed"
                 dangerouslySetInnerHTML={{ __html: heroAction.text.replace(/\*\*(.*?)\*\*/g, "<b>$1</b>") }} />
          </div>
        </div>
      ) : (
        <div className="card p-5 flex items-center gap-3">
          <CheckCircle2 className="w-5 h-5 text-buy shrink-0" />
          <div className="text-xs">
            <div className="font-semibold">All systems healthy</div>
            <div className="opacity-70 mt-0.5">No outstanding actions.</div>
          </div>
        </div>
      )}

      {/* ─── P&L: quiet but precise ─────────────────────────────── */}
      <div className="px-1">
        <div className="text-xxs uppercase opacity-50 tracking-wider mb-1">Portfolio</div>
        <div className="flex items-baseline gap-3 flex-wrap">
          <div className="text-4xl font-bold font-mono leading-none">
            {fmtUSD(acct.equity)}
          </div>
          <div className={cn("text-base font-medium font-mono", plColor)}>
            {pl >= 0 ? "+" : ""}{fmtUSD(pl)}
            <span className="text-xs opacity-70 ml-2">{fmtPct(acct.todays_pl_pct)} today</span>
          </div>
        </div>
      </div>

      {/* ─── Three-up stat strip ────────────────────────────────── */}
      <div className="grid grid-cols-3 gap-3 text-xs">
        <Stat label="Positions" value={String(portfolio?.length ?? 0)} sub={fmtUSD(acct.positions_value)} />
        <Stat label="Pending" value={String(orders?.length ?? 0)} sub="queued" />
        <Stat label="Watching" value={String((cards?.watchlist.buys ?? 0) + (cards?.watchlist.holds ?? 0))}
              sub={`${cards?.watchlist.buys ?? 0} buy`} />
      </div>

      {/* ─── Recent activity (clean) ────────────────────────────── */}
      <div>
        <div className="text-xxs uppercase opacity-50 tracking-wider mb-2 px-1">Recent activity</div>
        <div className="card overflow-hidden">
          {!feed || feed.length === 0 ? (
            <div className="px-3 py-6 text-xs opacity-50 text-center">
              No agent decisions or news shocks yet. The feed populates during
              market hours.
            </div>
          ) : (
            feed.slice(0, 8).map((e, i) => (
              <div key={i} className="px-3 py-2 flex items-start gap-3 border-t border-zinc-100 dark:border-zinc-800/50 first:border-t-0">
                <span className="text-base leading-none mt-0.5">
                  {e.kind === "news"
                    ? (e.classification === "POSITIVE_BOOST" ? "📈" : e.classification === "NEGATIVE_WARNING" ? "⚠️" : "🛑")
                    : (e.action === "FIRED_BUY" ? "🎯" : e.action === "FIRED_SELL" ? "📉" : "•")}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-xs flex items-baseline gap-2">
                    <span className="font-mono font-bold">{e.ticker}</span>
                    <span className="opacity-50 text-xxs font-mono">{shortTime(e.ts)}</span>
                  </div>
                  <div className="text-xxs opacity-70 mt-0.5 line-clamp-2 leading-snug">
                    {e.kind === "news"
                      ? `${e.classification?.replace(/_/g, " ")}: ${e.headline}`
                      : `${e.direction} ${e.action?.replace(/_/g, " ").toLowerCase()}${
                          e.score_before != null && e.score_after != null ? ` (${e.score_before}→${e.score_after})` : ""
                        }${e.price ? ` @ ${fmtUSD(e.price)}` : ""}`}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* ─── Additional next actions (if more beyond hero) ──────── */}
      {nextActions && nextActions.length > 1 && (
        <div>
          <div className="text-xxs uppercase opacity-50 tracking-wider mb-2 px-1">More to consider</div>
          <ul className="space-y-1.5 px-1">
            {nextActions.slice(1, 4).map((s, i) => (
              <li key={i} className="flex items-start gap-2 text-xs">
                <span className={cn(
                  "mt-1 w-1.5 h-1.5 rounded-full shrink-0",
                  s.level === "ok"   && "bg-buy",
                  s.level === "warn" && "bg-amber-500",
                  s.level === "err"  && "bg-sell",
                )} />
                <span className="opacity-80" dangerouslySetInnerHTML={{ __html: s.text.replace(/\*\*(.*?)\*\*/g, "<b>$1</b>") }} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card card-tight">
      <div className="text-xxs uppercase opacity-50 tracking-wider mb-1">{label}</div>
      <div className="text-2xl font-bold font-mono leading-none">{value}</div>
      {sub && <div className="text-xxs opacity-50 mt-1 font-mono">{sub}</div>}
    </div>
  );
}
