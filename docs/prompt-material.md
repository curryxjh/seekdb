# Agent Prompt material revision

This branch starts at `minivectordb-round2`, commit
`b3b16af1e8429b3daed14bbdfedc89bf67c6e2a8`. The user reports that branch
receives an Agent Prompt score; the numerical score and evaluated commit were
not supplied. Earlier failed admission records remain attached to their original
commits. This revision makes no platform submission or score claim.

The evaluated material is **`agent/conversation.json`**. Its embedded `summary`
and `evidence_guide` cover the four requested dimensions with RFC6901 references
to embedded messages and experiment outputs. External documents are supporting
audit records, not required to understand those four entries. The empty root
`conversation.json` is an inherited placeholder.

## What changed

- Added a concise material summary, four evidence navigation entries, exact
  excerpts of the actual user challenge and protocol diagnosis, and current
  user/Agent provenance.
- Preserved all 14 inherited messages, all 43 experiments, the single existing
  learning unit, engineering decisions, unanswered opportunities, limitations,
  original excerpt entries and all 82 artifact hashes.
- Compacted formatting whitespace in 10 embedded JSON output strings. Their
  parsed values remain identical; no measurements, failures, experiment order,
  status, command or source identity were changed. Original bytes are recoverable
  from the baseline commit. `tests/prompt_material.py` checks these constraints.
- Distinguished the user's actual platform counterexample from Agent diagnosis,
  Agent technical reflections and tool measurements. The current summary is
  explicitly a retrospective, not a fabricated historical prompt transcript.
- Added strict checks for duplicate keys, nonfinite numbers including exponent
  overflow, size, UTF-8, RFC6901 array indices, exact quotations and preservation.
  The original evidence checker additionally verifies all file hashes and source
  digests. These checks validate the package, not its eventual score.

Database source, build recipe, official driver and SQL assertions are unchanged.
Historical experiments retain their original environments and dates; they were
not rerun or relabeled as Windows measurements.

## Reproduce validation

Use Python 3.9+ with its standard library. Work from the repository root and a
clean **LF** checkout/export: historical hashes identify Git blob bytes. On
Windows, `core.autocrlf=true` also affects `git archive`; use an explicit override.
Ignore local Python caches in the working tree, since the inherited source-digest
routine hashes every immediate entry in `minivectordb/`.

After committing, export the relevant paths using:

```text
git -c core.autocrlf=false archive --format=zip --output=CHECKOUT.zip HEAD minivectordb tests docs Dockerfile build.sh CMakeLists.txt README.md protocol_check mysqltest STDIO_PROTOCOL.md agent conversation.json
```

Extract into a new directory. Save the original baseline JSON outside that export
using a binary-safe API (PowerShell redirection may transcode it):

```python
import pathlib, subprocess
pathlib.Path("BASELINE.json").write_bytes(subprocess.check_output([
    "git", "show",
    "b3b16af1e8429b3daed14bbdfedc89bf67c6e2a8:agent/conversation.json"
]))
```

From the exported root, run:

```text
python tests/prompt_material.py --self-test --baseline /absolute/path/BASELINE.json --report /absolute/path/material-check.json
```

The report computes the final material's SHA256 outside the material to avoid
self-reference. The committed local results are in
`docs/results/prompt-material-check.json`; the official native protocol smoke
check is recorded separately. Neither report estimates a score or declares
hidden platform tests passed. Adding a summary locally cannot guarantee that
the platform will produce its own evaluation summary.
