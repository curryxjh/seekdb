# Verified final candidate

`summary.json` contains all four runs in ABBA order. The original is the kernel
at `9eb94e4ca`; the candidate includes this submission's FTS changes. Each
`identity-*.json` records the actual `/proc/<pid>/exe` and executable SHA256.
`manifest.json` ties those hashes to the normalized sources and unchanged
official benchmark files. Both executables contain the kernel statically.

The official scorer's local 100-point output applies its repository baseline,
which also gives the original 100 points on this machine. It does not establish
100 points in the competition. The meaningful local comparison is the 9.01%
overall improvement in `summary.json`.

`correctness.json` records the complete SQL differential and the unchanged five
mysqltest failures. They are not counted as passing. `share-tests.log` contains
two FTS normalizer tests and four existing AI schema tests.

The query/tokenizer perf summaries describe the earlier, source-verified
candidate recorded in `identity-v2-previous.json`; the LIMIT profile describes
the original. They locate work to optimize and are not timed score runs.
Trailing display padding in the perf and EXPLAIN summaries was stripped.

The helper scripts are copies of the external local harness. Their absolute
paths and runtime/resource choices describe this WSL host and must be adapted
for another host. The official benchmark scripts remain in `tools/benchmark/`.
The built-in source revision and report `git_head` come from local build/test
copies, so source and binary hashes are recorded explicitly.
