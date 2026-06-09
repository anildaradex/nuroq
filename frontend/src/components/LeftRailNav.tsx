import {
  LineChart, BarChart3, Target, ScanSearch, Briefcase,
  Bot, History, Activity, FileText, Settings,
} from "lucide-react";
import { cn } from "../lib/cn";

export type ViewId =
  | "today" | "analyze" | "watchlist" | "scanner"
  | "portfolio" | "agent" | "signals" | "system" | "logs" | "config";

interface NavItem {
  id: ViewId;
  label: string;
  Icon: typeof LineChart;
}

const NAV: NavItem[] = [
  { id: "today",      label: "Today",          Icon: LineChart },
  { id: "analyze",    label: "Analyze",        Icon: BarChart3 },
  { id: "watchlist",  label: "Watchlist",      Icon: Target },
  { id: "scanner",    label: "Scanner",        Icon: ScanSearch },
  { id: "portfolio",  label: "Portfolio",      Icon: Briefcase },
  { id: "agent",      label: "Agent",          Icon: Bot },
  { id: "signals",    label: "Signal History", Icon: History },
  { id: "system",     label: "System",         Icon: Activity },
  { id: "logs",       label: "Logs",           Icon: FileText },
  { id: "config",     label: "Configuration",  Icon: Settings },
];

interface Props {
  active: ViewId;
  onSelect: (id: ViewId) => void;
}

export function LeftRailNav({ active, onSelect }: Props) {
  return (
    <nav
      className="w-[180px] shrink-0 border-r border-zinc-200 dark:border-zinc-800
                 bg-white dark:bg-zinc-950 py-3 px-2 flex flex-col gap-0.5"
    >
      {NAV.map(({ id, label, Icon }) => {
        const isActive = id === active;
        return (
          <button
            key={id}
            onClick={() => onSelect(id)}
            className={cn(
              "w-full flex items-center gap-2.5 px-2.5 py-2 rounded-md",
              "text-xs font-medium transition-colors",
              isActive
                ? "bg-accent/15 text-accent border border-accent/30"
                : "text-zinc-600 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-900 border border-transparent"
            )}
          >
            <Icon className="w-4 h-4" />
            <span>{label}</span>
          </button>
        );
      })}
      <div className="mt-auto pt-3 text-xxs text-zinc-500 px-2 leading-tight">
        <div className="opacity-60">v2 · React</div>
        <div className="opacity-60">Press <span className="kbd">⌘K</span> to search</div>
      </div>
    </nav>
  );
}
