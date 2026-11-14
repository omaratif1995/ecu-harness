"""Customer input files, approvals, the checks ledger, the audit record, undo, versions."""
import json
import os
import shutil
import subprocess
import sys
import unittest

from util import COM, COM_FILE, NVM, OS, PDU, SRC, SWC, Case, fake_tool

from ecuharness import ToolError, hostsetup
from ecuharness.changes import Changes
from ecuharness.compare import dbc_to_xml, load, rows_of, summarize
from ecuharness.evals import _git
from ecuharness.gitio import Repo
from ecuharness.tools import available, call_tool

HAVE_GIT = shutil.which("git") is not None
SET_PRIO = {"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "15"}
SET_LEN = {"op": "set", "path": PDU + "Pdu_BodyStatus", "param": "PduLength", "value": "6"}

DBC_OLD = b"""VERSION ""
BU_: Ecu Gateway
BO_ 256 EngineData: 8 Ecu
 SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|16383.75] "rpm" Gateway
 SG_ Gear M : 16|4@1+ (1,0) [0|15] "" Gateway,Tester
 SG_ GearDetail m2 : 20|4@0- (1,0) [-8|7] "" Gateway
BO_ 2147484000 Diagnostic: 8 Vector__XXX
 SG_ Raw : 0|8@1+ (1,0) [0|255] "" Vector__XXX
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_ "GenMsgCycleTime" BO_ 256 10;
BA_ "GenSigStartValue" SG_ 256 EngineSpeed 0;
VAL_ 256 Gear 1 "first" 0 "neutral" ;
"""
DBC_NEW = DBC_OLD.replace(b"EngineSpeed : 0|16@1+ (0.25,0)", b"EngineSpeed : 0|16@1+ (0.5,0)") \
                 .replace(b'BA_ "GenMsgCycleTime" BO_ 256 10;', b'BA_ "GenMsgCycleTime" BO_ 256 20.0;') \
                 .replace(b"BO_ 2147484000 Diagnostic:", b"BO_ 2147484000 DiagRequest:") \
                 .replace(b'1 "first" 0 "neutral"', b'0 "neutral" 1 "first" 2 "second"') \
                 .replace(b' SG_ GearDetail m2 : 20|4@0- (1,0) [-8|7] "" Gateway\n', b"")


class Dbc(unittest.TestCase):
    def test_a_can_database_is_read_with_its_properties(self):
        xml = dbc_to_xml(DBC_OLD).decode("utf-8")
        for piece in ("<SHORT-NAME>EngineData</SHORT-NAME>", "<ID>0x100</ID>", "<FRAME>standard</FRAME>",
                      "<DLC>8</DLC>", "<SENDER>Ecu</SENDER>", "<GenMsgCycleTime>10</GenMsgCycleTime>",
                      "<FACTOR>0.25</FACTOR>", "<BYTE-ORDER>little-endian</BYTE-ORDER>",
                      "<MULTIPLEX>selector</MULTIPLEX>", "<MULTIPLEX>selected by 2</MULTIPLEX>",
                      "<SIGNED>true</SIGNED>", "<BYTE-ORDER>big-endian</BYTE-ORDER>",
                      "<RECEIVERS>Gateway,Tester</RECEIVERS>", "<VALUES>0=neutral 1=first</VALUES>",
                      "<ID>0x160</ID>", "<FRAME>extended</FRAME>", "<GenSigStartValue>0</GenSigStartValue>"):
            self.assertIn(piece, xml)
        self.assertNotIn("Vector__XXX", xml)

    def test_two_deliveries_are_compared_by_content(self):
        rows = rows_of(load(DBC_OLD, "a.dbc"), load(DBC_NEW, "b.dbc"))
        by = {r["path"]: r for r in rows}
        self.assertEqual(by["/EngineData"]["what"], ["GenMsgCycleTime: 10 -> 20"])       # 20.0 is 20
        self.assertEqual(by["/EngineData/EngineSpeed"]["what"], ["FACTOR: 0.25 -> 0.5"])
        self.assertEqual(by["/EngineData/Gear"]["what"], ["VALUES: 0=neutral 1=first -> 0=neutral 1=first 2=second"])
        self.assertEqual((by["/EngineData/GearDetail"]["change"], by["/EngineData/GearDetail"]["kind"]),
                         ("removed", "SIGNAL"))
        self.assertEqual((by["/DiagRequest"]["change"], by["/DiagRequest"]["from"]), ("renamed", "Diagnostic"))
        self.assertNotIn("/Diagnostic", by)
        self.assertEqual(summarize(rows), {"MESSAGE": "1 changed, 1 renamed", "SIGNAL": "2 changed, 1 removed"})
        self.assertEqual(rows_of(load(DBC_OLD, "a.dbc"), load(DBC_OLD.replace(b"0|16@1+", b"0|16@1+ "), "b.dbc")), [])

    def test_bus_and_node_attributes_float_signals_and_odd_numbers(self):
        old = DBC_OLD + (b'BA_ "Baudrate" 500000;\nBA_ "BusType" "CAN";\nBA_ "NmStationAddress" BU_ Ecu 5;\n'
                         b'SIG_VALTYPE_ 256 EngineSpeed : 1;\n')
        new = old.replace(b"500000", b"250000").replace(b"BU_ Ecu 5", b"BU_ Ecu 6") \
                 .replace(b"EngineSpeed : 1;", b"EngineSpeed : 2;").replace(b"[0|16383.75]", b"[0|inf]")
        xml = dbc_to_xml(old).decode("utf-8")
        for piece in ("<NETWORK>", "<Baudrate>500000</Baudrate>", "<BusType>CAN</BusType>",
                      "<NmStationAddress>5</NmStationAddress>", "<VALUE-TYPE>float32</VALUE-TYPE>"):
            self.assertIn(piece, xml)
        by = {r["path"]: r for r in rows_of(load(old, "a.dbc"), load(new, "b.dbc"))}
        self.assertEqual(by["/"]["what"], ["NETWORK/Baudrate: 500000 -> 250000"])
        self.assertEqual(by["/Ecu"]["what"], ["NmStationAddress: 5 -> 6"])
        self.assertEqual(by["/EngineData/EngineSpeed"]["what"], ["MAX: 16383.75 -> inf", "VALUE-TYPE: float32 -> float64"])


class Compare(Case):
    def test_a_new_can_database_and_where_its_names_occur_in_the_project(self):
        r = self.ok("ecu_compare", new="Input/Vehicle_v2.dbc", old="Input/Vehicle.dbc")
        self.assertEqual((r["old"], r["new"], r["total"]), ("Input/Vehicle.dbc", "Input/Vehicle_v2.dbc", 3))
        self.assertEqual(r["summary"], {"MESSAGE": "1 changed", "SIGNAL": "1 added, 1 changed"})
        msg, door, window = r["changes"]
        self.assertEqual((msg["path"], msg["what"]), ("/BodyStatus", ["DLC: 8 -> 6"]))
        self.assertEqual(sorted(msg["in_project"]), [COM + "IPdu_BodyStatus [ComIPdu]", PDU + "Pdu_BodyStatus [Pdu]"])
        self.assertEqual((door["what"], door["in_project"]),
                         (["LENGTH: 4 -> 3", "MAX: 15 -> 7"], [COM + "Sig_DoorState [ComSignal]"]))
        self.assertEqual((window["change"], window["fields"]["START-BIT"], window["fields"]["LENGTH"]), ("added", "8", "4"))
        self.assertNotIn("in_project", window)                      # nothing in the project carries that name yet
        self.assertIn("integrate-input", r["next"])
        self.assertLess(len(json.dumps(r, separators=(",", ":"))), 1100)
        self.assertEqual(self.ok("ecu_compare", new="Input/Vehicle_v2.dbc", old="Input/Vehicle.dbc", query="door")["total"], 1)
        self.assertEqual(self.ok("ecu_compare", new="Input/Vehicle.dbc", old="Input/Vehicle.dbc")["summary"],
                         "no difference in content")

    def test_any_arxml_is_compared_by_element(self):
        new = SWC.replace(b"/Interfaces/TempIf", b"/Interfaces/TemperatureIf") \
                 .replace(b"<SHORT-NAME>SpeedOut</SHORT-NAME>", b"<SHORT-NAME>VehicleSpeedOut</SHORT-NAME>") \
                 .replace(b'xmlns="http://autosar.org/schema/r4.0"',
                          b'xmlns="http://autosar.org/schema/r4.0" xsi:schemaLocation="x AUTOSAR_00051.xsd"')
        old = SWC.replace(b'xmlns="http://autosar.org/schema/r4.0"',
                          b'xmlns="http://autosar.org/schema/r4.0" xsi:schemaLocation="x AUTOSAR_4-2-2.xsd"')
        self.write(old, "Input/Swc_old.arxml")
        self.write(new, "Input/Swc_new.arxml")
        r = self.ok("ecu_compare", new="Input/Swc_new.arxml", old="Input/Swc_old.arxml")
        by = {c["path"]: c for c in r["changes"]}
        self.assertEqual(by["/Components/EngineCtrl/TempIn"]["what"],
                         ["REQUIRED-INTERFACE-TREF: /Interfaces/TempIf -> /Interfaces/TemperatureIf"])
        port = by["/Components/EngineCtrl/VehicleSpeedOut"]
        self.assertEqual((port["change"], port["from"], port["kind"]), ("renamed", "SpeedOut", "P-PORT-PROTOTYPE"))
        self.assertEqual(r["autosar_schema"], {"old": "AUTOSAR_4-2-2.xsd", "new": "AUTOSAR_00051.xsd"})

    def test_requests_that_cannot_be_answered_say_what_to_do(self):
        self.assertIn("No file at", self.err("ecu_compare", new="Input/None.dbc")["error"])
        self.assertIn("folder", self.err("ecu_compare", new="Input", old="Input/Vehicle.dbc")["error"])
        self.assertIn("not an ARXML or DBC file", self.err("ecu_compare", new="ecuharness.json")["error"])
        self.assertIn("different kinds", self.err("ecu_compare", new="Input/Vehicle.dbc", old=COM_FILE)["error"])
        e = self.err("ecu_compare", new="Input/Vehicle_v2.dbc")     # no earlier version given, and no git here
        self.assertIn("Pass 'old'", e["hint"])

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_without_old_the_last_commit_is_the_earlier_version(self):
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        self.edit({"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"},
                  {"op": "rename", "path": COM + "Sig_LightState", "name": "Sig_Lamp"})
        self.ok("ecu_change_finish", summary="x")
        r = self.ok("ecu_compare", new=COM_FILE)
        self.assertEqual(r["old"], COM_FILE + " at the last commit")
        by = {c["path"]: c for c in r["changes"]}
        self.assertEqual(by[COM + "Sig_EngineTemp"]["what"], ["ComBitSize: 8 -> 7"])
        self.assertEqual((by[COM + "Sig_Lamp"]["change"], by[COM + "Sig_Lamp"]["from"]), ("renamed", "Sig_LightState"))
        self.assertNotIn("in_project", by[COM + "Sig_EngineTemp"])  # the file is the project itself
        shutil.copy(os.path.join(self.root, "Input", "Vehicle_v2.dbc"), os.path.join(self.root, "Input", "Vehicle.dbc"))
        self.assertEqual(self.ok("ecu_compare", new="Input/Vehicle.dbc")["total"], 3)      # the usual case: overwritten in place

    def test_masked_values_stay_masked(self):
        self.set_config(policy={"maskedParams": ["ComSignalInitValue"]})
        old = self.read()
        self.write(old, "Input/old_ecuc.arxml")
        self.write(old.replace(b"<VALUE>40</VALUE>", b"<VALUE>41</VALUE>"), "Input/new_ecuc.arxml")
        r = self.ok("ecu_compare", new="Input/new_ecuc.arxml", old="Input/old_ecuc.arxml")
        self.assertEqual(r["changes"][0]["what"], ["ComSignalInitValue: *** -> ***"])


class Approval(Case):
    def setUp(self):
        Case.setUp(self)
        self.set_config(policy={"approvalPaths": ["/ActiveEcuC/Os/*"]})

    def test_a_change_under_approval_waits_for_a_person(self):
        self.assertEqual(self.edit(SET_PRIO)["gate"], "pass")       # editing and validating are not held up
        e = self.err("ecu_change_finish", summary="Priority 15.")
        self.assertIn("needs a person's approval", e["error"])
        self.assertEqual(e["needs_approval"], [OS + "Task_10ms"])
        self.assertIn("change approve", e["hint"])
        self.assertIn("Never run that command yourself", e["hint"])
        ch = Changes(self.p)
        cur = ch.current()
        self.assertEqual(cur["status"], "open")
        ch.approve(cur, "tester")
        r = self.ok("ecu_change_finish", summary="Priority 15.")
        with open(os.path.join(self.root, r["report"]), encoding="utf-8") as fh:
            self.assertIn("| Approval | tester, ", fh.read())
        with open(os.path.join(self.root, os.path.dirname(r["report"]), "audit.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["approval"]["by"], "tester")

    def test_an_approval_covers_only_what_was_approved(self):
        self.edit(SET_PRIO)
        ch = Changes(self.p)
        ch.approve(ch.current(), "tester")
        self.edit({"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "99"})   # edited afterwards
        self.assertIn("approval", self.err("ecu_change_finish", summary="x")["error"])

    def test_other_paths_need_no_approval(self):
        self.edit(SET_LEN)
        r = self.ok("ecu_change_finish", summary="Length 6.")
        with open(os.path.join(self.root, r["report"]), encoding="utf-8") as fh:
            self.assertIn("| Approval | not required by project policy |", fh.read())

    def test_the_command_refuses_to_approve_without_a_person_at_the_keyboard(self):
        self.edit(SET_PRIO)
        ecuh = [sys.executable, os.path.join(os.path.dirname(SRC), "ecuh.py")]
        r = subprocess.run(ecuh + ["change", "approve"], cwd=self.root, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("~ /ActiveEcuC/Os/Task_10ms OsTaskPriority: 10 -> 15", r.stdout)
        self.assertIn("decision of a person", r.stdout)
        self.assertFalse(Changes(self.reopen()).approved(Changes(self.p).current()))


class Ledger(Case):
    """What was checked and what was not, in the result, the report and the audit record."""

    def test_every_check_is_pass_fail_or_not_run(self):
        self.edit(SET_LEN, ticket="REQ-12")
        r = self.ok("ecu_change_finish", summary="Length 6.")
        self.assertEqual(r["checks"],
                         "pass: structure, types and ranges, vendor validation (simulator, not a vendor tool). "
                         "not run: xsd schema (no schema validator in the harness), code generation, "
                         "build and tests (outside the harness).")
        self.assertIn("also what was not run", r["next"])
        folder = os.path.join(self.root, os.path.dirname(r["report"]))
        with open(os.path.join(folder, "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        for piece in ("## Checks", "| xsd schema | not run: no schema validator in the harness |",
                      "ecuh change undo ch-", "audit.json"):
            self.assertIn(piece, report)
        with open(os.path.join(folder, "audit.json"), encoding="utf-8") as fh:
            audit = json.load(fh)
        self.assertEqual((audit["goal"], audit["ticket"], audit["summary"]), ("Test change for unit tests", "REQ-12", "Length 6."))
        self.assertEqual(audit["changes"], ["~ /ActiveEcuC/EcuC/EcucPduCollection/Pdu_BodyStatus PduLength: 8 -> 6"])
        self.assertEqual(audit["checks"], {
            "structure": "pass", "types_and_ranges": "pass",
            "xsd_schema": "not run: no schema validator in the harness",
            "vendor_validation": "pass (simulator, not a vendor tool)",
            "code_generation": "not run", "build_and_tests": "not run: outside the harness"})
        f = audit["files"][0]
        self.assertEqual((f["path"], len(f["sha256_before"]), len(f["sha256_after"]), f["edited_outside"]),
                         ("Config/ECUC/DemoEcu_EcuC_ecuc.arxml", 64, 64, False))
        self.assertNotEqual(f["sha256_before"], f["sha256_after"])
        self.assertEqual((audit["findings"]["before"], audit["findings"]["new"]), ({"errors": 0, "warnings": 0}, []))
        self.assertEqual((audit["driver"]["type"], audit["undo"], audit["approval"]), ("sim", "ecuh change undo " + audit["change"], None))
        self.assertTrue(audit["user"] and audit["harness"])

    def test_without_a_vendor_tool_or_definitions_the_ledger_says_so(self):
        self.set_config(driver={"type": "none"}, model={"definitions": []})
        self.edit(SET_LEN)
        c = self.ok("ecu_change_finish", summary="x")["checks"]
        self.assertEqual(c, "pass: structure. not run: types and ranges (no module definitions loaded), xsd schema "
                            "(no schema validator in the harness), vendor validation (no tool driver configured), "
                            "code generation, build and tests (outside the harness).")

    def test_earlier_errors_are_named_not_hidden(self):
        self.write(self.read().replace(b"<VALUE>UINT16</VALUE>", b"<VALUE>UINT99</VALUE>"))      # broken before we came
        self.edit(SET_LEN)
        c = self.ok("ecu_change_finish", summary="x")["checks"]
        self.assertIn("pass: structure (for this change; 1 earlier error(s) remain), ", c)

    def test_generation_counts_only_when_it_ran_after_the_last_edit(self):
        exe = fake_tool(self.root, "import sys\nprint('fake 6.3' if sys.argv[1:2] == ['--version'] else 'Successful')\n")
        self.set_config(driver={"type": "dvcfg6", "exe": exe, "bswPackage": os.path.join(self.root, "Bswmd")})
        self.edit(SET_LEN)
        self.ok("ecu_generate")
        c = self.ok("ecu_change_finish", summary="x")["checks"]
        self.assertIn("vendor validation (dvcfg6), code generation (ran after the last edit).", c)
        self.edit(SET_PRIO)
        self.ok("ecu_generate")
        self.edit({"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "16"})
        self.assertIn("not run: xsd schema (no schema validator in the harness), code generation,",
                      self.ok("ecu_change_finish", summary="x")["checks"])


class Undo(Case):
    def finished(self, *ops):
        self.edit(*ops)
        r = self.ok("ecu_change_finish", summary="x")
        return os.path.basename(os.path.dirname(r["report"]))

    def test_a_finished_change_can_be_undone_by_a_person(self):
        before = self.read("Config/ECUC/DemoEcu_EcuC_ecuc.arxml")
        cid = self.finished(SET_LEN)
        ecuh = [sys.executable, os.path.join(os.path.dirname(SRC), "ecuh.py")]
        r = subprocess.run(ecuh + ["change", "undo", cid], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read("Config/ECUC/DemoEcu_EcuC_ecuc.arxml"), before)
        r = subprocess.run(ecuh + ["change", "undo", cid], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("is undone, not finished", r.stderr)
        self.assertEqual(subprocess.run(ecuh + ["change", "undo"], cwd=self.root, capture_output=True).returncode, 2)

    def test_undo_never_overwrites_later_work(self):
        cid = self.finished(SET_LEN)
        self.finished({"op": "set", "path": PDU + "Pdu_EngineData", "param": "PduLength", "value": "7"})   # same file
        with self.assertRaises(ToolError) as cm:
            Changes(self.p).undo(cid, "tester")
        self.assertIn("changed since", cm.exception.message)
        self.assertEqual(self.values(PDU + "Pdu_BodyStatus", "PduLength"), ["6"])
        with self.assertRaises(ToolError):
            Changes(self.p).undo("ch-nope", "tester")


class Versions(Case):
    def test_overview_names_the_schema_of_the_project_files(self):
        self.assertEqual(self.ok("ecu_overview")["autosar_schema"], "AUTOSAR_00048.xsd")
        self.write(self.read().replace(b"AUTOSAR_00048.xsd", b"AUTOSAR_4-2-2.xsd"))
        self.assertEqual(self.ok("ecu_overview")["autosar_schema"], {"AUTOSAR_00048.xsd": 4, "AUTOSAR_4-2-2.xsd": 1})

    def test_a_module_is_described_with_the_version_facts_of_its_definition_file(self):
        rel = "Bswmd/Demo_bswmd.arxml"
        impl = (b"        <BSW-IMPLEMENTATION>\n          <SHORT-NAME>Com_Impl</SHORT-NAME>\n"
                b"          <AR-RELEASE-VERSION>4.2.2</AR-RELEASE-VERSION>\n          <SW-VERSION>5.12.0</SW-VERSION>\n"
                b"          <VENDOR-ID>30</VENDOR-ID>\n        </BSW-IMPLEMENTATION>\n")
        data = self.read(rel)
        i = data.rindex(b"      </ELEMENTS>")
        self.write(data[:i] + impl + data[i:], rel)
        r = self.ok("ecu_describe", kind="Com")
        self.assertEqual(r["implementation"], [{"name": "Com_Impl", "sw_version": "5.12.0",
                                                "autosar_release": "4.2.2", "vendor_id": "30"}])
        self.assertEqual(r["definition_schema"], "AUTOSAR_00048.xsd")
        self.assertNotIn("implementation", self.ok("ecu_describe", kind="ComSignal"))


class Finding(Case):
    def test_counting_instead_of_listing(self):
        r = self.ok("ecu_find", kind="ComSignal", count_by="ComSignalType")
        self.assertEqual((r["total"], r["counted_by"]), (5, "ComSignalType"))
        self.assertEqual(r["groups"][0], {"value": "UINT8", "count": 4,
                                          "first": ["Sig_EngineTemp", "Sig_ThrottlePos", "Sig_DoorState"]})
        self.assertEqual(r["groups"][1]["value"], "UINT16")
        r = self.ok("ecu_find", kind="ComSignal", count_by="comtimeout")
        self.assertEqual({g["value"]: g["count"] for g in r["groups"]}, {"(not set)": 4, "0.1": 1})
        r = self.ok("ecu_find", kind="OsAlarm", count_by="OsAlarmTaskRef")
        self.assertEqual(sorted(g["value"] for g in r["groups"]), [OS + "Task_100ms", OS + "Task_10ms"])
        self.set_config(policy={"maskedParams": ["ComSignalType"]})
        self.assertEqual(self.ok("ecu_find", kind="ComSignal", count_by="ComSignalType")["groups"][0]["value"], "***")

    def test_relations_include_what_is_inside_a_container(self):
        r = self.ok("ecu_find", targets_of="/ActiveEcuC/Com/ComConfig")      # what does all of Com refer to outside?
        self.assertEqual([(m["path"], m["via"]) for m in r["matches"]],
                         [(PDU + "Pdu_EngineData", "IPdu_EngineData.ComPduIdRef"),
                          (PDU + "Pdu_BodyStatus", "IPdu_BodyStatus.ComPduIdRef")])
        r = self.ok("ecu_find", users_of="/ActiveEcuC/EcuC/EcucPduCollection")
        self.assertEqual([(m["path"], m["via"]) for m in r["matches"]],
                         [(COM + "IPdu_EngineData", "ComPduIdRef (to Pdu_EngineData)"),
                          (COM + "IPdu_BodyStatus", "ComPduIdRef (to Pdu_BodyStatus)")])
        r = self.ok("ecu_find", users_of=COM + "Sig_EngineTemp")             # unchanged for a plain container
        self.assertEqual([(m["path"], m["via"]) for m in r["matches"]], [(COM + "IPdu_EngineData", "ComIPduSignalRef")])


XCP = (b'<?xml version="1.0" encoding="UTF-8"?>\n<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
       b'  <AR-PACKAGES>\n    <AR-PACKAGE>\n      <SHORT-NAME>ActiveEcuC</SHORT-NAME>\n      <ELEMENTS>\n'
       b'        <ECUC-MODULE-CONFIGURATION-VALUES>\n          <SHORT-NAME>Xcp</SHORT-NAME>\n'
       b'          <DEFINITION-REF DEST="ECUC-MODULE-DEF">/AUTOSAR/EcucDefs/Xcp</DEFINITION-REF>\n'
       b'          <CONTAINERS>\n            <ECUC-CONTAINER-VALUE>\n              <SHORT-NAME>XcpGeneral</SHORT-NAME>\n'
       b'              <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AUTOSAR/EcucDefs/Xcp/XcpGeneral'
       b'</DEFINITION-REF>\n            </ECUC-CONTAINER-VALUE>\n          </CONTAINERS>\n'
       b'        </ECUC-MODULE-CONFIGURATION-VALUES>\n      </ELEMENTS>\n    </AR-PACKAGE>\n  </AR-PACKAGES>\n'
       b'</AUTOSAR>\n')


class VendorTasks(Case):
    TOOL = ("import os, sys\n"
            "a = sys.argv[1:]\n"
            "if a[:1] == ['--version']: print('fake 6.3'); sys.exit(0)\n"
            "root = [x[3:] for x in a if x.startswith('-p=')][0]\n"
            "if a[:2] == ['project', 'update'] or a[:2] == ['project', 'derive-ecuc']:\n"
            "    open(os.path.join(root, 'ran.txt'), 'a').write(' '.join(a) + '\\n')\n"
            "if a[:2] == ['project', 'update']:\n"
            "    p = os.path.join(root, 'Config', 'ECUC', 'DemoEcu_EcuC_ecuc.arxml')\n"
            "    s = open(p, 'rb').read().replace(b'<VALUE>8</VALUE>', b'<VALUE>6</VALUE>', 1)\n"
            "    open(p, 'wb').write(s)\n"
            "if a[:2] == ['module', 'add']:\n"
            "    open(os.path.join(root, 'Config', 'ECUC', 'DemoEcu Xcp ecuc.arxml'), 'wb').write(%r)\n"
            "print('Successful'); sys.exit(0)\n") % XCP

    def setUp(self):
        Case.setUp(self)
        exe = fake_tool(self.root, self.TOOL)
        self.bsw = os.path.join(self.root, "Bswmd")
        self.set_config(driver={"type": "dvcfg6", "exe": exe, "bswPackage": self.bsw}, policy={"scriptTasks": {
            "update-inputs": {"description": "Update the project from its input files",
                              "command": ["project", "update", "-acr"]},
            "derive": {"description": "Derive from the ECU extract",
                       "command": ["project", "derive-ecuc", "-p={project}", "-b={bsw}", "Input/Extract.arxml"]},
            "add-xcp": {"description": "Add the Xcp module", "command": ["module", "add", "Xcp"]}}})

    def ran(self):
        with open(os.path.join(self.root, "ran.txt")) as fh:
            return fh.read().splitlines()

    def test_an_approved_command_runs_inside_a_change_and_can_be_undone(self):
        r = self.ok("ecu_script_run", task="update-inputs", goal="Bring in the new customer file")
        self.assertTrue(r["opened_change"])
        self.assertEqual(r["config_files_changed_by_tool"], ["Config/ECUC/DemoEcu_EcuC_ecuc.arxml"])
        self.assertEqual(self.ran(), ["project update -acr -p=%s -b=%s" % (self.root, self.bsw)])
        d = self.ok("ecu_change_diff")
        self.assertEqual(d["changes"], ["~ /ActiveEcuC/EcuC/EcucPduCollection/Pdu_EngineData PduLength: 8 -> 6"])
        self.ok("ecu_change_discard")
        self.assertEqual(self.values(PDU + "Pdu_EngineData", "PduLength"), ["8"])

    def test_a_file_the_tool_creates_belongs_to_the_change_and_to_its_undo(self):
        new = "Config/ECUC/DemoEcu Xcp ecuc.arxml"                    # a name with spaces, too
        r = self.ok("ecu_script_run", task="add-xcp", goal="Add the Xcp module for calibration")
        self.assertEqual(r["config_files_changed_by_tool"], [new])
        self.assertEqual(self.ok("ecu_change_diff")["changes"], ["+ /ActiveEcuC/Xcp [Xcp] (+1 nested)"])
        r = self.ok("ecu_change_discard")
        self.assertNotIn("not_restored", r)
        self.assertIn("created by this change", r["restored_files"][0])
        self.assertFalse(os.path.exists(os.path.join(self.root, new)))   # set aside in the change's folder
        # finished, then undone by a person
        self.ok("ecu_script_run", task="add-xcp", goal="Add the Xcp module for calibration")
        self.ok("ecu_edit", ops=[SET_PRIO])
        cid = Changes(self.p).current()["id"]
        self.ok("ecu_change_finish", summary="Xcp added, Task_10ms priority 15")
        with open(os.path.join(self.root, ".ecuharness", "changes", cid, "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("+ /ActiveEcuC/Xcp [Xcp] (+1 nested)", report)
        self.assertNotIn("accepted by a human", report)
        done = Changes(self.reopen()).undo(cid, "tester")
        self.assertEqual(len(done), 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, new)))
        self.assertEqual(self.values(OS + "Task_10ms", "OsTaskPriority"), ["10"])

    def test_placeholders_arguments_and_unrecorded_runs(self):
        self.ok("ecu_script_run", task="derive")
        self.assertEqual(self.ran(), ["project derive-ecuc -p=%s -b=%s Input/Extract.arxml" % (self.root, self.bsw)])
        e = self.err("ecu_script_run", task="derive", args=[{"name": "x", "value": "--force"}])
        self.assertIn("takes no arguments", e["error"])
        r = self.ok("ecu_script_run", task="update-inputs")          # no goal, no open change
        self.assertIn("not recorded", r["next"])
        self.assertIn("update-inputs (Update the project from its input files)",
                      self.err("ecu_script_run", task="format-disk")["hint"])


class Setup(Case):
    def test_tools_a_project_does_not_need_can_be_switched_off(self):
        self.assertIn("ecu_compare", [t.name for t in available(self.p)])
        self.set_config(tools={"off": ["ecu_compare"]})
        names = [t.name for t in available(self.p)]
        self.assertEqual(("ecu_compare" in names, "ecu_find" in names), (False, True))
        with self.assertRaises(KeyError):                           # for the host the tool does not exist
            call_tool(self.p, "ecu_compare", {"new": "Input/Vehicle.dbc"})

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_the_merge_tool_is_offered_only_inside_a_git_repository(self):
        from ecuharness.tools import _in_git
        if _in_git(self.root):
            self.skipTest("the temporary folder lies inside a git repository on this PC")
        self.assertNotIn("ecu_merge", [t.name for t in available(self.p)])
        self.write(self.read().replace(b"<SHORT-NAME>ComConfig</SHORT-NAME>\n",           # markers start a line
                                       b"<SHORT-NAME>ComConfig</SHORT-NAME>\n<<<<<<< ours\n=======\n>>>>>>> theirs\n", 1))
        self.assertIn("version control first", self.ok("ecu_overview")["next"])
        _git(self.root, "init", "-q")
        self.assertIn("ecu_merge", [t.name for t in available(self.p)])
        self.assertIn("Call ecu_merge", self.ok("ecu_overview")["next"])

    def test_doctor_reports_how_merges_are_handled(self):
        hostsetup.init(self.root)
        lines, ok = hostsetup.doctor(self.root)
        self.assertTrue(ok, lines)
        self.assertTrue(any("git:" in ln and "[--]" in ln for ln in lines), lines)

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_init_can_install_the_merge_driver(self):
        _git(self.root, "init", "-q")
        lines = hostsetup.init(self.root, merge_driver=True)
        self.assertTrue(any("merge.ecuh.driver" in ln for ln in lines), lines)
        self.assertTrue(Repo(self.root).driver_installed())
        with open(os.path.join(self.root, ".gitattributes")) as fh:
            self.assertEqual(fh.read().strip(), "*.arxml merge=ecuh")
        lines, ok = hostsetup.doctor(self.root)
        self.assertTrue(any("merge driver installed" in ln and "[ok]" in ln for ln in lines), lines)
        _git(self.root, "config", "merge.ecuh.driver", '"C:/gone/python.exe" "C:/gone/ecuh.pyz" merge-file %O %A %B')
        lines, ok = hostsetup.doctor(self.root)                     # the harness moved: git could not start it
        self.assertFalse(ok)
        self.assertTrue(any("[!!]" in ln and "does not exist" in ln for ln in lines), lines)
        Repo(self.root).remove_driver()
        self.assertFalse(Repo(self.root).driver_installed())

    def test_the_skill_and_rules_cover_the_new_jobs(self):
        hostsetup.init(self.root)
        with open(os.path.join(self.root, ".github", "skills", "ecu-config", "SKILL.md"), encoding="utf-8") as fh:
            skill = fh.read()
        for piece in ("ecu_merge", "ecu_compare", "count_by", "recipes/merge-conflicts.md", "recipes/integrate-input.md",
                      "recipes/os-tasks.md", "recipes/plan-large-change.md", "recipes/final-report.md", "not run"):
            self.assertIn(piece, skill)
        with open(os.path.join(self.root, "AGENTS.md"), encoding="utf-8") as fh:
            rules = fh.read()
        for piece in ("ecu_merge", "ecu_compare", "ecuh change approve", "not run"):
            self.assertIn(piece, rules)
        for name in ("merge-conflicts", "integrate-input", "os-tasks", "plan-large-change", "final-report"):
            text = call_tool(self.p, "ecu_recipe", {"name": name})[0]["text"]
            self.assertIn("Use when:", text)
            self.assertLess(len(text), 2400, name)


if __name__ == "__main__":
    unittest.main()
