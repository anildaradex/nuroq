"""backtest — historical replay harness for the NuroQ day-trader.

  fill_model.py — slippage + commission simulation
  replay.py     — Simulator class + BarSource abstractions
  metrics.py    — aggregate stats over a TradeLog
  run.py        — CLI entrypoint: python -m backtest.run --tickers AAPL,NVDA ...

The Simulator and the live agent both consume the SAME Strategy interface
(defined in day_trader.py), so anything tuned in backtest behaves identically
when wired live.
"""
