"""Atomic, verified edits of ECUC values.

An edit is a list of small operations (set, add, clone, rename, remove). They
are applied to a logical *draft* of the touched containers first. Only if every
operation is valid are byte-exact text edits derived from the draft; the result
is re-parsed and compared with the draft before anything is written. Bytes the
edit does not concern stay exactly as they were.
"""
from __future__ import annotations

import difflib
import fnmatch
import re
import uuid
import xml.parsers.expat as expat

from . import InternalError, ToolError
from .arxml import (CONTVAL, IREFVAL, KIND_TAGS, MODCONF, NUMERIC_KINDS, NUMVAL,
                    REFVAL, TEXTVAL, Cont, iter_conts, mult_text, parse_number, scan, tag_end)

SHORT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
OPS = ("set", "append", "unset", "add", "clone", "rename", "remove")
_WS = b" \t"


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short(defref):
    return defref.rsplit("/", 1)[-1] if defref else "?"


def line_indent(data, pos):
    """Whitespace before ``pos`` when the element starts its line, else None."""
    i = pos
    while i > 0 and data[i - 1] in _WS:
        i -= 1
    if i == 0 or data[i - 1:i] == b"\n":
        return data[i:pos].decode("ascii")
    return None


def nl_before(data, pos):
    """Start of the line break (plus indentation) that precedes ``pos``."""
    i = pos
    while i > 0 and data[i - 1] in _WS:
        i -= 1
    if i > 0 and data[i - 1:i] == b"\n":
        i -= 1
        if i > 0 and data[i - 1:i] == b"\r":
            i -= 1
        return i
    return pos


def render(block, indent, sf, bare_first=False):
    """Block = [(level, text)]. indent None means: keep everything on one line."""
    if indent is None:
        return "".join(t for _, t in block)
    lines = [indent + sf.indent * lvl + t for lvl, t in block]
    if bare_first:
        lines[0] = block[0][1]
    return sf.nl.join(lines)


def shift(block, by):
    return [(lvl + by, t) for lvl, t in block]


def pdef_brief(p):
    if p.is_ref:
        to = " or ".join(short(d) for d in (p.dests or [])) or (p.dest_type or "element")
        return "%s (%s to %s)" % (p.name, p.kind, to)
    if p.kind == "enum":
        lits = p.literals or []
        return "%s (enum: %s%s)" % (p.name, " | ".join(lits[:12]), " | ..." if len(lits) > 12 else "")
    if p.kind in ("integer", "float") and (p.min is not None or p.max is not None):
        return "%s (%s %s..%s)" % (p.name, p.kind, _n(p.min), _n(p.max))
    return "%s (%s)" % (p.name, p.kind)


def _n(v):
    return "" if v is None else str(v)


def proj_cont(c):
    """Logical content of a parsed container, for verification and diffs."""
    return (c.name, c.defref,
            [(e.defref, e.value or "") for e in c.params or ()],
            [(e.defref, e.value or "") for e in c.refs or ()],
            list(c.kids) if c.kids else [])


class EDraft:
    __slots__ = ("orig", "tag", "defref", "def_dest", "value", "value_dest",
                 "target", "deleted", "touched")

    def __init__(self, orig=None):
        self.orig = orig
        self.tag = orig.tag if orig else None
        self.defref = orig.defref if orig else ""
        self.def_dest = orig.def_dest if orig else None
        self.value = orig.value if orig else None
        self.value_dest = orig.value_dest if orig else None
        self.target = None
        self.deleted = False
        self.touched = False

    @property
    def name(self):
        return self.defref.rsplit("/", 1)[-1]

    @property
    def is_ref(self):
        return self.tag == REFVAL or self.tag == IREFVAL

    def copy(self):
        e = EDraft()
        e.tag, e.defref, e.def_dest = self.tag, self.defref, self.def_dest
        e.value, e.value_dest, e.target = self.value, self.value_dest, self.target
        return e


class CDraft:
    __slots__ = ("orig", "name", "tag", "defref", "def_dest", "params", "refs",
                 "added", "removed", "renamed", "parent", "deleted", "cdef")

    def __init__(self, orig, name, defref, def_dest, parent):
        self.orig = orig
        self.name = name
        self.tag = orig.tag if orig is not None else CONTVAL
        self.defref = defref
        self.def_dest = def_dest
        self.params = [EDraft(e) for e in (orig.params or ())] if orig is not None else []
        self.refs = [EDraft(e) for e in (orig.refs or ())] if orig is not None else []
        self.added = []
        self.removed = {}
        self.renamed = {}
        self.parent = parent
        self.deleted = False
        self.cdef = None

    def find(self, entry):
        for e in self.params:
            if e.orig is entry:
                return e
        for e in self.refs:
            if e.orig is entry:
                return e
        return None

    def has(self, name):
        return any(not e.deleted and e.name == name for e in self.params + self.refs)


class Plan:
    def __init__(self):
        self.changes = []
        self.files = {}     # SrcFile -> new bytes
        self.scans = {}     # SrcFile -> FileScan of the new bytes
        self.removed = 0
        self.notes = []


class Batch:
    def __init__(self, model, policy, removed_before=0):
        self.model = model
        self.policy = policy
        self.drafts = {}      # id(existing Cont) -> CDraft
        self.new = []         # every CDraft created in this batch
        self.removed = 0
        self.removed_before = removed_before
        self.notes = []

    # ---- view over model + pending drafts --------------------------------
    def view(self, x):
        if x is None or isinstance(x, CDraft):
            return x
        return self.drafts.get(id(x), x)

    def draft(self, c):
        d = self.drafts.get(id(c))
        if d is None:
            d = CDraft(c, c.name, c.defref, c.def_dest, c.parent)
            self.drafts[id(c)] = d
        return d

    def as_draft(self, v):
        v = self.view(v)
        return v if isinstance(v, CDraft) else self.draft(v)

    def is_cont(self, v):
        return isinstance(v, (CDraft, Cont))

    def child(self, v, name):
        v = self.view(v)
        if isinstance(v, CDraft):
            for a in v.added:
                if a.name == name and not a.deleted:
                    return a
            if v.orig is None:
                return None
            k = v.renamed.get(name)
            if k is not None:
                return k
            kid = v.orig.kids.get(name) if v.orig.kids else None
            if kid is None:
                return None
            d = self.drafts.get(id(kid))
            if d is None:
                return kid
            return None if (d.deleted or d.name != name) else d
        kid = v.kids.get(name) if v.kids else None
        return self.view(kid)

    def children(self, v):
        v = self.view(v)
        out = []
        if isinstance(v, CDraft):
            if v.orig is not None and v.orig.kids:
                for kid in v.orig.kids.values():
                    d = self.drafts.get(id(kid))
                    if d is None:
                        out.append((kid.name, kid))
                    elif not d.deleted:
                        out.append((d.name, d))
            out.extend((a.name, a) for a in v.added if not a.deleted)
        elif v.kids:
            out.extend((self.view(k).name, self.view(k)) for k in v.kids.values())
        return out

    def parent_of(self, v):
        return self.view(self.view(v).parent)

    def vpath(self, v):
        parts = []
        v = self.view(v)
        while v is not None and v.parent is not None:
            parts.append(v.name)
            v = self.view(v.parent)
        return "/" + "/".join(reversed(parts))

    def gone(self, v):
        v = self.view(v)
        while v is not None:
            if isinstance(v, CDraft) and v.deleted:
                return True
            v = self.view(v.parent)
        return False

    def cdef_of(self, v):
        v = self.view(v)
        if isinstance(v, CDraft) and v.cdef is not None:
            return v.cdef
        return self.model.defs.get(v.defref) if getattr(v, "defref", None) else None

    def file_of(self, v):
        v = self.view(v)
        while v is not None:
            if isinstance(v, CDraft):
                if v.orig is not None:
                    return v.orig.file
            elif v.file is not None:
                return v.file
            v = self.view(v.parent)
        return None

    def module_of(self, v):
        v = self.view(v)
        while v is not None and v.tag != MODCONF:
            v = self.view(v.parent)
        return v

    def resolve(self, text):
        p = self.model.normalize(text)
        cur = self.model.root
        for seg in [s for s in p.split("/") if s]:
            nxt = self.child(cur, seg)
            if nxt is None:
                names = [n for n, _ in self.children(cur)]
                ci = [n for n in names if n.lower() == seg.lower()]
                if len(ci) == 1:
                    nxt = self.child(cur, ci[0])
                if nxt is None:
                    base = self.vpath(cur).rstrip("/")
                    close = difflib.get_close_matches(seg, names[:5000], n=5, cutoff=0.5)
                    hint = "Deepest existing element: %s." % (base or "/")
                    if close:
                        hint += " Did you mean: %s?" % ", ".join(base + "/" + c for c in close)
                    raise ToolError("No element at %s." % p, hint + " Use ecu_find to search by name.")
            cur = nxt
        return cur

    def container(self, text, what, allow_module=False):
        v = self.resolve(text)
        if not self.is_cont(v):
            raise ToolError("%s %s is a %s, not an ECUC container." % (what, self.vpath(v), v.tag),
                            "Only ECUC containers can be edited. Use ecu_get to inspect the element.")
        if v.tag == MODCONF and not allow_module:
            raise ToolError("%s is a module configuration; this operation needs a container inside it."
                            % self.vpath(v))
        if self.gone(v):
            raise ToolError("%s was removed earlier in this edit." % self.vpath(v))
        return v

    def guard(self, v):
        f = self.file_of(v)
        path = self.vpath(v)
        if f is None or f.role != "config":
            raise ToolError(
                "%s is read-only: its file %s is loaded as '%s'." % (path, f.rel if f else "?", f.role if f else "?"),
                "Only files matched by model.config in ecuharness.json can be edited. Do not work around this.")
        if f.enc.lower().replace("_", "-") not in ("utf-8", "utf8", "us-ascii", "ascii"):
            raise ToolError("%s is stored as %s; only UTF-8 files can be edited safely." % (f.rel, f.enc),
                            "Tell the user. The file can still be read.")
        m = self.module_of(v)
        if m is not None and m.name in (self.policy.get("readOnlyModules") or []):
            raise ToolError("Module %s is read-only by project policy." % m.name,
                            "Stop and ask the user; policy.readOnlyModules in ecuharness.json controls this.")
        for pat in self.policy.get("protectedPaths") or []:
            if fnmatch.fnmatchcase(path, pat):
                raise ToolError("%s is protected by project policy (%s)." % (path, pat),
                                "Stop and ask the user; policy.protectedPaths in ecuharness.json controls this.")

    # ---- operations ------------------------------------------------------
    def apply(self, op):
        if not isinstance(op, dict):
            raise ToolError("Each op must be an object.", 'Example: {"op":"set","path":"/A/B","param":"X","value":1}')
        kind = op.get("op")
        if kind not in OPS:
            raise ToolError("Unknown op '%s'." % kind, "Valid ops: %s." % ", ".join(OPS))
        getattr(self, "_op_" + kind)(op)

    @staticmethod
    def _need(op, *keys):
        missing = [k for k in keys if op.get(k) in (None, "")]
        if missing:
            raise ToolError("Op '%s' is missing: %s." % (op.get("op"), ", ".join(missing)),
                            "Required fields for %s: %s." % (op.get("op"), ", ".join(keys)))

    def _check_name(self, name, parent):
        if not isinstance(name, str) or not SHORT_NAME_RE.match(name):
            raise ToolError("'%s' is not a valid AUTOSAR short name." % name,
                            "Use a letter followed by letters, digits or underscores (max 128 characters).")
        if self.child(parent, name) is not None:
            raise ToolError("%s/%s already exists." % (self.vpath(parent).rstrip("/"), name),
                            "Choose another name, or edit the existing element with op 'set'.")

    def _op_set(self, op):
        self._need(op, "path", "param")
        if "value" not in op:
            raise ToolError("Op 'set' is missing 'value'.",
                            "Give the new value as text. To clear a parameter use op 'unset'.")
        v = self.container(op["path"], "Target")
        self.guard(v)
        d = self.as_draft(v)
        value = op["value"]
        if value is not None and not isinstance(value, (list, tuple)):
            _, name, existing = self._spec(d, op["param"])
            if len(existing) > 1:
                raise ToolError("%s holds %d values; 'set' would replace them all." % (name, len(existing)),
                                "Use op 'append' to add one value, or op 'unset' with 'value' to remove one.")
        self._assign(d, op["param"], value)

    def _op_append(self, op):
        self._need(op, "path", "param", "value")
        v = self.container(op["path"], "Target")
        self.guard(v)
        d = self.as_draft(v)
        pdef, name, existing = self._spec(d, op["param"])
        if pdef is not None and pdef.upper is not None and len(existing) >= pdef.upper:
            raise ToolError("%s already holds %d value(s), its maximum." % (name, len(existing)),
                            "Use op 'set' to replace the value." if pdef.upper == 1
                            else "Remove one first with op 'unset' and 'value'.")
        sample = existing[0] if existing else None
        new = self._coerce(d, name, pdef, sample, op["value"])
        final = self.vpath(new[1]) if new[1] is not None else new[0]
        if any(self._final(e) == final for e in existing):
            raise ToolError("%s already contains %s." % (name, final), "Nothing to do for this op; drop it.")
        self._new_entry(d, pdef, sample, new)

    def _op_unset(self, op):
        self._need(op, "path", "param")
        v = self.container(op["path"], "Target")
        self.guard(v)
        d = self.as_draft(v)
        _, name, existing = self._spec(d, op["param"])
        want = op.get("value")
        if want in (None, ""):
            for e in existing:
                e.deleted = True
            return
        text = str(want).strip()
        alt = text
        if existing and existing[0].is_ref:
            alt = self.model.normalize(text)
        num = parse_number(text)
        hits = [e for e in existing if self._final(e) in (text, alt)
                or (num is not None and parse_number(self._final(e)) == num)]
        if not hits:
            raise ToolError("%s has no value %s." % (name, text),
                            "Current values: %s." % (", ".join(self._final(e) for e in existing) or "none"))
        for e in hits:
            e.deleted = True

    @staticmethod
    def _pairs(op):
        """'values' as {param: value | [values]}; accepts a list of {param, value}."""
        raw = op.get("values")
        if raw in (None, "", [], {}):
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        if not isinstance(raw, list):
            raise ToolError("'values' must be a list of {param, value} objects.",
                            'Example: "values":[{"param":"ComBitSize","value":"8"}]')
        out = {}
        for it in raw:
            key = (it.get("param") or it.get("name")) if isinstance(it, dict) else None
            if not key or "value" not in it:
                raise ToolError("Each entry of 'values' needs 'param' and 'value'.",
                                'Example: "values":[{"param":"ComBitSize","value":"8"}]')
            if key in out:
                out[key] = (out[key] if isinstance(out[key], list) else [out[key]]) + [it["value"]]
            else:
                out[key] = it["value"]
        return out

    def _op_add(self, op):
        if op.get("kind") and not op.get("definition"):
            op = dict(op, definition=op["kind"])
        if not op.get("definition"):
            raise ToolError("Op 'add' is missing: kind.", "Give the kind of the new container, e.g. ComSignal.")
        self._need(op, "parent", "name")
        pv = self.container(op["parent"], "Parent", allow_module=True)
        self.guard(pv)
        pd = self.as_draft(pv)
        pdef = self.cdef_of(pd)
        if pdef is None:
            raise ToolError(
                "No definition is loaded for %s, so new containers cannot be type-checked." % self.vpath(pd),
                "Use op 'clone' on an existing sibling instead, or add the module's definition file "
                "under model.definitions in ecuharness.json.")
        want = op["definition"]
        cd = pdef.subs.get(short(want))
        if cd is None:
            ci = [n for n in pdef.subs if n.lower() == short(want).lower()]
            cd = pdef.subs[ci[0]] if len(ci) == 1 else None
        if cd is None:
            raise ToolError("%s cannot contain a '%s'." % (pdef.name, want),
                            "Allowed here: %s." % (", ".join("%s (%s)" % (s.name, mult_text(s))
                                                              for s in pdef.subs.values()) or "nothing"))
        self._check_name(op["name"], pd)
        sibs = self.children(pd)
        same = sum(1 for _, k in sibs if k.defref == cd.path)
        if cd.upper is not None and same >= cd.upper:
            raise ToolError("%s allows at most %d %s; it already has %d."
                            % (self.vpath(pd), cd.upper, cd.name, same))
        if pdef.choice and sibs:
            raise ToolError("%s is a choice container and already holds %s." % (self.vpath(pd), sibs[0][0]),
                            "Remove the existing choice first.")
        nd = CDraft(None, op["name"], cd.path,
                    "ECUC-CHOICE-CONTAINER-DEF" if cd.choice else "ECUC-PARAM-CONF-CONTAINER-DEF", pd)
        nd.cdef = cd
        pd.added.append(nd)
        self.new.append(nd)
        values = self._pairs(op)
        problems = []
        for k, val in values.items():
            try:
                self._assign(nd, k, val)
            except ToolError as exc:
                problems.append(exc.message + (" " + exc.hint if exc.hint else ""))
        missing = []
        for p in list(cd.params.values()) + list(cd.refs.values()):
            if p.lower < 1 or nd.has(p.name) or p.name in values:
                continue
            if p.default is not None and not p.is_ref:
                try:
                    self._assign(nd, p.name, p.default)
                    continue
                except ToolError:
                    pass
            missing.append(pdef_brief(p))
        if missing:
            problems.append("Missing required values: %s." % "; ".join(missing))
        if problems:
            raise ToolError("Cannot add %s '%s': %s" % (cd.name, op["name"], " ".join(problems)),
                            "Fix 'values' and send the whole edit again. ecu_describe shows every parameter of %s."
                            % cd.name)

    def _op_clone(self, op):
        self._need(op, "path", "name")
        sv = self.container(op["path"], "Source")
        pv = self.parent_of(sv)
        self.guard(pv)
        pd = self.as_draft(pv)
        self._check_name(op["name"], pd)
        pdef = self.cdef_of(pd)
        cd = pdef.subs.get(short(sv.defref)) if pdef is not None else None
        if cd is not None and cd.upper is not None:
            same = sum(1 for _, k in self.children(pd) if k.defref == sv.defref)
            if same >= cd.upper:
                raise ToolError("%s allows at most %d %s; it already has %d."
                                % (self.vpath(pd), cd.upper, cd.name, same))
        old_paths = {self.vpath(sv)}
        svv = self.view(sv)
        if isinstance(svv, CDraft) and svv.orig is not None:
            old_paths.add(svv.orig.path())
        mapping = {}
        nd = self._copy(sv, op["name"], pd, mapping)
        pd.added.append(nd)
        for d in self._subtree(nd):
            for e in d.refs:
                if e.tag == IREFVAL:
                    raise ToolError("%s contains instance references, which cannot be cloned in this version."
                                    % self.vpath(sv))
                if e.target is not None and id(self.view(e.target)) in mapping:
                    e.target = mapping[id(self.view(e.target))]
                    continue
                for old in old_paths:
                    if e.target is None and e.value and (e.value == old or e.value.startswith(old + "/")):
                        t = nd
                        for seg in [s for s in e.value[len(old):].split("/") if s]:
                            t = self.child(t, seg) if t is not None else None
                        if t is not None:
                            e.target = t
                        break
        for k, val in self._pairs(op).items():
            self._assign(nd, k, val)

    def _copy(self, v, name, parent, mapping):
        v = self.view(v)
        nd = CDraft(None, name, v.defref, v.def_dest, parent)
        nd.cdef = self.cdef_of(v)
        mapping[id(v)] = nd
        self.new.append(nd)
        src = v if isinstance(v, CDraft) else CDraft(v, v.name, v.defref, v.def_dest, v.parent)
        nd.params = [e.copy() for e in src.params if not e.deleted]
        nd.refs = [e.copy() for e in src.refs if not e.deleted]
        for kname, kid in self.children(v):
            nd.added.append(self._copy(kid, kname, nd, mapping))
        return nd

    def _subtree(self, d):
        yield d
        for a in d.added:
            if not a.deleted:
                for x in self._subtree(a):
                    yield x

    def _op_rename(self, op):
        self._need(op, "path", "name")
        v = self.container(op["path"], "Target")
        self.guard(v)
        pv = self.parent_of(v)
        if op["name"] == self.view(v).name:
            return
        self._check_name(op["name"], pv)
        d = self.view(v)
        if isinstance(d, CDraft) and d.orig is None:
            d.name = op["name"]
            return
        d = self.as_draft(v)
        pd = self.as_draft(pv)
        for k, x in list(pd.renamed.items()):
            if x is d:
                del pd.renamed[k]
        d.name = op["name"]
        if d.name != d.orig.name:
            pd.renamed[d.name] = d

    def _op_remove(self, op):
        self._need(op, "path")
        v = self.container(op["path"], "Target")
        self.guard(v)
        d = self.view(v)
        if isinstance(d, CDraft) and d.orig is None:
            d.deleted = True
            return
        d = self.as_draft(v)
        pd = self.as_draft(self.parent_of(v))
        d.deleted = True
        pd.removed[id(d.orig)] = d.orig
        self.removed += 1 + sum(1 for _ in iter_conts(d.orig))
        limit = self.policy.get("maxRemovalsPerChange")
        if limit is not None and self.removed + self.removed_before > limit:
            raise ToolError(
                "This change would remove %d containers; the project limit is %d."
                % (self.removed + self.removed_before, limit),
                "Mass removal needs the user's decision. Stop and ask; policy.maxRemovalsPerChange controls the limit.")

    # ---- values ----------------------------------------------------------
    def _spec(self, d, name):
        """-> (definition | None, canonical name, live entries with that name)"""
        cdef = self.cdef_of(d)
        pdef = None
        if cdef is not None:
            pdef = cdef.params.get(name) or cdef.refs.get(name)
            if pdef is None:
                ci = [n for n in list(cdef.params) + list(cdef.refs) if n.lower() == str(name).lower()]
                if len(ci) == 1:
                    pdef = cdef.params.get(ci[0]) or cdef.refs.get(ci[0])
                    name = pdef.name
        existing = [e for e in d.params + d.refs if not e.deleted and e.name == name]
        if pdef is None and not existing:
            if cdef is not None:
                known = list(cdef.params) + list(cdef.refs)
                close = difflib.get_close_matches(str(name), known, n=4, cutoff=0.4)
                raise ToolError("%s has no parameter or reference named '%s'." % (cdef.name, name),
                                "%sValid names: %s." % ("Did you mean %s? " % ", ".join(close) if close else "",
                                                        ", ".join(known[:40])))
            have = sorted({e.name for e in d.params + d.refs if not e.deleted})
            raise ToolError(
                "Cannot add '%s' to %s: no definition is loaded for it, so only existing values can be changed."
                % (name, self.vpath(d)),
                "Existing values: %s. Add the definition file under model.definitions to enable new values."
                % (", ".join(have) or "none"))
        sample = existing[0] if existing else None
        is_ref = pdef.is_ref if pdef is not None else sample.is_ref
        kind = pdef.kind if pdef is not None else None
        if kind in ("instance-reference", "add-info", "uri-reference") or (sample and sample.tag == IREFVAL):
            raise ToolError("'%s' is a %s; this version can read it but not write it."
                            % (name, kind or "instance reference"),
                            "Tell the user this value needs the vendor tool.")
        return pdef, name, existing

    def _new_entry(self, d, pdef, sample, coerced):
        e = EDraft()
        if pdef is not None:
            e.defref = pdef.path
            e.def_dest = KIND_TAGS[pdef.kind]
            e.tag = REFVAL if pdef.is_ref else (NUMVAL if pdef.kind in NUMERIC_KINDS else TEXTVAL)
        else:
            e.defref, e.def_dest, e.tag = sample.defref, sample.def_dest, sample.tag
        e.value, e.target, e.value_dest = coerced
        e.touched = True
        (d.refs if e.is_ref else d.params).append(e)

    def _assign(self, d, name, value):
        pdef, name, existing = self._spec(d, name)
        sample = existing[0] if existing else None
        values = [] if value is None else (list(value) if isinstance(value, (list, tuple)) else [value])
        if pdef is not None and pdef.upper is not None and len(values) > pdef.upper:
            raise ToolError("'%s' takes at most %d value(s), got %d." % (name, pdef.upper, len(values)))
        new = [self._coerce(d, name, pdef, sample, x) for x in values]
        for i, e in enumerate(existing):
            if i < len(new):
                e.value, e.target, vdest = new[i]
                if vdest:
                    e.value_dest = vdest
                e.touched = True
            else:
                e.deleted = True
        for coerced in new[len(existing):]:
            self._new_entry(d, pdef, sample, coerced)

    def _coerce(self, d, name, pdef, sample, x):
        """-> (text, target vnode | None, VALUE-REF DEST | None)"""
        kind = pdef.kind if pdef is not None else None
        is_ref = pdef.is_ref if pdef is not None else sample.is_ref
        if is_ref:
            if not isinstance(x, str) or not x.strip():
                raise ToolError("Reference '%s' needs the path of its target as a string." % name)
            try:
                t = self.resolve(x)
            except ToolError as exc:
                foreign = kind == "foreign-reference" or (
                    sample is not None and sample.value_dest not in (None, CONTVAL))
                if foreign and sample is not None and sample.value_dest:
                    self.notes.append("Target of '%s' is outside the loaded model; kept as text: %s" % (name, x))
                    return self.model.normalize(x), None, sample.value_dest
                raise ToolError("Reference '%s': %s" % (name, exc.message), exc.hint)
            if self.gone(t):
                raise ToolError("Reference '%s' points to %s, which this edit removes." % (name, self.vpath(t)))
            if pdef is not None and pdef.dests and kind != "foreign-reference":
                if not self.is_cont(t) or t.tag == MODCONF:
                    raise ToolError("Reference '%s' must point to an ECUC container; %s is a %s."
                                    % (name, self.vpath(t), t.tag))
                if not any(self.model.dest_ok(t.defref, dst) for dst in pdef.dests):
                    raise ToolError(
                        "Reference '%s' must point to a %s, but %s is a %s."
                        % (name, " or ".join(short(x) for x in pdef.dests), self.vpath(t), short(t.defref)),
                        self._targets_hint(pdef))
            return None, t, t.tag
        if isinstance(x, (dict, list)):
            raise ToolError("'%s' needs a single value, got %s." % (name, type(x).__name__))
        style01 = sample is not None and sample.orig is not None and sample.orig.value in ("0", "1")
        if kind is None:                      # untyped: follow the existing value's nature
            if isinstance(x, bool):
                return ("1" if x else "0") if style01 else ("true" if x else "false"), None, None
            return str(x).strip(), None, None
        if kind == "boolean":
            b = x if isinstance(x, bool) else {"true": True, "false": False, "1": True, "0": False}.get(
                str(x).strip().lower())
            if b is None:
                raise ToolError("'%s' is a boolean; got %r." % (name, x), "Use true or false.")
            return ("1" if b else "0") if style01 else ("true" if b else "false"), None, None
        if kind in ("integer", "float"):
            n = None if isinstance(x, bool) else (x if isinstance(x, (int, float)) else parse_number(str(x)))
            if n is None or (kind == "integer" and isinstance(n, float) and not n.is_integer()):
                raise ToolError("'%s' is %s; got %r." % (name, "an integer" if kind == "integer" else "a float", x))
            n = int(n) if kind == "integer" else float(n)
            if (pdef.min is not None and n < pdef.min) or (pdef.max is not None and n > pdef.max):
                raise ToolError("%s=%s is out of range; allowed: %s..%s." % (name, n, _n(pdef.min), _n(pdef.max)))
            if kind == "float" and n in (float("inf"), float("-inf")):
                return ("INF" if n > 0 else "-INF"), None, None
            return (str(n) if kind == "integer" else repr(n)), None, None
        text = str(x).strip() if not isinstance(x, bool) else ("true" if x else "false")
        if kind == "enum" and pdef.literals and text not in pdef.literals:
            ci = [lit for lit in pdef.literals if lit.lower() == text.lower()]
            if len(ci) != 1:
                raise ToolError("'%s' is not a valid value for %s." % (text, name),
                                "Allowed: %s." % " | ".join(pdef.literals[:40]))
            text = ci[0]
        return text, None, None

    def _targets_hint(self, pdef):
        found = []
        for m in self.model.modules():
            for c in iter_conts(m):
                if any(self.model.dest_ok(c.defref, dst) for dst in pdef.dests):
                    found.append(c.path())
                    if len(found) >= 5:
                        return "Valid targets include: %s." % ", ".join(found)
        if found:
            return "Valid targets: %s." % ", ".join(found)
        return "No container of that kind exists yet; add one first."

    # ---- planning --------------------------------------------------------
    def _masked(self, name):
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.policy.get("maskedParams") or ())

    def _final(self, e):
        if e.is_ref and e.target is not None:
            return self.vpath(e.target)
        return e.value or ""

    def _proj(self, d):
        return (d.name, d.defref,
                [(e.defref, self._final(e)) for e in d.params if not e.deleted],
                [(e.defref, self._final(e)) for e in d.refs if not e.deleted],
                [n for n, _ in self.children(d)])

    def plan(self):
        model = self.model
        refs_to = model.refs_to()
        # 1. references to renamed containers follow the rename
        for d in list(self.drafts.values()):
            if d.deleted or d.name == d.orig.name or self.gone(d):
                continue
            for n in [d.orig] + list(d.orig.walk()):
                for owner, entry in refs_to.get(n.path(), ()):
                    if self.gone(owner):
                        continue
                    ed = self.draft(owner).find(entry)
                    if ed is not None and not ed.deleted and not ed.touched:
                        ed.target = n
        # 2. nothing may keep pointing at a removed container
        dangling = []
        for d in list(self.drafts.values()):
            if not d.deleted or self.gone(self.parent_of(d)):
                continue
            for n in [d.orig] + list(d.orig.walk()):
                for owner, entry in refs_to.get(n.path(), ()):
                    if self.gone(owner):
                        continue
                    od = self.drafts.get(id(owner))
                    ed = od.find(entry) if od is not None else None
                    if ed is not None and (ed.deleted or ed.touched):
                        continue
                    dangling.append("%s.%s" % (self.vpath(owner), entry.name))
        live = [d for d in list(self.drafts.values()) + self.new if not self.gone(d)]
        for d in live:
            for e in d.refs:
                if not e.deleted and e.target is not None and self.gone(e.target):
                    dangling.append("%s.%s" % (self.vpath(d), e.name))
        if dangling:
            uniq = sorted(set(dangling))
            raise ToolError(
                "Cannot remove: still referenced by %s%s." % (", ".join(uniq[:8]),
                                                               " and %d more" % (len(uniq) - 8) if len(uniq) > 8 else ""),
                "In the same ecu_edit call, first retarget these references (op set) or unset them (value null).")
        # 3. text edits and human-readable change lines
        plan = Plan()
        plan.removed = self.removed
        plan.notes = self.notes
        edits = {}
        expect = []
        for d in list(self.drafts.values()):
            if d.deleted:
                parent = self.parent_of(d)
                if not self.gone(parent) and self.child(parent, d.orig.name) is None:
                    expect.append((d.orig.file, self.vpath(parent).rstrip("/") + "/" + d.orig.name, None))
                continue
            if self.gone(d):
                continue
            self._emit(d, edits, plan.changes)
            expect.append((d.orig.file, self.vpath(d), self._proj(d)))
        for d in self.new:
            if not self.gone(d):
                expect.append((self.file_of(d), self.vpath(d), self._proj(d)))
        for sf, items in edits.items():
            order = sorted(range(len(items)), key=lambda i: (items[i][0], items[i][1] > items[i][0], i))
            out, pos = [], 0
            for i in order:
                s, e, text = items[i]
                if s < pos:
                    raise InternalError("overlapping text edits in %s at byte %d" % (sf.rel, s))
                out.append(sf.data[pos:s])
                out.append(text.encode(sf.enc))
                pos = e
            out.append(sf.data[pos:])
            new = b"".join(out)
            if new != sf.data:
                plan.files[sf] = new
        # 4. prove the result before anything touches the disk
        for sf, new in plan.files.items():
            try:
                plan.scans[sf] = scan(new, sf)
            except (expat.ExpatError, ValueError) as exc:
                raise InternalError("edit of %s would not be well-formed XML: %s" % (sf.rel, exc))
        for sf, path, proj in expect:
            sc = plan.scans.get(sf)
            if sc is None:
                continue
            n = sc.node(path)
            if proj is None:
                if n is not None:
                    raise InternalError("verification failed: %s still exists after removal" % path)
            elif n is None or not isinstance(n, Cont) or proj_cont(n) != proj:
                raise InternalError("verification failed: %s does not match the intended result" % path)
        return plan

    def _emit(self, d, edits, changes):
        c = d.orig
        sf = c.file
        data = sf.data
        E = edits.setdefault(sf, [])
        path = self.vpath(d)
        ind = line_indent(data, c.s)
        kid_ind = None if ind is None else ind + sf.indent
        if d.name != c.name:
            if c.sn is None:
                raise InternalError("cannot rename %s: empty SHORT-NAME element" % c.path())
            E.append((tag_end(data, c.sn[0]), c.sn[1], esc(d.name)))
            changes.append("> %s renamed to %s" % (c.path(), d.name))
        prev_end = c.hdr_end
        for lst, W, wtag in ((d.params, c.wp, "PARAMETER-VALUES"), (d.refs, c.wr, "REFERENCE-VALUES")):
            deletes, blocks, survivors, names = [], [], 0, []
            for e in lst:
                if e.name not in names:
                    names.append(e.name)
                if e.orig is None:
                    if not e.deleted:
                        blocks.append(self._entry_block(e))
                    continue
                if e.deleted:
                    deletes.append((e.orig.s, e.orig.e))
                    continue
                survivors += 1
                final = self._final(e)
                if final != (e.orig.value or "") or (e.is_ref and e.value_dest != e.orig.value_dest):
                    if e.orig.vs is None:       # no value element yet: add one, keep everything else
                        pos = data.rfind(b"</", e.orig.s, e.orig.e)
                        if pos < 0:
                            raise InternalError("cannot add a value to %s.%s" % (path, e.name))
                        vind = line_indent(data, pos)
                        if vind is None:
                            E.append((pos, pos, self._value_elem(e)))
                        else:
                            at = nl_before(data, pos)
                            E.append((at, at, sf.nl + vind + sf.indent + self._value_elem(e)))
                    else:
                        E.append((e.orig.vs, e.orig.ve, self._value_elem(e)))
                    if e.orig.auto:
                        self.notes.append("%s of %s is marked as calculated by the tool (IS-AUTO-VALUE); "
                                          "the vendor tool may overwrite your value." % (e.name, path))
            for nm in names:
                old = [x.orig.value or "" for x in lst if x.orig is not None and x.name == nm]
                new = [self._final(x) for x in lst if not x.deleted and x.name == nm]
                if old != new:
                    if self._masked(nm):
                        old, new = ["***"] * len(old), ["***"] * len(new)
                    changes.append("~ %s %s: %s" % (path, nm, fmt_change(old, new)))
            self._wrap_update(E, sf, W, wtag, prev_end, deletes, blocks, survivors, kid_ind)
            if W is not None:
                prev_end = W[2]
        added = [a for a in d.added if not a.deleted]
        removed = list(d.removed.values())
        self._wrap_update(E, sf, c.ws, "CONTAINERS" if c.is_module else "SUB-CONTAINERS", prev_end,
                          [(k.s, k.e) for k in removed], [self._cont_block(a, sf) for a in added],
                          len(c.kids or {}) - len(removed), kid_ind, keep_empty=c.is_module)
        for a in added:
            vals = ["%s=%s" % (e.name, "***" if self._masked(e.name) else self._final(e))
                    for e in a.params + a.refs if not e.deleted]
            nested = sum(1 for _ in self._subtree(a)) - 1
            changes.append("+ %s [%s]%s%s" % (self.vpath(a), short(a.defref),
                                              " " + ", ".join(vals[:10]) if vals else "",
                                              " (+%d nested)" % nested if nested else ""))
        for k in removed:
            nested = sum(1 for _ in iter_conts(k))
            changes.append("- %s/%s [%s]%s" % (path.rstrip("/"), k.name, short(k.defref),
                                              " (+%d nested)" % nested if nested else ""))

    def _wrap_update(self, E, sf, W, wtag, anchor, deletes, blocks, survivors, kid_ind, keep_empty=False):
        data = sf.data
        if W is None or W[1] is None:           # wrapper missing, or written as <TAG/>
            if not blocks:
                return
            block = [(0, "<%s>" % wtag)]
            for b in blocks:
                block.extend(shift(b, 1))
            block.append((0, "</%s>" % wtag))
            if W is None:
                if anchor is None:
                    raise InternalError("no anchor to insert %s" % wtag)
                E.append((anchor, anchor, render(block, None, sf) if kid_ind is None
                          else sf.nl + render(block, kid_ind, sf)))
            else:
                E.append((W[0], W[2], render(block, line_indent(data, W[0]), sf, True)))
            return
        if deletes and survivors == 0 and not blocks and not keep_empty:
            E.append((nl_before(data, W[0]), W[2], ""))
            return
        for s, e in deletes:
            E.append((nl_before(data, s), e, ""))
        if blocks:
            close_ind = line_indent(data, W[1])
            if close_ind is None:
                E.append((W[1], W[1], "".join(render(b, None, sf) for b in blocks)))
            else:
                E.append((nl_before(data, W[1]), nl_before(data, W[1]),
                          "".join(sf.nl + render(b, close_ind + sf.indent, sf) for b in blocks)))

    def _value_elem(self, e):
        if e.tag == REFVAL:
            return '<VALUE-REF DEST="%s">%s</VALUE-REF>' % (e.value_dest or CONTVAL, esc(self._final(e)))
        return "<VALUE>%s</VALUE>" % esc(self._final(e))

    def _entry_block(self, e):
        return [(0, "<%s>" % e.tag),
                (1, '<DEFINITION-REF DEST="%s">%s</DEFINITION-REF>' % (e.def_dest, esc(e.defref))),
                (1, self._value_elem(e)),
                (0, "</%s>" % e.tag)]

    def _cont_block(self, a, sf):
        attr = ' UUID="%s"' % uuid.uuid4() if sf.scan.uses_uuid else ""
        b = [(0, "<%s%s>" % (CONTVAL, attr)),
             (1, "<SHORT-NAME>%s</SHORT-NAME>" % esc(a.name)),
             (1, '<DEFINITION-REF DEST="%s">%s</DEFINITION-REF>'
              % (a.def_dest or "ECUC-PARAM-CONF-CONTAINER-DEF", esc(a.defref)))]
        for wtag, lst in (("PARAMETER-VALUES", a.params), ("REFERENCE-VALUES", a.refs)):
            items = [self._entry_block(e) for e in lst if not e.deleted]
            if items:
                b.append((1, "<%s>" % wtag))
                for it in items:
                    b.extend(shift(it, 2))
                b.append((1, "</%s>" % wtag))
        kids = [k for k in a.added if not k.deleted]
        if kids:
            b.append((1, "<SUB-CONTAINERS>"))
            for k in kids:
                b.extend(shift(self._cont_block(k, sf), 2))
            b.append((1, "</SUB-CONTAINERS>"))
        b.append((0, "</%s>" % CONTVAL))
        return b


def _fmt(values):
    if not values:
        return "(unset)"
    return values[0] if len(values) == 1 else "[%s]" % ", ".join(values)


def fmt_change(old, new):
    """'a -> b' for single values; for lists only what was added or removed."""
    if len(old) <= 1 and len(new) <= 1:
        return "%s -> %s" % (_fmt(old), _fmt(new))
    added, removed = list(new), []
    for v in old:
        if v in added:
            added.remove(v)
        else:
            removed.append(v)
    parts = ["added " + v for v in added] + ["removed " + v for v in removed]
    return ", ".join(parts) if parts else "order changed"


def plan_ops(model, policy, ops, removed_before=0):
    """Validate all ops and return a verified Plan. Raises ToolError naming the
    first failing op; nothing is written here."""
    limit = policy.get("maxOpsPerEdit") or 50
    if not isinstance(ops, list) or not ops:
        raise ToolError("'ops' must be a non-empty list of operations.",
                        'Example: [{"op":"set","path":"/ActiveEcuC/Com/ComConfig/Sig","param":"ComBitSize","value":8}]')
    if len(ops) > limit:
        raise ToolError("Too many ops in one edit (%d); the limit is %d." % (len(ops), limit),
                        "Split the work into several ecu_edit calls.")
    batch = Batch(model, policy, removed_before)
    for i, op in enumerate(ops):
        try:
            batch.apply(op)
        except ToolError as exc:
            raise ToolError("op %d (%s): %s" % (i, op.get("op") if isinstance(op, dict) else "?", exc.message),
                            (exc.hint + " " if exc.hint else "") + "Nothing was written; fix this op and resend all ops.")
    return batch.plan()
