"""Synthetic demo ECU project.

Everything here is written for this project: a handful of small module
definitions that use the well-known AUTOSAR parameter names so the examples
read naturally, with descriptions in our own words. It contains no vendor or
standard definition file content and is only meant for demos, tests and evals.
"""
from __future__ import annotations

import json
import os
import uuid

from .arxml import KIND_TAGS, NUMERIC_KINDS

DEF_PKG = "/DEMO"
CFG_PKG = "/ActiveEcuC"
INF = None


def P(kind, desc, lower=1, upper=1, default=None, min=None, max=None, literals=None):
    return {"kind": kind, "desc": desc, "lower": lower, "upper": upper, "default": default,
            "min": min, "max": max, "literals": literals}


def R(dest, desc, lower=1, upper=1):
    return {"kind": "reference", "desc": desc, "lower": lower, "upper": upper, "dest": DEF_PKG + "/" + dest}


def D(desc, lower=0, upper=INF, params=None, refs=None, subs=None):
    return {"desc": desc, "lower": lower, "upper": upper, "params": params or {}, "refs": refs or {},
            "subs": subs or {}}


SIGNAL_TYPES = ["BOOLEAN", "UINT8", "UINT16", "UINT32", "SINT8", "SINT16", "SINT32"]

DEFS = {
    "EcuC": D("Stack-wide elements shared by several modules.", 0, 1, subs={
        "EcucPduCollection": D("All PDUs the communication stack knows.", 1, 1, subs={
            "Pdu": D("One PDU. Other modules refer to it instead of repeating its length.", params={
                "PduLength": P("integer", "Length of the PDU in bytes.", default="8", min=0, max=4095),
            }),
        }),
    }),
    "Com": D("Signal-based communication: packs signals into I-PDUs.", 0, 1, subs={
        "ComConfig": D("The communication configuration.", 1, 1, subs={
            "ComIPdu": D("An I-PDU handled by Com, with the signals it carries.", params={
                "ComIPduDirection": P("enum", "Whether the ECU sends or receives this I-PDU.",
                                      literals=["RECEIVE", "SEND"]),
            }, refs={
                "ComPduIdRef": R("EcuC/EcucPduCollection/Pdu", "The stack-wide PDU this I-PDU maps to."),
                "ComIPduSignalRef": R("Com/ComConfig/ComSignal", "A signal carried in this I-PDU.", 0, INF),
            }),
            "ComSignal": D("One signal: where it sits in its I-PDU and how it is typed.", params={
                "ComBitPosition": P("integer", "Start bit of the signal inside the I-PDU.", min=0, max=32767),
                "ComBitSize": P("integer", "Width of the signal in bits.", min=1, max=64),
                "ComSignalType": P("enum", "Data type the application sees.", literals=SIGNAL_TYPES),
                "ComSignalEndianness": P("enum", "Byte order of the signal on the bus.",
                                         default="LITTLE_ENDIAN", literals=["LITTLE_ENDIAN", "BIG_ENDIAN"]),
                "ComSignalInitValue": P("string", "Value used until the first reception or write.", lower=0),
                "ComTimeout": P("float", "Reception deadline in seconds; 0 switches monitoring off.",
                                lower=0, default="0.0", min=0, max=3600),
            }, subs={
                "ComFilter": D("Optional filter that decides which values pass.", 0, 1, params={
                    "ComFilterAlgorithm": P("enum", "Filter rule.",
                                            literals=["ALWAYS", "NEVER", "MASKED_NEW_DIFFERS_X"]),
                    "ComFilterMask": P("integer", "Bit mask used by the masked rules.", lower=0, min=0,
                                       max=4294967295),
                }),
            }),
        }),
    }),
    "NvM": D("Non-volatile memory manager: persistent data blocks.", 0, 1, subs={
        "NvMCommon": D("Settings shared by all blocks.", 1, 1, params={
            "NvMCompiledConfigId": P("integer", "Identifier of this configuration build.", default="1", min=0,
                                     max=65535),
            "NvMDatasetSelectionBits": P("integer", "Bits reserved for dataset selection.", default="1", min=0,
                                         max=8),
        }),
        "NvMBlockDescriptor": D("One persistent data block.", params={
            "NvMNvramBlockIdentifier": P("integer", "Unique block number; 0 and 1 are reserved.", min=2,
                                         max=65535),
            "NvMNvBlockLength": P("integer", "Size of the block data in bytes.", min=1, max=65535),
            "NvMBlockManagementType": P("enum", "How the block is stored.", default="NVM_BLOCK_NATIVE",
                                        literals=["NVM_BLOCK_NATIVE", "NVM_BLOCK_REDUNDANT", "NVM_BLOCK_DATASET"]),
            "NvMBlockUseCrc": P("boolean", "Protect the block with a checksum.", default="false"),
            "NvMBlockCrcType": P("enum", "Checksum width; needed when the checksum is on.", lower=0,
                                 literals=["NVM_CRC8", "NVM_CRC16", "NVM_CRC32"]),
            "NvMRamBlockDataAddress": P("string", "Symbol of the RAM mirror of the block.", lower=0),
        }),
    }),
    "Os": D("Operating system objects.", 0, 1, subs={
        "OsTask": D("A task.", params={
            "OsTaskPriority": P("integer", "Priority; a higher number wins.", min=0, max=255),
            "OsTaskSchedule": P("enum", "Whether the task can be preempted.", default="FULL",
                                literals=["FULL", "NON"]),
            "OsTaskActivation": P("integer", "How many activations may be queued.", default="1", min=1, max=255),
        }),
        "OsAlarm": D("An alarm that activates a task periodically.", params={
            "OsAlarmCycleTime": P("float", "Period in seconds.", min=0.0001, max=3600),
        }, refs={
            "OsAlarmTaskRef": R("Os/OsTask", "The task this alarm activates."),
        }),
    }),
    "Dem": D("Diagnostic event manager: events and trouble codes.", 0, 1, subs={
        "DemConfigSet": D("The diagnostic configuration.", 1, 1, subs={
            "DemDTC": D("A diagnostic trouble code.", params={
                "DemDtcValue": P("integer", "The code value reported to the tester; must be unique.", min=1,
                                 max=16777214),
                "DemDTCSeverity": P("enum", "How serious the fault is.", default="DEM_SEVERITY_NO_SEVERITY",
                                    literals=["DEM_SEVERITY_NO_SEVERITY", "DEM_SEVERITY_MAINTENANCE_ONLY",
                                              "DEM_SEVERITY_CHECK_AT_NEXT_HALT", "DEM_SEVERITY_CHECK_IMMEDIATELY"]),
            }),
            "DemEventParameter": D("A monitored fault condition.", params={
                "DemEventKind": P("enum", "Who reports the event.",
                                  literals=["DEM_EVENT_KIND_BSW", "DEM_EVENT_KIND_SWC"]),
                "DemEventFailureCycleCounterThreshold": P("integer", "Failed cycles before the fault is confirmed.",
                                                          lower=0, min=1, max=255),
            }, refs={
                "DemDTCRef": R("Dem/DemConfigSet/DemDTC", "The trouble code this event sets.", 0, 1),
            }),
        }),
    }),
}


def C(definition, name, params=None, refs=None, kids=None):
    return {"def": definition, "name": name, "params": params or {}, "refs": refs or {}, "kids": kids or []}


def _sig(name, pos, size, typ, kids=None, **extra):
    params = {"ComBitPosition": pos, "ComBitSize": size, "ComSignalType": typ,
              "ComSignalEndianness": "LITTLE_ENDIAN"}
    params.update(extra)
    return C("ComSignal", name, params, kids=kids)


def demo_values(extra_signals=0):
    com = "%s/Com/ComConfig/" % CFG_PKG
    pdu = "%s/EcuC/EcucPduCollection/" % CFG_PKG
    pdus = [C("Pdu", "Pdu_EngineData", {"PduLength": 8}), C("Pdu", "Pdu_BodyStatus", {"PduLength": 8}),
            C("Pdu", "Pdu_DiagResp", {"PduLength": 8})]
    signals = [
        _sig("Sig_EngineSpeed", 0, 16, "UINT16", ComSignalInitValue="0", ComTimeout=0.1),
        _sig("Sig_EngineTemp", 16, 8, "UINT8", ComSignalInitValue="40"),
        _sig("Sig_ThrottlePos", 24, 8, "UINT8",
             kids=[C("ComFilter", "ComFilter", {"ComFilterAlgorithm": "ALWAYS"})]),
        _sig("Sig_DoorState", 0, 4, "UINT8"),
        _sig("Sig_LightState", 4, 4, "UINT8"),
    ]
    ipdus = [
        C("ComIPdu", "IPdu_EngineData", {"ComIPduDirection": "SEND"},
          {"ComPduIdRef": pdu + "Pdu_EngineData",
           "ComIPduSignalRef": [com + "Sig_EngineSpeed", com + "Sig_EngineTemp", com + "Sig_ThrottlePos"]}),
        C("ComIPdu", "IPdu_BodyStatus", {"ComIPduDirection": "RECEIVE"},
          {"ComPduIdRef": pdu + "Pdu_BodyStatus",
           "ComIPduSignalRef": [com + "Sig_DoorState", com + "Sig_LightState"]}),
    ]
    for i in range(extra_signals // 8 + (1 if extra_signals % 8 else 0)):
        pname = "Pdu_Bulk_%04d" % i
        pdus.append(C("Pdu", pname, {"PduLength": 8}))
        names = []
        for j in range(min(8, extra_signals - i * 8)):
            sname = "Sig_Bulk_%04d_%d" % (i, j)
            names.append(com + sname)
            signals.append(_sig(sname, j * 8, 8, "UINT8"))
        ipdus.append(C("ComIPdu", "IPdu_Bulk_%04d" % i, {"ComIPduDirection": "RECEIVE"},
                       {"ComPduIdRef": pdu + pname, "ComIPduSignalRef": names}))
    os_ = "%s/Os/" % CFG_PKG
    dem = "%s/Dem/DemConfigSet/" % CFG_PKG
    return {
        "EcuC": [C("EcucPduCollection", "EcucPduCollection", kids=pdus)],
        "Com": [C("ComConfig", "ComConfig", kids=ipdus + signals)],
        "NvM": [
            C("NvMCommon", "NvMCommon", {"NvMCompiledConfigId": 1, "NvMDatasetSelectionBits": 1}),
            C("NvMBlockDescriptor", "NvM_Block_Odometer",
              {"NvMNvramBlockIdentifier": 2, "NvMNvBlockLength": 4, "NvMBlockManagementType": "NVM_BLOCK_NATIVE",
               "NvMBlockUseCrc": True, "NvMBlockCrcType": "NVM_CRC16"}),
            C("NvMBlockDescriptor", "NvM_Block_Config",
              {"NvMNvramBlockIdentifier": 3, "NvMNvBlockLength": 16, "NvMBlockManagementType": "NVM_BLOCK_NATIVE",
               "NvMBlockUseCrc": False}),
            C("NvMBlockDescriptor", "NvM_Block_ErrorLog",
              {"NvMNvramBlockIdentifier": 4, "NvMNvBlockLength": 32,
               "NvMBlockManagementType": "NVM_BLOCK_REDUNDANT", "NvMBlockUseCrc": False}),
        ],
        "Os": [
            C("OsTask", "Task_Bsw", {"OsTaskPriority": 20, "OsTaskSchedule": "FULL", "OsTaskActivation": 1}),
            C("OsTask", "Task_10ms", {"OsTaskPriority": 10, "OsTaskSchedule": "FULL", "OsTaskActivation": 1}),
            C("OsTask", "Task_100ms", {"OsTaskPriority": 5, "OsTaskSchedule": "FULL", "OsTaskActivation": 1}),
            C("OsAlarm", "Alarm_10ms", {"OsAlarmCycleTime": 0.01}, {"OsAlarmTaskRef": os_ + "Task_10ms"}),
            C("OsAlarm", "Alarm_100ms", {"OsAlarmCycleTime": 0.1}, {"OsAlarmTaskRef": os_ + "Task_100ms"}),
        ],
        "Dem": [C("DemConfigSet", "DemConfigSet", kids=[
            C("DemDTC", "DTC_EngineOverTemp", {"DemDtcValue": 74565, "DemDTCSeverity": "DEM_SEVERITY_CHECK_IMMEDIATELY"}),
            C("DemDTC", "DTC_ComTimeout", {"DemDtcValue": 790528, "DemDTCSeverity": "DEM_SEVERITY_MAINTENANCE_ONLY"}),
            C("DemEventParameter", "Event_EngineOverTemp", {"DemEventKind": "DEM_EVENT_KIND_SWC"},
              {"DemDTCRef": dem + "DTC_EngineOverTemp"}),
            C("DemEventParameter", "Event_ComTimeout", {"DemEventKind": "DEM_EVENT_KIND_BSW"},
              {"DemDTCRef": dem + "DTC_ComTimeout"}),
        ])],
    }


HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_00048.xsd">\n'
        '  <AR-PACKAGES>\n    <AR-PACKAGE>\n      <SHORT-NAME>%s</SHORT-NAME>\n      <ELEMENTS>\n')
TAIL = '      </ELEMENTS>\n    </AR-PACKAGE>\n  </AR-PACKAGES>\n</AUTOSAR>\n'


def _x(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mult(d, ind, out):
    out.append('%s<LOWER-MULTIPLICITY>%d</LOWER-MULTIPLICITY>' % (ind, d["lower"]))
    if d["upper"] is None:
        out.append('%s<UPPER-MULTIPLICITY-INFINITE>true</UPPER-MULTIPLICITY-INFINITE>' % ind)
    else:
        out.append('%s<UPPER-MULTIPLICITY>%d</UPPER-MULTIPLICITY>' % (ind, d["upper"]))


def _def_container(name, d, depth, out):
    ind = "  " * depth
    out.append('%s<ECUC-PARAM-CONF-CONTAINER-DEF>' % ind)
    out.append('%s  <SHORT-NAME>%s</SHORT-NAME>' % (ind, name))
    out.append('%s  <DESC><L-2 L="EN">%s</L-2></DESC>' % (ind, _x(d["desc"])))
    _mult(d, ind + "  ", out)
    if d["params"]:
        out.append('%s  <PARAMETERS>' % ind)
        for pn, p in d["params"].items():
            tag = KIND_TAGS[p["kind"]]
            i2 = ind + "    "
            out.append('%s<%s>' % (i2, tag))
            out.append('%s  <SHORT-NAME>%s</SHORT-NAME>' % (i2, pn))
            out.append('%s  <DESC><L-2 L="EN">%s</L-2></DESC>' % (i2, _x(p["desc"])))
            _mult(p, i2 + "  ", out)
            if p["default"] is not None:
                out.append('%s  <DEFAULT-VALUE>%s</DEFAULT-VALUE>' % (i2, _x(p["default"])))
            if p["max"] is not None:
                out.append('%s  <MAX>%s</MAX>' % (i2, p["max"]))
            if p["min"] is not None:
                out.append('%s  <MIN>%s</MIN>' % (i2, p["min"]))
            if p["literals"]:
                out.append('%s  <LITERALS>' % i2)
                for lit in p["literals"]:
                    out.append('%s    <ECUC-ENUMERATION-LITERAL-DEF><SHORT-NAME>%s</SHORT-NAME>'
                               '</ECUC-ENUMERATION-LITERAL-DEF>' % (i2, lit))
                out.append('%s  </LITERALS>' % i2)
            out.append('%s</%s>' % (i2, tag))
        out.append('%s  </PARAMETERS>' % ind)
    if d["refs"]:
        out.append('%s  <REFERENCES>' % ind)
        for rn, r in d["refs"].items():
            i2 = ind + "    "
            out.append('%s<ECUC-REFERENCE-DEF>' % i2)
            out.append('%s  <SHORT-NAME>%s</SHORT-NAME>' % (i2, rn))
            out.append('%s  <DESC><L-2 L="EN">%s</L-2></DESC>' % (i2, _x(r["desc"])))
            _mult(r, i2 + "  ", out)
            out.append('%s  <DESTINATION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">%s</DESTINATION-REF>'
                       % (i2, r["dest"]))
            out.append('%s</ECUC-REFERENCE-DEF>' % i2)
        out.append('%s  </REFERENCES>' % ind)
    if d["subs"]:
        out.append('%s  <SUB-CONTAINERS>' % ind)
        for sn, s in d["subs"].items():
            _def_container(sn, s, depth + 2, out)
        out.append('%s  </SUB-CONTAINERS>' % ind)
    out.append('%s</ECUC-PARAM-CONF-CONTAINER-DEF>' % ind)


def definitions_arxml():
    out = []
    for mn, m in DEFS.items():
        out.append('        <ECUC-MODULE-DEF>')
        out.append('          <SHORT-NAME>%s</SHORT-NAME>' % mn)
        out.append('          <DESC><L-2 L="EN">%s</L-2></DESC>' % _x(m["desc"]))
        _mult(m, "          ", out)
        out.append('          <CONTAINERS>')
        for cn, c in m["subs"].items():
            _def_container(cn, c, 6, out)
        out.append('          </CONTAINERS>')
        out.append('        </ECUC-MODULE-DEF>')
    return HEAD % DEF_PKG.strip("/") + "\n".join(out) + "\n" + TAIL


def _val_container(c, ddef, dpath, cpath, depth, out):
    ind = "  " * depth
    path = cpath + "/" + c["name"]
    out.append('%s<ECUC-CONTAINER-VALUE UUID="%s">' % (ind, uuid.uuid5(uuid.NAMESPACE_URL, path)))
    out.append('%s  <SHORT-NAME>%s</SHORT-NAME>' % (ind, c["name"]))
    out.append('%s  <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">%s</DEFINITION-REF>' % (ind, dpath))
    if c["params"]:
        out.append('%s  <PARAMETER-VALUES>' % ind)
        for pn, v in c["params"].items():
            p = ddef["params"][pn]
            tag = "ECUC-NUMERICAL-PARAM-VALUE" if p["kind"] in NUMERIC_KINDS else "ECUC-TEXTUAL-PARAM-VALUE"
            text = ("true" if v else "false") if isinstance(v, bool) else str(v)
            out.append('%s    <%s>' % (ind, tag))
            out.append('%s      <DEFINITION-REF DEST="%s">%s/%s</DEFINITION-REF>'
                       % (ind, KIND_TAGS[p["kind"]], dpath, pn))
            out.append('%s      <VALUE>%s</VALUE>' % (ind, _x(text)))
            out.append('%s    </%s>' % (ind, tag))
        out.append('%s  </PARAMETER-VALUES>' % ind)
    if c["refs"]:
        out.append('%s  <REFERENCE-VALUES>' % ind)
        for rn, targets in c["refs"].items():
            for t in targets if isinstance(targets, list) else [targets]:
                out.append('%s    <ECUC-REFERENCE-VALUE>' % ind)
                out.append('%s      <DEFINITION-REF DEST="ECUC-REFERENCE-DEF">%s/%s</DEFINITION-REF>'
                           % (ind, dpath, rn))
                out.append('%s      <VALUE-REF DEST="ECUC-CONTAINER-VALUE">%s</VALUE-REF>' % (ind, t))
                out.append('%s    </ECUC-REFERENCE-VALUE>' % ind)
        out.append('%s  </REFERENCE-VALUES>' % ind)
    if c["kids"]:
        out.append('%s  <SUB-CONTAINERS>' % ind)
        for k in c["kids"]:
            _val_container(k, ddef["subs"][k["def"]], dpath + "/" + k["def"], path, depth + 2, out)
        out.append('%s  </SUB-CONTAINERS>' % ind)
    out.append('%s</ECUC-CONTAINER-VALUE>' % ind)


def module_arxml(mod, conts):
    out = ['        <ECUC-MODULE-CONFIGURATION-VALUES UUID="%s">' % uuid.uuid5(uuid.NAMESPACE_URL, mod),
           '          <SHORT-NAME>%s</SHORT-NAME>' % mod,
           '          <DEFINITION-REF DEST="ECUC-MODULE-DEF">%s/%s</DEFINITION-REF>' % (DEF_PKG, mod),
           '          <IMPLEMENTATION-CONFIG-VARIANT>VARIANT-PRE-COMPILE</IMPLEMENTATION-CONFIG-VARIANT>',
           '          <CONTAINERS>']
    for c in conts:
        _val_container(c, DEFS[mod]["subs"][c["def"]], "%s/%s/%s" % (DEF_PKG, mod, c["def"]),
                       "%s/%s" % (CFG_PKG, mod), 6, out)
    out.append('          </CONTAINERS>')
    out.append('        </ECUC-MODULE-CONFIGURATION-VALUES>')
    return HEAD % CFG_PKG.strip("/") + "\n".join(out) + "\n" + TAIL


DBC = """VERSION ""

NS_ :

BS_:

BU_: DemoEcu Gateway

BO_ 256 EngineData: 8 DemoEcu
 SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|16383.75] "rpm" Gateway
 SG_ EngineTemp : 16|8@1+ (1,-40) [-40|215] "degC" Gateway
 SG_ ThrottlePos : 24|8@1+ (0.4,0) [0|100] "%%" Gateway

BO_ 512 BodyStatus: %(dlc)d Gateway
 SG_ DoorState : 0|%(door)d@1+ (1,0) [0|%(doormax)d] "" DemoEcu
 SG_ LightState : 4|4@1+ (1,0) [0|15] "" DemoEcu
%(extra)s
BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_ "GenMsgCycleTime" BO_ 256 10;
BA_ "GenMsgCycleTime" BO_ 512 100;
VAL_ 512 DoorState 0 "closed" 1 "open" ;
"""


def demo_dbc(new=False):
    """The CAN database the demo configuration follows, and a later delivery of it:
    BodyStatus shrinks to 6 bytes, DoorState to 3 bits, and WindowState is new."""
    if new:
        return DBC % {"dlc": 6, "door": 3, "doormax": 7,
                      "extra": ' SG_ WindowState : 8|4@1+ (1,0) [0|15] "" DemoEcu\n'}
    return DBC % {"dlc": 8, "door": 4, "doormax": 15, "extra": ""}


def write_demo(root, extra_signals=0, crlf=False, name="DemoEcu"):
    """Create the demo project in ``root`` (must be empty or absent)."""
    files = {"Bswmd/Demo_bswmd.arxml": definitions_arxml(),
             "Input/Vehicle.dbc": demo_dbc(), "Input/Vehicle_v2.dbc": demo_dbc(new=True)}
    for mod, conts in demo_values(extra_signals).items():
        files["Config/ECUC/%s_%s_ecuc.arxml" % (name, mod)] = module_arxml(mod, conts)
    cfg = {
        "name": name,
        "model": {"config": ["Config/ECUC/*.arxml"], "definitions": ["Bswmd/*.arxml"], "readonly": []},
        "driver": {"type": "sim"},
    }
    files["ecuharness.json"] = json.dumps(cfg, indent=2) + "\n"
    files[".gitignore"] = ".ecuharness/\nOutput/\n"
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if crlf and rel.endswith(".arxml"):
            text = text.replace("\n", "\r\n")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return sorted(files)
