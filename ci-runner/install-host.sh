#!/bin/sh
# Only for the Beelink's updater from before install_host.py ran by its own path, which runs this
# once from the new checkout; delete it once the Beelink has installed that.
exec /usr/bin/python3 -I "$(dirname "$0")/install_host.py" "$@"
