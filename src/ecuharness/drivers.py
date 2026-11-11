"""Tool drivers: who judges a configuration beyond the structural checks.

* ``none``        no external tool; structural checks only.
* ``sim``         built-in simulator with a few cross-module rules, for the
                  demo project, tests and evals. Not a model of any real tool.
* ``dvcfg6``      Vector DaVinci Configurator Classic 6 command line (dvcfg-b).
* ``dvadaptive``  Vector DaVinci Developer Adaptive command line (DvCliAdaptive).

The two vendor drivers are written from the public documentation only and are
EXPERIMENTAL until confirmed with `ecuh probe` on a machine that has the tool.
They never interpret a tool failure as success.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time

from . import ToolError
from .arxml import iter_conts, parse_number
from .check import Finding


class DriverResult:
    def __init__(self, status="ok", findings=None, detail="", seconds=0.0, code=None):
        self.status = status          # ok | failed | unavailable
        self.findings = findings or []
        self.detail = detail
        self.seconds = seconds
        self.code = code              # exit code of the tool, when one ran

    def summary(self):
        d = {"status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.seconds >= 0.05:
            d["seconds"] = round(self.seconds, 1)
        return d


class Driver:
    type = "none"
    may_modify_config = False

    def __init__(self, project):
        self.project = project
        self.cfg = project.cfg["driver"]

    def info(self):
        return {"type": self.type, "status": "ready", "note": "structural checks only, no vendor tool"}

    def validate(self):
        return DriverResult()

    def generate(self):
        return DriverResult("unavailable", detail="No tool driver is configured, so nothing can be generated.")

    def run_script(self, task, args):
        return DriverResult("unavailable", detail="No tool driver is configured.")


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------
TYPE_BITS = {"BOOLEAN": 1, "UINT8": 8, "SINT8": 8, "UINT16": 16, "SINT16": 16, "UINT32": 32, "SINT32": 32}


def _p(c, name):
    for e in c.params or ():
        if e.name == name:
            return e.value
    return None


def _i(c, name):
    n = parse_number(_p(c, name))
    return int(n) if isinstance(n, (int, float)) else None


def _r(c, name):
    return [e.value for e in c.refs or () if e.name == name and e.value]


def signal_bits(pos, size, endian):
    """Bit numbers a signal occupies inside its I-PDU."""
    if endian != "BIG_ENDIAN":
        return list(range(pos, pos + size))
    bits, p = [], pos
    for _ in range(size):
        bits.append(p)
        p = p + 15 if p % 8 == 0 else p - 1
    return bits


def free_ranges(used, total):
    out, start = [], None
    for b in range(total):
        if b not in used:
            start = b if start is None else start
        elif start is not None:
            out.append("%d..%d" % (start, b - 1))
            start = None
    if start is not None:
        out.append("%d..%d" % (start, total - 1))
    return out


class SimDriver(Driver):
    type = "sim"

    def info(self):
        return {"type": "sim", "status": "ready",
                "note": "built-in simulator for the demo project; not a real vendor tool"}

    def validate(self):
        t0 = time.time()
        model = self.project.model
        out = []

        def add(id, sev, path, msg, fix=None, param=None):
            out.append(Finding(id, sev, path, msg, param, fix, "sim"))

        by_def = {}
        for m in model.modules():
            for c in iter_conts(m):
                by_def.setdefault((m.name, c.defref.rsplit("/", 1)[-1]), []).append(c)

        # ---- Com ---------------------------------------------------------
        carried = set()
        for ipdu in by_def.get(("Com", "ComIPdu"), []):
            pdu = model.node((_r(ipdu, "ComPduIdRef") or [""])[0]) if _r(ipdu, "ComPduIdRef") else None
            length = _i(pdu, "PduLength") if pdu is not None else None
            total = length * 8 if length is not None else None
            used = {}
            for target in _r(ipdu, "ComIPduSignalRef"):
                carried.add(target)
                sig = model.node(target)
                if sig is None:
                    continue
                pos, size = _i(sig, "ComBitPosition"), _i(sig, "ComBitSize")
                if pos is None or size is None:
                    continue
                bits = signal_bits(pos, size, _p(sig, "ComSignalEndianness"))
                clash = sorted({used[b] for b in bits if b in used})
                if total is not None and bits and max(bits) >= total:
                    add("SIM-COM-01", "error", target,
                        "Signal occupies bits %d..%d but I-PDU %s is only %d bytes (bits 0..%d)."
                        % (min(bits), max(bits), ipdu.name, length, total - 1),
                        "Lower ComBitPosition or ComBitSize, or raise PduLength of %s to at least %d."
                        % (pdu.path(), max(bits) // 8 + 1))
                if clash:
                    free = free_ranges(set(used), total) if total else []
                    add("SIM-COM-02", "error", target,
                        "Signal overlaps %s in I-PDU %s." % (", ".join(clash), ipdu.name),
                        "Move it to free bits of this I-PDU: %s." % (", ".join(free) or "none left"))
                for b in bits:
                    used.setdefault(b, sig.name)
        for sig in by_def.get(("Com", "ComSignal"), []):
            path = sig.path()
            if path not in carried:
                add("SIM-COM-03", "warning", path, "Signal is not carried by any I-PDU.",
                    "Add its path to ComIPduSignalRef of the I-PDU that should carry it.")
            size, typ = _i(sig, "ComBitSize"), _p(sig, "ComSignalType")
            if size is not None and typ in TYPE_BITS and size > TYPE_BITS[typ]:
                add("SIM-COM-04", "error", path,
                    "ComBitSize=%d does not fit ComSignalType=%s (%d bits)." % (size, typ, TYPE_BITS[typ]),
                    "Reduce ComBitSize to at most %d or choose a wider ComSignalType." % TYPE_BITS[typ],
                    "ComBitSize")
        # ---- NvM ---------------------------------------------------------
        seen = {}
        blocks = by_def.get(("NvM", "NvMBlockDescriptor"), [])
        ids = {_i(b, "NvMNvramBlockIdentifier") for b in blocks}
        nxt = next(n for n in range(2, 65536) if n not in ids)
        for b in blocks:
            bid = _i(b, "NvMNvramBlockIdentifier")
            if bid is not None and bid in seen:
                add("SIM-NVM-01", "error", b.path(),
                    "NvMNvramBlockIdentifier=%d is already used by %s." % (bid, seen[bid]),
                    "Use a free identifier; the next free one is %d." % nxt, "NvMNvramBlockIdentifier")
            elif bid is not None:
                seen[bid] = b.name
            if (_p(b, "NvMBlockUseCrc") or "").lower() in ("true", "1") and not _p(b, "NvMBlockCrcType"):
                add("SIM-NVM-02", "error", b.path(), "NvMBlockUseCrc is on but NvMBlockCrcType is not set.",
                    "Set NvMBlockCrcType (NVM_CRC8 | NVM_CRC16 | NVM_CRC32) or switch NvMBlockUseCrc off.",
                    "NvMBlockCrcType")
        # ---- Dem ---------------------------------------------------------
        seen = {}
        for d in by_def.get(("Dem", "DemDTC"), []):
            v = _i(d, "DemDtcValue")
            if v is not None and v in seen:
                add("SIM-DEM-01", "error", d.path(), "DemDtcValue=%d is already used by %s." % (v, seen[v]),
                    "Trouble code values must be unique; choose another value.", "DemDtcValue")
            elif v is not None:
                seen[v] = d.name
        for ev in by_def.get(("Dem", "DemEventParameter"), []):
            if not _r(ev, "DemDTCRef"):
                add("SIM-DEM-02", "warning", ev.path(), "Event sets no trouble code (DemDTCRef is not set).",
                    "Set DemDTCRef to a DemDTC if the event should report a trouble code.", "DemDTCRef")
        return DriverResult("ok", out, seconds=time.time() - t0)

    def generate(self):
        t0 = time.time()
        from .check import check_model
        errors = [f for f in check_model(self.project.model, self.project.cfg) + self.validate().findings
                  if f.severity == "error"]
        if errors:
            return DriverResult("failed", detail="Generation stopped: the configuration has %d validation "
                                                 "error(s). Fix them first (ecu_validate scope=all)." % len(errors))
        outdir = os.path.join(self.project.root, "Output", "Generated")
        os.makedirs(outdir, exist_ok=True)
        names = []
        for m in self.project.model.modules():
            lines = ["/* Simulated generator output for module %s. */" % m.name,
                     "#define %s_CFG_CONTAINERS %du" % (m.name.upper(), sum(1 for _ in iter_conts(m)))]
            with open(os.path.join(outdir, "%s_Cfg.h" % m.name), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            names.append("%s_Cfg.h" % m.name)
        return DriverResult("ok", detail="Generated %d files in Output/Generated." % len(names),
                            seconds=time.time() - t0)

    def run_script(self, task, args):
        return DriverResult("unavailable", detail="The simulator has no script tasks.")


# --------------------------------------------------------------------------
# Vendor command lines
# --------------------------------------------------------------------------
def run_cli(cmd, cwd, timeout):
    """Run a command without a shell. -> (returncode | None, output tail, seconds)."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except FileNotFoundError:
        return None, "Executable not found: %s" % cmd[0], 0.0
    except subprocess.TimeoutExpired:
        return None, "Timed out after %d s: %s" % (timeout, " ".join(cmd[:3])), time.time() - t0
    except OSError as exc:
        return None, "Could not start %s: %s" % (cmd[0], exc), 0.0
    text = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    tail = "\n".join(text.splitlines()[-25:])
    return r.returncode, tail[-3000:], time.time() - t0


def parse_sarif(doc, source):
    """SARIF 2.1.0 results -> findings. Suppressed results are skipped."""
    out = []
    for run in doc.get("runs") or []:
        rules = {}
        for r in ((run.get("tool") or {}).get("driver") or {}).get("rules") or []:
            rules[r.get("id")] = r
        for res in run.get("results") or []:
            if res.get("suppressions"):
                continue
            rid = res.get("ruleId") or (res.get("rule") or {}).get("id") or "?"
            level = res.get("level") or ((rules.get(rid) or {}).get("defaultConfiguration") or {}).get("level")
            sev = {"error": "error", "warning": "warning", "note": "info", "none": "info"}.get(level, "warning")
            msg = res.get("message") or {}
            text = " ".join(str(msg.get("text") or msg.get("markdown") or "").split())[:500]
            path = None
            for loc in res.get("locations") or []:
                for ll in loc.get("logicalLocations") or []:
                    path = ll.get("fullyQualifiedName") or ll.get("name")
                    if path:
                        break
                if path:
                    break
                pl = loc.get("physicalLocation") or {}
                uri = (pl.get("artifactLocation") or {}).get("uri")
                if uri:
                    line = (pl.get("region") or {}).get("startLine")
                    path = uri + (":%d" % line if line else "")
                    break
            fixes = [((f.get("description") or {}).get("text")) for f in res.get("fixes") or []]
            fix = "; ".join(x for x in fixes if x) or None
            out.append(Finding(rid, sev, path, text, fix=fix, source=source))
    return out


def reports_since(root, patterns, since, source):
    """Parse SARIF / JSON report files written after ``since``."""
    findings, seen, notes = [], 0, []
    for pat in patterns:
        full = pat if os.path.isabs(pat) else os.path.join(root, pat)
        for f in sorted(glob.glob(full, recursive=True)):
            try:
                if os.path.getmtime(f) < since:
                    continue
                with open(f, "r", encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError) as exc:
                notes.append("unreadable report %s: %s" % (os.path.basename(f), exc))
                continue
            seen += 1
            if isinstance(doc, dict) and "runs" in doc:
                findings.extend(parse_sarif(doc, source))
            else:
                findings.extend(_loose_json(doc, source))
    return findings, seen, notes


def _loose_json(doc, source):
    """Best-effort reader for report formats we have not seen yet."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            keys = {k.lower(): k for k in x}
            mk = next((keys[k] for k in ("message", "description", "text") if k in keys), None)
            sk = next((keys[k] for k in ("severity", "level") if k in keys), None)
            if mk and sk and isinstance(x[mk], str) and isinstance(x[sk], str):
                sev = x[sk].lower()
                sev = "error" if "err" in sev else ("warning" if "warn" in sev else "info")
                ik = next((keys[k] for k in ("id", "ruleid", "validationid", "code") if k in keys), None)
                pk = next((keys[k] for k in ("path", "element", "location", "object") if k in keys), None)
                out.append(Finding(str(x[ik]) if ik else "?", sev, str(x[pk]) if pk else None,
                                   " ".join(x[mk].split())[:500], source=source))
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(doc)
    return out


class CliDriver(Driver):
    exe_default = ""
    experimental = ("EXPERIMENTAL: written from public documentation, not yet confirmed against a real "
                    "installation. Run `ecuh probe` on a machine with the tool.")

    def __init__(self, project):
        Driver.__init__(self, project)
        self.exe = self.cfg.get("exe") or self.exe_default
        self.bsw = self.cfg.get("bswPackage")
        self.timeout = int(self.cfg.get("timeoutSeconds") or 1800)

    def _resolved(self):
        return shutil.which(self.exe) or (self.exe if os.path.isfile(self.exe) else None)

    def info(self):
        exe = self._resolved()
        if exe is None:
            return {"type": self.type, "status": "unavailable",
                    "note": "'%s' was not found. Set driver.exe in ecuharness.json to its full path." % self.exe}
        rc, tail, _ = run_cli([exe, "--version"], self.project.root, 120)
        d = {"type": self.type, "status": "ready" if rc == 0 else "unavailable",
             "version": tail.splitlines()[-1] if tail else "?", "note": self.experimental}
        if not self.bsw:
            d["status"] = "unavailable"
            d["note"] = "driver.bswPackage is not set in ecuharness.json."
        return d

    def _missing(self):
        if self._resolved() is None:
            return "'%s' was not found. Set driver.exe in ecuharness.json." % self.exe
        if not self.bsw:
            return "driver.bswPackage is not set in ecuharness.json."
        return None

    def _exec(self, args, reports):
        missing = self._missing()
        if missing:
            return DriverResult("unavailable", detail=missing)
        started = time.time() - 2
        rc, tail, secs = run_cli([self._resolved()] + args, self.project.root, self.timeout)
        findings, seen, notes = reports_since(self.project.root, reports, started, self.type)
        if rc is None:
            return DriverResult("failed", findings, tail, secs)
        has_error = any(f.severity == "error" for f in findings)
        if rc == 0 or has_error:
            status = "ok"       # the tool ran; its findings (if any) carry the verdict
        else:
            status = "failed"   # non-zero exit and nothing machine-readable explains it
        detail = tail if status == "failed" else (tail.splitlines()[-1] if tail else "")
        if notes:
            detail = (detail + " | " + "; ".join(notes)).strip(" |")
        if status == "failed":
            detail = "exit code %s. %s" % (rc, detail)
        return DriverResult(status, findings, detail, secs, rc)


class Dvcfg6Driver(CliDriver):
    """DaVinci Configurator Classic 6: `dvcfg-b project validate|generate`,
    `dvcfg-b automation run` (commands as documented on help.vector.com)."""

    type = "dvcfg6"
    exe_default = "dvcfg-b"
    may_modify_config = True

    _proj = None      # set when the tool turned out to want the project file, not the folder

    def _project_arg(self):
        return self.cfg.get("project") or self._proj or self.project.root

    def _base(self):
        return ["-p=%s" % self._project_arg(), "-b=%s" % self.bsw]

    def _reports(self):
        return self.cfg.get("reportGlobs") or ["**/*.sarif"]

    def _project_cmd(self, verb):
        r = self._exec(["project", verb] + self._base(), self._reports())
        if r.code == 2 and not self.cfg.get("project") and self._proj is None:
            # Exit code 2 is documented as "usage error". The documentation shows -p with the project
            # path; if the folder is not accepted, try the project file once and remember what worked.
            files = glob.glob(os.path.join(self.project.root, "*.dvjson"))
            if len(files) == 1:
                self._proj = files[0]
                r2 = self._exec(["project", verb] + self._base(), self._reports())
                if r2.code != 2:
                    r2.detail = ("%s | The tool wants the project file: set driver.project to \"%s\" in "
                                 "ecuharness.json." % (r2.detail, os.path.basename(files[0]))).strip(" |")
                    return r2
                self._proj = None
        return r

    def validate(self):
        return self._project_cmd("validate")

    def generate(self):
        return self._project_cmd("generate")

    def run_script(self, task, args):
        spec = (self.project.policy.get("scriptTasks") or {}).get(task) or {}
        if args:
            return DriverResult("failed", detail="Arguments for dvcfg-b script tasks are not supported yet.")
        if not spec.get("location"):
            return DriverResult("failed", detail="policy.scriptTasks.%s.location (folder of the script) is not set."
                                % task)
        return self._exec(["automation", "run", "-t", task, "-l", spec["location"], "-b", self.bsw or "",
                           "-p", self._project_arg()], self._reports())


class DvAdaptiveDriver(CliDriver):
    """DaVinci Developer Adaptive: `DvCliAdaptive validate|generate|run`."""

    type = "dvadaptive"
    exe_default = "DvCliAdaptive"
    may_modify_config = True

    def _base(self):
        args = ["-b=%s" % self.bsw]
        if self.cfg.get("project"):
            args.append("-p=%s" % self.cfg["project"])
        elif self.cfg.get("models"):
            args.append("--models=%s" % ",".join(self.cfg["models"]))
        return args

    def _repdir(self):
        d = os.path.join(self.project.state, "reports")
        os.makedirs(d, exist_ok=True)
        return d

    def validate(self):
        d = self._repdir()
        return self._exec(["validate"] + self._base() + ["--report-dir=%s" % d, "--report-type=JSON"],
                          [os.path.join(d, "**", "*.json")])

    def generate(self):
        d = self._repdir()
        out = self.cfg.get("output") or os.path.join(self.project.root, "Output")
        return self._exec(["generate"] + self._base() + ["-o=%s" % out, "--report-dir=%s" % d,
                                                         "--report-type=JSON"],
                          [os.path.join(d, "**", "*.json")])

    def run_script(self, task, args):
        spec = (self.project.policy.get("scriptTasks") or {}).get(task) or {}
        cmd = ["run"] + self._base() + ["--tasks=%s" % task]
        for k, v in (args or {}).items():
            cmd.append("--task-args=%s:%s=%s" % (task, k, v))
        if spec.get("location"):
            cmd.append("--script-locations=%s" % spec["location"])
        if spec.get("save"):
            cmd.append("--save")
        return self._exec(cmd, [])


DRIVERS = {"none": Driver, "sim": SimDriver, "dvcfg6": Dvcfg6Driver, "dvadaptive": DvAdaptiveDriver}


def make_driver(project):
    t = (project.cfg.get("driver") or {}).get("type") or "none"
    if t not in DRIVERS:
        raise ToolError("Unknown driver type '%s' in ecuharness.json." % t,
                        "Valid types: %s." % ", ".join(DRIVERS))
    return DRIVERS[t](project)
