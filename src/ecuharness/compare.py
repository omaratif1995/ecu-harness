"""Compare two versions of an input file by content: what was added, removed
or changed, element by element.

Works for any ARXML (system description, ECU extract, software components,
ECUC values) and for CAN database files (DBC). A DBC file is first rewritten
as a small XML document (messages, signals and their properties), so one
comparison serves both formats.

The comparison says what changed in the *input*. For a project it can also
list the configuration containers whose names contain the name of a changed
element. That is a name match to start an impact analysis from, not a
derivation: bringing changed input into a vendor project is the job of the
vendor tool's own update.
"""
from __future__ import annotations

import bisect
import os
import re

from . import ToolError
from .arxml import iter_conts
from .edit import short as short_def
from .xtree import K_NAME, diff, kind_of, leaf_fields, parse_tree, value_text

_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\S+)")
_SG = re.compile(r"^\s*SG_\s+(\w+)\s*([Mm]\d*M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
                 r"\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*\[\s*([^|]*?)\s*\|\s*([^\]]*?)\s*\]\s*\"([^\"]*)\"\s*(.*)$")
_BA = re.compile(r'^BA_\s+"([^"]+)"\s+(?:(BO_)\s+(\d+)|(SG_)\s+(\d+)\s+(\w+)|(BU_)\s+(\w+))?\s*(.*?)\s*;\s*$')
_BA_DEF = re.compile(r'^BA_DEF_DEF_\s+"([^"]+)"\s+(.*?)\s*;\s*$')
_VAL = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*?)\s*;\s*$")
_VAL_PAIR = re.compile(r'(-?\d+)\s+"([^"]*)"')
_VALTYPE = re.compile(r"^SIG_VALTYPE_\s+(\d+)\s+(\w+)\s*:?\s*(\d)\s*;")
_TAG_OK = re.compile(r"[^A-Za-z0-9_.\-]")


def _x(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag(name):
    t = _TAG_OK.sub("_", name)
    return t if t[:1].isalpha() or t[:1] == "_" else "_" + t


def _num(text):
    """'8.0' -> '8', so a re-export with other number formatting is not a change."""
    t = text.strip().strip('"')
    try:
        f = float(t)
    except ValueError:
        return t
    if f != f or f in (float("inf"), float("-inf")):   # nan, inf: kept as written
        return t
    return str(int(f)) if f == int(f) and abs(f) < 1e15 else repr(f)


def dbc_to_xml(data):
    """A DBC file as an XML document with the same information."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")
    msgs, order, by_id, nodes, defaults, network, node_attrs = {}, [], {}, [], [], [], {}
    cur = None
    for line in text.splitlines():
        if line.startswith("BO_ "):
            m = _BO.match(line)
            cur = None
            if not m:
                continue
            raw = int(m.group(1))
            name = m.group(2)
            if name == "VECTOR__INDEPENDENT_SIG_MSG":
                continue
            ext = bool(raw & 0x80000000)
            cur = {"name": name, "fields": [("ID", "0x%X" % (raw & 0x1FFFFFFF)),
                                            ("FRAME", "extended" if ext else "standard"),
                                            ("DLC", m.group(3))],
                   "signals": {}, "sorder": []}
            if m.group(4) != "Vector__XXX":
                cur["fields"].append(("SENDER", m.group(4)))
            msgs[name] = cur
            order.append(name)
            by_id[raw] = cur
        elif line.lstrip().startswith("SG_ ") and cur is not None:
            m = _SG.match(line)
            if not m:
                continue
            name, mux, start, length, order_, sign, factor, offset, lo, hi, unit, recv = m.groups()
            f = [("START-BIT", start), ("LENGTH", length),
                 ("BYTE-ORDER", "little-endian" if order_ == "1" else "big-endian"),
                 ("SIGNED", "true" if sign == "-" else "false"),
                 ("FACTOR", _num(factor)), ("OFFSET", _num(offset)), ("MIN", _num(lo)), ("MAX", _num(hi))]
            if unit:
                f.append(("UNIT", unit))
            if mux:
                f.append(("MULTIPLEX", "selector" if mux == "M" else "selected by " + mux[1:].rstrip("M")))
            rs = sorted(r for r in re.split(r"[,\s]+", recv.strip()) if r and r != "Vector__XXX")
            if rs:
                f.append(("RECEIVERS", ",".join(rs)))
            cur["signals"][name] = f
            cur["sorder"].append(name)
        elif line.startswith("BU_:"):
            nodes = line[4:].split()
        elif line.startswith("BA_DEF_DEF_"):
            m = _BA_DEF.match(line)
            if m:
                defaults.append((_tag(m.group(1)), _num(m.group(2))))
        elif line.startswith("BA_ "):
            m = _BA.match(line)
            if not m:
                continue
            attr, value = _tag(m.group(1)), _num(m.group(9))
            if m.group(2):
                msg = by_id.get(int(m.group(3)))
                if msg is not None:
                    msg["fields"].append((attr, value))
            elif m.group(4):
                msg = by_id.get(int(m.group(5)))
                if msg is not None and m.group(6) in msg["signals"]:
                    msg["signals"][m.group(6)].append((attr, value))
            elif m.group(7):
                node_attrs.setdefault(m.group(8), []).append((attr, value))
            else:                                       # an attribute of the whole bus, e.g. the baud rate
                network.append((attr, value))
        elif line.startswith("SIG_VALTYPE_"):
            m = _VALTYPE.match(line)
            if m:
                msg = by_id.get(int(m.group(1)))
                if msg is not None and m.group(2) in msg["signals"]:
                    kind = {"1": "float32", "2": "float64"}.get(m.group(3), "integer")
                    msg["signals"][m.group(2)].append(("VALUE-TYPE", kind))
        elif line.startswith("VAL_ "):
            m = _VAL.match(line)
            if m:
                msg = by_id.get(int(m.group(1)))
                if msg is not None and m.group(2) in msg["signals"]:
                    pairs = sorted(_VAL_PAIR.findall(m.group(3)), key=lambda p: int(p[0]))
                    msg["signals"][m.group(2)].append(("VALUES", " ".join("%s=%s" % p for p in pairs)))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<DBC>"]

    def fields(fs, ind):
        seen = set()
        for k, v in fs:
            if k not in seen:                           # a repeated attribute line: the first one counts
                seen.add(k)
                out.append("%s<%s>%s</%s>" % (ind, k, _x(v), k))

    if network:
        out.append("  <NETWORK>")
        fields(network, "    ")
        out.append("  </NETWORK>")
    for name in order:
        msg = msgs[name]
        out.append("  <MESSAGE>")
        out.append("    <SHORT-NAME>%s</SHORT-NAME>" % _x(name))
        fields(msg["fields"], "    ")
        for sname in msg["sorder"]:
            out.append("    <SIGNAL>")
            out.append("      <SHORT-NAME>%s</SHORT-NAME>" % _x(sname))
            fields(msg["signals"][sname], "      ")
            out.append("    </SIGNAL>")
        out.append("  </MESSAGE>")
    for n in nodes:
        if n in node_attrs:
            out.append("  <NODE>")
            out.append("    <SHORT-NAME>%s</SHORT-NAME>" % _x(n))
            fields(node_attrs[n], "    ")
            out.append("  </NODE>")
        else:
            out.append("  <NODE><SHORT-NAME>%s</SHORT-NAME></NODE>" % _x(n))
    if defaults:
        out.append("  <ATTRIBUTE-DEFAULTS>")
        fields(defaults, "    ")
        out.append("  </ATTRIBUTE-DEFAULTS>")
    out.append("</DBC>")
    return "\n".join(out).encode("utf-8")


def load(data, name):
    """Bytes of a .dbc or XML file -> Tree."""
    if name.lower().endswith(".dbc"):
        if b"BO_ " not in data and b"BU_" not in data:
            raise ToolError("%s does not look like a DBC file." % name)
        return parse_tree(dbc_to_xml(data), name)
    return parse_tree(data, name)


def _nameless(tree, el):
    """Content of an element without its own name: equal for a pure rename."""
    return hash((el.tag, tuple(sorted(k.h for k in tree.kids(el) if k.tag != "SHORT-NAME"))))


def rows_of(a_tree, b_tree, masked=None):
    """One row per named element that was added, removed, renamed or changed."""
    rows = {}

    def visit(kind, trail, a, b):
        path, field, param = trail.where()
        if (kind == "add" or kind == "del") and trail.keys and trail.keys[-1][0] == K_NAME:
            el, tree = (b, b_tree) if kind == "add" else (a, a_tree)
            r = rows[path] = {"path": path, "kind": kind_of(el), "change": "added" if kind == "add" else "removed",
                              "_same": _nameless(tree, el)}
            if kind == "add":
                f = leaf_fields(tree, el)
                if masked is not None:
                    f = {k: ("***" if masked(k) else v) for k, v in f.items()}
                if f:
                    r["fields"] = f
            return
        r = rows.get(path)
        if r is None:
            r = rows[path] = {"path": path, "kind": kind_of(trail.named()) or b_tree.root.tag,
                              "change": "changed", "what": []}
        what = r.setdefault("what", [])
        if kind == "add" or kind == "del":
            el, tree = (b, b_tree) if kind == "add" else (a, a_tree)
            what.append("%s: %s %s" % (field or el.tag, "+" if kind == "add" else "-",
                                       value_text(tree, el, masked, param, 70)))
        elif kind == "set":
            what.append("%s: %s -> %s" % (field or a.tag, value_text(a_tree, a, masked, param, 70),
                                          value_text(b_tree, b, masked, param, 70)))
        elif kind == "attr":
            what.append("%s: attributes changed" % (field or a.tag))
        else:
            what.append("%s: order changed" % (field or a.tag))

    diff(a_tree, b_tree, visit)
    # a removal and an addition next to each other with the same content: that is a rename
    gone = {}
    for r in rows.values():
        if r["change"] == "removed":
            gone.setdefault((r["path"].rsplit("/", 1)[0], r["kind"], r["_same"]), []).append(r)
    for r in list(rows.values()):
        if r["change"] == "added":
            hit = gone.get((r["path"].rsplit("/", 1)[0], r["kind"], r["_same"]))
            if hit and len(hit) == 1:
                old = hit.pop()
                del rows[old["path"]]
                r["change"] = "renamed"
                r["from"] = old["path"].rsplit("/", 1)[-1]
                r.pop("fields", None)
    out = []
    for r in rows.values():
        r.pop("_same", None)
        out.append(r)
    return out


def summarize(rows):
    by = {}
    for r in rows:
        k = by.setdefault(r["kind"] or "?", {})
        k[r["change"]] = k.get(r["change"], 0) + 1
    return {kind: ", ".join("%d %s" % (n, c) for c, n in sorted(v.items())) for kind, v in sorted(by.items())}


# --------------------------------------------------------------------------
# Which configuration containers carry the name of an input element?
# --------------------------------------------------------------------------
def _index(model):
    idx = model.__dict__.get("_name_index")
    if idx is None or idx[0] != model.gen:
        conts, names, offs, pos = [], [], [], 0
        for m in model.modules():
            for c in iter_conts(m):
                n = (c.name or "").lower()
                conts.append(c)
                names.append(n)
                offs.append(pos)
                pos += len(n) + 1
        idx = model.__dict__["_name_index"] = (model.gen, "\n".join(names), offs, conts)
    return idx


def in_project(model, name, limit=3):
    """Containers whose short name contains ``name``. -> (first paths, total)"""
    tok = (name or "").lower()
    if len(tok) < 4:
        return [], 0
    _, blob, offs, conts = _index(model)
    hits, total, pos = [], 0, 0
    while True:
        pos = blob.find(tok, pos)
        if pos < 0:
            break
        i = bisect.bisect_right(offs, pos) - 1
        total += 1
        if len(hits) < limit:
            c = conts[i]
            hits.append("%s [%s]" % (c.path(), short_def(c.defref)))
        if i + 1 >= len(offs):
            break
        pos = offs[i + 1]
    return hits, total


def resolve(project, text):
    """A path the user or model gave -> absolute path of an existing file."""
    p = (text or "").strip().strip('"')
    full = p if os.path.isabs(p) else os.path.join(project.root, p)
    full = os.path.normpath(full)
    if os.path.isdir(full):
        raise ToolError("%s is a folder." % text, "Compare one file with one file.")
    if not os.path.isfile(full):
        raise ToolError("No file at %s." % text, "Give a path inside the project folder, or an absolute path.")
    if not full.lower().endswith((".arxml", ".dbc", ".xml")):
        raise ToolError("%s is not an ARXML or DBC file." % text, "Only .arxml, .xml and .dbc files are compared.")
    return full


def compare_files(project, new, old=None, masked=None):
    """-> (label of old, label of new, rows, (AUTOSAR schema of old, of new))"""
    from .project import schema_of
    new_path = resolve(project, new)
    with open(new_path, "rb") as fh:
        new_data = fh.read()
    new_label = project.rel(new_path)
    if old and not old.startswith("git:"):
        old_path = resolve(project, old)
        with open(old_path, "rb") as fh:
            old_data = fh.read()
        old_label = project.rel(old_path)
        old_name = old_path
    else:
        from .gitio import Repo
        rev = old[4:].strip() if old else "HEAD"
        try:
            repo = Repo(os.path.dirname(new_path))
        except ToolError:
            raise ToolError("'old' is missing and %s is not in a git repository." % new_label,
                            "Pass 'old': the path of the earlier version of the file.")
        rel = repo.rel(new_path)
        old_data = repo.at_head(rel, rev) if rel else None
        if old_data is None:
            raise ToolError("%s has no version at %s in git." % (new_label, rev),
                            "Pass 'old': the path of the earlier version of the file.")
        old_label = "%s at %s" % (new_label, "the last commit" if rev == "HEAD" else rev)
        old_name = new_path
    a = load(old_data, old_name)
    b = load(new_data, new_path)
    if a.root.tag != b.root.tag:
        raise ToolError("The two files are of different kinds (%s and %s)." % (a.root.tag, b.root.tag),
                        "Compare a DBC file with a DBC file and an ARXML file with an ARXML file.")
    return old_label, new_label, rows_of(a, b, masked), (schema_of(old_data), schema_of(new_data))
