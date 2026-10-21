#!/usr/bin/env python3
"""aicli - a proxy for AI coding CLIs (claude, agy, more to come).

It only builds an argv and execs the real, unmodified CLI under the user's
own login. It never reads, stores or forwards OAuth tokens, and it never
calls a vendor backend itself.

The config is `$AICLI_CONFIG`, else `~/.config/aicli/aicli.toml`, else the
`aicli.toml` shipped next to this file (see default_config_path).
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TOOLS = ("claude", "agy")
TIERS = ("low", "default", "deep")
EFFORTS = ("low", "medium", "high")
DEFAULT_USAGE_LOG = Path("~/.local/state/aicli/usage.jsonl").expanduser()
# A tmux wrapper (homelab's tools/optiplex-ai-tmux.sh) symlinks `claude`/`agy` here, so
# a PATH search that walked into them would just exec itself in a loop.
SKIP_DIRS = {"/usr/local/bin", "/usr/local/sbin"}

UNKNOWN = "unknown"
OK = "ok"


USER_CONFIG = Path("~/.config/aicli/aicli.toml")


def default_config_path():
    # A deployment (homelab, say) links its own config to ~/.config/aicli/;
    # without one, the aicli.toml shipped in this repo is the default.
    user_config = USER_CONFIG.expanduser()
    if user_config.is_file():
        return user_config
    # __file__ is the symlink path when run via the ~/.local/bin/aicli link
    # `make install` creates, so resolve it before finding aicli.toml next to
    # the real aicli.py in this repo.
    return Path(__file__).resolve().with_name("aicli.toml")


def load_config(path=None):
    path = Path(path) if path else Path(os.environ.get("AICLI_CONFIG") or default_config_path())
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"aicli: invalid config {path}: {e}", file=sys.stderr)
        raise SystemExit(2)


def resolve_bin(config, tool):
    """The real binary for `tool`: the configured `bin` if executable, else
    the first PATH match that isn't the tmux-wrapper symlink directory."""
    cfg = config[tool]
    configured = os.path.expanduser(cfg.get("bin", ""))
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or d in SKIP_DIRS:
            continue
        candidate = os.path.join(d, tool)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_argv(config, tool, tier="default", effort=None, extra=None, model=None):
    """The argv `run` would exec, or None if the binary can't be found. `model`
    replaces the tier's model and any `--model` the caller passed (a fallback step)."""
    cfg = config[tool]
    bin_path = resolve_bin(config, tool)
    if bin_path is None:
        return None
    extra = list(extra or [])
    if extra and (extra[0] in TOOLS or os.path.basename(extra[0]) in TOOLS):
        extra = extra[1:]

    argv = [bin_path, *cfg.get("args", [])]

    model_flag = cfg.get("model_flag", "")
    if model and model_flag:
        extra = _drop_flag(extra, model_flag)
    model = model or cfg.get("tiers", {}).get(tier)
    added_model = bool(model and model_flag and not _has_flag(extra, model_flag))
    if added_model:
        argv += [model_flag, model]

    effort_flag = cfg.get("effort_flag", "")
    if added_model and cfg.get("model_sets_effort"):
        # agy's model names carry their effort (gemini-3.8-flash-low) and agy
        # refuses an --effort that disagrees, so the tier's model decides.
        effort = None
        extra = _drop_flag(extra, effort_flag)
    if effort and effort_flag and not _has_flag(extra, effort_flag):
        argv += [effort_flag, effort]

    argv += extra
    return argv


def _has_flag(args, flag):
    return any(a == flag or a.startswith(flag + "=") for a in args)


def _flag_value(args, flag):
    """The value of the last `flag v` or `flag=v` in `args`, else None."""
    value = None
    for i, a in enumerate(args):
        if flag and a == flag and i + 1 < len(args):
            value = args[i + 1]
        elif flag and a.startswith(flag + "="):
            value = a[len(flag) + 1:]
    return value


def _drop_flag(args, flag):
    """`args` without `flag` and its value (`--flag v` or `--flag=v`)."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == flag:
            skip = True
        elif not a.startswith(flag + "="):
            out.append(a)
    return out


def _strip_dashdash(args):
    return args[1:] if args[:1] == ["--"] else args


# ---- routing layer ---------------------------------------------------------

def default_routes_path(config=None):
    if os.environ.get("AICLI_ROUTES"):
        return Path(os.environ["AICLI_ROUTES"])
    if config and "routes_file" in config:
        return Path(os.path.expanduser(config["routes_file"]))
    config_file = Path(os.environ.get("AICLI_CONFIG") or default_config_path())
    return config_file.with_name("routes.json")


def load_routes(config=None, routes_path=None):
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    # The [classes.*] tables in aicli.toml are the one source of the defaults;
    # routes.json (the live switchboard, e.g. homelab's cockpit) is layered over them.
    classes = {}
    if config and isinstance(config.get("classes"), dict):
        for name, d in config["classes"].items():
            if isinstance(d, dict):
                classes[name] = dict(d)
    clients = {}

    optiplex_projects = []
    if routes_file.is_file():
        try:
            with open(routes_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("classes"), dict):
                for name, d in data["classes"].items():
                    if isinstance(d, dict):
                        classes.setdefault(name, {}).update(d)
            if isinstance(data.get("clients"), dict):
                for name, d in data["clients"].items():
                    if isinstance(d, dict):
                        clients.setdefault(name, {}).update(d)
            if isinstance(data.get("optiplex_projects"), list):
                optiplex_projects = [str(x) for x in data["optiplex_projects"]]
        except (OSError, json.JSONDecodeError) as e:
            print(f"aicli: warning: could not read {routes_file}: {e}", file=sys.stderr)

    return {"classes": classes, "clients": clients, "optiplex_projects": optiplex_projects}


def save_routes(routes, config=None, routes_path=None):
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    # routes.json is often a link into the deployment's repo
    # (~/.config/aicli/routes.json); write the real file, not over the link.
    routes_file = routes_file.resolve()
    routes_file.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file: the cockpit saves from several threads of one
    # process, so a per-pid name would be shared between two saves.
    fd, tmp = tempfile.mkstemp(dir=routes_file.parent, prefix=routes_file.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(routes, f, indent=2)
            f.write("\n")
        os.replace(tmp, routes_file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def resolve_route(config, client=None, workload_class=None, requested_tool=None,
                  requested_tier=None, requested_effort=None, routes=None):
    if routes is None:
        routes = load_routes(config)
    classes = routes.get("classes") or {}
    clients = routes.get("clients") or {}

    resolved_class = workload_class
    target_tool = None
    target_tier = None
    target_effort = None

    client_entry = None
    if client:
        if client in clients:
            client_entry = clients[client]
        elif ":" in client:
            parts = client.split(":")
            # e.g. pjm:p-0001:auto-execute -> try pjm:auto-execute
            if len(parts) >= 3 and f"{parts[0]}:{parts[-1]}" in clients:
                client_entry = clients[f"{parts[0]}:{parts[-1]}"]
            elif len(parts) >= 4 and f"{parts[0]}:{parts[1]}:{parts[-1]}" in clients:
                client_entry = clients[f"{parts[0]}:{parts[1]}:{parts[-1]}"]
            elif len(parts) >= 3 and f"{parts[0]}:{parts[1]}" in clients:
                client_entry = clients[f"{parts[0]}:{parts[1]}"]

    if client_entry:
        if not resolved_class and "class" in client_entry:
            resolved_class = client_entry["class"]
        target_tool = client_entry.get("tool")
        target_tier = client_entry.get("tier")
        target_effort = client_entry.get("effort")

    if resolved_class:
        if resolved_class not in classes:
            known = ", ".join(sorted(classes.keys()))
            raise ValueError(f"unknown workload class {resolved_class!r} (known: {known})")
        cls_def = classes[resolved_class]
        if not target_tool:
            target_tool = cls_def.get("tool")
        if not target_tier:
            target_tier = cls_def.get("tier", "default")
        if not target_effort:
            target_effort = cls_def.get("effort")

    if requested_tool:
        target_tool = requested_tool
    if requested_tier:
        target_tier = requested_tier
    if requested_effort:
        target_effort = requested_effort

    if not target_tool:
        raise ValueError("either --tool, --class, or a known --client is required")

    if target_tool not in TOOLS:
        raise ValueError(f"unknown tool {target_tool!r} (known: {', '.join(TOOLS)})")

    target_tier = target_tier or "default"
    if target_tier not in TIERS:
        raise ValueError(f"unknown tier {target_tier!r} (known: {', '.join(TIERS)})")

    if target_effort and target_effort not in EFFORTS:
        raise ValueError(f"unknown effort {target_effort!r} (known: {', '.join(EFFORTS)})")

    return target_tool, target_tier, target_effort, resolved_class



# ---- /usage, moved from services/status/usage.py -------------------------

# "Current session: 76% used · resets Aug 25, 1:09am (Europe/Paris)"
_CLAUDE_SESSION_RE = re.compile(
    r"(?im)^Current session:\s*(\d+)%\s*used.*?resets\s+(.+?)\s*$"
)
# "Current week (all models): 6% used · resets Aug 31, 6:59pm (Europe/Paris)"
_CLAUDE_WEEK_RE = re.compile(
    r"(?im)^Current week[^:\n]*:\s*(\d+)%\s*used.*?resets\s+(.+?)\s*$"
)


def _run_usage(bin_path, timeout):
    try:
        return subprocess.run(
            [bin_path, "-p", "/usage"], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _claude_usage(config, timeout):
    bin_path = resolve_bin(config, "claude")
    if bin_path is None:
        return {"state": UNKNOWN, "detail": "claude CLI not found", "bars": {}}

    result = _run_usage(bin_path, timeout)
    if result is None:
        return {"state": UNKNOWN, "detail": "claude -p /usage timed out", "bars": {}}
    if result.returncode != 0:
        return {"state": UNKNOWN, "detail": "claude -p /usage failed", "bars": {}}

    text = result.stdout
    bars = {}
    session = _CLAUDE_SESSION_RE.search(text)
    if session:
        bars["five_hour"] = {"pct": int(session.group(1)), "reset": session.group(2)}
    week = _CLAUDE_WEEK_RE.search(text)
    if week:
        bars["weekly"] = {"pct": int(week.group(1)), "reset": week.group(2)}

    if not bars:
        return {"state": UNKNOWN, "detail": "could not parse /usage output", "bars": {}}
    return {"state": OK, "detail": "", "bars": bars}


# `agy -p "/usage"` prints one tab-separated line per (model group, period):
#   Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-24T21:11:51Z
#   Gemini Models\tFive Hour Limit Remaining\tdisabled\t
# A plan can carry more than one model group - Gemini (the one Antigravity
# actually drives you with) and "Claude and GPT models" (an ancillary,
# separately-metered allowance for picking a different model). They are not
# on the same billing cycle, so Gemini leads when it has a reading for the
# period; the other group only fills in when Gemini doesn't report one.
_AGY_PERIODS = {"Weekly": "weekly", "Five Hour": "five_hour"}
_AGY_PRIMARY_GROUP = "Gemini"


def _agy_usage(config, timeout):
    bin_path = resolve_bin(config, "agy")
    if bin_path is None:
        return {"state": UNKNOWN, "detail": "agy CLI not found", "bars": {}}

    result = _run_usage(bin_path, timeout)
    if result is None:
        return {"state": UNKNOWN, "detail": "agy -p /usage timed out", "bars": {}}
    if result.returncode != 0:
        return {"state": UNKNOWN, "detail": "agy -p /usage failed", "bars": {}}

    bars, groups = {}, {}
    for line in result.stdout.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 3:
            continue
        group, label, value = cols[0], cols[1], cols[2]
        reset = cols[3] if len(cols) > 3 else ""
        period = next(
            (key for prefix, key in _AGY_PERIODS.items() if label.startswith(prefix)),
            None,
        )
        if period is None or not value.endswith("%"):
            continue  # "disabled" - no limit to bar for this group/period
        try:
            remaining = int(value.rstrip("%"))
        except ValueError:
            continue
        used = 100 - remaining
        groups.setdefault(group, {})[period] = {"pct": used, "reset": reset}
        current = bars.get(period)
        is_primary = group.startswith(_AGY_PRIMARY_GROUP)
        was_primary = current is not None and current["group"].startswith(_AGY_PRIMARY_GROUP)
        if current is None or (is_primary and not was_primary):
            bars[period] = {"pct": used, "reset": reset, "group": group}

    if not bars:
        return {"state": UNKNOWN, "detail": "no active limits reported", "bars": {}}
    # Every group's own bars too: a fallback step that runs a Claude model through
    # agy is limited by "Claude and GPT models", not by the Gemini bars above.
    return {"state": OK, "detail": "", "bars": bars, "groups": groups}


def compute_usage(config):
    timeout = config.get("usage_timeout", 30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_result, agy_result = pool.map(
            lambda fn: fn(config, timeout), (_claude_usage, _agy_usage)
        )
    # Remembered for `run`, whose own probe has seconds, not half a minute: the
    # cockpit's poll keeps the cache warm.
    return {"claude": _cached_quota("claude", claude_result), "agy": _cached_quota("agy", agy_result)}


def get_tool_quota(config, tool, timeout=3.0):
    try:
        fn = _claude_usage if tool == "claude" else _agy_usage
        result = fn(config, timeout)
    except Exception:
        result = {"state": UNKNOWN, "bars": {}}
    return _cached_quota(tool, result)


QUOTA_CACHE_MAX_AGE = 30 * 60


def _quota_cache_path():
    return Path(os.environ.get("AICLI_QUOTA_CACHE", DEFAULT_USAGE_LOG.with_name("quota.json"))).expanduser()


def _cached_quota(tool, result):
    """`result`, remembered when it is a reading; when the probe timed out or failed,
    the last reading of the last half hour instead. `run` probes with a short timeout,
    and a fallback that only works when `claude -p /usage` answers in time would
    miss most of the runs it is there for."""
    path = _quota_cache_path()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    now = datetime.now(timezone.utc).timestamp()
    if result.get("state") == OK:
        cache[tool] = {"at": now, "result": result}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        return result
    if result.get("detail", "").endswith("not found"):
        return result       # no CLI to run at all: an old reading would hide that
    hit = cache.get(tool) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and now - hit.get("at", 0) <= QUOTA_CACHE_MAX_AGE:
        return {**hit["result"], "cached_at": datetime.fromtimestamp(hit["at"], timezone.utc).isoformat()}
    return result


# ---- fallback ----------------------------------------------------------------
#
# [fallback] in aicli.toml: once a tool's five-hour or weekly bar reaches `at`
# percent, its runs go to the first step of `[[fallback.<tool>]]` that still has
# room. A step names the tool, the agy quota group that limits it, and the model
# per tier. An unknown reading counts as room: a probe that timed out must not
# move work off a plan that still has quota.

DEFAULT_FALLBACK_AT = 95


def fallback_at(config):
    return int(config.get("fallback", {}).get("at", DEFAULT_FALLBACK_AT))


def fallback_steps(config, tool):
    steps = config.get("fallback", {}).get(tool, [])
    return [s for s in steps if isinstance(s, dict) and s.get("tool") in TOOLS]


def meter(result, group=None):
    """(highest % used across the bars, reset of that bar) for a quota result, or
    (None, "") when there is no reading. `group` picks one agy quota group."""
    if not result or result.get("state") != OK:
        return None, ""
    bars = result.get("groups", {}).get(group, {}) if group else result.get("bars", {})
    readings = [b for b in bars.values() if isinstance(b, dict) and "pct" in b]
    if not readings:
        return None, ""
    top = max(readings, key=lambda b: b["pct"])
    return top["pct"], top.get("reset", "")


def pick_fallback(config, tool, tier, quota):
    """The step to run instead of `tool`'s own `tier` model, or None when `tool` has
    room, has no fallback, or every step is out too. `quota(tool)` returns that
    tool's quota result; it is only asked for the tools the decision needs."""
    steps = fallback_steps(config, tool)
    if not steps:
        return None
    at = fallback_at(config)
    pct, reset = meter(quota(tool))
    if pct is None or pct < at:
        return None
    for step in steps:
        step_pct, _ = meter(quota(step["tool"]), step.get("group"))
        if step_pct is not None and step_pct >= at:
            continue
        model = step.get("tiers", {}).get(tier) or config.get(step["tool"], {}).get("tiers", {}).get(tier)
        return {
            "tool": step["tool"], "model": model, "group": step.get("group"),
            "from_tool": tool, "from_model": config.get(tool, {}).get("tiers", {}).get(tier),
            "reason": f"{tool} quota {pct}% used (fallback at {at}%)" + (f", resets {reset}" if reset else ""),
        }
    return None


def fallback_status(config, usage):
    """Each fallback chain as the cockpit shows it: one row per step (the tool's own
    plan first), its reading, and which step new runs go to now."""
    at = fallback_at(config)
    chains = {}
    for tool in TOOLS:
        steps = fallback_steps(config, tool)
        if not steps:
            continue
        rows = [{"tool": tool, "group": None, "label": f"{tool} plan", "tiers": config.get(tool, {}).get("tiers", {})}]
        rows += [{"tool": s["tool"], "group": s.get("group"),
                  "label": s.get("label") or f"{s['tool']}: {s.get('group') or 'plan'}",
                  "tiers": s.get("tiers") or config.get(s["tool"], {}).get("tiers", {})} for s in steps]
        active = None
        for i, row in enumerate(rows):
            row["pct"], row["reset"] = meter(usage.get(row["tool"]), row["group"])
            row["exhausted"] = row["pct"] is not None and row["pct"] >= at
            if active is None and not row["exhausted"]:
                active = i
        for i, row in enumerate(rows):
            row["active"] = i == active
        chains[tool] = {"at": at, "steps": rows, "active": active, "all_exhausted": active is None}
    return chains


def usage_log_path():
    return Path(os.environ.get("AICLI_USAGE_LOG", DEFAULT_USAGE_LOG)).expanduser()


# ---- command line ----------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="aicli")
    parser.add_argument("--config", help="path to aicli.toml (default: $AICLI_CONFIG, ~/.config/aicli/aicli.toml, or the one in this repo)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_which = sub.add_parser("which")
    p_which.add_argument("tool", choices=TOOLS)

    def add_tool_args(p):
        p.add_argument("--tool", choices=TOOLS)
        p.add_argument("--tier", choices=TIERS)
        p.add_argument("--effort", choices=EFFORTS)
        p.add_argument("--client", help="client identifier (e.g. pjm:maintainer, ai-job-search)")
        p.add_argument("--class", dest="workload_class", help="workload class (e.g. maintenance, standard-dev)")
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-fallback", action="store_true", help="never move to a [fallback] step")
        p.add_argument("args", nargs=argparse.REMAINDER)

    add_tool_args(sub.add_parser("argv"))
    p_run = sub.add_parser("run")
    add_tool_args(p_run)
    p_run.add_argument("--meta-out", help="write execution metadata (model, quota, timestamp) to path before exec")

    p_resolve = sub.add_parser("resolve")
    add_tool_args(p_resolve)
    p_resolve.add_argument("--quota", action="store_true", help="include live quota metadata")

    p_usage = sub.add_parser("usage")
    p_usage.add_argument("--json", action="store_true")

    p_fallback = sub.add_parser("fallback", help="each fallback chain, its readings and the step in use")
    p_fallback.add_argument("--json", action="store_true")

    p_routes = sub.add_parser("routes")
    p_routes.add_argument("--json", action="store_true")

    p_route = sub.add_parser("route")
    route_sub = p_route.add_subparsers(dest="route_command", required=True)
    p_set_cls = route_sub.add_parser("set-class")
    p_set_cls.add_argument("name")
    p_set_cls.add_argument("--tool", required=True, choices=TOOLS)
    p_set_cls.add_argument("--tier", default="default", choices=TIERS)

    p_set_cli = route_sub.add_parser("set-client")
    p_set_cli.add_argument("name")
    p_set_cli.add_argument("--class", dest="workload_class")
    p_set_cli.add_argument("--tool", choices=TOOLS)
    p_set_cli.add_argument("--tier", choices=TIERS)

    p_del_cli = route_sub.add_parser("remove-client")
    p_del_cli.add_argument("name")

    return parser


def cmd_which(args, config):
    bin_path = resolve_bin(config, args.tool)
    if bin_path is None:
        print(f"{args.tool} CLI not found", file=sys.stderr)
        return 1
    print(bin_path)
    return 0


def cmd_argv(args, config):
    extra = _strip_dashdash(args.args)
    try:
        tool, tier, effort, _ = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            requested_tool=args.tool,
            requested_tier=args.tier,
            requested_effort=args.effort,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    tool, fallback = _run_fallback(args, config, tool, tier, _quota_reader(config))
    argv = build_argv(config, tool, tier, effort, extra, model=(fallback or {}).get("model"))
    if argv is None:
        print(f"{tool} CLI not found", file=sys.stderr)
        return 1
    print(json.dumps(argv) if args.json else shlex.join(argv))
    return 0


def cmd_resolve(args, config):
    try:
        tool, tier, effort, resolved_class = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            requested_tool=args.tool,
            requested_tier=args.tier,
            requested_effort=args.effort,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    quota = _quota_reader(config)
    fallback = None if args.no_fallback else pick_fallback(config, tool, tier, quota)
    if fallback:
        tool = fallback["tool"]
    model = (fallback or {}).get("model") or config.get(tool, {}).get("tiers", {}).get(tier, "unknown")
    res = {
        "client": args.client,
        "class": resolved_class,
        "tool": tool,
        "tier": tier,
        "model": model,
        "effort": effort,
        "fallback": fallback,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.quota or args.json:
        res["quota"] = quota(tool)

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        q_str = ""
        if "quota" in res and res["quota"].get("bars"):
            bars = [f"{k}={v['pct']}%" for k, v in res["quota"]["bars"].items()]
            q_str = f" [quota: {', '.join(bars)}]"
        fb = f" [fallback: {fallback['reason']}]" if fallback else ""
        print(f"{resolved_class or 'unclassified'}: {tool}/{tier} ({model}){q_str}{fb}")
    return 0


def _run_fallback(args, config, tool, tier, quota):
    """(tool, fallback) for `run` and `argv`. A caller that pinned --tool built that
    CLI's own flags, which the other CLI would not understand: it asks `aicli
    resolve` first and builds for the answer. A step on the same tool only changes
    the model, so it still applies."""
    fallback = None if args.no_fallback else pick_fallback(config, tool, tier, quota)
    if fallback and args.tool and fallback["tool"] != tool:
        fallback = None
    return (fallback["tool"] if fallback else tool), fallback


def _quota_reader(config):
    """quota(tool), probing each tool at most once."""
    seen = {}

    def quota(tool):
        if tool not in seen:
            seen[tool] = get_tool_quota(config, tool)
        return seen[tool]
    return quota


def cmd_run(args, config):
    extra = _strip_dashdash(args.args)
    try:
        tool, tier, effort, resolved_class = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            requested_tool=args.tool,
            requested_tier=args.tier,
            requested_effort=args.effort,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    quota = _quota_reader(config)
    tool, fallback = _run_fallback(args, config, tool, tier, quota)
    argv = build_argv(config, tool, tier, effort, extra, model=(fallback or {}).get("model"))
    if argv is None:
        print(f"{tool} CLI not found", file=sys.stderr)
        return 1
    # Report what will really run: a caller's own --model/--effort wins over
    # the tier, and agy's tier model can drop the effort (model_sets_effort).
    model = _flag_value(argv, config[tool].get("model_flag", "")) or "unknown"
    effort = _flag_value(argv, config[tool].get("effort_flag", ""))
    quota = quota(tool)

    if getattr(args, "meta_out", None):
        meta = {
            "client": args.client,
            "class": resolved_class,
            "tool": tool,
            "tier": tier,
            "model": model,
            "effort": effort,
            "fallback": fallback,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "quota": quota,
        }
        meta_path = Path(args.meta_out)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = meta_path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, meta_path)

    try:
        usage_log = usage_log_path()
        usage_log.parent.mkdir(parents=True, exist_ok=True)
        with open(usage_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "client": args.client,
                "class": resolved_class,
                "tool": tool,
                "tier": tier,
                "model": model,
                "quota": quota,
                **({"fallback": fallback} if fallback else {}),
            }) + "\n")
    except OSError:
        pass

    os.execv(argv[0], argv)  # never returns


def cmd_usage(args, config):
    data = compute_usage(config)
    if args.json:
        print(json.dumps(data))
        return 0
    for tool, result in data.items():
        line = f"{tool}: {result['state']}"
        if result["detail"]:
            line += f" ({result['detail']})"
        for period, bar in result["bars"].items():
            line += f" {period}={bar['pct']}% resets {bar['reset']}"
        print(line)
    return 0


def cmd_fallback(args, config):
    chains = fallback_status(config, compute_usage(config))
    if args.json:
        print(json.dumps(chains, indent=2))
        return 0
    if not chains:
        print("no [fallback] chains configured")
    for tool, chain in chains.items():
        print(f"{tool} (moves on at {chain['at']}%):")
        for row in chain["steps"]:
            pct = "?" if row["pct"] is None else f"{row['pct']}%"
            mark = "->" if row["active"] else ("xx" if row["exhausted"] else "  ")
            print(f"  {mark} {row['label']:32} {pct:>5}  {row['reset']}")
        if chain["all_exhausted"]:
            print("  every step is out; runs stay on " + tool)
    return 0


def cmd_routes(args, config):
    routes = load_routes(config)
    classes = routes["classes"]
    clients = routes["clients"]
    if args.json:
        out = {"classes": {}, "clients": {}}
        for name, c in classes.items():
            tool = c.get("tool", "claude")
            tier = c.get("tier", "default")
            model = config.get(tool, {}).get("tiers", {}).get(tier, "")
            out["classes"][name] = {**c, "model": model}
        for name, c in clients.items():
            tool = c.get("tool")
            tier = c.get("tier")
            model = config.get(tool, {}).get("tiers", {}).get(tier, "") if tool and tier else ""
            out["clients"][name] = {**c, "model": model}
        print(json.dumps(out, indent=2))
        return 0

    print("WORKLOAD CLASSES:")
    for name, c in sorted(classes.items()):
        tool = c.get("tool", "claude")
        tier = c.get("tier", "default")
        model = config.get(tool, {}).get("tiers", {}).get(tier, "")
        print(f"  {name:20} -> {tool:6} (tier: {tier:7}, model: {model})")
        if c.get("description"):
            print(f"    {c['description']} [{c.get('frequency', '')}]")
    if clients:
        print("\nCLIENT OVERRIDES:")
        for name, c in sorted(clients.items()):
            route_str = f"class: {c.get('class', 'none')}"
            if c.get("tool"):
                route_str += f", tool: {c['tool']}"
            if c.get("tier"):
                route_str += f", tier: {c['tier']}"
            print(f"  {name:20} -> {route_str}")
    return 0


def cmd_route(args, config):
    routes = load_routes(config)
    if args.route_command == "set-class":
        if args.name not in routes["classes"]:
            routes["classes"][args.name] = {"description": "", "frequency": ""}
        routes["classes"][args.name]["tool"] = args.tool
        routes["classes"][args.name]["tier"] = args.tier
        save_routes(routes, config)
        print(f"route updated: class {args.name} -> {args.tool}/{args.tier}")
        return 0
    elif args.route_command == "set-client":
        if args.workload_class and args.workload_class not in routes["classes"]:
            known = ", ".join(sorted(routes["classes"]))
            print(f"aicli: unknown workload class {args.workload_class!r} (known: {known})", file=sys.stderr)
            return 2
        entry = routes["clients"].setdefault(args.name, {})
        if args.workload_class:
            entry["class"] = args.workload_class
        if args.tool:
            entry["tool"] = args.tool
        if args.tier:
            entry["tier"] = args.tier
        save_routes(routes, config)
        print(f"route updated: client {args.name} -> {entry}")
        return 0
    elif args.route_command == "remove-client":
        if args.name in routes["clients"]:
            del routes["clients"][args.name]
            save_routes(routes, config)
            print(f"client {args.name} removed from routes")
        else:
            print(f"client {args.name} not in routes", file=sys.stderr)
            return 1
        return 0
    return 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    handler = {
        "which": cmd_which, "argv": cmd_argv, "run": cmd_run, "resolve": cmd_resolve,
        "usage": cmd_usage, "fallback": cmd_fallback, "routes": cmd_routes, "route": cmd_route,
    }[args.command]
    return handler(args, config)


if __name__ == "__main__":
    sys.exit(main())

