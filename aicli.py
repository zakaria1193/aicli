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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOOLS = ("claude", "agy")
TIERS = ("low", "default", "deep")
EFFORTS = ("low", "medium", "high")
AVAILABLE_MODELS = {
    "claude": [
        "claude-haiku-5-5",
        "claude-sonnet-5",
        "claude-opus-5-5",
    ],
    "agy": [
        "gemini-4-hydrogen-low",
        "gemini-4-hydrogen-medium",
        "gemini-4-hydrogen-high",
        "gemini-4-hydrogen-xhigh",
        "gemini-4-argon-low",
        "gemini-4-argon-medium",
        "gemini-4-argon-high",
        "gemini-4-neon-low",
        "gemini-4-neon-medium",
        "gemini-4-neon-high",
        "gemini-4-neon-xhigh",
        "gemini-3.8-flash-low",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-high",
        "claude-sonnet-5-5-low",
        "claude-sonnet-5-5-medium",
        "claude-sonnet-5-5-high",
        "claude-opus-5-5-low",
        "claude-opus-5-5-medium",
        "claude-opus-5-5-high",
        "gpt-oss-120b-medium",
        "gemini-pro-orchestrator-neon",
        "gemini-pro-orchestrator-g1-pro",
    ],
}
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


def infer_agy_group(model):
    """Quota group in `agy -p /usage` that meters `model`."""
    if not model:
        return "Gemini Models"
    m = str(model).lower()
    if m.startswith("gemini-4-argon"):
        return "Gemini 4 Argon"
    if m.startswith("gemini-"):
        return "Gemini Models"
    if m.startswith(("claude-", "gpt-")):
        return "Claude and GPT models"
    return "Gemini Models"


OUTPUT_FORMATS = ("json", "stream-json")
PERMISSION_MODES = ("dontAsk", "auto", "bypass")
JOB_FIELDS = ("prompt", "system_prompt_file", "session_id", "resume", "conversation", "name", "settings",
              "mcp_config", "permission_mode", "output_format", "oneshot", "interactive", "remote_control",
              "chrome")


class JobError(ValueError):
    pass


def job_from_args(args):
    """The tool-neutral job the caller described (None when it passed none). A caller
    never names a CLI: aicli builds the command for whichever one the router picks."""
    job = {k: getattr(args, k, None) for k in JOB_FIELDS}
    if getattr(args, "prompt_file", None):
        if job["prompt"] is not None:
            raise JobError("pass either --prompt or --prompt-file, not both")
        try:
            job["prompt"] = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            raise JobError(f"cannot read --prompt-file: {e}")
    job = {k: v for k, v in job.items() if v}
    if job.get("session_id") and job.get("resume"):
        raise JobError("pass either --session-id or --resume, not both")
    if job.get("oneshot") and job.get("interactive"):
        raise JobError("pass either --oneshot or --interactive, not both")
    return job or None


def _job_claude(job, extra):
    out = []
    streaming = job.get("output_format") == "stream-json"
    if job.get("oneshot") or job.get("output_format"):
        out.append("-p")
    if job.get("output_format"):
        out += ["--output-format", job["output_format"]]
        if streaming:
            out += ["--verbose", "--include-partial-messages"]
    if job.get("resume"):
        out += ["--resume", job["resume"]]
    if job.get("session_id"):
        out += ["--session-id", job["session_id"]]
    if job.get("name"):
        out += ["--name", job["name"]]
    if job.get("remote_control"):
        out.append("--remote-control")
    if job.get("chrome"):
        out.append("--chrome")
    if job.get("mcp_config"):
        # --mcp-config swallows every argument up to the next option: it must
        # never come right before the prompt.
        out += ["--strict-mcp-config", "--mcp-config", job["mcp_config"]]
    if job.get("permission_mode"):
        out += ["--permission-mode", "bypassPermissions" if job["permission_mode"] == "bypass" else job["permission_mode"]]
    if job.get("system_prompt_file"):
        out += ["--append-system-prompt-file", job["system_prompt_file"]]
    if job.get("settings"):
        out += ["--settings", job["settings"]]
    out += extra
    if job.get("prompt"):
        # a leading "-" would be read as an option
        out.append(" " + job["prompt"] if job["prompt"].startswith("-") else job["prompt"])
    return out


def _job_agy(job, extra):
    """agy runs to completion (`-p`) unless the job is interactive (`-i`: it stays open for a
    human). It has no append-prompt, session, hook, MCP or permission flags, so the system
    prompt joins the prompt and those fields are left out: a caller that depends on them reads
    the exit code and the output instead. A conversation agy already has (`--conversation`)
    got the system prompt when it began, so it is not added again."""
    prompt = job.get("prompt") or ""
    if job.get("system_prompt_file") and not job.get("conversation"):
        try:
            system = Path(job["system_prompt_file"]).read_text(encoding="utf-8").rstrip()
        except OSError as e:
            raise JobError(f"cannot read --system-prompt-file: {e}")
        prompt = f"{system}\n\n{prompt}" if prompt else system
    if not prompt and job.get("interactive"):
        prompt = "The human opened your window. Say in one line that you are ready."
    out = ["-i" if job.get("interactive") else "-p", prompt]
    if job.get("permission_mode") == "bypass":
        out.append("--dangerously-skip-permissions")
    if job.get("output_format"):
        out += ["--output-format", job["output_format"]]
    if job.get("conversation"):
        out += ["--conversation", job["conversation"]]
    if job.get("remote_control"):
        out.append("--remote-control")
    return out + extra


JOB_BUILDERS = {"claude": _job_claude, "agy": _job_agy}


def build_argv(config, tool, tier="default", effort=None, extra=None, model=None, job=None, skill_flags=None):
    """The argv `run` would exec, or None if the binary can't be found. `model`
    replaces the tier's model and any `--model` the caller passed (a fallback step).
    `job` is the tool-neutral request (see job_from_args), built into this tool's flags.
    `skill_flags` (apply_skills) go right after the model and effort."""
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

    argv += list(skill_flags or [])
    argv += JOB_BUILDERS[tool](job, extra) if job else extra
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


# ---- skills ------------------------------------------------------------------
# aicli loads Agent Skills (a folder holding a SKILL.md) into each run natively:
# claude gets them as a per-run plugin (`--plugin-dir`), agy finds them in its own
# skills folder. Which skills a run gets comes from its class (and the CLI the
# router picked), plus any `--skill` the caller adds. The catalog is
# skills/skills.toml: each skill's public source, and the default skills per class.
# A cached copy of every skill is committed in skills/vendor/; `aicli skills update`
# (run by `make install`, so on every upgrade) fetches the latest into the cache dir,
# which wins over the vendored copy. A skill that can't be found never fails a run.

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
DEFAULT_SKILLS_CACHE = Path("~/.cache/aicli/skills")
AGY_SKILL_PREFIX = "aicli-"
SKILL_HASH_FILE = ".aicli-hash"
SKILLS_NOTE = "Skills loaded for this job (use the ones that fit): {names}."


def skills_catalog_path(config=None):
    if os.environ.get("AICLI_SKILLS"):
        return Path(os.environ["AICLI_SKILLS"])
    if config and config.get("skills_file"):
        return Path(config["skills_file"]).expanduser()
    return SKILLS_ROOT / "skills.toml"


def load_skills_catalog(config=None):
    path = skills_catalog_path(config)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"aicli: warning: skills catalog {path} unreadable, no skills loaded: {e}", file=sys.stderr)
        return {}


def skills_cache_dir():
    return Path(os.environ.get("AICLI_SKILLS_CACHE") or DEFAULT_SKILLS_CACHE).expanduser()


def skills_vendor_dir(catalog_path=None):
    return Path(catalog_path or skills_catalog_path()).parent / "vendor"


def find_skill(name, catalog_path=None):
    """The folder holding skill `name`: the fetched copy in the cache, else the
    vendored one next to the catalog, else None. A path to a folder with a
    SKILL.md (a caller's own skill, `--skill ./my-skill`) is used as is."""
    if "/" in name:
        p = Path(name).expanduser()
        return p.resolve() if (p / "SKILL.md").is_file() else None
    for root in (skills_cache_dir(), skills_vendor_dir(catalog_path)):
        if (root / name / "SKILL.md").is_file():
            return root / name
    return None


def _skill_names(spec, tool):
    """A class's `skills`: a list (every CLI) or {all = [...], claude = [...], agy = [...]}."""
    if isinstance(spec, list):
        return [str(s) for s in spec]
    if isinstance(spec, dict):
        return [str(s) for s in [*(spec.get("all") or []), *(spec.get(tool) or [])]]
    return []


def class_skill_names(config, workload_class, tool, routes=None, catalog=None):
    """The skill names a run of `workload_class` on `tool` loads. A `skills` key on the
    class (aicli.toml or routes.json, even an empty list) replaces the catalog's default."""
    if not workload_class:
        return []
    cls = _class_cfg(config, workload_class, routes)
    if "skills" in cls:
        return _skill_names(cls["skills"], tool)
    catalog = load_skills_catalog(config) if catalog is None else catalog
    return _skill_names((catalog.get("classes") or {}).get(workload_class), tool)


def resolve_skills(config, workload_class, tool, extra=(), routes=None):
    """[(name, folder)] for this run, in order, without duplicates. Unknown skills are
    reported on stderr and left out."""
    catalog_path = skills_catalog_path(config)
    names = class_skill_names(config, workload_class, tool, routes) + list(extra or [])
    out, seen = [], set()
    for n in names:
        folder = find_skill(n, catalog_path)
        if folder is None:
            print(f"aicli: warning: skill {n!r} not found, run goes on without it", file=sys.stderr)
            continue
        name = folder.name
        if name not in seen:
            seen.add(name)
            out.append((name, folder))
    return out


def tree_hash(folder):
    """sha256 of a folder's files (relative path and bytes), aicli's own marker excluded."""
    import hashlib
    h = hashlib.sha256()
    folder = Path(folder)
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name != SKILL_HASH_FILE and ".git" not in p.relative_to(folder).parts:
            h.update(str(p.relative_to(folder)).encode() + b"\0")
            h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def _copy_skill(src, dest):
    """Copy a skill folder to `dest` atomically (symlinks followed, .git left out)."""
    import shutil
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    shutil.rmtree(tmp)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(".git"))
    old = None
    if dest.exists():
        old = dest.with_name(f".{dest.name}.old.{os.getpid()}")
        os.replace(dest, old)
    os.replace(tmp, dest)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def stage_claude_plugin(skills):
    """A plugin folder holding exactly `skills`, for claude's `--plugin-dir`. It is
    content-addressed under the cache, so a run reuses it until a skill changes."""
    import hashlib
    key = hashlib.sha256("\n".join(f"{n}:{tree_hash(f)}" for n, f in skills).encode()).hexdigest()[:16]
    root = skills_cache_dir().parent / "plugins" / key
    if (root / ".claude-plugin" / "plugin.json").is_file():
        return root
    build = Path(tempfile.mkdtemp(prefix=".plugin.", dir=_mkdir(root.parent)))
    (build / ".claude-plugin").mkdir()
    (build / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "aicli", "version": "1.0.0",
        "description": "Skills aicli loads for this workload class",
    }, indent=2) + "\n")
    import shutil
    for name, folder in skills:
        shutil.copytree(folder, build / "skills" / name, ignore=shutil.ignore_patterns(".git"))
    try:
        os.replace(build, root)
    except OSError:  # another run staged the same set first
        shutil.rmtree(build, ignore_errors=True)
    return root


def _mkdir(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


def agy_skills_dir(config):
    """agy's own skills folder: `[agy] skills_dir`, else the global `config/skills` folder
    beside the CLI's settings folder (trust_file's parent's parent), else None. agy reads
    its global skills from there, not from the CLI's settings folder."""
    agy = config.get("agy") or {}
    path = os.environ.get("AICLI_AGY_SKILLS_DIR") or agy.get("skills_dir")
    if not path and agy.get("trust_file"):
        path = str(Path(agy["trust_file"]).expanduser().parent.parent / "config" / "skills")
    return Path(path).expanduser() if path else None


def install_agy_skills(config, skills):
    """agy has no per-run skills flag, so aicli keeps each skill in agy's own skills
    folder (as aicli-<name>) and refreshes it when its content changed. agy lists every
    skill there by its description only; the prompt names the ones this job should use."""
    root = agy_skills_dir(config)
    if root is None:
        print("aicli: warning: no agy skills folder ([agy] skills_dir), agy runs without skills", file=sys.stderr)
        return
    for name, folder in skills:
        dest = root / f"{AGY_SKILL_PREFIX}{name}"
        digest = tree_hash(folder)
        try:
            if (dest / SKILL_HASH_FILE).read_text().strip() == digest:
                continue
        except OSError:
            pass
        try:
            _copy_skill(folder, dest)
            (dest / SKILL_HASH_FILE).write_text(digest + "\n")
        except OSError as e:
            print(f"aicli: warning: could not install skill {name} for agy: {e}", file=sys.stderr)


def apply_skills(config, tool, job, skills):
    """(job, extra flags) once this run's skills are in place for `tool`. The prompt, when
    there is one, gets a line naming the skills so the model knows they are for this job."""
    if not skills:
        return job, []
    flags = []
    if tool == "claude":
        try:
            flags = ["--plugin-dir", str(stage_claude_plugin(skills))]
        except OSError as e:
            print(f"aicli: warning: could not stage skills for claude: {e}", file=sys.stderr)
            return job, []
    elif tool == "agy":
        install_agy_skills(config, skills)
    if job and job.get("prompt"):
        names = ", ".join(n for n, _ in skills)
        job = {**job, "prompt": f"{job['prompt']}\n\n{SKILLS_NOTE.format(names=names)}"}
    return job, flags


def run_skills(args, config, tool, resolved_class, routes, job):
    """apply_skills for `argv`/`run`: the class's skills for `tool` plus --skill, unless
    --no-skills. Returns (job, flags, names of the skills loaded)."""
    if getattr(args, "no_skills", False):
        return job, [], []
    skills = resolve_skills(config, resolved_class, tool, getattr(args, "skill", None) or [], routes)
    job, flags = apply_skills(config, tool, job, skills)
    return job, flags, [n for n, _ in skills]


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def update_skills(catalog, dest, only=None):
    """Fetch every catalog skill (or `only` these) from its source into `dest`, one shallow
    sparse clone per source repo. A skill whose fetch fails keeps its current copy.
    Returns [{name, status, commit, sha256}] and writes dest/skills.lock.json."""
    import shutil
    dest = _mkdir(Path(dest))
    lock_path = dest / "skills.lock.json"
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError):
        lock = {}
    sources = catalog.get("sources") or {}
    by_source = {}
    for name, s in (catalog.get("skills") or {}).items():
        if only and name not in only:
            continue
        by_source.setdefault(s.get("source"), []).append((name, s))
    report = []
    for src_name, items in by_source.items():
        repo = (sources.get(src_name) or {}).get("repo")
        ref = (sources.get(src_name) or {}).get("ref")
        tmp = Path(tempfile.mkdtemp(prefix="aicli-skills-"))
        try:
            if not repo:
                raise RuntimeError(f"source {src_name!r} has no repo")
            clone = ["clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse"]
            if ref:
                clone += ["--branch", ref]
            p = _git(*clone, repo, str(tmp / "r"))
            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip() or "git clone failed")
            p = _git("sparse-checkout", "set", *[s["path"] for _, s in items], cwd=tmp / "r")
            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip() or "git sparse-checkout failed")
            commit = _git("rev-parse", "HEAD", cwd=tmp / "r").stdout.strip()
            # keep the source's license next to the copies (cone mode checks out root files)
            for lic in sorted((tmp / "r").glob("LICEN[SC]E*"))[:1]:
                _mkdir(dest / "licenses")
                shutil.copyfile(lic, dest / "licenses" / f"{src_name}.txt")
            for name, s in items:
                src = tmp / "r" / s["path"]
                if not (src / "SKILL.md").is_file():
                    report.append({"name": name, "status": f"failed: no SKILL.md at {s['path']}"})
                    continue
                digest = tree_hash(src)
                old = (lock.get(name) or {}).get("sha256")
                if old != digest or not (dest / name / "SKILL.md").is_file():
                    _copy_skill(src, dest / name)
                lock[name] = {"source": src_name, "repo": repo, "path": s["path"], "commit": commit,
                              "sha256": digest, "license": (sources.get(src_name) or {}).get("license", "")}
                report.append({"name": name, "status": "unchanged" if old == digest else ("new" if old is None else "updated"),
                               "commit": commit, "sha256": digest})
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            for name, _ in items:
                report.append({"name": name, "status": f"failed: {e}"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return report


def cmd_skills(args, config):
    catalog = load_skills_catalog(config)
    if args.skills_command == "update":
        dest = skills_vendor_dir(skills_catalog_path(config)) if args.vendor else skills_cache_dir()
        report = update_skills(catalog, dest, only=set(args.only) if args.only else None)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for r in report:
                print(f"{r['name']}: {r['status']}" + (f" ({r['commit'][:10]})" if r.get("commit") else ""))
            print(f"[OK] skills in {dest}")
        # an upgrade must never fail because a skill source is unreachable
        return 0
    # list
    classes = sorted({*(catalog.get("classes") or {}), *(load_routes(config).get("classes") or {})})
    if args.workload_class:
        classes = [args.workload_class]
    rows = []
    for cls in classes:
        for tool in ([args.tool] if args.tool else TOOLS):
            names = class_skill_names(config, cls, tool, catalog=catalog)
            rows.append({"class": cls, "tool": tool, "skills": [
                {"name": n, "path": str(find_skill(n, skills_catalog_path(config)) or "")} for n in names]})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            if r["skills"]:
                print(f"{r['class']} ({r['tool']}): " + ", ".join(
                    s["name"] + ("" if s["path"] else " [missing]") for s in r["skills"]))
    return 0


# ---- routing layer ---------------------------------------------------------

DEFAULT_ROUTES_PATH = DEFAULT_USAGE_LOG.with_name("routes.json")


def default_routes_path(config=None):
    if os.environ.get("AICLI_ROUTES"):
        return Path(os.environ["AICLI_ROUTES"])
    if config and "routes_file" in config:
        return Path(os.path.expanduser(config["routes_file"]))
    return DEFAULT_ROUTES_PATH


def _default_sections(config):
    """The (classes, clients, optiplex_projects) defaults from aicli.toml, each a
    fresh copy so a caller can merge overrides into it without mutating `config`."""
    classes, clients = {}, {}
    for section, out in (("classes", classes), ("clients", clients)):
        if config and isinstance(config.get(section), dict):
            for name, d in config[section].items():
                if isinstance(d, dict):
                    out[name] = dict(d)
    optiplex_projects = [str(x) for x in config.get("optiplex_projects", [])] if config else []
    return classes, clients, optiplex_projects


def _read_state_file(routes_file):
    """The untracked state file's own content (overrides only, never merged
    with the defaults) - what a diff-only write reads before changing one row."""
    if not routes_file.is_file():
        return {}
    try:
        with open(routes_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"aicli: warning: could not read {routes_file}: {e}", file=sys.stderr)
        return {}


def load_routes(config=None, routes_path=None, now=None):
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    # The [classes.*]/[clients.*] tables in aicli.toml are the one source of the
    # defaults; the untracked state file (the live switchboard, e.g. homelab's
    # cockpit) is layered over them, field by field.
    classes, clients, optiplex_projects = _default_sections(config)
    state = _read_state_file(routes_file)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    if isinstance(state.get("classes"), dict):
        for name, d in state["classes"].items():
            if isinstance(d, dict):
                d_clean = dict(d)
                exp = d_clean.get("temporary_override_expires_at") or d_clean.get("model_expires_at")
                if exp and isinstance(exp, str):
                    exp_dt = parse_reset_time(exp, now=now_dt)
                    if exp_dt is not None and now_dt >= exp_dt:
                        d_clean.pop("temporary_override", None)
                        d_clean.pop("temporary_override_expires_at", None)
                        d_clean.pop("model", None)
                        d_clean.pop("model_expires_at", None)
                classes.setdefault(name, {}).update(d_clean)
    if isinstance(state.get("clients"), dict):
        for name, d in state["clients"].items():
            if isinstance(d, dict):
                d_clean = dict(d)
                exp = d_clean.get("temporary_override_expires_at") or d_clean.get("model_expires_at")
                if exp and isinstance(exp, str):
                    exp_dt = parse_reset_time(exp, now=now_dt)
                    if exp_dt is not None and now_dt >= exp_dt:
                        d_clean.pop("temporary_override", None)
                        d_clean.pop("temporary_override_expires_at", None)
                        d_clean.pop("model", None)
                        d_clean.pop("model_expires_at", None)
                clients.setdefault(name, {}).update(d_clean)
    if isinstance(state.get("optiplex_projects"), list):
        optiplex_projects = [str(x) for x in state["optiplex_projects"]]

    return {"classes": classes, "clients": clients, "optiplex_projects": optiplex_projects}


def _overridden_names(config=None, routes_path=None, now=None):
    """(overridden classes, overridden clients, stale classes, stale clients):
    which rows carry a state-file override, and which of those overrides refer
    to a row no longer in aicli.toml (ignored by load_routes unless it is a
    whole added row). Expired temporary model overrides are ignored."""
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    state = _read_state_file(routes_file)
    default_classes, default_clients, _ = _default_sections(config)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    overridden = {"classes": set(), "clients": set()}
    stale = {"classes": set(), "clients": set()}
    for section, defaults in (("classes", default_classes), ("clients", default_clients)):
        for name, d in (state.get(section) or {}).items():
            if not isinstance(d, dict):
                continue
            d_active = dict(d)
            exp = d_active.get("temporary_override_expires_at") or d_active.get("model_expires_at")
            if exp and isinstance(exp, str):
                exp_dt = parse_reset_time(exp, now=now_dt)
                if exp_dt is not None and now_dt >= exp_dt:
                    d_active.pop("temporary_override", None)
                    d_active.pop("temporary_override_expires_at", None)
                    d_active.pop("model", None)
                    d_active.pop("model_expires_at", None)
            if not d_active and name in defaults:
                continue
            overridden[section].add(name)
            if name not in defaults and not _is_full_row(section, d_active):
                stale[section].add(name)
    return overridden, stale


def _is_full_row(section, d):
    """True when `d` (a state-file row for a name absent from aicli.toml) has
    enough to stand on its own, so it is an added row rather than a stale
    leftover field-override for a deleted default."""
    if section == "clients":
        return bool(d.get("class") or d.get("tool"))
    return bool(d.get("tool"))


def _diff_from_default(default, merged):
    """The fields of `merged` that differ from `default` - the one row of a
    diff-only write. Empty once every field has been set back to its default."""
    return {k: v for k, v in merged.items() if default.get(k) != v}


def apply_route_override(section, name, updates, config=None, routes_path=None):
    """Merge `updates` into `name`'s row in `section` ("classes"/"clients") and
    write only the resulting diff from aicli.toml's default for that row into
    the untracked state file - never the merged result. A row absent from
    aicli.toml is written in full (there is nothing to diff against); setting
    every field of a default row back to its default drops the override."""
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    state = _read_state_file(routes_file)
    default_classes, default_clients, _ = _default_sections(config)
    defaults = default_classes if section == "classes" else default_clients

    state.setdefault(section, {})
    default_row = defaults.get(name, {})
    current_override = state[section].get(name) or {}
    merged = {**default_row, **current_override, **updates}

    if name in defaults:
        diff = _diff_from_default(default_row, merged)
        if diff:
            state[section][name] = diff
        else:
            state[section].pop(name, None)
    else:
        state[section][name] = merged

    save_routes(state, config, routes_path=routes_file)
    return merged


def reset_route_override(section, name, config=None, routes_path=None, field=None):
    """Drop `name`'s override in `section` - one `field` of it, or the whole
    row when `field` is None - from the state file, leaving aicli.toml's
    default (or, for a row not in aicli.toml, every other field) untouched.
    True when something was actually dropped."""
    routes_file = Path(routes_path) if routes_path else default_routes_path(config)
    state = _read_state_file(routes_file)
    row = (state.get(section) or {}).get(name)
    if not isinstance(row, dict):
        return False
    if field is None:
        del state[section][name]
    elif field in row or (field in ("model", "temporary_override") and any(k in row for k in ("model", "temporary_override", "model_expires_at", "temporary_override_expires_at"))):
        row.pop(field, None)
        if field in ("model", "temporary_override"):
            row.pop("model", None)
            row.pop("model_expires_at", None)
            row.pop("temporary_override", None)
            row.pop("temporary_override_expires_at", None)
    else:
        return False
    save_routes(state, config, routes_path=routes_file)
    return True


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


def _client_keys(client):
    """The `clients` keys that can match `client`, most specific first."""
    keys = [client]
    if ":" in client:
        parts = client.split(":")
        # e.g. pjm:p-0001:auto-execute -> try pjm:auto-execute
        if len(parts) >= 3:
            keys.append(f"{parts[0]}:{parts[-1]}")
        if len(parts) >= 4:
            keys.append(f"{parts[0]}:{parts[1]}:{parts[-1]}")
        if len(parts) >= 3:
            keys.append(f"{parts[0]}:{parts[1]}")
    return keys


def find_client_entry(clients, client, workload_class=None):
    """The routes.json `clients` entry for `client`. With a class, a `<key>@<class>` entry
    (e.g. `pjm:auto-spec@deep-spec`) is tried before `<key>`, so the same client can be
    routed one way for one class and another way for the next."""
    if not client:
        return None
    keys = _client_keys(client)
    if workload_class:
        keys = [k for key in keys for k in (f"{key}@{workload_class}", key)]
    for key in keys:
        if key in clients:
            return clients[key]
    return None


def route_model(routes, client, workload_class=None, now=None):
    """The `model` a routes.json client or class entry pins, else None.
    Temporary model overrides last up to 5 hours; expired overrides are ignored."""
    entry = find_client_entry((routes or {}).get("clients") or {}, client, workload_class)
    if entry:
        m = active_model_override(entry, now=now)
        if m:
            return m
    if entry and (entry.get("tool") or entry.get("tier")):
        return None
    cls_name = workload_class or (entry or {}).get("class")
    if cls_name:
        cls_def = ((routes or {}).get("classes") or {}).get(cls_name) or {}
        m = active_model_override(cls_def, now=now)
        if m:
            return m
    return None


def resolve_route(config, client=None, workload_class=None, routes=None):
    if routes is None:
        routes = load_routes(config)
    classes = routes.get("classes") or {}
    clients = routes.get("clients") or {}

    resolved_class = workload_class
    target_tool = None
    target_tier = None
    target_effort = None

    client_entry = find_client_entry(clients, client, workload_class)

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

    if not target_tool:
        raise ValueError("either --class or a known --client is required")

    if target_tool not in TOOLS:
        raise ValueError(f"unknown tool {target_tool!r} (known: {', '.join(TOOLS)})")

    target_tier = target_tier or "default"
    if target_tier not in TIERS:
        raise ValueError(f"unknown tier {target_tier!r} (known: {', '.join(TIERS)})")

    if target_effort and target_effort not in EFFORTS:
        raise ValueError(f"unknown effort {target_effort!r} (known: {', '.join(EFFORTS)})")

    return target_tool, target_tier, target_effort, resolved_class


# ---- /usage & reset time parsing -------------------------------------------

# "Current session: 76% used · resets Aug 25, 1:09am (Europe/Paris)"
_CLAUDE_SESSION_RE = re.compile(
    r"(?im)^Current session:\s*(\d+)%\s*used.*?resets\s+(.+?)\s*$"
)
# "Current week (all models): 6% used · resets Aug 31, 6:59pm (Europe/Paris)"
_CLAUDE_WEEK_RE = re.compile(
    r"(?im)^Current week[^:\n]*:\s*(\d+)%\s*used.*?resets\s+(.+?)\s*$"
)
_CLAUDE_RESET_RE = re.compile(
    r"(?i)^(?:([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*)\))?$"
)
_AGY_TTY_RESET_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s*([A-Za-z0-9_+/:-]+)?$"
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_TZ_ABBR_OFFSETS = {
    "UTC": timezone.utc,
    "Z": timezone.utc,
    "GMT": timezone.utc,
    "CET": timezone(timedelta(hours=1)),
    "CEST": timezone(timedelta(hours=2)),
}


def parse_reset_time(reset_str, now=None):
    """Parse a CLI reset string into a timezone-aware UTC datetime, or None.

    Supports:
    - ISO 8601: '2026-10-23T22:43:51Z', '2026-10-23T22:43:51+00:00'
    - agy TTY: '2026-10-24 00:43 CEST', '2026-10-28 22:54 CET'
    - claude: 'Oct 22, 3:49am (Europe/Paris)', 'Oct 26, 6pm (Europe/Paris)', '5am (Europe/Paris)'
    """
    if not reset_str or not isinstance(reset_str, str):
        return None
    s = reset_str.strip()
    if not s:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. ISO 8601
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 2. agy TTY date ("2026-10-24 00:43 CEST")
    m = _AGY_TTY_RESET_RE.match(s)
    if m:
        year, month, day, hour, minute, sec, tz_name = m.groups()
        tz = _TZ_ABBR_OFFSETS.get((tz_name or "UTC").upper())
        if tz is None and tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                tz = timezone.utc
        tz = tz or timezone.utc
        try:
            dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(sec or 0), tzinfo=tz)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    # 3. Claude human format ("Oct 22, 3:49am (Europe/Paris)" or "5am (Europe/Paris)")
    m = _CLAUDE_RESET_RE.match(s)
    if m:
        month_name, day, hour_s, min_s, meridiem, zone = m.groups()
        tz = timezone.utc
        if zone:
            try:
                tz = ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                tz = _TZ_ABBR_OFFSETS.get(zone.upper(), timezone.utc)
        hour, minute = int(hour_s), int(min_s or 0)
        if meridiem:
            if not 1 <= hour <= 12 or minute > 59:
                return None
            hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
        elif hour > 23 or minute > 59:
            return None
        local_now = now.astimezone(tz)
        if month_name is None:
            dt = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= local_now:
                dt += timedelta(days=1)
            return dt.astimezone(timezone.utc)
        key = month_name[:3].lower()
        if key not in _MONTHS:
            return None
        try:
            dt = datetime(local_now.year, _MONTHS.index(key) + 1, int(day), hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if dt < local_now - timedelta(days=180):
            dt = dt.replace(year=dt.year + 1)
        return dt.astimezone(timezone.utc)

    return None


def compute_model_override_expires_at(now=None):
    """Compute ISO-8601 `expires_at` for a temporary model override (up to 5 hours)."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return (now_dt + timedelta(hours=5)).isoformat()


def active_model_override(entry, now=None):
    """Return the active model name for a client or class entry if a temporary
    model override (temporary_override) is present and has not expired (`now < expires_at`), else None."""
    if not isinstance(entry, dict):
        return None
    model = entry.get("temporary_override") or entry.get("model")
    if not model or not isinstance(model, str) or not model.strip():
        return None
    exp = entry.get("temporary_override_expires_at") or entry.get("model_expires_at")
    if exp and isinstance(exp, str):
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        exp_dt = parse_reset_time(exp, now=now_dt)
        if exp_dt is not None and now_dt >= exp_dt:
            return None
    return model.strip()


def next_ok_time(result, at=95, group=None, now=None):
    """ISO UTC timestamp when all currently exhausted (>= `at`%) bars for `result`
    (or `group`) will have reset, or None if not exhausted or reset time is unparseable."""
    if not result or result.get("state") != OK:
        return None
    bars = result.get("groups", {}).get(group, {}) if group else result.get("bars", {})
    exhausted = [b for b in bars.values() if isinstance(b, dict) and b.get("pct", 0) >= at]
    if not exhausted:
        return None
    dts = []
    for b in exhausted:
        dt = parse_reset_time(b.get("reset"), now=now)
        if dt is None:
            return None
        dts.append(dt)
    return max(dts).isoformat() if dts else None


def _run_usage(bin_path, timeout):
    try:
        return subprocess.run(
            [bin_path, "-p", "/usage"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def parse_claude_usage(text):
    """Parse `claude -p /usage` stdout into {state, detail, bars, warnings?}."""
    bars = {}
    warnings = []
    session = _CLAUDE_SESSION_RE.search(text or "")
    if session:
        bars["five_hour"] = {"pct": int(session.group(1)), "reset": session.group(2).strip()}
    week = _CLAUDE_WEEK_RE.search(text or "")
    if week:
        bars["weekly"] = {"pct": int(week.group(1)), "reset": week.group(2).strip()}

    if not bars:
        return {"state": UNKNOWN, "detail": "could not parse /usage output", "bars": {}}

    for period, bar in bars.items():
        if parse_reset_time(bar.get("reset")) is None:
            warnings.append(f"claude {period} reset time not understood: {bar.get('reset')!r}")

    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if "resets" in low and "% used" in low:
            if not (_CLAUDE_SESSION_RE.match(s) or _CLAUDE_WEEK_RE.match(s)):
                warnings.append(f"unrecognized claude limit line: {s}")

    out = {"state": OK, "detail": "", "bars": bars}
    if warnings:
        out["warnings"] = warnings
    return out


def _claude_usage(config, timeout):
    bin_path = resolve_bin(config, "claude")
    if bin_path is None:
        return {"state": UNKNOWN, "detail": "claude CLI not found", "bars": {}}

    result = _run_usage(bin_path, timeout)
    if result is None:
        return {"state": UNKNOWN, "detail": "claude -p /usage timed out", "bars": {}}
    if result.returncode != 0:
        return {"state": UNKNOWN, "detail": "claude -p /usage failed", "bars": {}}

    res = parse_claude_usage(result.stdout)
    if res.get("state") != OK:
        try:
            auth_proc = subprocess.run([bin_path, "auth", "status"], capture_output=True, text=True, timeout=3)
            if auth_proc.returncode != 0 or '"loggedIn":false' in auth_proc.stdout.replace(" ", ""):
                return {"state": "down", "detail": "Claude OAuth session expired / not logged in", "bars": {}}
        except Exception:
            pass
    return res


# `agy -p "/usage"` prints one line per (model group, period), either tab-separated
# when piped or space-aligned on a TTY:
#   Gemini Models\tWeekly Limit Remaining\t30%\t2026-10-23T22:43:51Z
#   Gemini Models\tFive Hour Limit Remaining\t63%\t2026-10-22T02:33:30Z
#   Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-28T21:54:12Z
#   Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t93%\t2026-10-27T22:01:20Z
_AGY_PRIMARY_GROUP = "Gemini Models"


def _agy_period(label):
    low = label.lower()
    if "weekly" in low or "week" in low:
        return "weekly"
    if "five hour" in low or "5 hour" in low or "5-hour" in low or "session" in low:
        return "five_hour"
    return None


def parse_agy_usage(text):
    """Parse `agy -p /usage` stdout into {state, detail, bars, groups, warnings?}."""
    bars, groups, warnings = {}, {}, []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower() == "quota:" or "fetching" in line.lower():
            continue
        if "\t" in raw_line:
            cols = [c.strip() for c in raw_line.split("\t") if c.strip() != ""]
        else:
            cols = [c.strip() for c in re.split(r"\s{2,}", line) if c.strip() != ""]
        if len(cols) < 3:
            if "%" in line:
                warnings.append(f"unparsed agy usage line: {line}")
            continue
        group, label, value = cols[0], cols[1], cols[2]
        reset = cols[3] if len(cols) > 3 else ""
        if value.lower() == "disabled":
            continue
        period = _agy_period(label)
        if period is None or not value.endswith("%"):
            if "%" in line:
                warnings.append(f"unparsed agy usage line: {line}")
            continue
        try:
            pct_val = int(value.rstrip("%"))
        except ValueError:
            warnings.append(f"unparsed agy percentage: {line}")
            continue
        used = pct_val if "used" in label.lower() else (100 - pct_val)
        if reset and parse_reset_time(reset) is None:
            warnings.append(f"agy {group} {period} reset time not understood: {reset!r}")
        groups.setdefault(group, {})[period] = {"pct": used, "reset": reset}
        current = bars.get(period)
        is_primary = group == _AGY_PRIMARY_GROUP
        was_primary = current is not None and current["group"] == _AGY_PRIMARY_GROUP
        if current is None or (is_primary and not was_primary):
            bars[period] = {"pct": used, "reset": reset, "group": group}

    if not bars:
        return {"state": UNKNOWN, "detail": "no active limits reported", "bars": {}}
    out = {"state": OK, "detail": "", "bars": bars, "groups": groups}
    if warnings:
        out["warnings"] = warnings
    return out


def _agy_env(no_browser=False):
    env = dict(os.environ)
    env.setdefault("AGY_CLI_FORCE_FILE_TOKEN_STORAGE", "1")
    if no_browser:
        env["BROWSER"] = "/bin/true"
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
    return env


def _agy_output_needs_auth(text):
    low = (text or "").lower()
    return (
        "authentication required" in low
        or "accounts.google.com/o/oauth2/auth" in low
        or "not logged into antigravity" in low
    )


def _decode_stream(val):
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _agy_usage(config, timeout):
    bin_path = resolve_bin(config, "agy")
    if bin_path is None:
        return {"state": UNKNOWN, "detail": "agy CLI not found", "bars": {}}

    try:
        result = subprocess.run(
            [bin_path, "-p", "/usage"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout, check=False, env=_agy_env(no_browser=True),
        )
    except subprocess.TimeoutExpired as exc:
        combined = _decode_stream(exc.stdout) + "\n" + _decode_stream(exc.stderr)
        if _agy_output_needs_auth(combined):
            return {"state": "down", "detail": "Antigravity OAuth session expired / not logged in", "bars": {}}
        return {"state": UNKNOWN, "detail": "agy -p /usage timed out", "bars": {}}
    except OSError:
        return {"state": UNKNOWN, "detail": "agy -p /usage timed out", "bars": {}}

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if _agy_output_needs_auth(combined):
        return {"state": "down", "detail": "Antigravity OAuth session expired / not logged in", "bars": {}}
    if result.returncode != 0:
        return {"state": UNKNOWN, "detail": "agy -p /usage failed", "bars": {}}

    return parse_agy_usage(result.stdout)


def compute_usage(config):
    timeout = config.get("usage_timeout", 30)
    at = fallback_at(config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_result, agy_result = pool.map(
            lambda fn: fn(config, timeout), (_claude_usage, _agy_usage)
        )
    # Remembered for `run`/`resolve`; the cockpit's poll keeps the cache warm.
    return {
        "claude": _cached_quota("claude", claude_result, at=at),
        "agy": _cached_quota("agy", agy_result, at=at),
    }


QUOTA_CACHE_MAX_AGE = 30 * 60
QUOTA_RETEST_AGE = 5 * 60


def _quota_cache_path():
    return Path(os.environ.get("AICLI_QUOTA_CACHE", DEFAULT_USAGE_LOG.with_name("quota.json"))).expanduser()


def _read_quota_cache():
    path = _quota_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_entry_valid_for_timer(hit, at=95, now=None):
    """True when `hit` is a recent reading whose reset timer (`next_ok_at`) or
    any bar reset timestamp has NOT yet elapsed. Once `now >= next_ok_at` (or
    `now >= reset_dt`), the reading is expired and aicli must retest the first
    choice instead of trusting the old cache."""
    if not isinstance(hit, dict) or not isinstance(hit.get("result"), dict):
        return False
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_ts = now_dt.timestamp()
    age = now_ts - hit.get("at", 0)
    if age > QUOTA_CACHE_MAX_AGE:
        return False
    res = hit["result"]
    if res.get("state") != OK:
        return False
    hit_dt = datetime.fromtimestamp(hit.get("at", now_ts), timezone.utc)
    for b in (res.get("bars") or {}).values():
        if isinstance(b, dict) and b.get("reset"):
            reset_dt = parse_reset_time(b.get("reset"), now=hit_dt)
            if reset_dt is not None and now_dt >= reset_dt:
                return False
    for gbars in (res.get("groups") or {}).values():
        if not isinstance(gbars, dict):
            continue
        for b in gbars.values():
            if isinstance(b, dict) and b.get("reset"):
                reset_dt = parse_reset_time(b.get("reset"), now=hit_dt)
                if reset_dt is not None and now_dt >= reset_dt:
                    return False
    pct, _ = meter(res)
    if pct is not None and pct >= at:
        # Exhausted: check if the timer until next OK time has passed!
        next_ok_iso = hit.get("next_ok_at") or next_ok_time(res, at=at, now=hit_dt)
        next_ok_dt = parse_reset_time(next_ok_iso, now=hit_dt)
        if next_ok_dt is None or now_dt >= next_ok_dt:
            return False  # Timer elapsed (or unknown): must retest first choice!
    for grp, gbars in (res.get("groups") or {}).items():
        gpct, _ = meter(res, grp)
        if gpct is not None and gpct >= at:
            g_ok_iso = (hit.get("next_ok_groups") or {}).get(grp) or next_ok_time(res, at=at, group=grp, now=hit_dt)
            g_ok_dt = parse_reset_time(g_ok_iso, now=hit_dt)
            if g_ok_dt is None or now_dt >= g_ok_dt:
                return False
    return True


def _cached_quota(tool, result, at=95, now=None):
    """`result`, saved when it is a reading; when the probe timed out or failed,
    the cached reading if still within its reset timer (`next_ok_at`). Once the
    reset timer passes, an exhausted cache entry is never reused."""
    path = _quota_cache_path()
    cache = _read_quota_cache()
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_ts = now_dt.timestamp()
    if result.get("state") == OK:
        next_ok = next_ok_time(result, at=at, now=now_dt)
        next_ok_groups = {
            g: next_ok_time(result, at=at, group=g, now=now_dt)
            for g in (result.get("groups") or {})
        }
        entry = {"at": now_ts, "result": result}
        if next_ok:
            entry["next_ok_at"] = next_ok
        if any(next_ok_groups.values()):
            entry["next_ok_groups"] = {k: v for k, v in next_ok_groups.items() if v}
        cache[tool] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        return {**result, **({"next_ok_at": next_ok} if next_ok else {})}
    if result.get("state") == "down":
        # Auth is down: never mask this with a stale cached OK reading, and
        # overwrite the cache entry so consumers see the failure immediately.
        cache[tool] = {"at": now_ts, "result": result}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        return result
    if result.get("detail", "").endswith("not found"):
        return result  # no CLI to run at all: an old reading would hide that
    hit = cache.get(tool)
    if _cache_entry_valid_for_timer(hit, at=at, now=now_dt):
        out = {
            **hit["result"],
            "cached_at": datetime.fromtimestamp(hit["at"], timezone.utc).isoformat(),
        }
        if hit.get("next_ok_at"):
            out["next_ok_at"] = hit["next_ok_at"]
        return out
    return result


def get_tool_quota(config, tool, timeout=None, now=None):
    """Get quota for `tool`. Relies on the cached reading while within the reset
    timer (`next_ok_at`) and younger than QUOTA_RETEST_AGE; once `now >= next_ok_at`
    (or cache is stale/missing), probes the CLI live to retest the first choice."""
    at = fallback_at(config)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_ts = now_dt.timestamp()

    fake_env = "FAKE_CLAUDE_USAGE" if tool == "claude" else "FAKE_AGY_USAGE"
    if not os.environ.get(fake_env):
        cache = _read_quota_cache()
        hit = cache.get(tool)
        if _cache_entry_valid_for_timer(hit, at=at, now=now_dt):
            age = now_ts - hit.get("at", 0)
            if age < QUOTA_RETEST_AGE:
                out = {
                    **hit["result"],
                    "cached_at": datetime.fromtimestamp(hit["at"], timezone.utc).isoformat(),
                }
                if hit.get("next_ok_at"):
                    out["next_ok_at"] = hit["next_ok_at"]
                return out

    probe_timeout = timeout if timeout is not None else float(config.get("probe_timeout", 12.0))
    try:
        fn = _claude_usage if tool == "claude" else _agy_usage
        result = fn(config, probe_timeout)
    except Exception:
        result = {"state": UNKNOWN, "bars": {}}
    return _cached_quota(tool, result, at=at, now=now_dt)


# ---- fallback ----------------------------------------------------------------
#
# Per-workload-class fallback (plus optional tool-level fallback):
# Each class in `[classes.<name>]` (or `routes.json`) can configure `fallback`
# as a step `{ tool = "agy", model = "gemini-4-argon-medium", group = "Gemini 4 Argon" }`
# (or a list of steps, or `false`/omitted for no fallback).
# Once the primary plan's five-hour or weekly bar reaches `at` percent, runs of
# that class move to the first step whose own quota group has room.

DEFAULT_FALLBACK_AT = 97


def fallback_at(config):
    return int(config.get("fallback", {}).get("at", DEFAULT_FALLBACK_AT))


def fallback_reserve(config):
    """Guaranteed quota reserve percentage (0..99), default 0."""
    return _valid_pct(config.get("fallback", {}).get("reserve")) or 0


def normalize_fallback_steps(raw):
    """Normalize a class or tool `fallback` config into a list of valid step dicts."""
    if not raw or raw is False:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        if not isinstance(s, dict) or s.get("tool") not in TOOLS:
            continue
        step = dict(s)
        if step["tool"] == "agy" and not step.get("group") and step.get("model"):
            step["group"] = infer_agy_group(step["model"])
        out.append(step)
    return out


def fallback_steps(config, tool=None, workload_class=None, routes=None):
    """Fallback steps for `workload_class` (from routes.json / aicli.toml), falling
    back to `[fallback.<tool>]` only when the class does not specify its own."""
    if workload_class:
        if routes is None:
            routes = load_routes(config)
        classes = routes.get("classes") or {}
        cls_cfg = classes.get(workload_class) or (config.get("classes") or {}).get(workload_class) or {}
        if "fallback" in cls_cfg:
            return normalize_fallback_steps(cls_cfg["fallback"])
    if tool:
        return normalize_fallback_steps(config.get("fallback", {}).get(tool, []))
    return []


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


def _valid_pct(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if 1 <= val <= 100 else None
    if isinstance(val, str) and val.strip().isdigit():
        v = int(val.strip())
        return v if 1 <= v <= 100 else None
    return None


def _class_cfg(config, workload_class, routes=None):
    if not workload_class:
        return {}
    if routes is None:
        routes = load_routes(config)
    classes = (routes or {}).get("classes") or {}
    return classes.get(workload_class) or (config.get("classes") or {}).get(workload_class) or {}


def class_allowance(cls_cfg):
    """Base allowance percentage (1..100) defined on `cls_cfg`, or None."""
    if not isinstance(cls_cfg, dict):
        return None
    return _valid_pct(cls_cfg.get("allowance"))


def active_allowance_override(cls_cfg, now=None):
    """Return the active {"pct": int, "expires_at": str} override for `cls_cfg`,
    or None if missing, invalid, or expired (`now >= expires_at`)."""
    if not isinstance(cls_cfg, dict):
        return None
    ov = cls_cfg.get("allowance_override")
    if not isinstance(ov, dict):
        return None
    pct = _valid_pct(ov.get("pct"))
    expires_at = ov.get("expires_at")
    if pct is None or not expires_at or not isinstance(expires_at, str):
        return None
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    expires_dt = parse_reset_time(expires_at, now=now_dt)
    if expires_dt is None or now_dt >= expires_dt:
        return None
    return {"pct": pct, "expires_at": expires_dt.isoformat()}


def tool_has_critical_classes(config, tool):
    """Return True if any workload class on `tool` is configured with priority='critical'."""
    classes = config.get("classes", {})
    return any(
        (c.get("tool") or "claude") == tool and c.get("priority") == "critical"
        for c in classes.values()
    )


def class_primary_at(config, cls_cfg=None, now=None):
    """Effective primary quota threshold (`primary_at`) for a workload class.
    1. Active temporary allowance override (5h slot).
    2. Explicit class-level `allowance` (legacy/manual override).
    3. If priority == 'critical': gets full quota ceiling (fallback_at).
    4. Standard priority classes yield at (100 - reserve)% on tools with critical classes to guarantee headroom."""
    ov = active_allowance_override(cls_cfg, now=now)
    if ov is not None:
        return ov["pct"]
    base = class_allowance(cls_cfg)
    if base is not None:
        return base
    at = fallback_at(config)
    reserve = fallback_reserve(config)
    if reserve > 0:
        priority = (cls_cfg or {}).get("priority", "standard")
        tool = (cls_cfg or {}).get("tool") or "claude"
        if priority != "critical" and tool_has_critical_classes(config, tool):
            return min(at, max(1, 100 - reserve))
    return at


def compute_allowance_expires_at(config, cls_cfg, usage=None, now=None):
    """Compute ISO-8601 `expires_at` for a 5-hour-slot allowance override from the
    class primary quota group's `five_hour` reset timestamp, or `now + 5h`."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    tool = (cls_cfg or {}).get("tool") or "claude"
    tier = (cls_cfg or {}).get("tier") or "default"
    model = (cls_cfg or {}).get("model") or (config.get(tool, {}).get("tiers") or {}).get(tier)
    group = infer_agy_group(model) if tool == "agy" else None
    q_res = (usage or {}).get(tool) if isinstance(usage, dict) else None
    if q_res is None:
        hit = (_read_quota_cache() or {}).get(tool) or {}
        q_res = hit.get("result") if isinstance(hit, dict) else None
    reset_str = None
    if isinstance(q_res, dict):
        if tool == "agy" and group:
            g_bars = (q_res.get("groups") or {}).get(group) or {}
            reset_str = (g_bars.get("five_hour") or {}).get("reset")
        if not reset_str:
            reset_str = ((q_res.get("bars") or {}).get("five_hour") or {}).get("reset")
    if reset_str:
        reset_dt = parse_reset_time(reset_str, now=now_dt)
        if reset_dt is not None and reset_dt > now_dt:
            return reset_dt.isoformat()
    return (now_dt + timedelta(hours=5)).isoformat()


def apply_class_route_updates(name, c, config, usage=None, now=None):
    """Apply a class update dict from /api/ai-routes (or routes_api.save) via
    `apply_route_override` / `reset_route_override`."""
    routes = load_routes(config)
    cls_cfg = (routes.get("classes") or {}).get(name) or {}
    updates = {}
    if c.get("tool") in TOOLS:
        updates["tool"] = c["tool"]
    if c.get("tier") in TIERS:
        updates["tier"] = c["tier"]
    if "effort" in c:
        if c["effort"] in EFFORTS:
            updates["effort"] = c["effort"]
        elif c["effort"] in (None, ""):
            updates["effort"] = None
    if "priority" in c:
        if c["priority"] in ("critical", "standard"):
            updates["priority"] = c["priority"]
        elif c["priority"] in (None, ""):
            reset_route_override("classes", name, config, field="priority")
    if "temporary_override" in c or "model" in c:
        m = c.get("temporary_override") or c.get("model")
        if isinstance(m, str) and m.strip():
            exp_iso = compute_model_override_expires_at(now)
            updates["temporary_override"] = m.strip()
            updates["temporary_override_expires_at"] = exp_iso
            updates["model"] = m.strip()
            updates["model_expires_at"] = exp_iso
        elif m in (None, ""):
            reset_route_override("classes", name, config, field="temporary_override")
            reset_route_override("classes", name, config, field="temporary_override_expires_at")
            reset_route_override("classes", name, config, field="model")
            reset_route_override("classes", name, config, field="model_expires_at")
    if "fallback" in c:
        fb_raw = c["fallback"]
        if not fb_raw or fb_raw is False:
            updates["fallback"] = None
        else:
            steps = normalize_fallback_steps(fb_raw)
            updates["fallback"] = steps[0] if len(steps) == 1 else (steps if steps else None)
    if "allowance" in c:
        if c["allowance"] in (None, ""):
            reset_route_override("classes", name, config, field="allowance")
        else:
            pct = _valid_pct(c["allowance"])
            if pct is not None:
                updates["allowance"] = pct
    if "allowance_override" in c:
        ov_raw = c["allowance_override"]
        if ov_raw is None or ov_raw is False:
            reset_route_override("classes", name, config, field="allowance_override")
        elif isinstance(ov_raw, dict):
            pct = _valid_pct(ov_raw.get("pct"))
            if pct is not None:
                now_dt = now or datetime.now(timezone.utc)
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=timezone.utc)
                exp = None
                raw_exp = ov_raw.get("expires_at")
                if isinstance(raw_exp, str) and raw_exp.strip():
                    exp_dt = parse_reset_time(raw_exp, now=now_dt)
                    if exp_dt is not None and exp_dt > now_dt:
                        exp = exp_dt.isoformat()
                if not exp:
                    effective_cls = {**cls_cfg, **updates}
                    exp = compute_allowance_expires_at(config, effective_cls, usage=usage, now=now_dt)
                updates["allowance_override"] = {"pct": pct, "expires_at": exp}
    else:
        raw_state = _read_state_file(default_routes_path(config))
        state_ov = ((raw_state.get("classes") or {}).get(name) or {}).get("allowance_override")
        if state_ov is not None and active_allowance_override({"allowance_override": state_ov}, now=now) is None:
            reset_route_override("classes", name, config, field="allowance_override")
    if updates:
        apply_route_override("classes", name, updates, config)


def pick_fallback(config, tool, tier, quota, workload_class=None, routes=None, effort=None, now=None):
    """The step to run instead of `tool`'s own `tier` model for `workload_class`,
    or None when `tool` has room, has no fallback configured, or every step is out."""
    if workload_class and routes is None:
        routes = load_routes(config)
    steps = fallback_steps(config, tool=tool, workload_class=workload_class, routes=routes)
    if not steps:
        return None
    cls_cfg = _class_cfg(config, workload_class, routes=routes)
    at = fallback_at(config)
    reserve = fallback_reserve(config)
    ov = active_allowance_override(cls_cfg, now=now)
    base_allowance = class_allowance(cls_cfg)
    primary_at = class_primary_at(config, cls_cfg=cls_cfg, now=now)
    has_allowance_cap = (ov is not None or base_allowance is not None)
    cls_model = cls_cfg.get("model") if (not cls_cfg.get("tool") or cls_cfg.get("tool") == tool) else None
    primary_model = cls_model or config.get(tool, {}).get("tiers", {}).get(tier)
    primary_group = infer_agy_group(primary_model) if tool == "agy" else None
    q_res = quota(tool)
    pct, reset = meter(q_res, primary_group)
    if pct is None or pct < primary_at:
        return None
    next_ok = next_ok_time(q_res, at=primary_at, group=primary_group, now=now) or (
        q_res.get("next_ok_at") if primary_at == at else None
    )
    if has_allowance_cap:
        cap_desc = f"allowance cap {primary_at}%"
    elif reserve > 0 and (cls_cfg or {}).get("priority") != "critical" and tool_has_critical_classes(config, tool):
        cap_desc = f"reserve yield at {primary_at}% (reserve: {reserve}%)"
    else:
        cap_desc = f"fallback at {primary_at}%"
    for step in steps:
        step_tool = step["tool"]
        step_model = (
            step.get("model")
            or (step.get("tiers") or {}).get(tier)
            or (config.get(step_tool, {}).get("tiers") or {}).get(step.get("tier") or tier)
        )
        step_group = step.get("group") or (infer_agy_group(step_model) if step_tool == "agy" else None)
        step_at = _valid_pct(step.get("at")) or at
        step_pct, _ = meter(quota(step_tool), step_group)
        if step_pct is not None and step_pct >= step_at:
            continue
        step_effort = step.get("effort")
        out = {
            "tool": step_tool,
            "model": step_model,
            "group": step_group,
            "from_tool": tool,
            "from_model": primary_model,
            "reason": f"{tool} quota {pct}% used ({cap_desc})" + (f", resets {reset}" if reset else ""),
        }
        if step_effort:
            out["effort"] = step_effort
        if next_ok:
            out["next_ok_at"] = next_ok
        return out
    return None


def fallback_status(config, usage, routes=None, now=None):
    """Per-class (and optional per-tool) fallback status as the cockpit and CLI show it."""
    if routes is None:
        routes = load_routes(config)
    at = fallback_at(config)
    usage = usage or {}
    quota_fn = usage.get

    classes_out = {}
    for cls_name, cls_cfg in sorted((routes.get("classes") or {}).items()):
        tool = cls_cfg.get("tool", "claude")
        tier = cls_cfg.get("tier", "default")
        effort = cls_cfg.get("effort")
        primary_model = cls_cfg.get("model") or config.get(tool, {}).get("tiers", {}).get(tier, "unknown")
        primary_group = infer_agy_group(primary_model) if tool == "agy" else None
        base_allowance = class_allowance(cls_cfg)
        active_ov = active_allowance_override(cls_cfg, now=now)
        primary_at = class_primary_at(config, cls_cfg=cls_cfg, now=now)
        q_res = quota_fn(tool)
        p_pct, p_reset = meter(q_res, primary_group)
        p_exhausted = p_pct is not None and p_pct >= primary_at
        next_ok = (
            next_ok_time(q_res, at=primary_at, group=primary_group, now=now)
            or ((q_res or {}).get("next_ok_at") if primary_at == at else None)
        ) if p_exhausted else None

        cfg_steps = fallback_steps(config, tool=tool, workload_class=cls_name, routes=routes)
        rows = [{
            "tool": tool,
            "group": primary_group,
            "model": primary_model,
            "effort": effort,
            "label": f"{tool}: {primary_model}",
            "pct": p_pct,
            "reset": p_reset,
            "at": primary_at,
            "exhausted": p_exhausted,
            "active": False,
        }]
        for s in cfg_steps:
            s_tool = s["tool"]
            s_model = (
                s.get("model")
                or (s.get("tiers") or {}).get(tier)
                or (config.get(s_tool, {}).get("tiers") or {}).get(s.get("tier") or tier)
            )
            s_group = s.get("group") or (infer_agy_group(s_model) if s_tool == "agy" else None)
            s_at = _valid_pct(s.get("at")) or at
            s_pct, s_reset = meter(quota_fn(s_tool), s_group)
            s_exhausted = s_pct is not None and s_pct >= s_at
            rows.append({
                "tool": s_tool,
                "group": s_group,
                "model": s_model,
                "effort": s.get("effort"),
                "label": s.get("label") or f"{s_tool}: {s_model}",
                "pct": s_pct,
                "reset": s_reset,
                "at": s_at,
                "exhausted": s_exhausted,
                "active": False,
            })

        active_idx = next((i for i, r in enumerate(rows) if not r["exhausted"]), None)
        for i, r in enumerate(rows):
            r["active"] = (i == active_idx)

        fb = pick_fallback(config, tool, tier, quota_fn, workload_class=cls_name, routes=routes, effort=effort, now=now)
        if not p_exhausted:
            state = "ok"
        elif fb is not None:
            state = "fallback"
        else:
            state = "blocked"

        classes_out[cls_name] = {
            "tool": tool,
            "tier": tier,
            "effort": effort,
            "priority": cls_cfg.get("priority", "standard"),
            "model": primary_model,
            "group": primary_group,
            "allowance": base_allowance,
            "effective_at": primary_at,
            "allowance_override": active_ov,
            "primary_pct": p_pct,
            "primary_reset": p_reset,
            "primary_exhausted": p_exhausted,
            "next_ok_at": next_ok,
            "state": state,
            "effective_tool": fb["tool"] if fb else tool,
            "effective_model": fb["model"] if fb else primary_model,
            "effective_effort": fb.get("effort") if fb else effort,
            "fallback": fb,
            "configured_fallback": cfg_steps,
            "has_fallback_configured": bool(cfg_steps),
            "steps": rows,
            "active": active_idx,
            "all_exhausted": active_idx is None,
        }

    chains = {"at": at, "reserve": fallback_reserve(config), "classes": classes_out}
    for tool in TOOLS:
        steps = fallback_steps(config, tool=tool)
        if not steps:
            continue
        rows = [{"tool": tool, "group": None, "label": f"{tool} plan", "tiers": config.get(tool, {}).get("tiers", {})}]
        rows += [{"tool": s["tool"], "group": s.get("group"),
                  "label": s.get("label") or f"{s['tool']}: {s.get('group') or s.get('model') or 'plan'}",
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


def check_quota_health(config, fixtures_dir=None, live=True):
    """Verify quota parsing against fixtures and (optionally) the live CLIs.
    Returns {"ok": bool, "checks": [...], "errors": [...]}."""
    checks = []
    errors = []
    if fixtures_dir is None:
        candidate = Path(__file__).resolve().parent / "tests" / "fixtures"
        if candidate.is_dir():
            fixtures_dir = candidate

    def verify_result(name, tool, res):
        errs = []
        if res.get("state") != OK:
            errs.append(f"{name}: state is {res.get('state')!r} ({res.get('detail', '')})")
        bars = res.get("bars") or {}
        if not bars:
            errs.append(f"{name}: no bars parsed")
        if tool == "claude":
            for req in ("five_hour", "weekly"):
                if req not in bars:
                    errs.append(f"{name}: missing {req!r} bar")
        elif tool == "agy":
            if "weekly" not in bars:
                errs.append(f"{name}: missing 'weekly' bar")
            if not res.get("groups"):
                errs.append(f"{name}: no quota groups parsed")
        for period, b in bars.items():
            pct = b.get("pct")
            if not isinstance(pct, int) or not (0 <= pct <= 100):
                errs.append(f"{name}: invalid pct {pct!r} for {period}")
            if b.get("reset") and parse_reset_time(b["reset"]) is None:
                errs.append(f"{name}: unparseable reset {b.get('reset')!r} for {period}")
        for grp, gbars in (res.get("groups") or {}).items():
            for period, b in gbars.items():
                if b.get("reset") and parse_reset_time(b["reset"]) is None:
                    errs.append(f"{name}: unparseable reset {b.get('reset')!r} for {grp}/{period}")
        for w in res.get("warnings") or []:
            errs.append(f"{name}: {w}")
        checks.append({"name": name, "tool": tool, "ok": not errs, "errors": errs, "result": res})
        errors.extend(errs)

    if fixtures_dir and Path(fixtures_dir).is_dir():
        for f in sorted(Path(fixtures_dir).glob("*.txt")):
            text = f.read_text(encoding="utf-8")
            if f.name.startswith("claude"):
                verify_result(f"fixture:{f.name}", "claude", parse_claude_usage(text))
            elif f.name.startswith("agy"):
                verify_result(f"fixture:{f.name}", "agy", parse_agy_usage(text))

    if live:
        timeout = config.get("usage_timeout", 30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            c_res, a_res = pool.map(lambda fn: fn(config, timeout), (_claude_usage, _agy_usage))
        verify_result("live:claude", "claude", c_res)
        verify_result("live:agy", "agy", a_res)

    return {"ok": len(errors) == 0, "checks": checks, "errors": errors}


_CLAUDE_MODEL_KEYS = {"inputTokens": "input", "outputTokens": "output",
                      "cacheCreationInputTokens": "cache_write", "cacheReadInputTokens": "cache_read"}
_USAGE_KEYS = {"input_tokens": "input", "prompt_tokens": "input",
               "output_tokens": "output", "completion_tokens": "output",
               "cache_creation_input_tokens": "cache_write", "cache_read_input_tokens": "cache_read",
               "total_tokens": "total"}


def usage_counts(raw, keys):
    out = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    for key, kind in keys.items():
        value = raw.get(key)
        if isinstance(value, (int, float)):
            out[kind] = out.get(kind, 0) + int(value)
    if not out.get("total"):
        out["total"] = out["input"] + out["output"] + out["cache_write"] + out["cache_read"]
    return out


def usage_of(tool, result, model):
    """Token counts per model from the CLI's final result: {model: counts}, or
    None when the CLI reported none. claude gives `modelUsage` per model (and
    a flat `usage`); agy gives a flat `usage` under its result."""
    if not isinstance(result, dict):
        return None
    if tool == "claude" and isinstance(result.get("modelUsage"), dict) and result["modelUsage"]:
        return {m: usage_counts(u, _CLAUDE_MODEL_KEYS)
                for m, u in result["modelUsage"].items() if isinstance(u, dict)}
    usage = result.get("usage")
    if tool == "agy" and not isinstance(usage, dict) and isinstance(result.get("result"), dict):
        usage = result["result"].get("usage")
    if isinstance(usage, dict) and usage:
        return {model or tool: usage_counts(usage, _USAGE_KEYS)}
    return None


def parse_cli_output(text):
    """The last JSON object a CLI printed that carries token counts, or None.
    Handles one JSON document (`--output-format json`) and JSON lines
    (`stream-json`, where the final `result` event has the counts)."""
    found = None
    docs = [text] + text.splitlines()
    for chunk in docs:
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and (obj.get("modelUsage") or obj.get("usage")
                                      or isinstance(obj.get("result"), dict)):
            found = obj
    return found


def estimate_tokens(chars):
    """~4 characters per token. Only used when the CLI reports nothing."""
    return max(1, round(chars / 4)) if chars else 0


def run_usage_fields(tool, model, stdout_text, argv_chars, stdout_chars):
    """Fields `aicli run` adds to its usage-log line: {model: counts} rows plus
    whether they are an estimate. Counts the CLI reported win; otherwise the
    prompt (argv) and the answer (stdout) are estimated and marked."""
    reported = usage_of(tool, parse_cli_output(stdout_text), model)
    if reported:
        return reported, False
    out = estimate_tokens(stdout_chars)
    inp = estimate_tokens(argv_chars)
    if not (inp or out):
        return None, False
    return {model or tool: {"input": inp, "output": out, "cache_write": 0, "cache_read": 0,
                            "total": inp + out}}, True


def usage_log_path():
    return Path(os.environ.get("AICLI_USAGE_LOG", DEFAULT_USAGE_LOG)).expanduser()


# ---- CLI authentication ---------------------------------------------------

_URL_RE = re.compile(r"https://claude\.com/cai/oauth/authorize\?[^\s]+")
_ANSI_STRIP = re.compile(r"\x1b\][^\x07\x1b]*[\x07\x1b]|\x1b\[[0-9;]*[a-zA-Z]|\x07")
_login_lock = threading.Lock()
_current_logins = {}


def auth_status(config, tool=None):
    """Check authentication status for CLIs via their native CLI commands.
    Returns dict of {tool: {authenticated: bool, detail: str, ...}}."""
    tools = [tool] if tool else TOOLS
    out = {}
    for t in tools:
        bin_path = resolve_bin(config, t)
        if not bin_path:
            out[t] = {"authenticated": False, "detail": f"{t} CLI not found"}
            continue
        if t == "claude":
            try:
                p = subprocess.run([bin_path, "auth", "status"], capture_output=True, text=True, timeout=5, check=False)
                data = {}
                try:
                    data = json.loads(p.stdout)
                except Exception:
                    pass
                if isinstance(data, dict) and data.get("loggedIn") is True:
                    method = data.get("authMethod", "oauth")
                    out[t] = {"authenticated": True, "auth_method": method, "detail": f"Authenticated ({method})"}
                else:
                    out[t] = {"authenticated": False, "detail": "OAuth session expired / not logged in"}
            except Exception as e:
                out[t] = {"authenticated": False, "detail": f"Check failed: {e}"}
        elif t == "agy":
            try:
                p = subprocess.run(
                    [bin_path, "-p", "/usage"], capture_output=True, text=True,
                    stdin=subprocess.DEVNULL, timeout=12, check=False, env=_agy_env(no_browser=True),
                )
                combined = (p.stdout or "") + "\n" + (p.stderr or "")
                if _agy_output_needs_auth(combined):
                    _cached_quota("agy", {"state": "down", "detail": "Antigravity OAuth session expired / not logged in", "bars": {}})
                    out[t] = {"authenticated": False, "detail": "Antigravity OAuth session expired / not logged in"}
                elif p.returncode == 0 and ("Limit" in p.stdout or "Quota" in p.stdout or "Gemini" in p.stdout):
                    out[t] = {"authenticated": True, "detail": "Authenticated"}
                elif "fake" in bin_path and p.returncode == 0:
                    out[t] = {"authenticated": True, "detail": "Authenticated"}
                else:
                    out[t] = {"authenticated": False, "detail": "Not signed in or session expired"}
            except subprocess.TimeoutExpired as exc:
                combined = _decode_stream(exc.stdout) + "\n" + _decode_stream(exc.stderr)
                detail = (
                    "Antigravity OAuth session expired / not logged in"
                    if _agy_output_needs_auth(combined)
                    else "Timed out checking agy auth (likely not signed in)"
                )
                _cached_quota("agy", {"state": "down", "detail": detail, "bars": {}})
                out[t] = {"authenticated": False, "detail": detail}
            except Exception as e:
                out[t] = {"authenticated": False, "detail": f"Check failed: {e}"}
    return out[tool] if tool else out


def start_login(config, tool):
    """Initiates login flow for `tool`. For claude, returns {'ok': True, 'auth_url': '...'};
    for agy, returns instructions and interactive command."""
    if tool != "claude":
        bin_path = resolve_bin(config, tool) or tool
        return {
            "ok": True,
            "interactive": True,
            "message": f"{tool} requires an interactive terminal for login",
            "command": f"{bin_path} --no-tmux",
        }

    global _current_logins
    with _login_lock:
        now = time.time()
        cur = _current_logins.get(tool)
        if cur and cur["proc"].poll() is None and (now - cur["started"] < 300) and cur["auth_url"]:
            return {"ok": True, "auth_url": cur["auth_url"]}

        if cur and cur["proc"].poll() is None:
            try:
                cur["proc"].terminate()
                cur["proc"].wait(timeout=2)
            except Exception:
                pass
        _current_logins.pop(tool, None)

        bin_path = resolve_bin(config, tool)
        if not bin_path:
            return {"ok": False, "error": f"{tool} CLI not found"}

        try:
            p = subprocess.Popen(
                [bin_path, "auth", "login"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            return {"ok": False, "error": f"Failed spawning {tool}: {e}"}

        auth_url = ""
        start_t = time.time()
        while time.time() - start_t < 8.0:
            line = p.stdout.readline()
            if not line and p.poll() is not None:
                break
            clean = _ANSI_STRIP.sub(" ", line)
            m = _URL_RE.search(clean)
            if m:
                auth_url = m.group(0)
                break
            if "visit:" in clean:
                cand = clean.split("visit:")[-1].strip().split()[0]
                if cand.startswith("http"):
                    auth_url = cand
                    break
            time.sleep(0.05)

        if not auth_url:
            try:
                p.terminate()
            except Exception:
                pass
            return {"ok": False, "error": f"Timed out waiting for {tool} OAuth URL"}

        _current_logins[tool] = {"proc": p, "auth_url": auth_url, "started": now}
        return {"ok": True, "auth_url": auth_url}


def submit_code(config, tool, code):
    """Submits authorization code to the pending login process for `tool`."""
    global _current_logins
    with _login_lock:
        cur = _current_logins.get(tool)
        if not cur or cur["proc"].poll() is not None:
            return {"ok": False, "error": f"No active {tool} login session in progress. Please restart login."}

        code_clean = (code or "").strip()
        if not code_clean:
            return {"ok": False, "error": "Authorization code cannot be empty"}

        proc = cur["proc"]
        try:
            proc.stdin.write(code_clean + "\n")
            proc.stdin.flush()
        except OSError as e:
            return {"ok": False, "error": f"Failed writing code to CLI process: {e}"}

        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            _current_logins.pop(tool, None)
            return {"ok": False, "error": f"{tool} login timed out verifying code"}

        _current_logins.pop(tool, None)
        st = auth_status(config, tool)
        if st.get("authenticated"):
            return {"ok": True, "message": f"{tool} successfully authenticated!"}
        err_tail = (stderr or stdout or "Code rejected").strip()[-200:]
        return {"ok": False, "error": f"{tool} authentication failed: {err_tail}"}


def cancel_login(config, tool):
    """Cancels pending login for `tool`."""
    global _current_logins
    with _login_lock:
        cur = _current_logins.pop(tool, None)
        if cur and cur["proc"].poll() is None:
            try:
                cur["proc"].terminate()
                cur["proc"].wait(timeout=2)
            except Exception:
                pass
        return {"ok": True}


# ---- command line ----------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="aicli")
    parser.add_argument("--config", help="path to aicli.toml (default: $AICLI_CONFIG, ~/.config/aicli/aicli.toml, or the one in this repo)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_which = sub.add_parser("which")
    p_which.add_argument("tool", choices=TOOLS)

    def add_tool_args(p):
        p.add_argument("--client", help="client identifier (e.g. pjm:maintainer, ai-job-search)")
        p.add_argument("--class", dest="workload_class", help="workload class (e.g. maintenance, standard-dev)")
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-fallback", action="store_true", help="never move to a fallback step")
        p.add_argument("--skill", action="append", metavar="NAME_OR_DIR",
                       help="load this skill too: a catalog name or a folder holding a SKILL.md (repeatable)")
        p.add_argument("--no-skills", action="store_true", help="load no skills, not even the class's")
        p.add_argument("args", nargs=argparse.REMAINDER)

    def add_job_args(p):
        # The tool-neutral job: aicli builds it into the flags of whichever CLI the router picks.
        p.add_argument("--prompt", help="the prompt (claude: interactive unless --oneshot or --output-format; agy: always one-shot)")
        p.add_argument("--prompt-file", help="read the prompt from this file")
        p.add_argument("--system-prompt-file", help="instructions added to the CLI's own (agy: put in front of the prompt)")
        p.add_argument("--session-id", help="start a session with this UUID (agy: ignored)")
        p.add_argument("--resume", help="resume this session (agy: ignored)")
        p.add_argument("--conversation", help="continue this agy conversation, an id agy printed (claude: ignored)")
        p.add_argument("--name", help="display name of the session (agy: ignored)")
        p.add_argument("--settings", help="claude settings file, hooks included (agy: ignored)")
        p.add_argument("--mcp-config", help="MCP servers file, the only ones loaded (agy: ignored)")
        p.add_argument("--permission-mode", choices=PERMISSION_MODES, help="dontAsk, auto, or bypass (no permission prompts at all); agy only honours bypass")
        p.add_argument("--output-format", choices=OUTPUT_FORMATS, help="one-shot output; stream-json prints events as they come")
        p.add_argument("--oneshot", action="store_true", help="print the answer and exit")
        p.add_argument("--interactive", action="store_true", help="stay open for a human to type into (agy: -i instead of -p)")
        p.add_argument("--remote-control", action="store_true", help="enable Remote Control at start (claude: --remote-control; agy: --remote-control)")
        p.add_argument("--chrome", action="store_true", help="enable Claude in Chrome integration (claude: --chrome; agy: ignored)")

    p_argv = sub.add_parser("argv")
    add_tool_args(p_argv)
    add_job_args(p_argv)
    p_run = sub.add_parser("run")
    add_tool_args(p_run)
    add_job_args(p_run)
    p_run.add_argument("--meta-out", help="write execution metadata (model, quota, timestamp) to path before exec")

    p_resolve = sub.add_parser("resolve")
    add_tool_args(p_resolve)
    p_resolve.add_argument("--quota", action="store_true", help="include live quota metadata")

    p_usage = sub.add_parser("usage")
    p_usage.add_argument("--json", action="store_true")

    p_fallback = sub.add_parser("fallback", help="each workload class and fallback chain, its readings and active state")
    p_fallback.add_argument("--json", action="store_true")

    p_check_quota = sub.add_parser("check-quota", help="verify quota & reset parsing against fixtures and live CLIs")
    p_check_quota.add_argument("--fixtures-only", action="store_true", help="only check saved fixtures, do not run live CLIs")
    p_check_quota.add_argument("--json", action="store_true")

    p_routes = sub.add_parser("routes")
    p_routes.add_argument("--json", action="store_true")

    p_rapi = sub.add_parser("routes-api", help="what the routing page loads and saves (routes_static/), as JSON")
    rapi_sub = p_rapi.add_subparsers(dest="rapi_command", required=True)
    p_rget = rapi_sub.add_parser("get", help="the page's data: routes, fallbacks, readings")
    p_rget.add_argument("--scope", default="", help="only clients starting with this prefix (e.g. pjm:)")
    p_rget.add_argument("--live", action="store_true", help="probe the CLIs now instead of using the last readings")
    p_rsave = rapi_sub.add_parser("save", help="apply the page's JSON body from stdin")
    p_rsave.add_argument("--scope", default="", help="only touch clients starting with this prefix")
    rapi_sub.add_parser("static", help="print the folder holding the page's html, js and css")
    p_rpage = rapi_sub.add_parser("page", help="print the page's HTML for a host to serve")
    p_rpage.add_argument("--scope", default="", help="limit the page to clients starting with this prefix")
    p_rpage.add_argument("--api", default="/api/ai-routes", help="URL the page loads and saves through")
    p_rpage.add_argument("--static", default="/routes_static", help="URL prefix the host serves the folder at")
    p_rpage.add_argument("--title", default="", help="the host's name, after the page title")
    p_rpage.add_argument("--back-href", default="/")
    p_rpage.add_argument("--back-label", default="Back")
    p_rpage.add_argument("--lede", default="", help="intro text (HTML)")
    p_rpage.add_argument("--head", default="", help="extra HTML for <head> (styles)")
    p_rpage.add_argument("--save-note", default="Saved. aicli reads it on the next run.")

    p_route = sub.add_parser("route")
    route_sub = p_route.add_subparsers(dest="route_command", required=True)
    p_set_cls = route_sub.add_parser("set-class")
    p_set_cls.add_argument("name")
    p_set_cls.add_argument("--tool", required=True, choices=TOOLS)
    p_set_cls.add_argument("--tier", default="default", choices=TIERS)
    p_set_cls.add_argument("--effort", choices=EFFORTS)
    p_set_cls.add_argument("--priority", choices=["critical", "standard"], help="workload priority tier (critical runs up to fallback ceiling; standard yields at reserve threshold)")
    p_set_cls.add_argument("--fallback-tool", choices=TOOLS, help="fallback tool when primary quota is exhausted")
    p_set_cls.add_argument("--fallback-model", help="fallback model name (e.g. gemini-4-argon-medium)")
    p_set_cls.add_argument("--no-fallback", action="store_true", help="disable fallback for this class")

    p_set_cli = route_sub.add_parser("set-client", help="configure client routing or a temporary 5-hour model override (temporary_override)")
    p_set_cli.add_argument("name")
    p_set_cli.add_argument("--class", dest="workload_class")
    p_set_cli.add_argument("--tool", choices=TOOLS)
    p_set_cli.add_argument("--tier", choices=TIERS)
    p_set_cli.add_argument("--effort", choices=EFFORTS)
    p_set_cli.add_argument("--temporary-override", "--model", dest="temporary_override",
                           help="temporary model override for the client (lasts up to 5 hours; clients must stop using overrides permanently and reuse or create a workload class in aicli.toml instead)")

    p_temp_ov = route_sub.add_parser("temporary-override", help="set temporary model override for a client (lasts up to 5 hours)")
    p_temp_ov.add_argument("client", help="client identifier")
    p_temp_ov.add_argument("model", help="model name to temporarily override to (expires in 5h)")
    p_temp_ov.add_argument("--hours", type=float, default=5.0, help="override duration in hours (default 5.0, max 5.0)")

    p_del_cli = route_sub.add_parser("remove-client")
    p_del_cli.add_argument("name")

    p_reset_cls = route_sub.add_parser("reset-class", help="drop a class's override, back to aicli.toml")
    p_reset_cls.add_argument("name")
    p_reset_cls.add_argument("--field", help="drop only this field; omit to drop the whole override")

    p_reset_cli = route_sub.add_parser("reset-client", help="drop a default client's override, back to aicli.toml")
    p_reset_cli.add_argument("name")
    p_reset_cli.add_argument("--field", help="drop only this field; omit to drop the whole override")

    p_set_allow = route_sub.add_parser("set-allowance", help="set a temporary 5-hour-slot allowance override (1..100)")
    p_set_allow.add_argument("pct", help="allowance percentage (1..100)")
    p_set_allow.add_argument("--class", dest="workload_class", default="standard-dev", help="workload class (default: standard-dev)")

    p_clr_allow = route_sub.add_parser("clear-allowance", help="clear a class's temporary allowance override")
    p_clr_allow.add_argument("--class", dest="workload_class", default="standard-dev", help="workload class (default: standard-dev)")

    p_skills = sub.add_parser("skills", help="the skills each class loads, and fetching them")
    skills_sub = p_skills.add_subparsers(dest="skills_command", required=True)
    p_sl = skills_sub.add_parser("list", help="skills per class and CLI")
    p_sl.add_argument("--class", dest="workload_class")
    p_sl.add_argument("--tool", choices=TOOLS)
    p_sl.add_argument("--json", action="store_true")
    p_su = skills_sub.add_parser("update", help="fetch the latest of every catalog skill into the cache (never fails)")
    p_su.add_argument("--vendor", action="store_true", help="write into the repo's skills/vendor/ instead (to commit)")
    p_su.add_argument("--only", action="append", help="only this skill (repeatable)")
    p_su.add_argument("--json", action="store_true")

    p_auth = sub.add_parser("auth", help="manage and check CLI authentication")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_auth_status = auth_sub.add_parser("status", help="check authentication status of CLIs")
    p_auth_status.add_argument("--tool", choices=TOOLS, help="only check specific tool")
    p_auth_status.add_argument("--json", action="store_true")

    p_auth_start = auth_sub.add_parser("start", help="start login flow for a CLI")
    p_auth_start.add_argument("tool", choices=TOOLS)
    p_auth_start.add_argument("--json", action="store_true")

    p_auth_submit = auth_sub.add_parser("submit", help="submit authorization code for a CLI")
    p_auth_submit.add_argument("tool", choices=TOOLS)
    p_auth_submit.add_argument("code", help="authorization code")
    p_auth_submit.add_argument("--json", action="store_true")

    p_auth_cancel = auth_sub.add_parser("cancel", help="cancel pending login flow")
    p_auth_cancel.add_argument("tool", choices=TOOLS)
    p_auth_cancel.add_argument("--json", action="store_true")

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
        job = job_from_args(args)
    except JobError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2
    routes = load_routes(config)
    try:
        tool, tier, effort, resolved_class = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            routes=routes,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    tool, fallback = _run_fallback(args, config, tool, tier, _quota_reader(config),
                                   workload_class=resolved_class, routes=routes, effort=effort)
    eff = fallback.get("effort") if fallback else effort
    pinned = None if fallback else route_model(routes, args.client, resolved_class)
    job, skill_flags, skill_names = run_skills(args, config, tool, resolved_class, routes, job)
    try:
        argv = build_argv(config, tool, tier, eff, extra, model=(fallback or {}).get("model") or pinned,
                          job=job, skill_flags=skill_flags)
    except JobError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2
    if argv is None:
        print(f"{tool} CLI not found", file=sys.stderr)
        return 1
    print(json.dumps(argv) if args.json else shlex.join(argv))
    return 0


def cmd_resolve(args, config):
    routes = load_routes(config)
    try:
        tool, tier, effort, resolved_class = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            routes=routes,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    quota = _quota_reader(config)
    fallback = None if args.no_fallback else pick_fallback(
        config, tool, tier, quota, workload_class=resolved_class, routes=routes, effort=effort
    )
    if fallback:
        tool = fallback["tool"]
        effort = fallback.get("effort")
    pinned = None if fallback else route_model(routes, args.client, resolved_class)
    model = ((fallback or {}).get("model") or pinned
             or config.get(tool, {}).get("tiers", {}).get(tier, "unknown"))
    res = {
        "client": args.client,
        "class": resolved_class,
        "tool": tool,
        "tier": tier,
        "model": model,
        "effort": effort,
        "fallback": fallback,
        "skills": class_skill_names(config, resolved_class, tool, routes),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.quota:
        res["quota"] = quota(tool)  # forces a live probe
    elif args.json:
        cached = quota.cached(tool)  # never probes
        if cached is not None:
            res["quota"] = cached

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


def _run_fallback(args, config, tool, tier, quota, workload_class=None, routes=None, effort=None):
    """(tool, fallback) for `run` and `argv`: the tool the router picked, or the fallback
    step that replaces it. The caller named no CLI, so any step may apply."""
    fallback = None if args.no_fallback else pick_fallback(
        config, tool, tier, quota, workload_class=workload_class, routes=routes, effort=effort
    )
    return (fallback["tool"] if fallback else tool), fallback


def _cached_quota_only(tool):
    """The on-disk cached quota reading for `tool`, or None when there is
    none. Never probes: a run whose class has no fallback step never waits on
    one just to log a number nothing uses for a decision."""
    hit = _read_quota_cache().get(tool)
    if not isinstance(hit, dict) or not isinstance(hit.get("result"), dict):
        return None
    out = {**hit["result"], "cached_at": datetime.fromtimestamp(hit["at"], timezone.utc).isoformat()}
    if hit.get("next_ok_at"):
        out["next_ok_at"] = hit["next_ok_at"]
    return out


def _quota_reader(config):
    """quota(tool) probes live (for a fallback decision), each tool at most
    once. quota.cached(tool) returns that same reading when a fallback
    decision already probed this tool, else the on-disk cache only - it never
    starts a probe of its own."""
    seen = {}

    def quota(tool):
        if tool not in seen:
            seen[tool] = get_tool_quota(config, tool)
        return seen[tool]

    def cached(tool):
        return seen[tool] if tool in seen else _cached_quota_only(tool)

    quota.cached = cached
    return quota


def cmd_run(args, config):
    extra = _strip_dashdash(args.args)
    try:
        job = job_from_args(args)
    except JobError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2
    routes = load_routes(config)
    try:
        tool, tier, effort, resolved_class = resolve_route(
            config,
            client=args.client,
            workload_class=args.workload_class,
            routes=routes,
        )
    except ValueError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2

    quota = _quota_reader(config)
    tool, fallback = _run_fallback(args, config, tool, tier, quota,
                                   workload_class=resolved_class, routes=routes, effort=effort)
    eff = fallback.get("effort") if fallback else effort
    pinned = None if fallback else route_model(routes, args.client, resolved_class)
    job, skill_flags, skill_names = run_skills(args, config, tool, resolved_class, routes, job)
    try:
        argv = build_argv(config, tool, tier, eff, extra, model=(fallback or {}).get("model") or pinned,
                          job=job, skill_flags=skill_flags)
    except JobError as e:
        print(f"aicli: {e}", file=sys.stderr)
        return 2
    if argv is None:
        print(f"{tool} CLI not found", file=sys.stderr)
        return 1
    # Report what will really run: a caller's own --model/--effort wins over
    # the tier, and agy's tier model can drop the effort (model_sets_effort).
    model = _flag_value(argv, config[tool].get("model_flag", "")) or "unknown"
    effort = _flag_value(argv, config[tool].get("effort_flag", ""))
    # Never probes on its own: the fallback decision above already probed when
    # one was needed, so a class with no fallback never waits here either.
    quota_res = quota.cached(tool)

    if getattr(args, "meta_out", None):
        meta = {
            "client": args.client,
            "class": resolved_class,
            "tool": tool,
            "tier": tier,
            "model": model,
            "effort": effort,
            "fallback": fallback,
            "skills": skill_names,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **({"quota": quota_res} if quota_res is not None else {}),
        }
        meta_path = Path(args.meta_out)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = meta_path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, meta_path)

    row = {
        "client": args.client,
        "class": resolved_class,
        "tool": tool,
        "tier": tier,
        "model": model,
        **({"quota": quota_res} if quota_res is not None else {}),
        **({"fallback": fallback} if fallback else {}),
    }
    trust_cwd(tool, config=config)
    if sys.stdout.isatty():
        # An interactive session: its output is the screen, not a pipe we can
        # read, so exec the CLI as-is and log the request without counts.
        _log_run(row, None, False)
        if tool == "agy":
            os.execve(argv[0], argv, _agy_env())  # never returns
        os.execv(argv[0], argv)  # never returns
    return _run_captured(argv, tool, model, row)


def _log_run(row, usage, estimated):
    """Append the run to the usage log, one line per model the CLI used. A
    run without counts gets one line with `reported: false`; estimated counts
    are `reported: true, estimated: true`. An unwritable log never fails a run."""
    at = datetime.now(timezone.utc).isoformat()
    if usage:
        lines = [{"at": at, **row, "model": m, "reported": True, **({"estimated": True} if estimated else {}), **counts}
                 for m, counts in usage.items()]
    else:
        lines = [{"at": at, **row, "reported": False}]
    try:
        path = usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
    except OSError:
        pass


MAX_CAPTURE = 16 * 1024 * 1024


def _git_root(cwd):
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out or None


def _rewrite_json(conf, data):
    # Written aside and renamed: running CLIs rewrite these files too.
    tmp = conf.with_name(f"{conf.name}.aicli-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    if conf.exists():
        os.chmod(tmp, conf.stat().st_mode & 0o777)
    os.replace(tmp, conf)


def trust_claude(folders, conf=None):
    """Mark each folder trusted in claude's ~/.claude.json (AICLI_CLAUDE_JSON)."""
    conf = Path(conf or os.environ.get("AICLI_CLAUDE_JSON") or "~/.claude.json").expanduser().resolve()
    try:
        data = json.loads(conf.read_text())
        projects = data.setdefault("projects", {})
        changed = False
        for f in folders:
            entry = projects.setdefault(f, {})
            if not entry.get("hasTrustDialogAccepted"):
                entry["hasTrustDialogAccepted"] = True
                changed = True
        if changed:
            _rewrite_json(conf, data)
    except (OSError, ValueError, AttributeError):
        return False
    return True


def trust_agy(folders, conf=None):
    """Add each folder to agy's `trustedWorkspaces` (AICLI_AGY_SETTINGS)."""
    # The default location lives in aicli.toml ([agy] trust_file), not in this source file,
    # which stays free of vendor config paths (see SourceSafetyTests).
    conf = os.environ.get("AICLI_AGY_SETTINGS") or conf
    if not conf:
        return False
    conf = Path(conf).expanduser().resolve()
    try:
        try:
            data = json.loads(conf.read_text())
        except FileNotFoundError:
            if not conf.parent.is_dir():
                return False   # agy not set up here
            data = {}
        trusted = data.setdefault("trustedWorkspaces", [])
        if not isinstance(trusted, list):
            return False
        missing = [f for f in folders if f not in trusted]
        if missing:
            trusted.extend(missing)
            _rewrite_json(conf, data)
    except (OSError, ValueError, AttributeError):
        return False
    return True


TRUSTERS = {"claude": trust_claude, "agy": trust_agy}


def trust_cwd(tool, cwd=None, config=None):
    """Mark the working directory (and its git root) trusted for `tool` before it launches,
    so no "trust this folder" prompt can ever appear, whoever the caller is. Never fails a run."""
    cwd = str(Path(cwd or os.getcwd()).resolve())
    folders = [cwd]
    root = _git_root(cwd)
    if root and root not in folders:
        folders.append(str(Path(root).resolve()))
    trust = TRUSTERS.get(tool)
    if not trust:
        return False
    if tool == "agy":
        try:
            config = config or load_config()
        except BaseException:
            config = {}
        return trust(folders, (config.get("agy") or {}).get("trust_file"))
    return trust(folders)


def _run_captured(argv, tool, model, row):
    """Run the CLI with its stdout passed through unchanged while a copy is
    kept, then log the token counts it reported (or an estimate)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, env=_agy_env() if tool == "agy" else None)
    old = signal.signal(signal.SIGTERM, lambda signum, frame: proc.terminate())
    kept, size = [], 0
    try:
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            if size < MAX_CAPTURE:
                kept.append(chunk)
            size += len(chunk)
        code = proc.wait()
    except KeyboardInterrupt:
        proc.wait()
        code = 130
    except BrokenPipeError:
        proc.terminate()
        code = proc.wait()
    finally:
        signal.signal(signal.SIGTERM, old)
    text = b"".join(kept).decode("utf-8", errors="replace")
    if tool == "agy" and code != 0 and _agy_output_needs_auth(text):
        _cached_quota(
            "agy",
            {"state": "down", "detail": "Antigravity OAuth session expired / not logged in", "bars": {}},
        )
    argv_chars = sum(len(a) for a in argv[1:])
    usage, estimated = run_usage_fields(tool, model, text, argv_chars, len(text))
    _log_run(row, usage, estimated)
    return code


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
    at = chains.get("at", DEFAULT_FALLBACK_AT)
    classes = chains.get("classes") or {}
    if not classes:
        print("no workload classes configured")
        return 0
    print(f"WORKLOAD CLASSES (fallback threshold: {at}%):")
    for cls_name, info in sorted(classes.items()):
        state = info["state"].upper()
        p_pct = "?" if info["primary_pct"] is None else f"{info['primary_pct']}%"
        primary_str = f"{info['tool']}/{info['model']} ({p_pct})"
        if info["state"] == "fallback" and info.get("fallback"):
            fb = info["fallback"]
            fb_step = next((s for s in info["steps"][1:] if s.get("active")), None)
            fb_pct = "?" if not fb_step or fb_step["pct"] is None else f"{fb_step['pct']}%"
            extra = f" -> FALLBACK {fb['tool']}/{fb['model']} ({fb_pct})"
            if info.get("next_ok_at"):
                extra += f" [retests primary at {info['next_ok_at']}]"
        elif info["state"] == "blocked":
            why = "all fallbacks exhausted" if info["has_fallback_configured"] else "no fallback"
            extra = f" — BLOCKED ({why})"
            if info.get("next_ok_at"):
                extra += f" [next OK: {info['next_ok_at']}]"
        else:
            if info["has_fallback_configured"]:
                s0 = info["configured_fallback"][0]
                extra = f" [fallback ready: {s0['tool']}/{s0.get('model') or s0.get('tier', 'default')}]"
            else:
                extra = " [no fallback]"
        print(f"  {cls_name:20} [{state:8}] {primary_str}{extra}")
    return 0


def cmd_check_quota(args, config):
    report = check_quota_health(config, live=not args.fixtures_only)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    for c in report["checks"]:
        mark = "OK" if c["ok"] else "FAIL"
        print(f"[{mark}] {c['name']}")
        for err in c["errors"]:
            print(f"  - {err}")
    if not report["ok"]:
        print(f"aicli check-quota: {len(report['errors'])} error(s) found", file=sys.stderr)
        return 1
    print(f"aicli check-quota: all {len(report['checks'])} check(s) passed")
    return 0


def cmd_routes(args, config):
    routes = load_routes(config)
    classes = routes["classes"]
    clients = routes["clients"]
    overridden, stale = _overridden_names(config)
    if args.json:
        out = {"classes": {}, "clients": {}, "optiplex_projects": routes.get("optiplex_projects") or []}
        for name, c in classes.items():
            tool = c.get("tool", "claude")
            tier = c.get("tier", "default")
            model = c.get("model") or config.get(tool, {}).get("tiers", {}).get(tier, "")
            out["classes"][name] = {**c, "model": model, "overridden": name in overridden["classes"],
                                     "stale": name in stale["classes"]}
        for name, c in clients.items():
            tool = c.get("tool")
            tier = c.get("tier")
            model = c.get("model") or (config.get(tool, {}).get("tiers", {}).get(tier, "") if tool and tier else "")
            out["clients"][name] = {**c, "model": model, "overridden": name in overridden["clients"],
                                     "stale": name in stale["clients"]}
        print(json.dumps(out, indent=2))
        return 0

    print("WORKLOAD CLASSES:")
    for name, c in sorted(classes.items()):
        tool = c.get("tool", "claude")
        tier = c.get("tier", "default")
        effort = c.get("effort")
        model = c.get("model") or config.get(tool, {}).get("tiers", {}).get(tier, "")
        eff_str = f", effort: {effort}" if effort else ""
        fb_steps = normalize_fallback_steps(c.get("fallback"))
        fb_str = f", fallback: {fb_steps[0]['tool']}/{fb_steps[0].get('model', '')}" if fb_steps else ", fallback: none"
        tag = " [stale override]" if name in stale["classes"] else (" [overridden]" if name in overridden["classes"] else "")
        print(f"  {name:20} -> {tool:6} (tier: {tier:7}, model: {model}{eff_str}{fb_str}){tag}")
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
            tag = " [stale override]" if name in stale["clients"] else ""
            print(f"  {name:20} -> {route_str}{tag}")
    return 0


def cmd_route(args, config):
    if args.route_command == "set-class":
        updates = {"tool": args.tool, "tier": args.tier}
        if getattr(args, "effort", None):
            updates["effort"] = args.effort
        if getattr(args, "priority", None):
            updates["priority"] = args.priority
        if getattr(args, "no_fallback", False):
            updates["fallback"] = None
        elif getattr(args, "fallback_tool", None) and getattr(args, "fallback_model", None):
            fb = {"tool": args.fallback_tool, "model": args.fallback_model}
            if args.fallback_tool == "agy":
                fb["group"] = infer_agy_group(args.fallback_model)
            updates["fallback"] = fb
        apply_route_override("classes", args.name, updates, config)
        print(f"route updated: class {args.name} -> {args.tool}/{args.tier}")
        return 0
    elif args.route_command == "set-client":
        known_classes = load_routes(config)["classes"]
        if args.workload_class and args.workload_class not in known_classes:
            known = ", ".join(sorted(known_classes))
            print(f"aicli: unknown workload class {args.workload_class!r} (known: {known})", file=sys.stderr)
            return 2
        updates = {}
        if args.workload_class:
            updates["class"] = args.workload_class
        if args.tool:
            updates["tool"] = args.tool
        if args.tier:
            updates["tier"] = args.tier
        if args.effort:
            updates["effort"] = args.effort
        model_val = getattr(args, "temporary_override", None)
        if model_val:
            exp_iso = compute_model_override_expires_at()
            updates["model"] = model_val
            updates["model_expires_at"] = exp_iso
            updates["temporary_override"] = model_val
            updates["temporary_override_expires_at"] = exp_iso
        entry = apply_route_override("clients", args.name, updates, config)
        if model_val:
            print(f"route updated: client {args.name} -> {entry} (temporary_override expires in 5h; clients must reuse or create a workload class in aicli.toml instead of using overrides)")
        else:
            print(f"route updated: client {args.name} -> {entry}")
        return 0
    elif args.route_command in ("temporary-override", "set-override"):
        duration = min(float(getattr(args, "hours", 5.0) or 5.0), 5.0)
        exp_iso = (datetime.now(timezone.utc) + timedelta(hours=duration)).isoformat()
        updates = {
            "model": args.model,
            "model_expires_at": exp_iso,
            "temporary_override": args.model,
            "temporary_override_expires_at": exp_iso,
        }
        entry = apply_route_override("clients", args.client, updates, config)
        print(f"temporary_override set: client {args.client} -> {args.model} (expires in {duration:g}h at {exp_iso}; clients must reuse or create a class in aicli.toml rather than relying on overrides)")
        return 0
    elif args.route_command == "remove-client":
        routes_path = default_routes_path(config)
        state = _read_state_file(routes_path)
        if args.name in (state.get("clients") or {}):
            del state["clients"][args.name]
            save_routes(state, config, routes_path=routes_path)
            print(f"client {args.name} removed from routes")
        else:
            print(f"client {args.name} not in routes", file=sys.stderr)
            return 1
        return 0
    elif args.route_command == "reset-class":
        if reset_route_override("classes", args.name, config, field=args.field):
            print(f"class {args.name} reset" + (f" (field {args.field})" if args.field else ""))
            return 0
        print(f"class {args.name} has no override" + (f" for field {args.field}" if args.field else ""), file=sys.stderr)
        return 1
    elif args.route_command == "reset-client":
        if reset_route_override("clients", args.name, config, field=args.field):
            print(f"client {args.name} reset" + (f" (field {args.field})" if args.field else ""))
            return 0
        print(f"client {args.name} has no override" + (f" for field {args.field}" if args.field else ""), file=sys.stderr)
        return 1
    elif args.route_command == "set-allowance":
        pct = _valid_pct(args.pct)
        if pct is None:
            print(f"aicli: allowance pct must be an integer between 1 and 100, got {args.pct!r}", file=sys.stderr)
            return 2
        known_classes = load_routes(config)["classes"]
        cls_name = args.workload_class
        if cls_name not in known_classes:
            known = ", ".join(sorted(known_classes))
            print(f"aicli: unknown workload class {cls_name!r} (known: {known})", file=sys.stderr)
            return 2
        exp = compute_allowance_expires_at(config, known_classes[cls_name])
        apply_route_override("classes", cls_name, {"allowance_override": {"pct": pct, "expires_at": exp}}, config)
        print(f"route updated: class {cls_name} allowance override -> {pct}% until {exp}")
        return 0
    elif args.route_command == "clear-allowance":
        cls_name = args.workload_class
        known_classes = load_routes(config)["classes"]
        if cls_name not in known_classes:
            known = ", ".join(sorted(known_classes))
            print(f"aicli: unknown workload class {cls_name!r} (known: {known})", file=sys.stderr)
            return 2
        if reset_route_override("classes", cls_name, config, field="allowance_override"):
            print(f"class {cls_name} allowance override cleared")
        else:
            print(f"class {cls_name} has no allowance override")
        return 0
    return 1


def cmd_routes_api(args, config):
    sys.modules.setdefault("aicli", sys.modules[__name__])  # run as a script: routes_api imports this very module
    import routes_api
    if args.rapi_command == "static":
        print(routes_api.STATIC_DIR)
        return 0
    if args.rapi_command == "page":
        sys.stdout.write(routes_api.render_page(
            {"api": args.api, "scope": args.scope, "saveNote": args.save_note},
            title=args.title, back_href=args.back_href, back_label=args.back_label, lede=args.lede,
            head=args.head or f'<link rel="stylesheet" href="{args.static}/standalone.css">', static=args.static))
        return 0
    if args.rapi_command == "get":
        usage = compute_usage(config) if args.live else routes_api.cached_usage()
        out = routes_api.payload(config, usage, scope=args.scope)
    else:
        try:
            body = json.load(sys.stdin)
        except ValueError:
            print("routes-api save: stdin is not JSON", file=sys.stderr)
            return 2
        out = routes_api.save(config, body if isinstance(body, dict) else {}, scope=args.scope)
    print(json.dumps(out))
    return 0


def cmd_auth(args, config):
    cmd = args.auth_command
    if cmd == "status":
        tool_filter = getattr(args, "tool", None)
        st = auth_status(config, tool_filter)
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            items = {tool_filter: st} if tool_filter else st
            for tool, info in items.items():
                mark = "OK" if info.get("authenticated") else "FAIL"
                print(f"[{mark}] {tool}: {info.get('detail')}")
        all_ok = all(v.get("authenticated", False) for v in (st.values() if not tool_filter else [st]))
        return 0 if all_ok else 1
    elif cmd == "start":
        res = start_login(config, args.tool)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("ok"):
                if res.get("auth_url"):
                    print(f"Authorize at: {res['auth_url']}")
                else:
                    print(res.get("message") or "Login started")
            else:
                print(f"Error: {res.get('error')}", file=sys.stderr)
        return 0 if res.get("ok") else 1
    elif cmd == "submit":
        res = submit_code(config, args.tool, args.code)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("ok"):
                print(res.get("message", "Success"))
            else:
                print(f"Error: {res.get('error')}", file=sys.stderr)
        return 0 if res.get("ok") else 1
    elif cmd == "cancel":
        res = cancel_login(config, args.tool)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Cancelled {args.tool} login session")
        return 0
    return 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    handler = {
        "which": cmd_which, "argv": cmd_argv, "run": cmd_run, "resolve": cmd_resolve,
        "usage": cmd_usage, "fallback": cmd_fallback, "check-quota": cmd_check_quota,
        "routes": cmd_routes, "routes-api": cmd_routes_api, "route": cmd_route, "auth": cmd_auth,
        "skills": cmd_skills,
    }[args.command]
    return handler(args, config)


if __name__ == "__main__":
    sys.exit(main())
