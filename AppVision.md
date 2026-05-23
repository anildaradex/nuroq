AppVision
Project Blueprint: AntiGravity Local Quant Agent
1. Core Objective
Transform the existing Ensemble AI Dashboard into a fully autonomous local trading agent. AntiGravity will utilize locally-downloaded DeepSeek-R1 and Mistral models to scan the market, perform RAG-based analysis, and execute shadow-to-live trades via Alpaca.

2. Technical Infrastructure & Integrations
Intelligence: Local MLX-LM framework running DeepSeek (Logic) and Mistral (Consensus).

Market Data (Polygon.io): Primary source for daily aggregates, OHLCV history, and real-time news headlines used in technical calculations.

Fundamentals (yfinance): Secondary source for key ratios (P/E, forward P/E, Market Cap) and growth metrics extracted from SEC 10-Q/K filings.

Research (RAG): SentenceTransformer (all-MiniLM-L6-v2) embedding SEC filings via edgartools and Polygon/yfinance news for grounded reasoning.

Brokerage: Alpaca Markets used for trade execution and portfolio management.

Human-in-the-loop: Telegram Gatekeeper for real-time trade approvals via your phone.

3. Data Flow & Missing Piece Formalization
To move from a manual dashboard to an Autonomous Agent, the following data loops must be finalized:

A. The "Hybrid" Research Engine
Polygon Integration: Used to trigger the deep_market_scan by filtering the top 100 movers based on volume and 2-day momentum.

yfinance Integration: Once a ticker is flagged, get_fundamentals pulls the valuation layer to ensure the AI isn't buying overextended "hype" stocks.

Requirement: Ensure these two sources are merged into the final prompt for the EnsembleAnalyst.

B. Autonomous Triggers (Headless Mode)
Current State: Triggered manually via Gradio.

Requirement: Implement a background thread in dashboard.py to run the scan cycle:

Polygon Fetch: Pull market aggregates for the day.

yfinance Filter: Enrich candidates with valuation data.

RAG Analysis: Pass movers through rag_engine.py for context.

Consensus: Execute get_consensus to confirm the trade.

C. Enhanced Signal Logic
Scoring: Only "BUY" signals with Score >= 80 and Consensus = True trigger the Telegram Gatekeeper.

Safety: Explicitly restricted to Stocks Only—no options API calls allowed.

4. Execution Flow (The "Silent Operator")
Ingestion: RAGEngine pulls 10-K/Q and Polygon/yfinance news.

Analysis: EnsembleAnalyst runs the "Dual-Model Consensus".

Gatekeeping: Agent sends a Telegram Inline Keyboard for approval.

Logging: Upon approval, ShadowExecutor logs the trade to your local CSV/ledger.

5. Initial Instruction for AntiGravity
"AntiGravity, assume control of the dashboard.py and rag_engine.py environment. Your mission is to automate the daily scan. Use Polygon.io for initial volume/momentum filtering and yfinance for fundamental valuation checks. Initialize by verifying the MLX models and the Telegram Gatekeeper. Provide a RAG-grounded report for any stock with a Conviction Score exceeding 75. Do not execute trades without the Telegram 'EXECUTE' callback."