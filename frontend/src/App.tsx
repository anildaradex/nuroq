import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { TopBar } from "./components/TopBar";
import { LeftRailNav, type ViewId } from "./components/LeftRailNav";
import { MobileNav } from "./components/MobileNav";
import { CommandPalette } from "./components/CommandPalette";
import { QuickTrade, type QuickTradePrefill } from "./components/QuickTrade";
import { OrderReviewModal, type OrderReview } from "./components/OrderReviewModal";
import { LoginScreen, ChangePasswordPanel } from "./components/LoginScreen";
import { TodayView } from "./views/TodayView";
import { AnalyzeView } from "./views/AnalyzeView";
import { WatchlistView } from "./views/WatchlistView";
import { PortfolioView } from "./views/PortfolioView";
import { AgentView } from "./views/AgentView";
import { ScannerView } from "./views/ScannerView";
import { SignalsView } from "./views/SignalsView";
import { SystemView } from "./views/SystemView";
import { LogsView } from "./views/LogsView";
import { ConfigurationView } from "./views/ConfigurationView";
import { api, type AuthStatus } from "./lib/api";
import { haptic, hideSplashWhenReady, onAppResume, syncStatusBar } from "./lib/native";

function getInitialView(): ViewId {
  const saved = localStorage.getItem("nuroq.view") as ViewId | null;
  return saved ?? "today";
}

export default function App() {
  // Auth gate: poll /api/auth/status. While loading, render nothing (avoids a
  // flash of "Login" then the real UI). On `authenticated:false`, show the
  // login screen. Any /api/* call that 401s also invalidates this query so the
  // screen flips back to Login mid-session if the cookie expires.
  const qcAuth = useQueryClient();
  const auth = useQuery<AuthStatus>({
    queryKey: ["auth-status"],
    queryFn: api.authStatus,
    staleTime: 60_000,
    retry: false,
  });
  if (auth.isPending) return null;
  if (!auth.data?.authenticated) {
    return <LoginScreen onLoggedIn={() => qcAuth.invalidateQueries({ queryKey: ["auth-status"] })} />;
  }
  return <AuthenticatedApp mustChangePassword={!!auth.data.must_change_password} />;
}

function AuthenticatedApp({ mustChangePassword }: { mustChangePassword: boolean }) {
  const [view, setView] = useState<ViewId>(getInitialView());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [analyzeTicker, setAnalyzeTicker] = useState<string | undefined>();
  const [qtPrefill, setQtPrefill] = useState<QuickTradePrefill | undefined>();
  // Auto-show the change-password modal while the seeded "nuroq" still works.
  // Dismissible (in case the user wants to look around first); reappears next
  // session because `must_change_password` will still be true.
  const [showChangePw, setShowChangePw] = useState(mustChangePassword);
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
          {view === "config"    && <ConfigurationView />}
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
      {showChangePw && (
        <div className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm flex items-center justify-center px-4">
          <div className="relative">
            <ChangePasswordPanel onDone={() => {
              setShowChangePw(false);
              // Re-query so the banner doesn't reappear after navigation
              qc.invalidateQueries({ queryKey: ["auth-status"] });
            }} />
            <button
              onClick={() => setShowChangePw(false)}
              className="absolute -top-2 -right-2 btn btn-ghost !p-1 !min-h-0 text-xs"
              title="Dismiss"
            >✕</button>
          </div>
        </div>
      )}
    </div>
  );
}
