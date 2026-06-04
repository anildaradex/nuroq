import { Brain, Search, Moon, Sun, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { StatusPills } from "./StatusPills";
import { useQueryClient } from "@tanstack/react-query";
import { haptic, syncStatusBar } from "../lib/native";

interface Props {
  onOpenPalette: () => void;
}

export function TopBar({ onOpenPalette }: Props) {
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark")
  );
  const qc = useQueryClient();

  useEffect(() => {
    if (dark) document.documentElement.classList.add("dark");
    else document.documentElement.classList.remove("dark");
    localStorage.setItem("nuroq.theme", dark ? "dark" : "light");
    syncStatusBar(dark);
  }, [dark]);

  return (
    <header
      className="sticky top-0 z-30 flex items-center gap-2 px-3 py-2
                 border-b border-zinc-200 dark:border-zinc-800
                 bg-white/85 dark:bg-zinc-950/85 backdrop-blur
                 pt-safe pl-safe pr-safe"
    >
      {/* Brand — full text on desktop, just logo+name on mobile */}
      <div className="flex items-center gap-2 min-w-0 shrink-0">
        <Brain className="w-5 h-5 text-accent shrink-0" />
        <span className="font-semibold text-sm">NuroQ</span>
        <span className="hidden xl:inline text-xxs font-mono opacity-60 truncate">
          Frontier Neural Quant
        </span>
      </div>

      {/* Search — full button desktop, icon-only mobile */}
      <button
        onClick={onOpenPalette}
        className="hidden md:flex flex-1 max-w-md mx-auto items-center gap-2 px-3 py-1.5
                   rounded-md border border-zinc-300 dark:border-zinc-800
                   bg-zinc-50 dark:bg-zinc-900/60 text-xs
                   text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900
                   transition-colors"
      >
        <Search className="w-3.5 h-3.5" />
        <span className="flex-1 text-left">Search ticker or command…</span>
        <span className="kbd">⌘K</span>
      </button>
      <button
        onClick={onOpenPalette}
        className="md:hidden ml-auto p-2 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-900"
        aria-label="Search"
      >
        <Search className="w-4 h-4" />
      </button>

      {/* Status pills — full on desktop, dots-only on mobile */}
      <div className="flex items-center gap-1.5 shrink-0">
        <StatusPills />
      </div>
      <button
        onClick={() => qc.invalidateQueries()}
        className="btn btn-ghost !px-2 hidden sm:inline-flex"
        title="Refresh everything"
      >
        <RefreshCw className="w-3.5 h-3.5" />
      </button>
      <button
        onClick={() => { haptic.tap(); setDark((d) => !d); }}
        className="btn btn-ghost !px-2"
        title="Toggle theme"
      >
        {dark ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
      </button>
    </header>
  );
}
