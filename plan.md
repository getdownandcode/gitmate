# gitmate — AI Git Assistant CLI: Build Plan

A CLI that generates commit messages, PR summaries, and changelogs from your
git diffs using an LLM — with cost control, caching, and a human always in
the loop. Goal: something you actually run every day, not just a portfolio repo.

---

## Design decisions locked in up front

| Decision | Choice | Why |
|---|---|---|
| Caching strategy | **Hash-based** (`sha256(diff)` → response) over semantic caching | Semantic caching (embedding similarity) needs a vector store, an embedding model call on every lookup, and a similarity threshold you'll forever be tuning. Hash caching is O(1), free, and correct — its only cost is a ~0% hit rate on a diff that changed by one line. That's an acceptable, explainable tradeoff for v1. Revisit semantic caching only if cache-hit-rate data (Phase 6) shows it's worth the complexity. |
| Git access | **`subprocess` calls to the `git` binary** over GitPython | You need exact control over diff flags (`--staged`, `-U0`, path filters) and want zero abstraction leakage. GitPython is a nice-to-have wrapper you don't need — it's another dependency and another place for version mismatches to bite you. |
| LLM SDK | **Google GenAI SDK (Gemini)** default, behind your own `LLMProvider` interface (Anthropic secondary/deferred) | Official SDK (`google-genai`), targets Gemini 3 Flash / Flash-Lite per AGENTS.md. Handles generation and token counting behind a swappable interface without guessing with third-party tokenizers. |
| Config format | **TOML** (`~/.config/gitmate/config.toml`) | Human-editable, standard for Python CLIs (same as `pyproject.toml`), no extra parsing dependency (`tomllib` is stdlib in 3.11+). |
| Secrets | **`keyring`** library, not plaintext config | Uses the OS credential store (macOS Keychain / Windows Credential Manager / Secret Service on Linux). No API key ever touches a file you could accidentally `git add`. |
| Cache store | **`diskcache`** over raw SQLite | Gives you TTL, LRU eviction, and thread-safety out of the box with a dict-like API — SQLite is fine but you'd be re-implementing what this library already does correctly. |
| CLI framework | **Typer** + **Rich** | Typer for decorator-based commands and auto-generated `--help`; Rich for readable diffs/diffs-with-syntax-highlighting and a nice review prompt in the terminal. |
| Packaging | **`pyproject.toml` + `hatchling`**, installed via **`pipx`** | `pipx` is the standard way people install CLI tools without polluting their global Python env. Publish to PyPI once stable. |
| Automatic trigger | **`pre-commit` framework hook**, optional `prepare-commit-msg` git hook | This is what turns it from "a script I remember to run" into "part of my git workflow." |

---

## Phase 0 — Skeleton & Config (½–1 day)

**Goal:** `gitmate config set-key` works, `gitmate --help` shows all planned commands (stubbed).

- Set up `pyproject.toml`, package layout (`src/gitmate/`), Typer app with stub commands: `commit`, `pr-summary`, `changelog`, `doc`, `config`.
- `config` command: read/write `~/.config/gitmate/config.toml` (model choice, default commit style, budget cap); store API key via `keyring`.
- Basic Rich-styled CLI output (banner, error formatting).

**Deliverables:** installable package (`pipx install -e .`), working `config` command, CI skeleton (GitHub Actions running `pytest` on push).

**Tech:** Typer, Rich, `tomllib`/`tomli-w`, `keyring`, `pytest`, GitHub Actions.

---

## Phase 1 — Diff/Context Extractor (1 day)

**Goal:** `gitmate debug-diff` prints a cleaned, filtered diff for any git range.

- Wrap `subprocess.run(["git", "diff", ...])` for three modes: staged (`--staged`), branch-vs-branch (for PR summaries), and range (for changelogs, `tagA..tagB`).
- Noise filtering: skip lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`), minified/generated files, binary diffs — via a configurable ignore-glob list (defaults + user overrides in config).
- Return a structured object: list of `FileDiff(path, additions, deletions, patch_text, is_binary)`, not a raw string — every downstream layer works off this.

**Deliverables:** `DiffExtractor` class with unit tests against a scratch git repo (use `pytest` fixtures that `git init` a temp dir and make real commits — don't mock git itself, it's cheap to run for real).

**Tech:** `subprocess`, `pytest` with `tmp_path` fixtures.

---

## Phase 2 — Token Budget Manager (½–1 day)

**Goal:** Given a `FileDiff` list, decide: send whole, truncate, or chunk-and-summarize.

- Count tokens via the google-genai SDK's token-counting support (`GeminiTokenCounter`, provider-specific behind a `TokenCounter` Protocol).
- Strategy: if under budget, send as-is. If over, drop full patches for the largest low-signal files first (test snapshots, lockfile-adjacent generated code) and replace with a one-line "N files changed, omitted for size" note. If still over after that, fall back to chunk-and-summarize-then-merge (summarize each file's diff separately, then feed summaries into the final prompt).
- Make the strategy explicit and loggable — the user should be able to run `--verbose` and see "included 8/12 files, truncated 4."

**Deliverables:** `TokenBudgetManager` with a documented strategy function and tests covering: fits easily, needs truncation, needs chunking.

**Tech:** `google-genai` SDK token counting, plain Python (no extra deps needed here).

---

## Phase 3 — Provider Abstraction + Cache Layer (1 day)

**Goal:** `LLMProvider.generate(prompt) -> LLMResponse`, with caching in front of it, swappable backend.

- Define a small `Protocol`/ABC: `LLMProvider.generate(prompt: str, model: str) -> LLMResponse`.
- Implement `GeminiProvider` first, targeting **Gemini 3 Flash / Flash-Lite** via the `google-genai` SDK (`client.models.generate_content`). This is the locked-in default per AGENTS.md; Anthropic is the secondary provider using the same interface and is deferred to the stretch-ideas list until the abstraction is proven once. Model IDs churn fast (2.0 Flash is retired; Flash-Lite previews deprecate within months), so the ID stays configurable in `config.py` rather than hardcoded.
- Cache: wrap the provider call — `diskcache.Cache` keyed on `sha256(prompt + model + template_version)`, with a configurable TTL (default: no expiry, since a given diff's ideal message doesn't go stale — but bump the key if you change your prompt template, hence `template_version` in the key). Template version is a hardcoded `"1"` placeholder until Phase 4 ships real templates, so Phase 3 cache entries don't need a key migration later.
- Cache location follows the project's XDG convention: `~/.cache/gitmate/`, overridable via a `cache_dir` config field.
- Retry with exponential backoff (`tenacity` library) on transient errors (5xx, 429 rate-limit, transport failures); on final failure, raise a typed `ProviderUnavailable` exception (Phase 4 catches this).
- **Named Risk (from Phase 2 review):** `TokenBudgetManager` has a hardcoded `context_window` default (1,048,576) that does not inspect `model`. Add a `TokenBudgetManager.from_config(cfg, counter)` factory that resolves `get_context_window(cfg)`, so non-Gemini models (e.g. Claude's 200k limit) never get silently over-budgeted to 1M.

**Deliverables:** `LLMProvider` interface, `GeminiProvider` implementation, `CachedProvider` wrapper, `from_config` factory, tests using a fake in-memory provider (no real API calls in CI — mock at the `LLMProvider` boundary, not deeper; include a call-count test proving the cache is consulted before `generate()`).

**Tech:** `google-genai` SDK, `diskcache`, `tenacity` for retries. (Anthropic SDK, already a dependency, stays unused until the secondary provider is implemented.)

---

## Phase 4 — Graceful Degradation + Prompt Templates (½ day)

**Goal:** If the API is down/rate-limited/offline, you still get a usable commit message.

- Template-based fallback: derive a message from `git status` + file extensions (e.g. "Update 3 files in src/auth (added: 2, modified: 1)"). No LLM call at all.
- Prompt templates as versioned files (`templates/commit_conventional.txt`, `templates/commit_plain.txt`, `templates/pr_summary.txt`), selected by config. Bump a version string in each when you edit it — this is the `template_version` the cache key depends on.
- Wire this so `LLMProvider` failure (`ProviderUnavailable`) → caught → fallback path, with a visible warning ("⚠ API unavailable, using template fallback") so the user isn't confused about message quality.
- Chunk-and-summarize execution (deferred from Phase 2): when `TokenBudgetManager.assess()` returns `BudgetStrategy.NEEDS_CHUNKING`, consume `omitted_diffs`, summarize chunks via the provider, and merge summaries into the final prompt (with graceful fallback if the provider fails).

**Deliverables:** fallback generator, prompt template engine with at least 2 prompt template styles, chunk-and-summarize orchestrator, tests that simulate provider failure, template rendering, and chunk-and-summarize prompt merging.

**Tech:** plain Python, Jinja2 (optional, if templates get more than string `.format()` can cleanly handle).

---

## Phase 5 — Output & Human-in-the-Loop (½ day)

**Goal:** `gitmate commit` shows the generated message, lets you edit/accept/reject, then actually commits.

- Default: print the message in Rich, prompt `[a]ccept / [e]dit / [r]egenerate / [c]ancel`.
- Edit path: open `$EDITOR` pre-filled with the message (mirrors `git commit -e`).
- Accept path: write to `.git/COMMIT_EDITMSG` and run `git commit -F`.
- Never auto-commit without one of these confirmations, even with `--yes` flags — make `--yes` require an explicit config opt-in, not a default flag.

**Deliverables:** working `gitmate commit` end-to-end against a real repo; integration test using a temp git repo.

**Tech:** Rich `Prompt`, `subprocess` for `$EDITOR` and final `git commit`.

---

## Phase 6 — Metrics & the README table (½ day)

**Goal:** Every run logs latency, tokens used, cache hit/miss, and estimated cost to a local SQLite table — this is what generates your README's metrics table, and it's also what tells you later whether semantic caching would actually help.

- `~/.local/share/gitmate/metrics.db`: one row per invocation (timestamp, command, tokens_in, tokens_out, cache_hit, latency_ms, estimated_cost_usd, fallback_used).
- `gitmate stats` command: prints aggregate table (avg latency, cache hit rate, total estimated spend this month) — this is the exact table you paste into the README.

**Deliverables:** metrics recording wired into Phases 3–5, `gitmate stats` command, a real 2-week's worth of your own usage data before you write the README.

**Tech:** stdlib `sqlite3` (this one really is simple enough not to need `diskcache`'s abstraction — it's structured rows, not a KV cache).

---

## Phase 7 — Beyond commits: PR summaries & changelogs (1–1.5 days)

**Goal:** the tool earns its keep beyond `git commit` — this is what makes it a daily-use tool instead of a one-trick demo.

- `gitmate pr-summary --base main`: diff current branch against `main`, generate a PR description (what changed, why, testing notes) using the `pr_summary` template. Copies to clipboard (`pyperclip`) and/or opens the `gh pr create` flow if the `gh` CLI is detected.
- `gitmate changelog --from v1.0.0 --to v1.1.0`: walks commit messages + diffs between two tags/refs, groups by conventional-commit type (feat/fix/chore), generates a `CHANGELOG.md` section.
- Both reuse the same Diff Extractor / Token Budget Manager / Provider stack — this is the payoff of building those as real layers instead of one script.

**Deliverables:** two new working commands, tests, and (this is the actually-useful part) you running `pr-summary` on your next 3 real PRs instead of writing them by hand.

**Tech:** `pyperclip`, optional `gh` CLI shell-out detection.

---

## Phase 8 — Automatic trigger: git hook integration (½ day)

**Goal:** stop having to remember to run the tool at all.

- Ship an optional local `prepare-commit-msg` hook. It calls only the generation pipeline, writes the message for Git's normal editor, and never runs `git commit` or the interactive accept/edit/regenerate loop.
- Fail open: the launcher catches failures and exits 0; the generation worker has a four-second timeout and uses Phase 4 fallback when available. Message writes are atomic so a failed write leaves Git's original message intact.
- Generate only when the source is `template` or absent. Leave `message` (explicit `-m`/`-F`), `merge`, `squash`, and all other source values untouched.
- Refuse to overwrite an existing hook, support idempotent install and managed-hook uninstall, and honor Git's configured hooks path.
- Also provide `.pre-commit-hooks.yaml` with `language: python` and `stages: [prepare-commit-msg]` for team adoption. The local `.git/hooks` installer is for one developer; the versioned pre-commit declaration is for teams. Its isolated environment still uses the user's OS credential store through keyring.

**Deliverables:** `gitmate install-hook` and `gitmate uninstall-hook`, crash/timeout safe real-repository tests, and a documented pre-commit config snippet.

**Tech:** `pre-commit` framework config format, plain shell/Python hook script.

---

## Phase 9 — Packaging, docs, and the story (½–1 day)

**Goal:** ship it.

- Finalize `pyproject.toml`, publish to PyPI (`pip install gitmate` or your chosen name — check it's not taken).
- README: architecture diagram (your existing one, updated with Config/Metrics/Hook boxes), the real metrics table from Phase 6, a demo GIF (`vhs` or `asciinema` + `agg` are free tools for scripted terminal GIFs), and the caching-tradeoff paragraph already decided above.
- Write the "interview story": lead with the graceful-degradation and human-in-the-loop decisions — those are the two choices that signal product judgment, not just API-calling.

**Deliverables:** PyPI package, finished README, demo GIF.

**Tech:** `hatchling`/`build`/`twine` for publishing, `vhs` (Charm) for demo recording.

---

## Suggested order & time budget

Phases 0→5 are the MVP (~4–5 days) and get you a working `gitmate commit`.
Phase 6 (metrics) should run *underneath* everything from Phase 3 onward even though it's written up last — start logging early so you have real data by the time you write the README.
Phases 7–8 are what make this a tool you keep using after the demo, not just for the interview — don't skip them for "future work" bullet points.

## Stretch ideas (only after the above works and you're using it daily)
- Budget cap: hard-stop or warn when `metrics.db` shows spend exceeding a configured monthly cap.
- Multi-provider: add an `OpenAIProvider` to prove the abstraction actually swaps cleanly.
- Revisit semantic caching *only if* Phase 6 data shows hash-cache hit rate is too low to matter.
