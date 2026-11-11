"""Change sessions.

Every edit happens inside one open *change*: it has a stated goal, a snapshot
of each file before its first modification, a validation baseline taken when it
began, and a journal of every operation. A change can be reviewed (diff),
checked against its baseline (only new findings count), finished (writes a
review report) or discarded (restores the snapshot).

All state lives on disk under ``.ecuharness/changes/<id>``, so it survives
restarts of the server and does not depend on any connection.
"""
from __future__ import annotations

import binascii
import json
import os
import time

from . import ToolError, __version__
from .arxml import Cont, dispose, iter_conts, scan
from .check import Finding, check_model
from .edit import fmt_change, plan_ops, short
from .project import masked


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


if os.name == "nt":
    import msvcrt

    def _os_lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _os_unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _os_lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Lock:
    """Cross-process lock so two harness processes never write at once.

    It is an operating-system file lock: if the process that holds it dies, the
    system releases it, so a crash can never leave the project blocked."""

    def __init__(self, state_dir, wait=30.0):
        self.dir = state_dir
        self.path = os.path.join(state_dir, "lock")
        self.wait = wait
        self.fh = None

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        self.fh = open(self.path, "a+b")
        deadline = time.time() + self.wait
        while True:
            try:
                _os_lock(self.fh)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    self.fh = None
                    raise ToolError("Another harness process is working on this project right now.",
                                    "Wait a moment and call the tool again.")
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                _os_unlock(self.fh)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


def write_file(path, data):
    """Atomic replace, patient with virus scanners and sync clients."""
    tmp = path + ".ecuh-tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.15 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise ToolError("%s is locked by another program." % os.path.basename(path),
                    "Ask the user to close it there (vendor tool, editor, sync client), then retry.")


def _entries(c, policy):
    out = {}
    for e in list(c.params or ()) + list(c.refs or ()):
        v = "***" if masked(policy, e.name) else (e.value or "")
        out.setdefault(e.name, []).append(v)
    return out


def _fmt(values):
    if not values:
        return "(unset)"
    return values[0] if len(values) == 1 else "[%s]" % ", ".join(values)


def diff_roots(old_root, new_root, policy):
    """Semantic diff of two scans of the same file: container-level lines."""
    def conts(root):
        return {n.path(): n for n in root.walk() if isinstance(n, Cont)} if root is not None else {}

    a, b = conts(old_root), conts(new_root)
    out = []
    for p, n in a.items():
        if p in b:
            continue
        parent = p.rsplit("/", 1)[0]
        if parent in a and parent not in b:
            continue
        nested = sum(1 for _ in iter_conts(n))
        out.append("- %s [%s]%s" % (p, short(n.defref), " (+%d nested)" % nested if nested else ""))
    for p, n in b.items():
        if p in a:
            eo, en = _entries(a[p], policy), _entries(n, policy)
            for name in list(eo) + [k for k in en if k not in eo]:
                if eo.get(name) != en.get(name):
                    out.append("~ %s %s: %s" % (p, name, fmt_change(eo.get(name) or [], en.get(name) or [])))
            continue
        parent = p.rsplit("/", 1)[0]
        if parent in b and parent not in a:
            continue
        vals = ["%s=%s" % (k, _fmt(v)) for k, v in _entries(n, policy).items()]
        nested = sum(1 for _ in iter_conts(n))
        out.append("+ %s [%s]%s%s" % (p, short(n.defref), " " + ", ".join(vals[:10]) if vals else "",
                                      " (+%d nested)" % nested if nested else ""))
    return out


class Changes:
    def __init__(self, project):
        self.project = project
        self.base = os.path.join(project.state, "changes")

    # ---- storage ---------------------------------------------------------
    def _dir(self, cid):
        return os.path.join(self.base, cid)

    def _load(self, cid):
        try:
            with open(os.path.join(self._dir(cid), "change.json"), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save(self, ch):
        os.makedirs(self._dir(ch["id"]), exist_ok=True)
        write_file(os.path.join(self._dir(ch["id"]), "change.json"),
                   json.dumps(ch, indent=1, ensure_ascii=False).encode("utf-8"))

    def _journal(self, ch, entry):
        entry = dict(entry, ts=now())
        with open(os.path.join(self._dir(ch["id"]), "journal.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def all(self):
        out = []
        if os.path.isdir(self.base):
            for cid in os.listdir(self.base):
                ch = self._load(cid)
                if ch:
                    out.append(ch)
        return sorted(out, key=lambda c: c.get("created", ""))

    def current(self):
        for ch in reversed(self.all()):
            if ch.get("status") == "open":
                return ch
        return None

    def require(self, cid=None):
        ch = self.current()
        if ch is None:
            raise ToolError("No change is open.",
                            "ecu_edit opens one when you give it a 'goal' (one sentence: what changes and why).")
        if cid and cid != ch["id"]:
            raise ToolError("Change %s is not open. The open change is %s (%s)." % (cid, ch["id"], ch["goal"]),
                            "Use that change_id, or leave change_id out.")
        return ch

    def _config(self):
        return {sf.rel: sf for sf in self.project.model.files.values() if sf.role == "config"}

    # ---- validation ------------------------------------------------------
    def run_driver(self, label, fn, ch=None):
        """Run a tool-driver action. A vendor tool may rewrite configuration
        files while it runs. With an open change that is recorded, kept
        undoable, and not mistaken for an edit made behind the harness."""
        before = {rel: (sf.sha, sf.data) for rel, sf in self._config().items()}
        result = fn()
        self.project.model.refresh()
        modified = []
        for rel, sf in self._config().items():
            old = before.get(rel)
            if old is not None and old[0] == sf.sha:
                continue
            modified.append(rel)
            if ch is None:
                continue
            if old is not None:
                self._snapshot_bytes(ch, rel, old[1])
            ch["files"].setdefault(rel, {"before": old[0] if old else None})["after"] = sf.sha
            ch["hashes"][rel] = sf.sha
        if modified and ch is not None:
            self._save(ch)
            self._journal(ch, {"event": "driver", "action": label, "modified": modified})
        return result, modified

    def run_validation(self, ch=None):
        drv, _ = self.run_driver("validate", self.project.driver.validate, ch)
        findings = check_model(self.project.model, self.project.cfg)
        return findings + list(drv.findings), drv

    def structure_check(self):
        return check_model(self.project.model, self.project.cfg)

    def delta(self, ch, findings, drv=None, structure_only=False):
        try:
            with open(os.path.join(self._dir(ch["id"]), "baseline.json"), "r", encoding="utf-8") as fh:
                base = json.load(fh)
        except (OSError, ValueError):
            base = []
        if structure_only:
            base = [b for b in base if b.get("source", "structure") == "structure"]
        bkeys = {b["key"] for b in base}
        cur = {}
        for f in findings:
            cur.setdefault(f.key(), f)
        new = [f for k, f in cur.items() if k not in bkeys]
        resolved = [Finding.from_dict(b) for b in base if b["key"] not in cur]
        strict = self.project.policy.get("gate") == "no-new-findings"
        blocking = [f for f in new if f.severity == "error" or (strict and f.severity == "warning")]
        reasons = []
        if not structure_only and drv is not None and drv.status != "ok":
            reasons.append("the tool driver could not verify the configuration (%s%s)"
                           % (drv.status, ": " + drv.detail if drv.detail else ""))
        if blocking:
            reasons.append("%d new finding(s) introduced by this change" % len(blocking))
        return {"new": new, "resolved": resolved, "blocking": blocking, "reasons": reasons,
                "gate": "fail" if reasons else "pass"}

    # ---- lifecycle -------------------------------------------------------
    def begin(self, goal, ticket=None):
        open_ = self.current()
        if open_ is not None:
            raise ToolError("Change %s is still open (goal: %s)." % (open_["id"], open_["goal"]),
                            "Continue it, or close it first with ecu_change_finish or ecu_change_discard.")
        goal = " ".join(str(goal or "").split())
        if len(goal) < 8:
            raise ToolError("The goal is too short.", "Say in one sentence what will change and why.")
        findings, drv = self.run_validation(None)
        ch = {
            "id": "ch-" + binascii.hexlify(os.urandom(3)).decode("ascii"),
            "goal": goal[:300], "ticket": ticket or None, "status": "open", "created": now(),
            "harness": __version__, "driver": self.project.driver.type,
            "hashes": {rel: sf.sha for rel, sf in self._config().items()},
            "files": {}, "edits": 0, "removed": 0,
            "baseline": {"driver": drv.summary(),
                         "errors": sum(1 for f in findings if f.severity == "error"),
                         "warnings": sum(1 for f in findings if f.severity == "warning")},
        }
        os.makedirs(self._dir(ch["id"]), exist_ok=True)
        write_file(os.path.join(self._dir(ch["id"]), "baseline.json"),
                   json.dumps([f.to_dict(True) for f in findings], ensure_ascii=False).encode("utf-8"))
        self._save(ch)
        self._journal(ch, {"event": "begin", "goal": ch["goal"]})
        return ch

    def external(self, ch):
        """Config files that changed without going through the harness."""
        cur = {rel: sf.sha for rel, sf in self._config().items()}
        out = [rel for rel, sha in ch["hashes"].items() if rel in cur and cur[rel] != sha]
        out += [rel + " (deleted)" for rel in ch["hashes"] if rel not in cur]
        out += [rel + " (new)" for rel in cur if rel not in ch["hashes"]]
        return out

    def require_clean(self, ch):
        ext = self.external(ch)
        if ext:
            raise ToolError(
                "Files changed outside the harness during this change: %s." % ", ".join(ext[:6]),
                "ARXML files may only be changed through ecu_edit. Stop and tell the user. They can undo the "
                "outside edit, accept it knowingly with `ecuh change accept-external`, or have you discard "
                "this change with ecu_change_discard.")

    def _snapshot(self, ch, sf):
        self._snapshot_bytes(ch, sf.rel, sf.data)

    def _snapshot_bytes(self, ch, rel, data):
        """Keep the content a file had before this change first touched it."""
        if os.path.isabs(rel):
            raise ToolError("%s lies outside the project folder and cannot be edited." % rel)
        dst = os.path.join(self._dir(ch["id"]), "snapshot", rel.replace("/", os.sep))
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)

    def edit(self, ch, ops, dry_run=False):
        self.require_clean(ch)
        model = self.project.model
        plan = plan_ops(model, self.project.policy, ops, ch.get("removed", 0))
        if dry_run or not plan.files:
            return plan
        written = []
        try:
            for sf, data in plan.files.items():
                self._snapshot(ch, sf)
                write_file(sf.path, data)
                written.append(sf)
        except Exception:
            for sf in written:
                write_file(sf.path, sf.data)
            raise
        for sf, data in plan.files.items():
            before = sf.sha
            model.adopt(sf, data, plan.scans[sf])
            ch["hashes"][sf.rel] = sf.sha
            ch["files"].setdefault(sf.rel, {"before": before})["after"] = sf.sha
        ch["edits"] = ch.get("edits", 0) + 1
        ch["removed"] = ch.get("removed", 0) + plan.removed
        self._save(ch)
        self._journal(ch, {"event": "edit", "ops": ops, "changes": plan.changes,
                           "files": {sf.rel: sf.sha for sf in plan.files}})
        return plan

    def accept_external(self, ch, who="human"):
        """Human-only: take outside edits as part of this change (not diffable)."""
        self.project.model.refresh()
        ext = self.external(ch)
        for rel, sf in self._config().items():
            old = ch["hashes"].get(rel)
            if old != sf.sha:
                ch["files"].setdefault(rel, {"before": old})["after"] = sf.sha
                ch["files"][rel]["external"] = True
                ch["hashes"][rel] = sf.sha
        for rel in list(ch["hashes"]):
            if rel not in self._config():
                del ch["hashes"][rel]
        self._save(ch)
        self._journal(ch, {"event": "accept-external", "by": who, "files": ext})
        return ext

    def diff(self, ch):
        cfg = self._config()
        lines = []
        for rel in sorted(ch.get("files") or {}):
            snap = os.path.join(self._dir(ch["id"]), "snapshot", rel.replace("/", os.sep))
            sf = cfg.get(rel)
            if not os.path.isfile(snap):
                lines.append("? %s was changed outside the harness and accepted by a human; "
                             "that part is not covered by this diff" % rel)
                continue
            cache = self.project.__dict__.setdefault("_snapshots", {})
            old = cache.get(snap)
            if old is None:                      # a snapshot never changes once it is written
                with open(snap, "rb") as fh:
                    old = cache[snap] = scan(fh.read())
            lines.extend(diff_roots(old.root, sf.scan.root if sf is not None and sf.scan else None,
                                    self.project.policy))
        return lines

    def _forget_snapshots(self):
        for old in self.project.__dict__.pop("_snapshots", {}).values():
            dispose(old.root)

    def finish(self, ch, summary):
        self.require_clean(ch)
        findings, drv = self.run_validation(ch)
        d = self.delta(ch, findings, drv)
        if d["gate"] != "pass":
            raise ToolError("Cannot finish: %s." % "; ".join(d["reasons"]),
                            "Fix the findings listed here with ecu_edit, then finish again. If the goal cannot "
                            "be reached validly, call ecu_change_discard and tell the user why.",
                            {"new": [f.to_dict() for f in d["blocking"][:8]]} if d["blocking"] else None)
        lines = self.diff(ch)
        ch["status"] = "finished"
        ch["finished"] = now()
        ch["summary"] = " ".join(str(summary or "").split())[:1000]
        report = os.path.join(self._dir(ch["id"]), "report.md")
        write_file(report, self._report(ch, lines, findings, d, drv).encode("utf-8"))
        self._save(ch)
        self._forget_snapshots()
        self._journal(ch, {"event": "finish", "changes": len(lines)})
        return {"report": self.project.rel(report), "lines": lines, "delta": d}

    def discard(self, ch):
        snap_root = os.path.join(self._dir(ch["id"]), "snapshot")
        restored = []
        cfg = self._config()
        for rel, info in (ch.get("files") or {}).items():
            snap = os.path.join(snap_root, rel.replace("/", os.sep))
            if not os.path.isfile(snap):
                continue
            with open(snap, "rb") as fh:
                data = fh.read()
            target = cfg[rel].path if rel in cfg else os.path.join(self.project.root, rel.replace("/", os.sep))
            write_file(target, data)
            restored.append(rel)
        self.project.model.refresh()
        left = [r for r in self.external(ch) if r.split(" ")[0] not in restored]
        ch["status"] = "discarded"
        ch["finished"] = now()
        self._save(ch)
        self._forget_snapshots()
        self._journal(ch, {"event": "discard", "restored": restored})
        return restored, left

    # ---- report ----------------------------------------------------------
    def _report(self, ch, lines, findings, d, drv):
        errs = sum(1 for f in findings if f.severity == "error")
        warns = sum(1 for f in findings if f.severity == "warning")
        out = ["# Change %s" % ch["id"], "",
               "| | |", "|---|---|",
               "| Goal | %s |" % ch["goal"],
               "| Ticket | %s |" % (ch.get("ticket") or "-"),
               "| Opened / finished | %s / %s |" % (ch["created"], ch["finished"]),
               "| Harness / driver | ecu-harness %s / %s (%s) |" % (__version__, self.project.driver.type, drv.status),
               "", "## Summary by the agent", "", ch.get("summary") or "(none given)", "",
               "## What changed (%d)" % len(lines), ""]
        out += ["    " + ln for ln in lines[:500]] or ["Nothing."]
        if len(lines) > 500:
            out.append("    ... %d more lines; see journal.jsonl" % (len(lines) - 500))
        b = ch["baseline"]
        out += ["", "## Validation", "",
                "- Before: %d errors, %d warnings. After: %d errors, %d warnings."
                % (b["errors"], b["warnings"], errs, warns),
                "- New findings that block: none (otherwise the change could not be finished)."]
        soft = [f for f in d["new"] if f not in d["blocking"]]
        for f in soft[:50]:
            out.append("- New %s %s at %s: %s" % (f.severity, f.id, f.path, f.message))
        for f in d["resolved"][:50]:
            out.append("- Resolved %s %s at %s: %s" % (f.severity, f.id, f.path, f.message))
        out += ["", "## Files", ""]
        for rel, info in sorted((ch.get("files") or {}).items()):
            out.append("- `%s`  %s -> %s%s" % (rel, (info.get("before") or "?")[:12], (info.get("after") or "?")[:12],
                                               "  (edited outside the harness)" if info.get("external") else ""))
        out += ["", "## For the reviewer", "",
                "- The lines above are computed from the files, not taken from the agent's own account.",
                "- Every operation is in `journal.jsonl` next to this report; the files as they were before are in `snapshot/`.",
                "- Structural checks ran on the whole model%s."
                % ("" if self.project.driver.type == "none" else " and the tool driver validated it"),
                "- Open the project in the vendor tool before you release: this harness does not replace its validation.",
                ""]
        return "\n".join(out)
