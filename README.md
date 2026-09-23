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

## Usage

```bash
uv run gitmate --help
uv run gitmate config set-key   # stored in the OS credential store, never in a file
uv run gitmate config show
uv run gitmate config set model gemini-3.5-flash-lite
uv run gitmate config set commit_style conventional      # or 'plain'
uv run gitmate config set ignore_globs '*.gen.py,*.tmp'  # comma-separated, empty clears
uv run gitmate config set max_context_tokens 8000        # override model context limit
uv run gitmate config set reserved_output_tokens 1500    # reserve headroom for model reply
uv run gitmate config set template_overhead 600          # reserve headroom for prompt template
uv run gitmate config set cache_dir /path/to/cache       # custom prompt cache directory
uv run gitmate debug-diff             # staged diff: per-file table + cleaned patch
uv run gitmate debug-diff --summary   # per-file stats table only, no patch text
uv run gitmate debug-diff --base main # branch-vs-main diff (PR path)
uv run gitmate commit                 # review, edit, accept, or cancel a message
uv run gitmate pr-summary --base main
uv run gitmate changelog --from v1.0.0 --to v1.1.0
uv run gitmate stats
uv run gitmate install-hook          # optional local prepare-commit-msg hook
uv run gitmate uninstall-hook        # remove the local hook
```

Phase 4 introduces versioned prompt templates (`src/gitmate/templates/`: `commit_conventional.txt`,
`commit_plain.txt`, `pr_summary.txt`) and deterministic template-based fallback degradation
(`fallback.py`). When the LLM backend is offline, rate-limited, unauthenticated, or fails retries,
`generate_commit_message` warns the user (`⚠ API unavailable, using template fallback`) and
derives a clean commit message directly from diff metadata without making an LLM call.
`doc` is an unscheduled stub; commit, PR summary, changelog, stats, and hook
commands are implemented.

### Automatic commit message generation

For a personal checkout, run `gitmate install-hook`. It installs a local
`prepare-commit-msg` hook; Git opens the usual editor with the generated message
so you can review or change it before saving. The hook never runs `git commit`
and has a four-second generation timeout. If generation fails or times out, it
leaves Git's existing message in place and exits successfully so the commit is
not blocked. It only generates when the source is `template` or absent; explicit
`git commit -m`/`-F` messages (`message`), merge messages (`merge`), squash
messages (`squash`), and other source values are left alone. Existing hooks are
refused rather than overwritten; `gitmate uninstall-hook` removes only the
gitmate-managed hook. Both commands work with Git's configured `core.hooksPath`.

For teams using the `pre-commit` framework, add this to `.pre-commit-config.yaml`
and run `pre-commit install --hook-type prepare-commit-msg`:

```yaml
repos:
  - repo: https://github.com/getdownandcode/gitmate
    rev: <gitmate-version>
    hooks:
      - id: gitmate-prepare-commit-msg
```

The repository's `.pre-commit-hooks.yaml` declares the `prepare-commit-msg`
stage and `language: python`, so pre-commit installs gitmate into its isolated
environment for each adopting checkout. `keyring` still uses the user's OS
credential store from that environment; the API key is not copied into the
repository or the pre-commit environment. The `gitmate install-hook` route is
local-only because `.git/hooks` is not version controlled; the pre-commit route
lets a team version the hook declaration for everyone who opts in.

## Diagrams

Editable draw.io sources plus publishable PNG exports (open the `.drawio`
files in the draw.io desktop app to edit):

| Diagram | Source | Export |
|---|---|---|
| System architecture (Phases 0–8, gemini-3.5-flash-lite) | [`docs/architecture.drawio`](docs/architecture.drawio) | [`docs/architecture.png`](docs/architecture.png) |
| Class diagram (Phases 0–4: config, diffs, budget, providers, templates & fallback) | [`docs/class-diagram.drawio`](docs/class-diagram.drawio) | [`docs/class-diagram.png`](docs/class-diagram.png) |
| Use cases (implemented + planned) | [`docs/use-case.drawio`](docs/use-case.drawio) | [`docs/use-case.png`](docs/use-case.png) |
| `config` command sequences | [`docs/sequence.drawio`](docs/sequence.drawio) | [`docs/sequence.png`](docs/sequence.png) |
