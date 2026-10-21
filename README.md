# aicli

**A proxy for AI coding CLIs.** Point your scripts, cron jobs and services at
`aicli`, and it runs them through the AI CLI subscriptions you already pay for
(Claude Code, Antigravity, more to come), routed and logged from one place.

## Why

- **Subscriptions are far cheaper than APIs.** A flat monthly plan for a
  coding CLI covers an amount of work that would cost many times more at
  per-token API prices. If you already pay for one, your own automation
  should use it too.
- **Nothing lets you use several and switch between them.** Each CLI has its
  own flags, model names and quota. Every script ends up hardcoding one of
  them, and moving a job from one provider to another means editing every
  caller.

aicli fixes both. Callers say *what kind of work* they're doing (a workload
class like `lightweight-task` or `deep-spec`). aicli decides *which CLI and
model* runs it, from one config file you can change live, and keeps one log
of who used what.

## How it works

```
your script ──> aicli run --client myrepo:summarize --class lightweight-task -- -p "..."
                  │
                  ├─ looks up the class in routes.json   -> agy, tier "low"
                  ├─ builds the real argv               -> agy --model gemini-3.8-flash-low -p "..."
                  ├─ appends one line to the usage log
                  └─ execs the unmodified CLI under your own login
```

aicli never reads, stores or forwards your OAuth tokens, and never calls a
vendor backend itself. It only builds a command line and runs the official
CLI, exactly as you would by hand. Exit code, stdin/stdout, signals and TTY
are the real CLI's.

For callers that want text in, text out, an optional HTTP service
(`server.py`) puts the same routing behind an **OpenAI-compatible
`/v1/chat/completions`** endpoint.

## Backends

| Backend | CLI | Status |
|---|---|---|
| Claude Code | `claude` | supported |
| Antigravity | `agy` | supported |
| Others (Codex, Gemini CLI, ...) | | planned |

Each backend is a table in `aicli.toml`: its binary, model flag, effort flag
and three model tiers (`low`, `default`, `deep`).

## Quick start

Needs Python 3.11+ and nothing else (standard library only).

```sh
git clone https://github.com/zakaria1193/aicli ~/my_repos/aicli
cd ~/my_repos/aicli
make install    # links ~/.local/bin/aicli -> aicli.py
make test       # unit tests against a fake CLI; never spends real quota

aicli resolve --class standard-dev                 # where would this go?
aicli run --client demo --class lightweight-task -- -p "say hi"
```

## Concepts

**Workload classes** describe the job, not the tool. The defaults:

| Class | For |
|---|---|
| `lightweight-task` | extraction, scraping, tagging; high volume |
| `maintenance` | background fixes, dependency bumps, CI repairs |
| `managerial-audit` | scheduled reports, watchdogs, audits |
| `standard-dev` | everyday code changes |
| `deep-spec` | architecture, long specs, deep reviews |
| `interactive-shell` | a human at a terminal |

**Clients** name the caller, like `myrepo:summarize`. A client can override
its class's route, and a longer name also matches a shorter one
(`pjm:p-0001:auto-execute` falls back to `pjm:auto-execute`).

**Routing order:** an explicit `--tool`, `--tier` or `--effort` wins, then the
client's own route, then its class. Without a class or a `--tool`, aicli
exits 2.

## Config

aicli reads the first config that exists:

1. `$AICLI_CONFIG`
2. `~/.config/aicli/aicli.toml`
3. `aicli.toml` in this repo (the defaults)

`aicli.toml` holds the backends and the default classes. `routes.json`,
next to it (or at `$AICLI_ROUTES`), holds live overrides: class changes and
client routes. Edit it with `aicli route ...` or from a UI; saves go through
a symlink to the real file, so a deployment can keep its routes in its own
repo.

### Fallback

`[fallback]` moves runs off a plan that is out of quota. Once a tool's
five-hour or weekly bar reaches `at` percent (95 by default), `resolve`,
`argv` and `run` use the first `[[fallback.<tool>]]` step that still has
room. A step names a tool, the agy quota `group` that limits it, and a model
per tier. agy meters its Claude models apart from Gemini, so the shipped
chain is: Claude plan → Claude through agy → Gemini 3.8 through agy.

- An unknown reading counts as room. A probe that times out never moves work.
- `run` probes with a short timeout. When that probe fails, it uses the last
  good reading of the last 30 minutes, kept in `~/.local/state/aicli/quota.json`
  (`$AICLI_QUOTA_CACHE`). `aicli usage` refreshes that file.
- A caller that pins `--tool` built that CLI's own flags, so `run` never
  moves it to another CLI. Such a caller asks `aicli resolve --tool T --json`
  first and builds for the `tool` and `model` it returns. A fallback to a
  step on the same tool still applies.
- `--no-fallback` turns it off for one call. The usage log and `--meta-out`
  record every fallback with its reason.

## Commands

| Command | Does |
|---|---|
| `aicli run [opts] [--meta-out FILE] -- ARGS` | Route, log, then exec the real CLI |
| `aicli argv [opts] [--json] -- ARGS` | Print the command `run` would exec (for tmux, systemd) |
| `aicli resolve [opts] [--quota] [--json]` | Print where a call would go, without running it |
| `aicli which <tool>` | Print the real binary's path |
| `aicli usage [--json]` | Each subscription's quota bars, from the CLI's `/usage` |
| `aicli fallback [--json]` | Each fallback chain, its readings and the step in use |
| `aicli routes [--json]` | Show the routing table |
| `aicli route set-class NAME --tool T [--tier ...]` | Change a class's route |
| `aicli route set-client ID [--class C] [--tool T] [--tier ...]` | Route one client |
| `aicli route remove-client ID` | Drop a client's route |

`opts` are `--client ID`, `--class NAME`, `--tool`, `--tier low|default|deep`,
`--effort low|medium|high` and `--no-fallback`.

Details worth knowing:

- If your args already set the model or effort, aicli leaves them alone, and
  the log records what really ran.
- A backend with `model_sets_effort = true` (agy, whose model names include
  the effort) gets no separate effort flag when aicli picks the model.
- `which` skips `/usr/local/bin` and `/usr/local/sbin`, so a wrapper that
  symlinks `claude` there can't make aicli call itself.
- `usage` exits 0 even when a backend's state is `unknown`; the state is in
  the output.

## HTTP service

`python3 server.py` starts an OpenAI-compatible server on `127.0.0.1:8500`.
Settings are environment variables; see `.env.example`.

| Endpoint | |
|---|---|
| `GET /healthz` | `200 ok`, runs nothing |
| `GET /v1/models` | `claude`, `agy`, and `<tool>/<tier>` for every tier |
| `POST /v1/chat/completions` | Runs the CLI once; `stream` and `reasoning_effort` supported |

- Each request runs in an empty scratch folder with no permission-skipping
  flag. At most `AICLI_MAX_CONCURRENT` runs at once; the rest queue.
- Errors: 400 bad request or unknown model, 413 prompt over 120 KiB, 502 the
  CLI failed, 504 timeout.
- **Loopback only by default.** Binding elsewhere needs both
  `AICLI_ALLOW_NON_LOOPBACK=1` and `AICLI_API_KEY`, and remote callers must
  send `Authorization: Bearer <key>`.

This repo ships no systemd unit; run it under whatever supervisor your host
uses.

## Usage log

Every `aicli run` and every HTTP request appends a JSON line to
`$AICLI_USAGE_LOG` (default `~/.local/state/aicli/usage.jsonl`): when, client,
class, backend, tier, model, and for HTTP requests the token counts the CLI
reported.

```json
{"at": "2026-10-14T18:20:00+00:00", "tool": "claude", "tier": "default",
 "model": "claude-sonnet-5", "client": "pjm", "reported": true,
 "input": 3, "output": 7, "cache_write": 20, "cache_read": 100, "total": 130}
```

The HTTP client name comes from the `X-Aicli-Client` header, else the OpenAI
`user` field, else `AICLI_REMOTE_CLIENT` for remote callers, else `unknown`.
A log that can't be written never fails a request.

## Fair use

aicli runs the official, unmodified CLIs under **your own** login, for
**your own** tools. That is what the subscriptions are for. Don't put it
behind a public or multi-user app, and don't use it to share one plan
between people: that breaks the providers' terms and puts your account at
risk.

## Files

```
aicli.py       the aicli command
server.py      the optional OpenAI-compatible HTTP service
aicli.toml     default config: backends, tiers, workload classes
.env.example   the service's settings
tests/         unit tests and the fake CLIs they run against
```

## License

MIT, see [LICENSE](LICENSE).
