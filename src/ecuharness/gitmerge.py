"""Merging ARXML inside a git repository.

When git stops a merge (or rebase, or cherry-pick) because an ARXML file was
changed on both sides, it keeps the three versions of the file in its index.
This module reads them, merges them by element (``merge.py``), writes the
result into the working file, and remembers which conflicts are still open
until a person has decided each one. It never stages and never commits.

The same merge can run as a git *merge driver*, so that git calls it during
the merge itself and files without real conflicts never stop the merge.

State lives in the git folder (``ecuh-merge.json``), not in the project.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from . import InternalError, ToolError, __version__
from .changes import write_file
from .gitio import Repo, text_merge
from .merge import KINDS, merge_bytes
from .project import DEFAULT_CONFIG, masked, sha256

STATE_NAME = "ecuh-merge.json"
BACKUP_DIR = "ecuh-merge-backup"
_MARKERS = re.compile(br"^(?:<<<<<<<|>>>>>>>) ", re.M)


def is_arxml(path):
    return path.lower().endswith(".arxml")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _eol_style(data):
    head = data[:262144]
    lf, crlf = head.count(b"\n"), head.count(b"\r\n")
    if lf == 0:
        return None
    return "crlf" if crlf == lf else ("lf" if crlf == 0 else "mixed")


def _eol_like(data, like):
    """``data`` with the line endings of the working file ``like``. Git hands out
    the stored versions with the line endings its settings ask for; if those
    settings and the file in the folder disagree, the file in the folder wins."""
    if like is None:
        return data
    have, want = _eol_style(data), _eol_style(like)
    if have == "lf" and want == "crlf":
        return data.replace(b"\n", b"\r\n")
    if have == "crlf" and want == "lf":
        return data.replace(b"\r\n", b"\n")
    return data


class MergeSession:
    def __init__(self, start, masked=None):
        self.repo = Repo(start)
        self.masked = masked
        self.path = os.path.join(self.repo.gitdir, STATE_NAME)
        self.state = {"seq": 0, "files": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                self.state = data
        except (OSError, ValueError):
            pass
        self.other = []

    def _save(self):
        write_file(self.path, json.dumps(self.state, indent=1, ensure_ascii=False).encode("utf-8"))

    def _versions(self, path, stages):
        base = self.repo.blob(stages[0], path) if stages[0] else None
        return base, self.repo.blob(stages[1], path), self.repo.blob(stages[2], path)

    # ---- bring the state in line with git --------------------------------
    def sync(self, redo=False):
        """Merge every conflicted ARXML file that has not been merged yet."""
        unmerged = self.repo.unmerged()
        files = self.state["files"]
        changed = False
        same = any(path in unmerged and e.get("stages") == [unmerged[path].get(i) for i in (1, 2, 3)]
                   for path, e in files.items())
        for path in list(files):
            if path not in unmerged:
                del files[path]                         # staged or aborted: no longer our business
                changed = True
        if redo or not same:
            self.state["seq"] = 0                       # a new merge numbers its conflicts from c1
        self.other = sorted(p for p in unmerged if not is_arxml(p))
        for path in sorted(unmerged):
            if is_arxml(path):
                changed = self._file(path, unmerged[path], redo) or changed
        if changed:
            self._save()
        return self

    def _file(self, path, st, redo):
        files = self.state["files"]
        stages = [st.get(1), st.get(2), st.get(3)]
        entry = files.get(path)
        if entry is not None and entry.get("stages") == stages and not redo:
            return False
        new = {"stages": stages, "count": 0, "applied": [], "conflicts": [], "time": _now()}
        files[path] = new
        if not stages[1] or not stages[2]:
            new["skipped"] = ("one side deleted this file and the other changed it. Decide with git: "
                              "`git rm` to delete it, `git add` to keep it")
            return True
        full = self.repo.abs(path)
        work = _read(full)
        try:
            res = merge_bytes(*self._versions(path, stages), masked=self.masked)
        except (ToolError, InternalError) as exc:
            new["skipped"] = ("could not be merged by element (%s). It is as git left it"
                              % getattr(exc, "message", exc))
            return True
        data = _eol_like(res.data, work)
        if work is not None and work != data and not redo and not _MARKERS.search(work):
            new["skipped"] = ("was edited after git stopped, so it is left as it is. If it is finished, stage it "
                              "with `git add`. `ecuh merge --redo` replaces it with the merge by element")
            return True
        if work is not None and work != data:
            self._backup(path, work)
        if work != data:
            write_file(full, data)
        new["written"] = sha256(data)
        new["count"] = res.count
        new["applied"] = res.applied
        for c in res.conflicts:
            self.state["seq"] = self.state.get("seq", 0) + 1
            c["id"] = "c%d" % self.state["seq"]
            new["conflicts"].append(c)
        return True

    def _backup(self, path, data):
        dst = os.path.join(self.repo.gitdir, BACKUP_DIR, path.replace("/", "__"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)
        except OSError:
            pass

    # ---- decisions -------------------------------------------------------
    def decide(self, theirs=(), ours=(), who="user"):
        """Record decisions for conflicts and write the files they change."""
        files = self.state["files"]
        index = {c["id"]: (path, c) for path, e in files.items() for c in e.get("conflicts", [])}
        picks = {}
        for side, ids in (("theirs", theirs), ("ours", ours)):
            for i in ids:
                i = str(i).strip()
                if i not in index:
                    known = [k for k, (_, c) in index.items() if not c.get("decision")]
                    raise ToolError("There is no conflict '%s'." % i,
                                    "Open conflicts: %s." % (", ".join(known) or "none"))
                if picks.get(i, side) != side:
                    raise ToolError("Conflict %s is in both lists." % i, "Give each conflict to one side.")
                picks[i] = side
        plans = []
        for path, e in files.items():
            mine = {c["id"]: picks[c["id"]] for c in e.get("conflicts", []) if c["id"] in picks}
            if not mine:
                continue
            rewrite = any((c.get("decision") or "ours") != mine[c["id"]] for c in e["conflicts"] if c["id"] in mine)
            data = None
            if rewrite:
                full = self.repo.abs(path)
                work = _read(full)
                if work is None or sha256(work) != e.get("written"):
                    raise ToolError(
                        "%s was changed after it was merged, so their side cannot be written into it "
                        "automatically any more." % path,
                        "Nothing was changed. Set the value you want with ecu_edit, then pass the conflict in "
                        "keep_ours to mark it as decided. Decide conflicts before editing next time.")
                decisions = {c["key"]: mine.get(c["id"], c.get("decision")) for c in e["conflicts"]}
                res = merge_bytes(*self._versions(path, e["stages"]),
                                  decisions={k: v for k, v in decisions.items() if v}, masked=self.masked)
                data = _eol_like(res.data, work)
                still = {c["key"] for c in res.conflicts}
                plans.append((path, e, mine, data, res, still))
            else:
                plans.append((path, e, mine, None, None, None))
        for path, e, mine, data, res, still in plans:
            if data is not None:
                write_file(self.repo.abs(path), data)
                e["written"] = sha256(data)
                e["count"] = res.count
                e["applied"] = res.applied
            for c in e["conflicts"]:
                if c["id"] in mine:
                    c["decision"], c["by"], c["at"] = mine[c["id"]], who, _now()
                elif still is not None and c["key"] not in still and not c.get("decision"):
                    c["decision"], c["by"], c["at"] = "theirs", "follows from another decision", _now()
        if plans:
            self._save()
        return [p[0] for p in plans if p[3] is not None]

    # ---- views -----------------------------------------------------------
    def files(self):
        return self.state["files"]

    def conflicts(self, only_open=False):
        out = []
        for path in sorted(self.state["files"]):
            for c in self.state["files"][path].get("conflicts", []):
                if not only_open or not c.get("decision"):
                    out.append((path, c))
        return out

    def label(self):
        """What is being merged, in git's own words (first line of its message)."""
        try:
            with open(os.path.join(self.repo.gitdir, "MERGE_MSG"), "r", encoding="utf-8", errors="replace") as fh:
                return fh.readline().strip()[:120] or None
        except OSError:
            return None

    def report(self, extra=None):
        """Markdown account of the merge: what was taken, what was decided, by whom."""
        op = self.repo.operation()
        head = self.repo.head()
        out = ["# ARXML merge report", "",
               "| | |", "|---|---|",
               "| Written | %s by ecu-harness %s |" % (_now(), __version__),
               "| Git operation | %s%s |" % (op or "none in progress", " (%s)" % self.label() if self.label() else ""),
               "| Our side | branch %s at %s |" % (head.get("branch") or "?", head.get("commit") or "?"),
               ""]
        if op == "rebase":
            out += ["During a rebase, \"ours\" is the branch being rebased onto and \"theirs\" is the commit "
                    "being replayed.", ""]
        for path in sorted(self.state["files"]):
            e = self.state["files"][path]
            out += ["## `%s`" % path, ""]
            if e.get("skipped"):
                out += ["Not merged by the harness: %s." % e["skipped"], ""]
                continue
            out.append("Taken from their side without conflict: %d change(s)." % e.get("count", 0))
            out += ["", "    " + "\n    ".join(e["applied"])] if e.get("applied") else []
            if e.get("count", 0) > len(e.get("applied") or []):
                out.append("    ... %d more" % (e["count"] - len(e["applied"])))
            cs = e.get("conflicts") or []
            out += ["", "Conflicts: %d." % len(cs), ""]
            if cs:
                out += ["| id | where | what | base | ours | theirs | decision |", "|---|---|---|---|---|---|---|"]
            for c in cs:
                dec = c.get("decision")
                dec = "%s (%s, %s)" % (dec, c.get("by", "?"), c.get("at", "?")) if dec else "**open: our side is in the file**"
                cell = lambda v: str(v if v is not None else "-").replace("|", "\\|")   # noqa: E731
                out.append("| %s | %s%s | %s | %s | %s | %s | %s |" % (
                    c["id"], cell(c["where"]), " " + cell(c.get("param") or c.get("field")) if (c.get("param") or c.get("field")) else "",
                    KINDS.get(c["kind"], c["kind"]) + (" (calculated by the tool on both sides)" if c.get("calculated") else ""),
                    cell(c.get("base")), cell(c.get("ours")), cell(c.get("theirs")), dec))
            out.append("")
        if self.other:
            out += ["## Other conflicted files", "", "Not ARXML, not handled here: %s." % ", ".join("`%s`" % p for p in self.other), ""]
        out += list(extra or [])
        out += ["## For the reviewer", "",
                "- Each file is our version with their changes copied in as bytes from their version. The harness "
                "parsed the result again and compared its content with what the merge decided before writing it.",
                "- A merge by element cannot know intent. Two changes that do not touch the same element can still "
                "contradict each other; the validation of the vendor tool is what finds that.",
                "- Nothing was staged or committed. `git add` the files when you are satisfied.", ""]
        return "\n".join(out)


# --------------------------------------------------------------------------
# Git merge driver
# --------------------------------------------------------------------------
def driver_command():
    """The command git must run, for `git config merge.ecuh.driver`."""
    from .hostsetup import launch
    command, args, env = launch()
    fwd = lambda s: s.replace("\\", "/")                # noqa: E731
    parts = ['%s="%s"' % (k, fwd(v)) for k, v in env.items()]
    parts.append('"%s"' % fwd(command))
    parts += [a if a.startswith("-") else '"%s"' % fwd(a) for a in args]
    return " ".join(parts) + " merge-file %O %A %B --path %P"


def driver(base, ours, theirs, label=None, out=None, err=None):
    """Merge three files the way git asks a merge driver to: the result replaces
    ``ours``; exit code 0 = clean, 1 = conflicts are left. Never worse than git's
    own text merge: if the merge by element cannot be done, that one runs."""
    err = err or sys.stderr
    label = label or os.path.basename(ours)
    b, o, t = _read(base), _read(ours), _read(theirs)
    try:
        if o is None or t is None:
            raise ToolError("a version of the file is missing")
        res = merge_bytes(b, o, t, masked=lambda n: masked(DEFAULT_CONFIG["policy"], n))
    except Exception as exc:                            # includes harness defects: fall back, never block git
        if out is None:
            code = text_merge(ours, base, theirs)
            err.write("ecu-harness: %s was merged as text (%s)\n" % (label, getattr(exc, "message", exc)))
            return code
        err.write("ecu-harness: %s cannot be merged by element (%s)\n" % (label, getattr(exc, "message", exc)))
        return 2
    target = out or ours
    if res.data != o or out:
        with open(target, "wb") as fh:
            fh.write(res.data)
    left = res.open
    if left:
        err.write("ecu-harness: %s: %d change(s) merged, %d conflict(s) need a decision. Our side is in the "
                  "file for those. Run `ecuh merge`, or ask your assistant (ecu_merge).\n"
                  % (label, res.count, len(left)))
        for c in left[:10]:
            err.write("  %s %s: base %s, ours %s, theirs %s\n" % (
                c["where"], c.get("param") or c.get("field") or c["kind"], c.get("base", "-"), c.get("ours", "-"),
                c.get("theirs", "-")))
        return 1
    err.write("ecu-harness: %s: %d change(s) merged by element, no conflict\n" % (label, res.count))
    return 0
