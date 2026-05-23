# Project Context: Stock AI Startup
- This project uses Python 3.12 managed by `uv`.
- We use `mlx-lm` for local inference on M3 Pro.
- Always use the `make_sampler` pattern for generation.
- The `adapters` folder contains our custom fine-tuned weights.

## Agent Context Layer (.agents/)
- **Rules:** See `.agents/rules/` for CLAUDE.md (linting/build) and DECISIONS.md (ADRs).
- **Skills:** See `.agents/skills/quant_analyst/` for ARCHITECTURE.md and API_CONTRACTS.md.

## Testing & Quality Assurance
- **Master Test Suite:** We have a comprehensive mock-based test suite (`master_test_suite.py`).
- **Rule:** You MUST run `uv run python master_test_suite.py` to verify functionality before finalizing ANY code changes. A pre-commit hook is also active to block broken commits.
