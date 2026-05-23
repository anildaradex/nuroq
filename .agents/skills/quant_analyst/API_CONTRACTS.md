# API Contracts & Data Structures

This file provides "Grounding Data" to prevent agents from hallucinating JSON structures.

## 1. Polygon.io (Aggregates)
**Endpoint:** `/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}`
**Expected Structure:**
```json
{
  "ticker": "AAPL",
  "results": [
    {
      "v": 54123456,  // Volume
      "o": 150.25,    // Open
      "c": 153.12,    // Close
      "h": 154.00,    // High
      "l": 149.50,    // Low
      "t": 1672531200000 // Timestamp
    }
  ]
}
```

## 2. yfinance (Ticker Info)
**Call:** `yf.Ticker("AAPL").info`
**Key Fields:**
- `trailingPE`: float (e.g., 28.5)
- `forwardPE`: float
- `revenueGrowth`: float (0.15 = 15%)
- `longName`: string
- `industry`: string

## 3. Telegram (Inline Keyboard)
**Payload for Approval:**
```json
{
  "text": "🚀 AI TRADE SIGNAL: AAPL...",
  "reply_markup": {
    "inline_keyboard": [
      [
        {"text": "✅ EXECUTE", "callback_data": "EXECUTE"},
        {"text": "❌ CANCEL", "callback_data": "CANCEL"}
      ]
    ]
  }
}
```
