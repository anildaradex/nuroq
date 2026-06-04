// Today shell — wraps 4 design variants with a switcher so we can compare
// fully-rendered layouts side-by-side with real data. Variant persists to
// localStorage so your pick survives reloads.

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import { haptic } from "../lib/native";
import { TodayA } from "./today/TodayA";
import { TodayB } from "./today/TodayB";
import { TodayC } from "./today/TodayC";
import { TodayD } from "./today/TodayD";

type Variant = "A" | "B" | "C" | "D";

const VARIANTS: Array<{ key: Variant; label: string; tagline: string }> = [
  { key: "A", label: "Hero",     tagline: "Robinhood / Schwab — big P&L + actions" },
  { key: "B", label: "Terminal", tagline: "Bloomberg — dense multi-pane" },
  { key: "C", label: "Calm",     tagline: "Linear / Notion — Next Action forward" },
  { key: "D", label: "Stream",   tagline: "Trade Ideas — live feed primary" },
];

export function TodayView() {
  const [variant, setVariant] = useState<Variant>(
    (localStorage.getItem("nuroq.todayVariant") as Variant) || "A"
  );

  // Persist + haptic feedback on switch
  useEffect(() => {
    localStorage.setItem("nuroq.todayVariant", variant);
  }, [variant]);

  // Shared data — fetched once at the shell level so all 4 variants render
  // from identical state instantly when you flip between them.
  const acct = useQuery({ queryKey: ["alpaca-summary"], queryFn: api.alpacaSummary, refetchInterval: 30_000 });
  const hist = useQuery({ queryKey: ["alpaca-history"], queryFn: () => api.alpacaHistory(30), refetchInterval: 5 * 60_000 });
  const cards = useQuery({ queryKey: ["today-cards"], queryFn: api.todayCards, refetchInterval: 30_000 });
  const nextActions = useQuery({ queryKey: ["next-actions"], queryFn: api.nextActions, refetchInterval: 60_000 });
  const feed = useQuery({ queryKey: ["feed"], queryFn: api.feed, refetchInterval: 30_000 });
  const orders = useQuery({ queryKey: ["pending-orders"], queryFn: api.pendingOrders, refetchInterval: 60_000 });
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: api.portfolio, refetchInterval: 60_000 });

  const shared = {
    acct: acct.data, history: hist.data, cards: cards.data,
    nextActions: nextActions.data, feed: feed.data, orders: orders.data,
    portfolio: portfolio.data,
  };

  const currentMeta = VARIANTS.find((v) => v.key === variant)!;

  return (
    <div className="max-w-7xl mx-auto space-y-3">
      {/* Design-lab banner — remove this whole block once a variant is picked */}
      <div className="card card-tight flex items-center gap-2 flex-wrap border-accent/30 bg-accent/5">
        <Sparkles className="w-3.5 h-3.5 text-accent shrink-0" />
        <span className="text-xxs font-medium opacity-80">Design lab — same data, four layouts:</span>
        <div className="flex border border-zinc-300 dark:border-zinc-700 rounded overflow-hidden">
          {VARIANTS.map((v) => (
            <button
              key={v.key}
              onClick={() => { haptic.tap(); setVariant(v.key); }}
              className={cn(
                "px-2.5 py-1 text-xxs font-medium border-r border-zinc-300 dark:border-zinc-700 last:border-r-0",
                variant === v.key
                  ? "bg-accent text-white"
                  : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
              )}
              title={v.tagline}
            >
              <span className="font-mono opacity-70">{v.key}</span> · {v.label}
            </button>
          ))}
        </div>
        <span className="text-xxs opacity-60 italic">{currentMeta.tagline}</span>
      </div>

      {/* Variant body */}
      {variant === "A" && <TodayA {...shared} />}
      {variant === "B" && <TodayB {...shared} />}
      {variant === "C" && <TodayC {...shared} />}
      {variant === "D" && <TodayD {...shared} />}
    </div>
  );
}
