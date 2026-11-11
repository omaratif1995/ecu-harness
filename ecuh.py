#!/usr/bin/env python3
"""Run ECU Harness straight from this folder, nothing to install:

    python ecuh.py init        (in your project folder: python <path>/ecuh.py init)
"""
import os
import sys

if sys.version_info < (3, 8):
    sys.exit("ECU Harness needs Python 3.8 or newer; this is Python %d.%d." % sys.version_info[:2])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ecuharness.cli import main  # noqa: E402

sys.exit(main())
