"""Generate a contest-shaped synthetic dataset with exact brute-force GT.

Mirrors the real evaluation sets: n ~250k, dim 128, nq=100 queries, exact
top-100 ground truth. Clustered rather than uniform because uniform random
vectors in 128 dims are unrealistically easy for graph indexes (everything is
nearly equidistant); real embedding data has structure that makes Recall@100
genuinely hard, which is exactly the regime where niter starts to matter.
"""
import argparse
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=250000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--nq", type=int, default=100)
    ap.add_argument("--k", type=int, default=100)
    ap.add_argument("--clusters", type=int, default=256)
    ap.add_argument("--out", default="/workspace/datasets")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    rs = np.random.RandomState(a.seed)
    # anisotropic Gaussian mixture: cluster centres on a sphere, tight-ish spread
    centres = rs.randn(a.clusters, a.dim).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    assign = rs.randint(0, a.clusters, size=a.n)
    base = centres[assign] + rs.randn(a.n, a.dim).astype(np.float32) * 0.35
    base = base.astype(np.float32)

    qassign = rs.randint(0, a.clusters, size=a.nq)
    query = centres[qassign] + rs.randn(a.nq, a.dim).astype(np.float32) * 0.35
    query = query.astype(np.float32)

    # exact L2 ground truth, chunked to bound memory
    gt = np.empty((a.nq, a.k), dtype=np.int32)
    base_sq = np.einsum("ij,ij->i", base, base)
    for i in range(a.nq):
        d = base_sq - 2.0 * (base @ query[i]) + float(query[i] @ query[i])
        gt[i] = np.argpartition(d, a.k)[:a.k][np.argsort(d[np.argpartition(d, a.k)[:a.k]])]

    base.tofile(f"{a.out}/synthetic_base.f32")
    query.tofile(f"{a.out}/synthetic_query.f32")
    gt.tofile(f"{a.out}/synthetic_gt.i32")
    print(f"wrote n={a.n} dim={a.dim} nq={a.nq} k={a.k} clusters={a.clusters} -> {a.out}")


if __name__ == "__main__":
    main()
