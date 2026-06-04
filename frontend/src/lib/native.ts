// Capacitor bridge: detects when we're running inside the native iOS shell
// vs a regular browser, and exposes lightweight helpers that no-op cleanly
// on the web. The React app code never has to branch on platform — it just
// calls `haptic.tap()` and gets a real iOS tap on the phone, nothing in Chrome.

import { Capacitor } from "@capacitor/core";
import { Haptics, ImpactStyle, NotificationType } from "@capacitor/haptics";
import { StatusBar, Style } from "@capacitor/status-bar";
import { SplashScreen } from "@capacitor/splash-screen";
import { App } from "@capacitor/app";

export const isNative = Capacitor.isNativePlatform();
export const platform = Capacitor.getPlatform(); // "ios" | "android" | "web"

// ─── Haptics ───────────────────────────────────────────────────────────────
// Apple HIG: use sparingly. We use them on order submit, agent start/stop,
// and tab changes. NOT on every button click — that gets annoying fast.

export const haptic = {
  tap:    () => isNative && Haptics.impact({ style: ImpactStyle.Light }).catch(() => {}),
  medium: () => isNative && Haptics.impact({ style: ImpactStyle.Medium }).catch(() => {}),
  heavy:  () => isNative && Haptics.impact({ style: ImpactStyle.Heavy }).catch(() => {}),
  success: () => isNative && Haptics.notification({ type: NotificationType.Success }).catch(() => {}),
  warning: () => isNative && Haptics.notification({ type: NotificationType.Warning }).catch(() => {}),
  error:   () => isNative && Haptics.notification({ type: NotificationType.Error }).catch(() => {}),
};

// ─── Status bar theming ────────────────────────────────────────────────────
// Match the React theme to the iOS status bar (icons + tint).
// Style.Dark = light icons (for dark backgrounds), Style.Light = dark icons.

export async function syncStatusBar(isDark: boolean) {
  if (!isNative) return;
  try {
    await StatusBar.setStyle({ style: isDark ? Style.Dark : Style.Light });
    await StatusBar.setBackgroundColor({ color: isDark ? "#0a0a0a" : "#fafafa" });
  } catch {
    /* StatusBar not available on web or old Android */
  }
}

// ─── Splash screen ────────────────────────────────────────────────────────
// Capacitor auto-shows the splash; we hide it as soon as React mounts so the
// user doesn't see a stale logo when the app is already interactive.

export async function hideSplashWhenReady() {
  if (!isNative) return;
  try {
    await SplashScreen.hide({ fadeOutDuration: 200 });
  } catch {
    /* swallow */
  }
}

// ─── App lifecycle ─────────────────────────────────────────────────────────
// Fire a refresh callback whenever the app comes back to the foreground —
// stale watchlist / agent status / prices get re-fetched immediately.

export function onAppResume(cb: () => void) {
  if (!isNative) return () => {};
  const handle = App.addListener("appStateChange", ({ isActive }) => {
    if (isActive) cb();
  });
  return () => { handle.then(h => h.remove()); };
}
