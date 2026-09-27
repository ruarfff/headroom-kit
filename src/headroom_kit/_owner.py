"""Private detached owner. Not a console script."""

import signal
import sys

from headroom_kit.proxy import owner_main
from headroom_kit.runtime import stop

if __name__ == "__main__":
    # The owner is a new session. Without these, SIGTERM/SIGHUP skip cleanup
    # and the proxy child keeps the port and control socket.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    sys.exit(owner_main())
