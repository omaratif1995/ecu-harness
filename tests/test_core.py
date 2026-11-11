"""Reader and edit engine: are edits byte-exact, complete and provable?"""
import os
import re
import unittest
import xml.parsers.expat as expat

from util import COM, COM_FILE, DEM, NVM, OS, PDU, Case, inserted_block

from ecuharness.arxml import Cont, scan, tag_end
from ecuharness.check import check_model

MINI = b"""<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>ActiveEcuC</SHORT-NAME>
      <ELEMENTS>
        <ECUC-MODULE-CONFIGURATION-VALUES>
          <SHORT-NAME>Com</SHORT-NAME>
          <DEFINITION-REF DEST="ECUC-MODULE-DEF">/DEMO/Com</DEFINITION-REF>
          <CONTAINERS>
            <ECUC-CONTAINER-VALUE>
              <SHORT-NAME>ComConfig</SHORT-NAME>
              <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/DEMO/Com/ComConfig</DEFINITION-REF>
              <SUB-CONTAINERS>
                <ECUC-CONTAINER-VALUE>
                  <SHORT-NAME>Bare</SHORT-NAME>
                  <!-- a comment that must survive -->
                  <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/DEMO/Com/ComConfig/ComSignal</DEFINITION-REF>
                  <SUB-CONTAINERS/>
                </ECUC-CONTAINER-VALUE>
              </SUB-CONTAINERS>
            </ECUC-CONTAINER-VALUE>
          </CONTAINERS>
        </ECUC-MODULE-CONFIGURATION-VALUES>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


EDGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>ActiveEcuC</SHORT-NAME>
      <ELEMENTS>
        <ECUC-MODULE-CONFIGURATION-VALUES UUID="a>b">
          <SHORT-NAME>Mod</SHORT-NAME>
          <ADMIN-DATA><SDGS><SDG GID="DV:X"><SD GID="k">v</SD></SDG></SDGS></ADMIN-DATA>
          <DEFINITION-REF DEST="ECUC-MODULE-DEF">/V/Mod</DEFINITION-REF>
          <IMPLEMENTATION-CONFIG-VARIANT>VARIANT-PRE-COMPILE</IMPLEMENTATION-CONFIG-VARIANT>
          <CONTAINERS>
            <ECUC-CONTAINER-VALUE UUID='q"1>2'>
              <SHORT-NAME>A</SHORT-NAME>
              <LONG-NAME><L-4 L="EN">long <E>name</E></L-4></LONG-NAME>
              <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/V/Mod/A</DEFINITION-REF>
              <PARAMETER-VALUES>
                <ECUC-TEXTUAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/V/Mod/A/S1</DEFINITION-REF>
                  <ANNOTATIONS><ANNOTATION><ANNOTATION-ORIGIN>x</ANNOTATION-ORIGIN></ANNOTATION></ANNOTATIONS>
                  <VALUE>a &amp; b &lt;c&gt; &#65;&#x42;</VALUE>
                </ECUC-TEXTUAL-PARAM-VALUE>
                <ECUC-TEXTUAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/V/Mod/A/S2</DEFINITION-REF>
                  <VALUE>before<!-- note -->after</VALUE>
                </ECUC-TEXTUAL-PARAM-VALUE>
                <ECUC-TEXTUAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/V/Mod/A/S3</DEFINITION-REF>
                  <VALUE><![CDATA[x < y && z]]></VALUE>
                </ECUC-TEXTUAL-PARAM-VALUE>
                <ECUC-TEXTUAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/V/Mod/A/S4</DEFINITION-REF>
                  <VALUE>ends with /></VALUE>
                </ECUC-TEXTUAL-PARAM-VALUE>
                <ECUC-TEXTUAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/V/Mod/A/S5</DEFINITION-REF>
                  <VALUE/>
                </ECUC-TEXTUAL-PARAM-VALUE>
                <ECUC-NUMERICAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-INTEGER-PARAM-DEF">/V/Mod/A/N1</DEFINITION-REF>
                  <VALUE>   </VALUE>
                </ECUC-NUMERICAL-PARAM-VALUE>
                <ECUC-NUMERICAL-PARAM-VALUE>
                  <DEFINITION-REF DEST="ECUC-INTEGER-PARAM-DEF">/V/Mod/A/N2</DEFINITION-REF>
                </ECUC-NUMERICAL-PARAM-VALUE>
                <ECUC-TEXTUAL-PARAM-VALUE><DEFINITION-REF DEST="ECUC-MULTILINE-STRING-PARAM-DEF">/V/Mod/A/M</DEFINITION-REF><VALUE>line1
line2</VALUE></ECUC-TEXTUAL-PARAM-VALUE>
              </PARAMETER-VALUES>
              <REFERENCE-VALUES>
                <ECUC-REFERENCE-VALUE>
                  <DEFINITION-REF DEST="ECUC-REFERENCE-DEF">/V/Mod/A/R1</DEFINITION-REF>
                  <VALUE-REF DEST="ECUC-CONTAINER-VALUE">/ActiveEcuC/Mod/B</VALUE-REF>
                </ECUC-REFERENCE-VALUE>
                <ECUC-REFERENCE-VALUE>
                  <DEFINITION-REF DEST="ECUC-FOREIGN-REFERENCE-DEF">/V/Mod/A/R2</DEFINITION-REF>
                  <VALUE-REF DEST="I-SIGNAL"/>
                </ECUC-REFERENCE-VALUE>
                <ECUC-INSTANCE-REFERENCE-VALUE>
                  <DEFINITION-REF DEST="ECUC-INSTANCE-REFERENCE-DEF">/V/Mod/A/I1</DEFINITION-REF>
                  <VALUE-IREF>
                    <CONTEXT-ELEMENT-REF DEST="ROOT-SW-COMPOSITION-PROTOTYPE">/Sys/Root</CONTEXT-ELEMENT-REF>
                    <CONTEXT-ELEMENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Sys/Comp/Proto</CONTEXT-ELEMENT-REF>
                    <TARGET-REF DEST="P-PORT-PROTOTYPE">/Types/Swc/Port</TARGET-REF>
                  </VALUE-IREF>
                </ECUC-INSTANCE-REFERENCE-VALUE>
              </REFERENCE-VALUES>
              <SUB-CONTAINERS>
                <ECUC-CONTAINER-VALUE><SHORT-NAME>Inner</SHORT-NAME><DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/V/Mod/A/Inner</DEFINITION-REF><PARAMETER-VALUES/><SUB-CONTAINERS/></ECUC-CONTAINER-VALUE>
              </SUB-CONTAINERS>
              <VARIATION-POINT><SHORT-LABEL>vp</SHORT-LABEL></VARIATION-POINT>
            </ECUC-CONTAINER-VALUE>
            <ECUC-CONTAINER-VALUE>
              <SHORT-NAME>B</SHORT-NAME>
              <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/V/Mod/B</DEFINITION-REF>
            </ECUC-CONTAINER-VALUE>
            <ECUC-CONTAINER-VALUE>
              <SHORT-NAME>B</SHORT-NAME>
              <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/V/Mod/B</DEFINITION-REF>
            </ECUC-CONTAINER-VALUE>
          </CONTAINERS>
        </ECUC-MODULE-CONFIGURATION-VALUES>
        <SYSTEM><SHORT-NAME>Sys</SHORT-NAME><CATEGORY>X</CATEGORY></SYSTEM>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def dump(sc):
    """Everything a scan knows, in a form two scans can be compared by."""
    rows = []
    for n in sc.root.walk():
        row = [n.path(), n.tag, n.s, n.e]
        if isinstance(n, Cont):
            row += [n.defref, n.def_dest, n.sn, n.hdr_end, n.wp, n.wr, n.ws, n.uuid, n.params is None,
                    n.refs is None]
            for e in list(n.params or []) + list(n.refs or []):
                row.append((e.tag, e.defref, e.name, e.def_dest, e.value, e.value_dest, e.contexts,
                            e.s, e.e, e.vs, e.ve, e.auto))
        rows.append(row)
    return rows, sc.problems, sc.uses_uuid


class TwoReaders(Case):
    """The fast streaming reader and the simple capture reader are separate
    implementations. They must agree on every name, value and byte offset."""

    def same(self, data):
        fast, slow = dump(scan(data)), dump(scan(data, generic=True))
        self.assertEqual(fast, slow)
        return fast

    def test_awkward_xml(self):
        rows, problems, _ = self.same(EDGE)
        a = scan(EDGE).node("/ActiveEcuC/Mod/A")
        vals = {e.name: e.value for e in a.params}
        self.assertEqual(vals, {"S1": "a & b <c> AB", "S2": "beforeafter", "S3": "x < y && z", "S4": "ends with />",
                                "S5": "", "N1": "", "N2": None, "M": "line1\nline2"})
        self.assertEqual([(e.name, e.value, e.value_dest) for e in a.refs],
                         [("R1", "/ActiveEcuC/Mod/B", "ECUC-CONTAINER-VALUE"), ("R2", "", "I-SIGNAL"),
                          ("I1", "/Types/Swc/Port", "P-PORT-PROTOTYPE")])
        self.assertEqual(a.refs[2].contexts, ["/Sys/Root", "/Sys/Comp/Proto"])
        self.assertEqual(problems, ["duplicate SHORT-NAME 'B' in Mod"])
        for e in a.params:
            if e.vs is not None:
                self.assertTrue(EDGE[e.vs:e.ve].startswith(b"<VALUE"), e.name)
                self.assertTrue(EDGE[e.vs:e.ve].endswith(b">"), e.name)
        inner = a.kids["Inner"]
        self.assertEqual((inner.wp[1], inner.ws[1], inner.params), (None, None, []))
        self.assertEqual(EDGE[a.s:a.e].count(b"<ECUC-CONTAINER-VALUE"), 2)
        self.assertIsNotNone(scan(EDGE).node("/ActiveEcuC/Sys"))

    def test_other_layouts(self):
        self.same(EDGE.replace(b"\n", b"\r\n"))
        self.same(re.sub(br">\s+<", b"><", EDGE))
        self.same(MINI)
        prefixed = re.sub(br"<(/?)([A-Z])", br"<\1AR:\2", EDGE).replace(b"xmlns=", b"xmlns:AR=")
        rows = self.same(prefixed)[0]
        self.assertIn("/ActiveEcuC/Mod/A/Inner", [r[0] for r in rows])

    def test_demo_files(self):
        for sf in self.p.model.files.values():
            with open(sf.path, "rb") as fh:
                data = fh.read()
            self.same(data)
            self.same(re.sub(br">\s+<", b"><", data))


class Reader(unittest.TestCase):
    def test_tag_end_ignores_gt_inside_attributes(self):
        data = b'<A x="1>2" y=\'>\'><B/></A>'
        self.assertEqual(data[:tag_end(data, 0)], b'<A x="1>2" y=\'>\'>')

    def test_spans_point_at_the_elements(self):
        sc = scan(MINI)
        bare = sc.node("/ActiveEcuC/Com/ComConfig/Bare")
        self.assertTrue(MINI[bare.s:bare.e].startswith(b"<ECUC-CONTAINER-VALUE>"))
        self.assertTrue(MINI[bare.s:bare.e].endswith(b"</ECUC-CONTAINER-VALUE>"))
        self.assertEqual(MINI[tag_end(MINI, bare.sn[0]):bare.sn[1]], b"Bare")
        self.assertIsNone(bare.ws[1])            # <SUB-CONTAINERS/> has no inner part
        self.assertIsNone(bare.wp)

    def test_malformed_xml_is_reported_not_guessed(self):
        with self.assertRaises(expat.ExpatError):
            scan(MINI.replace(b"</AUTOSAR>", b""))


class Definitions(Case):
    def test_definitions_are_parsed(self):
        d = self.p.model.defs["/DEMO/Com/ComConfig/ComSignal/ComBitSize"]
        self.assertEqual((d.kind, d.min, d.max, d.lower, d.upper), ("integer", 1, 64, 1, 1))
        ref = self.p.model.defs["/DEMO/Com/ComConfig/ComIPdu/ComIPduSignalRef"]
        self.assertEqual((ref.is_ref, ref.upper, ref.dests), (True, None, ["/DEMO/Com/ComConfig/ComSignal"]))
        self.assertEqual(check_model(self.p.model, self.p.cfg), [])


class ByteExact(Case):
    def test_set_changes_exactly_one_line(self):
        old = self.read()
        self.begin()
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitPosition", "value": "40"})
        new = self.read()
        a, b = old.splitlines(), new.splitlines()
        self.assertEqual(len(a), len(b))
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0][1].strip(), b"<VALUE>40</VALUE>")

    def test_add_inserts_one_block_and_nothing_else(self):
        old = self.read()
        self.begin()
        self.edit({"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "Sig_X",
                   "values": [{"param": "ComBitPosition", "value": "0"}, {"param": "ComBitSize", "value": "8"},
                              {"param": "ComSignalType", "value": "UINT8"}]})
        block = inserted_block(old, self.read())
        self.assertIsNotNone(block)
        self.assertIn(b"<SHORT-NAME>Sig_X</SHORT-NAME>", block)
        self.assertIn(b'UUID="', block)                       # the file uses UUIDs, so new ones get one
        first = block.splitlines()[0]
        self.assertEqual(len(first) - len(first.lstrip()), 16)   # same indentation as its siblings

    def test_unset_removes_the_entry_and_empty_wrappers(self):
        old = self.read()
        self.begin()
        self.edit({"op": "unset", "path": COM + "Sig_EngineSpeed", "param": "ComTimeout"})
        new = self.read()
        self.assertEqual(len(old.splitlines()) - len(new.splitlines()), 4)
        self.assertIsNotNone(inserted_block(new, old))
        self.edit({"op": "unset", "path": COM + "Sig_ThrottlePos/ComFilter", "param": "ComFilterAlgorithm"})
        n = self.p.model.node(COM + "Sig_ThrottlePos/ComFilter")
        self.assertIsNone(n.wp)                               # the empty PARAMETER-VALUES wrapper went too
        self.assertNotIn(b"<PARAMETER-VALUES>\n", self.read().split(b"<SHORT-NAME>ComFilter</SHORT-NAME>")[1][:200])


class Crlf(Case):
    crlf = True

    def test_line_endings_are_kept(self):
        self.begin()
        self.edit({"op": "clone", "path": COM + "Sig_ThrottlePos", "name": "Sig_T2",
                   "values": [{"param": "ComBitPosition", "value": "40"}]},
                  {"op": "unset", "path": COM + "Sig_EngineSpeed", "param": "ComTimeout"})
        data = self.read()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertIsNotNone(self.p.model.node(COM + "Sig_T2/ComFilter"))


class Shapes(Case):
    def _write(self, data, rel=COM_FILE):
        with open(os.path.join(self.root, rel), "wb") as fh:
            fh.write(data)
        self.reopen()

    def test_missing_and_empty_wrappers_are_created(self):
        self._write(MINI)
        self.begin()
        r = self.edit({"op": "set", "path": COM + "Bare", "param": "ComBitSize", "value": "8"},
                      {"op": "add", "parent": COM + "Bare", "kind": "ComFilter", "name": "F",
                       "values": [{"param": "ComFilterAlgorithm", "value": "NEVER"}]})
        self.assertTrue(r["written"])
        data = self.read()
        self.assertIn(b"<!-- a comment that must survive -->", data)
        self.assertNotIn(b"<SUB-CONTAINERS/>", data)
        body = data.split(b"<SHORT-NAME>Bare</SHORT-NAME>")[1]
        self.assertLess(body.index(b"</DEFINITION-REF>"), body.index(b"<PARAMETER-VALUES>"))
        self.assertLess(body.index(b"</PARAMETER-VALUES>"), body.index(b"<SUB-CONTAINERS>"))
        self.assertEqual(self.values(COM + "Bare/F", "ComFilterAlgorithm"), ["NEVER"])

    def test_single_line_xml_is_edited_correctly(self):
        self._write(re.sub(br">\s+<", b"><", self.read()))
        self.begin()
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"},
                  {"op": "unset", "path": COM + "Sig_EngineSpeed", "param": "ComTimeout"},
                  {"op": "clone", "path": COM + "Sig_ThrottlePos", "name": "Sig_T2",
                   "values": [{"param": "ComBitPosition", "value": "40"}]},
                  {"op": "remove", "path": COM + "Sig_LightState"},
                  {"op": "unset", "path": COM + "IPdu_BodyStatus", "param": "ComIPduSignalRef",
                   "value": COM + "Sig_LightState"})
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComBitSize"), ["7"])
        self.assertIsNone(self.p.model.node(COM + "Sig_LightState"))
        self.assertEqual(self.read().count(b"\n"), 1)         # still one line plus the XML declaration
        scan(self.read())

    def test_special_characters_are_escaped(self):
        self.begin()
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComSignalInitValue", "value": "a<b&c>d"})
        self.assertIn(b"<VALUE>a&lt;b&amp;c&gt;d</VALUE>", self.read())
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComSignalInitValue"), ["a<b&c>d"])


class Semantics(Case):
    def test_type_and_range_errors_write_nothing(self):
        old = self.read()
        self.begin()
        for op, needle in (
                ({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "65"}, "out of range"),
                ({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "wide"}, "integer"),
                ({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComSignalType", "value": "FLOAT"}, "Allowed"),
                ({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSiz", "value": "8"}, "ComBitSize"),
                ({"op": "set", "path": COM + "Sig_Nope", "param": "ComBitSize", "value": "8"}, "Did you mean"),
                ({"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "9bad"}, "short name"),
                ({"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "Ok1"}, "Missing required"),
                ({"op": "add", "parent": COM.rstrip("/"), "kind": "OsTask", "name": "T"}, "Allowed here"),
                ({"op": "set", "path": OS + "Alarm_10ms", "param": "OsAlarmTaskRef", "value": COM + "Sig_EngineTemp"},
                 "Valid targets"),
        ):
            r = self.err("ecu_edit", ops=[op])
            self.assertIn(needle, r["error"] + " " + r.get("hint", ""), op)
        self.assertEqual(self.read(), old)

    def test_batch_is_all_or_nothing(self):
        old = self.read()
        self.begin()
        r = self.err("ecu_edit", ops=[
            {"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"},
            {"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "999"}])
        self.assertTrue(r["error"].startswith("op 1"))
        self.assertEqual(self.read(), old)

    def test_values_are_coerced_from_sloppy_input(self):
        self.begin()
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": 7},
                  {"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComSignalType", "value": "uint16"},
                  {"op": "set", "path": NVM + "NvM_Block_Config", "param": "NvMBlockUseCrc", "value": True},
                  {"op": "set", "path": NVM + "NvM_Block_Config", "param": "NvMNvBlockLength", "value": "0x20"},
                  {"op": "set", "path": COM + "Sig_EngineSpeed", "param": "ComTimeout", "value": "2"})
        self.assertEqual(self.values(COM + "Sig_EngineTemp", "ComSignalType"), ["UINT16"])
        self.assertEqual(self.values(NVM + "NvM_Block_Config", "NvMBlockUseCrc"), ["true"])
        self.assertEqual(self.values(NVM + "NvM_Block_Config", "NvMNvBlockLength"), ["32"])
        self.assertEqual(self.values(COM + "Sig_EngineSpeed", "ComTimeout"), ["2.0"])

    def test_rename_updates_every_reference(self):
        self.begin()
        r = self.edit({"op": "rename", "path": PDU + "Pdu_EngineData", "name": "Pdu_Engine"},
                      {"op": "rename", "path": COM + "Sig_EngineSpeed", "name": "Sig_Rpm"})
        self.assertEqual(self.values(COM + "IPdu_EngineData", "ComPduIdRef"), [PDU + "Pdu_Engine"])
        self.assertIn(COM + "Sig_Rpm", self.values(COM + "IPdu_EngineData", "ComIPduSignalRef"))
        self.assertEqual(len(r["changes"]), 4)
        self.assertEqual(check_model(self.p.model, self.p.cfg), [])

    def test_remove_is_refused_while_referenced(self):
        self.begin()
        r = self.err("ecu_edit", ops=[{"op": "remove", "path": OS + "Task_10ms"}])
        self.assertIn("Alarm_10ms.OsAlarmTaskRef", r["error"])
        self.edit({"op": "set", "path": OS + "Alarm_10ms", "param": "OsAlarmTaskRef", "value": OS + "Task_Bsw"},
                  {"op": "remove", "path": OS + "Task_10ms"})
        self.assertIsNone(self.p.model.node(OS + "Task_10ms"))

    def test_lists_are_protected_from_accidental_replacement(self):
        self.begin()
        r = self.err("ecu_edit", ops=[{"op": "set", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
                                        "value": COM + "Sig_DoorState"}])
        self.assertIn("append", r["hint"])
        self.edit({"op": "append", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
                   "value": "Com/ComConfig/Sig_DoorState"},
                  {"op": "unset", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
                   "value": COM + "Sig_EngineTemp"})
        self.assertEqual(self.values(COM + "IPdu_EngineData", "ComIPduSignalRef"),
                         [COM + "Sig_EngineSpeed", COM + "Sig_ThrottlePos", COM + "Sig_DoorState"])

    def test_clone_copies_children_and_takes_overrides(self):
        self.begin()
        self.edit({"op": "clone", "path": COM + "Sig_ThrottlePos", "name": "Sig_T2",
                   "values": [{"param": "ComBitPosition", "value": "48"}]})
        self.assertEqual(self.values(COM + "Sig_T2", "ComBitPosition"), ["48"])
        self.assertEqual(self.values(COM + "Sig_T2/ComFilter", "ComFilterAlgorithm"), ["ALWAYS"])
        self.assertEqual(self.values(COM + "Sig_ThrottlePos", "ComBitPosition"), ["24"])

    def test_ops_in_one_call_can_build_on_each_other(self):
        self.begin()
        self.edit({"op": "add", "parent": DEM.rstrip("/"), "kind": "DemDTC", "name": "DTC_New",
                   "values": [{"param": "DemDtcValue", "value": "4660"}]},
                  {"op": "rename", "path": DEM + "DTC_New", "name": "DTC_Renamed"},
                  {"op": "add", "parent": DEM.rstrip("/"), "kind": "DemEventParameter", "name": "Event_New",
                   "values": [{"param": "DemEventKind", "value": "DEM_EVENT_KIND_SWC"},
                              {"param": "DemDTCRef", "value": DEM + "DTC_Renamed"}]})
        self.assertEqual(self.values(DEM + "Event_New", "DemDTCRef"), [DEM + "DTC_Renamed"])


class RealWorldDetails(Case):
    """Things real tool files contain that the demo does not."""

    def setUp(self):
        Case.setUp(self)
        self.set_config(model={"definitions": []})
        with open(os.path.join(self.root, COM_FILE), "wb") as fh:
            fh.write(EDGE)
        self.reopen()

    def test_a_value_is_added_to_an_entry_that_had_none_and_the_rest_is_kept(self):
        a = "/ActiveEcuC/Mod/A"
        r = self.edit({"op": "set", "path": a, "param": "N2", "value": "5"},
                      {"op": "set", "path": a, "param": "S1", "value": "changed"})
        self.assertTrue(r["written"])
        self.assertEqual(self.values(a, "N2"), ["5"])
        data = self.read()
        entry = data.split(b"/V/Mod/A/N2</DEFINITION-REF>")[1].split(b"</ECUC-NUMERICAL-PARAM-VALUE>")[0]
        self.assertEqual(entry.strip(), b"<VALUE>5</VALUE>")
        s1 = data.split(b"/V/Mod/A/S1</DEFINITION-REF>")[1].split(b"</ECUC-TEXTUAL-PARAM-VALUE>")[0]
        self.assertIn(b"<ANNOTATION-ORIGIN>x</ANNOTATION-ORIGIN>", s1)       # the annotation survived
        self.assertIn(b"<VALUE>changed</VALUE>", s1)
        self.assertIn(b"<VARIATION-POINT><SHORT-LABEL>vp</SHORT-LABEL></VARIATION-POINT>", data)
        self.assertIn(b"<ADMIN-DATA><SDGS>", data)

    def test_a_value_the_tool_calculates_itself_is_changed_with_a_warning(self):
        data = EDGE.replace(b"<VALUE>   </VALUE>", b"<IS-AUTO-VALUE>true</IS-AUTO-VALUE>\n"
                                                   b"                  <VALUE>3</VALUE>")
        with open(os.path.join(self.root, COM_FILE), "wb") as fh:
            fh.write(data)
        self.reopen()
        r = self.edit({"op": "set", "path": "/ActiveEcuC/Mod/A", "param": "N1", "value": "4"})
        self.assertIn("IS-AUTO-VALUE", r["notes"][0])
        self.assertIn(b"<IS-AUTO-VALUE>true</IS-AUTO-VALUE>", self.read())
        self.assertEqual(self.values("/ActiveEcuC/Mod/A", "N1"), ["4"])

    def test_instance_references_are_read_but_never_written(self):
        g = self.ok("ecu_get", path="/ActiveEcuC/Mod/A")
        self.assertEqual(g["refs"]["I1"], "/Types/Swc/Port")
        r = self.err("ecu_edit", goal="Try to change an instance reference",
                     ops=[{"op": "set", "path": "/ActiveEcuC/Mod/A", "param": "I1", "value": "/Types/Swc/Other"}])
        self.assertIn("not write", r["error"])


class Untyped(Case):
    """No definition files: the common case when the BSW package is elsewhere."""

    def setUp(self):
        Case.setUp(self)
        self.set_config(model={"definitions": []})

    def test_existing_values_and_clone_work_without_definitions(self):
        self.assertEqual(self.p.model.defs, {})
        self.begin()
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"},
                  {"op": "clone", "path": COM + "Sig_EngineTemp", "name": "Sig_Copy",
                   "values": [{"param": "ComBitPosition", "value": "48"}]},
                  {"op": "append", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
                   "value": COM + "Sig_Copy"})
        self.assertEqual(self.values(COM + "Sig_Copy", "ComBitPosition"), ["48"])
        r = self.err("ecu_edit", ops=[{"op": "add", "parent": COM.rstrip("/"), "kind": "ComSignal", "name": "S"}])
        self.assertIn("clone", r["hint"])
        r = self.err("ecu_edit", ops=[{"op": "set", "path": COM + "Sig_Copy", "param": "ComNew", "value": "1"}])
        self.assertIn("no definition", r["error"])


class Scale(Case):
    signals = 4000

    def test_a_larger_model_stays_usable(self):
        import time
        size = len(self.read())
        t0 = time.time()
        self.reopen()
        load = time.time() - t0
        self.begin()
        t0 = time.time()
        self.edit({"op": "set", "path": COM + "Sig_Bulk_0100_3", "param": "ComBitSize", "value": "4"})
        one = time.time() - t0
        self.assertEqual(self.ok("ecu_find", query="Bulk_0100", kind="ComSignal")["total"], 8)
        print("\n  scale: %.1f MB Com file, %d containers: load %.2fs, one verified edit %.2fs"
              % (size / 1e6, self.p.model.count_containers(self.p.model.module_named("Com")[0]), load, one))
        self.assertLess(load, 30)
        self.assertLess(one, 30)


if __name__ == "__main__":
    unittest.main()
