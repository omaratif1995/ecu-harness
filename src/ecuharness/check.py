"""Structural checks of ECUC values against their definitions.

These are the checks the harness can do by itself, without any vendor tool:
does every value fit the type, range and multiplicity its definition allows,
and does every reference point at something that exists and has the right
kind. Semantic rules across modules are the vendor tool's job (see drivers).
"""
from __future__ import annotations

import hashlib

from .arxml import CONTVAL, IREFVAL, Cont, iter_conts, parse_number
from .edit import SHORT_NAME_RE, short

SEVERITIES = ("error", "warning", "info")


class Finding:
    __slots__ = ("id", "severity", "path", "param", "message", "fix", "source")

    def __init__(self, id, severity, path, message, param=None, fix=None, source="structure"):
        self.id = id
        self.severity = severity
        self.path = path
        self.param = param
        self.message = message
        self.fix = fix
        self.source = source

    def key(self):
        raw = "|".join((self.id, self.path or "", self.param or "", self.message))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def to_dict(self, with_key=False):
        d = {"id": self.id, "severity": self.severity, "path": self.path, "message": self.message}
        if self.param:
            d["param"] = self.param
        if self.fix:
            d["fix"] = self.fix
        if self.source != "structure":
            d["source"] = self.source
        if with_key:
            d["key"] = self.key()
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["severity"], d.get("path"), d["message"], d.get("param"),
                   d.get("fix"), d.get("source", "structure"))


def _range(p):
    return "%s..%s" % ("" if p.min is None else p.min, "" if p.max is None else p.max)


def check_model(model, cfg=None):
    """Run all structural rules. Returns a list of Finding."""
    out = []
    defs = model.defs
    check_foreign = bool(((cfg or {}).get("checks") or {}).get("foreignRefs"))
    for problem in model.problems:
        out.append(Finding("S006", "error", None, problem,
                           fix="Duplicate or unreadable elements must be fixed in the file."))
    for mod in model.modules():
        mdef = defs.get(mod.defref)
        if mdef is None:
            out.append(Finding("S000", "info", mod.path(),
                               "No definition loaded for module %s (%s); its values are not type-checked."
                               % (mod.name, mod.defref or "no DEFINITION-REF"),
                               fix="Add the module's definition file to model.definitions in ecuharness.json."))
        _children(mod, mdef, out)
        for c in iter_conts(mod):
            path = None
            if not SHORT_NAME_RE.match(c.name or ""):
                path = c.path()
                out.append(Finding("S007", "warning", path, "'%s' is not a valid AUTOSAR short name." % c.name))
            cdef = defs.get(c.defref)
            if cdef is None:
                if mdef is not None:
                    out.append(Finding("S001", "error", c.path(),
                                       "Unknown container definition %s." % c.defref,
                                       fix="The definition does not exist in the loaded module definition."))
                _refs_exist(model, c, out, check_foreign)
                continue
            path = path or c.path()
            counts = {}
            for e in c.params or ():
                counts[e.name] = counts.get(e.name, 0) + 1
                p = defs.get(e.defref)
                if p is None or p.is_ref:
                    out.append(Finding("S001", "warning", path, "Unknown parameter definition %s." % e.defref,
                                       param=e.name))
                    continue
                _value(p, e, path, out)
            for e in c.refs or ():
                counts[e.name] = counts.get(e.name, 0) + 1
                p = defs.get(e.defref)
                if p is None or not p.is_ref:
                    out.append(Finding("S001", "warning", path, "Unknown reference definition %s." % e.defref,
                                       param=e.name))
                    continue
                _ref(model, p, e, path, out, check_foreign)
            for p in list(cdef.params.values()) + list(cdef.refs.values()):
                n = counts.get(p.name, 0)
                if p.upper is not None and n > p.upper:
                    out.append(Finding("S003", "error", path,
                                       "%s is set %d times; at most %d allowed." % (p.name, n, p.upper),
                                       param=p.name, fix="Remove the surplus values."))
                elif n < p.lower and (p.is_ref or p.default is None):
                    out.append(Finding("S003", "error", path,
                                       "Required %s %s is missing." % ("reference" if p.is_ref else "parameter", p.name),
                                       param=p.name, fix="Set it with ecu_edit op 'set'."))
            _children(c, cdef, out)
    return out


def _children(c, cdef, out):
    if cdef is None:
        return
    counts = {}
    for k in (c.kids or {}).values():
        if isinstance(k, Cont):
            counts[k.defref] = counts.get(k.defref, 0) + 1
    path = None
    for s in cdef.subs.values():
        n = counts.get(s.path, 0)
        if s.upper is not None and n > s.upper:
            path = path or c.path()
            out.append(Finding("S003", "error", path, "%d %s containers; at most %d allowed." % (n, s.name, s.upper),
                               fix="Remove the surplus containers."))
        elif n < s.lower and not cdef.choice:
            path = path or c.path()
            out.append(Finding("S003", "error", path,
                               "Needs at least %d %s container(s); has %d." % (s.lower, s.name, n),
                               fix="Add one with ecu_edit op 'add' (definition %s)." % s.name))
    if cdef.choice and sum(counts.values()) > 1:
        out.append(Finding("S003", "error", c.path(), "Choice container holds more than one choice."))


def _value(p, e, path, out):
    v = e.value
    if v is None or v == "":
        out.append(Finding("S002", "error", path, "%s has no value." % p.name, param=p.name))
        return
    if p.kind in ("integer", "float"):
        n = parse_number(v)
        if n is None or (p.kind == "integer" and isinstance(n, float)):
            out.append(Finding("S002", "error", path, "%s=%s is not a valid %s." % (p.name, v, p.kind), param=p.name))
        elif (p.min is not None and n < p.min) or (p.max is not None and n > p.max):
            out.append(Finding("S002", "error", path,
                               "%s=%s is out of range %s." % (p.name, v, _range(p)), param=p.name,
                               fix="Set a value within %s." % _range(p)))
    elif p.kind == "boolean":
        if v.lower() not in ("true", "false", "1", "0"):
            out.append(Finding("S002", "error", path, "%s=%s is not a boolean." % (p.name, v), param=p.name))
    elif p.kind == "enum":
        if p.literals and v not in p.literals:
            out.append(Finding("S002", "error", path,
                               "%s=%s is not one of its literals." % (p.name, v), param=p.name,
                               fix="Allowed: %s." % " | ".join(p.literals[:20])))


def _ref(model, p, e, path, out, check_foreign):
    if e.tag == IREFVAL or p.kind in ("instance-reference", "foreign-reference", "uri-reference"):
        if check_foreign and e.value and model.node(e.value) is None:
            out.append(Finding("S004", "warning", path, "%s points to %s, which is not in the loaded model."
                               % (p.name, e.value), param=p.name))
        return
    if not e.value:
        out.append(Finding("S004", "error", path, "%s has no target." % p.name, param=p.name))
        return
    t = model.node(e.value)
    if t is None:
        out.append(Finding("S004", "error", path, "%s points to %s, which does not exist." % (p.name, e.value),
                           param=p.name, fix="Retarget it with ecu_edit op 'set', or unset it with value null."))
    elif p.dests and (t.tag != CONTVAL or not any(model.dest_ok(t.defref, d) for d in p.dests)):
        out.append(Finding("S005", "error", path,
                           "%s must point to a %s; %s is a %s."
                           % (p.name, " or ".join(short(d) for d in p.dests), e.value,
                              short(getattr(t, "defref", "")) if t.tag == CONTVAL else t.tag),
                           param=p.name))


def _refs_exist(model, c, out, check_foreign):
    """Untyped fallback: ECUC-internal references must at least resolve."""
    for e in c.refs or ():
        if e.tag == IREFVAL or not e.value:
            continue
        if e.value_dest == CONTVAL and model.node(e.value) is None:
            out.append(Finding("S004", "error", c.path(),
                               "%s points to %s, which does not exist." % (e.name, e.value), param=e.name,
                               fix="Retarget it with ecu_edit op 'set', or unset it with value null."))
        elif check_foreign and e.value_dest != CONTVAL and model.node(e.value) is None:
            out.append(Finding("S004", "warning", c.path(),
                               "%s points to %s, which is not in the loaded model." % (e.name, e.value),
                               param=e.name))
