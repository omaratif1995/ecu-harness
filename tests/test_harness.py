"""Change sessions, policy, masking, drivers, setup: do the guardrails hold?"""
import json
import os
import shutil
import tempfile
import unittest

from util import COM, COM_FILE, GOAL, NVM, OS, PDU, SWC, Case, fake_tool, remove_tree

from ecuharness import ToolError, hostsetup
from ecuharness.changes import Changes, Lock
from ecuharness.cli import parse_call_args
from ecuharness.drivers import parse_sarif, signal_bits
from ecuharness.evals import TASKS, run_reference
from ecuharness.tools import available

SET7 = {"op": "set", "path": COM + "Sig_EngineTemp", "param": "ComBitSize", "value": "7"}


class ChangeFlow(Case):
    def test_edit_needs_a_goal_and_then_opens_the_change_itself(self):
        r = self.err("ecu_edit", ops=[SET7])
        self.assertIn("goal", r["hint"])
        r = self.ok("ecu_edit", ops=[SET7], goal="Shrink the temperature signal to 7 bits", ticket="DEMO-1")
        self.assertEqual(r["opened"]["goal"], "Shrink the temperature signal to 7 bits")
        self.assertEqual((r["written"], r["gate"]), (True, "pass"))
        self.assertNotIn("new", r)
        ch = Changes(self.p).current()
        self.assertEqual((ch["ticket"], ch["edits"]), ("DEMO-1", 1))
        r = self.ok("ecu_edit", ops=[dict(SET7, value="8")])       # the change is open: no goal needed
        self.assertNotIn("opened", r)

    def test_only_one_change_at_a_time(self):
        self.begin()
        with self.assertRaises(ToolError):
            self.begin("A second change at the same time")

    def test_edit_reports_new_findings_and_finish_lists_them_when_blocked(self):
        r = self.edit(dict(SET7, value="12"))
        self.assertEqual(r["gate"], "fail")
        self.assertEqual({f["id"] for f in r["new"]}, {"SIM-COM-02", "SIM-COM-04"})
        self.assertTrue(all(f.get("fix") for f in r["new"]))
        e = self.err("ecu_change_finish", summary="too early")
        self.assertIn("Cannot finish", e["error"])
        self.assertEqual({f["id"] for f in e["new"]}, {"SIM-COM-02", "SIM-COM-04"})   # no extra call needed
        r = self.edit(dict(SET7, value="6"))
        self.assertEqual(r["gate"], "pass")
        f = self.ok("ecu_change_finish", summary="Six bits.")
        self.assertEqual(f["changes"], ["~ /ActiveEcuC/Com/ComConfig/Sig_EngineTemp ComBitSize: 8 -> 6"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, f["report"].replace("/", os.sep))))
        self.assertIsNone(Changes(self.p).current())

    def test_old_findings_do_not_block_only_new_ones_do(self):
        # seed a fault outside any change, as if it had always been there
        data = self.read().replace(b"<VALUE>16</VALUE>", b"<VALUE>20</VALUE>", 1)
        self.assertNotEqual(data, self.read())
        self.write(data)
        r = self.edit({"op": "set", "path": NVM + "NvM_Block_Config", "param": "NvMNvBlockLength", "value": "24"},
                      goal="Unrelated change in NvM while Com is already faulty")
        self.assertGreater(r["opened"]["errors_before"], 0)
        self.assertEqual(r["gate"], "pass")
        self.assertNotIn("new", r)
        v = self.ok("ecu_validate")
        self.assertEqual((v["gate"], v["new"]), ("pass", []))
        self.assertGreater(v["errors"], 0)
        self.ok("ecu_change_finish", summary="Block length 24.")

    def test_dry_run_checks_without_writing_or_opening_a_change(self):
        old = self.read()
        r = self.ok("ecu_edit", ops=[SET7], dry_run=True)
        self.assertEqual((r["written"], len(r["changes"])), (False, 1))
        self.assertEqual(self.read(), old)
        self.assertIsNone(Changes(self.p).current())
        self.assertIn("out of range", self.err("ecu_edit", ops=[dict(SET7, value="99")], dry_run=True)["error"])

    def test_discard_restores_the_exact_bytes(self):
        old = self.read()
        self.edit({"op": "remove", "path": COM + "Sig_ThrottlePos"},
                  {"op": "unset", "path": COM + "IPdu_EngineData", "param": "ComIPduSignalRef",
                   "value": COM + "Sig_ThrottlePos"})
        self.assertNotEqual(self.read(), old)
        r = self.ok("ecu_change_discard")
        self.assertEqual(r["restored_files"], [COM_FILE])
        self.assertEqual(self.read(), old)
        self.assertIsNotNone(self.p.model.node(COM + "Sig_ThrottlePos"))

    def test_edits_outside_the_harness_are_caught(self):
        self.edit(SET7)
        outside = self.read().replace(b"<VALUE>7</VALUE>", b"<VALUE>6</VALUE>")
        with open(os.path.join(self.root, COM_FILE), "wb") as fh:      # someone edits the file directly
            fh.write(outside)
        r = self.err("ecu_edit", ops=[dict(SET7, value="5")])
        self.assertIn("outside the harness", r["error"])
        self.assertEqual(self.ok("ecu_validate")["gate"], "fail")
        self.assertIn("outside the harness", self.err("ecu_change_finish", summary="x")["error"])
        Changes(self.p).accept_external(Changes(self.p).require())     # the human-only way out
        self.ok("ecu_change_finish", summary="Size 6 after a manual edit.")

    def test_state_survives_a_restart(self):
        self.edit(SET7)
        self.reopen()                                       # a brand new process would look like this
        d = self.ok("ecu_change_diff")
        self.assertEqual((d["goal"], len(d["changes"])), (GOAL, 1))

    def test_diff_is_computed_from_files(self):
        self.edit(SET7)
        self.edit(dict(SET7, value="8"))
        self.assertEqual(self.ok("ecu_change_diff")["changes"], [])      # back to the start: no difference


class Locking(Case):
    def test_a_second_writer_waits_and_a_released_lock_is_free_again(self):
        with Lock(self.p.state):
            with self.assertRaises(ToolError):
                with Lock(self.p.state, wait=0.3):
                    pass
        with Lock(self.p.state, wait=0.3):
            pass


class Policy(Case):
    def test_read_only_module_and_protected_path(self):
        self.set_config(policy={"readOnlyModules": ["Os"], "protectedPaths": [NVM + "NvM_Block_Odometer*"]})
        self.begin()
        r = self.err("ecu_edit", ops=[{"op": "set", "path": OS + "Task_10ms", "param": "OsTaskPriority", "value": "15"}])
        self.assertIn("read-only by project policy", r["error"])
        r = self.err("ecu_edit", ops=[{"op": "remove", "path": NVM + "NvM_Block_Odometer"}])
        self.assertIn("protected", r["error"])

    def test_definition_files_cannot_be_edited(self):
        self.set_config(model={"config": ["Config/ECUC/*NvM*.arxml"], "readonly": ["Config/ECUC/*Com*.arxml"]})
        self.assertTrue(self.ok("ecu_get", path=COM + "Sig_EngineTemp")["readonly"])
        self.begin()
        self.assertIn("read-only", self.err("ecu_edit", ops=[SET7])["error"])

    def test_mass_removal_needs_a_human(self):
        self.set_config(policy={"maxRemovalsPerChange": 2})
        self.edit({"op": "remove", "path": NVM + "NvM_Block_Config"})
        self.edit({"op": "remove", "path": NVM + "NvM_Block_ErrorLog"})
        r = self.err("ecu_edit", ops=[{"op": "remove", "path": NVM + "NvM_Block_Odometer"}])
        self.assertIn("limit", r["error"])
        self.assertIn("ask", r["hint"])

    def test_masked_values_never_reach_the_model(self):
        self.set_config(policy={"maskedParams": ["NvMRamBlockDataAddress"]})
        e = self.edit({"op": "set", "path": NVM + "NvM_Block_Config", "param": "NvMRamBlockDataAddress",
                       "value": "TopSecret_Symbol"})
        out = json.dumps([e, self.ok("ecu_get", path=NVM + "NvM_Block_Config", raw=True),
                          self.ok("ecu_change_diff"), self.ok("ecu_find", param="NvMRamBlockDataAddress"),
                          self.ok("ecu_find", kind="NvMBlockDescriptor", show="NvMRamBlockDataAddress"),
                          self.ok("ecu_change_finish", summary="Set the symbol.")])
        self.assertNotIn("TopSecret_Symbol", out)
        self.assertIn("***", out)
        self.assertIn("masked", self.err("ecu_find", param="NvMRamBlockDataAddress", value="TopSecret_Symbol")["error"])


class Reading(Case):
    def test_find_get_describe(self):
        r = self.ok("ecu_find", kind="ComSignal")
        self.assertEqual(r["total"], 5)
        self.assertNotIn("kind", r["matches"][0])                  # not repeated when it was the filter
        self.assertEqual(self.ok("ecu_find", param="ComBitSize", value="8", under="Com/ComConfig")["total"], 2)
        self.assertIn("Similar names", self.ok("ecu_find", query="EngineSped")["hint"])
        g = self.ok("ecu_get", path="com/comconfig/sig_enginespeed")        # forgiving about case
        self.assertEqual(g["params"]["ComBitSize"], 16)
        self.assertEqual(g["referenced_by"]["count"], 1)
        g = self.ok("ecu_get", path="Com/ComConfig", depth=1)
        self.assertEqual(len(g["children"]), 7)
        d = self.ok("ecu_describe", kind="NvMBlockDescriptor", filter="crc")
        self.assertEqual([p.split(":")[0] for p in d["params"]], ["NvMBlockUseCrc", "NvMBlockCrcType"])
        self.assertIn("integer 1..64, required", self.ok("ecu_describe", kind="ComBitSize")["param"])
        d = self.ok("ecu_describe", kind="ComSignal")
        self.assertIn("ComSignalType: enum BOOLEAN | UINT8 | UINT16 | UINT32 | SINT8 | SINT16 | SINT32, required. "
                      "Data type the application sees.", d["params"])
        self.assertEqual(d["children"], ["ComFilter (0..1): Optional filter that decides which values pass."])
        self.assertEqual(self.ok("ecu_find", query="checksum", scope="definitions")["total"], 2)

    def test_one_call_answers_a_question(self):
        r = self.ok("ecu_find", targets_of=COM + "IPdu_EngineData", kind="ComSignal",
                    show="ComBitPosition, ComBitSize")
        self.assertEqual(r["matches"], [
            {"path": COM + "Sig_EngineSpeed", "via": "ComIPduSignalRef", "ComBitPosition": 0, "ComBitSize": 16},
            {"path": COM + "Sig_EngineTemp", "via": "ComIPduSignalRef", "ComBitPosition": 16, "ComBitSize": 8},
            {"path": COM + "Sig_ThrottlePos", "via": "ComIPduSignalRef", "ComBitPosition": 24, "ComBitSize": 8}])
        r = self.ok("ecu_find", users_of=PDU + "Pdu_EngineData")
        self.assertEqual(r["matches"], [{"path": COM + "IPdu_EngineData", "kind": "ComIPdu", "via": "ComPduIdRef"}])
        r = self.ok("ecu_get", path="Os/Task_10ms Os/Task_Bsw, /No/Such")
        self.assertEqual([e.get("params", {}).get("OsTaskPriority") for e in r["elements"]], [10, 20, None])
        self.assertIn("error", r["elements"][2])

    def test_results_stay_small(self):
        sizes = {n: len(json.dumps(self.ok(n, **a), separators=(",", ":"))) for n, a in (
            ("ecu_overview", {}), ("ecu_find", {"kind": "ComSignal"}), ("ecu_get", {"path": "Com/ComConfig"}),
            ("ecu_describe", {"kind": "ComSignal"}), ("ecu_validate", {}), ("ecu_recipe", {}))}
        self.assertLess(max(sizes.values()), 1500, sizes)


class OtherArxml(Case):
    def test_non_ecuc_elements_can_be_found_and_read_but_not_edited(self):
        self.write(SWC, "Input/Swc.arxml")
        self.set_config(model={"readonly": ["Input/*.arxml"]})
        o = self.ok("ecu_overview")
        self.assertEqual(o["other_elements"], {"APPLICATION-SW-COMPONENT-TYPE": 1, "SENDER-RECEIVER-INTERFACE": 1})
        r = self.ok("ecu_find", scope="all", kind="P-PORT-PROTOTYPE")
        self.assertEqual(r["matches"], [{"path": "/Components/EngineCtrl/SpeedOut"}])
        g = self.ok("ecu_get", path="/Components/EngineCtrl")
        self.assertEqual(g["props"], {"CATEGORY": "APP",
                                      "PORTS": {"P-PORT-PROTOTYPE": "SpeedOut", "R-PORT-PROTOTYPE": "TempIn"}})
        self.assertTrue(g["readonly"])
        g = self.ok("ecu_get", path="/Components/EngineCtrl/SpeedOut")
        self.assertEqual(g["props"], {"PROVIDED-INTERFACE-TREF": "/Interfaces/SpeedIf"})
        r = self.err("ecu_edit", goal=GOAL, ops=[{"op": "set", "path": "/Components/EngineCtrl",
                                                  "param": "CATEGORY", "value": "X"}])
        self.assertIn("not an ECUC container", r["error"])


class Drivers(Case):
    def test_big_endian_bit_walk(self):
        self.assertEqual(signal_bits(7, 12, "BIG_ENDIAN"), [7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12])
        self.assertEqual(signal_bits(4, 4, "LITTLE_ENDIAN"), [4, 5, 6, 7])

    def test_sarif_is_read_and_suppressed_results_are_skipped(self):
        doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x", "rules": [
            {"id": "COM01", "defaultConfiguration": {"level": "error"}}]}}, "results": [
            {"ruleId": "COM01", "message": {"text": "Signal  too\nlong"},
             "locations": [{"logicalLocations": [{"fullyQualifiedName": "/ActiveEcuC/Com/X"}]}],
             "fixes": [{"description": {"text": "Shorten it"}}]},
            {"ruleId": "NVM02", "level": "warning", "message": {"text": "w"},
             "locations": [{"physicalLocation": {"artifactLocation": {"uri": "Config/a.arxml"},
                                                 "region": {"startLine": 7}}}]},
            {"ruleId": "ACK", "level": "error", "message": {"text": "acknowledged"}, "suppressions": [{}]}]}]}
        f = parse_sarif(doc, "dvcfg6")
        self.assertEqual([(x.id, x.severity, x.path) for x in f],
                         [("COM01", "error", "/ActiveEcuC/Com/X"), ("NVM02", "warning", "Config/a.arxml:7")])
        self.assertEqual((f[0].message, f[0].fix), ("Signal too long", "Shorten it"))

    def test_vendor_cli_findings_and_failures_reach_the_gate(self):
        exe = fake_tool(
            self.root,
            "import json, os, sys\n"
            "a = sys.argv[1:]\n"
            "if a[:1] == ['--version']: print('fake 6.3'); sys.exit(0)\n"
            "root = [x[3:] for x in a if x.startswith('-p=')][0]\n"
            "bad = os.path.exists(os.path.join(root, 'make_error'))\n"
            "if os.path.exists(os.path.join(root, 'crash')): print('license missing'); sys.exit(1)\n"
            "os.makedirs(os.path.join(root, 'Output', 'Log'), exist_ok=True)\n"
            "res = [{'ruleId': 'COM02270', 'level': 'error', 'message': {'text': 'broken'},\n"
            "        'locations': [{'logicalLocations': [{'fullyQualifiedName': '/ActiveEcuC/Com'}]}]}] if bad else []\n"
            "json.dump({'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'f'}}, 'results': res}]},\n"
            "          open(os.path.join(root, 'Output', 'Log', 'validation.sarif'), 'w'))\n"
            "print('Validation Successful' if not bad else 'Validation failed'); sys.exit(1 if bad else 0)\n")
        self.set_config(driver={"type": "dvcfg6", "exe": exe, "bswPackage": os.path.join(self.root, "Bswmd")})
        self.assertEqual(self.ok("ecu_overview")["driver"]["status"], "ready")
        r = self.edit(SET7)
        self.assertEqual(r["gate"], "pending")           # structure checked now; the vendor tool judges at finish
        self.assertEqual(self.ok("ecu_validate")["gate"], "pass")
        open(os.path.join(self.root, "make_error"), "w").close()
        v = self.ok("ecu_validate")
        self.assertEqual((v["gate"], v["new"][0]["id"], v["new"][0]["source"]), ("fail", "COM02270", "dvcfg6"))
        e = self.err("ecu_change_finish", summary="x")
        self.assertEqual(e["new"][0]["id"], "COM02270")
        os.remove(os.path.join(self.root, "make_error"))
        open(os.path.join(self.root, "crash"), "w").close()
        v = self.ok("ecu_validate")                      # a tool that cannot run is never read as "valid"
        self.assertEqual(v["gate"], "fail")
        self.assertIn("license missing", json.dumps(v))
        self.assertIn("Cannot finish", self.err("ecu_change_finish", summary="x")["error"])

    def test_project_file_is_tried_when_the_folder_is_a_usage_error(self):
        open(os.path.join(self.root, "DemoEcu.dvjson"), "w").close()
        exe = fake_tool(
            self.root,
            "import os, sys\n"
            "a = sys.argv[1:]\n"
            "if a[:1] == ['--version']: print('fake 6.3'); sys.exit(0)\n"
            "p = [x[3:] for x in a if x.startswith('-p=')][0]\n"
            "if os.path.isdir(p): print('Usage: -p needs the project file'); sys.exit(2)\n"
            "print('Validation Successful'); sys.exit(0)\n")
        self.set_config(driver={"type": "dvcfg6", "exe": exe, "bswPackage": "Bswmd"})
        v = self.ok("ecu_validate")
        self.assertEqual(v["driver"]["status"], "ok")
        self.assertIn("driver.project", v["driver"]["detail"])
        v = self.ok("ecu_validate")                      # remembered: no second detour, no repeated note
        self.assertEqual(v["driver"]["status"], "ok")
        self.assertNotIn("driver.project", v["driver"].get("detail", ""))

    def test_tool_runs_that_rewrite_files_are_recorded_and_scripts_need_approval(self):
        exe = fake_tool(
            self.root,
            "import os, sys\n"
            "a = sys.argv[1:]\n"
            "if a[:1] == ['--version']: print('fake 6.3'); sys.exit(0)\n"
            "root = None\n"
            "for i, x in enumerate(a):\n"
            "    if x.startswith('-p='): root = x[3:]\n"
            "    elif x == '-p': root = a[i + 1]\n"
            "if a[:2] == ['automation', 'run']:\n"
            "    open(os.path.join(root, 'script_ran.txt'), 'w').write(' '.join(a)); sys.exit(0)\n"
            "if a[:2] == ['project', 'generate']:\n"
            "    p = os.path.join(root, 'Config', 'ECUC', 'DemoEcu_Os_ecuc.arxml')\n"
            "    s = open(p, 'rb').read().replace(b'<VALUE>20</VALUE>', b'<VALUE>21</VALUE>')\n"
            "    open(p, 'wb').write(s); print('Generation Successful'); sys.exit(0)\n"
            "print('Validation Successful'); sys.exit(0)\n")
        self.set_config(driver={"type": "dvcfg6", "exe": exe, "bswPackage": os.path.join(self.root, "Bswmd")},
                        policy={"scriptTasks": {"Hello": {"description": "Says hello", "location": "scripts"}}})
        names = [t.name for t in available(self.p)]
        self.assertIn("ecu_script_run", names)
        self.assertIn("ecu_generate", names)
        r = self.err("ecu_script_run", task="FormatDisk")            # not on the approved list
        self.assertIn("Hello (Says hello)", r["hint"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "script_ran.txt")))
        self.ok("ecu_script_run", task="Hello")
        with open(os.path.join(self.root, "script_ran.txt")) as fh:
            self.assertIn("automation run -t Hello -l scripts", fh.read())
        self.begin()
        g = self.ok("ecu_generate")                                  # the tool rewrites a file while it runs
        self.assertEqual(g["config_files_changed_by_tool"], ["Config/ECUC/DemoEcu_Os_ecuc.arxml"])
        d = self.ok("ecu_change_diff")
        self.assertEqual(d["changes"], ["~ /ActiveEcuC/Os/Task_Bsw OsTaskPriority: 20 -> 21"])
        self.assertNotIn("outside_edits", d)
        self.assertEqual(self.ok("ecu_validate")["gate"], "pass")
        self.ok("ecu_change_discard")                                # and even that can be undone
        self.assertEqual(self.values(OS + "Task_Bsw", "OsTaskPriority"), ["20"])

    def test_without_approved_scripts_the_tool_does_not_exist(self):
        self.assertNotIn("ecu_script_run", [t.name for t in available(self.p)])

    def test_missing_vendor_tool_is_reported_clearly(self):
        self.set_config(driver={"type": "dvcfg6", "exe": "no-such-tool-xyz", "bswPackage": "x"})
        self.assertEqual(self.ok("ecu_overview")["driver"]["status"], "unavailable")
        r = self.edit(SET7)
        self.assertIn("cannot be finished", r["warning"])
        self.assertIn("could not verify", self.err("ecu_change_finish", summary="x")["error"])


class LazyDefinitions(Case):
    def test_only_definitions_of_used_modules_are_parsed(self):
        extra = os.path.join(self.root, "Bswmd", "Unused_bswmd.arxml")
        with open(os.path.join(self.root, "Bswmd", "Demo_bswmd.arxml"), "rb") as fh:
            data = fh.read()
        for name in (b"EcuC", b"Com", b"NvM", b"Os", b"Dem"):          # same content under other module names
            data = data.replace(b"<SHORT-NAME>" + name + b"</SHORT-NAME>", b"<SHORT-NAME>X" + name + b"</SHORT-NAME>")
        with open(extra, "wb") as fh:
            fh.write(data)
        self.reopen()
        files = {os.path.basename(sf.path): sf for sf in self.p.model.files.values() if sf.role == "definitions"}
        self.assertTrue(files["Demo_bswmd.arxml"].loaded)
        self.assertFalse(files["Unused_bswmd.arxml"].loaded)
        self.assertEqual(files["Unused_bswmd.arxml"].defnames, {"XEcuC", "XCom", "XNvM", "XOs", "XDem"})
        self.assertNotIn("/DEMO/XCom", self.p.model.defs)
        self.assertIn("/DEMO/Com", self.p.model.defs)
        self.assertEqual(self.p.model.problems, [])
        self.assertEqual(self.ok("ecu_describe", kind="ComSignal")["definition"], "/DEMO/Com/ComConfig/ComSignal")


class Setup(Case):
    def test_init_is_repeatable_and_keeps_other_servers(self):
        os.makedirs(os.path.join(self.root, ".vscode"))
        with open(os.path.join(self.root, ".vscode", "mcp.json"), "w") as fh:
            json.dump({"servers": {"other": {"type": "stdio", "command": "x"}}}, fh)
        with open(os.path.join(self.root, "AGENTS.md"), "w") as fh:
            fh.write("# Team rules\n\nBe kind.\n")
        first = hostsetup.init(self.root)
        second = hostsetup.init(self.root)
        self.assertTrue(all("unchanged" in ln for ln in second), second)
        self.assertTrue(any("create" in ln for ln in first))
        with open(os.path.join(self.root, ".vscode", "mcp.json")) as fh:
            servers = json.load(fh)["servers"]
        self.assertEqual(set(servers), {"other", "ecu-harness"})
        self.assertEqual(servers["ecu-harness"]["args"][-1], "${workspaceFolder}")
        with open(os.path.join(self.root, ".mcp.json")) as fh:
            self.assertEqual(json.load(fh)["mcpServers"]["ecu-harness"]["type"], "local")
        with open(os.path.join(self.root, "AGENTS.md")) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("# Team rules"))
        self.assertEqual(text.count(hostsetup.MARK_A), 1)

    def test_init_detects_the_layout_of_an_unknown_project(self):
        os.remove(os.path.join(self.root, "ecuharness.json"))
        cfg, notes = hostsetup.default_config(self.root)
        self.assertEqual((cfg["model"]["config"], cfg["model"]["definitions"], cfg["driver"]),
                         (["Config/ECUC/*.arxml"], ["Bswmd/*.arxml"], {"type": "none"}))
        self.assertIn("found 5 ECUC value file(s), 1 definition file(s)", notes[0])

    def test_init_takes_definitions_from_a_bsw_package_and_recognises_a_vendor_project(self):
        bsw = tempfile.mkdtemp(prefix="ecuh_bsw_")
        try:
            os.makedirs(os.path.join(bsw, "Components", "Com"))
            shutil.move(os.path.join(self.root, "Bswmd", "Demo_bswmd.arxml"),
                        os.path.join(bsw, "Components", "Com", "Demo_bswmd.arxml"))
            os.remove(os.path.join(self.root, "ecuharness.json"))
            open(os.path.join(self.root, "DemoEcu.dvjson"), "w").close()
            cfg, notes = hostsetup.default_config(self.root, bsw=bsw)
            base = os.path.abspath(bsw).replace("\\", "/")
            self.assertEqual(cfg["model"]["definitions"], [base + "/**/*[bB][sS][wW][mM][dD]*.arxml"])
            self.assertTrue(any("DaVinci Configurator" in n for n in notes), notes)
            with open(os.path.join(self.root, "ecuharness.json"), "w") as fh:
                json.dump(cfg, fh)
            self.reopen()
            self.assertIn("/DEMO/Com/ComConfig/ComSignal", self.p.model.defs)
        finally:
            remove_tree(bsw)

    def test_a_project_without_ecuc_files_is_set_up_for_reading(self):
        root = tempfile.mkdtemp(prefix="ecuh_ro_")
        try:
            os.makedirs(os.path.join(root, "model"))
            with open(os.path.join(root, "model", "Swc.arxml"), "wb") as fh:
                fh.write(SWC)
            cfg, _ = hostsetup.default_config(root)
            self.assertEqual((cfg["model"]["config"], cfg["model"]["readonly"]), ([], ["model/*.arxml"]))
            hostsetup.init(root)
            lines, ok = hostsetup.doctor(root)
            self.assertTrue(ok, lines)
            self.assertTrue(any("searched and read, not edited" in ln for ln in lines))
        finally:
            remove_tree(root)

    def test_doctor_starts_the_server_and_the_terminal_fallback(self):
        hostsetup.init(self.root)
        lines, ok = hostsetup.doctor(self.root)
        self.assertTrue(ok, lines)
        self.assertTrue(any("terminal fallback" in ln and "[ok]" in ln for ln in lines), lines)


class FirstRun(unittest.TestCase):
    def test_everything_works_from_folders_with_spaces_and_umlauts(self):
        """The harness and the project both live in awkward paths, as on a real company PC."""
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = tempfile.mkdtemp(prefix="ecuh_")
        try:
            home = os.path.join(base, "Büro Tools")
            proj = os.path.join(base, "Mein Steuergerät")
            shutil.copytree(os.path.join(repo, "src"), os.path.join(home, "src"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(os.path.join(repo, "ecuh.py"), home)
            ecuh = [sys.executable, os.path.join(home, "ecuh.py")]
            self.assertEqual(subprocess.run(ecuh + ["demo", proj], capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(ecuh + ["init"], cwd=proj, capture_output=True).returncode, 0)
            r = subprocess.run(ecuh + ["doctor"], cwd=proj, capture_output=True)
            out = r.stdout.decode("utf-8", "replace")
            self.assertEqual(r.returncode, 0, out)
            self.assertIn("[ok] terminal fallback", out)
            self.assertIn("[ok] server start exactly as VS Code will start it", out)
            # the terminal fallback, called the way the skill tells an agent to call it
            with open(os.path.join(proj, "edit.json"), "w", encoding="utf-8") as fh:
                json.dump({"goal": "Set the block length to 24 from the terminal",
                           "ops": [{"op": "set", "path": "NvM/NvM_Block_Config", "param": "NvMNvBlockLength",
                                    "value": "24"}]}, fh)
            with open(os.path.join(proj, "finish.json"), "w", encoding="utf-8") as fh:
                json.dump({"summary": "Block length is 24."}, fh)
            cli = os.path.join(proj, ".ecuharness", "ecuh.cmd" if os.name == "nt" else "ecuh")
            r = subprocess.run([cli, "call", "ecu_edit", "@edit.json"], cwd=proj, capture_output=True)
            self.assertIn(b'"written":true', r.stdout, r.stdout + r.stderr)
            r = subprocess.run([cli, "call", "ecu_change_finish", "@finish.json"], cwd=proj, capture_output=True)
            self.assertIn(b'"status":"finished"', r.stdout, r.stdout + r.stderr)
            self.assertTrue(os.path.isfile(os.path.join(proj, ".ecuharness", "ecuh")))     # for bash users
            if shutil.which("git"):                 # git must be able to call the harness from these paths too
                from ecuharness.evals import _edit_files, _git
                _git(proj, "init", "-q")
                _git(proj, "checkout", "-q", "-b", "main")
                r = subprocess.run(ecuh + ["merge", "--install-driver"], cwd=proj, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                _git(proj, "add", "-A")
                _git(proj, "commit", "-q", "-m", "base")
                sig = "/ActiveEcuC/Com/ComConfig/Sig_EngineTemp"
                _git(proj, "checkout", "-q", "-b", "feature")
                _edit_files(proj, [{"op": "set", "path": sig, "param": "ComSignalInitValue", "value": "50"}])
                _git(proj, "commit", "-q", "-am", "feature")
                _git(proj, "checkout", "-q", "main")
                _edit_files(proj, [{"op": "set", "path": sig, "param": "ComBitSize", "value": "7"}])
                _git(proj, "commit", "-q", "-am", "main")
                r = _git(proj, "merge", "feature", "-m", "both")
                self.assertIn("merged by element, no conflict", r.stderr)
                with open(os.path.join(proj, "Config", "ECUC", "DemoEcu_Com_ecuc.arxml"), "rb") as fh:
                    data = fh.read()
                self.assertIn(b"<VALUE>50</VALUE>", data)
                self.assertIn(b"<VALUE>7</VALUE>", data)
        finally:
            remove_tree(base)


class Cli(Case):
    def test_tool_arguments_from_the_shell(self):
        self.assertEqual(parse_call_args([]), {})
        self.assertEqual(parse_call_args(['{"path": "A"}']), {"path": "A"})
        self.assertEqual(parse_call_args(["path=Com/ComConfig", "depth=1"]), {"path": "Com/ComConfig", "depth": "1"})
        f = os.path.join(self.root, "ops.json")
        with open(f, "w") as fh:
            json.dump([{"op": "set"}], fh)
        self.assertEqual(parse_call_args(["goal=Do it now", "ops=@" + f]),
                         {"goal": "Do it now", "ops": [{"op": "set"}]})
        with self.assertRaises(ToolError):
            parse_call_args(["nonsense"])


class Evals(unittest.TestCase):
    def test_every_reference_solution_passes_its_own_checks(self):
        for t in TASKS:
            results, _, _ = run_reference(t)
            self.assertTrue(all(r[1] for r in results), (t["id"], results))


if __name__ == "__main__":
    unittest.main()
