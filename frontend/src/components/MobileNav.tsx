import { useState } from "react";
import {
  LineChart, BarChart3, Target, Briefcase, MoreHorizontal, X,
  ScanSearch, Bot, History, Activity, FileText,
} from "lucide-react";
import { cn } from "../lib/cn";
import type { ViewId } from "./LeftRailNav";

interface Props {
  active: ViewId;
  onSelect: (id: ViewId) => void;
}

// 5-tab iOS-style bottom bar. The 4 most-used views get their own slot;
// "More" opens a sheet with the rest. Tap targets are 56px tall (above
// Apple's 44pt min), tabs respect the home-indicator safe area.
const PRIMARY: Array<{ id: ViewId; label: string; Icon: typeof LineChart }> = [
  { id: "today",     label: "Today",     Icon: LineChart },
  { id: "analyze",   label: "Analyze",   Icon: BarChart3 },
  { id: "watchlist", label: "Watchlist", Icon: Target },
  { id: "portfolio", label: "Positions", Icon: Briefcase },
];

const SECONDARY: Array<{ id: ViewId; label: string; Icon: typeof LineChart }> = [
  { id: "scanner", label: "Scanner",        Icon: ScanSearch },
  { id: "agent",   label: "Agent",          Icon: Bot },
  { id: "signals", label: "Signal History", Icon: History },
  { id: "system",  label: "System",         Icon: Activity },
  { id: "logs",    label: "Logs",           Icon: FileText },
];

export function MobileNav({ active, onSelect }: Props) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const inSecondary = SECONDARY.some((s) => s.id === active);

  return (
    <>
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-30
                   border-t border-zinc-200 dark:border-zinc-800
                   bg-white/95 dark:bg-zinc-950/95 backdrop-blur
                   pb-safe"
      >
        <div className="flex items-stretch h-14">
          {PRIMARY.map(({ id, label, Icon }) => (
            <TabBtn
              key={id}
              label={label}
              Icon={Icon}
              active={id === active}
              onClick={() => onSelect(id)}
            />
          ))}
          <TabBtn
            label="More"
            Icon={MoreHorizontal}
            active={inSecondary || sheetOpen}
            onClick={() => setSheetOpen(true)}
          />
        </div>
      </nav>

      {sheetOpen && (
        <div
          className="lg:hidden fixed inset-0 z-40 bg-black/50 backdrop-blur-sm
                     flex items-end"
          onClick={() => setSheetOpen(false)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full bg-white dark:bg-zinc-950 rounded-t-xl
                       border-t border-zinc-200 dark:border-zinc-800
                       pb-safe animate-in slide-in-from-bottom duration-200"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-200 dark:border-zinc-800">
              <span className="text-sm font-semibold">More</span>
              <button
                onClick={() => setSheetOpen(false)}
                className="p-2 -mr-2 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-900"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-2">
              {SECONDARY.map(({ id, label, Icon }) => (
                <button
                  key={id}
                  onClick={() => { onSelect(id); setSheetOpen(false); }}
                  className={cn(
                    "w-full flex items-center gap-3 px-3 py-3 rounded-md text-sm",
                    id === active
                      ? "bg-accent/15 text-accent"
                      : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
                  )}
                >
                  <Icon className="w-5 h-5" />
                  <span>{label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function TabBtn({
  label, Icon, active, onClick,
}: { label: string; Icon: typeof LineChart; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex-1 flex flex-col items-center justify-center gap-0.5 transition-colors",
        active ? "text-accent" : "text-zinc-500 dark:text-zinc-400"
      )}
    >
      <Icon className="w-5 h-5" />
      <span className="text-xxs font-medium">{label}</span>
    </button>
  );
}
