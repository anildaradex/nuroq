import type { CapacitorConfig } from "@capacitor/cli";

// Two-mode setup:
//   Production build → the app loads the bundled dist/ (offline-capable).
//   Dev mode (NUROQ_DEV_SERVER set) → the app loads from your Mac's LAN IP,
//     so iOS gets live HMR from Vite without rebuilding the .app every change.
//
// To switch to dev mode before opening Xcode:
//   NUROQ_DEV_SERVER=http://192.168.4.168:5173 npx cap sync ios
// To go back to bundled:
//   unset NUROQ_DEV_SERVER && npm run build && npx cap sync ios

const devServer = process.env.NUROQ_DEV_SERVER;

const config: CapacitorConfig = {
  appId: "com.nuroq.app",
  appName: "NuroQ",
  webDir: "dist",
  // App fills the whole screen (under the notch) — our React app already
  // respects env(safe-area-inset-*).
  ios: {
    contentInset: "never",
    backgroundColor: "#0a0a0a",
    // Don't let WebView's bouncy overscroll yank the UI around.
    scrollEnabled: true,
  },
  server: devServer
    ? {
        url: devServer,
        cleartext: true, // dev only — http (not https) on LAN
      }
    : undefined,
  plugins: {
    SplashScreen: {
      launchShowDuration: 600,
      backgroundColor: "#0a0a0a",
      androidScaleType: "CENTER_CROP",
      splashFullScreen: true,
      splashImmersive: true,
    },
    StatusBar: {
      style: "DARK", // dark mode = light icons; flips automatically in our boot code
      backgroundColor: "#0a0a0a",
      overlaysWebView: true,
    },
  },
};

export default config;
