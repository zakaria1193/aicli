# aicli agent rules

## Mandatory: every `aicli run` records token counts

Recording token usage is a mandatory feature of `aicli run`, not an option. Every run must append to the usage log (`$AICLI_USAGE_LOG`) with token counts, so pjm's usage page and any other reader never shows "–" for a client.

- Counts the wrapped CLI reports (claude and agy `--output-format json` / `stream-json`) are logged as `reported: true`.
- When the CLI reports none, log an estimate marked `"estimated": true`. Never drop the counts silently.
- Only an interactive terminal (stdout is a tty) or a run with no output may log `reported: false`.
- Keep the log schema backward compatible: new fields are optional, existing ones keep their meaning, so readers (pjm `proxy_summary`) keep working.
- Any change to `aicli run`, the log format, or the parsing needs tests (`make test`) and a README update (see "Usage log").
