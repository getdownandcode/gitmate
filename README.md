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

## Diagrams

Editable draw.io sources plus publishable PNG exports (open the `.drawio`
files in the draw.io desktop app to edit):

| Diagram | Source | Export |
|---|---|---|
| System architecture (Phases 0–8, gemini-3.5-flash-lite) | [`docs/architecture.drawio`](docs/architecture.drawio) | [`docs/architecture.png`](docs/architecture.png) |
| Phase 0 class diagram (`config.py`, `cli.py`) | [`docs/class-diagram.drawio`](docs/class-diagram.drawio) | [`docs/class-diagram.png`](docs/class-diagram.png) |
| Use cases (implemented + planned) | [`docs/use-case.drawio`](docs/use-case.drawio) | [`docs/use-case.png`](docs/use-case.png) |
| `config` command sequences | [`docs/sequence.drawio`](docs/sequence.drawio) | [`docs/sequence.png`](docs/sequence.png) |
