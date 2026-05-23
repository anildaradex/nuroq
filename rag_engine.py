import os
import requests
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from edgar import set_identity, get_filings
from sentence_transformers import SentenceTransformer
from typing import List, Dict

# Load .env so POLYGON_API_KEY is available when run standalone
load_dotenv()

# SEC Identity Requirement
set_identity("Anil Dara anil@example.com")

class RAGEngine:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        print(f"🧠 Initializing RAG Model ({model_name})...")
        self.model = SentenceTransformer(model_name)
        self.polygon_api_key = os.getenv("POLYGON_API_KEY")

    def get_sec_filings(self, ticker: str) -> str:
        """Fetches Risk Factors and MD&A from the latest 10-K/Q."""
        try:
            from edgar import Company
            c = Company(ticker)
            filings = c.get_filings()
            latest = filings.filter(form=["10-K", "10-Q"]).latest()
            if not latest:
                return ""

            doc = latest.obj()
            content = []

            for section in ["Item 1A", "Item 7", "Item 2"]:
                try:
                    text = doc[section]
                    if text:
                        content.append(f"--- SEC {section} ---\n{text[:5000]}")
                except:
                    continue

            return "\n".join(content)
        except Exception as e:
            print(f"⚠️ SEC Fetch Error for {ticker}: {e}")
            return ""

    def get_news(self, ticker: str) -> str:
        """Fetches latest news from Polygon."""
        url = f"https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=5&apiKey={self.polygon_api_key}"
        try:
            resp = requests.get(url, timeout=10).json()
            results = resp.get("results", [])
            news_text = [f"News: {n['title']} - {n.get('description', '')}" for n in results]
            return "\n".join(news_text)
        except:
            return ""

    def chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """Simple character-based chunking."""
        if not text:
            return []
        return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

    def get_grounded_context(self, ticker: str, query: str = "financial risks and outlook") -> str:
        """Retrieves the most relevant snippets for the AI analyst."""
        # 1. Fetch
        sec_text = self.get_sec_filings(ticker)
        news_text = self.get_news(ticker)
        full_text = sec_text + "\n" + news_text

        if not full_text.strip():
            return "No additional grounded context available."

        # 2. Chunk
        chunks = self.chunk_text(full_text)
        if not chunks:
            return ""

        # 3. Embed & Rank
        query_emb = self.model.encode([query])[0]
        chunk_embs = self.model.encode(chunks)

        # Simple Cosine Similarity
        norms = np.linalg.norm(chunk_embs, axis=1) * np.linalg.norm(query_emb)
        # Guard against zero-norm chunks
        norms = np.where(norms == 0, 1e-9, norms)
        scores = np.dot(chunk_embs, query_emb) / norms
        top_indices = np.argsort(scores)[::-1][:3]  # Top 3 snippets

        context = "\n".join([chunks[i] for i in top_indices])
        return context

if __name__ == "__main__":
    engine = RAGEngine()
    print("Testing RAG for NVDA...")
    ctx = engine.get_grounded_context("NVDA")
    print(f"\nRetrieved Context (Snippet):\n{ctx[:500]}...")
