"""
llm_queue.py — Phase 4b: single-worker queue for on-demand LLM re-scores.

When the NewsPoller detects a shock (NEGATIVE_BLOCK / NEGATIVE_WARNING /
POSITIVE_BOOST), the cached AI score is no longer trustworthy. We:
  1. Call ai_score_cache.invalidate(ticker) so the live agent stops using it
  2. Enqueue a fresh re-score for that ticker
  3. Worker thread picks up tickers, runs the analysis (single worker — Gemma
     isn't safe to parallelize on Metal), writes new score back to cache

Deduplication: enqueueing the same ticker multiple times before the worker
gets to it only schedules ONE re-score (the latest invalidation wins).
"""

from __future__ import annotations

import threading
import queue
import time
from typing import Callable, Optional


class LLMRescoreQueue:
    """
    Single-worker re-score queue. Tickers are deduped via an internal set,
    so a flurry of news on the same name doesn't queue multiple re-scores.
    """

    def __init__(
        self,
        run_analysis: Callable[[str], None],
        logger,
        worker_idle_sleep_s: float = 0.5,
    ):
        self._run_analysis = run_analysis
        self.logger = logger
        self._queue: queue.Queue = queue.Queue()
        self._pending: set = set()          # tickers currently in queue or being worked
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.is_running = False
        self.worker_idle_sleep_s = worker_idle_sleep_s
        # Metrics
        self.total_enqueued = 0
        self.total_processed = 0
        self.total_dedup_drops = 0
        self.total_errors = 0

    # ─── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> str:
        if self.is_running:
            return "LLMRescoreQueue already running."
        self.is_running = True
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="llm-rescore-q",
                                        daemon=True)
        self._worker.start()
        self.logger.log("🟢 LLMRescoreQueue worker started.")
        return "LLMRescoreQueue started."

    def stop(self) -> str:
        if not self.is_running:
            return "LLMRescoreQueue already stopped."
        self.is_running = False
        self._stop_event.set()
        # Push a sentinel so worker wakes from queue.get
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=2.0)
        self.logger.log(
            f"🔴 LLMRescoreQueue stopped. processed={self.total_processed}, "
            f"dedup_drops={self.total_dedup_drops}, errors={self.total_errors}"
        )
        return "LLMRescoreQueue stopped."

    # ─── enqueue ─────────────────────────────────────────────────────────────

    def enqueue(self, ticker: str, reason: str = "") -> bool:
        """
        Add ticker to the re-score queue. Deduplicates on (ticker), so a
        flurry of news shocks on the same name only triggers ONE re-score.
        Returns True if enqueued, False if deduped.
        """
        if not ticker:
            return False
        ticker = ticker.upper()
        with self._pending_lock:
            if ticker in self._pending:
                self.total_dedup_drops += 1
                return False
            self._pending.add(ticker)
        self._queue.put((ticker, reason))
        self.total_enqueued += 1
        return True

    # ─── worker loop ─────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:                # shutdown sentinel
                return
            ticker, reason = item
            try:
                self.logger.log(f"🔁 LLMRescoreQueue: re-scoring {ticker} ({reason or 'on demand'})")
                self._run_analysis(ticker)
                self.total_processed += 1
            except Exception as e:
                self.total_errors += 1
                self.logger.log(f"⚠️ LLMRescoreQueue: {ticker} re-score failed: {e}",
                                level="WARNING")
            finally:
                with self._pending_lock:
                    self._pending.discard(ticker)
                self._queue.task_done()
                # Small idle gap so we don't pin the GPU
                if self.worker_idle_sleep_s > 0:
                    time.sleep(self.worker_idle_sleep_s)

    # ─── status ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._pending_lock:
            pending = len(self._pending)
        return {
            "running":              self.is_running,
            "queue_depth":          self._queue.qsize(),
            "pending_tickers":      pending,
            "total_enqueued":       self.total_enqueued,
            "total_processed":      self.total_processed,
            "total_dedup_drops":    self.total_dedup_drops,
            "total_errors":         self.total_errors,
        }
