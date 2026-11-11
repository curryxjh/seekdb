# MiniVectorDB requirement ledger

Initial baseline checked on 2026-11-09: `9f6df2ba5acf871376e22b64a3d0724e4b575044`.
The official protocol and driver were added upstream on 2026-11-10 at
`f4d4e26653af823a7601668802f80e374ad6643c` and merged unchanged. Public manifest:
`minivectordb-public-v4`, 20 cases, 95 SQL statements.

| Requirement | Source | Implementation | Verification | Status |
| --- | --- | --- | --- | --- |
| Own storage, SQL, transaction and vector execution | supplied requirements; competition description | `minivectordb/` | source audit | implemented, no external engine; borrowed scan fields and packed append splits reverified |
| SQL CRUD, signed BIGINT, Unicode VARCHAR, float32 VECTOR | public cases + supplied requirements | parser, executor, types | public runner + independent Python oracle | LOCAL PASS: 20 cases / 95 SQL, boundaries and 5 seeds |
| Atomic statements, read own writes, multi-table commit | public transaction cases + supplied requirements | COW page roots and atomic catalog | regression, differential and fault tests | LOCAL PASS, including failed late INSERT and index mutations |
| Acknowledged commits survive SIGKILL; repeatable recovery | public README + supplied requirements | data fsync → catalog fsync → rename → directory fsync | deterministic process kill at publication boundaries | LOCAL PASS: 23 kill scenarios, 36 recovery kills, 18 repeated starts |
| Data exceeds memory, including transactions and sorting | public README + supplied requirements | disk pages, bounded cache and external runs | Linux cgroup test, swap disabled | PASS: 142734352-byte committed file in 96 MiB, 40000 rows |
| Exact filtered TopK; deterministic ties; live index maintenance | public vector cases + supplied requirements | scan, bounded selection, disk index | independent float32 truth, mutation tests | LOCAL PASS: 375 sweep checks; three seeds per benchmark, both recall groups 1.0 |
| Approximate acceleration | supplied requirements; frozen requirements unavailable | ann uses exact fallback, physical postings on explicit CREATE INDEX | measured exact comparison | no sublinear ANN claim; index resource tradeoff documented |
| Container foreground, stdin/stdout, no runtime network, `/data` | supplied requirements; competition description | `Dockerfile`, official adapter | Linux amd64 container end to end | PASS on Docker Desktop emulation |
| Formal JSONL framing, ready marker, errors, mode switching and shutdown | `STDIO_PROTOCOL.md`, official `protocol_check/` | `protocol.*`, `main.cpp` | unchanged official driver, native and Docker | **PASS: 20/20, 95 SQL** |
| Frozen limits, timeouts, baseline and physical I/O device | public README delegates frozen configuration | benchmark scripts | organizer environment | **not supplied / unverified** |
| Complete 29-case admission and both ranking results | public README + platform | platform | user-reported submission, authenticated record inspected | **FAIL admission at 3ccc14e; both ranked evaluations skipped; no case logs returned** |
| One authentic learning artifact ≤128 KiB | supplied `req.md` | `agent/conversation.json` | `tests/evidence.py` | strict self-check; no student technical-learning contribution claimed |
| Same revision for source, container recipe and evidence | supplied `req.md` | final Git commit | clean exported build and remote SHA check | delivery checks recorded in validation report |

The documents are task references, not evidence that a student performed their
embedded plans. Only actual user utterances are student contributions.

Conflicts resolved for this work:

* The supplied older `/Volumes/expand/...` checkout is not the current workspace.
  Development uses an isolated sibling worktree based on the verified upstream.
* Current AGENTS guidance forbids the `codex/` prefix: use `minivectordb-round2`.
* The user's latest instruction permits any branch in the registered fork:
  deliver `minivectordb-round2` without force push.
* Maintain the one active learning file at `agent/conversation.json`; the upstream
  empty root placeholder is not a second evidence artifact.
* The user supplied a failed platform record for the delivered commit. The agent
  inspected it without submitting again. No authorization for a retry is inferred.
* Public `.result` JSON is an assertion format, while `STDIO_PROTOCOL.md` and
  `protocol_check/` define the released wire protocol used by this revision.

## First vertical experiment

Create and populate a table. Begin a transaction, update one row successfully,
then execute an INSERT whose last row has a duplicate key. Query the table,
commit, restart, and verify only the earlier update survived. Also repeat with
ROLLBACK. These checks passed in the public transaction cases and the independent
regression suite; the successful statements remain, the failed statement's rows
are absent, and ROLLBACK restores the prior snapshot.

## Latest performance iteration

Final core cf294bbc8a664988ef11b5884d35ae57c1dd12cb passed native, ASan/UBSan,
Linux fault-build and production-container checks. Each of three 66002-row key
orders covers multi-level splits, eviction, rollback, failed statements and
SIGKILL/restart. All commands and hashes are in `docs/results/opt-final-tests.json`.
The three-metric LOCAL comparison pairs entire rounds before taking the median;
it retains an unfavorable spool experiment and its withdrawal. Official E,
rankings and student learning scores remain unverified.

## Remaining admission and scoring gaps

The official driver exposed that the delivered revision used an incompatible
custom transport. Its handshake failure is retained in
`results/protocol-before/result.json`; the official driver and all 20 public cases
pass on the corrected native build and Docker image. The original platform record
has not been resubmitted. The nine unpublished cases, platform admission and
ranking remain unverified. See [platform investigation](platform-failure.md).

All four supplied rule pages were read during this session. The TopicDetails
performance page states one shared submission for both rankings. The agent did
not submit or retry; older wording about daily frequency/default version selection
does not authorize that action. The user's explicit fork/any-branch instruction
governs this Git delivery.
