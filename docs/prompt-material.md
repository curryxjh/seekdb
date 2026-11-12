# Agent Prompt material and performance merge

On 2026-11-12 this branch merges `minivectordb-round2` commit
`12b8554683f8f3208e46d74ccf048d76a592c102` into the Prompt branch at
`9043810731ed75090b8305521b4e00b692e42f5c`. The optimized source and its
new tests, reports and documentation are taken unchanged from that commit.
Both parent histories remain available in the merge commit.

The evaluated artifact remains **`agent/conversation.json`**. Its `summary`
and four `evidence_guide` entries retain the Prompt branch's navigation to
embedded messages and outputs. Current metadata, source identity, 47 experiments,
101 artifact hashes and reported historical platform scores come from the new
performance baseline. Historical scores belong to the original evaluated commit;
they are not measurements of the merge. No platform evaluation was initiated.

## Conflict resolution

- Keep all 16 messages from the new baseline, followed by the two messages
  previously added by the Prompt branch and the actual merge instruction.
  Existing upstream message indices and experiment references remain valid.
- Keep all 47 upstream experiments and their output strings exactly. Repeated
  environment/limitation/scope/working-directory strings are stored once in
  `contexts`, with RFC6901 arrays named `<field>_refs` in each affected record.
  Expanding these pointers reconstructs the original records exactly. The
  references and strings are embedded within the sole evaluated JSON file.
- Keep the new baseline's engineering decisions, single learning unit,
  unanswered opportunities, limitations and historical excerpts. Add the Prompt
  branch's exact excerpts of the user challenge and handshake error.
- Shorten Agent-authored summary text and revision notes to satisfy 128 KiB.
  They remain explicitly attributed retrospective descriptions, not fabricated
  historical prompt transcripts or student technical answers. Earlier wording
  and reports remain recoverable from the Prompt parent.

`tests/prompt_material.py` now compares against `12b8554`. It expands shared
context and verifies record equality, all original metadata except the destination
branch, source identity, strict UTF-8 JSON, finite numbers, exact quotations and
RFC6901 references. `tests/evidence.py` checks every historical artifact hash and
final-source digest. Local reports are in `docs/results/prompt-material-check.json`
and `docs/results/prompt-native-check.json`. Previous reports are preserved with
`-20261111` suffixes and describe only their original source revision.

## Reproduce

Use Python 3.9+ and a clean LF export. On Windows override `core.autocrlf`, which
also affects `git archive`. Do not include working-tree Python caches in the
source directory: the inherited digest routine reads every immediate entry.

```text
git -c core.autocrlf=false archive --format=zip --output=CHECKOUT.zip HEAD minivectordb tests docs Dockerfile build.sh CMakeLists.txt README.md protocol_check mysqltest STDIO_PROTOCOL.md agent conversation.json
```

Save the baseline using a binary-safe API, then extract the archive and run the
checker from its root. PowerShell redirection may transcode the original bytes.

```python
import pathlib, subprocess
pathlib.Path("BASELINE.json").write_bytes(subprocess.check_output([
    "git", "show",
    "12b8554683f8f3208e46d74ccf048d76a592c102:agent/conversation.json"
]))
```

```text
python tests/prompt_material.py --self-test --baseline /absolute/path/BASELINE.json --report /absolute/path/material-check.json
```

The checker computes the material SHA256 outside the JSON to avoid self-reference.
These checks validate packaging and preservation; they do not predict a score.
The empty root `conversation.json` remains an inherited placeholder.
