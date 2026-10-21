# aicli

One command every script reaches `claude` (Claude Code) and `agy`
(Antigravity) through. Binary paths, model tiers, standard flags, workload
routing and `/usage` parsing live in one config file instead of being copied
into every project.

aicli only builds an argv and runs the real, unmodified CLI under your own
login. It never reads, stores or forwards OAuth tokens
(`~/.claude/.credentials.json`, `~/.gemini/`), and it never calls a vendor
backend itself.

It has two halves:

- `aicli.py`, the `aicli` command. Callers exec it instead of `claude`/`agy`:
  `aicli run --client <repo>:<use> --class <workload-class> -- <args...>`.
- `server.py`, an optional HTTP service (`aicli-ai`) that fronts the same argv
  builder with an OpenAI-compatible `/v1/chat/completions` endpoint, for
  callers that want text in, text out.

Python 3.11+ standard library only. No dependencies.

## Quick start

```
git clone https://github.com/zakaria1193/aicli ~/my_repos/aicli
cd ~/my_repos/aicli
make install   # links ~/.local/bin/aicli -> aicli.py
make test      # unit tests against a fake CLI, never the real one
aicli resolve --class standard-dev
aicli run --client myrepo:summarize --class lightweight-task -- -p "hello"
```

## Config

aicli reads the first of these that exists:

1. `$AICLI_CONFIG` (tests use it),
2. `~/.config/aicli/aicli.toml`, where a deployment links its own config,
3. `aicli.toml` in this repo, the defaults.

`aicli.toml` holds, per tool, `bin`, standard `args`, `model_flag`,
`effort_flag` and a `low`/`default`/`deep` tier table, plus the default
workload classes (`[classes.*]`).

`routes.json` is the live switchboard layered over those classes: client
overrides, class changes and an `optiplex_projects` list. It sits next to the
config file unless `AICLI_ROUTES` or the config's `routes_file` says
otherwise. `aicli route ...` edits it, and so can a UI such as homelab's
cockpit. A save writes through a symlink to the real file, so a deployment
can keep `~/.config/aicli/routes.json` as a link into its own repo.

## Files

- `aicli.py` - the `aicli` command: `which`, `argv`, `run`, `resolve`,
  `usage`, `routes`, `route`.
- `aicli.toml` - the default config.
- `server.py` - the `aicli-ai` HTTP service: `/healthz`, `/v1/models`,
  `/v1/chat/completions`.
- `.env.example` - the service's settings (`AICLI_HOST`, `AICLI_PORT`,
  `AICLI_MAX_CONCURRENT`, `AICLI_TIMEOUT`, `AICLI_USAGE_LOG`, ...).
- `tests/` - unit tests and the fake CLI they run against
  (`tests/fake_cli/`). No test runs the real `claude` or `agy`.

## Commands

```
aicli which <claude|agy>
```
Prints the absolute path of the real binary: the configured `bin` if it's
executable, otherwise the first match on `PATH` (skipping `/usr/local/bin`
and `/usr/local/sbin`, where the tmux wrapper's symlinks live, to avoid a
loop). Exits 1 with `<tool> CLI not found` if neither resolves.

```
aicli argv [--tool <claude|agy>] [--tier low|default|deep] [--effort low|medium|high]
           [--client <id>] [--class <name>] [--json] [-- args...]
```
Prints the argv that `run` would exec: one shell-quoted line by default, or a
JSON array with `--json`. For callers that launch the CLI themselves (tmux,
systemd). `--tier` defaults to `default`. If `args` already holds the model
or effort flag, aicli does not add its own. A tool with
`model_sets_effort = true` (agy: `gemini-3.8-flash-low` names its effort, and
agy rejects a different `--effort`) gets no effort flag at all when aicli adds
the tier's model; the tier decides.

`--client` and `--class` pick the tool and tier from `routes.json` instead:
a client's own row first (`pjm:p-0001:auto-execute` also matches
`pjm:auto-execute`), then its class. An explicit `--tool`, `--tier` or
`--effort` always wins. Without a class or a `--tool`, it exits 2.

```
aicli run [same options as argv] [--meta-out <path>] [-- args...]
```
Same options as `argv`, but `exec`s that argv - the exit code, signals,
stdin/stdout and TTY are exactly the real CLI's. Before the exec it appends
one line (client, class, tool, tier, model, quota) to `AICLI_USAGE_LOG`, and
`--meta-out` writes the same facts as JSON to `<path>`. The model and effort
are read back from the argv, so a caller's own `--model` is what gets
recorded.

```
aicli resolve [--client <id>] [--class <name>] [--tool ...] [--tier ...] [--quota] [--json]
```
Prints where a call would go (class, tool, tier, model) without running it.

```
aicli routes [--json]
aicli route set-class <name> --tool <claude|agy> [--tier ...]
aicli route set-client <id> [--class <name>] [--tool ...] [--tier ...]
aicli route remove-client <id>
```
Shows and edits `routes.json`.

```
aicli usage [--json]
```
Runs `claude -p /usage` and `agy -p /usage` side by side and parses them
. Exits 0 even when a tool's
state is `unknown`; the state is in the output. `--json` prints
`{"claude": {state, detail, bars}, "agy": {...}}`.

## The service (`aicli-ai`)

An HTTP service on port 8500 speaking the OpenAI chat-completions protocol,
for text callers that can't exec the CLI themselves (ai-job-search, or a
container through the docker bridge). It binds `127.0.0.1` by default. A
non-loopback `AICLI_HOST` needs both `AICLI_ALLOW_NON_LOOPBACK=1` and
`AICLI_API_KEY`, or `server.py` refuses to start. Loopback callers need no
key; any other caller must send `Authorization: Bearer <AICLI_API_KEY>` on
`/v1/*` or gets a 401 (`/healthz` stays open).

- `GET /healthz` - `200 ok`, no CLI run.
- `GET /v1/models` - the ids `claude`, `agy` and `<tool>/<tier>` for every
  tier (8 ids). A bare tool id means its `default` tier.
- `POST /v1/chat/completions` - `model` one of those ids, `messages`
  flattened into one prompt, `reasoning_effort` maps to `--effort`,
  `stream: true|false`. Each request runs the CLI once through the same
  argv builder as `aicli run`, in an empty scratch directory, with no
  permission-skipping flag. At most `AICLI_MAX_CONCURRENT` runs happen at
  once; more wait their turn. Errors: 400 (bad request/unknown model), 413
  (prompt over 120 KiB), 502 (non-zero CLI exit), 504 (timeout).

### Token usage log

Every chat-completions request appends one JSON line per model to
`AICLI_USAGE_LOG` (default `~/.local/state/aicli/usage.jsonl`). `aicli run`
writes to the same file, with its own row shape (client, class, quota, no
token counts):

```
{"at": "2026-10-14T18:20:00+00:00", "tool": "claude", "tier": "default", "stream": false,
 "model": "claude-sonnet-5", "reported": true, "client": "pjm",
 "input": 3, "output": 7, "cache_write": 20, "cache_read": 100, "total": 130}
```

The counts come from the CLI's own final result: claude's `modelUsage` (one
line per model it used), agy's `usage` (often only a `total`). Non-streaming
claude runs use `--output-format json` so their counts can be read; agy without
streaming prints plain text, so its line says `"reported": false`. Failed runs
are logged too. A log that can't be written never fails the request. pjm's
Usage page and homelab's cockpit both read this file.

Every HTTP line also carries `client`: who made the request, for this log
only - it is never used for routing or auth. It is chosen in order: the
`X-Aicli-Client` request header, else the OpenAI `user` field of the request
body, else - for a non-loopback caller that passed the API key check -
the `AICLI_REMOTE_CLIENT` setting, else `"unknown"`. The name is trimmed,
stripped of control characters and cut to 80 characters. For example
`AICLI_REMOTE_CLIENT=karakeep:tagger` labels the calls of a client that sends
neither the header nor the `user` field.

## Running the service

This repo does not install a systemd unit. Run `python3 server.py` with the
settings from `.env.example` in its environment, under whatever supervisor
the host uses. On Zak's OptiPlex the unit and its `.env` live in homelab
(`services/AI/aicliAI`), which runs `server.py` from a checkout of this repo
at `~/my_repos/aicli` and links its config into `~/.config/aicli/`.

## Who uses it

Each caller names itself with `--client` so the usage log and routes can tell
them apart: pjm (ticket runs, maintainers, scrum master), homelab (cockpit
quota bars, the tmux wrapper, auto-heal), social-helper, ai-job-search, and
karakeep through the HTTP service.

aicli is for private tools and automation that run under your own login and
quota. Don't put it behind a public, user-facing app.

## License

MIT, see `LICENSE`.
