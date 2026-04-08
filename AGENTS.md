# Repository Guidelines

## Project Structure & Module Organization
Core application code lives at the repository root and in small domain packages. `main.py` exposes the Typer CLI entrypoint (`ta`) for `list-assignments`, `grade`, `review`, and `submit`. Shared data models are in `models.py`, runtime settings are in `config.py`, and package folders split responsibilities by workflow stage: `auth/` for PKU IAAA login, `crawler/` for Blackboard data collection, `scorer/` for LLM grading and parsers, `review/` for spreadsheet/TUI review flows, and `submitter/` for posting approved scores. Prompt templates live in `prompts/`. Tests mirror the code layout under `tests/`.

## Build, Test, and Development Commands
Use Python 3.12+ and `uv`.

```bash
uv sync
uv run python main.py --help
uv run python main.py list-assignments --course _98024_1
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md
uv run python main.py review --needs-review
uv run python main.py submit --course _98024_1 --column 423829 --scores scores.xlsx --dry-run
uv run pytest
```

`uv sync` installs runtime and dev dependencies. Use `list-assignments` to discover assignment `gradeBookPK` values before grading or submission. The `grade`, `review`, and `submit` commands map directly to the production workflow described in `README.md`. Run `uv run pytest` before opening a PR.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, type hints on public functions, and concise docstrings where behavior is not obvious. Use `snake_case` for functions, variables, and modules, `PascalCase` for Pydantic models and enums, and keep CLI option names aligned with existing long-form flags such as `--regrade-unapproved`. Prefer small, focused modules over large cross-cutting helpers.

## Testing Guidelines
Tests use `pytest` and are named `tests/test_*.py`. Add focused unit tests next to the affected area, for example model behavior in `tests/test_models.py` or spreadsheet logic in `tests/test_spreadsheet.py`. Cover both happy paths and grading/review edge cases such as missing files, zero scores, resume flows, and overrides.

## Commit & Pull Request Guidelines
Recent history favors short, imperative commit subjects like `Fix: Add batch download fallback` or `Refactor review TUI into separate files`. Keep commits scoped to one change. PRs should include a clear summary, user-visible impact, test coverage notes, and screenshots or terminal snippets when changing the review TUI or CLI output. Link related issues when applicable.

## Configuration & Security
Store secrets in `.env`, using `.env.example` as the template. Never commit PKU credentials, API keys, downloaded submissions, or generated review spreadsheets unless the change explicitly requires sanitized fixtures.
