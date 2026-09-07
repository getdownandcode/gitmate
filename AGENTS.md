# AGENTS.md — instructions for AI coding agents working on this repo

This file is read automatically by most agentic coding tools (OpenCode, Claude
Code, Cursor, etc.). It states decisions already made — don't re-litigate
them without asking. For the full phased roadmap and rationale, see
`plan.md` at repo root; work through it **one phase at a time**, not all at
once. Stop and report back at the end of each phase before starting the next.

## Project

A CLI (`gitmate`) that generates git commit messages, PR summaries, and
changelogs from diffs using an LLM, with caching, cost control, and a human
always in the loop before anything is committed.

## Locked-in decisions — do not change without asking

| Area | Decision |
|---|---|
| CLI framework | Typer + Rich |
| Git access | `subprocess` calling the real `git` binary — no GitPython |
| LLM provider | Behind an `LLMProvider` Protocol/ABC. Default implementation targets **Gemini** (`google-genai` SDK, model: Gemini 3 Flash / Flash-Lite — NOT 2.5 Flash, which is being retired). Anthropic is a secondary provider using the same interface. |
| Config | TOML at `~/.config/gitmate/config.toml`, loaded via stdlib `tomllib` |
| Secrets | `keyring` library only. Never write API keys to a config file or `.env` that could be committed. |
| Cache | `diskcache`, keyed on `sha256(prompt + model + template_version)` |
| Metrics | stdlib `sqlite3` at `~/.local/share/gitmate/metrics.db` — one row per invocation |
| Packaging | `pyproject.toml` + `hatchling` build backend, installed locally via `pipx install -e .` |
| Python version | 3.11+ (relies on stdlib `tomllib`) |

## Directory layout

```
src/gitmate/
  cli.py              # Typer app, command definitions only — no business logic here
  diff_extractor.py   # Phase 1
  token_budget.py     # Phase 2
  providers/
    base.py           # LLMProvider Protocol
    gemini.py
    anthropic.py
    cache.py           # CachedProvider wrapper
  fallback.py         # Phase 4 template-based degradation
  templates/           # versioned prompt templates, plain text or Jinja2
  config.py
  metrics.py
tests/
  conftest.py          # shared fixtures, incl. temp git repo fixture
  test_*.py
plan.md
AGENTS.md
```

## Coding standards

- Type hints on all function signatures. This is a small enough codebase that `mypy --strict` should stay clean.
- Format/lint with `ruff` (covers both). Run `ruff check .` and `ruff format .` before considering any phase done.
- Every module that does I/O (git, filesystem, network) must be behind an interface that tests can substitute — no real network calls in the test suite. Real `git` calls in a `tmp_path` fixture are fine and preferred over mocking git itself.
- Docstrings on public functions/classes: one line on what, one line on why if the "why" isn't obvious from the plan.
- Do not add a dependency not listed in `pyproject.toml` without flagging it first — check it's actively maintained and has no better stdlib alternative.

## Test policy

- `pytest`, tests live in `tests/`, mirroring `src/gitmate/` module names.
- Mock at the `LLMProvider` boundary (a fake in-memory provider), never deeper — we want to test our own logic, not the SDK.
- Each phase in `plan.md` lists its own deliverables — treat those as the acceptance criteria/test list for that phase.

## Workflow expectations

- After finishing a phase, run the full test suite and `ruff check .`, then summarize what was built against that phase's deliverables list before moving on.
- If a plan.md decision turns out to be wrong once you're implementing it (e.g. a library is unmaintained), stop and flag it with the reason — don't silently substitute something else.
- Never wire up `git commit` to run non-interactively without explicit `--yes` opt-in — human-in-the-loop by default is a hard requirement, not a nice-to-have (see Phase 5 in plan.md).
