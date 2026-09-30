"""`python -m lightrelay` runs the relay or the inbox, chosen by ROLE."""

import os

role = os.environ.get("ROLE", "relay").strip()
if role == "relay":
    from .relay import main
elif role == "inbox":
    from .inbox import main
else:
    raise SystemExit(f"ROLE must be 'relay' or 'inbox', not {role!r}")

main()
