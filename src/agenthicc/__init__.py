"""agenthicc — an agent runtime built around lauren-ai."""

import time

#: Monotonic time at which this process first imported agenthicc. Start-up
#: diagnostics measure the first TUI frame from here (AGX-02).
STARTED_AT: float = time.monotonic()
