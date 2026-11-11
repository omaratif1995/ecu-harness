"""MCP server: both protocol eras, portable schemas, small context footprint."""
import json
import os
import re
import unittest

from util import SRC, Case

from ecuharness.mcp import LEGACY, MODERN, PV, Server
from ecuharness.tools import TOOLS, available

META = {"_meta": {PV: "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}}


class Protocol(Case):
    def rpc(self, method, params=None, mid=1):
        srv = getattr(self, "_srv", None) or Server(self.root)
        self._srv = srv
        return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_legacy_handshake(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["capabilities"])
        self.assertIn("ecu_", r["instructions"])
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], LEGACY[0])
        self.assertIsNone(self._srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        r = self.rpc("tools/list")["result"]
        self.assertNotIn("resultType", r)
        self.assertNotIn("ttlMs", r)

    def test_modern_stateless_requests(self):
        d = self.rpc("server/discover", dict(META))["result"]
        self.assertEqual((d["resultType"], d["supportedVersions"]), ("complete", MODERN))
        self.assertEqual(d["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "ecu-harness")
        r = self.rpc("tools/list", dict(META))["result"]          # no handshake before it
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("ttlMs", r)
        r = self.rpc("tools/call", dict(META, name="ecu_overview", arguments={}))["result"]
        self.assertEqual((r["resultType"], r["isError"]), ("complete", False))
        e = self.rpc("tools/list", {"_meta": {PV: "2031-01-01"}})["error"]
        self.assertEqual((e["code"], e["data"]["supported"]), (-32022, MODERN))

    def test_errors_follow_the_spec(self):
        self.assertEqual(self.rpc("nope/nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        r = self.rpc("tools/call", {"name": "ecu_get", "arguments": {"path": "/No/Such"}})["result"]
        self.assertTrue(r["isError"])                 # tool problems come back as results the model can read
        self.assertIn("hint", json.loads(r["content"][0]["text"]))
        self.assertEqual(self._srv.handle_line("{not json")["error"]["code"], -32700)
        both = self._srv.handle_line(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                                 {"jsonrpc": "2.0", "method": "notifications/x"}]))
        self.assertEqual(len(both), 1)

    def test_without_a_project_the_server_explains_what_to_do(self):
        srv = Server(os.path.join(self.root, "Config"))
        tools = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertGreater(len(tools), 5)
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "ecu_overview", "arguments": {}}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("ecuh init", r["content"][0]["text"])

    def test_recipes_are_offered_as_prompts(self):
        names = [p["name"] for p in self.rpc("prompts/list")["result"]["prompts"]]
        self.assertIn("work-loop", names)
        self.assertIn("trace-and-mirror", names)
        m = self.rpc("prompts/get", {"name": "work-loop", "arguments": {"task": "Do X"}})["result"]["messages"]
        self.assertTrue(m[0]["content"]["text"].endswith("Task: Do X"))

    def test_sloppy_arguments_are_repaired_or_explained(self):
        def c(n, a):
            return json.loads(self.rpc("tools/call", {"name": n, "arguments": a})["result"]["content"][0]["text"])

        self.assertEqual(c("ecu_find", {"kind": "ComSignal", "limit": "2"})["total"], 5)        # "2" for 2
        self.assertEqual(len(c("ecu_find", {"kind": "comsignal", "limit": 2.0})["matches"]), 2)
        self.assertIn("Arguments:", c("ecu_find", {"name": "x"})["hint"])                        # unknown argument
        self.assertIn("path", c("ecu_get", {})["error"])                                          # missing argument
        self.assertIn("Use one of", c("ecu_validate", {"scope": "everything"})["hint"])
        self.assertEqual(len(c("ecu_get", {"path": ["Os/Task_10ms", "Os/Task_Bsw"]})["elements"]), 2)   # list for text
        r = c("ecu_edit", {"goal": "Repair of sloppy arguments in ops", "change_id": "ch-zzzzzz",   # stray id
                           "ops": {"op": "set", "path": "Com/ComConfig/Sig_EngineTemp", "param": "ComBitSize",
                                   "value": 7}})                                                 # one op, not a list
        self.assertTrue(r["written"])
        r = c("ecu_edit", {"ops": "[{\"op\":\"set\",\"path\":\"Com/ComConfig/Sig_EngineTemp\","
                                  "\"param\":\"ComBitSize\",\"value\":\"8\"}]", "dry_run": "true"})   # a JSON string
        self.assertFalse(r["written"])


class LongCalls(Case):
    def test_progress_is_reported_while_a_slow_call_is_working(self):
        """A vendor tool can run for minutes; the client must see the call is alive."""
        import io
        import time

        from ecuharness import mcp

        real = mcp.Server.handle_line

        def slow(self, text):
            if "ecu_overview" in text:
                time.sleep(0.4)
            return real(self, text)

        calls = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "ecu_overview", "arguments": {}, "_meta": {"progressToken": "tok-1"}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "ecu_overview", "arguments": {}}}]
        inp = io.BytesIO("".join(json.dumps(c) + "\n" for c in calls).encode("utf-8"))
        out = io.BytesIO()
        mcp.Server.handle_line = slow
        try:
            mcp.serve(self.root, inp, out, tick=0.1)
        finally:
            mcp.Server.handle_line = real
        msgs = [json.loads(ln) for ln in out.getvalue().decode("utf-8").splitlines()]
        progress = [m for m in msgs if m.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 2)
        self.assertTrue(all(m["params"]["progressToken"] == "tok-1" for m in progress))
        ids = [m.get("id") for m in msgs]
        self.assertLess(max(i for i, m in enumerate(msgs) if m.get("method")), ids.index(2))   # none after call 1
        self.assertEqual([m["id"] for m in msgs if "result" in m], [1, 2])


def walk(schema, path="$"):
    yield path, schema
    for k, v in (schema.get("properties") or {}).items():
        for x in walk(v, path + "." + k):
            yield x
    if isinstance(schema.get("items"), dict):
        for x in walk(schema["items"], path + "[]"):
            yield x


class Schemas(unittest.TestCase):
    """Every model provider must accept the tool definitions unchanged."""

    def test_only_the_portable_json_schema_subset_is_used(self):
        allowed = {"type", "description", "properties", "required", "items", "enum"}
        for t in TOOLS:
            spec = t.spec()
            self.assertRegex(spec["name"], r"^[a-z_]{3,40}$")
            for path, s in walk(spec["inputSchema"]):
                where = "%s %s" % (t.name, path)
                self.assertTrue(set(s) <= allowed, where + " uses " + str(set(s) - allowed))
                self.assertIn(s.get("type"), ("object", "array", "string", "integer", "boolean"), where)
                if s["type"] == "array":
                    self.assertIn("items", s, where)
                if s["type"] == "object":
                    self.assertIn("properties", s, where)
                    self.assertTrue(set(s.get("required", [])) <= set(s["properties"]), where)

    def test_definitions_fit_a_small_context_budget(self):
        specs = [t.spec() for t in available(None)]
        size = sum(len(json.dumps({k: s[k] for k in ("name", "description", "inputSchema")},
                                  separators=(",", ":"))) for s in specs)
        self.assertLessEqual(len(specs), 10)
        self.assertLess(size, 6500, "tool definitions grew to %d characters" % size)

    def test_read_only_tools_are_marked_so_hosts_can_skip_confirmation(self):
        ro = {t.name for t in TOOLS if t.spec()["annotations"]["readOnlyHint"]}
        self.assertEqual(ro, {"ecu_overview", "ecu_find", "ecu_get", "ecu_describe", "ecu_validate",
                              "ecu_change_diff", "ecu_recipe"})


class Hygiene(unittest.TestCase):
    def test_no_network_no_shell_no_third_party_imports(self):
        std = {"__future__", "argparse", "binascii", "copy", "difflib", "fcntl", "fnmatch", "gc", "glob", "hashlib",
               "html", "importlib", "json", "msvcrt", "os", "pkgutil", "re", "shutil", "stat", "subprocess", "sys",
               "tempfile", "threading", "time", "traceback", "uuid", "xml"}
        pkg = os.path.join(SRC, "ecuharness")
        for fn in sorted(os.listdir(pkg)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(pkg, fn), "r", encoding="utf-8") as fh:
                text = fh.read()
            for m in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
                top = m.group(1).split(".")[0]
                self.assertTrue(top in std or top in ("ecuharness", "") or m.group(1).startswith("."),
                                "%s imports %s" % (fn, m.group(1)))
            self.assertNotIn("shell=True", text, fn)
            self.assertNotIn("os.system", text, fn)
            self.assertIsNone(re.search(r"\beval\(|\bexec\(", text), fn)

    def test_the_code_parses_as_python_3_8(self):
        import ast
        root = os.path.dirname(SRC)
        files = [os.path.join(root, "ecuh.py"), os.path.join(root, "tools", "build_pyz.py")]
        for base in (os.path.join(SRC, "ecuharness"), os.path.join(root, "tests")):
            files += [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".py")]
        for f in files:
            with open(f, "r", encoding="utf-8") as fh:
                ast.parse(fh.read(), filename=f, feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main()
