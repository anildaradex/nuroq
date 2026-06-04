import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Bot, Play, Square, RefreshCw, Loader2 } from "lucide-react";
import { api } from "../lib/api";
import { cn, fmtUSD, shortTime, relTime } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  onDrillIn: (ticker: string) => void;
}

export function AgentView({ onDrillIn }: Props) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["agent-status"], queryFn: api.todayCards,
    refetchInterval: 15_000,
    select: (d) => d.agent,
  });
  const log = useQuery({
    queryKey: ["agent-log"], queryFn: () => api.agentLog(100),
    refetchInterval: 30_000,
  });

  const startMut = useMutation({
    mutationFn: api.agentStart,
    onSuccess: () => { haptic.success(); qc.invalidateQueries(); },
    onError: () => haptic.error(),
  });
  const stopMut = useMutation({
    mutationFn: api.agentStop,
    onSuccess: () => { haptic.medium(); qc.invalidateQueries(); },
    onError: () => haptic.error(),
  });

  const s = status.data;
  const lastMsg = startMut.data?.message ?? stopMut.data?.message;

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="card card-tight">
        <div className="flex items-center gap-2 mb-3">
          <Bot className="w-4 h-4 text-accent" />
          <h2 className="text-base font-semibold">Live Reactive Agent</h2>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <button
            onClick={() => startMut.mutate()}
            disabled={startMut.isPending || (s?.running ?? false)}
            className="btn btn-primary"
          >
            {startMut.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            Start Agent
          </button>
          <button
            onClick={() => stopMut.mutate()}
            disabled={stopMut.isPending || !s?.running}
            className="btn btn-sell"
          >
            {stopMut.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Square className="w-3.5 h-3.5" />}
            Stop Agent
          </button>
          <div className="flex-1" />
          <button
            onClick={() => { haptic.tap(); qc.invalidateQueries(); }}
            className="btn btn-ghost !px-2"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>

        {lastMsg && (
          <div className={cn(
            "text-xs px-2 py-1.5 rounded mb-3",
            lastMsg.includes("✅") || lastMsg.includes("🟢") ? "bg-buy/10 text-buy" :
            lastMsg.includes("⚠️") ? "bg-amber-500/10 text-amber-500" :
            lastMsg.includes("🛑") || lastMsg.includes("❌") ? "bg-sell/10 text-sell" :
            "bg-zinc-100 dark:bg-zinc-800"
          )}>
            {lastMsg}
          </div>
        )}

        {s && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xxs">
            <Stat label="Status" value={
              <span className={s.running ? "text-buy" : "opacity-60"}>
                {s.running ? <><span className="nuroq-live-dot inline-block mr-1" /> Running</> : "Stopped"}
              </span>
            } />
            <Stat label="Subscribed" value={`${s.subscribed_tickers} tickers`} />
            <Stat label="Bars processed" value={s.bars_processed.toLocaleString()} />
            <Stat label="Last bar" value={s.latest_bar_ts ? shortTime(s.latest_bar_ts) : "—"} />
            <Stat label="BUYs today" value={`${s.buys_fired_today}/${s.buys_cap}`} />
            <Stat label="SELLs today" value={String(s.sells_fired_today)} />
            <Stat label="Started" value={s.started_at ? new Date(s.started_at).toLocaleTimeString() : "—"} />
          </div>
        )}
      </div>

      <div className="card overflow-hidden">
        <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 text-xs font-semibold opacity-80">
          📋 Recent triggers (last {log.data?.length ?? 0})
        </div>
        {log.data && log.data.length === 0 ? (
          <div className="p-6 text-center text-xs opacity-50">
            No live triggers yet. The agent populates this log as it detects
            threshold crossings during market hours.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full table-zebra">
              <thead>
                <tr className="text-left">
                  <th>Time</th>
                  <th>Ticker</th>
                  <th>Dir</th>
                  <th className="text-right">Score</th>
                  <th className="text-right">Price</th>
                  <th>Action</th>
                  <th className="hidden md:table-cell">Notes</th>
                </tr>
              </thead>
              <tbody>
                {log.data?.map((r, i) => (
                  <tr key={i} onClick={() => { haptic.tap(); onDrillIn(r.ticker); }}>
                    <td className="font-mono opacity-60 text-xxs">
                      {r.ts ? relTime(r.ts) : "—"}
                    </td>
                    <td className="font-mono font-bold">{r.ticker}</td>
                    <td className={cn(
                      "font-medium",
                      r.direction === "BUY" ? "text-buy" : "text-sell"
                    )}>{r.direction}</td>
                    <td className="text-right font-mono">
                      {r.score_before != null && r.score_after != null
                        ? `${r.score_before}→${r.score_after}` : "—"}
                    </td>
                    <td className="text-right font-mono">{r.price ? fmtUSD(r.price) : "—"}</td>
                    <td>
                      <span className={cn(
                        "px-1.5 py-0.5 rounded text-xxs",
                        r.action === "FIRED_BUY"  && "bg-buy/15 text-buy",
                        r.action === "FIRED_SELL" && "bg-sell/15 text-sell",
                        r.action.startsWith("SUPPRESSED") && "bg-amber-500/15 text-amber-500",
                      )}>{r.action}</span>
                    </td>
                    <td className="hidden md:table-cell opacity-60 text-xxs truncate max-w-xs">
                      {r.notes ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="border border-zinc-200 dark:border-zinc-800 rounded px-2 py-1.5">
      <div className="opacity-50">{label}</div>
      <div className="font-mono font-medium text-xs mt-0.5">{value}</div>
    </div>
  );
}
