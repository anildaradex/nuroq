# System Architecture: AntiGravity Local Quant Agent

## Overview
AntiGravity is an autonomous trading system that combines local LLM reasoning (Dual-Model Ensemble) with objective quantitative metrics.

## Data Flow Diagram
```mermaid
graph TD
    A[MarketStreamer] -->|Price Volatility| B(AgentLoop)
    C[Polygon API] -->|Bulk Daily Aggs| B
    D[yfinance] -->|Fundamentals/News| B
    B -->|Build Prompt| E[EnsembleAnalyst]
    
    subgraph "Local Intelligence (M3 Pro)"
        E -->|Inference| F[DeepSeek-R1 7B]
        E -->|Inference| G[Mistral 7B]
        F & G -->|Consensus| H{Consensus Check}
    end
    
    H -->|Match| I[Trade Signal]
    H -->|No Match| J[HOLD / Skip]
    
    I --> K[Telegram Gatekeeper]
    K -->|User Approved| L[ShadowExecutor]
    L -->|Log| M[(shadow_trades.csv)]
    L -->|Update| N[(portfolio.csv)]
```

## Component Definitions
- **MarketStreamer:** Background thread polling for 2% price moves across 100+ tickers.
- **EnsembleAnalyst:** Orchestrates two 4-bit quantized models via `mlx-lm`.
- **AgentLoop:** The "Silent Operator" that cycles every 4 hours to find new setups.
- **TradeGatekeeper:** Telegram-based Human-in-the-loop (HITL) approval system.
