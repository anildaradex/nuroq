import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { TopBar } from "./components/TopBar";
import { LeftRailNav, type ViewId } from "./components/LeftRailNav";
import { MobileNav } from "./components/MobileNav";
import { CommandPalette } from "./components/CommandPalette";
import { QuickTrade, type QuickTradePrefill } from "./components/QuickTrade";
import { OrderReviewModal, type OrderReview } from "./components/OrderReviewModal";
import { TodayView } from "./views/TodayView";
import { AnalyzeView } from "./views/AnalyzeView";
import { WatchlistView } from "./views/WatchlistView";
import { PortfolioView } from "./views/PortfolioView";
import { AgentView } from "./views/AgentView";
import { ScannerView } from "./views/ScannerView";
import { SignalsView } from "./views/SignalsView";
import { SystemView } from "./views/SystemView";
import { LogsView } from "./views/LogsView";
import { haptic, hideSplashWhenReady, onAppResume, syncStatusBar } from "./lib/native";

function getInitialView(): ViewId {
  const saved = localStorage.getItem("nuroq.view") as ViewId | null;
  return saved ?? "today";
}

export default function App() {
  const [view, setView] = useState<ViewId>(getInitialView());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [analyzeTicker, setAnalyzeTicker] = useState<string | undefined>();
  const [qtPrefill, setQtPrefill] = useState<QuickTradePrefill | undefined>();
  // Order review modal — opens when the user taps ⚡ on a Watchlist row.
  // Spacious surface for reviewing/editing a sized order before submission.
  // Coexists with QuickTrade (which stays for fast manual typing).
  const [orderReview, setOrderReview] = useState<OrderReview | null>(null);
  const qc = useQueryClient();

  useEffect(() => {
    localStorage.setItem("nuroq.view", view);
    haptic.tap();
  }, [view]);

  useEffect(() => {
    hideSplashWhenReady();
    syncStatusBar(document.documentElement.classList.contains("dark"));
    const unsub = onAppResume(() => qc.invalidateQueries());
    return unsub;
  }, [qc]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((p) => !p);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Drill-down: any table row → switch to Analyze with that ticker preloaded
  const drillIn = useCallback((ticker: string) => {
    setAnalyzeTicker(ticker.toUpperCase());
    setView("analyze");
  }, []);

  // Send-to-Quick-Trade: AnalyzeView → fills the bottom strip (kept for the
  // "I've already analyzed this and want to type in the strip" workflow).
  const sendToQT = useCallback((ticker: string, shares: number, sl: number, tp: number) => {
    setQtPrefill({ ticker, shares, sl, tp, _ts: Date.now() });
  }, []);

  // Open the spacious Order Review modal (Watchlist ⚡ → here). Gives full
  // breathing room to inspect cost / risk / reward / R:R before submitting,
  // and big touch-friendly Cancel + Submit buttons.
  const openOrderReview = useCallback((order: OrderReview) => {
    setOrderReview(order);
  }, []);

  return (
    <div className="flex flex-col" style={{ minHeight: "100dvh" }}>
      <TopBar onOpenPalette={() => setPaletteOpen(true)} />
      <div className="flex flex-1 min-h-0">
        <div className="hidden lg:block">
          <LeftRailNav active={view} onSelect={setView} />
        </div>
        <main
          className="flex-1 min-w-0 overflow-y-auto bg-zinc-50 dark:bg-zinc-950
                     px-3 py-3 lg:px-4
                     pb-[calc(env(safe-area-inset-bottom)+5rem)] lg:pb-3"
        >
          {view === "today"     && <TodayView />}
          {view === "analyze"   && <AnalyzeView initialTicker={analyzeTicker} onSendToQuickTrade={sendToQT} />}
          {view === "watchlist" && <WatchlistView onDrillIn={drillIn} onReviewOrder={openOrderReview} />}
          {view === "scanner"   && <ScannerView onDrillIn={drillIn} />}
          {view === "portfolio" && <PortfolioView onDrillIn={drillIn} />}
          {view === "agent"     && <AgentView onDrillIn={drillIn} />}
          {view === "signals"   && <SignalsView onDrillIn={drillIn} />}
          {view === "system"    && <SystemView />}
          {view === "logs"      && <LogsView />}
        </main>
      </div>
      <QuickTrade prefill={qtPrefill} />
      <OrderReviewModal
        order={orderReview}
        onClose={() => setOrderReview(null)}
      />
      <MobileNav active={view} onSelect={setView} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNavigate={(v) => { setView(v); setPaletteOpen(false); }}
        onAnalyze={(ticker) => { drillIn(ticker); setPaletteOpen(false); }}
      />
    </div>
  );
}
