"""PID 1 of the sandbox: reaps orphaned children, and exits at the run's hard
deadline. The container is started with --rm, so if the controller dies mid-run
the sandbox still stops and is removed by Docker on its own."""
import os
import signal
import sys
import time

deadline = time.monotonic() + float(sys.argv[1]) if len(sys.argv) > 1 else None
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
while True:
    if deadline is not None and time.monotonic() > deadline:
        os._exit(0)
    try:
        pid, _ = os.waitpid(-1, os.WNOHANG)
        if pid:
            continue
    except ChildProcessError:
        pass
    time.sleep(0.5)
