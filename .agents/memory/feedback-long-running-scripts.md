---
name: feedback-long-running-scripts
description: "The user runs long corpus scripts in their own terminal and requires live progress logging, not silent background tasks"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 582004de-6866-4fec-9c55-eb646cc5a738
  modified: 2026-09-10T08:15:17.636Z
---

Long-running scripts over `raw_data/` (corpus validation runs, batch conversions) must print
**live progress**, and the user prefers to run them in their own terminal rather than have them
run as a background task.

**Why:** on 2026-09-10 a background corpus run produced no visible output for many minutes —
Python fully buffers stdout when piped, so `print` progress lines never surfaced, and the run
looked hung. The user asked for the logs, then asked to stop it and run it themselves, then asked
explicitly for progress logging "so I can know at least something is happening". The full run
takes ~75 minutes, and rate varies 3-180 files/s depending on file size, so an apparently frozen
run is indistinguishable from a real hang without a heartbeat.

**How to apply:** when writing any script that iterates the corpus:
- Heartbeat every N files **scanned**, not every N files *processed* — most of the corpus is not
  in any one route, so gating on processed-count leaves long silent stretches.
- Include elapsed time, files/sec, estimated time remaining, running tallies (ok/failed/errors)
  and the current file path in each heartbeat line.
- Print failures and exceptions inline as they happen, not only in the final report.
- Pass `flush=True` on every print — do not rely on the caller remembering `python -u`.
- Hand the user the exact command to run rather than launching it in the background, and write
  the final report to a file as well as stdout.

Related: [[session-handoff]], [[working-rules]].
