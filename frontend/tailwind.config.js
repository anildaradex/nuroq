/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        // Native system-UI stack — SF Pro on Apple, Segoe UI on Windows,
        // Roboto on Android. Same approach WhatsApp uses: the OS's own UI
        // font renders crisp at small sizes and needs no webfont download.
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "SF Pro Text",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      colors: {
        // "Greenback" palette — deep US-dollar money-green. `accent` is the
        // brand/primary (buttons, focus, logo); `buy` is a hair brighter for
        // gains/up. Both pass WCAG AA: white text on the solid, and the solid
        // as text on white. Tints (50/100) for soft backgrounds and washes.
        buy: {
          DEFAULT: "#1a8348",
          50: "#e8f5ed",
          100: "#c6e7d1",
          600: "#156b3a",
          700: "#11542d",
          900: "#0a341c",
        },
        sell: { DEFAULT: "#ef4444", 50: "#fef2f2", 900: "#7f1d1d" },
        accent: {
          DEFAULT: "#147a45",
          50: "#e7f3ec",
          100: "#c4e3cf",
          600: "#10633a",
          700: "#0c4b2c",
          900: "#07311c",
        },
      },
      // Type scale tuned for clarity (Google Finance-inspired). Each entry is
      // [size, line-height]. Bumped ~1-2px over Tailwind defaults and given
      // comfortable leading — the previous cramped 10-13px scale read as dense.
      // Overriding the scale here means every existing text-xs/text-sm/etc.
      // class across all views inherits the bump with zero per-component edits.
      fontSize: {
        xxs:  ["11.5px", { lineHeight: "1.4" }],   // micro labels, timestamps
        xs:   ["13px",  { lineHeight: "1.5" }],    // secondary text, table cells
        sm:   ["14.5px", { lineHeight: "1.5" }],   // body, primary labels (WhatsApp ~14.2)
        base: ["15.5px", { lineHeight: "1.55" }],
        lg:   ["17px",  { lineHeight: "1.5" }],    // section headers
        xl:   ["19px",  { lineHeight: "1.4" }],
        "2xl":["24px",  { lineHeight: "1.3" }],    // big numbers
        "3xl":["30px",  { lineHeight: "1.2" }],
        "4xl":["37px",  { lineHeight: "1.1" }],    // hero P&L
        "5xl":["46px",  { lineHeight: "1.05" }],
        "6xl":["58px",  { lineHeight: "1" }],
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
    },
  },
  plugins: [],
};
