"""Command line: `ecuh` (or `python -m ecuharness`, or `python ecuh.pyz`)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import ToolError, __version__
from .project import CONFIG_NAME


def find_root(arg):
    """--project, else ECUHARNESS_PROJECT, else the nearest folder with a config."""
    if arg:
        return os.path.abspath(arg)
    env = os.environ.get("ECUHARNESS_PROJECT")
    if env:
        return os.path.abspath(env)
    cur = os.getcwd()
    while True:
        if os.path.isfile(os.path.join(cur, CONFIG_NAME)):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            return os.getcwd()
        cur = up


def _out(text=""):
    sys.stdout.write(text + "\n")


def cmd_mcp(a):
    from .mcp import serve
    serve(find_root(a.project))
    return 0


def cmd_init(a):
    from .hostsetup import init
    root = find_root(a.project)
    hosts = tuple(h.strip() for h in a.hosts.split(",") if h.strip())
    lines = init(root, a.dry_run, a.driver, a.exe, a.bsw_package, hosts)
    _out("ecu-harness init in %s%s" % (root, "  (dry run, nothing written)" if a.dry_run else ""))
    for ln in lines:
        _out(ln)
    if not a.dry_run:
        _out("")
        _out("Next:")
        _out("  1. ecuh doctor          checks the setup and starts the server once")
        _out("  2. VS Code: open this folder, open Chat in Agent mode, press the tools button and")
        _out("     confirm that 'ecu-harness' is listed. Trust the server when VS Code asks.")
        _out("  3. Ask: \"Give me an overview of the ECU configuration.\"")
        _out("The MCP files contain paths of this PC. Colleagues run `ecuh init` themselves.")
    return 0


def cmd_doctor(a):
    from .hostsetup import doctor
    lines, ok = doctor(find_root(a.project))
    _out("ecu-harness doctor")
    for ln in lines:
        _out(ln)
    _out("")
    _out("All good." if ok else "Problems found: see the lines marked !!")
    return 0 if ok else 1


def cmd_demo(a):
    from .demo import write_demo
    target = os.path.abspath(a.dir)
    if os.path.isdir(target) and os.listdir(target):
        _out("Folder is not empty: %s" % target)
        return 2
    files = write_demo(target, a.signals)
    _out("Demo ECU project created in %s (%d files)." % (target, len(files)))
    _out("Next:  cd \"%s\"   then   ecuh init   then   ecuh doctor" % target)
    return 0


def _project(a):
    from .project import Project
    return Project(find_root(a.project))


def cmd_tools(a):
    from .tools import available
    try:
        p = _project(a)
    except ToolError:
        p = None
    specs = [t.spec() for t in available(p)]
    if a.json:
        _out(json.dumps(specs, indent=2))
        return 0
    total = 0
    for s in specs:
        size = len(json.dumps({k: s[k] for k in ("name", "description", "inputSchema")}, separators=(",", ":")))
        total += size
        _out("  %-20s %5d chars  %s" % (s["name"], size, s["description"][:70]))
    _out("  %d tools, %d characters of definitions (about %d tokens per request)" % (len(specs), total, total // 4))
    return 0


def _load_arg(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


def parse_call_args(items):
    """Tool arguments from the shell: one JSON object, @file, '-' for stdin,
    or name=value pairs (a value may be @file, e.g. ops=@ops.json)."""
    if not items:
        return {}
    if len(items) == 1:
        one = items[0]
        if one == "-":
            return json.loads(sys.stdin.read() or "{}")
        if one.lstrip()[:1] in ("{", "["):
            return json.loads(one)
        if one.startswith("@"):
            return _load_arg(one[1:])
    out = {}
    for it in items:
        if "=" not in it:
            raise ToolError("Cannot read the argument '%s'." % it,
                            "Write arguments as name=value (path=Com/ComConfig), or pass one JSON object, "
                            "or @file.json.")
        k, v = it.split("=", 1)
        out[k.strip()] = _load_arg(v[1:]) if v.startswith("@") else v
    return out


def cmd_call(a):
    from .tools import call_tool
    try:
        args = parse_call_args(a.args)
    except (ValueError, OSError) as exc:
        _out(json.dumps({"error": "Arguments could not be read: %s" % exc,
                         "hint": "Use name=value pairs, one JSON object, or @file.json."}))
        return 2
    try:
        result, is_error = call_tool(_project(a), a.tool, args)
    except KeyError:
        from .tools import available
        _out(json.dumps({"error": "Unknown tool '%s'." % a.tool,
                         "hint": "Tools: %s." % ", ".join(t.name for t in available(None))}))
        return 2
    pretty = a.pretty or (sys.stdout.isatty() and not a.compact)
    _out(json.dumps(result, ensure_ascii=False, indent=2) if pretty
         else json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 1 if is_error else 0


def cmd_change(a):
    from .changes import Changes, Lock
    p = _project(a)
    p.model.refresh()
    ch = Changes(p)
    if a.action == "list":
        for c in ch.all():
            _out("  %s  %-9s %s  edits:%d  %s" % (c["id"], c["status"], c["created"], c.get("edits", 0), c["goal"][:60]))
        return 0
    with Lock(p.state):
        cur = ch.require()
        ext = ch.accept_external(cur, os.environ.get("USERNAME") or os.environ.get("USER") or "human")
    _out("Accepted %d outside edit(s) into change %s: %s" % (len(ext), cur["id"], ", ".join(ext) or "none"))
    _out("They are recorded in the journal and marked in the report as not covered by the diff.")
    return 0


def cmd_probe(a):
    from .probe import probe
    return probe(_project(a), a.validate, a.out, a.include_output)


def cmd_eval(a):
    from .evals import main as eval_main
    return eval_main(a)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ecuh", description="ECU Harness %s: an AI agent harness for AUTOSAR "
                                 "ECU configuration tools." % __version__)
    ap.add_argument("--project", help="project folder (default: nearest folder with %s)" % CONFIG_NAME)
    ap.add_argument("--version", action="version", version="ecu-harness %s" % __version__)
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    add("mcp", cmd_mcp, "run the MCP server on stdin/stdout (started by the AI host)")
    sp = add("init", cmd_init, "set up this project for VS Code, the Copilot app/CLI and Claude Code")
    sp.add_argument("--dry-run", action="store_true", help="show what would be written")
    sp.add_argument("--hosts", default="vscode,copilot", help="comma list of: vscode, copilot, claude")
    sp.add_argument("--driver", choices=["none", "sim", "dvcfg6", "dvadaptive"], help="tool driver for a new config")
    sp.add_argument("--exe", help="full path of the vendor command line (dvcfg-b, DvCliAdaptive)")
    sp.add_argument("--bsw-package", help="path of the BSW package")
    add("doctor", cmd_doctor, "check the setup and start the server once")
    sp = add("demo", cmd_demo, "create a small synthetic ECU project to try things out")
    sp.add_argument("dir")
    sp.add_argument("--signals", type=int, default=0, help="add this many extra signals (size tests)")
    sp = add("tools", cmd_tools, "list the tools with their size in the model's context")
    sp.add_argument("--json", action="store_true")
    sp = add("call", cmd_call, "call one tool from the shell, e.g. ecuh call ecu_overview")
    sp.add_argument("tool")
    sp.add_argument("args", nargs="*", help="name=value pairs, one JSON object, @file.json, or - for stdin")
    sp.add_argument("--compact", action="store_true", help="one-line JSON even in a terminal")
    sp.add_argument("--pretty", action="store_true", help="indented JSON")
    sp = add("change", cmd_change, "list changes, or accept edits made outside the harness")
    sp.add_argument("action", choices=["list", "accept-external"])
    sp = add("probe", cmd_probe, "record how the vendor command line behaves on this PC")
    sp.add_argument("--validate", action="store_true", help="also run one real validation")
    sp.add_argument("--include-output", action="store_true", help="include tool output text (may name project data)")
    sp.add_argument("--out", default="ecuh-probe.json")
    sp = add("eval", cmd_eval, "run or grade the evaluation tasks")
    sp.add_argument("action", choices=["list", "run", "setup", "grade"])
    sp.add_argument("task", nargs="?")
    sp.add_argument("dir", nargs="?")
    sp.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # never fail on a console that cannot show a character
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    try:
        return a.fn(a) or 0
    except ToolError as exc:
        sys.stderr.write("ecuh: %s\n" % exc.message)
        if exc.hint:
            sys.stderr.write("      %s\n" % exc.hint)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
