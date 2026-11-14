"""The few things the harness asks of git: read, never commit.

Used to find files a merge left in conflict (with their three versions), to
read the last committed version of a file, and to note branch and commit in
reports. Git is started without a shell and only with fixed read commands.
The two exceptions are explicit actions of a person: installing the merge
driver (`git config` in this clone) and the driver's fallback to git's own
text merge (`git merge-file` on git's temporary files).
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import ToolError


OPERATIONS = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
              ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))


def find_git():
    return shutil.which("git")


def git_dir_of(start):
    """The git folder of the working tree that contains ``start``, found on disk
    without starting git. None outside a working tree."""
    d = os.path.abspath(start)
    while True:
        p = os.path.join(d, ".git")
        if os.path.isdir(p):
            return p
        if os.path.isfile(p):                           # a linked worktree or submodule: "gitdir: <path>"
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    line = fh.readline().strip()
            except OSError:
                return None
            if not line.startswith("gitdir:"):
                return None
            g = line[7:].strip()
            return os.path.normpath(g if os.path.isabs(g) else os.path.join(d, g))
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def operation_in(gitdir):
    """What git is in the middle of in that git folder: merge, rebase, cherry-pick, revert or None."""
    for name, what in OPERATIONS:
        if os.path.exists(os.path.join(gitdir, name)):
            return what
    return None


class Repo:
    """The git repository that contains ``start``."""

    def __init__(self, start):
        self.exe = find_git()
        if not self.exe:
            raise ToolError("git was not found on this PC.",
                            "This function needs git on the PATH. Tell the user.")
        self.cwd = os.path.abspath(start)
        rc, out, _ = self.run(["rev-parse", "--show-toplevel"])
        if rc != 0:
            raise ToolError("%s is not inside a git repository." % self.cwd,
                            "This function works on files that git tracks. Tell the user.")
        self.top = os.path.normpath(out.decode("utf-8", "replace").strip())
        rc, out, _ = self.run(["rev-parse", "--absolute-git-dir"])
        if rc != 0:
            rc, out, _ = self.run(["rev-parse", "--git-dir"])
        gd = out.decode("utf-8", "replace").strip()
        self.gitdir = os.path.normpath(gd if os.path.isabs(gd) else os.path.join(self.cwd, gd))
        self.start = self.cwd
        self.cwd = self.top             # every later command runs at the top: paths are repository paths

    def run(self, args, data=None, timeout=300):
        """-> (return code, stdout bytes, stderr text)."""
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
        try:
            r = subprocess.run([self.exe] + list(args), cwd=self.cwd, input=data, capture_output=True,
                               timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, b"", str(exc)
        return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace").strip()

    # ---- paths -----------------------------------------------------------
    def rel(self, path):
        """Path inside the repository, the way git writes it; None when outside."""
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(self.start, path))
        try:
            r = os.path.relpath(full, os.path.realpath(self.top))
        except ValueError:
            return None
        return None if r.startswith("..") else r.replace("\\", "/")

    def abs(self, rel):
        return os.path.join(self.top, rel.replace("/", os.sep))

    # ---- reading ---------------------------------------------------------
    def unmerged(self):
        """Files a merge left in conflict: {path: {stage: blob id}} (1 base, 2 ours, 3 theirs)."""
        rc, out, err = self.run(["ls-files", "-u", "-z", "--full-name"])
        if rc != 0:
            raise ToolError("git could not list the conflicted files: %s" % err)
        files = {}
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            parts = meta.split()
            if len(parts) != 3:
                continue
            files.setdefault(path.decode("utf-8", "replace"), {})[int(parts[2])] = parts[1].decode("ascii")
        return files

    def blob(self, sha, path):
        """Content of a blob the way it would be in the working folder (line endings)."""
        rc, out, _ = self.run(["cat-file", "--filters", "--path=" + path, sha])
        if rc != 0:
            rc, out, err = self.run(["cat-file", "blob", sha])
            if rc != 0:
                raise ToolError("git could not read version %s of %s: %s" % (sha[:10], path, err))
        return out

    def at_head(self, path, rev="HEAD"):
        """Content of the committed version of a file, or None when there is none."""
        rc, out, _ = self.run(["cat-file", "--filters", "%s:%s" % (rev, path)])
        if rc != 0:
            rc, out, _ = self.run(["cat-file", "blob", "%s:%s" % (rev, path)])
        return out if rc == 0 else None

    def operation(self):
        """What git is in the middle of: merge, rebase, cherry-pick, revert or None."""
        return operation_in(self.gitdir)

    def head(self):
        """{"branch": ..., "commit": ...} of the current checkout; values may be None."""
        rc, out, _ = self.run(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.decode("utf-8", "replace").strip() if rc == 0 else None
        rc, out, _ = self.run(["rev-parse", "--short", "HEAD"])
        commit = out.decode("ascii", "replace").strip() if rc == 0 else None
        return {"branch": branch, "commit": commit}

    # ---- the merge driver ------------------------------------------------
    def install_driver(self, command):
        """Tell this clone to merge *.arxml with the harness. -> lines for the user."""
        lines = []
        for key, value in (("merge.ecuh.name", "ECU Harness merge of ARXML by element"),
                           ("merge.ecuh.driver", command)):
            rc, _, err = self.run(["config", "--local", key, value])
            if rc != 0:
                raise ToolError("git config failed: %s" % err)
        lines.append("set merge.ecuh.driver in this clone's git config")
        attr = os.path.join(self.top, ".gitattributes")
        old = ""
        if os.path.isfile(attr):
            with open(attr, "r", encoding="utf-8", errors="replace", newline="") as fh:
                old = fh.read()
        if not any(ln.split() == ["*.arxml", "merge=ecuh"] for ln in old.splitlines()):
            nl = "\r\n" if "\r\n" in old else "\n"
            with open(attr, "w", encoding="utf-8", newline="") as fh:
                fh.write((old.rstrip("\r\n") + nl if old.strip() else "") + "*.arxml merge=ecuh" + nl)
            lines.append("added '*.arxml merge=ecuh' to .gitattributes (commit this file so the team gets it)")
        else:
            lines.append(".gitattributes already names the driver")
        return lines

    def remove_driver(self):
        self.run(["config", "--local", "--remove-section", "merge.ecuh"])
        return ["removed merge.ecuh from this clone's git config; the line in .gitattributes is harmless "
                "without it (git then merges as text)"]

    def driver_installed(self):
        return bool(self.driver_setting())

    def driver_setting(self):
        """The merge driver command set in this clone, or None."""
        rc, out, _ = self.run(["config", "--local", "--get", "merge.ecuh.driver"])
        return out.decode("utf-8", "replace").strip() or None if rc == 0 else None


def text_merge(ours, base, theirs):
    """Git's own line merge, writing conflict markers into ``ours``. -> exit code."""
    exe = find_git()
    if not exe:
        return 1
    try:
        r = subprocess.run([exe, "merge-file", "-L", "ours", "-L", "base", "-L", "theirs", ours, base, theirs],
                           capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 1 if r.returncode else 0
