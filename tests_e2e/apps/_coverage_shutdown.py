"""Installs a SIGTERM handler that saves coverage data before exiting.

Only matters when this app is launched under `coverage run
--parallel-mode` (see tests_e2e/conftest.py), to measure real coverage
from the e2e suite's subprocess-launched scenario apps rather than just
the main pytest process. Needed because uvicorn's own shutdown sequence
(uvicorn.server.Server.capture_signals) restores whatever SIGTERM handler
was installed *before* it ran, then re-raises the signal through that
restored handler once graceful shutdown finishes. With no handler of our
own, that's Python's default disposition -- the OS kills the process
outright, and atexit (how coverage.py normally flushes parallel-mode
data) never runs, even though the graceful shutdown logs ("Finished
server process") make it look like a clean exit.

Installing our own handler first means uvicorn restores *this* handler
instead of the default, and this one saves coverage before actually
exiting -- via sys.exit(), a real Python exit that does run atexit,
rather than signal.raise_signal()'s OS-level kill.

Call install() before app.run()/uvicorn.run() -- it has to be in place
before uvicorn's Server.capture_signals() captures "the handler before
it" as the one to restore.
"""

import signal
import sys


def install():
    try:
        import coverage
    except ImportError:
        return

    def _save_and_exit(signum, frame):
        cov = coverage.Coverage.current()
        if cov is not None:
            cov.stop()
            cov.save()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _save_and_exit)
