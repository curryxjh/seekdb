# MiniVectorDB requirement ledger

Baseline checked on 2026-11-09 (Asia/Shanghai):
`upstream/2026-competition-round2` =
`9f6df2ba5acf871376e22b64a3d0724e4b575044`.
Public manifest: `minivectordb-public-v4`, 20 cases, 95 SQL statements.

| Requirement | Source | Implementation | Verification | Status |
| --- | --- | --- | --- | --- |
| Own storage, SQL, transaction and vector execution | supplied requirements; competition description | `minivectordb/` | source audit | implemented, no external engine |
| SQL CRUD, signed BIGINT, Unicode VARCHAR, float32 VECTOR | public cases + supplied requirements | parser, executor, types | public runner + independent Python oracle | LOCAL PASS: 20 cases / 95 SQL, boundaries and 5 seeds |
| Atomic statements, read own writes, multi-table commit | public transaction cases + supplied requirements | COW page roots and atomic catalog | regression, differential and fault tests | LOCAL PASS, including failed late INSERT and index mutations |
| Acknowledged commits survive SIGKILL; repeatable recovery | public README + supplied requirements | data fsync → catalog fsync → rename → directory fsync | deterministic process kill at publication boundaries | LOCAL PASS: 23 kill scenarios, 36 recovery kills, 18 repeated starts |
| Data exceeds memory, including transactions and sorting | public README + supplied requirements | disk pages, bounded cache and external runs | Linux cgroup test, swap disabled | PASS: 143434768-byte committed file in 96 MiB, 40000 rows |
| Exact filtered TopK; deterministic ties; live index maintenance | public vector cases + supplied requirements | scan, bounded selection, disk index | independent float32 truth, mutation tests | LOCAL PASS: 375 sweep checks; three seeds per benchmark, both recall groups 1.0 |
| Approximate acceleration | supplied requirements; frozen requirements unavailable | ann uses exact fallback, physical postings on explicit CREATE INDEX | measured exact comparison | no sublinear ANN claim; index resource tradeoff documented |
| Container foreground, stdin/stdout, no runtime network, `/data` | supplied requirements; competition description | `Dockerfile`, local adapter | Linux amd64 container end to end | PASS on Docker Desktop emulation; formal adapter pending |
| Formal framing, ready marker, error codes, mode switching | public README explicitly delegates to organizer driver | isolated local adapter | organizer driver | **not supplied / unverified** |
| Frozen limits, timeouts, baseline and physical I/O device | public README delegates frozen configuration | benchmark scripts | organizer environment | **not supplied / unverified** |
| Complete 29-case admission and both ranking results | public README + platform | platform | authorized formal submission | **not run** |
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
* Formal platform submission remains unauthorized. The conflicting submission
  frequency/default selection wording does not authorize a platform action.
* Public `.result` JSON is an assertion format, not a wire protocol. The local
  adapter and its tests must not be described as a frozen organizer protocol.

## First vertical experiment

Create and populate a table. Begin a transaction, update one row successfully,
then execute an INSERT whose last row has a duplicate key. Query the table,
commit, restart, and verify only the earlier update survived. Also repeat with
ROLLBACK. These checks passed in the public transaction cases and the independent
regression suite; the successful statements remain, the failed statement's rows
are absent, and ROLLBACK restores the prior snapshot.

## Remaining admission and scoring gaps

The current formal driver, frozen limits/baseline and platform result are absent.
Neither 29/29 admission nor any ranking/80-point result is claimed. Formal platform
submission has not been authorized or attempted. The learning artifact includes
engineering evidence, but no student explanation, prediction, correction or
transfer experiment was supplied; Agent work is not substituted for it.

All four supplied rule pages were read during this session. The TopicDetails
performance page states one shared submission for both rankings. No submission is
performed, so older wording about daily frequency/default version selection does
not affect an action. The user's explicit fork/any-branch instruction governs
this Git delivery.
