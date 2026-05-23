import sys
import os
import asyncio

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dashboard import analyze_stock, analyst, logger
import pandas as pd
import os

async def run_test_scan():
    tickers = ["AAPL", "NVDA"]
    print(f"🚀 Starting test scan for: {tickers}")
    
    # Ensure analyst is loaded (Single mode for speed if not ensemble)
    if not hasattr(analyst, 'models') or not analyst.models:
        print("📥 Loading models...")
        analyst.load_all()

    results = []
    for ticker in tickers:
        print(f"\n🔍 Analyzing {ticker}...")
        try:
            # analyze_stock returns (output_rec, reasoning, history_df)
            output, reasoning, history = analyze_stock(ticker, is_auto=True)
            print(f"✅ {ticker} Analysis Complete.")
            print(f"--- {ticker} SUMMARY ---")
            print(output)
            results.append({"Ticker": ticker, "Result": "Success"})
        except Exception as e:
            print(f"❌ Error analyzing {ticker}: {e}")
            results.append({"Ticker": ticker, "Result": f"Error: {e}"})

    print("\n--- TEST SCAN COMPLETE ---")
    print(pd.DataFrame(results))

if __name__ == "__main__":
    # We need to run in an event loop because of the Telegram gatekeeper asyncio parts
    try:
        # Since analyze_stock might use asyncio.run_coroutine_threadsafe with gatekeeper.loop
        # we need to make sure the gatekeeper loop is running if ensemble/auto is used.
        # However, for a simple test, we might just want the analysis.
        asyncio.run(run_test_scan())
    except KeyboardInterrupt:
        pass
