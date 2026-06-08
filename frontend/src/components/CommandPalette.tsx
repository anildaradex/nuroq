import { Command } from "cmdk";
import { useEffect, useState } from "react";
import {
  LineChart, BarChart3, Target, ScanSearch, Briefcase, Bot,
  History, Activity, FileText, Play, Square, FlaskConical, RefreshCw, Search,
} from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { ViewId } from "./LeftRailNav";

interface Props {
  open: boolean;
  onClose: () => void;
  onNavigate: (id: ViewId) => void;
  onAnalyze?: (ticker: string) => void;
}

export function CommandPalette({ open, onClose, onNavigate, onAnalyze }: Props) {
  // Local query state so we can detect when the user typed a bare ticker
  // and offer an "Analyze TICKER" action below the navigation items.
  const [q, setQ] = useState("");
  const looksLikeTicker = /^[A-Z]{1,6}(\.[A-Z]{1,2})?$/.test(q.trim().toUpperCase());
  const qc = useQueryClient();
  const startAgent = useMutation({
    mutationFn: api.agentStart,
    onSettled: () => qc.invalidateQueries(),
  });
  const stopAgent = useMutation({
    mutationFn: api.agentStop,
    onSettled: () => qc.invalidateQueries(),
  });
  const runResearch = useMutation({
    mutationFn: api.researchCycle,
    onSettled: () => {
      // Bump the research-status query so TodayA's running banner appears
      // immediately on the next render, not on the next 60s tick.
      qc.invalidateQueries({ queryKey: ["research-status"] });
      qc.invalidateQueries();
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-start justify-center pt-[15vh]"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl mx-4"
      >
        <Command
          className="rounded-lg border border-zinc-300 dark:border-zinc-800
                     bg-white dark:bg-zinc-950 shadow-2xl overflow-hidden"
          label="Command Menu"
        >
          <div className="flex items-center border-b border-zinc-200 dark:border-zinc-800 px-3">
            <Command.Input
              autoFocus
              value={q}
              onValueChange={setQ}
              placeholder="Type a ticker (NVDA), or 'start agent', 'run scan'…"
              className="w-full bg-transparent py-3 text-sm outline-none placeholder:text-zinc-400"
            />
            <span className="kbd">ESC</span>
          </div>
          <Command.List className="max-h-[60vh] overflow-y-auto p-2">
            <Command.Empty className="py-6 text-center text-xs text-zinc-500">
              No results. Try a ticker like "NVDA" or a command name.
            </Command.Empty>

            {looksLikeTicker && onAnalyze && (
              <Command.Group heading="Analyze" className="text-xxs uppercase text-zinc-500 px-2 py-1">
                <PItem
                  icon={Search}
                  label={`Analyze ${q.trim().toUpperCase()}`}
                  onSelect={() => { onAnalyze(q.trim().toUpperCase()); }}
                />
              </Command.Group>
            )}

            <Command.Group heading="Navigate" className="text-xxs uppercase text-zinc-500 px-2 py-1">
              <PItem icon={LineChart}   label="Today"          onSelect={() => onNavigate("today")} />
              <PItem icon={BarChart3}   label="Analyze"        onSelect={() => onNavigate("analyze")} />
              <PItem icon={Target}      label="Watchlist"      onSelect={() => onNavigate("watchlist")} />
              <PItem icon={ScanSearch}  label="Scanner"        onSelect={() => onNavigate("scanner")} />
              <PItem icon={Briefcase}   label="Portfolio"      onSelect={() => onNavigate("portfolio")} />
              <PItem icon={Bot}         label="Agent"          onSelect={() => onNavigate("agent")} />
              <PItem icon={History}     label="Signal History" onSelect={() => onNavigate("signals")} />
              <PItem icon={Activity}    label="System"         onSelect={() => onNavigate("system")} />
              <PItem icon={FileText}    label="Logs"           onSelect={() => onNavigate("logs")} />
            </Command.Group>

            <Command.Group heading="Actions" className="text-xxs uppercase text-zinc-500 px-2 py-1 mt-1">
              <PItem icon={Play}          label="Start Agent"    onSelect={() => { startAgent.mutate(); onClose(); }} />
              <PItem icon={Square}        label="Stop Agent"     onSelect={() => { stopAgent.mutate(); onClose(); }} />
              <PItem icon={FlaskConical}  label="Run Research Cycle (~9 min)" onSelect={() => { runResearch.mutate(); onClose(); }} />
              <PItem icon={RefreshCw}     label="Refresh All"    onSelect={() => { qc.invalidateQueries(); onClose(); }} />
            </Command.Group>
          </Command.List>
        </Command>
      </div>
    </div>
  );
}

function PItem({
  icon: Icon,
  label,
  onSelect,
}: { icon: typeof LineChart; label: string; onSelect: () => void }) {
  return (
    <Command.Item
      onSelect={onSelect}
      className="flex items-center gap-2 px-2 py-1.5 rounded text-xs
                 cursor-pointer aria-selected:bg-accent/15 aria-selected:text-accent"
    >
      <Icon className="w-3.5 h-3.5" />
      <span>{label}</span>
    </Command.Item>
  );
}
