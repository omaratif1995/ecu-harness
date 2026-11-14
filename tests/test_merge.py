"""Merging ARXML by element: the engine, its laws, and the workflow inside git."""
import io
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from util import COM, DEM, NVM, OS, SRC, SWC, Case

from ecuharness import ToolError
from ecuharness.arxml import scan
from ecuharness.changes import write_file
from ecuharness.demo import write_demo
from ecuharness.edit import plan_ops
from ecuharness.evals import _edit_files, _git
from ecuharness.gitio import Repo
from ecuharness.gitmerge import MergeSession, driver, driver_command
from ecuharness.merge import merge_bytes
from ecuharness.project import Project
from ecuharness.xtree import change_lines, parse_tree

HAVE_GIT = shutil.which("git") is not None
HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        '<AR-PACKAGES>\n<AR-PACKAGE>\n<SHORT-NAME>P</SHORT-NAME>\n<ELEMENTS>\n'
        '<ECUC-MODULE-CONFIGURATION-VALUES>\n<SHORT-NAME>M</SHORT-NAME>\n'
        '<DEFINITION-REF DEST="ECUC-MODULE-DEF">/D/M</DEFINITION-REF>\n<CONTAINERS>\n')
TAIL = '</CONTAINERS>\n</ECUC-MODULE-CONFIGURATION-VALUES>\n</ELEMENTS>\n</AR-PACKAGE>\n</AR-PACKAGES>\n</AUTOSAR>\n'


def P(name, value, more=""):
    return ('<ECUC-NUMERICAL-PARAM-VALUE>\n<DEFINITION-REF DEST="ECUC-INTEGER-PARAM-DEF">/D/M/C/%s</DEFINITION-REF>\n'
            '<VALUE>%s</VALUE>\n%s</ECUC-NUMERICAL-PARAM-VALUE>\n' % (name, value, more))


def R(name, target):
    return ('<ECUC-REFERENCE-VALUE>\n<DEFINITION-REF DEST="ECUC-REFERENCE-DEF">/D/M/C/%s</DEFINITION-REF>\n'
            '<VALUE-REF DEST="ECUC-CONTAINER-VALUE">%s</VALUE-REF>\n</ECUC-REFERENCE-VALUE>\n' % (name, target))


def C(name, params="", refs="", subs="", head="", empty_subs=False):
    s = ('<ECUC-CONTAINER-VALUE%s>\n<SHORT-NAME>%s</SHORT-NAME>\n'
         '<DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/D/M/C</DEFINITION-REF>\n' % (head, name))
    if params:
        s += "<PARAMETER-VALUES>\n%s</PARAMETER-VALUES>\n" % params
    if refs:
        s += "<REFERENCE-VALUES>\n%s</REFERENCE-VALUES>\n" % refs
    if subs:
        s += "<SUB-CONTAINERS>\n%s</SUB-CONTAINERS>\n" % subs
    elif empty_subs:
        s += "<SUB-CONTAINERS/>\n"
    return s + "</ECUC-CONTAINER-VALUE>\n"


def doc(*conts):
    return (HEAD + "".join(conts) + TAIL).encode("utf-8")


def vals(data, path, name):
    n = scan(data).node(path)
    return sorted(e.value for e in list(n.params or ()) + list(n.refs or ()) if e.name == name)


def names(data, path):
    return sorted((scan(data).node(path).kids or {}))


class Always:
    """Decides every conflict the same way."""

    def __init__(self, side):
        self.side = side

    def get(self, key):
        return self.side


class Engine(unittest.TestCase):
    def test_changes_of_both_sides_are_kept_and_only_their_lines_change_in_our_file(self):
        base = doc(C("A", P("X", 1) + P("Y", 2)), C("B", P("X", 3)))
        ours = doc(C("A", P("X", 5) + P("Y", 2)), C("B", P("X", 3)))
        theirs = doc(C("A", P("X", 1) + P("Y", 7)), C("B", P("X", 3)), C("N", P("X", 9)))
        r = merge_bytes(base, ours, theirs)
        self.assertEqual((r.conflicts, r.count), ([], 2))
        self.assertEqual((vals(r.data, "/P/M/A", "X"), vals(r.data, "/P/M/A", "Y")), (["5"], ["7"]))
        self.assertEqual(names(r.data, "/P/M"), ["A", "B", "N"])
        self.assertEqual(r.applied, ["~ /P/M/A Y: 2 -> 7", "+ /P/M/N [C]"])
        # our file with exactly their two changes: one value line replaced, one block inserted
        a, b = ours.split(b"\n"), r.data.split(b"\n")
        self.assertEqual([x for x in a if x not in b], [b"<VALUE>2</VALUE>"])
        added = [x for x in b if x not in a]
        self.assertEqual(added[0], b"<VALUE>7</VALUE>")
        self.assertIn(b"<SHORT-NAME>N</SHORT-NAME>", added)

    def test_the_same_change_on_both_sides_is_not_a_conflict(self):
        base = doc(C("A", P("X", 1)))
        both = doc(C("A", P("X", 4)), C("N", P("X", 9), head=' UUID="1111"'))
        other = both.replace(b'UUID="1111"', b'UUID="2222"')        # created twice, identical but for the id
        r = merge_bytes(base, both, other)
        self.assertEqual((r.conflicts, r.count, r.data), ([], 0, both))

    def test_a_value_both_sides_changed_is_one_conflict_a_person_can_decide(self):
        base, ours, theirs = (doc(C("A", P("X", v) + P("Y", 2))) for v in (1, 5, 6))
        r = merge_bytes(base, ours, theirs)
        self.assertEqual(r.data, ours)                              # undecided: our side stays in the file
        c = r.conflicts[0]
        self.assertEqual((c["kind"], c["where"], c["param"], c["base"], c["ours"], c["theirs"]),
                         ("value", "/P/M/A", "X", "1", "5", "6"))
        self.assertEqual(len(r.open), 1)
        t = merge_bytes(base, ours, theirs, {c["key"]: "theirs"})
        self.assertEqual((vals(t.data, "/P/M/A", "X"), t.open), (["6"], []))
        o = merge_bytes(base, ours, theirs, {c["key"]: "ours"})
        self.assertEqual((o.data, o.open, o.conflicts[0]["decision"]), (ours, [], "ours"))

    def test_deleted_on_one_side_and_changed_on_the_other(self):
        base = doc(C("A", P("X", 1)), C("B", P("X", 2)), C("K", P("X", 3)))
        edited = doc(C("A", P("X", 1)), C("B", P("X", 8)), C("K", P("X", 3)))
        deleted = doc(C("A", P("X", 1)))                            # B and K gone
        r = merge_bytes(base, edited, deleted)                      # they deleted what we changed
        self.assertEqual(names(r.data, "/P/M"), ["A", "B"])        # K: deleted there, untouched here -> gone
        c = r.conflicts[0]
        self.assertEqual((c["kind"], c["where"], c["element"], c["theirs"]), ("deleted-by-theirs", "/P/M/B", "C", "deleted"))
        self.assertIn("X: 2 -> 8", c["ours"])
        self.assertEqual(names(merge_bytes(base, edited, deleted, {c["key"]: "theirs"}).data, "/P/M"), ["A"])
        r = merge_bytes(base, deleted, edited)                      # we deleted what they changed
        c = r.conflicts[0]
        self.assertEqual((c["kind"], c["ours"]), ("deleted-by-ours", "deleted"))
        self.assertEqual(names(r.data, "/P/M"), ["A"])
        back = merge_bytes(base, deleted, edited, {c["key"]: "theirs"})
        self.assertEqual((names(back.data, "/P/M"), vals(back.data, "/P/M/B", "X")), (["A", "B"], ["8"]))

    def test_the_same_name_added_twice_with_different_content(self):
        base = doc(C("A", P("X", 1)))
        ours = doc(C("A", P("X", 1)), C("N", P("X", 5)))
        theirs = doc(C("A", P("X", 1)), C("N", P("X", 6) + P("Y", 1)))
        r = merge_bytes(base, ours, theirs)
        c = r.conflicts[0]
        self.assertEqual((c["kind"], c["where"], len(r.conflicts)), ("both-added", "/P/M/N", 1))
        t = merge_bytes(base, ours, theirs, {c["key"]: "theirs"})
        self.assertEqual((vals(t.data, "/P/M/N", "X"), vals(t.data, "/P/M/N", "Y")), (["6"], ["1"]))

    def test_first_children_added_on_both_sides_end_up_in_one_wrapper_in_schema_order(self):
        base = doc(C("A", P("X", 1)))
        with_sub = doc(C("A", P("X", 1), subs=C("S1", P("X", 2))))
        with_ref = doc(C("A", P("X", 1), refs=R("T", "/P/M/A")))
        other_sub = doc(C("A", P("X", 1), subs=C("S2", P("X", 3))))
        for ours, theirs in ((with_sub, with_ref), (with_ref, with_sub)):
            r = merge_bytes(base, ours, theirs)
            self.assertEqual(r.conflicts, [])
            self.assertEqual((names(r.data, "/P/M/A"), vals(r.data, "/P/M/A", "T")), (["S1"], ["/P/M/A"]))
            self.assertLess(r.data.index(b"<PARAMETER-VALUES>"), r.data.index(b"<REFERENCE-VALUES>"))
            self.assertLess(r.data.index(b"</REFERENCE-VALUES>"), r.data.index(b"<SUB-CONTAINERS>"))
        r = merge_bytes(base, with_sub, other_sub)                  # both created the wrapper
        self.assertEqual((names(r.data, "/P/M/A"), r.data.count(b"<SUB-CONTAINERS>"), r.conflicts), (["S1", "S2"], 1, []))

    def test_an_empty_element_receives_content(self):
        base = doc(C("A", P("X", 1), empty_subs=True))
        theirs = doc(C("A", P("X", 1), subs=C("S", P("X", 2))))
        ours = doc(C("A", P("X", 4), empty_subs=True))
        r = merge_bytes(base, ours, theirs)
        self.assertEqual((names(r.data, "/P/M/A"), vals(r.data, "/P/M/A", "X"), r.conflicts), (["S"], ["4"], []))
        self.assertNotIn(b"<SUB-CONTAINERS/>", r.data)

    def test_a_list_parameter_merges_as_a_set(self):
        base = doc(C("A", refs=R("T", "/x/1") + R("T", "/x/2") + R("T", "/x/3")))
        ours = doc(C("A", refs=R("T", "/x/1") + R("T", "/x/2") + R("T", "/x/3") + R("T", "/x/ours")))
        theirs = doc(C("A", refs=R("T", "/x/1") + R("T", "/x/3") + R("T", "/x/theirs")))
        r = merge_bytes(base, ours, theirs)
        self.assertEqual((vals(r.data, "/P/M/A", "T"), r.conflicts), (["/x/1", "/x/3", "/x/ours", "/x/theirs"], []))
        self.assertEqual(sorted(r.applied), ["~ /P/M/A T: + /x/theirs", "~ /P/M/A T: - /x/2"])

    def test_values_of_different_variants_are_kept_apart(self):
        def variant(label):
            return "<VARIATION-POINT><SHORT-LABEL>%s</SHORT-LABEL></VARIATION-POINT>\n" % label

        def both(left, right):
            return doc(C("A", P("X", left, variant("Left")) + P("X", right, variant("Right"))))

        r = merge_bytes(both(1, 2), both(5, 2), both(1, 6))        # each side changed another variant
        self.assertEqual((vals(r.data, "/P/M/A", "X"), r.conflicts), (["5", "6"], []))
        r = merge_bytes(both(1, 2), both(5, 2), both(7, 2))        # both changed the same variant
        self.assertEqual((len(r.conflicts), r.conflicts[0]["ours"], r.conflicts[0]["theirs"]), (1, "5", "7"))

    def test_a_conflict_between_two_values_the_tool_calculated_is_marked(self):
        auto = "<IS-AUTO-VALUE>true</IS-AUTO-VALUE>\n"
        base, ours, theirs = (doc(C("A", P("X", v, auto) + P("Y", w))) for v, w in ((1, 1), (5, 2), (6, 3)))
        c = {x["param"]: x for x in merge_bytes(base, ours, theirs).conflicts}
        self.assertTrue(c["X"]["calculated"])
        self.assertNotIn("calculated", c["Y"])
        mixed = doc(C("A", P("X", 6) + P("Y", 3)))                  # their side set it by hand
        self.assertNotIn("calculated", merge_bytes(base, ours, mixed).conflicts[0])

    def test_layout_comments_and_line_endings_of_our_file_survive(self):
        base = doc(C("A", P("X", 1) + P("Y", 2)), C("B", P("X", 3)))
        ours = base.replace(b"<CONTAINERS>\n", b"<CONTAINERS>\n\t\t<!-- kept: a note of ours -->\n", 1)
        ours = ours.replace(b"<VALUE>1</VALUE>", b"<VALUE>  1  </VALUE>").replace(b"\n", b"\r\n")
        theirs = base.replace(b"<VALUE>2</VALUE>", b"<VALUE>7</VALUE>") \
                     .replace(TAIL.encode(), (C("N", P("X", 9)) + TAIL).encode())
        r = merge_bytes(base, ours, theirs)
        self.assertEqual(r.conflicts, [])
        self.assertIn(b"<!-- kept: a note of ours -->", r.data)
        self.assertIn(b"<VALUE>  1  </VALUE>", r.data)
        self.assertEqual(r.data.count(b"\n"), r.data.count(b"\r\n"))        # their LF block arrived as CRLF
        self.assertEqual(names(r.data, "/P/M"), ["A", "B", "N"])

    def test_identity_attributes_are_no_difference_but_real_attributes_are(self):
        base = doc(C("A", P("X", 1), head=' UUID="aaa"'))
        ours = doc(C("A", P("X", 4), head=' UUID="aaa"'))
        theirs = doc(C("A", P("X", 1), head=' UUID="bbb" T="2026-01-01"'))
        r = merge_bytes(base, ours, theirs)
        self.assertEqual((r.data, r.count), (ours, 0))
        theirs = base.replace(b'xmlns="http://autosar.org/schema/r4.0"',
                              b'xmlns="http://autosar.org/schema/r4.0" S="x" DEMO="new"')
        r = merge_bytes(base, ours, theirs)
        self.assertEqual((r.conflicts, r.count), ([], 1))
        self.assertIn(b'DEMO="new"', r.data)
        self.assertEqual(vals(r.data, "/P/M/A", "X"), ["4"])

    def test_values_of_masked_parameters_do_not_appear_in_reports(self):
        base, ours, theirs = (doc(C("A", P("ComPassword", v) + P("Y", w))) for v, w in ((1, 1), (5, 1), (6, 2)))
        r = merge_bytes(base, ours, theirs, masked=lambda name: "Password" in name)
        c = r.conflicts[0]
        self.assertEqual((c["base"], c["ours"], c["theirs"]), ("***", "***", "***"))
        self.assertEqual(r.applied, ["~ /P/M/A Y: 1 -> 2"])

    def test_two_files_without_a_common_ancestor(self):
        ours = doc(C("A", P("X", 1)), C("O", P("X", 2)))
        theirs = doc(C("A", P("X", 1)), C("T", P("X", 3)))
        for base in (None, b"", b"  \n"):
            r = merge_bytes(base, ours, theirs)
            self.assertEqual((names(r.data, "/P/M"), r.conflicts), (["A", "O", "T"], []))
        r = merge_bytes(None, ours, theirs.replace(b"<VALUE>1<", b"<VALUE>8<"))      # a real difference
        self.assertEqual([(c["kind"], c["where"], c["param"], c.get("base"), c["ours"], c["theirs"])
                          for c in r.conflicts], [("value", "/P/M/A", "X", None, "1", "8")])

    def test_what_cannot_be_merged_is_refused_not_guessed(self):
        good = doc(C("A", P("X", 1)))
        with self.assertRaises(ToolError):
            merge_bytes(good, good, good.replace(b"</AUTOSAR>", b""))
        with self.assertRaises(ToolError):
            merge_bytes(good, good, b"<<<<<<< HEAD\n" + good)
        with self.assertRaises(ToolError):
            merge_bytes(good, good, good.decode().encode("utf-16"))


class OtherArxml(unittest.TestCase):
    """Software components and anything else that is not ECUC values."""

    def test_ports_added_and_changed_on_both_sides(self):
        new_port = (b"            <R-PORT-PROTOTYPE>\n              <SHORT-NAME>PressureIn</SHORT-NAME>\n"
                    b"            </R-PORT-PROTOTYPE>\n")
        ours = SWC.replace(b"          </PORTS>", new_port + b"          </PORTS>")
        theirs = SWC.replace(b"/Interfaces/TempIf", b"/Interfaces/TemperatureIf") \
                    .replace(b"<IS-SERVICE>false", b"<IS-SERVICE>true")
        r = merge_bytes(SWC, ours, theirs)
        self.assertEqual(r.conflicts, [])
        self.assertEqual(sorted(r.applied), [
            "~ /Components/EngineCtrl/TempIn REQUIRED-INTERFACE-TREF: /Interfaces/TempIf -> /Interfaces/TemperatureIf",
            "~ /Components/SpeedIf IS-SERVICE: false -> true"])
        for piece in (b"PressureIn", b"/Interfaces/TemperatureIf", b"<IS-SERVICE>true"):
            self.assertIn(piece, r.data)
        both = merge_bytes(SWC, ours, SWC.replace(b"<CATEGORY>APP", b"<CATEGORY>SENSOR")
                           .replace(b"/Interfaces/TempIf", b"/Interfaces/Other"))
        self.assertEqual(both.conflicts, [])
        c = merge_bytes(SWC, ours.replace(b"/Interfaces/TempIf", b"/Interfaces/Mine"), theirs).conflicts
        self.assertEqual([(x["where"], x["field"], x["ours"], x["theirs"]) for x in c],
                         [("/Components/EngineCtrl/TempIn", "REQUIRED-INTERFACE-TREF", "/Interfaces/Mine",
                           "/Interfaces/TemperatureIf")])

    def test_a_new_order_is_taken_from_the_one_side_that_changed_it(self):
        i, j = SWC.index(b"            <P-PORT-PROTOTYPE>"), SWC.index(b"            <R-PORT-PROTOTYPE>")
        k = SWC.index(b"          </PORTS>")
        swapped = SWC[:i] + SWC[j:k] + SWC[i:j] + SWC[k:]           # the two ports change places
        r = merge_bytes(SWC, SWC.replace(b"<IS-SERVICE>false", b"<IS-SERVICE>true"), swapped)
        self.assertEqual(r.conflicts, [])
        self.assertLess(r.data.index(b"TempIn"), r.data.index(b"SpeedOut"))
        self.assertIn(b"<IS-SERVICE>true", r.data)
        # our side changed a port, their side only the order: both are kept, no question
        r = merge_bytes(SWC, SWC.replace(b"/Interfaces/TempIf", b"/Interfaces/Mine"), swapped)
        self.assertEqual(r.conflicts, [])
        self.assertLess(r.data.index(b"TempIn"), r.data.index(b"SpeedOut"))
        self.assertIn(b"/Interfaces/Mine", r.data)
        self.assertIn("~ /Components/EngineCtrl PORTS: order of the entries changed", r.applied)
        self.assertEqual(r.data, swapped.replace(b"/Interfaces/TempIf", b"/Interfaces/Mine"))
        # their side reordered, our side changed: their order with our change
        r = merge_bytes(SWC, swapped.replace(b"/Interfaces/TempIf", b"/Interfaces/Mine"), swapped)
        self.assertEqual(r.conflicts, [])

    def test_both_sides_reordered_differently_is_asked_and_never_loses_content(self):
        i, j = SWC.index(b"            <P-PORT-PROTOTYPE>"), SWC.index(b"            <R-PORT-PROTOTYPE>")
        k = SWC.index(b"          </PORTS>")
        speed, temp = SWC[i:j], SWC[j:k]
        port = lambda name: temp.replace(b"TempIn", name).replace(b"TempIf", name + b"If")   # noqa: E731
        pressure = port(b"PressureIn")
        doc = lambda *ports: SWC[:i] + b"".join(ports) + SWC[k:]                            # noqa: E731
        base = doc(speed, temp, pressure)
        ours = doc(temp.replace(b"TempIf", b"Mine"), port(b"OursNew"), speed, pressure)
        theirs = doc(pressure, speed, temp, port(b"TheirsNew")).replace(b"<IS-SERVICE>false", b"<IS-SERVICE>true")
        names = lambda data: re.findall(br"<SHORT-NAME>(\w+(?:In|Out|New))</SHORT-NAME>", data)    # noqa: E731

        r = merge_bytes(base, ours, theirs)
        self.assertEqual([(c["kind"], c["ours"], c["theirs"]) for c in r.conflicts],
                         [("order", "our order: TempIn, SpeedOut, PressureIn",
                           "their order: PressureIn, SpeedOut, TempIn")])
        self.assertEqual(names(r.data), [b"TempIn", b"TheirsNew", b"OursNew", b"SpeedOut", b"PressureIn"])
        for piece in (b"/Interfaces/Mine", b"<IS-SERVICE>true"):
            self.assertIn(piece, r.data)

        r = merge_bytes(base, ours, theirs, {r.conflicts[0]["key"]: "theirs"})
        self.assertEqual(r.open, [])
        self.assertEqual(names(r.data), [b"PressureIn", b"SpeedOut", b"TempIn", b"OursNew", b"TheirsNew"])
        for piece in (b"/Interfaces/Mine", b"<IS-SERVICE>true"):
            self.assertIn(piece, r.data)
        self.assertIn(b"</R-PORT-PROTOTYPE>\n          </PORTS>", r.data)
        self.assertNotIn(b"PROTOTYPE>            <", r.data)                # every port on its own line
        self.assertEqual(parse_tree(r.data).root.h,
                         parse_tree(doc(pressure, speed, temp.replace(b"TempIf", b"Mine"), port(b"OursNew"),
                                        port(b"TheirsNew")).replace(b"<IS-SERVICE>false", b"<IS-SERVICE>true")).root.h)

    def test_identical_siblings_get_their_own_conflict_and_decision(self):
        ref = (b"                <ECUC-REFERENCE-VALUE>\n"
               b"                  <DEFINITION-REF DEST=\"ECUC-REFERENCE-DEF\">/D/M/C/Ref</DEFINITION-REF>\n"
               b"                  <ANNOTATIONS>%s</ANNOTATIONS>\n"
               b"                  <VALUE-REF DEST=\"ECUC-CONTAINER-VALUE\">/P/M/T</VALUE-REF>\n"
               b"                </ECUC-REFERENCE-VALUE>\n")
        doc = lambda v: (b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<AUTOSAR xmlns=\"http://autosar.org/schema/r4.0\">\n"   # noqa: E731
                         b"  <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS>\n"
                         b"        <ECUC-MODULE-CONFIGURATION-VALUES><SHORT-NAME>M</SHORT-NAME><CONTAINERS>\n"
                         b"            <ECUC-CONTAINER-VALUE><SHORT-NAME>C</SHORT-NAME>\n"
                         b"              <DEFINITION-REF DEST=\"ECUC-PARAM-CONF-CONTAINER-DEF\">/D/M/C</DEFINITION-REF>\n"
                         b"              <REFERENCE-VALUES>\n" + (ref % v) * 2 +
                         b"              </REFERENCE-VALUES>\n            </ECUC-CONTAINER-VALUE>\n"
                         b"        </CONTAINERS></ECUC-MODULE-CONFIGURATION-VALUES>\n"
                         b"  </ELEMENTS></AR-PACKAGE></AR-PACKAGES>\n</AUTOSAR>\n")
        r = merge_bytes(doc(b"a"), doc(b"b"), doc(b"c"))
        keys = [c["key"] for c in r.conflicts]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        r = merge_bytes(doc(b"a"), doc(b"b"), doc(b"c"), {keys[0]: "theirs", keys[1]: "ours"})
        self.assertEqual(r.open, [])
        self.assertEqual(re.findall(br"<ANNOTATIONS>(\w)</ANNOTATIONS>", r.data), [b"c", b"b"])


V = lambda p, v: {"param": p, "value": str(v)}      # noqa: E731
FILES = ["Config/ECUC/DemoEcu_Com_ecuc.arxml", "Config/ECUC/DemoEcu_Os_ecuc.arxml",
         "Config/ECUC/DemoEcu_NvM_ecuc.arxml"]


def random_edits(rng, p, n):
    """Edit the demo project n times at random, through the harness's own edit engine."""
    m = p.model
    for _ in range(n):
        kids = list(m.resolve(COM.rstrip("/")).kids.values())
        sigs = [c for c in kids if c.defref.endswith("ComSignal")]
        ipdus = [c for c in kids if c.defref.endswith("ComIPdu")]
        s = rng.choice(sigs)
        pick = rng.random()
        if pick < 0.35:
            ops = [{"op": "set", "path": s.path(), "param": rng.choice(["ComBitSize", "ComBitPosition",
                                                                       "ComSignalInitValue"]),
                    "value": str(rng.randint(1, 8))}]
        elif pick < 0.47:
            ops = [{"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "Sig_New%d" % rng.randint(0, 4),
                    "values": [V("ComBitPosition", rng.randint(0, 3) * 8), V("ComBitSize", rng.choice([4, 8])),
                               V("ComSignalType", "UINT8")]}]
        elif pick < 0.56:
            ops = [{"op": "append", "path": rng.choice(ipdus).path(), "param": "ComIPduSignalRef", "value": s.path()}]
        elif pick < 0.64:
            ip = rng.choice(ipdus)
            refs = [e.value for e in ip.refs or () if e.name == "ComIPduSignalRef"]
            if not refs:
                continue
            ops = [{"op": "unset", "path": ip.path(), "param": "ComIPduSignalRef", "value": rng.choice(refs)}]
        elif pick < 0.71:
            ops = [{"op": "unset", "path": c.path(), "param": e.name, "value": s.path()}
                   for c, e in m.refs_to().get(s.path(), [])] + [{"op": "remove", "path": s.path()}]
        elif pick < 0.77:
            ops = [{"op": "rename", "path": s.path(), "name": s.name + "x"}]
        elif pick < 0.84:
            ops = [{"op": "add", "parent": s.path(), "kind": "ComFilter", "name": "ComFilter",
                    "values": [V("ComFilterAlgorithm", rng.choice(["ALWAYS", "NEVER"]))]}]
        elif pick < 0.92:
            ops = [{"op": "set", "path": OS + rng.choice(["Task_Bsw", "Task_10ms", "Task_100ms"]),
                    "param": rng.choice(["OsTaskPriority", "OsTaskActivation"]), "value": str(rng.randint(1, 30))}]
        else:
            ops = [{"op": "set", "path": NVM + "NvM_Block_Config", "param": "NvMNvBlockLength",
                    "value": str(rng.randint(1, 64))}]
        try:
            plan = plan_ops(m, dict(p.policy, maxRemovalsPerChange=10000), ops)
        except ToolError:
            continue                                                # an invalid random edit: skip it
        for sf, data in plan.files.items():
            write_file(sf.path, data)
            m.adopt(sf, data, plan.scans[sf])


def edited_demo(seed, n, crlf=False):
    tmp = tempfile.mkdtemp(prefix="ecuh_mrg_")
    try:
        write_demo(tmp, 16, crlf)
        p = Project(tmp)
        p.model.refresh()
        if n:
            random_edits(random.Random(seed), p, n)
        out = {}
        for f in FILES:
            with open(os.path.join(tmp, f), "rb") as fh:
                out[f] = fh.read()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Laws(unittest.TestCase):
    """Properties every merge must have, checked on random edits of both sides."""

    def content(self, data):
        return parse_tree(data).root.h

    def check(self, seed, crlf):
        base = edited_demo(0, 0, crlf)
        ours = edited_demo(seed * 2 + 1, 10, crlf)
        theirs = edited_demo(seed * 2 + 2, 10, crlf)
        merged = conflicts = 0
        for f in FILES:
            b, o, t = base[f], ours[f], theirs[f]
            r = merge_bytes(b, o, t)
            merged += r.count
            conflicts += len(r.conflicts)
            where = (seed, f)
            scan(r.data)                                            # the ECUC reader accepts the result
            only_theirs = merge_bytes(b, b, t)                      # we changed nothing: the result is theirs
            self.assertEqual((only_theirs.conflicts, self.content(only_theirs.data)), ([], self.content(t)), where)
            only_ours = merge_bytes(b, o, b)                        # they changed nothing: not one byte moves
            self.assertEqual((only_ours.data, only_ours.conflicts, only_ours.count), (o, [], 0), where)
            # deciding every conflict for one side gives the same content, whichever file is "ours"
            self.assertEqual(self.content(merge_bytes(b, o, t, Always("theirs")).data),
                             self.content(merge_bytes(b, t, o, Always("ours")).data), where)
            self.assertEqual(self.content(merge_bytes(b, o, t, Always("ours")).data),
                             self.content(merge_bytes(b, t, o, Always("theirs")).data), where)
            again = merge_bytes(b, r.data, t)                       # merging twice changes nothing more
            self.assertEqual((again.count, [c["key"] for c in again.conflicts]),
                             (0, [c["key"] for c in r.conflicts]), where)
            if not r.conflicts:                                     # nothing of ours was lost
                back = merge_bytes(b, r.data, o)
                self.assertEqual((back.count, back.conflicts), (0, []), where)
        return merged, conflicts

    def test_the_laws_hold_however_much_is_kept_in_memory(self):
        from ecuharness import xtree
        keep = xtree.PRUNE
        try:
            for prune in (0, 150):                                  # load nothing on demand; nearly everything
                xtree.PRUNE = prune
                self.check(3, crlf=False)
        finally:
            xtree.PRUNE = keep

    def test_merge_laws_hold_on_random_edits(self):
        merged = conflicts = 0
        for seed in range(8):
            m, c = self.check(seed, crlf=seed % 4 == 3)
            merged, conflicts = merged + m, conflicts + c
        self.assertGreater(merged, 40)                              # the test really exercised merges ...
        self.assertGreater(conflicts, 3)                            # ... and conflicts


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class InGit(Case):
    """Two branches from one commit that changed the same files."""

    THEIRS = [{"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComSignalInitValue", "value": "50"},
              {"op": "set", "path": COM + "Sig_DoorState", "param": "ComBitSize", "value": "2"},
              {"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "Sig_Theirs",
               "values": [V("ComBitPosition", 48), V("ComBitSize", 8), V("ComSignalType", "UINT8")]},
              {"op": "append", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
               "value": COM + "Sig_Theirs"},
              {"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "12"}]
    OURS = [{"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"},
            {"op": "set", "path": COM + "Sig_DoorState", "param": "ComBitSize", "value": "3"},
            {"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "Sig_Ours",
             "values": [V("ComBitPosition", 40), V("ComBitSize", 8), V("ComSignalType", "UINT8")]},
            {"op": "append", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef", "value": COM + "Sig_Ours"},
            {"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "11"}]
    COMF, OSF = "Config/ECUC/DemoEcu_Com_ecuc.arxml", "Config/ECUC/DemoEcu_Os_ecuc.arxml"

    def branches(self, theirs=None, ours=None, top=None):
        top = top or self.root                                      # the repository; the project may sit below it
        _git(top, "init", "-q")
        _git(top, "checkout", "-q", "-b", "main")
        _git(top, "add", "-A")
        _git(top, "commit", "-q", "-m", "base")
        _git(top, "checkout", "-q", "-b", "feature")
        _edit_files(self.root, self.THEIRS if theirs is None else theirs)
        _git(top, "commit", "-q", "-am", "feature work")
        _git(top, "checkout", "-q", "main")
        if ours is None or ours:
            _edit_files(self.root, self.OURS if ours is None else ours)
            _git(top, "commit", "-q", "-am", "main work")

    def conflicted(self, **kw):
        self.branches(**kw)
        _git(self.root, "merge", "-q", "feature", ok=(1,))
        return self.reopen()

    def status(self):
        return _git(self.root, "status", "--porcelain").stdout

    def test_conflict_markers_are_replaced_by_a_merge_by_element(self):
        self.conflicted()
        self.assertIn(b"<<<<<<< ", self.read(self.COMF))
        o = self.ok("ecu_overview")
        self.assertEqual(sorted(o["merge_conflicts"]), [self.COMF, self.OSF])
        self.assertIn("ecu_merge", o["next"])
        r = self.ok("ecu_merge")
        self.assertEqual(r["files"], [{"file": self.COMF, "merged_from_theirs": 3, "conflicts": 1, "open": 1},
                                      {"file": self.OSF, "merged_from_theirs": 0, "conflicts": 1, "open": 1}])
        self.assertEqual(r["conflicts"], [
            {"id": "c1", "file": self.COMF, "where": COM + "Sig_DoorState", "param": "ComBitSize",
             "base": "4", "ours": "3", "theirs": "2"},
            {"id": "c2", "file": self.OSF, "where": OS + "Task_10ms", "param": "OsTaskPriority",
             "base": "10", "ours": "11", "theirs": "12"}])
        # both files are whole again and hold the compatible work of both branches
        self.assertNotIn(b"<<<<<<<", self.read(self.COMF) + self.read(self.OSF))
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComBitSize"), ["7"])              # ours
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComSignalInitValue"), ["50"])     # theirs
        self.assertLessEqual({COM + "Sig_Ours", COM + "Sig_Theirs"},
                             set(self.values(COM + "IPdu_EngineData", "ComIPduSignalRef")))
        self.assertEqual(self.values(OS + "Task_10ms", "OsTaskPriority"), ["11"])               # open: ours for now
        self.assertEqual(self.ok("ecu_merge")["conflicts"], r["conflicts"])                     # asking again is harmless
        r = self.ok("ecu_merge", take_theirs=["c2"], keep_ours="c1")                            # text for a list
        self.assertNotIn("conflicts", r)
        self.assertEqual(r["errors"], 0)
        self.assertIn("user's steps", r["next"])
        self.assertEqual(self.values(OS + "Task_10ms", "OsTaskPriority"), ["12"])
        self.assertEqual(self.values(COM + "Sig_DoorState", "ComBitSize"), ["3"])
        self.assertEqual(self.ok("ecu_validate", scope="all")["errors"], 0)
        with open(os.path.join(self.root, r["report"]), encoding="utf-8") as fh:
            report = fh.read()
        for piece in ("+ /ActiveEcuC/Com/ComConfig/Sig_Theirs [ComSignal]", "| c2 |", "theirs (", "| c1 |", "ours (",
                      "Merge branch 'feature'"):
            self.assertIn(piece, report)
        self.assertIn("UU " + self.COMF, self.status())             # staging stays the engineer's step
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "merged")
        self.assertEqual(self.ok("ecu_merge")["status"], "No .arxml file is in conflict.")

    def test_unknown_ids_double_ids_and_an_open_change_are_refused(self):
        self.conflicted()
        self.ok("ecu_merge")
        self.assertIn("Open conflicts: c1, c2", self.err("ecu_merge", take_theirs=["c9"])["hint"])
        self.assertIn("both lists", self.err("ecu_merge", take_theirs=["c1"], keep_ours=["c1"])["error"])
        self.ok("ecu_merge", keep_ours=["c1", "c2"])
        self.edit({"op": "set", "path": OS + "Task_Bsw", "param": "OsTaskPriority", "value": "21"})
        self.assertIn("A change is open", self.err("ecu_merge")["error"])

    def test_their_side_cannot_be_written_into_a_file_that_changed_since(self):
        self.conflicted()
        self.ok("ecu_merge")
        self.edit({"op": "set", "path": OS + "Task_Bsw", "param": "OsTaskPriority", "value": "21"})
        self.ok("ecu_change_finish", summary="x")
        e = self.err("ecu_merge", take_theirs=["c2"])
        self.assertIn("changed after it was merged", e["error"])
        self.assertIn("keep_ours", e["hint"])
        self.assertEqual(self.values(OS + "Task_Bsw", "OsTaskPriority"), ["21"])                 # nothing was lost
        self.assertEqual(len(self.ok("ecu_merge", keep_ours=["c2"])["conflicts"]), 1)            # marking it is fine

    def test_a_file_resolved_by_hand_is_left_alone_until_asked(self):
        self.conflicted(theirs=self.THEIRS[-1:], ours=self.OURS[-1:])                # only the Os conflict
        mine = _git(self.root, "show", ":2:" + self.OSF).stdout.encode("utf-8").replace(b"<VALUE>11<", b"<VALUE>13<")
        self.write(mine, self.OSF)                                                   # the engineer's own resolution
        r = self.ok("ecu_merge")
        self.assertIn("edited after git stopped", r["files"][0]["not_merged"])
        self.assertEqual(self.read(self.OSF), mine)
        ses = MergeSession(self.root).sync(redo=True)                                # `ecuh merge --redo`
        self.assertEqual([c["id"] for _, c in ses.conflicts(only_open=True)], ["c1"])
        self.assertEqual(self.reopen() and self.values(OS + "Task_10ms", "OsTaskPriority"), ["11"])
        backup = os.path.join(Repo(self.root).gitdir, "ecuh-merge-backup", self.OSF.replace("/", "__"))
        with open(backup, "rb") as fh:
            self.assertEqual(fh.read(), mine)                                        # and still not lost

    def test_delete_against_change_of_a_whole_file_stays_with_git(self):
        self.branches(theirs=self.THEIRS[-1:], ours=[])
        _git(self.root, "rm", "-q", self.OSF)
        _git(self.root, "commit", "-q", "-m", "drop Os")
        _git(self.root, "merge", "-q", "feature", ok=(1,))
        self.set_config(model={"config": ["Config/ECUC/*.arxml"]})
        r = self.ok("ecu_merge")
        self.assertIn("one side deleted this file", r["files"][0]["not_merged"])
        self.assertIn("theirs to settle", r["next"])

    def test_a_project_in_a_subfolder_of_the_repository(self):
        top = self.root
        sub = os.path.join(top, "ecu")
        os.makedirs(sub)
        for name in os.listdir(top):
            if name != "ecu":
                shutil.move(os.path.join(top, name), os.path.join(sub, name))
        self.root = sub
        try:
            self.reopen()
            self.branches(theirs=self.THEIRS[-1:], ours=self.OURS[-1:], top=top)
            _git(top, "merge", "-q", "feature", ok=(1,))
            self.reopen()
            r = self.ok("ecu_merge")
            self.assertEqual(r["files"], [{"file": "ecu/" + self.OSF, "merged_from_theirs": 0, "conflicts": 1, "open": 1}])
            self.ok("ecu_merge", take_theirs=["c1"])
            self.assertEqual(self.values(OS + "Task_10ms", "OsTaskPriority"), ["12"])
        finally:
            self.root = top

    def test_cherry_pick_and_the_command_line(self):
        self.branches()
        _git(self.root, "cherry-pick", "feature", ok=(1,))
        self.assertEqual(Repo(self.root).operation(), "cherry-pick")
        ecuh = [sys.executable, os.path.join(os.path.dirname(SRC), "ecuh.py")]
        r = subprocess.run(ecuh + ["merge"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("c1    OPEN    /ActiveEcuC/Com/ComConfig/Sig_DoorState ComBitSize", r.stdout)
        self.assertIn("base: 10 | ours: 11 | theirs: 12", r.stdout)
        r = subprocess.run(ecuh + ["merge", "--theirs", "c1,c2"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Every conflict is decided", r.stdout)
        self.reopen()
        self.assertEqual(self.values(OS + "Task_10ms", "OsTaskPriority"), ["12"])

    def test_as_merge_driver_git_merges_by_element_itself(self):
        self.branches(theirs=self.THEIRS[:1] + self.THEIRS[2:4], ours=self.OURS[:1] + self.OURS[2:4])
        lines = Repo(self.root).install_driver(driver_command())
        self.assertIn(".gitattributes", " ".join(lines))
        self.assertTrue(Repo(self.root).driver_installed())
        self.assertEqual(Repo(self.root).install_driver(driver_command())[-1], ".gitattributes already names the driver")
        r = _git(self.root, "merge", "feature", "-m", "merge by element")            # no conflict left: git commits
        self.assertIn("merged by element, no conflict", r.stderr)
        self.reopen()
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComBitSize"), ["7"])
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComSignalInitValue"), ["50"])
        self.assertLessEqual({COM + "Sig_Ours", COM + "Sig_Theirs"},
                             set(self.values(COM + "IPdu_EngineData", "ComIPduSignalRef")))
        self.assertNotIn("UU", self.status())

    def test_the_driver_leaves_real_conflicts_for_a_decision(self):
        self.branches()
        Repo(self.root).install_driver(driver_command())
        r = _git(self.root, "merge", "feature", ok=(1,))
        self.assertIn("1 conflict(s) need a decision", r.stderr)
        self.assertIn("/ActiveEcuC/Os/Task_10ms OsTaskPriority: base 10, ours 11, theirs 12", r.stderr)
        self.assertNotIn(b"<<<<<<<", self.read(self.COMF))                           # the file stays usable
        self.reopen()
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComSignalInitValue"), ["50"])
        o = self.ok("ecu_overview")                                                  # no markers, still seen
        self.assertEqual(o["merge_conflicts"], [self.COMF, self.OSF])
        self.assertIn("Call ecu_merge", o["next"])
        r = self.ok("ecu_merge")                                                     # picks up where the driver stopped
        self.assertEqual([c["id"] for c in r["conflicts"]], ["c1", "c2"])
        self.ok("ecu_merge", take_theirs=["c1", "c2"])
        self.assertEqual(self.values(COM + "Sig_DoorState", "ComBitSize"), ["2"])
        self.assertIn("user's steps", self.ok("ecu_overview")["next"])               # only staging is left
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "merged")
        self.assertNotIn("merge_conflicts", self.ok("ecu_overview"))

    def test_a_project_opened_through_a_link_still_names_its_own_files(self):
        """Git names folders as they really are. A project opened through a junction, a mapped drive or a
        short name (GitHub's Windows machines keep temp files under one) must still see its own paths."""
        link = self.root + "_link"
        if os.name == "nt":
            if subprocess.run(["cmd", "/c", "mklink", "/J", link, self.root], capture_output=True).returncode:
                self.skipTest("cannot create a junction here")
        else:
            os.symlink(self.root, link)
        self.addCleanup(lambda: os.rmdir(link) if os.name == "nt" else os.remove(link))
        self.conflicted()
        real, self.root = self.root, link
        try:
            self.reopen()
            self.assertEqual(sorted(self.ok("ecu_overview")["merge_conflicts"]), [self.COMF, self.OSF])
            self.assertEqual([c["id"] for c in self.ok("ecu_merge")["conflicts"]], ["c1", "c2"])
        finally:
            self.root = real

    def test_a_new_git_operation_numbers_its_conflicts_from_c1(self):
        self.conflicted(theirs=self.THEIRS[-1:], ours=self.OURS[-1:])                # only the Os conflict
        self.assertEqual([c["id"] for c in self.ok("ecu_merge")["conflicts"]], ["c1"])
        self.ok("ecu_merge", take_theirs=["c1"])
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "merged")
        _git(self.root, "checkout", "-q", "-b", "again", "HEAD~1")                    # the same value, once more
        _edit_files(self.root, [{"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "9"}])
        _git(self.root, "commit", "-q", "-am", "again")
        _git(self.root, "merge", "-q", "main", ok=(1,))
        self.reopen()
        r = self.ok("ecu_merge")
        self.assertEqual([(c["id"], c["ours"], c["theirs"]) for c in r["conflicts"]], [("c1", "9", "12")])


class Driver(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ecuh_drv_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_exit_codes_and_result_file(self):
        base = doc(C("A", P("X", 1) + P("Y", 2)))
        b, o = self.put("b", base), self.put("o", base.replace(b"<VALUE>1<", b"<VALUE>5<"))
        t = self.put("t", base.replace(b"<VALUE>2<", b"<VALUE>7<"))
        err = io.StringIO()
        self.assertEqual(driver(b, o, t, "x.arxml", err=err), 0)
        with open(o, "rb") as fh:
            self.assertEqual((vals(fh.read(), "/P/M/A", "X"), "no conflict" in err.getvalue()), (["5"], True))
        t = self.put("t", base.replace(b"<VALUE>1<", b"<VALUE>6<"))
        err = io.StringIO()
        self.assertEqual(driver(b, o, t, "x.arxml", err=err), 1)
        self.assertIn("X: base 1, ours 5, theirs 6", err.getvalue())

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_a_file_that_is_not_xml_falls_back_to_gits_own_merge(self):
        b, o = self.put("b", b"a\nb\nc\nd\ne\n"), self.put("o", b"A\nb\nc\nd\ne\n")
        t = self.put("t", b"a\nb\nc\nd\nE\n")
        err = io.StringIO()
        self.assertEqual(driver(b, o, t, "notes.arxml", err=err), 0)
        with open(o, "rb") as fh:
            self.assertEqual(fh.read().replace(b"\r\n", b"\n"), b"A\nb\nc\nd\nE\n")
        self.assertIn("merged as text", err.getvalue())


class Readable(unittest.TestCase):
    def test_a_difference_is_told_in_the_words_of_the_change_reports(self):
        a = doc(C("A", P("X", 1), refs=R("T", "/x/1"), subs=C("S", P("X", 2))), C("B", P("X", 3)))
        b = doc(C("A", P("X", 4), refs=R("T", "/x/2")), C("N", P("X", 9)))
        self.assertEqual(change_lines(parse_tree(a), parse_tree(b)), [
            "- /P/M/B [C]", "- /P/M/A/S [C]", "~ /P/M/A X: 1 -> 4", "~ /P/M/A T: /x/1 -> /x/2", "+ /P/M/N [C]"])


if __name__ == "__main__":
    unittest.main()
