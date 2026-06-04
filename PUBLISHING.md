# NuroQ — Publishing to TestFlight & the App Store

> **Two distribution paths**, both via your $99 Apple Developer Program account.
> Most personal/early use cases want path A (TestFlight). Path B is a bigger lift.
>
> **Last updated:** 2026-05-25

---

## Big picture — pick your path

| Path | Audience | Effort | Backend hosting | Apple Review |
|---|---|---|---|---|
| **A · TestFlight Internal** | You + up to 100 named teammates | ~30 min | Your Mac OK | None |
| **A · TestFlight External** | Up to 10,000 by email or public link | ~1 hour | Your Mac OK | Brief 24h review |
| **B · App Store** | Anyone on the App Store | Days | **Must be hosted (not your Mac)** | Multi-day full review |

**Honest take for NuroQ specifically:** start with **TestFlight Internal**. It's the right fit for a personal quant tool you want on your phone and maybe share with 2-3 friends. App Store distribution requires the backend to live on a real server (not your Mac), plus a privacy policy, plus Apple may scrutinize trading-related apps. We'll cover both, but Path A is what you almost certainly want first.

---

## Prereqs (one-time, ~20 min)

### 1. Apple Developer Program enrollment

You said you have an account — confirm it's the **paid $99/year Developer Program** (not just a free Apple ID). Check at https://developer.apple.com/account. The membership tier needs to say "Apple Developer Program".

### 2. Xcode + Mac setup

```bash
xcodebuild -version       # need 16.0+ (you have 16.4)
xcrun simctl list devices | head -5
```

### 3. Sign in to Xcode with your developer Apple ID

Xcode → Settings → Accounts → **+ Add Apple ID** → sign in. After it loads, you should see your Developer Program team(s) in the **Teams** list.

### 4. App identifier on developer.apple.com (optional — auto-managed signing creates this for you)

If you want explicit control:
- https://developer.apple.com/account/resources/identifiers/list → **+**
- Type: App IDs → App
- Bundle ID: `com.<yourname>.nuroq` (must be globally unique)
- Capabilities: leave everything off for now (no push, no Sign In with Apple, no iCloud needed yet)

For automatic signing (recommended for first-time): skip this. Xcode auto-registers the ID when you build.

---

## Path A · TestFlight (recommended first)

### Step 1 — Open the project + set your team

```bash
cd "/Users/anildara/Documents/Agent Driven Development/stock-ai-startup"
./scripts/ios.sh        # auto-detect LAN IP, build, sync, open Xcode
```

In Xcode:
1. Click **App** in the file tree → **Signing & Capabilities** tab
2. Check **"Automatically manage signing"**
3. **Team:** pick your Apple Developer Program team
4. **Bundle Identifier:** if `com.nuroq.app` errors, change to `com.<yourname>.nuroq`
5. Xcode should show a green check next to "Signing Certificate: Apple Development"

### Step 2 — Bump version + build numbers

In the **General** tab:
- **Version:** `1.0.0` (semantic for users)
- **Build:** `1` (must increment for every upload to App Store Connect, even if version stays the same)

### Step 3 — Create the app entry in App Store Connect

https://appstoreconnect.apple.com → **My Apps** → **+** → **New App**

Fill in:
- **Platform:** iOS
- **Name:** `NuroQ` (or your variant — must be App Store-unique)
- **Primary Language:** English (U.S.)
- **Bundle ID:** select the one Xcode registered (`com.<yourname>.nuroq`)
- **SKU:** internal identifier, e.g. `nuroq-v1`
- **User Access:** Full Access

Click **Create**.

### Step 4 — Archive the build

In Xcode top bar:
1. Click the **device dropdown** → choose **"Any iOS Device (arm64)"** (not a simulator)
2. **Product → Archive**
3. Wait 1-3 min while Xcode builds for release

The **Organizer** window opens automatically when done.

### Step 5 — Upload to App Store Connect

In Organizer (showing your fresh archive):
1. Click **Distribute App** (right side)
2. Choose **App Store Connect** → **Next**
3. **Upload** (not Export) → **Next**
4. Defaults are fine: ✓ Strip Swift symbols, ✓ Upload symbols, ✓ Manage version automatically
5. **Automatically manage signing** → **Next**
6. Review summary → **Upload**

Wait ~5 min. You'll get an email when "processed" in App Store Connect.

### Step 6 — Set up TestFlight Internal Testing

In App Store Connect → your app → **TestFlight** tab:
1. The new build should appear under **iOS Builds** (status: Processing → Ready). Allow ~10 min if it's still processing.
2. Click the build → **Add Build Information**:
   - **What to Test:** "Initial private beta. Paper-trading research tool. Backend runs on developer's local Mac — needs same WiFi."
   - **Test Information** → **Sign-In Information** (skip if not needed)
3. **Internal Testing** (left side) → **+** to add an internal group
4. Add yourself (and up to 99 others on your dev team) by Apple ID email
5. Internal testers get an email from TestFlight within minutes — they install the **TestFlight app** from the App Store, accept the invite, and tap **Install**.

**No Apple review needed for Internal Testing.** Builds expire 90 days after upload.

### Step 7 — (Optional) External Testing

If you want to share with people outside your dev team (up to 10,000):
1. TestFlight tab → **External Testing** → **+** to add a public group
2. Add testers by email, **or** enable the **public link**
3. Submit for Beta App Review — usually 24h
4. Once approved, share the link freely

---

## Path B · Full App Store distribution

Only do this when you actually want random strangers using the app. For NuroQ, that means:

**Required first:**

1. **Host the backend somewhere always-on.** Your Mac doesn't count. Options:
   - Cloud Mac mini (MacStadium ~$60/mo, Scaleway, AWS EC2 Mac M1 instance)
   - Replace MLX with a non-Apple-Silicon LLM and host on a Linux VM ($10-50/mo)
   - Skip the LLM entirely on the server (deterministic-only scoring) — backend runs anywhere
2. **Multi-user auth.** Currently the app is single-tenant. Add Sign In with Apple, JWT-based session, per-user portfolio/watchlist tables.
3. **Privacy Policy URL** (Apple requires). Pages it must cover: what data you collect, where it's stored, who it's shared with, how to delete it. Free hosting via Notion, GitHub Pages, or termly.io.
4. **Disclaimer in app + metadata** that it's an educational paper-trading tool, not financial advice, not a broker.
5. **Decide the App Store category** — `Finance` is risky (heightened review); `Productivity` or `Utilities` may be safer for a research tool.

**If those are addressed:**

### App Store metadata (required fields)

In App Store Connect → your app → **App Store** tab → **iOS App 1.0**:

- **Promotional Text** (170 chars): "AI-driven paper-trading research. Live agent reacts to bars and proposes trades for your approval."
- **Description** (~3000 chars): explain what NuroQ does, what it doesn't do, that it's paper-only via Alpaca
- **Keywords** (100 chars, comma-separated): `trading,quant,stocks,AI,research,paper trading,signals,watchlist`
- **Support URL:** GitHub repo URL or contact page
- **Marketing URL:** optional landing page
- **Privacy Policy URL:** **REQUIRED**

### Screenshots (required, multiple sizes)

Apple requires screenshots for at least:
- **6.7" iPhone** (iPhone 16 Pro Max simulator) — 1290×2796px
- **6.5" iPhone** (iPhone 11 Pro Max) — 1284×2778px
- Optional: 5.5" iPhone 8 Plus, 12.9" iPad

How to take them:
```bash
# In Xcode, run NuroQ on a simulator (Cmd+R with the right device picked)
# Then in the simulator: File → Save Screen → saves to ~/Desktop
# Take 3-10 representative shots (Today, Analyze, Watchlist, etc.)
```

Upload via App Store Connect → Media Manager.

### App Privacy "Nutrition Label"

Apple's privacy section. For NuroQ as currently built:
- **Data Linked to You:** None (no user accounts in v1)
- **Data Not Linked to You:** None
- **Data Not Collected:** check this if you genuinely don't collect any
- If you add analytics later, disclose them here

### App Icon

App Store requires a 1024×1024 PNG with no transparency, no rounded corners (Apple adds them). Already generated at `frontend/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png` from `nuroq_logo.png`. Verify it doesn't have alpha:
```bash
sips -g hasAlpha frontend/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png
# Should report "hasAlpha: no"
```
If it says yes, flatten:
```bash
sips -s format png --setProperty formatOptions normal nuroq_logo.png --out frontend/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png
```

### Age Rating

Walk through Apple's wizard. For a trading research tool: no objectionable content, **4+**.

### Submission

Once everything above is filled out:
1. Pick the build from TestFlight in the **Build** section
2. **Save** then **Add for Review**
3. Apple Review: usually 24-72 hours
4. Possible rejection reasons:
   - Backend connection fails (reviewer's Mac doesn't have your backend!) → **app must work standalone, or auth into a hosted backend**
   - Missing privacy policy
   - Crashes
   - Looks like a "broker" without licensing

---

## NuroQ-specific publishing gotchas

1. **Backend dependency is fatal for App Store.** During review, Apple's reviewer launches the app on their device. If the only backend is your Mac on your home network, the app appears broken to them → rejected. Either host the backend or include a clear demo mode that works without a backend.

2. **Telegram + Alpaca tokens.** Currently your `.env` has your personal API keys. If you publish, each user needs their own. Build a settings screen for API key entry; store in `@capacitor/preferences` (keychain-backed on iOS).

3. **MLX = Apple Silicon only.** If you ever host the backend, it must be on an M1/M2/M3 Mac (cloud Mac mini providers exist) OR strip the local LLM and use Anthropic/OpenAI API for the AI score.

4. **Free Apple ID signing** (no Developer Program) re-signs every 7 days — fine for personal use, useless for TestFlight/App Store. Confirm Developer Program enrollment.

5. **Bundle ID is permanent.** Once you ship `com.<yourname>.nuroq` to TestFlight, you can't change it without creating a separate app entry. Pick it deliberately.

6. **TestFlight builds expire 90 days from upload.** Re-upload a new build (build number bump) to extend.

7. **Version vs Build:**
   - **Version** (`CFBundleShortVersionString`): user-visible, e.g. `1.0.0`. Same for all builds of a release.
   - **Build** (`CFBundleVersion`): must increment every upload, even for the same version. `1`, `2`, `3`…

---

## Quick reference — common commands

```bash
# Build + sync + open Xcode
./scripts/ios.sh

# Just rebuild and re-sync (Xcode already open)
cd frontend
VITE_API_BASE="http://$(ifconfig | grep 'inet ' | grep -v 127.0.0.1 | awk '{print $2}' | head -1):8000" npm run build
npx cap sync ios

# Bump build number from CLI (Info.plist)
cd ios/App/App
agvtool next-version -all
# or set explicitly
agvtool new-version -all 7
```

---

## What to do next session

If you want to publish to TestFlight Internal now:
1. Bump version to `1.0.0`, build to `1` in Xcode → General
2. Product → Archive
3. Organizer → Distribute → App Store Connect → Upload
4. App Store Connect → Create app entry → TestFlight → Internal → invite yourself
5. Install TestFlight app on iPhone, accept invite, install NuroQ

If you want to move toward App Store, biggest blockers in order:
1. Move the backend off your Mac (cloud Mac mini or strip MLX)
2. Add per-user API key settings + secure storage
3. Write privacy policy + host it somewhere
4. Take screenshots from the simulator
5. Then submit for review
