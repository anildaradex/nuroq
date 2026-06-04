import { useQuery } from "@tanstack/react-query";
import { api, type PillState } from "../lib/api";
import { cn } from "../lib/cn";

const labels: Record<keyof Omit<Awaited<ReturnType<typeof api.statusPills>>, "ts">, string> = {
  agent: "Agent",
  stream: "Stream",
  telegram: "Telegram",
  alpaca: "Alpaca",
};

const dotColor = (s: PillState) =>
  s === "ok"   ? "bg-buy" :
  s === "warn" ? "bg-amber-500" :
  s === "err"  ? "bg-sell" :
                 "bg-zinc-400";

const pillClass = (s: PillState) =>
  s === "ok"   ? "pill pill-ok" :
  s === "warn" ? "pill pill-warn" :
  s === "err"  ? "pill pill-err" :
                 "pill pill-off";

export function StatusPills() {
  const { data } = useQuery({
    queryKey: ["status-pills"],
    queryFn: api.statusPills,
    refetchInterval: 15_000,
  });

  const pills: Array<{ key: keyof typeof labels; state: PillState }> = data
    ? (Object.keys(labels) as Array<keyof typeof labels>).map((k) => ({
        key: k,
        state: data[k] as PillState,
      }))
    : (Object.keys(labels) as Array<keyof typeof labels>).map((k) => ({
        key: k,
        state: "off" as PillState,
      }));

  return (
    <>
      {/* Mobile: dots-only, tight */}
      <div className="flex sm:hidden items-center gap-1">
        {pills.map(({ key, state }) => (
          <span
            key={key}
            title={`${labels[key]}: ${state}`}
            className={cn("w-2 h-2 rounded-full", dotColor(state))}
          />
        ))}
      </div>
      {/* Desktop: full pills with labels */}
      <div className="hidden sm:flex items-center gap-1.5">
        {pills.map(({ key, state }) => (
          <span key={key} className={cn(pillClass(state), "!py-0.5 !text-xxs")}>
            <span className={cn("w-1.5 h-1.5 rounded-full", dotColor(state))} />
            {labels[key]}
          </span>
        ))}
      </div>
    </>
  );
}
