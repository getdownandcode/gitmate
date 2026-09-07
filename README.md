# gitmate

AI-assisted git commit messages, PR summaries, and changelogs — with caching,
cost control, and a human always in the loop before anything is committed.

## Install (development)

Requires [uv](https://docs.astral.sh/uv/). No manual install step is needed;
`uv run` syncs the environment automatically:

```bash
git clone https://github.com/getdownandcode/gitmate.git
cd gitmate
uv run gitmate --help
```

For the full dev toolchain (pytest, ruff, mypy):

```bash
uv sync --extra dev
uv run pytest
```

## Usage (Phase 0)

```bash
uv run gitmate --help
uv run gitmate config set-key   # stored in the OS credential store, never in a file
uv run gitmate config show
uv run gitmate config set model gemini-flash
```

`commit`, `pr-summary`, `changelog`, and `doc` are stubbed until their phases
land — see `AGENTS.md` and the phased roadmap for details.
