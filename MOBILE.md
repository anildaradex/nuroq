# NuroQ — iOS / Mobile Guide

> How to develop, run, and ship the NuroQ iOS app via Capacitor 8.
> Same React codebase as web (`FRONTEND.md`). Backend unchanged (`backend/api.py`).
>
> **Last updated:** 2026-05-24

---

## What you have

A real native iOS app (`.app`, App Store-ready) that loads your React UI inside a WKWebView shell with native plugin access. Not a PWA — a proper iOS application.

**Native features wired in:**
- Splash screen (NuroQ logo on `#0a0a0a` background)
- App icon (regenerated from `nuroq_logo.png`)
- Status bar auto-themed to dark/light per UI
- Haptic feedback on tab change, theme toggle, order submit
- Auto-refresh data when app returns from background
- Full safe-area handling (notch + home indicator)

**Bundled but not yet exercised:**
- `@capacitor/push-notifications` — needs APNs key from developer.apple.com
- `@capacitor/preferences` — encrypted key/value (for biometric-gated settings)
- `@capacitor/app` — appState listeners (already used in `App.tsx`)

---

## Architecture

```
iPhone running NuroQ.app                Mac running FastAPI
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  WKWebView                  │         │  uvicorn :8000 (LAN-bound)   │
│  ├── React bundle (in app)  │  HTTPS  │  ├── /api/* JSON endpoints   │
│  ├── Capacitor bridge       │ ──────→ │  ├── /ws (future WebSocket)  │
│  ├── @capacitor/haptics     │  fetch  │  └── /              (SPA)    │
│  ├── @capacitor/status-bar  │         │                              │
│  └── … (6 plugins total)    │  CORS   │  Python compute (unchanged): │
│                             │  ←───── │  scoring · live_agent ·      │
│  Native iOS UI:             │  json   │  alpaca · MLX Gemma · etc.   │
│  ├── Splash screen          │         └──────────────────────────────┘
│  ├── Status bar             │
│  └── Home screen icon       │
└─────────────────────────────┘
```

**Key insight:** the backend runs on your Mac. Your iPhone needs to be on the same WiFi to reach `http://<mac-lan-ip>:8000`. For internet deployment, the backend would need to live on a cloud VM (any host that runs Python + MLX, so Apple Silicon Mac or remote Mac mini).

---

## Stack

| Layer | Choice |
|---|---|
| Native shell | Capacitor 8 (was Cordova's spiritual successor) |
| iOS deps | Swift Package Manager (NOT CocoaPods — Cap 8 dropped it) |
| Min iOS | 13.0 (default; iOS 14+ in practice for modern WKWebView) |
| Min Xcode | 16.x (we built with 16.4) |
| Plugins | `@capacitor/{app,haptics,preferences,push-notifications,splash-screen,status-bar}` |

---

## File layout

```
frontend/
├── capacitor.config.ts        # appId, two-mode dev/prod URL switch
├── src/lib/native.ts          # Capacitor bridge (no-ops on web)
└── ios/                       # Xcode project (committed)
    ├── App/
    │   ├── App.xcodeproj      # ← open this in Xcode
    │   ├── App/
    │   │   ├── AppDelegate.swift
    │   │   ├── Info.plist     # iOS app metadata
    │   │   ├── Assets.xcassets/
    │   │   │   ├── AppIcon.appiconset/AppIcon-512@2x.png    (1024×1024 NuroQ logo)
    │   │   │   └── Splash.imageset/splash-2732x2732*.png    (NuroQ logo centered on dark bg)
    │   │   ├── capacitor.config.json    # auto-synced from capacitor.config.ts
    │   │   └── public/        # built React bundle (auto-synced from frontend/dist/)
    │   └── CapApp-SPM/        # Swift Package Manager deps
    └── debug.xcconfig
```

**`ios/` should be committed to git.** Xcode-managed files inside change a lot, but the project structure is part of your app.

---

## One-command rebuild & push to phone

```bash
./scripts/ios.sh
```

What it does:
1. Detects your Mac's LAN IP (first non-loopback IPv4)
2. Builds React with `VITE_API_BASE=http://<lan-ip>:8000`
3. `npx cap sync ios` to copy bundle into Xcode project + update plugins
4. `npx cap open ios` to open Xcode

Then in Xcode: Cmd+R to install on your connected iPhone.

---

## First-time Xcode setup

(One-time, ~3 minutes.)

1. **Plug iPhone into Mac with USB cable.** Trust the computer on the phone if prompted.
2. In Xcode top bar, click the **device dropdown** (next to ▶ Run button) → pick **your iPhone** (not a simulator).
3. In Xcode file tree on the left, click **App** → top tab **"Signing & Capabilities"**:
   - **Team:** pick your Apple ID. If none listed, click *Add Account* — sign in with Apple ID. **Free, no $99 Developer Program needed** for personal sideload (re-install required every 7 days for free accounts; $99 lifts that).
   - **Bundle Identifier:** if Xcode complains `com.nuroq.app` is taken (it's a generic ID), change to `com.<yourname>.nuroq` and save.
4. Cmd+R → Xcode builds, installs, launches NuroQ on your iPhone.
5. **First launch only** — iOS will show "Untrusted Developer". Go to **Settings → General → VPN & Device Management → tap "Apple Development: <your email>" → Trust**.
6. Re-open NuroQ from your home screen.

You should see the NuroQ splash, then the Today view with live data from your Mac.

---

## Daily workflow

### Just change something and re-test on phone

```bash
./scripts/ios.sh
# then Cmd+R in Xcode
```

### Live HMR on phone (advanced — instant updates, no rebuild)

```bash
# 1. Make sure backend is running
NUROQ_BACKGROUND_SERVICES=0 ./.venv/bin/uvicorn backend.api:app --host 0.0.0.0 --port 8000

# 2. Run Vite dev server (will serve over LAN)
cd frontend && npm run dev -- --host 0.0.0.0

# 3. Sync iOS app with dev-mode URL
LAN_IP=$(ifconfig | grep "inet " | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
NUROQ_DEV_SERVER="http://$LAN_IP:5173" npx cap sync ios

# 4. Cmd+R in Xcode
# Now React/CSS changes on Mac hot-reload on phone instantly
```

### Switch back to bundled (offline) mode

```bash
unset NUROQ_DEV_SERVER
cd frontend && npm run build && npx cap sync ios
# Cmd+R in Xcode
```

---

## Adding a native capability

Capacitor 8 plugins are npm packages. Pattern:

```bash
# 1. Install
cd frontend && npm install @capacitor/<plugin-name>

# 2. Sync (registers plugin with Xcode Swift Package Manager)
npx cap sync ios

# 3. Import + use in React
```

Example — add Camera:
```bash
cd frontend && npm install @capacitor/camera
npx cap sync ios
```
```ts
// src/lib/native.ts (add to existing helpers)
import { Camera, CameraResultType } from "@capacitor/camera";

export async function snapPhoto() {
  if (!isNative) return null;
  const photo = await Camera.getPhoto({ resultType: CameraResultType.Uri });
  return photo.webPath;
}
```

Some plugins need an Info.plist permission (Camera needs `NSCameraUsageDescription`). The plugin docs say which. Add via Xcode → App → Info → Custom iOS Target Properties, or edit `ios/App/App/Info.plist`.

---

## Push notifications (next planned feature)

To replace/augment Telegram approvals with native push:

1. **Apple Developer setup** (free):
   - developer.apple.com → Certificates, Identifiers & Profiles → Keys → register an APNs Authentication Key
   - Download the `.p8` file (one-time)
   - Note your Key ID and Team ID

2. **Backend (`backend/api.py` + new `backend/push.py`):**
   - Add `POST /api/devices/register` — receives APNs token from app, stores in DB
   - On agent crossing fire in `live_agent.py`, post to `https://api.push.apple.com/3/device/<token>` with JWT signed by `.p8`

3. **Frontend (`src/lib/native.ts`):**
   ```ts
   import { PushNotifications } from "@capacitor/push-notifications";

   export async function registerForPush() {
     await PushNotifications.requestPermissions();
     await PushNotifications.register();
     PushNotifications.addListener("registration", async ({ value: token }) => {
       await fetch(API_BASE + "/api/devices/register",
         { method: "POST", body: JSON.stringify({ token }) });
     });
   }
   ```

4. **Xcode → Signing & Capabilities → + Capability → Push Notifications**

Estimated effort: ~1 session.

---

## Face ID gate on trades

```bash
cd frontend && npm install @capacitor-community/biometric-auth
npx cap sync ios
```
```ts
// src/lib/native.ts
import { BiometricAuth } from "@capacitor-community/biometric-auth";

export async function authBeforeTrade(): Promise<boolean> {
  if (!isNative) return true;
  try {
    await BiometricAuth.authenticate({
      reason: "Confirm trade with Face ID",
      cancelTitle: "Cancel",
    });
    return true;
  } catch {
    return false;
  }
}
```

Then in `QuickTrade.tsx` before `placeOrder`:
```ts
if (Number(shares) * estimatedPrice > 1000 && !(await authBeforeTrade())) {
  return;
}
```

`Info.plist` needs `NSFaceIDUsageDescription`.

---

## TestFlight distribution (for sharing with others)

Requires $99/year **Apple Developer Program** membership.

1. App Store Connect → My Apps → + → fill in metadata
2. Xcode → Product → Archive → Distribute → App Store Connect → Upload
3. App Store Connect → TestFlight → invite testers by email (or share a public link)
4. Testers install via TestFlight app on their phones (no App Store review needed)

Estimated effort once enrolled: ~30 minutes for first upload.

---

## App Store submission

Same as TestFlight, but after upload, submit for review:
- Privacy policy URL required
- App description, screenshots (multiple sizes)
- Review takes 1-3 days typically
- Annual fee $99

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| iPhone won't connect to backend | Same WiFi as Mac? `ifconfig en0 \| grep inet` to confirm LAN IP. macOS firewall may need to allow incoming on first attempt. |
| `npx cap sync ios` fails with "pod not found" | Don't need pod for Cap 8 — uses Swift Package Manager. If error persists, `brew install cocoapods` as fallback. |
| Xcode "No team selected" | Add Apple ID in Xcode → Settings → Accounts. Then re-pick in Signing & Capabilities. |
| iOS "Untrusted Developer" alert | Settings → General → VPN & Device Management → Trust the developer profile. |
| App installs but white screen | Check `VITE_API_BASE` in `frontend/.env.local` or pass via `scripts/ios.sh`. White screen usually means React loaded but API calls failed. |
| Status bar overlaps content | Verify `pt-safe` on top elements. Check `Info.plist` has `UIStatusBarStyle` if you want to override. |
| Haptics don't fire | Only work on real iPhone, not simulator. Confirm `isNative` is true. |
| Bundle re-built but phone shows old UI | `npx cap sync ios` then Cmd+R in Xcode. Bundle is copied into the app at sync time, not loaded over the network. |

---

## What's still pending (next-session backlog)

1. Wire `@capacitor/push-notifications` end-to-end with APNs key
2. Pull-to-refresh on Today / Watchlist / Portfolio
3. Swipe actions on Watchlist rows (send to QT / dismiss)
4. Face ID gate on trades > $X
5. Home screen widget showing today's P&L
6. TestFlight upload + share with anyone
7. Apple Watch companion (separate target in Xcode)

See `HANDOFF.md` for the full priority order.
