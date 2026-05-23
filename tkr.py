import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("POLYGON_API_KEY")
TICKER = "NVDA"

# Fetching the previous day's close
url = f"https://api.polygon.io/v2/aggs/ticker/{TICKER}/prev?adjusted=true&apiKey={API_KEY}"

response = requests.get(url, timeout=10)
if response.status_code == 200:
    data = response.json()
    print(f"Data for {TICKER}:", data['results'][0])
else:
    print("Error:", response.status_code)
