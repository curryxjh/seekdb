# Evidence status

Only `verified-v3/` contains the corrected A/B evaluation for the current change.

Files directly in this directory were produced during an earlier run whose
external launcher ignored the requested `BIN`. They do **not** establish an
original/candidate performance comparison or SQL equivalence. They are retained
for audit history; the old `summary.json`, `manifest.json`, and `correctness.json`
are explicitly marked invalid. The previous 0.97% improvement claim is withdrawn.

The official benchmark, data generator, SQL, scorer, and baseline were not changed.
