"""Span-preserving reader for AUTOSAR XML (ARXML).

One expat pass per file produces a detached ``FileScan``:

* a tree of *identifiable* elements (everything with a SHORT-NAME), each with
  its byte span in the file;
* ECUC value objects (module configurations, containers, parameter and
  reference values) with the byte spans needed for surgical text edits;
* ECUC definition objects (module, container, parameter, reference defs).

Nothing here writes files. All offsets are byte offsets into the scanned data.
"""
from __future__ import annotations

import gc
import re
import sys
import xml.parsers.expat as expat
from html import unescape

PKG = "AR-PACKAGE"
MODCONF = "ECUC-MODULE-CONFIGURATION-VALUES"
CONTVAL = "ECUC-CONTAINER-VALUE"
MODDEF = "ECUC-MODULE-DEF"
REFVAL = "ECUC-REFERENCE-VALUE"
IREFVAL = "ECUC-INSTANCE-REFERENCE-VALUE"
NUMVAL = "ECUC-NUMERICAL-PARAM-VALUE"
TEXTVAL = "ECUC-TEXTUAL-PARAM-VALUE"

# Elements that may follow DEFINITION-REF in a module configuration and must
# stay in front of the CONTAINERS wrapper.
_MOD_HEADER = frozenset((
    "ECUC-DEF-EDITION", "IMPLEMENTATION-CONFIG-VARIANT",
    "MODULE-DESCRIPTION-REF", "POST-BUILD-VARIANT-USED",
))

PARAM_KINDS = {
    "ECUC-INTEGER-PARAM-DEF": "integer",
    "ECUC-FLOAT-PARAM-DEF": "float",
    "ECUC-BOOLEAN-PARAM-DEF": "boolean",
    "ECUC-ENUMERATION-PARAM-DEF": "enum",
    "ECUC-STRING-PARAM-DEF": "string",
    "ECUC-FUNCTION-NAME-DEF": "function-name",
    "ECUC-LINKER-SYMBOL-DEF": "linker-symbol",
    "ECUC-MULTILINE-STRING-PARAM-DEF": "multiline-string",
    "ECUC-ADD-INFO-PARAM-DEF": "add-info",
}
REF_KINDS = {
    "ECUC-REFERENCE-DEF": "reference",
    "ECUC-CHOICE-REFERENCE-DEF": "choice-reference",
    "ECUC-SYMBOLIC-NAME-REFERENCE-DEF": "symbolic-name-reference",
    "ECUC-FOREIGN-REFERENCE-DEF": "foreign-reference",
    "ECUC-INSTANCE-REFERENCE-DEF": "instance-reference",
    "ECUC-URI-REFERENCE-DEF": "uri-reference",
}
KIND_TAGS = {v: k for k, v in PARAM_KINDS.items()}
KIND_TAGS.update({v: k for k, v in REF_KINDS.items()})
NUMERIC_KINDS = ("integer", "float", "boolean")
CONT_DEF_TAGS = ("ECUC-PARAM-CONF-CONTAINER-DEF", "ECUC-CHOICE-CONTAINER-DEF")


# --------------------------------------------------------------------------
# Tree model
# --------------------------------------------------------------------------
class Node:
    """An identifiable element. ``kids`` maps SHORT-NAME to child Node."""

    __slots__ = ("name", "tag", "parent", "kids", "file", "s", "e")

    def __init__(self, name, tag, file=None, s=-1, e=-1):
        self.name = name
        self.tag = tag
        self.parent = None
        self.kids = None
        self.file = file
        self.s = s
        self.e = e

    def path(self):
        parts = []
        n = self
        while n is not None and n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))

    def add(self, kid):
        if self.kids is None:
            self.kids = {}
        self.kids[kid.name] = kid
        kid.parent = self

    def walk(self):
        """All descendants, parents before children."""
        stack = list(reversed(list(self.kids.values()))) if self.kids else []
        while stack:
            n = stack.pop()
            yield n
            if n.kids:
                stack.extend(reversed(list(n.kids.values())))


class Entry:
    """One parameter or reference value inside a container."""

    __slots__ = ("tag", "defref", "name", "def_dest", "value", "value_dest", "contexts",
                 "s", "e", "vs", "ve", "auto")

    def __init__(self, tag, s, e):
        self.tag = tag
        self.defref = ""
        self.name = ""
        self.auto = False     # IS-AUTO-VALUE: the tool calculates this value itself
        self.def_dest = None
        self.value = None
        self.value_dest = None
        self.contexts = None
        self.s = s
        self.e = e
        self.vs = None   # span of the VALUE / VALUE-REF / VALUE-IREF element
        self.ve = None

    @property
    def is_ref(self):
        return self.tag == REFVAL or self.tag == IREFVAL


class Cont(Node):
    """An ECUC container value, or a module configuration (tag MODCONF)."""

    __slots__ = ("defref", "def_dest", "params", "refs", "sn", "hdr_end",
                 "wp", "wr", "ws", "uuid")

    def __init__(self, tag, file, s):
        Node.__init__(self, None, tag, file, s)
        self.defref = ""
        self.def_dest = None
        self.params = None
        self.refs = None
        self.sn = None        # (start of the SHORT-NAME element, end of its text)
        self.hdr_end = None   # end of the last header element
        self.wp = None        # wrapper spans: (start, inner_end | None, end)
        self.wr = None
        self.ws = None
        self.uuid = False

    @property
    def is_module(self):
        return self.tag == MODCONF

    def module(self):
        n = self
        while n is not None and n.tag != MODCONF:
            n = n.parent
        return n


def iter_conts(node):
    """All ECUC containers below ``node`` (modules excluded)."""
    for n in node.walk():
        if n.tag == CONTVAL:
            yield n


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------
class PDef:
    """Parameter or reference definition."""

    __slots__ = ("name", "path", "kind", "is_ref", "desc", "lower", "upper",
                 "default", "min", "max", "literals", "dests", "dest_type", "parent")

    def __init__(self, name, path, kind, is_ref):
        self.name = name
        self.path = path
        self.kind = kind
        self.is_ref = is_ref
        self.desc = ""
        self.lower = 1
        self.upper = 1
        self.default = None
        self.min = None
        self.max = None
        self.literals = None
        self.dests = None
        self.dest_type = None
        self.parent = None


class CDef:
    """Container definition (or module definition when ``is_module``)."""

    __slots__ = ("name", "path", "desc", "lower", "upper", "params", "refs",
                 "subs", "choice", "parent", "is_module", "refined", "file")

    def __init__(self, name, path, is_module=False):
        self.name = name
        self.path = path
        self.desc = ""
        self.lower = 1
        self.upper = 1
        self.params = {}
        self.refs = {}
        self.subs = {}
        self.choice = False
        self.parent = None
        self.is_module = is_module
        self.refined = None
        self.file = None

    def module(self):
        d = self
        while d.parent is not None:
            d = d.parent
        return d

    def all_defs(self):
        yield self
        for p in self.params.values():
            yield p
        for r in self.refs.values():
            yield r
        for s in self.subs.values():
            for d in s.all_defs():
                yield d


def mult_text(d):
    return "%d..%s" % (d.lower, "*" if d.upper is None else d.upper)


def parse_number(text):
    """AUTOSAR numeric literal to int or float; None when it is not a number."""
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    try:
        return int(t, 0)
    except ValueError:
        pass
    try:
        if t.lower().startswith(("0x", "0b", "0o")):
            return None
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------
class FileScan:
    __slots__ = ("root", "moddefs", "uses_uuid", "problems", "enc")

    def __init__(self):
        self.root = Node("", "#root")
        self.root.kids = {}
        self.moddefs = []
        self.uses_uuid = False
        self.problems = []
        self.enc = "utf-8"

    def node(self, path):
        return walk_path(self.root, path)


def walk_path(root, path):
    n = root
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        kids = n.kids
        n = kids.get(seg) if kids else None
        if n is None:
            return None
    return n


def detect_encoding(data):
    m = re.search(br"encoding=[\"']([\w.\-]+)[\"']", data[:200])
    return m.group(1).decode("ascii") if m else "utf-8"


def tag_end(data, pos):
    """Index just after the '>' that closes the start tag beginning at pos."""
    i = pos
    while True:
        j = data.find(b">", i)
        if j < 0:
            raise ValueError("unterminated tag at byte %d" % pos)
        q1 = data.find(b'"', i, j)
        q2 = data.find(b"'", i, j)
        if q1 < 0 and q2 < 0:
            return j + 1
        if q1 >= 0 and (q2 < 0 or q1 < q2):
            q, ch = q1, b'"'
        else:
            q, ch = q2, b"'"
        k = data.find(ch, q + 1)
        if k < 0:
            raise ValueError("unterminated attribute at byte %d" % pos)
        i = k + 1


# frame kinds of the streaming reader for module configurations
K_CONT, K_ENTRY, K_LEAF, K_WP, K_WR, K_WS, K_IREF = range(7)


def scan(data, src=None, generic=False):
    """Parse ARXML bytes. Raises ``expat.ExpatError`` when not well-formed.

    Module configurations are by far the largest part of real files, so they
    are read by a streaming state machine that builds the model objects
    directly and takes element text straight from the bytes. Module definitions
    use a simpler capture-then-convert path. ``generic=True`` forces that
    simpler path for configurations as well; the tests use it as a second,
    independent implementation to compare against.
    """
    out = FileScan()
    enc = out.enc = detect_encoding(data)
    stack = []          # outside modules: [tag, start, node]
    nodes = [out.root]  # active identifiable nodes
    cs = []             # streaming frames: [kind, object, start]
    cap = None          # capture stack of records [tag, attrs, s, end, inner_end, text, kids]
    mode = 0            # 0 outside, 1 streaming a configuration, 2 capturing
    skip = 0            # depth inside an element we do not interpret
    strip = None        # do tag names carry a namespace prefix?
    parser = expat.ParserCreate()
    parser.buffer_text = True
    intern = sys.intern
    find = data.find
    names = {}

    def slow_text(raw):
        """Exact text of a leaf that contains markup (comment, CDATA, elements)."""
        buf = []
        p2 = expat.ParserCreate(None if enc.lower().replace("-", "") == "utf8" else enc)
        p2.buffer_text = True
        p2.CharacterDataHandler = buf.append
        p2.Parse(b"<x>" + raw + b"</x>", True)
        return "".join(buf).strip()

    def leaf(s, i, attrs):
        """Text between the start tag at s and the end tag at i."""
        te = tag_end(data, s) if attrs else find(b">", s) + 1
        if te >= i:
            return ""
        raw = data[te:i]
        if b"<" in raw:
            return slow_text(raw)
        t = raw.decode(enc, "replace")
        if "&" in t:
            t = unescape(t)
        if "\r" in t:
            t = t.replace("\r\n", "\n").replace("\r", "\n")
        return t.strip()

    def short_of(d):
        nm = names.get(d)
        if nm is None:
            nm = names[d] = intern(d.rsplit("/", 1)[-1])
        return nm

    def attach_module(c):
        parent = nodes[-1]
        if not c.name:
            out.problems.append("module configuration without SHORT-NAME")
        elif parent.kids and c.name in parent.kids:
            out.problems.append("duplicate element %s/%s" % (parent.path().rstrip("/"), c.name))
        else:
            parent.add(c)

    # ---- capture-then-convert path ---------------------------------------
    def txt(el):
        t = el[5]
        return t.strip() if t else ""

    def dest(el):
        a = el[1]
        return a.get("DEST") if a else None

    def conv_entry(x):
        en = Entry(x[0], x[2], x[3])
        for k in x[6]:
            t = k[0]
            if t == "DEFINITION-REF":
                en.defref = intern(txt(k))
                en.name = short_of(en.defref)
                en.def_dest = dest(k)
            elif t == "VALUE":
                en.value = txt(k)
                en.vs, en.ve = k[2], k[3]
            elif t == "VALUE-REF":
                en.value = txt(k)
                en.value_dest = dest(k)
                en.vs, en.ve = k[2], k[3]
            elif t == "IS-AUTO-VALUE":
                en.auto = txt(k).lower() in ("true", "1")
            elif t == "VALUE-IREF":
                en.contexts = []
                for kk in k[6]:
                    if kk[0] == "CONTEXT-ELEMENT-REF":
                        en.contexts.append(txt(kk))
                    elif kk[0] == "TARGET-REF":
                        en.value = txt(kk)
                        en.value_dest = dest(kk)
                en.vs, en.ve = k[2], k[3]
        return en

    def conv_cont(el):
        c = Cont(el[0], src, el[2])
        c.e = el[3]
        a = el[1]
        if a and "UUID" in a:
            c.uuid = True
            out.uses_uuid = True
        is_mod = el[0] == MODCONF
        for k in el[6]:
            t = k[0]
            if t == "SHORT-NAME":
                c.name = txt(k)
                if k[4] is not None:
                    c.sn = (k[2], k[4])
                c.hdr_end = k[3]
            elif t == "DEFINITION-REF":
                c.defref = intern(txt(k))
                c.def_dest = dest(k)
                c.hdr_end = k[3]
            elif t == "PARAMETER-VALUES":
                c.wp = (k[2], k[4], k[3])
                c.params = [conv_entry(x) for x in k[6]]
            elif t == "REFERENCE-VALUES":
                c.wr = (k[2], k[4], k[3])
                c.refs = [conv_entry(x) for x in k[6]]
            elif t == "SUB-CONTAINERS" or t == "CONTAINERS":
                c.ws = (k[2], k[4], k[3])
                for x in k[6]:
                    if x[0] != CONTVAL:
                        continue
                    kid = conv_cont(x)
                    if not kid.name:
                        out.problems.append("container without SHORT-NAME at byte %d" % x[2])
                    elif c.kids and kid.name in c.kids:
                        out.problems.append("duplicate SHORT-NAME '%s' in %s" % (kid.name, c.name))
                    else:
                        c.add(kid)
            elif is_mod and t in _MOD_HEADER:
                c.hdr_end = k[3]
        return c

    def child(el, tag):
        for k in el[6]:
            if k[0] == tag:
                return k
        return None

    def ctext(el, tag):
        k = child(el, tag)
        return txt(k) if k is not None else None

    def all_text(el):
        parts = [el[5] or ""]
        for k in el[6]:
            parts.append(all_text(k))
        return " ".join(" ".join(parts).split())

    def common(el, d):
        k = child(el, "DESC")
        if k is not None:
            d.desc = all_text(k)
        lo = ctext(el, "LOWER-MULTIPLICITY")
        up = ctext(el, "UPPER-MULTIPLICITY")
        inf = ctext(el, "UPPER-MULTIPLICITY-INFINITE")
        d.lower = int(lo) if lo and lo.isdigit() else 1
        if (inf and inf.lower() in ("true", "1")) or up == "*":
            d.upper = None
        elif up and up.isdigit():
            d.upper = int(up)
        else:
            d.upper = 1

    def conv_pdef(el, parent):
        name = ctext(el, "SHORT-NAME") or ""
        is_ref = el[0] in REF_KINDS
        kind = REF_KINDS.get(el[0]) or PARAM_KINDS.get(el[0])
        d = PDef(name, parent.path + "/" + name, kind, is_ref)
        d.parent = parent
        common(el, d)
        if is_ref:
            dests = []
            for k in el[6]:
                if k[0] == "DESTINATION-REF":
                    dests.append(txt(k))
                elif k[0] == "DESTINATION-REFS":
                    dests.extend(txt(x) for x in k[6] if x[0] == "DESTINATION-REF")
            d.dests = dests
            d.dest_type = ctext(el, "DESTINATION-TYPE")
        else:
            d.default = ctext(el, "DEFAULT-VALUE")
            d.min = parse_number(ctext(el, "MIN"))
            d.max = parse_number(ctext(el, "MAX"))
            lits = child(el, "LITERALS")
            if lits is not None:
                d.literals = [ctext(x, "SHORT-NAME") or "" for x in lits[6]]
        return d

    def conv_cdef(el, parent):
        name = ctext(el, "SHORT-NAME") or ""
        d = CDef(name, parent.path + "/" + name)
        d.parent = parent
        d.choice = el[0] == "ECUC-CHOICE-CONTAINER-DEF"
        common(el, d)
        for k in el[6]:
            t = k[0]
            if t == "PARAMETERS":
                for x in k[6]:
                    if x[0] in PARAM_KINDS:
                        p = conv_pdef(x, d)
                        d.params[p.name] = p
            elif t == "REFERENCES":
                for x in k[6]:
                    if x[0] in REF_KINDS:
                        p = conv_pdef(x, d)
                        d.refs[p.name] = p
            elif t == "SUB-CONTAINERS" or t == "CHOICES":
                for x in k[6]:
                    if x[0] in CONT_DEF_TAGS:
                        s = conv_cdef(x, d)
                        d.subs[s.name] = s
        return d

    def conv_moddef(el, pkg_path):
        name = ctext(el, "SHORT-NAME") or ""
        d = CDef(name, pkg_path.rstrip("/") + "/" + name, is_module=True)
        d.file = src
        common(el, d)
        d.refined = ctext(el, "REFINED-MODULE-DEF-REF")
        k = child(el, "CONTAINERS")
        if k is not None:
            for x in k[6]:
                if x[0] in CONT_DEF_TAGS:
                    s = conv_cdef(x, d)
                    d.subs[s.name] = s
        return d

    # ---- expat handlers --------------------------------------------------
    def start(tag, attrs):
        nonlocal cap, mode, skip, strip
        if skip:
            skip += 1
            return
        i = parser.CurrentByteIndex
        if strip is None:
            strip = ":" in tag
        if strip and ":" in tag:
            tag = tag.rsplit(":", 1)[1]
        if mode == 1:
            fr = cs[-1]
            k = fr[0]
            if k == K_WP or k == K_WR:
                cs.append([K_ENTRY, Entry(tag, i, i), i])
            elif k == K_ENTRY:
                if tag == "VALUE" or tag == "DEFINITION-REF" or tag == "VALUE-REF" or tag == "IS-AUTO-VALUE":
                    cs.append([K_LEAF, attrs, i])
                elif tag == "VALUE-IREF":
                    fr[1].contexts = []
                    cs.append([K_IREF, fr[1], i])
                else:
                    skip = 1
            elif k == K_CONT:
                c = fr[1]
                if tag == "SHORT-NAME" or tag == "DEFINITION-REF":
                    cs.append([K_LEAF, attrs, i])
                elif tag == "PARAMETER-VALUES":
                    c.params = []
                    cs.append([K_WP, c, i])
                elif tag == "REFERENCE-VALUES":
                    c.refs = []
                    cs.append([K_WR, c, i])
                elif tag == "SUB-CONTAINERS" or tag == "CONTAINERS":
                    cs.append([K_WS, c, i])
                elif c.tag == MODCONF and tag in _MOD_HEADER:
                    cs.append([K_LEAF, attrs, i])
                else:
                    skip = 1
            elif k == K_WS and tag == CONTVAL:
                c = Cont(tag, src, i)
                if attrs and "UUID" in attrs:
                    c.uuid = True
                    out.uses_uuid = True
                cs.append([K_CONT, c, i])
            elif k == K_IREF and (tag == "CONTEXT-ELEMENT-REF" or tag == "TARGET-REF"):
                cs.append([K_LEAF, attrs, i])
            else:
                skip = 1
            return
        if mode == 2:
            el = [tag, attrs or None, i, i, None, None, []]
            cap[-1][6].append(el)
            cap.append(el)
            return
        if tag == MODCONF and not generic:
            c = Cont(tag, src, i)
            if attrs and "UUID" in attrs:
                c.uuid = True
                out.uses_uuid = True
            cs.append([K_CONT, c, i])
            mode = 1
        elif tag == MODCONF or tag == MODDEF:
            cap = [[tag, attrs or None, i, i, None, None, []]]
            mode = 2
            parser.CharacterDataHandler = chars
        else:
            stack.append([tag, i, None])

    def chars(d):
        el = cap[-1]
        t = el[5]
        if t is None:
            if not d.isspace():
                el[5] = d
        else:
            el[5] = t + d

    def end(tag):
        nonlocal cap, mode, skip
        if skip:
            skip -= 1
            return
        i = parser.CurrentByteIndex
        if mode == 1:
            fr = cs.pop()
            k = fr[0]
            s = fr[2]
            if data[i - 2:i] == b"/>" and tag_end(data, s) == i:
                e_end, inner = i, None                 # <X/>: the end event sits right after the tag
            else:
                j = i + 2 + len(tag)                   # </X>
                e_end = j + 1 if data[j:j + 1] == b">" else find(b">", i) + 1
                inner = i
            if k == K_LEAF:
                if strip and ":" in tag:
                    tag = tag.rsplit(":", 1)[1]
                attrs = fr[1]
                text = leaf(s, i, attrs) if inner is not None else ""
                pf = cs[-1]
                pk = pf[0]
                obj = pf[1]
                if pk == K_ENTRY:
                    if tag == "VALUE":
                        obj.value = text
                        obj.vs, obj.ve = s, e_end
                    elif tag == "DEFINITION-REF":
                        obj.defref = d = intern(text)
                        obj.name = short_of(d)
                        obj.def_dest = attrs.get("DEST") if attrs else None
                    elif tag == "IS-AUTO-VALUE":
                        obj.auto = text.lower() in ("true", "1")
                    else:
                        obj.value = text
                        obj.value_dest = attrs.get("DEST") if attrs else None
                        obj.vs, obj.ve = s, e_end
                elif pk == K_CONT:
                    if tag == "SHORT-NAME":
                        obj.name = text
                        if inner is not None:
                            obj.sn = (s, i)
                    elif tag == "DEFINITION-REF":
                        obj.defref = intern(text)
                        obj.def_dest = attrs.get("DEST") if attrs else None
                    obj.hdr_end = e_end
                elif tag == "TARGET-REF":
                    obj.value = text
                    obj.value_dest = attrs.get("DEST") if attrs else None
                else:
                    obj.contexts.append(text)
            elif k == K_ENTRY:
                en = fr[1]
                en.e = e_end
                pf = cs[-1]
                (pf[1].params if pf[0] == K_WP else pf[1].refs).append(en)
            elif k == K_WP:
                fr[1].wp = (s, inner, e_end)
            elif k == K_WR:
                fr[1].wr = (s, inner, e_end)
            elif k == K_WS:
                fr[1].ws = (s, inner, e_end)
            elif k == K_IREF:
                fr[1].vs, fr[1].ve = s, e_end
            else:
                c = fr[1]
                c.e = e_end
                if not cs:
                    mode = 0
                    attach_module(c)
                else:
                    parent = cs[-1][1]
                    if not c.name:
                        out.problems.append("container without SHORT-NAME at byte %d" % s)
                    elif parent.kids and c.name in parent.kids:
                        out.problems.append("duplicate SHORT-NAME '%s' in %s" % (c.name, parent.name))
                    else:
                        parent.add(c)
            return
        if mode == 2:
            el = cap.pop()
            if el[5] is None and not el[6] and data[i - 2:i] == b"/>":
                el[3] = i
            else:
                j = i + 2 + len(tag)
                el[3] = j + 1 if data[j:j + 1] == b">" else find(b">", i) + 1
                el[4] = i
            if not cap:
                cap = None
                mode = 0
                parser.CharacterDataHandler = None
                if el[0] == MODCONF:
                    attach_module(conv_cont(el))
                else:
                    out.moddefs.append(conv_moddef(el, nodes[-1].path()))
            return
        fr = stack.pop()
        if fr[0] == "SHORT-NAME":
            name = leaf(fr[1], i, None)
            if stack and stack[-1][2] is None and name:
                pf = stack[-1]
                parent = nodes[-1]
                if parent.kids and name in parent.kids:
                    out.problems.append("duplicate element %s/%s" % (parent.path().rstrip("/"), name))
                    return
                node = Node(name, pf[0], None if pf[0] == PKG else src, pf[1])
                parent.add(node)
                pf[2] = node
                nodes.append(node)
        elif fr[2] is not None:
            fr[2].e = find(b">", i) + 1
            nodes.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    collecting = gc.isenabled()
    gc.disable()            # building a large tree: the cycle collector only costs time here
    try:
        parser.Parse(data, True)
    finally:
        if collecting:
            gc.enable()
    return out


def dispose(root):
    """Cut the parent links of a tree that is no longer used, so its memory is
    released at once instead of waiting for the cycle collector."""
    stack = [root]
    while stack:
        n = stack.pop()
        kids = n.kids
        if kids:
            stack.extend(kids.values())
            n.kids = None
        n.parent = None
