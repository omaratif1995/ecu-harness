import sys

if sys.version_info < (3, 8):
    sys.exit("ECU Harness needs Python 3.8 or newer; this is Python %d.%d." % sys.version_info[:2])

from ecuharness.cli import main  # noqa: E402

sys.exit(main())
