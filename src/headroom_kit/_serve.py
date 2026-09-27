"""Private proxy server. Not a console script."""

import sys

from headroom_kit.proxy import serve_main

if __name__ == "__main__":
    sys.exit(serve_main())
