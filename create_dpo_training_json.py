import requests
import json
import time
import random
import yfinance as yf
from datetime import datetime, timedelta
from collections import defaultdict
import concurrent.futures

# --- CONFIGURATION ---
API_KEY = "YClbpkzp3UYeUUyTEEjUBiAevqCe_Z6H"
DPO_FILE = "dpo_train.jsonl"
MIN_PRICE = 5.0
MIN_VOLUME = 1000000 

def fetch_market_snapshot(date_str):
    """Fetches market-wide snapshot for a specific date."""
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_str}?adjusted=true&apiKey={API_KEY}"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json().get("results", [])
    except Exception as e:
        print(f"Error fetching {date_str}: {e}")
    return []

def get_trading_days(n_ago):
    """Returns the date N trading days ago."""
    d = datetime.now()
    count = 0
    while count < n_ago:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d.strftime('%Y-%m-%d')

def generate_reasoning(ticker, techs, score, rating):
    """Generates a synthetic reasoning string for a given score/rating."""
    steps = [
        f"Analyzing {ticker} at ${techs['price']}. Trend is {techs['trend']}.",
        f"RSI is {techs['rsi']}, indicating {'overbought' if techs['rsi']>70 else ('oversold' if techs['rsi']<30 else 'neutral')} levels.",
        f"Relative Volume is {techs['rel_vol']}x."
    ]
    return f"REASONING:\n" + "\n".join(steps) + f"\n\nFINAL RATING: {rating}, FINAL SCORE: {score}"

def main():
    # 1. Define Dates: 25 days ago (Prompt) -> 5 days ago (Outcome) = 20-day window
    prompt_date = get_trading_days(25)
    outcome_date = get_trading_days(5)
    
    print(f"📡 Fetching DPO Snapshots: Prompt({prompt_date}) and Outcome({outcome_date})")
    
    prompt_results = fetch_market_snapshot(prompt_date)
    time.sleep(1)
    outcome_results = fetch_market_snapshot(outcome_date)
    
    outcome_map = {item['T']: item['c'] for item in outcome_results}
    
    dpo_samples = []
    
    # Process top liquid stocks from prompt date
    prompt_results.sort(key=lambda x: x.get('v', 0) * x.get('c', 0), reverse=True)
    
    for item in prompt_results[:300]: # Top 300 for quality
        ticker = item['T']
        if ticker not in outcome_map: continue
        
        price_start = item['c']
        price_end = outcome_map[ticker]
        perf = (price_end - price_start) / price_start
        
        # Mock techs for the prompt date
        techs = {
            "price": price_start,
            "rsi": random.uniform(30, 70), # In a real scenario, we'd calculate these properly
            "rel_vol": round(item['v'] / 1000000, 2),
            "trend": "UP" if random.random() > 0.5 else "DOWN"
        }
        
        prompt_str = f"Ticker: {ticker}, Close: ${price_start}, RSI: {techs['rsi']}, RelVol: {techs['rel_vol']}x"
        
        # DPO Logic: Preference based on actual performance
        if perf > 0.05: # Winner
            chosen = generate_reasoning(ticker, techs, random.randint(75, 95), "BUY")
            rejected = generate_reasoning(ticker, techs, random.randint(10, 40), "SELL")
        elif perf < -0.05: # Loser
            chosen = generate_reasoning(ticker, techs, random.randint(10, 40), "SELL")
            rejected = generate_reasoning(ticker, techs, random.randint(75, 95), "BUY")
        else:
            continue # Skip neutral for stronger DPO signal
            
        dpo_samples.append({
            "prompt": prompt_str,
            "chosen": chosen,
            "rejected": rejected
        })

    with open(DPO_FILE, "w") as f:
        for s in dpo_samples:
            f.write(json.dumps(s) + "\n")
            
    print(f"✅ Created {len(dpo_samples)} DPO preference pairs in {DPO_FILE}")

if __name__ == "__main__":
    main()