import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { cn, fmtUSD } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  onDrillIn: (ticker: string) => void;
}

export function SignalsView({ onDrillIn }: Props) {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"all" | "BUY" | "HOLD" | "SELL">("all");
  const { data, isLoading } = useQuery({
    queryKey: ["signals"], queryFn: api.signals, refetchInterval: 60_000,
  });

  const rows = (data ?? []).filter((r) => filter === "all" || r.signal === filter);

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-base font-semibold flex items-center gap-2">
          <History className="w-4 h-4 text-accent" /> Signal History
        </h2>
        <span className="text-xxs opacity-60">• {data?.length ?? 0} entries</span>
        <div className="flex border border-zinc-300 dark:border-zinc-700 rounded overflow-hidden ml-2">
          {(["all", "BUY", "HOLD", "SELL"] as const).map((f) => (
            <button
              key={f}
              onClick={() => { haptic.tap(); setFilter(f); }}
              className={cn(
                "px-2 py-1 text-xxs",
                filter === f ? "bg-accent text-white" : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              )}
            >{f}</button>
          ))}
        </div>
        <div className="flex-1" />
        <button
          onClick={() => { haptic.tap(); qc.invalidateQueries({ queryKey: ["signals"] }); }}
          className="btn btn-ghost !px-2"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isLoading && "animate-spin")} />
        </button>
      </div>

      {!data && isLoading && <div className="card card-tight h-40 animate-pulse" />}

      {rows.length === 0 && (
        <div className="card card-tight nuroq-empty-state">
          <h3 className="text-sm font-semibold">No signals match</h3>
          <p className="text-xs mt-1">Try a different filter or run an analysis from the Analyze tab.</p>
        </div>
      )}

      {rows.length > 0 && (
        <div className="card overflow-hidden overflow-x-auto">
          <table className="w-full table-zebra">
            <thead>
              <tr className="text-left">
                <th>Time</th>
                <th>Ticker</th>
                <th className="hidden md:table-cell">Name</th>
                <th className="hidden lg:table-cell">Industry</th>
                <th className="text-right">Price</th>
                <th>Signal</th>
                <th className="text-right">Quant</th>
                <th className="text-right">AI</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} onClick={() => { haptic.tap(); onDrillIn(r.ticker); }}>
                  <td className="font-mono opacity-60 text-xxs">{r.timestamp}</td>
                  <td className="font-mono font-bold">{r.ticker}</td>
                  <td className="hidden md:table-cell opacity-80 text-xxs truncate max-w-[160px]">
                    {r.name}
                  </td>
                  <td className="hidden lg:table-cell opacity-60 text-xxs truncate max-w-[140px]">
                    {r.industry}
                  </td>
                  <td className="text-right font-mono">{fmtUSD(r.price)}</td>
                  <td>
                    <span className={cn(
                      "px-1.5 py-0.5 rounded text-xxs font-bold",
                      r.signal === "BUY"  && "bg-buy/15 text-buy",
                      r.signal === "HOLD" && "bg-zinc-200 dark:bg-zinc-800",
                      r.signal === "SELL" && "bg-sell/15 text-sell",
                    )}>{r.signal}</span>
                  </td>
                  <td className="text-right font-mono">{r.quant_score ?? "—"}</td>
                  <td className="text-right font-mono opacity-70">{r.ai_score ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
