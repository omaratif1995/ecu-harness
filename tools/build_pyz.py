"""Build dist/ecuh.pyz: the whole harness as one file.

For PCs where nothing may be installed: copy the file, then run
`python ecuh.pyz init` in the project folder. Needs only Python 3.8+.
"""
import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(out=None):
    out = out or os.path.join(ROOT, "dist", "ecuh.pyz")
    stage = tempfile.mkdtemp(prefix="ecuh_pyz_")
    try:
        shutil.copytree(os.path.join(ROOT, "src", "ecuharness"), os.path.join(stage, "ecuharness"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(stage, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\n\nif sys.version_info < (3, 8):\n"
                     "    sys.exit('ECU Harness needs Python 3.8 or newer; this is Python %d.%d.'"
                     " % sys.version_info[:2])\n\nfrom ecuharness.cli import main\n\nsys.exit(main())\n")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        zipapp.create_archive(stage, out, compressed=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return out


if __name__ == "__main__":
    path = build(sys.argv[1] if len(sys.argv) > 1 else None)
    print("%s  (%d kB)" % (path, os.path.getsize(path) // 1024))
