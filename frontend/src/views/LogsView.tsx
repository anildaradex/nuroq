import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import { haptic } from "../lib/native";

export function LogsView() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["logs"], queryFn: () => api.logs(300), refetchInterval: 15_000,
  });

  // Color-code log lines by level
  const colorFor = (line: string) =>
    line.includes("[ERROR]")   ? "text-sell" :
    line.includes("[WARNING]") ? "text-amber-500" :
    line.includes("[INFO]")    ? "opacity-80" :
                                 "opacity-60";

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold flex items-center gap-2">
          <FileText className="w-4 h-4 text-accent" /> Activity Logs
        </h2>
        <span className="text-xxs opacity-60">• tail of activity.log</span>
        <div className="flex-1" />
        <button
          onClick={() => { haptic.tap(); qc.invalidateQueries({ queryKey: ["logs"] }); }}
          className="btn btn-ghost !px-2"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isLoading && "animate-spin")} />
        </button>
      </div>

      {!data && isLoading && <div className="card card-tight h-40 animate-pulse" />}

      {data && (
        <div className="card overflow-hidden">
          <div className="bg-zinc-950 dark:bg-black text-zinc-200 font-mono text-xxs leading-relaxed overflow-x-auto max-h-[70vh] overflow-y-auto">
            {data.lines.length === 0 ? (
              <div className="p-4 text-zinc-500">Log file is empty.</div>
            ) : (
              data.lines.map((line, i) => (
                <div
                  key={i}
                  className={cn("px-3 py-0.5 whitespace-pre-wrap break-all", colorFor(line))}
                >
                  {line}
                </div>
              ))
            )}
          </div>
          <div className="px-3 py-1 text-xxs opacity-50 border-t border-zinc-200 dark:border-zinc-800 font-mono">
            {data.path}
          </div>
        </div>
      )}
    </div>
  );
}
