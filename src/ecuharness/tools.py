"""The tools an AI agent can call. One implementation serves MCP and the CLI.

Design rules (they are what keeps weak models reliable and strong models cheap):

* few tools, each with one job and flat, plainly typed arguments (only the
  JSON Schema subset every model provider accepts: no unions, no free-form
  objects, every array has typed items);
* the shortest path is short: look, `ecu_edit` (opens the change, checks every
  op, reports new findings), `ecu_change_finish`;
* every result is compact with capped lists and a short ``next`` hint;
* every error says what was wrong and what to do instead;
* reading never needs a change; writing always happens inside one.
"""
from __future__ import annotations

import difflib
import json
import os
import pkgutil
import re
import time
import xml.etree.ElementTree as ET

from . import InternalError, ToolError
from .arxml import CONTVAL, MODCONF, PKG, Cont, iter_conts, mult_text, parse_number
from .changes import Changes, Lock
from .edit import OPS, short
from .project import masked, typed_value

S, I, B = "string", "integer", "boolean"
RECIPES = ("work-loop", "add-container", "change-values", "fix-findings", "rename-remove",
           "trace-and-mirror", "review-change", "when-blocked")


class Tool:
    def __init__(self, name, title, desc, props, required, fn, write, destructive, when):
        self.name, self.title, self.desc, self.props = name, title, desc, props
        self.required, self.fn, self.write, self.destructive, self.when = required, fn, write, destructive, when

    def spec(self):
        schema = {"type": "object", "properties": self.props}
        if self.required:
            schema["required"] = list(self.required)
        return {"name": self.name, "title": self.title, "description": self.desc, "inputSchema": schema,
                "annotations": {"readOnlyHint": not self.write, "destructiveHint": self.destructive,
                                "idempotentHint": not self.write, "openWorldHint": False}}


TOOLS = []


def tool(name, title, desc, props=None, required=(), write=False, destructive=False, when=None):
    def deco(fn):
        TOOLS.append(Tool(name, title, desc, props or {}, required, fn, write, destructive, when))
        return fn
    return deco


class Ctx:
    def __init__(self, project):
        self.project = project
        self.model = project.model
        self.policy = project.policy
        self.changes = Changes(project)


def _p(type_, desc, **extra):
    d = {"type": type_, "description": desc}
    d.update(extra)
    return d


_SEV = {"error": 0, "warning": 1, "info": 2}


def _by_severity(findings):
    return sorted(findings, key=lambda f: (_SEV.get(f.severity, 3), f.id, f.path or ""))


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
@tool("ecu_overview", "Project overview", "Start here: modules, tool driver, open change.")
def overview(ctx):
    p, m = ctx.project, ctx.model
    mods = []
    for mod in m.modules()[:80]:
        d = {"name": mod.name, "path": mod.path(), "containers": m.count_containers(mod)}
        if mod.defref not in m.defs:
            d["untyped"] = True
        if mod.file.role != "config":
            d["readonly"] = True
        mods.append(d)
    out = {"project": p.name, "modules": mods, "driver": p.driver_info()}
    if len(m.modules()) > 80:
        out["more_modules"] = len(m.modules()) - 80
    others = {}
    for n in m.top_elements():
        if n.tag != MODCONF:
            others[n.tag] = others.get(n.tag, 0) + 1
    if others:
        out["other_elements"] = dict(sorted(others.items(), key=lambda kv: -kv[1])[:12])
    ch = ctx.changes.current()
    out["open_change"] = {"goal": ch["goal"], "edits": ch["edits"]} if ch else None
    if m.problems:
        out["problems"] = m.problems[:5]
    if mods:
        out["next"] = "Look with ecu_find / ecu_get. To change something call ecu_edit with a goal."
    elif others:
        out["next"] = ("No ECU configuration modules here; the other elements can be searched with "
                       "ecu_find scope=all and read with ecu_get. They cannot be edited.")
    else:
        out["next"] = ("Nothing is loaded. Check model.config in ecuharness.json (now: %s) or run `ecuh doctor`."
                       % json.dumps(p.cfg["model"].get("config")))
    return out


def _kind(n):
    if n.tag == CONTVAL:
        return short(n.defref)
    return "module" if n.tag == MODCONF else n.tag


def _values(ctx, entries, only=None):
    out = {}
    for e in entries or ():
        if only is not None and e.name.lower() not in only:
            continue
        v = "***" if masked(ctx.policy, e.name) else typed_value(e, ctx.model.defs.get(e.defref))
        if e.name in out:
            out[e.name] = (out[e.name] if isinstance(out[e.name], list) else [out[e.name]]) + [v]
        else:
            out[e.name] = v
    return out


@tool("ecu_find", "Search the configuration",
      "Search containers by name, kind, parameter value or reference relation. Returns paths for the "
      "other tools; 'show' adds parameter values to each result, so one call can answer a question.",
      {"query": _p(S, "Text that the short name must contain."),
       "kind": _p(S, "Container kind (definition name), e.g. ComSignal."),
       "under": _p(S, "Only search below this path."),
       "param": _p(S, "Only containers that set this parameter or reference."),
       "value": _p(S, "With param: only containers where it has this value."),
       "show": _p(S, "Parameter names to include in each result, comma-separated."),
       "targets_of": _p(S, "Path: only the containers that this container refers to."),
       "users_of": _p(S, "Path: only the containers that refer to this container."),
       "scope": _p(S, "config = ECUC containers (default); definitions = parameter definitions; "
                      "all = every named element of all loaded ARXML files.",
                   enum=["config", "definitions", "all"]),
       "limit": _p(I, "Maximum results, default 20."),
       "offset": _p(I, "Skip this many results (paging).")})
def find(ctx, query=None, kind=None, under=None, param=None, value=None, show=None, targets_of=None,
         users_of=None, scope="config", limit=20, offset=0):
    m = ctx.model
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    q = (query or "").strip().lower()
    if scope == "definitions":
        return _find_defs(m, q, kind, limit, offset)
    if param and value is not None and masked(ctx.policy, param):
        raise ToolError("Values of '%s' are masked by project policy and cannot be searched." % param)
    via = {}
    prefix = None
    if targets_of or users_of:
        cands, seen = [], set()
        if targets_of:
            src = m.resolve(targets_of)
            for e in getattr(src, "refs", None) or ():
                n = m.node(e.value) if e.value else None
                if n is not None and id(n) not in seen:
                    seen.add(id(n))
                    cands.append(n)
                    via[id(n)] = e.name
        else:
            tgt = m.resolve(users_of)
            for c, e in m.refs_to().get(tgt.path(), ()):
                if id(c) not in seen:
                    seen.add(id(c))
                    cands.append(c)
                    via[id(c)] = e.name
        if under:
            prefix = m.resolve(under).path().rstrip("/") + "/"
        sources = [cands]
        roots = []
    else:
        roots = [m.resolve(under)] if under else (m.modules() if scope == "config" else [m.root])
        sources = [(iter_conts(r) if scope == "config" else r.walk()) for r in roots]
    want = str(value).strip() if value is not None else None
    wnum = parse_number(want) if want is not None else None
    knd = kind.strip().lower() if kind else None
    shown = [x.strip().lower() for x in re.split(r"[,;\s]+", show or "") if x.strip()][:8]
    matches, total = [], 0
    for source in sources:
        for n in source:
            if n.tag == PKG or (q and q not in n.name.lower()):
                continue
            if knd and _kind(n).lower() != knd:
                continue
            if prefix and not n.path().startswith(prefix):
                continue
            hit = None
            if param:
                if not isinstance(n, Cont):
                    continue
                es = [e for e in list(n.params or ()) + list(n.refs or ()) if e.name.lower() == param.lower()]
                if not es:
                    continue
                if want is not None:
                    es = [e for e in es if (e.value or "") == want or (e.value or "").lower() == want.lower()
                          or (e.is_ref and (e.value or "").endswith("/" + want.strip("/")))
                          or (wnum is not None and parse_number(e.value) == wnum)]
                    if not es:
                        continue
                hit = _values(ctx, es)
            total += 1
            if offset < total <= offset + limit:
                row = {"path": n.path()}
                if not knd:
                    row["kind"] = _kind(n)
                if id(n) in via:
                    row["via"] = via[id(n)]
                if hit:
                    row.update(hit)
                if shown and isinstance(n, Cont):
                    row.update(_values(ctx, list(n.params or ()) + list(n.refs or ()), shown))
                matches.append(row)
    out = {"total": total, "matches": matches}
    if total > offset + limit:
        out["more"] = "%d more; raise offset or narrow the search." % (total - offset - limit)
    if total == 0:
        out["hint"] = _no_match_hint(m, roots, q, knd, scope) if roots else "No such relation exists."
    return out


def _no_match_hint(m, roots, q, knd, scope):
    names, kinds = set(), set()
    for root in roots:
        for n in (iter_conts(root) if scope == "config" else root.walk()):
            if len(names) < 20000:
                names.add(n.name)
            kinds.add(_kind(n))
    tips = []
    if q:
        close = difflib.get_close_matches(q, [n.lower() for n in names], n=5, cutoff=0.6)
        real = [n for n in names if n.lower() in close][:5]
        if real:
            tips.append("Similar names: %s." % ", ".join(sorted(real)))
    if knd and knd not in {k.lower() for k in kinds}:
        tips.append("Kinds that exist here: %s." % ", ".join(sorted(kinds)[:25]))
    return " ".join(tips) or "Nothing matches. Try a shorter query, or ecu_overview to see the modules."


def _find_defs(m, q, kind, limit, offset):
    rows = []
    for d in m.defs.values():
        if q and q not in d.name.lower() and q not in (d.desc or "").lower():
            continue
        t = getattr(d, "kind", None) or ("module" if getattr(d, "is_module", False) else "container")
        if kind and kind.lower() not in (t, d.name.lower()):
            continue
        rows.append({"definition": d.path, "type": t, "about": (d.desc or "")[:100]})
    out = {"total": len(rows), "matches": rows[offset:offset + limit]}
    if not m.defs:
        out["hint"] = "No definitions are loaded. Add definition files to model.definitions in ecuharness.json."
    elif len(rows) > offset + limit:
        out["more"] = "%d more; raise offset or narrow the search." % (len(rows) - offset - limit)
    return out


def _children(n):
    groups = {}
    for k in (n.kids or {}).values():
        groups.setdefault(_kind(k), []).append(k.name)
    return {kind: {"count": len(names), "names": names[:25]} for kind, names in groups.items()}


def _props(n, budget=1400):
    """Compact view of a non-ECUC element (component, interface, manifest ...)."""
    f = n.file
    if f is None or not f.data or n.s < 0 or f.enc.lower().replace("-", "") != "utf8":
        return None
    try:
        el = ET.fromstring(f.data[n.s:n.e])
    except ET.ParseError:
        return None
    left = [budget]

    def conv(e, depth):
        if left[0] <= 0:
            return "..."
        kids = list(e)
        if not kids:
            t = " ".join((e.text or "").split())
            left[0] -= len(t) + 4
            return t
        if depth and any(k.tag == "SHORT-NAME" for k in kids):
            name = next((k.text or "" for k in kids if k.tag == "SHORT-NAME"), "")
            left[0] -= len(name) + 4
            return name.strip()                 # a named child: read it with its own path
        out = {}
        for k in kids:
            if k.tag == "SHORT-NAME":
                continue
            left[0] -= len(k.tag) + 4
            v = conv(k, depth + 1)
            if k.tag in out:
                out[k.tag] = (out[k.tag] if isinstance(out[k.tag], list) else [out[k.tag]]) + [v]
            else:
                out[k.tag] = v
            if left[0] <= 0:
                out["..."] = "cut; use raw=true or read the children"
                break
        return out

    props = conv(el, 0)
    return props if isinstance(props, dict) and props else None


def _view(ctx, n, depth, budget):
    out = {"path": n.path(), "kind": _kind(n)}
    if isinstance(n, Cont):
        if n.is_module:
            out["definition"] = n.defref
        vals = _values(ctx, n.params)
        if vals:
            out["params"] = vals
        refs = _values(ctx, n.refs)
        if refs:
            out["refs"] = refs
    elif n.tag not in (PKG, "#root"):
        props = _props(n)
        if props:
            out["props"] = props
    kids = list((n.kids or {}).values())
    if kids and depth > 0 and budget[0] > 0:
        shown = []
        for k in kids:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            shown.append(_view(ctx, k, depth - 1, budget))
        out["children"] = shown
        if len(shown) < len(kids):
            out["children_not_shown"] = len(kids) - len(shown)
    elif kids:
        out["children"] = _children(n)
    return out


def _get_one(ctx, path, depth, raw, budget):
    m = ctx.model
    n = m.resolve(path)
    out = _view(ctx, n, depth, budget)
    if n.file is not None and n.file.role != "config":
        out["readonly"] = True
    if n.tag == CONTVAL:
        users = m.refs_to().get(out["path"], [])
        if users:
            out["referenced_by"] = {"count": len(users),
                                    "first": ["%s.%s" % (c.path(), e.name) for c, e in users[:5]]}
    if raw and n.file is not None and n.s >= 0 and n.file.data:
        if any(masked(ctx.policy, e.name) for c in [n] + list(n.walk()) if isinstance(c, Cont)
               for e in list(c.params or ())):
            out["xml"] = "(not shown: contains masked parameters)"
        else:
            text = n.file.data[n.s:n.e].decode(n.file.enc, "replace")
            out["xml"] = text[:2000] + ("..." if len(text) > 2000 else "")
    return out


@tool("ecu_get", "Read elements",
      "Read elements by path: values, references, children. Several paths separated by spaces are read "
      "in one call.",
      {"path": _p(S, "AUTOSAR path such as /ActiveEcuC/Com/ComConfig or Com/ComConfig; or several."),
       "depth": _p(I, "0 = list child names (default), 1-3 = include children with their values."),
       "raw": _p(B, "Also return the element's XML text (max 2000 characters).")},
      required=("path",))
def get(ctx, path, depth=0, raw=False):
    paths = [x for x in re.split(r"[\s,;]+", path.strip()) if x][:10]
    depth = max(0, min(int(depth), 3))
    budget = [40]
    if len(paths) <= 1:
        return _get_one(ctx, path, depth, raw, budget)
    out = []
    for pth in paths:
        try:
            out.append(_get_one(ctx, pth, depth, raw, budget))
        except ToolError as exc:
            out.append(dict(exc.to_dict(), path=pth))
    return {"elements": out}


def _pline(p, full=False):
    """One line per parameter: 'Name: type range, default, required. About.'"""
    if p.is_ref:
        what = "%s to %s" % (p.kind, " or ".join(short(x) for x in p.dests or []) or p.dest_type or "an element")
    elif p.kind == "enum":
        lits = p.literals or []
        what = "enum " + " | ".join(lits[:40]) + (" | ..." if len(lits) > 40 else "")
    elif p.min is not None or p.max is not None:
        what = "%s %s..%s" % (p.kind, "" if p.min is None else p.min, "" if p.max is None else p.max)
    else:
        what = p.kind
    bits = [what]
    if p.default is not None:
        bits.append("default %s" % p.default)
    if p.lower >= 1 and (p.is_ref or p.default is None):
        bits.append("required")
    if p.upper != 1:
        bits.append("list %s" % mult_text(p))
    line = "%s: %s" % (p.name, ", ".join(bits))
    if p.desc:
        line += ". " + (p.desc if full else p.desc[:100])
    return line


@tool("ecu_describe", "Explain a definition",
      "What a container kind or parameter allows: type, range, allowed values, required, children. "
      "Check before adding containers or setting values you are unsure about.",
      {"kind": _p(S, "Definition name or path, e.g. ComSignal or Com/ComConfig/ComSignal."),
       "path": _p(S, "Or: path of an existing container; describes its kind."),
       "filter": _p(S, "Only parameters whose name contains this text.")})
def describe(ctx, kind=None, path=None, filter=None):
    m = ctx.model
    if path:
        n = m.resolve(path)
        d = m.defs.get(getattr(n, "defref", "") or "")
        if d is None:
            raise ToolError(
                "No definition is loaded for %s." % n.path(),
                "Existing values can still be changed and containers copied with op 'clone'. For type checks "
                "the user must add the module's definition file to model.definitions in ecuharness.json.")
    elif kind:
        d = _lookup_def(m, kind)
    else:
        raise ToolError("Give 'kind' or 'path'.")
    if getattr(d, "kind", None):            # a parameter or reference definition
        return {"definition": d.path, "param": _pline(d, full=True)}
    out = {"definition": d.path, "type": "module" if d.is_module else "container"}
    if not d.is_module:
        out["count_allowed"] = mult_text(d)
    if d.desc:
        out["about"] = d.desc[:300]
    f = (filter or "").lower()
    ps = [p for p in list(d.params.values()) + list(d.refs.values()) if f in p.name.lower()]
    if ps:
        brief = len(ps) > 15 and not f      # many parameters: names and types only, details via filter
        out["params"] = [(_pline(p).split(". ")[0] if brief else _pline(p)) for p in ps[:60]]
        if brief:
            out["note"] = "Descriptions left out; use 'filter' to read about specific parameters."
        if len(ps) > 60:
            out["more_params"] = "%d more; use 'filter' to narrow." % (len(ps) - 60)
    if d.subs:
        out["children"] = ["%s (%s)%s" % (s.name, mult_text(s), ": " + s.desc[:80] if s.desc else "")
                           for s in list(d.subs.values())[:40]]
    if d.choice:
        out["note"] = "Choice container: holds exactly one of its children."
    return out


def _lookup_def(m, text):
    t = text.strip().replace("\\", "/")
    if not m.defs:
        raise ToolError("No definitions are loaded.",
                        "The user must add definition files to model.definitions in ecuharness.json.")
    if t in m.defs:
        return m.defs[t]
    tail = "/" + t.strip("/")
    hits = [d for d in m.defs.values() if d.path.endswith(tail)]
    if not hits:
        hits = [d for d in m.defs.values() if d.path.lower().endswith(tail.lower())]
    if len(hits) > 1:
        conts = [d for d in hits if not getattr(d, "kind", None)]
        if len(conts) == 1:
            return conts[0]
        raise ToolError("'%s' is ambiguous." % text,
                        "Use one of: %s." % ", ".join(d.path for d in hits[:8]))
    if not hits:
        names = sorted({d.name for d in m.defs.values()})
        close = difflib.get_close_matches(t.rsplit("/", 1)[-1], names, n=6, cutoff=0.5)
        raise ToolError("No definition named '%s'." % text,
                        ("Did you mean: %s? " % ", ".join(close) if close else "")
                        + "Search with ecu_find scope=definitions.")
    return hits[0]


# --------------------------------------------------------------------------
# Changing
# --------------------------------------------------------------------------
OP_ITEM = {"type": "object", "required": ["op"], "properties": {
    "op": _p(S, "set = set a value. append = add one more value to a list parameter. unset = clear a "
                "parameter (with value: only that one). add = new container. clone = copy a container. "
                "rename, remove = a container.", enum=list(OPS)),
    "path": _p(S, "Container the op acts on (all ops except add)."),
    "param": _p(S, "Parameter or reference name (set, append, unset)."),
    "value": _p(S, "The value as text: 8, true, UINT8, or the path of a reference target."),
    "parent": _p(S, "Path of the container that receives the new one (add)."),
    "kind": _p(S, "Kind of the new container, e.g. ComSignal (add)."),
    "name": _p(S, "Short name of the new container (add, clone) or the new name (rename)."),
    "values": {"type": "array", "description": "Initial values (add) or overrides (clone).",
               "items": {"type": "object", "required": ["param", "value"],
                         "properties": {"param": {"type": S}, "value": {"type": S}}}},
}}


@tool("ecu_edit", "Edit the configuration",
      "Change the configuration. All ops succeed together or nothing is written; values are type-checked, "
      "references stay consistent, and the result lists new findings. The first call needs 'goal' and "
      "opens a change.",
      {"ops": {"type": "array", "description": "Operations, applied in order.", "items": OP_ITEM},
       "goal": _p(S, "One sentence: what will change and why. Needed when no change is open."),
       "ticket": _p(S, "Optional ticket or requirement id, stored with the change."),
       "dry_run": _p(B, "Check the ops and show the result without writing.")},
      required=("ops",), write=True)
def edit(ctx, ops, goal=None, ticket=None, dry_run=False):
    ch = ctx.changes.current()
    out = {}
    if ch is None and dry_run:
        from .edit import plan_ops
        plan = plan_ops(ctx.model, ctx.policy, ops)
        return {"written": False, "changes": plan.changes[:60],
                "next": "Dry run only. Call ecu_edit with a goal and without dry_run to write."}
    if ch is None:
        if not goal:
            raise ToolError("No change is open.",
                            "Call ecu_edit again with 'goal': one sentence saying what will change and why.")
        ch = ctx.changes.begin(goal, ticket)
        out["opened"] = {"goal": ch["goal"], "errors_before": ch["baseline"]["errors"]}
        if ch["baseline"]["driver"]["status"] != "ok":
            out["warning"] = ("The tool driver is not working (%s), so this change cannot be finished until "
                              "it is. Tell the user." % (ch["baseline"]["driver"].get("detail") or "unavailable"))
    plan = ctx.changes.edit(ch, ops, bool(dry_run))
    written = bool(plan.files) and not dry_run
    out["written"] = written
    out["changes"] = plan.changes[:60]
    if len(plan.changes) > 60:
        out["more_changes"] = len(plan.changes) - 60
    if plan.notes:
        out["notes"] = plan.notes[:5]
    if dry_run:
        out["next"] = "Dry run only. Call ecu_edit again without dry_run to write."
        return out
    if not plan.changes:
        out["next"] = "No difference: the configuration already has these values."
        return out
    drv = ctx.project.driver
    if drv.type in ("none", "sim") or ctx.project.cfg["driver"].get("validateOnEdit"):
        findings, res = ctx.changes.run_validation(ch)
        d = ctx.changes.delta(ch, findings, res)
        out["gate"] = d["gate"]
        if d["reasons"] and not d["blocking"]:
            out["why"] = d["reasons"]
    else:                                   # a vendor tool run is slow: check structure now, the tool at finish
        d = ctx.changes.delta(ch, ctx.changes.structure_check(), structure_only=True)
        out["gate"] = "fail" if d["blocking"] else "pending"
    new = _by_severity(d["new"])
    if new:
        out["new"] = [f.to_dict() for f in new[:8]]
        if len(new) > 8:
            out["new_more"] = len(new) - 8
    if out["gate"] == "pass":
        out["next"] = "No blocking findings. Call ecu_change_finish with a summary, or continue editing."
    elif out["gate"] == "pending":
        out["next"] = ("Structure is fine. The vendor tool has not judged it yet; ecu_change_finish runs it "
                       "(ecu_validate does too).")
    else:
        out["next"] = "Fix the new findings with ecu_edit (see each 'fix')."
    return out


@tool("ecu_validate", "Validate",
      "Run the full validation. With an open change: the findings it introduced and whether it may be "
      "finished (gate).",
      {"scope": _p(S, "new = only findings introduced by the open change (default); all = every finding.",
                   enum=["new", "all"]),
       "limit": _p(I, "Maximum findings to list, default 20."),
       "offset": _p(I, "Skip this many findings (paging).")})
def validate(ctx, scope="new", limit=20, offset=0):
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    ch = ctx.changes.current()
    findings, drv = ctx.changes.run_validation(ch)
    findings = _by_severity(findings)
    out = {"errors": sum(1 for f in findings if f.severity == "error"),
           "warnings": sum(1 for f in findings if f.severity == "warning")}
    if ctx.project.driver.type != "none" or drv.status != "ok":
        out["driver"] = drv.summary()
    if ch is not None:
        d = ctx.changes.delta(ch, findings, drv)
        ext = ctx.changes.external(ch)
        out["gate"] = "fail" if ext else d["gate"]
        why = list(d["reasons"])
        if ext:
            why.append("files changed outside the harness: %s" % ", ".join(ext[:5]))
        if why:
            out["why"] = why
        new = _by_severity(d["new"])
        out["new"] = [f.to_dict() for f in new[offset:offset + limit]]
        if len(new) > offset + limit:
            out["new_more"] = len(new) - offset - limit
        out["resolved"] = len(d["resolved"])
        if out["gate"] == "pass":
            out["next"] = "Gate passed. Call ecu_change_finish with a short summary, or continue editing."
        elif ext:
            out["next"] = "Stop and tell the user about the outside edits."
        else:
            out["next"] = "Fix the new findings with ecu_edit (see each 'fix'), then validate again."
    if scope == "all" or ch is None:
        out["findings"] = [f.to_dict() for f in findings[offset:offset + limit]]
        if len(findings) > offset + limit:
            out["findings_more"] = len(findings) - offset - limit
    return out


@tool("ecu_change_diff", "Show the change",
      "Everything the open change has modified so far, computed from the files.")
def change_diff(ctx):
    ch = ctx.changes.require()
    lines = ctx.changes.diff(ch)
    out = {"goal": ch["goal"], "edits": ch["edits"], "changes": lines[:80]}
    if len(lines) > 80:
        out["more_changes"] = len(lines) - 80
    ext = ctx.changes.external(ch)
    if ext:
        out["outside_edits"] = ext[:10]
    return out


@tool("ecu_change_finish", "Finish the change",
      "Validate and close the open change. Writes the review report for the user. Nothing is committed "
      "to version control.",
      {"summary": _p(S, "What was changed and why, for the human reviewer.")},
      required=("summary",), write=True)
def change_finish(ctx, summary):
    ch = ctx.changes.require()
    r = ctx.changes.finish(ch, summary)
    out = {"status": "finished", "changes": r["lines"][:40], "report": r["report"]}
    if len(r["lines"]) > 40:
        out["more_changes"] = len(r["lines"]) - 40
    out["next"] = "Tell the user what changed and where the report is. They review and commit."
    return out


@tool("ecu_change_discard", "Discard the change",
      "Undo every edit of the open change by restoring the files as they were, and close it.",
      write=True, destructive=True)
def change_discard(ctx):
    ch = ctx.changes.require()
    restored, left = ctx.changes.discard(ch)
    out = {"status": "discarded", "restored_files": restored}
    if left:
        out["not_restored"] = left
        out["next"] = "These files were changed outside the harness and are untouched. Tell the user."
    return out


def _driver_action(ctx, label, run):
    ch = ctx.changes.current()
    if ch is not None:
        ctx.changes.require_clean(ch)
    r, modified = ctx.changes.run_driver(label, lambda: run(ctx.project.driver), ch)
    if r.status != "ok":
        raise ToolError("%s did not succeed (%s): %s" % (label, r.status, r.detail or "no details"),
                        "Do not retry blindly. Report this to the user with the text above.")
    out = dict(r.summary(), action=label)
    if modified:
        out["config_files_changed_by_tool"] = modified
    return out


@tool("ecu_generate", "Generate code",
      "Run the code generator of the vendor tool. Slow; only when the user asked for it.",
      write=True, when=lambda p: p.driver.type != "none" and p.policy.get("allowGenerate", True))
def generate(ctx):
    return _driver_action(ctx, "generate", lambda d: d.generate())


@tool("ecu_script_run", "Run an approved script task",
      "Run one of the automation script tasks the project owner approved.",
      {"task": _p(S, "Name of the approved script task."),
       "args": {"type": "array", "description": "Arguments, if the task takes any.",
                "items": {"type": "object", "required": ["name", "value"],
                          "properties": {"name": {"type": S}, "value": {"type": S}}}}},
      required=("task",), write=True, when=lambda p: bool(p.policy.get("scriptTasks")))
def script_run(ctx, task, args=None):
    allowed = ctx.policy.get("scriptTasks") or {}
    if task not in allowed:
        raise ToolError("'%s' is not an approved script task." % task,
                        "Approved: %s. Others need the user to add them to policy.scriptTasks."
                        % (", ".join("%s (%s)" % (k, (v or {}).get("description", "no description"))
                                     for k, v in allowed.items()) or "none"))
    kv = {}
    for a in args or []:
        if not isinstance(a, dict) or "name" not in a:
            raise ToolError("Each entry of 'args' needs 'name' and 'value'.")
        kv[str(a["name"])] = str(a.get("value", ""))
    return _driver_action(ctx, "script %s" % task, lambda d: d.run_script(task, kv))


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------
def load_recipes(project=None):
    """name -> markdown text. Project recipes override the built-in ones."""
    items = {}
    for name in RECIPES:
        try:
            data = pkgutil.get_data("ecuharness", "recipes/%s.md" % name)
        except (OSError, ValueError):
            data = None
        if data:
            items[name] = data.decode("utf-8").replace("\r\n", "\n")
    if project is not None:
        for d in project.cfg.get("recipes") or []:
            full = os.path.join(project.root, d)
            if os.path.isdir(full):
                for fn in sorted(os.listdir(full)):
                    if fn.endswith(".md"):
                        with open(os.path.join(full, fn), "r", encoding="utf-8") as fh:
                            items[fn[:-3]] = fh.read()
    return items


def recipe_meta(name, text):
    title, when = name, ""
    for line in text.splitlines():
        if line.startswith("# ") and title == name:
            title = line[2:].strip()
        elif line.lower().startswith("use when:"):
            when = line.split(":", 1)[1].strip()
            break
    return title, when


@tool("ecu_recipe", "Step-by-step procedures",
      "Proven procedures for common tasks. Without name: list them. With name: read one.",
      {"name": _p(S, "Recipe name from the list.")})
def recipe(ctx, name=None):
    items = load_recipes(ctx.project)
    if not name:
        return {"recipes": ["%s: %s" % (k, recipe_meta(k, v)[1]) for k, v in sorted(items.items())]}
    key = name.strip().lower().replace(" ", "-")
    if key not in items:
        close = difflib.get_close_matches(key, list(items), n=3, cutoff=0.4)
        raise ToolError("No recipe named '%s'." % name, "Available: %s." % ", ".join(close or sorted(items)))
    return {"name": key, "text": items[key]}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def available(project=None):
    out = []
    for t in TOOLS:
        if t.when is not None:
            try:
                if project is None or not t.when(project):
                    continue
            except Exception:
                continue
        out.append(t)
    return out


_ALIASES = {"definition": "kind", "q": "query", "name_contains": "query", "paths": "path"}
_IGNORED = ("change_id", "changeId")       # one change is open at a time; an id is never needed


def clean_args(t, args):
    """Check arguments and repair the slips small models typically make."""
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if not isinstance(args, dict):
        raise ToolError("Arguments of %s must be a JSON object." % t.name)
    out = {}
    for k, v in args.items():
        if k not in t.props:
            alias = _ALIASES.get(k)
            if alias in t.props and alias not in args:
                k = alias
            elif k in _IGNORED:
                continue
            else:
                raise ToolError("%s has no argument '%s'." % (t.name, k),
                                "Arguments: %s." % (", ".join(t.props) or "none"))
        if v is None:
            continue
        typ = t.props[k]["type"]
        if typ == I:
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                v = int(v)
            elif isinstance(v, float) and v.is_integer():
                v = int(v)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ToolError("Argument '%s' must be a whole number." % k)
        elif typ == B:
            if isinstance(v, str):
                v = {"true": True, "false": False, "1": True, "0": False}.get(v.strip().lower(), v)
            if not isinstance(v, bool):
                raise ToolError("Argument '%s' must be true or false." % k)
        elif typ == S:
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (int, float)):
                v = str(v)
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                v = " ".join(v)             # a list where text was expected, e.g. several paths
            if not isinstance(v, str):
                raise ToolError("Argument '%s' must be text." % k)
            if v.strip() == "" and k not in t.required:
                continue
        elif typ == "array":
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    raise ToolError("Argument '%s' must be a JSON list." % k)
            if isinstance(v, dict):
                v = [v]
            if not isinstance(v, list):
                raise ToolError("Argument '%s' must be a list." % k)
        enum = t.props[k].get("enum")
        if enum and v not in enum:
            ci = [x for x in enum if str(x).lower() == str(v).lower()]
            if not ci:
                raise ToolError("'%s' is not valid for '%s'." % (v, k), "Use one of: %s." % ", ".join(enum))
            v = ci[0]
        out[k] = v
    for r in t.required:
        if r not in out or out[r] in ("", []):
            raise ToolError("%s needs the argument '%s'." % (t.name, r), t.props[r].get("description"))
    return out


def call_tool(project, name, args):
    """-> (result dict, is_error). Never raises for tool-level problems."""
    t = next((x for x in available(project) if x.name == name), None)
    if t is None:
        raise KeyError(name)
    t0 = time.time()
    ok = False
    try:
        kwargs = clean_args(t, args)
        if t.write:
            with Lock(project.state):
                project.model.refresh()
                result = t.fn(Ctx(project), **kwargs)
        else:
            project.model.refresh()
            result = t.fn(Ctx(project), **kwargs)
        ok = True
        return result, False
    except ToolError as exc:
        return exc.to_dict(), True
    except InternalError as exc:
        return {"error": "The harness could not prove the result of this operation, so nothing was written: %s"
                         % exc,
                "hint": "This is a harness defect, not your mistake. Do not edit the files another way. "
                        "Tell the user and include this message."}, True
    finally:
        try:
            brief = json.dumps(args, ensure_ascii=False, default=str)[:2000] if args else ""
            project.audit({"tool": name, "args": brief, "ok": ok, "ms": int((time.time() - t0) * 1000)})
        except Exception:
            pass
