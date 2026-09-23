# Changelog

All notable changes to gitmate are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project follows
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-25

First public release. AI-assisted commit messages, PR summaries, and
changelogs with cost control, caching, graceful degradation, and a human
always in the loop.

### Added

- **Commit messages** — `gitmate commit` generates a message from the staged
  diff, then asks before anything is committed: accept, edit, regenerate, or
  cancel. Non-interactive `--yes` is opt-in only.
- **PR summaries** — `gitmate pr-summary --base main` diffs a branch against
  its base and generates a ready-to-paste description (copied to clipboard
  where supported).
- **Changelogs** — `gitmate changelog --from <ref> --to <ref>` groups commits
  by Conventional Commits type into a release-notes section.
- **Diff extraction** — `gitmate debug-diff` prints a cleaned, filtered diff
  (staged, branch, or tag range) with rename detection, binary handling, and
  gitignore-style noise filtering; ignore patterns are user-configurable.
- **Token budgeting** — diffs over the model's context window are truncated
  largest-first or chunked and summarized; budgets resolve per model
  (Gemini ~1M, Claude ~200k) with user overrides.
- **Provider layer** — Gemini via `google-genai` (default
  `gemini-3.5-flash-lite`), behind a swappable `LLMProvider` protocol with
  retries on transient errors and a typed `ProviderUnavailable`.
- **Response caching** — `diskcache` keyed on
  `sha256(template_version + model + prompt)`; repeat diffs cost nothing.
- **Graceful degradation** — when the API is offline, rate-limited, or
  keyless, deterministic template-based messages are generated locally with a
  visible warning.
- **Cost telemetry** — every invocation logs latency, tokens, cache hits, and
  estimated spend to a local SQLite database; `gitmate stats` reports it, with
  free-tier accounting.
- **Git hooks** — `gitmate install-hook` adds a crash-safe
  `prepare-commit-msg` hook (4-second budget, atomic writes, fail-open);
  a `.pre-commit-hooks.yaml` entry is included for pre-commit framework users.
- **Secrets in the OS keychain** — API keys are stored via `keyring`, never
  in config files.

### Fixed

- Hardened git-diff parsing against filenames with spaces, quotes, backslashes,
  and non-ASCII characters, plus ambient git config that breaks naive parsers
  (`diff.mnemonicPrefix`, `core.quotepath`, `diff.renames`).
- Token budgets reject non-positive or negative inputs instead of silently
  over-budgeting.
- Hook-mode retries capped to stay inside the hook timeout; SDK-level retries
  disabled for hook calls.
- Re-running `install-hook` upgrades a gitmate-managed hook in place; foreign
  hooks are never overwritten.

### Security

- API keys live only in the OS credential store and are never written to disk
  or logs.
