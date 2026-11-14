"""Project configuration and the in-memory model of all loaded ARXML files."""
from __future__ import annotations

import copy
import difflib
import fnmatch
import glob
import hashlib
import json
import os
import re
import time

from . import ToolError
from .arxml import (CONTVAL, MODCONF, PKG, Node, detect_encoding, dispose, iter_conts, parse_number, scan)

CONFIG_NAME = "ecuharness.json"
STATE_DIR = ".ecuharness"

DEFAULT_CONFIG = {
    "name": None,
    "model": {
        "config": ["Config/**/*.arxml"],   # files the harness may edit
        "definitions": [],                 # BSWMD / parameter definitions (never edited)
        "readonly": [],                    # other ARXML to resolve references (never edited)
    },
    "driver": {"type": "none"},
    "policy": {
        "gate": "no-new-errors",           # or "no-new-findings"
        "maxOpsPerEdit": 50,
        "maxRemovalsPerChange": 25,
        "readOnlyModules": [],
        "protectedPaths": [],
        "approvalPaths": [],               # changes below these paths need a person's approval to finish
        "allowGenerate": True,
        "scriptTasks": {},
        # values of these parameters are never shown to the AI host
        "maskedParams": ["*Password*", "*Passphrase*", "*Secret*", "*KeyElementInitValue*"],
    },
    "checks": {"foreignRefs": False},
    "tools": {"off": []},                  # names of tools this project does not need
    "recipes": ["ecu-recipes"],
}


def masked(policy, name):
    return any(fnmatch.fnmatchcase(name, pat) for pat in policy.get("maskedParams") or ())


_MODDEF_NAME = re.compile(br"<(?:\w+:)?ECUC-MODULE-DEF\b[^>]*>\s*<(?:\w+:)?SHORT-NAME>\s*([^<\s]+)")
_SCHEMA = re.compile(br"AUTOSAR_([0-9A-Za-z\-]+)\.xsd")
_IMPL = re.compile(br"<(?:\w+:)?BSW-IMPLEMENTATION\b.*?</(?:\w+:)?BSW-IMPLEMENTATION>", re.S)


def schema_of(data):
    """The AUTOSAR schema file an ARXML file names in its first lines, e.g. AUTOSAR_00048.xsd."""
    m = _SCHEMA.search(data[:4000])
    return "AUTOSAR_%s.xsd" % m.group(1).decode("ascii") if m else None


def impls_of(data):
    """Version facts of the BSW implementations described in a definition file."""
    out = []
    for m in _IMPL.finditer(data):
        blk = m.group(0)
        d = {}
        for key, tag in (("name", b"SHORT-NAME"), ("sw_version", b"SW-VERSION"),
                         ("autosar_release", b"AR-RELEASE-VERSION"), ("vendor_id", b"VENDOR-ID")):
            mm = re.search(br"<(?:\w+:)?" + tag + br">\s*([^<]*?)\s*</", blk)
            if mm and mm.group(1):
                d[key] = mm.group(1).decode("utf-8", "replace")
        if d:
            out.append(d)
    return out[:8]


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class SrcFile:
    """One loaded ARXML file with the layout facts needed to edit it."""

    def __init__(self, path, rel, role):
        self.path = path
        self.rel = rel
        self.role = role
        self.data = b""
        self.sig = None
        self.sha = ""
        self.enc = "utf-8"
        self.nl = "\n"
        self.indent = "  "
        self.scan = None
        self.attached = []
        self.error = None
        self.merge_problems = []
        self.defnames = None      # definition files: module definitions inside (None = unknown)
        self.loaded = False       # definition files are only parsed when a module needs them
        self.schema = None        # AUTOSAR schema file named in the header
        self.impls = []           # definition files: BSW implementation versions

    def set_data(self, data, scanned=None):
        old = self.scan
        self.scan = None
        if old is not None:
            dispose(old.root)
        self.data = data
        self.sha = sha256(data)
        self.schema = schema_of(data)
        self.enc = detect_encoding(data)
        self.nl = "\r\n" if b"\r\n" in data[:20000] else "\n"
        m = re.search(br"\n([ \t]+)<", data[:20000])
        self.indent = m.group(1).decode("ascii") if m else "  "
        self.scan = scanned if scanned is not None else scan(data, self)


class Model:
    """Merged view over all loaded files. Rebuilt incrementally on refresh()."""

    def __init__(self, project):
        self.project = project
        self.files = {}
        self.root = Node("", "#root")
        self.root.kids = {}
        self.defs = {}
        self.gen = 0
        self.problems = []
        self._refs = None
        self._modules = None

    # ---- loading ---------------------------------------------------------
    def _wanted(self):
        root = self.project.root
        want = {}
        mcfg = self.project.cfg["model"]
        for role in ("readonly", "definitions", "config"):
            for pat in mcfg.get(role) or []:
                full = pat if os.path.isabs(pat) else os.path.join(root, pat)
                for f in sorted(glob.glob(full, recursive=True)):
                    if os.path.isfile(f):
                        want[os.path.normcase(os.path.abspath(f))] = (os.path.abspath(f), role)
        return want

    def refresh(self):
        """Load new/changed files, drop deleted ones. Returns changed rel paths."""
        want = self._wanted()
        changed = []
        for key in list(self.files):
            if key not in want:
                gone = self.files.pop(key)
                self._detach(gone)
                if gone.scan is not None:
                    dispose(gone.scan.root)
                changed.append(key)
        definitions = []
        for key, (path, role) in want.items():
            if role == "definitions":
                definitions.append((key, path))
                continue
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
            sf = self.files.get(key)
            if sf is not None and sf.sig == sig and sf.role == role:
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            if sf is None:
                sf = SrcFile(path, self.project.rel(path), role)
                self.files[key] = sf
            else:
                self._detach(sf)
            sf.role = role
            sf.sig = sig
            try:
                sf.set_data(data)
                sf.error = None
            except Exception as exc:  # not well-formed: keep it visible, not loaded
                sf.scan = None
                sf.data = data
                sf.error = str(exc)
                changed.append(sf.rel)
                continue
            self._attach(sf)
            changed.append(sf.rel)
        if changed:
            self._modules = None
        need = {m.defref.rsplit("/", 1)[-1] for m in self.modules() if m.defref}
        for key, path in definitions:
            self._refresh_definitions(key, path, need, changed)
        if changed:
            self._bump()
        return changed

    def _refresh_definitions(self, key, path, need, changed):
        """A BSW package holds definitions for every module it ships. Only the
        files that define a module this project configures are parsed."""
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
        sf = self.files.get(key)
        data = None
        if sf is None or sf.sig != sig or sf.role != "definitions":
            with open(path, "rb") as fh:
                data = fh.read()
            if sf is None:
                sf = SrcFile(path, self.project.rel(path), "definitions")
                self.files[key] = sf
            else:
                self._detach(sf)
                if sf.scan is not None:
                    dispose(sf.scan.root)
                    sf.scan = None
                    changed.append(sf.rel)
            sf.role, sf.sig, sf.loaded, sf.error = "definitions", sig, False, None
            names = {n.decode("ascii", "replace") for n in _MODDEF_NAME.findall(data)}
            sf.defnames = names if (names or b"ECUC-MODULE-DEF" not in data) else None
        if sf.loaded or sf.error or not (sf.defnames is None or sf.defnames & need):
            return
        if data is None:
            with open(path, "rb") as fh:
                data = fh.read()
        try:
            sf.set_data(data)
            sf.loaded = True
            sf.impls = impls_of(data)
            self._attach(sf)
        except Exception as exc:
            sf.scan = None
            sf.error = str(exc)
        sf.data = b""               # definitions are never edited; no need to keep the bytes
        changed.append(sf.rel)

    def adopt(self, sf, data, scanned=None):
        """Replace a file's content after the harness wrote it."""
        self._detach(sf)
        sf.set_data(data, scanned)
        st = os.stat(sf.path)
        sf.sig = (st.st_mtime_ns, st.st_size)
        self._attach(sf)
        self._bump()

    def _bump(self):
        self.gen += 1
        self._refs = None
        self._modules = None
        self.defs = {}
        self.problems = []
        for sf in self.files.values():
            if sf.scan is None:
                if sf.error:
                    self.problems.append("%s: cannot parse (%s)" % (sf.rel, sf.error))
                continue
            for p in list(sf.scan.problems) + sf.merge_problems:
                self.problems.append("%s: %s" % (sf.rel, p))
            for md in sf.scan.moddefs:
                for d in md.all_defs():
                    self.defs[d.path] = d

    def _attach(self, sf):
        sf.attached = []
        sf.merge_problems = []
        self._merge(self.root, sf.scan.root, sf)

    def _merge(self, gparent, fnode, sf):
        for name, kid in list((fnode.kids or {}).items()):
            if kid.tag == PKG:
                g = gparent.kids.get(name) if gparent.kids else None
                if g is None:
                    g = Node(name, PKG)
                    g.kids = {}
                    gparent.add(g)
                elif g.tag != PKG:
                    sf.merge_problems.append("package %s collides with an element" % name)
                    continue
                self._merge(g, kid, sf)
            else:
                if gparent.kids and name in gparent.kids:
                    sf.merge_problems.append("duplicate element %s/%s (this copy is ignored)"
                                             % (gparent.path().rstrip("/"), name))
                    continue
                gparent.add(kid)
                sf.attached.append(kid)

    def _detach(self, sf):
        for kid in sf.attached:
            p = kid.parent
            if p is not None and p.kids and p.kids.get(kid.name) is kid:
                del p.kids[kid.name]
                while p is not None and p.parent is not None and p.tag == PKG and not p.kids:
                    del p.parent.kids[p.name]
                    p = p.parent
        sf.attached = []

    # ---- queries ---------------------------------------------------------
    def modules(self):
        if self._modules is None:
            self._modules = [n for n in _walk_top(self.root) if n.tag == MODCONF]
        return self._modules

    def top_elements(self):
        """Every element that sits directly in a package, in any file."""
        return _walk_top(self.root)

    def module_named(self, name):
        hits = [m for m in self.modules() if m.name == name]
        if not hits:
            hits = [m for m in self.modules() if m.name.lower() == name.lower()]
        return hits

    def node(self, path):
        n = self.root
        for seg in path.strip("/").split("/"):
            if not seg:
                continue
            n = n.kids.get(seg) if n.kids else None
            if n is None:
                return None
        return n

    def normalize(self, text):
        """Accept '/Abs/Path' and module-relative 'Com/ComConfig/X'."""
        p = (text or "").strip().replace("\\", "/")
        if not p:
            raise ToolError("Empty path.", "Give an AUTOSAR path such as /ActiveEcuC/Com/ComConfig.")
        if not p.startswith("/"):
            first = p.split("/", 1)[0]
            hits = self.module_named(first)
            if len(hits) == 1:
                p = hits[0].path() + p[len(first):]
            else:
                p = "/" + p
        return p.rstrip("/") or "/"

    def resolve(self, text):
        p = self.normalize(text)
        n = self.node(p)
        if n is not None:
            return n
        # case-insensitive, only when unambiguous
        cur = self.root
        segs = [s for s in p.split("/") if s]
        for i, seg in enumerate(segs):
            kids = cur.kids or {}
            nxt = kids.get(seg)
            if nxt is None:
                ci = [k for k in kids if k.lower() == seg.lower()]
                if len(ci) == 1:
                    nxt = kids[ci[0]]
            if nxt is None:
                raise ToolError("No element at %s." % p, self.suggest(cur, seg))
            cur = nxt
        return cur

    def suggest(self, parent, seg, names=None):
        names = names if names is not None else list((parent.kids or {}).keys())
        base = parent.path().rstrip("/") if hasattr(parent, "path") else ""
        close = difflib.get_close_matches(seg, names[:5000], n=5, cutoff=0.5)
        if not close:
            low = seg.lower()
            close = [n for n in names if low in n.lower()][:5]
        where = base or "/"
        if close:
            return "Deepest existing element: %s. Did you mean: %s? Use ecu_find to search by name." % (
                where, ", ".join(base + "/" + c for c in close))
        some = ", ".join(names[:8])
        return "Deepest existing element: %s (children: %s%s). Use ecu_find to search by name." % (
            where, some or "none", ", ..." if len(names) > 8 else "")

    def refs_to(self):
        """target path -> [(container, entry)] for every reference value."""
        if self._refs is None:
            idx = {}
            for m in self.modules():
                for c in iter_conts(m):
                    for e in c.refs or ():
                        if e.value:
                            idx.setdefault(e.value, []).append((c, e))
            self._refs = idx
        return self._refs

    def cdef(self, cont):
        return self.defs.get(cont.defref) if cont.defref else None

    def dest_ok(self, target_def, dest):
        """Is a container with definition ``target_def`` a valid target for a
        reference whose definition names ``dest``?  Vendor module definitions
        refine the standard ones, so only the module-relative part must match."""
        if target_def == dest:
            return True
        t = target_def.strip("/").split("/")
        d = dest.strip("/").split("/")
        tdef = self.defs.get(target_def)
        if tdef is not None:
            rel = t[len(tdef.module().path.strip("/").split("/")) - 1:]
            return d[-len(rel):] == rel
        return len(t) >= 2 and t[-2:] == d[-2:]

    def count_containers(self, node):
        return sum(1 for _ in iter_conts(node))


def _walk_top(root):
    """Elements directly inside packages (any package depth)."""
    stack = [root]
    while stack:
        n = stack.pop()
        for kid in (n.kids or {}).values():
            if kid.tag == PKG:
                stack.append(kid)
            else:
                yield kid


def typed_value(entry, pdef=None):
    """Entry value as a JSON-friendly Python value."""
    v = entry.value
    if v is None:
        return None
    if entry.is_ref:
        return v
    kind = pdef.kind if pdef is not None else None
    if kind == "boolean" or (kind is None and v.lower() in ("true", "false")):
        return v.lower() in ("true", "1")
    if kind in ("integer", "float") or (kind is None and entry.tag.startswith("ECUC-NUMERICAL")):
        n = parse_number(v)
        return v if n is None else n
    return v


class Project:
    """A project root with its config, model, driver and state directory."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        path = os.path.join(self.root, CONFIG_NAME)
        if not os.path.isfile(path):
            raise ToolError(
                "No %s in %s." % (CONFIG_NAME, self.root),
                "Run `ecuh init` in the project folder, or pass --project <folder>.")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as exc:
            raise ToolError("%s is not valid JSON: %s" % (CONFIG_NAME, exc))
        self.cfg = _merge(DEFAULT_CONFIG, raw)
        self.name = self.cfg.get("name") or os.path.basename(self.root)
        self.state = os.path.join(self.root, STATE_DIR)
        self.model = Model(self)
        self._driver = None

    def rel(self, path):
        try:
            r = os.path.relpath(path, self.root)
        except ValueError:
            return path.replace("\\", "/")
        return path.replace("\\", "/") if r.startswith("..") else r.replace("\\", "/")

    @property
    def policy(self):
        return self.cfg["policy"]

    @property
    def driver(self):
        if self._driver is None:
            from .drivers import make_driver
            self._driver = make_driver(self)
        return self._driver

    def driver_info(self):
        if getattr(self, "_drv_info", None) is None:
            self._drv_info = self.driver.info()
        return self._drv_info

    def audit(self, record):
        d = os.path.join(self.state, "log")
        os.makedirs(d, exist_ok=True)
        record = dict(record, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(os.path.join(d, time.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
