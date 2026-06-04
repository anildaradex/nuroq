// Tiny className combiner. Avoids pulling in clsx-everywhere boilerplate.
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function fmtUSD(n: number, decimals = 2): string {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtNum(n: number, decimals = 0): string {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtPct(n: number, decimals = 2): string {
  const s = n >= 0 ? "+" : "";
  return `${s}${n.toFixed(decimals)}%`;
}

export function relTime(ts: number): string {
  const seconds = (Date.now() / 1000) - ts;
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.floor(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${hours.toFixed(1)}h ago`;
  const days = hours / 24;
  return `${days.toFixed(1)}d ago`;
}

export function shortTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
