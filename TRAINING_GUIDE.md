# NuroQ Training Guide — Gemma DPO (RLFF)

This project trains a **single LoRA adapter on Gemma using Direct Preference
Optimization** with preference pairs derived from real forward returns and the
live signal log. No SFT step. No Mistral/DeepSeek pipelines.

---

## What the training does (and what it doesn't)

DPO does **not** teach the model technical analysis — `scoring.py` already
computes RSI, Bollinger Bands, ATR, etc. deterministically. DPO teaches Gemma:

1. **Output shape**: emit the JSON schema `dashboard.get_structured_data` parses.
2. **Direction alignment**: rate setups that historically produced +5% over the
   next 20 trading days as BUY/high-score, and setups that lost ≥5% as
   SELL/low-score.

The model is the "writer" + "directional voter"; the deterministic Python in
`scoring.py` remains the quant safety net.

---

## Pipeline overview

```
nuroq.db  (price_history + all_signals)
   │
   ▼
create_dpo_training_json.py      ──►  dpo_data/{train,valid}.jsonl
   │                                  (prompt, chosen, rejected) triples
   ▼
train_dpo.py  (uses mlx-lm-lora)  ──►  adapters_gemma_dpo/
   │
   ▼
dashboard.py  loads adapter via MODELS_CFG['gemma']['adapter']
```

---

## Step 1 — Generate preference pairs

The generator pulls bars from `nuroq.db.price_history`, computes real technicals
via `scoring.calculate_technicals` on historical slices, and labels each as-of
date by its **20-day forward return**. Optionally augments with RLFF pairs from
the `all_signals` table once those signals are old enough to have a realized
outcome.

```bash
python create_dpo_training_json.py
```

Useful flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--horizon` | `20` | Forward-return window in trading days |
| `--threshold` | `0.05` | `|return|` above this counts as a clear winner/loser |
| `--max-per-ticker` | `20` | Caps pairs/ticker so no single name dominates |
| `--rlff-only` | off | Use only logged signals (small, may have no BUYs) |
| `--no-rlff` | off | Skip RLFF augmentation; historical OHLCV only |
| `--valid-frac` | `0.10` | Ticker-level holdout (no same-ticker leakage) |

Output: `dpo_data/train.jsonl`, `dpo_data/valid.jsonl`. The validation split is
held out **at the ticker level** so a name in `valid.jsonl` never appears in
`train.jsonl` — eval loss reflects real generalization, not memorization.

> Prerequisite: the dashboard must have been run at least once so
> `price_history` is populated. With ~300 tickers and ~5 months of bars you'll
> get 5–7k preference pairs.

---

## Step 2 — Train the adapter

```bash
python train_dpo.py
```

Defaults: Gemma 3 4B (4-bit) base, 400 iters, batch=2, 16 LoRA layers, lr=5e-7,
DPO loss with `--beta 0.1`, `--dpo-cpo-loss-type sigmoid`. Adapter saved to
`adapters_gemma_dpo/`.

Tune via flags: `--iters`, `--batch-size`, `--num-layers`, `--learning-rate`,
`--beta`, `--loss-type {sigmoid,hinge,ipo,dpop}`, `--adapter-path`.

The underlying command this wraps is:

```bash
python -m mlx_lm_lora.train \
  --model mlx-community/gemma-3-4b-it-4bit \
  --train --train-mode dpo \
  --data dpo_data \
  --beta 0.1 --dpo-cpo-loss-type sigmoid \
  --iters 400 --batch-size 2 --num-layers 16 --learning-rate 5e-7 \
  --adapter-path adapters_gemma_dpo --save-every 100
```

---

## Step 3 — Wire the adapter into the dashboard

In `dashboard.py`, set the adapter on Gemma:

```python
MODELS_CFG = {
    "gemma": {
        "path":    os.path.expanduser("~/.cache/huggingface/hub/.../snapshots/..."),
        "adapter": "adapters_gemma_dpo",
    }
}
```

Restart the dashboard. The adapter loads via `mlx_lm.load(path, adapter_path=...)`.

---

## How often to retrain

DPO is cheap relative to SFT — a few hundred iters on 4B params at batch=2
takes ~10–20 minutes on M-series silicon. Re-run when:

- `all_signals` has accumulated meaningfully more matured signals (every few weeks).
- The inference-time prompt format in `dashboard.py` changes (the generator
  uses the inference template, so prompts must stay in sync).
- Quant logic in `scoring.py` changes the technical fields exposed to the prompt
  (`trend`, `semantic_rsi`, `semantic_bb`).

You do **not** need to retrain to pick up new prices or news — those are
injected at inference time via the existing context fetchers.

---

## Notes & limitations

- **Prompt parity is load-bearing.** `create_dpo_training_json.build_prompt`
  must mirror `dashboard.analyze_single_ticker_data`. If you change one, change
  both, or DPO will train on prompts the model never sees at inference.
- **Fundamentals are not time-travelled.** Training prompts use `N/A` for
  PE/growth/news/memory because yfinance only returns current values. The
  adapter therefore learns to lean on the technical fields. This is acceptable
  — `scoring.calculate_quant_score` is the layer that consumes fundamentals.
- **RLFF pairs require matured signals.** A signal logged today doesn't produce
  a preference pair until the horizon has elapsed and price_history covers it.
- **Ticker-level holdout** prevents the most common leakage failure mode (the
  same name in train and valid). Random-row holdout would mask overfitting.
