import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import { haptic } from "../lib/native";

export function SystemView() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["system-health"], queryFn: api.systemHealth, refetchInterval: 30_000,
  });

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold flex items-center gap-2">
          <Activity className="w-4 h-4 text-accent" /> System Health
        </h2>
        <div className="flex-1" />
        <button
          onClick={() => { haptic.tap(); qc.invalidateQueries({ queryKey: ["system-health"] }); }}
          className="btn btn-ghost !px-2"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isLoading && "animate-spin")} />
        </button>
      </div>

      {!data && isLoading && <div className="card card-tight h-40 animate-pulse" />}

      {data && (
        <div className="card overflow-hidden">
          {data.map((c, i) => (
            <div
              key={i}
              className={cn(
                "flex items-start gap-3 px-3 py-2.5 text-xs",
                i > 0 && "border-t border-zinc-200 dark:border-zinc-800"
              )}
            >
              <span className={cn(
                "mt-1 w-2 h-2 rounded-full shrink-0",
                c.state === "ok"   && "bg-buy",
                c.state === "warn" && "bg-amber-500",
                c.state === "err"  && "bg-sell",
                c.state === "off"  && "bg-zinc-400",
              )} />
              <div className="flex-1 min-w-0">
                <div className="font-medium">{c.name}</div>
                <div className="text-xxs opacity-70 break-words">{c.status}</div>
              </div>
              <span className={cn(
                "px-1.5 py-0.5 rounded text-xxs font-bold shrink-0",
                c.state === "ok"   && "bg-buy/15 text-buy",
                c.state === "warn" && "bg-amber-500/15 text-amber-500",
                c.state === "err"  && "bg-sell/15 text-sell",
                c.state === "off"  && "bg-zinc-200 dark:bg-zinc-800 opacity-70",
              )}>{c.state.toUpperCase()}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
