import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ScanSearch, Loader2 } from "lucide-react";
import { api } from "../lib/api";
import { cn, fmtUSD, fmtPct } from "../lib/cn";
import { haptic } from "../lib/native";

interface Props {
  onDrillIn: (ticker: string) => void;
}

type ScanRow = {
  Ticker: string; Price: number; "Change %": number;
  Score: number; Rating: string; Analysis?: string;
};

export function ScannerView({ onDrillIn }: Props) {
  const [mode, setMode] = useState<"top20" | "global">("top20");
  const scan = useMutation({
    mutationFn: () => api.scan(mode),
    onSuccess: () => haptic.success(),
    onError: () => haptic.error(),
  });

  const rows = ((scan.data?.rows as ScanRow[]) ?? []);

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      <div className="card card-tight">
        <div className="flex items-center gap-2 mb-3">
          <ScanSearch className="w-4 h-4 text-accent" />
          <h2 className="text-base font-semibold">Market Scanner</h2>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex border border-zinc-300 dark:border-zinc-700 rounded overflow-hidden">
            <button
              onClick={() => { haptic.tap(); setMode("top20"); }}
              className={cn(
                "px-3 py-1.5 text-xs",
                mode === "top20" ? "bg-accent text-white" : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              )}
            >Top 20 Universe</button>
            <button
              onClick={() => { haptic.tap(); setMode("global"); }}
              className={cn(
                "px-3 py-1.5 text-xs",
                mode === "global" ? "bg-accent text-white" : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              )}
            >Global Deep Scan</button>
          </div>
          <button
            onClick={() => { haptic.medium(); scan.mutate(); }}
            disabled={scan.isPending}
            className="btn btn-primary"
          >
            {scan.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
            Run Scan
          </button>
          <div className="text-xxs opacity-60">
            {mode === "top20"
              ? "~30s · curated 20-ticker universe"
              : "~3 min · full market sweep (GPU-bound)"}
          </div>
        </div>

        {scan.data?.summary && (
          <div className="text-xs opacity-70 mt-2">{scan.data.summary}</div>
        )}
      </div>

      {scan.isError && (
        <div className="card card-tight border-sell/30 text-xs text-sell">
          Scan failed: {(scan.error as Error).message}
        </div>
      )}

      {rows.length > 0 && (
        <div className="card overflow-hidden overflow-x-auto">
          <table className="w-full table-zebra">
            <thead>
              <tr className="text-left">
                <th>Ticker</th>
                <th className="text-right">Price</th>
                <th className="text-right">Δ%</th>
                <th className="text-right">Score</th>
                <th>Rating</th>
                <th className="hidden md:table-cell">Analysis</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} onClick={() => { haptic.tap(); onDrillIn(r.Ticker); }}>
                  <td className="font-mono font-bold">{r.Ticker}</td>
                  <td className="text-right font-mono">{fmtUSD(r.Price)}</td>
                  <td className={cn("text-right font-mono",
                    r["Change %"] >= 0 ? "text-buy" : "text-sell")}>
                    {fmtPct(r["Change %"])}
                  </td>
                  <td className="text-right font-mono font-bold">{r.Score}</td>
                  <td>
                    <span className={cn(
                      "px-1.5 py-0.5 rounded text-xxs font-bold",
                      r.Rating === "BUY"  && "bg-buy/15 text-buy",
                      r.Rating === "HOLD" && "bg-zinc-200 dark:bg-zinc-800",
                      r.Rating === "SELL" && "bg-sell/15 text-sell",
                    )}>{r.Rating}</span>
                  </td>
                  <td className="hidden md:table-cell opacity-70 text-xxs truncate max-w-md">
                    {r.Analysis}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!scan.isPending && rows.length === 0 && !scan.isError && (
        <div className="card card-tight nuroq-empty-state">
          Pick a mode and click <b>Run Scan</b>.
        </div>
      )}
    </div>
  );
}
