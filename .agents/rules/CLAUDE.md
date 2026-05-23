# Claude Project Rules: Stock AI Startup

## Build & Run Commands
- **Environment:** Always use `uv` for package management.
- **Run Dashboard:** `uv run dashboard.py --mode ensemble`
- **Install Dependency:** `uv add <package>`

## Coding Standards
- **Naming:** Use `snake_case` for functions and variables. Use `PascalCase` for classes.
- **Types:** Use Python type hints where possible (e.g., `def analyze(ticker: str) -> dict:`).
- **Inference Pattern:** Always use the `make_sampler(temp=0.0)` utility from `mlx_lm` to ensure deterministic output for financial analysis.
- **Loops:** When adding background threads, always use `threading.Event()` to ensure clean shutdown capabilities.

## UI Guidelines (Gradio)
- **Theme:** Use `gr.themes.Soft()`.
- **Force Light Mode:** Always include the `light_mode_js` snippet to ensure visibility in high-contrast environments.

## Context Hooks
- Prioritize context from `ARCHITECTURE.md` before refactoring core analysis loops.
- Refer to `API_CONTRACTS.md` for Polygon/yfinance data structures.

## Testing & Quality Assurance
- **Master Test Suite:** We have a mock-based test suite (`master_test_suite.py`) validating the data, scoring, and LLM layers.
- **Rule:** You MUST run `uv run python master_test_suite.py` to verify functionality before finalizing ANY code changes. Ensure all tests pass.
