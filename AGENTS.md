# aicli agent rules

## Mandatory: every `aicli run` records token counts

Recording token usage is a mandatory feature of `aicli run`, not an option. Every run must append to the usage log (`$AICLI_USAGE_LOG`) with token counts, so pjm's usage page and any other reader never shows "–" for a client.

- Counts the wrapped CLI reports (claude and agy `--output-format json` / `stream-json`) are logged as `reported: true`.
- When the CLI reports none, log an estimate marked `"estimated": true`. Never drop the counts silently.
- Only an interactive terminal (stdout is a tty) or a run with no output may log `reported: false`.
- Keep the log schema backward compatible: new fields are optional, existing ones keep their meaning, so readers (pjm `proxy_summary`) keep working.
- Any change to `aicli run`, the log format, or the parsing needs tests (`make test`) and a README update (see "Usage log").

## Callers never pick a tool

`aicli run`, `argv` and `resolve` have no `--tool`: a caller passes `--client` and `--class` only, and the router (`aicli.toml` plus `routes.json`) picks the CLI, model, tier and effort, fallbacks included. A caller describes its job with the tool-neutral options (`--prompt`, `--system-prompt-file`, `--session-id`, `--output-format`, ...) and aicli builds the command for whichever CLI it chose.

- A new job option must say what it means for every tool in `JOB_BUILDERS`, ignoring it explicitly where a CLI cannot do it, and be tested for both.
- `--tool` stays only where an operator administers the router or a login (`route set-*`, `auth`, `which`, `usage`).

## Never leave a "trust this folder" prompt

Before launching any backend, `aicli run` (and the HTTP service's scratch runs) calls `trust_cwd(tool)`: the cwd and its git root are marked trusted in that backend's own state (claude: `~/.claude.json`, override `AICLI_CLAUDE_JSON`; agy: `trustedWorkspaces` in the file named by `[agy] trust_file` in `aicli.toml`, override `AICLI_AGY_SETTINGS`). It never fails a run.

## Adding a new backend / CLI

Every backend added to `aicli.toml` and `JOB_BUILDERS` must implement the same tool-neutral job abstraction so callers (such as `pjm`) stay backend-agnostic:

- **Interactive mode + turn-completion (`Stop`) hooks**: wire `--interactive` (live TUI in tmux so the operator can watch and intervene at any time) together with the CLI's lifecycle / `Stop` hook (or equivalent turn-completion signal) rather than leaving the backend stuck in headless-only `-p` mode. Document any gap explicitly if the CLI truly has no hook mechanism.
- **All `JOB_BUILDERS` options**: handle `--prompt` / `--prompt-file`, `--system-prompt-file`, `--oneshot`, `--interactive`, `--output-format`, `--session-id` / `--resume` / `--conversation`, `--settings` (hooks), `--mcp-config`, `--permission-mode`, and `--remote-control` (explicitly ignoring unsupported ones with a comment and test coverage).
- **Folder trust**: an entry in `TRUSTERS` and a test in `TrustTests`, so its runs never stop on a "trust this folder" prompt (see above).
- **Usage & quota**: report or estimate token counts in the usage log (above) and wire `/usage` quota probing if the backend participates in `[fallback]`.
- Update the `Jobs` table in `README.md` and add unit tests in `tests/`.
