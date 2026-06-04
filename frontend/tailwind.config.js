/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
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
        buy: { DEFAULT: "#10b981", 50: "#ecfdf5", 900: "#064e3b" },
        sell: { DEFAULT: "#ef4444", 50: "#fef2f2", 900: "#7f1d1d" },
        accent: { DEFAULT: "#14b8a6", 50: "#f0fdfa", 900: "#134e4a" },
      },
      // Type scale tuned for clarity (Google Finance-inspired). Each entry is
      // [size, line-height]. Bumped ~1-2px over Tailwind defaults and given
      // comfortable leading — the previous cramped 10-13px scale read as dense.
      // Overriding the scale here means every existing text-xs/text-sm/etc.
      // class across all views inherits the bump with zero per-component edits.
      fontSize: {
        xxs:  ["11px",  { lineHeight: "1.45" }],   // was 10px — micro labels, timestamps
        xs:   ["12.5px", { lineHeight: "1.5" }],   // was 12px — secondary text, table cells
        sm:   ["14px",  { lineHeight: "1.5" }],    // was 14px — body, primary labels
        base: ["15px",  { lineHeight: "1.55" }],   // was 16px
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
