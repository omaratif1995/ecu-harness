"""Three-way merge of ARXML by element identity instead of by text line.

A text merge sees two engineers who changed neighbouring lines of the same
file and gives up. This merge matches the elements of the three versions
(common ancestor, ours, theirs) by what they are: a container by its short
name, a parameter value by its definition, and so on (see ``xtree``). What
only one side changed is taken. What both sides changed in the same way is
kept. Only what both sides changed differently is a conflict, and it is
reported as one line a person can decide: this parameter, base, ours, theirs.

The result is our file with their changes spliced in as bytes copied from
their file. Nothing is re-written or re-formatted, so vendor extensions,
comments and layout survive. Before the result is handed back it is parsed
again and its content compared with the content the merge decided on; if the
two differ, there is no result.
"""
from __future__ import annotations

import re

from . import InternalError, ToolError
from .arxml import MODCONF
from .xtree import (K_DEF, K_TAG, Trail, child_keys, diff, expand, key_name, kind_of, line_for, parse_tree,
                    value_text)

_WS = b" \t\r\n"
_ECUC_ORDER = {"PARAMETER-VALUES": 1, "REFERENCE-VALUES": 2, "SUB-CONTAINERS": 3, "CONTAINERS": 3}
_RAW_NAME = re.compile(br"<([^\s/>]+)")
KINDS = {
    "value": "both sides set a different value",
    "both-added": "both sides added an element with this name, with different content",
    "deleted-by-theirs": "their side deleted what our side changed",
    "deleted-by-ours": "our side deleted what their side changed",
    "attributes": "both sides changed the attributes of the element",
    "order": "both sides changed the order of the entries (the content of both sides is kept either way)",
}


def _order_text(keys, other):
    """A few names of an ordered list, from the first place where it differs from ``other``."""
    i = 0
    while i < len(keys) and i < len(other) and keys[i] == other[i]:
        i += 1
    i = max(0, i - 1)
    names = [key_name(k) for k in keys[i:i + 4]]
    return ("... " if i else "") + ", ".join(names) + (", ..." if len(keys) > i + 4 else "")


def _lead(data, pos):
    """Start of the whitespace that precedes pos."""
    i = pos
    while i > 0 and data[i - 1] in _WS:
        i -= 1
    return i


def _with_lead(data, el):
    """The element's bytes including the line break and indentation before it."""
    i = _lead(data, el.s)
    if i > 0 and data[i - 1:i] != b">":
        i = el.s
    return data[i:el.e]


def _open_tag(data, el):
    """Start tag of an element, in the form that takes content."""
    if el.cs is not None:
        return data[el.s:el.cs]
    return data[el.s:el.e - 2].rstrip() + b">"


def _empty_tag(data, el):
    if el.cs is None:
        return data[el.s:el.e]
    return data[el.s:el.cs - 1].rstrip() + b"/>"


def _nl(data):
    return b"\r\n" if b"\r\n" in data[:20000] else b"\n"


class MergeResult:
    __slots__ = ("data", "conflicts", "applied", "count")

    def __init__(self, data, conflicts, applied, count):
        self.data = data            # merged bytes; undecided conflicts keep our side
        self.conflicts = conflicts  # [{key, kind, where, param|field, base, ours, theirs, decision?}]
        self.applied = applied      # lines describing what was taken from their side (capped)
        self.count = count          # how many changes were taken from their side

    @property
    def open(self):
        return [c for c in self.conflicts if not c.get("decision")]


class Merge3:
    def __init__(self, base, ours, theirs, decisions=None, masked=None):
        self.O = parse_tree(ours, "Our version")
        self.T = parse_tree(theirs, "Their version")
        self.B = parse_tree(base, "The common ancestor") if base is not None and base.strip() else None
        if self.O.enc.lower().replace("-", "") != self.T.enc.lower().replace("-", ""):
            raise ToolError("The two versions use different encodings (%s and %s)." % (self.O.enc, self.T.enc),
                            "Bring both files to the same encoding first.")
        self.decisions = decisions or {}
        self.masked = masked
        self.edits = []
        self.conflicts = []
        self.applied = []
        self.count = 0
        self.trail = Trail()
        self._ord = None
        self._owner = None          # the ECUC value elements (ours, theirs) the walk is inside of
        self._keys = {}             # conflict key -> how often it occurred (keys must be unique)
        onl, tnl = _nl(ours), _nl(theirs)
        self._eol = None if onl == tnl else (tnl, onl)

    # ---- bytes -----------------------------------------------------------
    def _theirs(self, blob):
        """Bytes copied from their file, in the line-ending style of ours."""
        if self._eol is not None:
            if self._eol[1] == b"\r\n":
                blob = blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            else:
                blob = blob.replace(b"\r\n", b"\n")
        return blob

    def _edit(self, s, e, blob):
        self.edits.append((s, e, len(self.edits), blob))

    def _take(self, o, t):
        self._edit(o.s, o.e, self._theirs(self.T.data[t.s:t.e]))

    def _remove(self, on):
        data = self.O.data
        i = _lead(data, on.s)
        if i > 0 and data[i - 1:i] != b">":
            i = on.s
        self._edit(i, on.e, b"")

    def _apply(self):
        data = self.O.data
        out, pos = [], 0
        for s, e, _, blob in sorted(self.edits):
            if s < pos:
                raise InternalError("merge edits overlap at byte %d" % s)
            out.append(data[pos:s])
            out.append(blob)
            pos = e
        out.append(data[pos:])
        return b"".join(out)

    # ---- bookkeeping -----------------------------------------------------
    def _note(self, kind, a, b):
        """Record one change that was taken from their side."""
        def emit(tr, x, y):
            self.count += 1
            if len(self.applied) < 400:
                self.applied.append(line_for(kind, tr, self.O, x, self.T, y, self.masked))

        if kind == "add":
            expand(self.T, self.trail, b, lambda tr, el: emit(tr, None, el))
        elif kind == "del":
            expand(self.O, self.trail, a, lambda tr, el: emit(tr, el, None))
        else:
            emit(self.trail, a, b)

    def _changes(self, a_tree, a, b_tree, b, ecuc=False):
        """What one side did to an element, in a few words."""
        out = []

        def visit(kind, trail, x, y):
            if len(out) > 3:
                return
            _, field, param = trail.where()
            if kind == "set":
                out.append("%s: %s -> %s" % (field or x.tag, value_text(a_tree, x, self.masked, param, 40),
                                             value_text(b_tree, y, self.masked, param, 40)))
            elif kind in ("add", "del"):
                el, tree = (y, b_tree) if kind == "add" else (x, a_tree)
                name = tree.text(el.name) if el.name is not None else (field or el.tag)
                out.append("%s %s" % ("+" if kind == "add" else "-", name))
            else:
                out.append("%s changed" % (field or x.tag))

        diff(a_tree, b_tree, visit, a, b, ecuc)
        if len(out) > 3:
            out[3:] = ["..."]
        return ", ".join(out) or "content"

    @staticmethod
    def _auto(tree, el):
        """Is this ECUC value marked as calculated by the tool (IS-AUTO-VALUE)?"""
        return any(k.tag == "IS-AUTO-VALUE" and (k.text or b"").lower() in (b"true", b"1") for k in tree.kids(el))

    def _conflict(self, kind, b, o, t, ecuc=False, orders=None):
        """Record a conflict. -> the decision taken for it, or None."""
        tr = self.trail
        key = "%s !%s" % (tr.key(), kind)
        n = self._keys[key] = self._keys.get(key, 0) + 1
        if n > 1:                                       # same place twice (identical siblings): still one key each
            key += " #%d" % n
        path, field, param = tr.where()
        c = {"key": key, "kind": kind, "where": path}
        if param:
            c["param"] = param
        elif field:
            c["field"] = field
        m = self.masked
        if kind == "deleted-by-theirs":
            c["element"] = kind_of(o)
            c["ours"] = "changed (%s)" % self._changes(self.B, b, self.O, o, ecuc)
            c["theirs"] = "deleted"
        elif kind == "deleted-by-ours":
            c["element"] = kind_of(t)
            c["ours"] = "deleted"
            c["theirs"] = "changed (%s)" % self._changes(self.B, b, self.T, t, ecuc)
        elif kind == "order":
            c["ours"] = "our order: " + _order_text(orders[0], orders[1])
            c["theirs"] = "their order: " + _order_text(orders[1], orders[0])
        elif kind == "attributes":
            for side, el in (("base", b), ("ours", o), ("theirs", t)):
                if el is not None:
                    c[side] = " ".join('%s="%s"' % kv for kv in el.attrs or ()) or "(none)"
        elif kind == "both-added":
            c["element"] = kind_of(o)
            c["ours"] = value_text(self.O, o, m, param)
            c["theirs"] = "differs: %s" % self._changes(self.O, o, self.T, t, ecuc)
        else:
            if b is not None:
                c["base"] = value_text(self.B, b, m, param)
            c["ours"] = value_text(self.O, o, m, param)
            c["theirs"] = value_text(self.T, t, m, param)
            if (kind == "value" and self._owner is not None and self._auto(self.O, self._owner[0])
                    and self._auto(self.T, self._owner[1])):
                c["calculated"] = True      # both values come from the tool, which will compute it again
        d = self.decisions.get(key)
        if d in ("ours", "theirs"):
            c["decision"] = d
        else:
            d = None
        self.conflicts.append(c)
        return d

    def _precedes(self, parent, a, b):
        """Does a child with tag ``a`` come before one with tag ``b``?"""
        if a == b:
            return False
        ia, ib = _ECUC_ORDER.get(a), _ECUC_ORDER.get(b)
        if ia and ib:
            return ia < ib
        if self._ord is None:                   # the order the files themselves use, noted while reading
            pairs = set()
            for tree in (self.O, self.T, self.B):
                for ptag, seq in (tree.orders if tree is not None else ()):
                    for i, x in enumerate(seq):
                        for y in seq[i + 1:]:
                            pairs.add((ptag, x, y))
            self._ord = pairs
        return (parent, a, b) in self._ord and (parent, b, a) not in self._ord

    # ---- the merge -------------------------------------------------------
    def run(self):
        o, t = self.O.root, self.T.root
        b = self.B.root if self.B is not None else None
        if o.tag != t.tag or (b is not None and b.tag != o.tag):
            raise ToolError("The versions do not have the same root element.")
        want = self._merge(b, o, t, False)
        data = self._apply() if self.edits else self.O.data
        if self.edits:
            try:
                got = parse_tree(data, "The merged result").root.h
            except ToolError as exc:
                raise InternalError(exc.message)
            if got != want:
                raise InternalError("the merged file does not have the content the merge decided on")
        return MergeResult(data, self.conflicts, self.applied, self.count)

    def _merge(self, b, o, t, ecuc):
        """Merge one matched element. -> content hash the merged element must have."""
        if b is not None and t.h == b.h and (ecuc or t.ho == b.ho):
            return o.h                                  # their side did not touch it
        if o.h == t.h and (ecuc or o.ho == t.ho):
            return o.h                                  # both sides agree
        o_leaf = o.kids is None or o.mixed
        t_leaf = t.kids is None or t.mixed
        b_leaf = b is None or b.kids is None or b.mixed
        if ((o_leaf and t_leaf and b_leaf) or o.mixed or t.mixed or (b is not None and b.mixed)
                or (o_leaf and o.text) or (t_leaf and t.text) or (b is not None and b_leaf and b.text)):
            return self._whole(b, o, t, "value", ecuc)
        return self._inner(b, o, t, ecuc or o.tag == MODCONF)

    def _whole(self, b, o, t, kind, ecuc):
        """An element that is compared in one piece."""
        if b is not None and o.h == b.h and (ecuc or o.ho == b.ho):
            self._take(o, t)
            self._note("set", o, t)
            return t.h
        if self._conflict(kind, b, o, t, ecuc) == "theirs":
            self._take(o, t)
            return t.h
        return o.h

    def _inner(self, b, o, t, ecuc):
        O, T = self.O.data, self.T.data
        okids = self.O.kids(o)
        tkids = self.T.kids(t)
        kb, ko, kt = child_keys(self.B.kids(b) if b is not None else (), okids, tkids)
        reorder = False                                 # build the content in their order?
        if not ecuc and b is not None:                  # (the order of ECUC values carries no meaning)
            shared = [k for k in kt if k in kb and k in ko]
            if shared:
                common = set(shared)
                was = [k for k in kb if k in common]
                ours = [k for k in ko if k in common]
                if shared != was and ours != shared:    # their side reordered, and we do not have their order
                    if o.ho == b.ho:                    # our side did not touch the element at all
                        self._take(o, t)
                        self._note("order", o, t)
                        return t.h
                    if ours == was:                     # only their side changed the order: take it
                        reorder = True
                        self._note("order", o, t)
                    else:
                        reorder = self._conflict("order", b, o, t, ecuc, (ours, shared)) == "theirs"
        new_tag = False
        if o.attrs != t.attrs:
            if b is not None and t.attrs == b.attrs:
                pass
            elif b is not None and o.attrs == b.attrs:
                new_tag = True
                self._note("attr", o, t)
            elif self._conflict("attributes", b, o, t, ecuc) == "theirs":
                new_tag = True
        attrs = t.attrs if new_tag else o.attrs
        hs = []
        inserts = []
        kept, added = set(), set()                      # which of our children stay; which of theirs come in
        trail = self.trail
        okeys = list(ko)
        pos_of = {k: i for i, k in enumerate(okeys)}
        if reorder:                                     # the children's edits are collected apart
            outer, self.edits = self.edits, []

        def anchor(last, tn):
            """Where a new child from their side goes: after the sibling that precedes it there,
            and after children only we have that must come first."""
            idx = last
            j = idx + 1
            while j < len(okeys) and okeys[j] not in kt and self._precedes(o.tag, okids[j].tag, tn.tag):
                idx = j
                j += 1
            return okids[idx].e if idx >= 0 else None

        for k, on in ko.items():                        # children their side does not have
            if k in kt:
                continue
            bn = kb.get(k)
            if bn is None:
                hs.append(on.h)                         # we added it
                kept.add(k)
                continue
            trail.push(k, on)
            if on.h == bn.h and (ecuc or on.ho == bn.ho):
                self._remove(on)                        # they deleted it, we did not touch it
                self._note("del", on, None)
            elif self._conflict("deleted-by-theirs", bn, on, None, ecuc) == "theirs":
                self._remove(on)
            else:
                hs.append(on.h)
                kept.add(k)
            trail.pop()
        last = -1
        for k, tn in kt.items():
            on = ko.get(k)
            bn = kb.get(k)
            trail.push(k, tn)
            if on is not None:
                last = pos_of[k]
                kept.add(k)
                # A named element that both sides created with different content is a coincidence a
                # person must look at. Without any common ancestor everything is "created by both", so
                # there the two versions are merged like any other element.
                if (bn is None and on.h != tn.h and self.B is not None
                        and not (k[0] == K_DEF or k[0] == K_TAG or on.tag == "AR-PACKAGE")):
                    if self._conflict("both-added", None, on, tn, ecuc) == "theirs":
                        self._take(on, tn)
                        hs.append(tn.h)
                    else:
                        hs.append(on.h)
                else:
                    keep = self._owner
                    if k[0] == K_DEF:
                        self._owner = (on, tn)
                    hs.append(self._merge(bn, on, tn, ecuc))
                    self._owner = keep
            elif bn is None:
                inserts.append((anchor(last, tn), self._theirs(_with_lead(T, tn))))     # they added it
                added.add(k)
                self._note("add", None, tn)
                hs.append(tn.h)
            elif tn.h == bn.h and (ecuc or tn.ho == bn.ho):
                pass                                    # we deleted it, they did not touch it
            elif self._conflict("deleted-by-ours", bn, None, tn, ecuc) == "theirs":
                inserts.append((anchor(last, tn), self._theirs(_with_lead(T, tn))))
                added.add(k)
                hs.append(tn.h)
            trail.pop()
        if reorder:
            inner, self.edits = self.edits, outer
            self._edit(o.cs, o.ce, self._in_their_order(o, t, okids, tkids, ko, kt, kept, added, inner))
            inserts = []
        if inserts:
            if o.cs is None or (not okids and not O[o.cs:o.ce].strip()):
                # our element has no content yet: write it out in full
                tail = self._theirs(T[_lead(T, t.ce):t.ce]) if t.ce is not None else b""
                head = self._theirs(_open_tag(T, t)) if new_tag else _open_tag(O, o)
                self._edit(o.s, o.e, head + b"".join(blob for _, blob in inserts) + tail
                           + b"</" + _RAW_NAME.match(O, o.s).group(1) + b">")
                new_tag = False
            else:
                for pos, blob in inserts:
                    p = o.cs if pos is None else pos
                    self._edit(p, p, blob)
        if new_tag:
            if o.cs is None:
                self._edit(o.s, o.e, self._theirs(_empty_tag(T, t)))
            else:
                self._edit(o.s, o.cs, self._theirs(_open_tag(T, t)))
        hs.sort()
        return hash((o.tag, attrs, tuple(hs)))

    def _in_their_order(self, o, t, okids, tkids, ko, kt, kept, added, inner):
        """The content of our element with its merged children in their order. ``inner`` holds the edits
        made inside the children. A child only we have stays behind the sibling it follows in our file.
        Each child keeps what stands before it in its own file (line break, indentation, comments)."""
        O, T = self.O.data, self.T.data
        inner = sorted(inner)

        def ours(i):
            on = okids[i]
            out = [O[okids[i - 1].e if i else o.cs:on.s]]
            pos = on.s
            for s, e, _, blob in inner:
                if on.s <= s and e <= on.e:
                    out.append(O[pos:s])
                    out.append(blob)
                    pos = e
            out.append(O[pos:on.e])
            return b"".join(out)

        okeys, tkeys = list(ko), list(kt)
        follow, head, prev = {}, [], None
        for i, k in enumerate(okeys):
            if k in kt:
                prev = k
            elif k in kept:
                (head if prev is None else follow.setdefault(prev, [])).append(i)
        at = {k: i for i, k in enumerate(okeys)}
        parts = [ours(i) for i in head]
        for j, k in enumerate(tkeys):
            if k in ko:
                parts.append(ours(at[k]))
                parts += [ours(i) for i in follow.get(k, ())]
            elif k in added:
                parts.append(self._theirs(T[tkids[j - 1].e if j else t.cs:tkids[j].e]))
        parts.append(O[okids[-1].e:o.ce])
        return b"".join(parts)


def merge_bytes(base, ours, theirs, decisions=None, masked=None):
    """Merge three versions of one file. ``base`` may be None (both sides added the file).

    ``decisions`` maps conflict keys to "ours" or "theirs". Conflicts without a
    decision keep our side in the result and are listed as open."""
    return Merge3(base, ours, theirs, decisions, masked).run()
