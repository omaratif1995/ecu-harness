"""A plain element tree of an ARXML (or any XML) file that remembers bytes.

The ECUC reader in ``arxml.py`` understands module configurations. This one
understands nothing: it keeps elements with their byte spans and gives each
child an *identity key*, so two or three versions of a file can be matched
element by element instead of line by line. Merging and comparing build on it.

Identity of a child among its siblings:

* an element with a SHORT-NAME is identified by tag and name;
* an ECUC value (it has a DEFINITION-REF and no name) by tag and definition,
  plus its value where the parameter holds several values, or its variant
  where it carries a variation point;
* an element with a GID attribute by tag and GID;
* a tag that occurs once among its siblings by the tag alone;
* anything else is a list item, identified by its content.

Memory. Every element gets a content hash while the file is read. The
children of a *small* element are dropped again right after its hash is known;
the element stays, with its hash and its span. Two versions of a file differ
in few places, and only there are the children needed: ``Tree.kids`` reads
those few elements a second time. A 100 MB file therefore needs the memory of
its large elements only, not of its two million small ones.
"""
from __future__ import annotations

import gc
import hashlib
import re
import xml.parsers.expat as expat
from html import unescape

from . import InternalError, ToolError
from .arxml import MODCONF, detect_encoding, tag_end

IGNORED_ATTRS = ("UUID", "S", "T")      # identity, checksum and time stamp: never a reason for a difference
MAX_BYTES = 400 * 1024 * 1024
PRUNE = 4096                            # children of elements shorter than this (bytes) are loaded on demand
_NOT_TEXT = re.compile(br"<!--.*?-->|<\?.*?\?>", re.S)


class _Lazy:
    """Stands for children that were dropped after hashing. Using it by
    mistake fails loudly instead of looking like an element without children."""

    __slots__ = ()

    def __iter__(self):
        raise InternalError("children of an element were used before they were loaded")

    __len__ = __getitem__ = __iter__

    def __bool__(self):
        return True


LAZY = _Lazy()


class El:
    """One element. Offsets are byte offsets into the file it came from."""

    __slots__ = ("tag", "attrs", "s", "cs", "ce", "e", "kids", "text", "name", "dref", "val", "vp",
                 "h", "ho", "mixed")

    def __init__(self, tag, attrs, s):
        self.tag = tag
        self.attrs = attrs      # sorted tuple of (name, value) without the ignored ones, or None
        self.s = s              # start of the start tag
        self.cs = None          # start of the content; None for <X/>
        self.ce = None          # end of the content (start of the end tag)
        self.e = s              # end of the element
        self.kids = None        # child elements in document order; None = no children; LAZY = not loaded
        self.text = None        # leaf: stripped text as bytes
        self.name = None        # text of a direct SHORT-NAME child
        self.dref = None        # text of a direct DEFINITION-REF child
        self.val = None         # text of a direct VALUE or VALUE-REF child
        self.vp = None          # variant the element belongs to (label of a direct VARIATION-POINT child)
        self.h = 0              # content hash, order of children ignored
        self.ho = 0             # content hash, order of children included
        self.mixed = False      # text and elements interleaved: compared as one piece


def _build(data, encoding, strip, prune, orders):
    """One expat pass over ``data``. -> (root element, do tag names carry a prefix?)"""
    parser = expat.ParserCreate(encoding)
    stack = []
    out = []
    find = data.find
    acache = {}
    ecuc = [0]
    strip = [strip]
    ign = IGNORED_ATTRS

    def start(tag, attrs):
        if strip[0] is None:
            strip[0] = ":" in tag
        local = tag.rsplit(":", 1)[1] if strip[0] and ":" in tag else tag
        a = None
        if attrs:
            items = [kv for kv in attrs.items() if kv[0] not in ign]
            if items:
                items.sort()
                a = tuple(items)
                a = acache.setdefault(a, a)
        if local == MODCONF:
            ecuc[0] += 1
        stack.append((El(local, a, parser.CurrentByteIndex), bool(attrs)))

    def end(tag):
        el, has_attrs = stack.pop()
        i = parser.CurrentByteIndex
        kids = el.kids
        s = el.s
        local = el.tag
        a = el.attrs
        if kids is None and data[i - 2:i] == b"/>" and (tag_end(data, s) if has_attrs else find(b">", s) + 1) == i:
            el.e = i                                    # <X/>
            el.text = text = b""
            el.h = el.ho = hash((local, a, ()))
        else:
            j = i + 2 + len(tag)
            el.e = j + 1 if data[j:j + 1] == b">" else find(b">", i) + 1
            el.ce = i
            cs = el.cs = tag_end(data, s) if has_attrs else find(b">", s) + 1
            if kids is None:
                text = data[cs:i].strip()
                if b"\r" in text:
                    text = text.replace(b"\r\n", b"\n")
                if b"<" in text:                        # a comment is not content
                    text = _NOT_TEXT.sub(b"", text).strip()
                el.text = text
                el.h = el.ho = hash((local, a, text if text else ()))
            else:
                text = None
                p = cs
                mixed = False
                for k in kids:
                    if k.s != p and not data[p:k.s].isspace() and _NOT_TEXT.sub(b"", data[p:k.s]).strip():
                        mixed = True
                        break
                    p = k.e
                else:
                    if p != i and not data[p:i].isspace() and _NOT_TEXT.sub(b"", data[p:i]).strip():
                        mixed = True
                if mixed:
                    el.mixed = True
                    el.h = el.ho = hash((local, a, b" ".join(data[cs:i].split())))
                else:
                    hs = [k.h for k in kids]
                    hs.sort()
                    el.h = h = hash((local, a, tuple(hs)))
                    el.ho = h if ecuc[0] else hash((local, a, tuple([k.ho for k in kids])))
                    if orders is not None:              # remember in which order child tags occur here
                        seq = []
                        for k in kids:
                            if k.tag not in seq:
                                seq.append(k.tag)
                        if len(seq) > 1:
                            orders.add((local, tuple(seq)))
        if local == MODCONF:
            ecuc[0] -= 1
        if stack:
            p = stack[-1][0]
            if p.kids is None:
                p.kids = [el]
            else:
                p.kids.append(el)
            if text is not None:
                if local == "SHORT-NAME":
                    p.name = text
                elif local == "DEFINITION-REF":
                    p.dref = text
                elif local == "VALUE" or local == "VALUE-REF":
                    p.val = text
            elif local == "VARIATION-POINT":
                label = None
                for k in kids or ():
                    if k.tag == "SHORT-LABEL" and k.text:
                        label = k.text
                p.vp = label or hashlib.sha1(b" ".join(data[s:el.e].split())).hexdigest()[:12].encode("ascii")
        else:
            out.append(el)
        if kids is not None and el.e - s < prune:
            el.kids = LAZY                              # small: hash and span are enough until someone asks

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    collecting = gc.isenabled()
    gc.disable()
    try:
        parser.Parse(data, True)
    finally:
        if collecting:
            gc.enable()
    return out[0], bool(strip[0])


class Tree:
    __slots__ = ("data", "root", "enc", "strip", "orders")

    def __init__(self, data, root, enc, strip, orders):
        self.data = data
        self.root = root
        self.enc = enc
        self.strip = strip
        self.orders = orders    # {(parent tag, (child tags in the order they occur))}

    def text(self, raw):
        """Bytes of the file -> readable text."""
        if raw is None:
            return None
        t = raw.decode(self.enc, "replace")
        return unescape(t) if "&" in t else t

    def kids(self, el):
        """The children of an element, read again from the file if they were dropped."""
        k = el.kids
        if k is LAZY:
            utf8 = self.enc.lower().replace("-", "").replace("_", "") in ("utf8", "usascii", "ascii")
            try:
                root, _ = _build(self.data[el.s:el.e], None if utf8 else self.enc, self.strip, 0, None)
            except expat.ExpatError as exc:
                raise InternalError("an element could not be read a second time: %s" % exc)
            if root.h != el.h:
                raise InternalError("an element read a second time had other content")
            off = el.s
            stack = list(root.kids or ())
            while stack:
                n = stack.pop()
                n.s += off
                n.e += off
                if n.cs is not None:
                    n.cs += off
                    n.ce += off
                if n.kids:
                    stack.extend(n.kids)
            k = el.kids = root.kids
        return k if k is not None else ()


def parse_tree(data, what="The file", prune=None):
    """Parse XML bytes into a Tree. Raises ToolError when it cannot be used."""
    if len(data) > MAX_BYTES:
        raise ToolError("%s is larger than %d MB; that is more than this function handles."
                        % (what, MAX_BYTES // (1024 * 1024)))
    enc = detect_encoding(data)
    low = enc.lower().replace("_", "-")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or low.startswith(("utf-16", "utf16", "utf-32", "utf32", "ucs")):
        raise ToolError("%s is stored as UTF-16 or UTF-32; only UTF-8 and other single-byte-compatible "
                        "encodings are supported." % what)
    try:
        b"".decode(enc)
    except LookupError:
        enc = "utf-8"
    orders = set()
    try:
        root, strip = _build(data, None, None, PRUNE if prune is None else prune, orders)
    except expat.ExpatError as exc:
        raise ToolError("%s is not well-formed XML: %s." % (what, exc))
    return Tree(data, root, enc, strip, orders)


# --------------------------------------------------------------------------
# Identity keys
# --------------------------------------------------------------------------
K_NAME, K_DEF, K_GID, K_TAG = 0, 1, 2, 3


def _prov(k):
    if k.name is not None:
        return (K_NAME, k.tag, k.name)
    if k.dref is not None:
        return (K_DEF, k.tag, k.dref) if k.vp is None else (K_DEF, k.tag, k.dref, b"@" + k.vp)
    a = k.attrs
    if a:
        for n, v in a:
            if n == "GID":
                return (K_GID, k.tag, v)
    return (K_TAG, k.tag)


def child_keys(*lists):
    """Keys for the children of matched parents (one list per version).

    -> one {key: element} dict per list, in document order. The keys are
    decided jointly, so the same child gets the same key in every version."""
    multi = set()
    provs = []
    for lst in lists:
        seen = set()
        ps = []
        for k in lst:
            p = _prov(k)
            ps.append(p)
            if p in seen:
                multi.add(p)
            else:
                seen.add(p)
        provs.append(ps)
    out = []
    for lst, ps in zip(lists, provs):
        d = {}
        if not multi:
            for k, p in zip(lst, ps):
                d[p] = k
        else:
            count = {}
            for k, p in zip(lst, ps):
                if p in multi:
                    if p[0] == K_NAME:
                        q = p
                    elif p[0] == K_DEF and k.val is not None:
                        q = p + (k.val,)
                    else:
                        q = p + (k.h,)
                    n = count.get(q, 0)
                    count[q] = n + 1
                    p = q + ("#", n) if (n or p[0] == K_NAME) else q
                d[p] = k
        out.append(d)
    return out


def short(raw):
    """Last segment of a path given as bytes or text."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw.rsplit("/", 1)[-1]


def key_label(key):
    """Stable, readable text for one key (used in conflict keys). Content hashes
    differ from run to run, so they never appear in it."""
    kind, tag = key[0], key[1]
    rest = key[2:]
    n = 0
    if len(rest) > 2 and rest[-2] == "#":
        n, rest = rest[-1], rest[:-2]
    if kind == K_NAME:
        s = "%s[%s]" % (tag, rest[0].decode("utf-8", "replace"))
    elif kind == K_DEF:
        s = "%s{%s}" % (tag, rest[0].decode("utf-8", "replace"))
        for r in rest[1:]:
            if not isinstance(r, bytes):
                s += "=?"
            elif r[:1] == b"@":                         # the variant
                s += r.decode("utf-8", "replace")
            else:
                s += "=" + r.decode("utf-8", "replace")
    elif kind == K_GID:
        s = "%s@%s" % (tag, rest[0] if isinstance(rest[0], str) else "?")
    else:
        s = tag + ("#" if rest else "")
    return s + ("#%d" % n if n else "")


def key_name(key):
    """The short word for a key in a list: the element's name, definition or tag."""
    kind = key[0]
    if kind == K_NAME:
        return key[2].decode("utf-8", "replace")
    if kind == K_DEF:
        return short(key[2])
    if kind == K_GID:
        return str(key[2])
    return key[1]


class Trail:
    """Where the walk is: the keys from the root down to the current element."""

    def __init__(self):
        self.keys = []
        self.nodes = []

    def push(self, key, node):
        self.keys.append(key)
        self.nodes.append(node)

    def pop(self):
        self.keys.pop()
        self.nodes.pop()

    def key(self):
        return "/".join(key_label(k) for k in self.keys)

    def where(self):
        """-> (AUTOSAR path of the nearest named element, field below it, parameter name)."""
        names, tail, param = [], [], None
        for k in self.keys:
            if k[0] == K_NAME:
                names.append(k[2].decode("utf-8", "replace"))
                tail = []
                param = None
            elif k[0] == K_DEF:
                param = short(k[2])
                tail = []
            else:
                tail.append(k[1])
        path = "/" + "/".join(names)
        if param is not None:
            return path, param, param
        return path, "/".join(tail) or None, None

    def named(self):
        """The nearest named element on the trail, or None."""
        for k, n in zip(reversed(self.keys), reversed(self.nodes)):
            if k[0] == K_NAME:
                return n
        return None


def kind_of(el):
    """What an engineer would call the element: its definition name, or its tag."""
    if el is None:
        return None
    if el.dref is not None:
        return short(el.dref)
    return el.tag


def leaf_fields(tree, el, limit=12):
    """The plain values directly inside an element: {field: text}."""
    out = {}

    def take(n, depth):
        for k in tree.kids(n):
            if len(out) >= limit:
                return
            if k.tag == "SHORT-NAME" or k.tag == "DEFINITION-REF":
                continue
            if k.dref is not None and k.name is None:
                name = short(k.dref)
                v = tree.text(k.val) if k.val is not None else "..."
                out[name] = (out[name] + ", " + v) if name in out else v
            elif k.kids is None:
                if k.text:
                    out[k.tag] = tree.text(k.text)[:80]
            elif depth == 0 and k.name is None and k.tag in ("PARAMETER-VALUES", "REFERENCE-VALUES"):
                take(k, 1)

    take(el, 0)
    return out


def expand(tree, trail, el, emit):
    """Report an added or removed element with ``emit(trail, element)``. An
    element that only groups others (a wrapper such as SUB-CONTAINERS) is
    replaced by the elements inside it, so the report names real things."""
    if el.name is None and el.dref is None and el.kids is not None and not el.mixed:
        for k, n in child_keys(tree.kids(el))[0].items():
            trail.push(k, n)
            expand(tree, trail, n, emit)
            trail.pop()
    else:
        emit(trail, el)


# --------------------------------------------------------------------------
# Two-way difference
# --------------------------------------------------------------------------
def diff(a_tree, b_tree, visit, a=None, b=None, ecuc=False):
    """Walk two versions (or two matched elements of them) and report what differs.

    ``visit(kind, trail, a_el, b_el)`` is called with kind

    * ``add`` / ``del``  an element exists only in the new / old version;
    * ``set``            a leaf (or a piece compared as a whole) changed;
    * ``attr``           the attributes of an element changed;
    * ``order``          the order of the children changed (outside ECUC values).
    """
    trail = Trail()

    def walk(a, b, ecuc):
        if a.h == b.h and (ecuc or a.ho == b.ho):
            return
        a_leaf = a.kids is None or a.mixed
        b_leaf = b.kids is None or b.mixed
        if (a_leaf and b_leaf) or a.mixed or b.mixed or (a_leaf and a.text) or (b_leaf and b.text):
            visit("set", trail, a, b)
            return
        ecuc = ecuc or a.tag == MODCONF
        if a.attrs != b.attrs:
            visit("attr", trail, a, b)
        ka, kb = child_keys(a_tree.kids(a), b_tree.kids(b))
        if not ecuc:
            ca = [k for k in ka if k in kb]
            cb = [k for k in kb if k in ka]
            if ca != cb:
                visit("order", trail, a, b)
        for k, an in ka.items():
            if k not in kb:
                trail.push(k, an)
                expand(a_tree, trail, an, lambda tr, el: visit("del", tr, el, None))
                trail.pop()
        for k, bn in kb.items():
            an = ka.get(k)
            trail.push(k, bn)
            if an is None:
                expand(b_tree, trail, bn, lambda tr, el: visit("add", tr, None, el))
            else:
                walk(an, bn, ecuc)
            trail.pop()

    ra = a_tree.root if a is None else a
    rb = b_tree.root if b is None else b
    if ra.tag != rb.tag:
        visit("set", trail, ra, rb)
        return
    walk(ra, rb, ecuc)


def value_text(tree, el, masked=None, param=None, limit=90):
    """Short text for one side of a difference."""
    if el is None:
        return "(absent)"
    if masked is not None and param and masked(param):
        return "***"
    if el.kids is None or el.mixed:
        raw = el.text if el.kids is None else b" ".join(tree.data[el.cs:el.ce].split())
        t = tree.text(raw)
        return t if len(t) <= limit else t[:limit - 3] + "..."
    if el.dref is not None and el.name is None:           # an ECUC value element
        if el.val is not None:
            return tree.text(el.val)[:limit]
        for k in tree.kids(el):
            if k.tag == "VALUE-IREF":
                for kk in tree.kids(k):
                    if kk.tag == "TARGET-REF" and kk.text:
                        return tree.text(kk.text)[:limit]
        return "(value)"
    fields = leaf_fields(tree, el, 5)                   # a whole element: its kind and first values
    if masked is not None:
        fields = {k: ("***" if masked(k) else v) for k, v in fields.items()}
    t = "[%s]" % kind_of(el)
    if fields:
        t += " " + ", ".join("%s=%s" % kv for kv in fields.items())
    return t if len(t) <= limit else t[:limit - 3] + "..."


def line_for(kind, trail, a_tree, a, b_tree, b, masked=None):
    """One difference as a text line, the way the change reports write them:

        ~ /Path Field: old -> new      + /Path [Kind]      - /Path [Kind]
    """
    path, field, param = trail.where()
    if kind == "add" or kind == "del":
        el = b if kind == "add" else a
        tree = b_tree if kind == "add" else a_tree
        sign = "+" if kind == "add" else "-"
        if trail.keys and trail.keys[-1][0] == K_NAME:
            return "%s %s [%s]" % (sign, path, kind_of(el))
        return "~ %s %s: %s %s" % (path, field or el.tag, sign, value_text(tree, el, masked, param))
    if kind == "set":
        return "~ %s %s: %s -> %s" % (path, field or a.tag, value_text(a_tree, a, masked, param),
                                      value_text(b_tree, b, masked, param))
    if kind == "attr":
        return "~ %s %s: attributes %s -> %s" % (path, field or a.tag, _attrs(a), _attrs(b))
    return "~ %s %s: order of the entries changed" % (path, field or a.tag)


def change_lines(a_tree, b_tree, masked=None, limit=None, a=None, b=None):
    """The whole difference of two versions as text lines."""
    lines = []

    def visit(kind, trail, x, y):
        if limit is None or len(lines) < limit:
            lines.append(line_for(kind, trail, a_tree, x, b_tree, y, masked))

    diff(a_tree, b_tree, visit, a, b)
    return lines


def _attrs(el):
    return " ".join('%s="%s"' % kv for kv in el.attrs or ()) or "(none)"
