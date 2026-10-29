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
