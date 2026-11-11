"""`ecuh probe`: record how the vendor command line really behaves on this PC.

The vendor drivers were written from public documentation. This command
collects what is needed to confirm or correct them: the version, the help text
of every command, and (with --validate) the outcome of one real validation
together with the *shape* of the report files it wrote. It reads no
configuration values into its report unless --include-output is given.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time

from . import __version__

_CMD = re.compile(r"^\s{2,}([a-z][a-z0-9-]{1,30})(?:,\s*[a-z-]+)*\s{2,}\S")


def _run(exe, args, cwd, timeout=180):
    t0 = time.time()
    try:
        r = subprocess.run([exe] + args, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
        text = ((r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr.strip() else "")).strip()
        return r.returncode, text[:20000], round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        return None, "timeout after %d s" % timeout, round(time.time() - t0, 1)
    except OSError as exc:
        return None, "could not start: %s" % exc, 0.0


def _shape(x, depth=0):
    """Structure of a JSON document without its values."""
    if isinstance(x, dict):
        return {k: _shape(v, depth + 1) for k, v in list(x.items())[:40]} if depth < 6 else "{...}"
    if isinstance(x, list):
        return [_shape(x[0], depth + 1), "... %d items" % len(x)] if x else []
    return type(x).__name__


def probe(project, do_validate, out_path, include_output):
    drv = project.driver
    if drv.type not in ("dvcfg6", "dvadaptive"):
        print("ecuh probe needs a vendor driver. Set driver.type to dvcfg6 or dvadaptive in ecuharness.json")
        print("(with driver.exe and driver.bswPackage), then run it again.")
        return 2
    exe = drv._resolved()
    rep = {"harness": __version__, "driver": drv.type, "platform": sys.platform,
           "python": sys.version.split()[0], "exe_found": bool(exe), "steps": []}
    if not exe:
        print("Executable '%s' not found. Set driver.exe in ecuharness.json to its full path." % drv.exe)
        return 2

    def step(args, timeout=180):
        rc, text, secs = _run(exe, args, project.root, timeout)
        rep["steps"].append({"cmd": [os.path.basename(exe)] + args, "exit": rc, "seconds": secs, "output": text})
        print("  %-40s exit %s  %ss" % (" ".join(args)[:40], rc, secs))
        return text

    print("Probing %s (%s)" % (exe, drv.type))
    step(["--version"])
    top = step(["--help"])
    seen = set()
    for line in top.splitlines():
        m = _CMD.match(line)
        if not m or m.group(1) in seen or len(seen) >= 14 or m.group(1) in ("stop", "status"):
            continue                    # only ask for help texts; never call a command that acts
        seen.add(m.group(1))
        sub = step([m.group(1), "--help"])
        subs = set()
        for line2 in sub.splitlines():
            m2 = _CMD.match(line2)
            if m2 and m2.group(1) not in subs and m2.group(1) != m.group(1) and len(subs) < 14:
                subs.add(m2.group(1))
                step([m.group(1), m2.group(1), "--help"])
    if drv.type == "dvcfg6":
        step(["status"])
    if do_validate:
        print("Running one real validation (this can take minutes) ...")
        started = time.time() - 2
        r = drv.validate()
        files = []
        pats = drv._reports() if hasattr(drv, "_reports") else [os.path.join(drv._repdir(), "**", "*.json")]
        for pat in pats:
            full = pat if os.path.isabs(pat) else os.path.join(project.root, pat)
            for f in sorted(glob.glob(full, recursive=True)):
                if os.path.getmtime(f) < started:
                    continue
                info = {"name": os.path.basename(f), "bytes": os.path.getsize(f)}
                try:
                    with open(f, "r", encoding="utf-8-sig") as fh:
                        info["shape"] = _shape(json.load(fh))
                except (OSError, ValueError) as exc:
                    info["unreadable"] = str(exc)
                files.append(info)
        sev = {}
        for f in r.findings:
            sev[f.severity] = sev.get(f.severity, 0) + 1
        rep["validate"] = {"status": r.status, "seconds": round(r.seconds, 1), "findings_parsed": len(r.findings),
                           "by_severity": sev, "rule_ids": sorted({f.id for f in r.findings})[:60],
                           "report_files": files,
                           "output": r.detail if include_output else "(omitted; rerun with --include-output)"}
        print("  validate: status %s, %d findings parsed, %d report file(s)" % (r.status, len(r.findings), len(files)))
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    print("")
    print("Written: %s" % out_path)
    print("It holds the tool's help texts%s and no configuration values%s."
          % (" and the shape of its report files" if do_validate else "",
             " except the tool output you asked to include" if include_output else ""))
    print("Read it before you pass it on, and follow your company's rules for sharing tool information.")
    return 0
