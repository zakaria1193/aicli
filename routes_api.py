"""The data behind the routing page (`routes_static/`): what it loads and what
it saves. Any host serves the page by calling `payload` and `save`, or by
shelling out to `aicli routes-api get|save`.

`scope` limits the view to one client prefix (pjm passes `pjm:`): only those
clients, the classes they run as, and their own project show up, and a save can
only touch those rows. Classes stay shared, so `class_shared` says how many
clients outside the scope also run each one.
"""
import json
from pathlib import Path

import aicli

STATIC_DIR = Path(__file__).resolve().parent / "routes_static"


def _base(client_id):
    return client_id.split("@", 1)[0]


def _project(client_id):
    return _base(client_id).split(":", 1)[0]


def _in_scope(client_id, scope):
    return not scope or _base(client_id).startswith(scope)


def _class_of(client_id, row):
    return (row or {}).get("class") or (client_id.split("@", 1)[1] if "@" in client_id else "")


PAGE_VARS = ("HEADING", "TITLE", "HEAD", "BODY_CLASS", "BACK_HREF", "BACK_LABEL", "LEDE", "EXTRA_TOP",
             "USAGE_LINK", "EXTRA_BOTTOM", "STATIC")


def render_page(config=None, **parts):
    """`routes_static/page.html` with the host's pieces filled in. `config` becomes
    `window.AICLI_ROUTES` (see routes.js); `parts` are the HTML pieces, named in
    PAGE_VARS (lowercase), HTML-escaping their own text. `static` is the URL prefix
    the host serves `routes_static/` at. A scoped page gets the `scoped` body class."""
    cfg = {**(config or {})}
    page = (STATIC_DIR / "page.html").read_text(encoding="utf-8")
    parts.setdefault("heading", "AI Routing")
    parts.setdefault("title", "")
    parts["body_class"] = ("scoped " if cfg.get("scope") else "") + parts.get("body_class", "")
    parts.setdefault("static", "/routes_static")
    for name in PAGE_VARS:
        page = page.replace(f"__{name}__", parts.get(name.lower(), ""))
    # `</` would end the script block early inside an inline JSON string.
    return page.replace("__CONFIG__", json.dumps(cfg).replace("</", "<\\/"))


def cached_usage():
    """The last quota readings aicli saved, no live probe: fast enough for a page load."""
    cache = aicli._read_quota_cache()
    return {tool: (cache.get(tool) or {}).get("result") for tool in aicli.TOOLS}


def payload(config, usage=None, scope=""):
    routes = aicli.load_routes(config)
    overridden, stale = aicli._overridden_names(config)
    default_classes, default_clients, _ = aicli._default_sections(config)
    classes = {n: c for n, c in routes["classes"].items() if n not in stale["classes"]}
    clients = {n: c for n, c in routes["clients"].items() if n not in stale["clients"]}

    shared = {}
    if scope:
        for cid, row in clients.items():
            if not _in_scope(cid, scope):
                k = _class_of(cid, row)
                shared[k] = shared.get(k, 0) + 1
        clients = {n: c for n, c in clients.items() if _in_scope(n, scope)}
        used = {_class_of(n, c) for n, c in clients.items()}
        classes = {n: c for n, c in classes.items() if n in used}
        shared = {k: n for k, n in shared.items() if k in classes}

    registry = _project_registry(config)
    projects = {_project(n) for n in clients}
    if scope:
        registry = {n: p for n, p in registry.items() if n in projects}
    fb = aicli.fallback_status(config, usage or {}, routes=routes)
    fb["classes"] = {n: c for n, c in fb["classes"].items() if n in classes}
    keep = lambda names, rows: sorted(n for n in names if n in rows)
    return {
        "ok": True,
        "scope": scope,
        "classes": classes,
        "clients": clients,
        "class_shared": shared,
        "optiplex_projects": [n for n in routes.get("optiplex_projects", []) if not scope or n in projects],
        "project_registry": registry,
        "overridden": {"classes": keep(overridden["classes"], classes), "clients": keep(overridden["clients"], clients)},
        "stale": {"classes": [] if scope else sorted(stale["classes"]),
                  "clients": [] if scope else sorted(stale["clients"])},
        "default_classes": keep(default_classes, classes),
        "default_clients": keep(default_clients, clients),
        "usage": usage,
        "fallback_status": fb,
        "models": {tool: config.get(tool, {}).get("tiers", {}) for tool in aicli.TOOLS},
        "available_models": aicli.AVAILABLE_MODELS,
        "tools": list(aicli.TOOLS),
        "tiers": list(aicli.TIERS),
        "efforts": list(aicli.EFFORTS),
    }


def _project_registry(config):
    """Registered projects {name: {"enabled": bool}}, seeded from the enabled-name
    list `optiplex_projects` when absent. `project_registry` is a sibling key in the
    state file that aicli's own merge ignores."""
    routes = aicli.load_routes(config)
    raw = aicli._read_state_file(aicli.default_routes_path(config))
    reg = raw.get("project_registry")
    if not isinstance(reg, dict):
        reg = {}
    for n in routes.get("optiplex_projects", []):
        reg.setdefault(n, {"enabled": True})
    return {n: {"enabled": bool(p.get("enabled", True)) if isinstance(p, dict) else bool(p)}
            for n, p in reg.items()}


def _fallback_update(raw):
    if not raw or raw is False:
        return None
    steps = aicli.normalize_fallback_steps(raw)
    return steps[0] if len(steps) == 1 else (steps or None)


def save(config, body, scope=""):
    """Apply what the page sends. Each row is merged over its current value and diffed
    against aicli.toml by `apply_route_override`, so only real changes are written."""
    routes = aicli.load_routes(config)
    efforts = aicli.EFFORTS
    visible_clients = {n for n in routes["clients"] if _in_scope(n, scope)}
    visible_classes = {n for n in routes["classes"]
                       if not scope or n in {_class_of(c, routes["clients"][c]) for c in visible_clients}}

    if isinstance(body.get("classes"), dict):
        for name, c in body["classes"].items():
            if not isinstance(c, dict) or name not in routes["classes"] or name not in visible_classes:
                continue
            updates = {}
            if c.get("tool") in aicli.TOOLS:
                updates["tool"] = c["tool"]
            if c.get("tier") in aicli.TIERS:
                updates["tier"] = c["tier"]
            if "effort" in c:
                if c["effort"] in efforts:
                    updates["effort"] = c["effort"]
                elif c["effort"] in (None, ""):
                    updates["effort"] = None
            if "fallback" in c:
                updates["fallback"] = _fallback_update(c["fallback"])
            if updates:
                aicli.apply_route_override("classes", name, updates, config)

    if isinstance(body.get("clients"), dict):
        for name, c in body["clients"].items():
            if not isinstance(c, dict) or not _in_scope(name, scope):
                continue
            updates = {}
            # An unknown class would make every run of that client exit 2.
            if c.get("class") in routes["classes"]:
                updates["class"] = c["class"]
            if c.get("tool") in aicli.TOOLS:
                updates["tool"] = c["tool"]
            if c.get("tier") in aicli.TIERS:
                updates["tier"] = c["tier"]
            if c.get("effort") in efforts:
                updates["effort"] = c["effort"]
            if isinstance(c.get("model"), str) and c["model"].strip():
                updates["model"] = c["model"].strip()
            if c.get("description"):
                updates["description"] = c["description"]
            if c.get("frequency"):
                updates["frequency"] = c["frequency"]
            if updates:
                aicli.apply_route_override("clients", name, updates, config)
            # The page clears a pinned model/effort/tier by omitting it.
            for field in ("class", "tool", "tier", "effort", "model", "description", "frequency"):
                if field not in updates:
                    aicli.reset_route_override("clients", name, config, field=field)
        # A client the page no longer sends was removed (or renamed).
        for name in visible_clients:
            if name not in body["clients"]:
                aicli.reset_route_override("clients", name, config)

    if isinstance(body.get("reset"), dict):
        for name in body["reset"].get("classes") or []:
            if str(name) in visible_classes:
                aicli.reset_route_override("classes", str(name), config)
        for name in body["reset"].get("clients") or []:
            if _in_scope(str(name), scope):
                aicli.reset_route_override("clients", str(name), config)

    if not scope and ("project_registry" in body or "optiplex_projects" in body):
        registry = _project_registry(config)
        if isinstance(body.get("project_registry"), dict):
            registry = {
                str(n).strip(): {"enabled": bool(p.get("enabled", True)) if isinstance(p, dict) else bool(p)}
                for n, p in body["project_registry"].items() if str(n).strip()
            }
        elif isinstance(body.get("optiplex_projects"), list):
            names = {str(x).strip() for x in body["optiplex_projects"] if str(x).strip()}
            registry = {n: {"enabled": False} for n in registry}
            registry.update({n: {"enabled": True} for n in names})
        routes_path = aicli.default_routes_path(config)
        raw = aicli._read_state_file(routes_path)
        raw["project_registry"] = dict(sorted(registry.items()))
        enabled = [n for n, p in sorted(registry.items()) if p["enabled"]]
        _, _, default_optiplex = aicli._default_sections(config)
        if sorted(enabled) == sorted(default_optiplex):
            raw.pop("optiplex_projects", None)
        else:
            raw["optiplex_projects"] = enabled
        aicli.save_routes(raw, config, routes_path=routes_path)

    return {"ok": True, "routes": aicli.load_routes(config), "project_registry": _project_registry(config)}
