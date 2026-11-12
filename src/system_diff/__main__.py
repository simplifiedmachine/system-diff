import sys

if sys.argv[1:2] == ["progress"]:   # asked every second while the pane waits: answered without loading the rest
    from pathlib import Path

    from .progress import line
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if sys.argv[2:3] == ["--wait"] and len(sys.argv) > 3:   # the pane asks again only after this: its pace
        import time
        time.sleep(min(10.0, float(sys.argv[3])))
    print(line(Path.cwd()))
    sys.exit(0)

if "panel" in sys.argv[1:4]:   # what the pane runs at every look: the engine answers, without loading the CLI
    from .panel import fast
    code = fast(sys.argv[1:])
    if code is not None:
        sys.exit(code)

from .cli import main

sys.exit(main())
