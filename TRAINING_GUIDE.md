# Stock AI Startup: Model Fine-Tuning & Training Guide

This guide documents the local model fine-tuning architecture of the Stock AI Startup project (Nuroq). It explains the purpose of the models, when and how to train them, and best practices for future AI agents to follow.

---

## 📖 Executive Summary & Core Philosophy

### 1. Do we actually need to train and fine-tune all these models?
**No, fine-tuning is not strictly necessary for the system to execute trades.** 
* **Deterministic Logic:** The quantitative math (calculating trend, SMA20, RSI, Bollinger Bands, and P/E ratios) is done entirely via standard Python in `scoring.py`.
* **The Role of Fine-Tuning:** The models are fine-tuned **not** to "learn math" (which they are bad at), but to **mimic the structured formatting, logical reasoning steps, and strict JSON output schemas** of a professional Hedge Fund Analyst.
* **Out-of-the-Box Models:** In `dashboard.py`, we include `gemma-3-4b-it` under `MODELS_CFG` with `adapter: None`. This proves that highly capable zero-shot/few-shot models can perform analysis without *any* fine-tuning, as long as they are provided with rich context and robust system instructions.
* **Why fine-tune at all?** Fine-tuning bakes your specific logical flow and structured JSON schema directly into the model's weights. This allows us to use **shorter system prompts**, which dramatically reduces latency (token processing time) on local Apple Silicon hardware.

---

## 🛠️ The 3 Models Explained

The application's `MODELS_CFG` in `dashboard.py` is configured around three local Apple Silicon-compatible models:

| Model Identifier | Base Model Name | Fine-Tuning Adapter Path | Core Purpose |
| :--- | :--- | :--- | :--- |
| **`deepseek`** | `mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit` | `./adapters_deepseek` | Performs deep, logical chain-of-thought financial reasoning. |
| **`mistral`** | `mlx-community/Mistral-7B-Instruct-v0.3-4bit` | `./adapters` | Acts as the secondary ensemble evaluator for consensus checking. |
| **`gemma`** | `mlx-community/gemma-3-4b-it-4bit` | *None (Zero-Shot)* | Lightweight, fine-tuning-free alternative for fast, general analysis. |

---

## ⏱️ Training Frequencies: When to Train?

Fine-tuning should be done selectively based on the type of training:

### A. Supervised Fine-Tuning (SFT) — *Almost Never*
* **Purpose:** Teaches the model *how* to write (formatting JSON, tone, structure).
* **When to run:** Only if you make structural changes to your input prompt formats, change the target JSON structure, or decide to add a brand-new quantitative indicator to the prompt.
* **Why:** Once the model understands how to output the correct JSON, training it on more of the same data adds zero value.

### B. Direct Preference Optimization (DPO) — *Monthly or Quarterly*
* **Purpose:** Reinforcement learning based on **actual trading performance**. It teaches the model *what* constitutes a good vs. bad trade.
* **When to run:** Run DPO periodically (e.g., every 30 days) using historical simulated trade data.
* **How it works:** It maps a prompt to a **Chosen** response (representing a trade that proved highly profitable after 20 days) and a **Rejected** response (representing a trade that lost money or hit a stop loss).
* **Benefit:** Aligns the model's subjective reasoning with actual market-winning setups.

### C. Real-Time Price Action & News — *Continuous (0 retrains)*
* **Important:** You do **not** need to retrain models to update them on today's stock prices or news!
* **Context Injection:** `dashboard.py` dynamically fetches live market aggregated data (via Polygon.io) and news/sentiment (via yfinance) and injects them straight into the model's prompt in real-time. This is called **In-Context Learning** and requires no training whatsoever.

---

## 🚀 Step-by-Step Training Execution

For agents or developers executing the training pipeline on macOS Apple Silicon:

### Phase 1: Generate Data
Run the SFT data generator. This fetches liquid stock symbols from Polygon, grabs their fundamentals using `yfinance` in parallel, and structures them into `train.jsonl` and `valid.jsonl` using the deterministic Python scoring engine:
```bash
python create_training_json.py
```

### Phase 2: SFT Fine-Tuning (via MLX)
Run the master training orchestrator. This triggers the SFT fine-tuning for both Mistral and DeepSeek-R1 models utilizing Apple's Metal Performance Shaders (MPS):
```bash
python train_models.py
```
*Behind the scenes, this executes:*
```bash
python -m mlx_lm.lora --model mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit --train --data . --iters 200 --batch-size 1 --num-layers 16 --learning-rate 1e-5 --adapter-path adapters_deepseek
```

### Phase 3: DPO Fine-Tuning (Preference Tuning)
1. Generate the preference dataset (mapping a prompt to a winning outcome vs. losing outcome based on a 20-day price window):
   ```bash
   python create_dpo_training_json.py
   ```
2. Run the DPO training script to fine-tune the model against these preference pairs:
   ```bash
   python train_dpo_deepseek.py
   ```

---

## 📈 Methods for Maximizing Efficiency and Performance

To make the local training and inference pipeline faster, cleaner, and more profitable, adopt these methods:

### 1. Unified Single-Model Engine (Recommended over Ensemble)
* **Problem:** Loading both DeepSeek and Mistral simultaneously takes significant memory (~10GB+ VRAM) and performs sequential CPU/GPU evaluations, making scanning slow.
* **Solution:** Standardize on a single, powerful 7B or 8B model (like `DeepSeek-R1-Distill-Qwen-7B-4bit` or `Llama-3-8B-Instruct`) and bypass the ensemble. A single well-fine-tuned model is faster and uses half the memory.

### 2. Reinforcement Learning from Financial Feedback (RLFF)
* **Mechanism:** Feed actual results from `nuroq.db` (your `shadow_trades` and `portfolio` tables) directly into `create_dpo_training_json.py` instead of relying on synthetic random DPO mock outcomes.
* **Why it's better:** The model learns directly from your specific portfolio's performance, correcting its own biases over time.

### 3. Prompt Caching
* **Mechanism:** MLX supports prompt caching. Ensure the system instructions and memory context remain static across evaluations.
* **Why it's better:** Bypasses processing hundreds of tokens on every single ticker scan, speeding up inference by up to **4x**.

### 4. Hybrid Quant-Score Safety Nets
* **Mechanism:** Maintain `calculate_quant_score` in `scoring.py`. 
* **Why it's better:** If the LLM has a "hallucination" or an overly bullish bias, the fast Python-based quant filters (e.g. downgrading BUYs to HOLDs if the overall technical score is under 75) act as a secure, hard-coded risk gatekeeper before any trade is executed.

---

## 🤖 Instructions for AI Agents
When performing tasks in this codebase:
1. **Always preserve `.gitignore` entries:** Never commit `.env`, `*.db`, `*.log`, or `.gradio/`.
2. **Verify weights before pushing modifications:** If modifying model parameters, run the local unit tests via `python master_test_suite.py` to ensure that model inference parses JSON output correctly.
3. **Keep prompts strict:** When editing prompts, always mandate valid JSON output wrapped with strict delimiters to prevent the models from outputting trailing conversational text.
