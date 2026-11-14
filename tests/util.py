import json
import os
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ecuharness.changes import Changes            # noqa: E402
from ecuharness.demo import write_demo            # noqa: E402
from ecuharness.evals import remove_tree          # noqa: E402,F401  (also used by the test modules)
from ecuharness.project import Project            # noqa: E402
from ecuharness.tools import call_tool            # noqa: E402

COM = "/ActiveEcuC/Com/ComConfig/"
PDU = "/ActiveEcuC/EcuC/EcucPduCollection/"
NVM = "/ActiveEcuC/NvM/"
OS = "/ActiveEcuC/Os/"
DEM = "/ActiveEcuC/Dem/DemConfigSet/"
COM_FILE = "Config/ECUC/DemoEcu_Com_ecuc.arxml"
GOAL = "Test change for unit tests"

SWC = b"""<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Components</SHORT-NAME>
      <ELEMENTS>
        <APPLICATION-SW-COMPONENT-TYPE>
          <SHORT-NAME>EngineCtrl</SHORT-NAME>
          <CATEGORY>APP</CATEGORY>
          <PORTS>
            <P-PORT-PROTOTYPE>
              <SHORT-NAME>SpeedOut</SHORT-NAME>
              <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Interfaces/SpeedIf</PROVIDED-INTERFACE-TREF>
            </P-PORT-PROTOTYPE>
            <R-PORT-PROTOTYPE>
              <SHORT-NAME>TempIn</SHORT-NAME>
              <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Interfaces/TempIf</REQUIRED-INTERFACE-TREF>
            </R-PORT-PROTOTYPE>
          </PORTS>
        </APPLICATION-SW-COMPONENT-TYPE>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>SpeedIf</SHORT-NAME>
          <IS-SERVICE>false</IS-SERVICE>
        </SENDER-RECEIVER-INTERFACE>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def fake_tool(root, body, name="dvcfg-b"):
    """A stand-in for a vendor command line: proves our process handling, not the vendor's behaviour."""
    script = os.path.join(root, "fake_tool.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(body)
    if os.name == "nt":
        exe = os.path.join(root, name + ".cmd")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        exe = os.path.join(root, name)
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        os.chmod(exe, 0o755)
    return exe


class Case(unittest.TestCase):
    """A fresh demo project per test."""

    crlf = False
    signals = 0

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ecuh_test_")
        write_demo(self.root, self.signals, self.crlf)
        self.p = Project(self.root)
        self.p.model.refresh()

    def tearDown(self):
        remove_tree(self.root)

    def reopen(self):
        self.p = Project(self.root)
        self.p.model.refresh()
        return self.p

    def set_config(self, **patch):
        path = os.path.join(self.root, "ecuharness.json")
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return self.reopen()

    def read(self, rel=COM_FILE):
        with open(os.path.join(self.root, rel), "rb") as fh:
            return fh.read()

    def write(self, data, rel=COM_FILE):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return self.reopen()

    def call(self, name, **args):
        return call_tool(self.p, name, args)

    def ok(self, name, **args):
        result, is_error = call_tool(self.p, name, args)
        self.assertFalse(is_error, result)
        return result

    def err(self, name, **args):
        result, is_error = call_tool(self.p, name, args)
        self.assertTrue(is_error, result)
        return result

    def begin(self, goal=GOAL):
        """Open a change without editing (the tools do this through ecu_edit's goal)."""
        return Changes(self.p).begin(goal)["id"]

    def edit(self, *ops, **kw):
        kw.setdefault("goal", GOAL)
        return self.ok("ecu_edit", ops=list(ops), **kw)

    def values(self, path, name):
        n = self.p.model.node(path)
        return [e.value for e in list(n.params or ()) + list(n.refs or ()) if e.name == name]


def inserted_block(old, new):
    """If new == old with one contiguous block inserted, return that block."""
    a, b = old.splitlines(True), new.splitlines(True)
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    j = 0
    while j < len(a) - i and j < len(b) - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    if i + j != len(a):
        return None
    return b"".join(b[i:len(b) - j])
