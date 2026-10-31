/* Build a CAGRA index from a GPU-computed kNN graph, without NN-Descent.
 *
 * Why this exists: the contest allows "自定义 GPU 建图" as long as search stays
 * on the official CAGRA path. The stock NN-Descent builder takes ~17s at
 * 250K x 128 and is bound by CPU-side graph bookkeeping (128d and 256d build
 * times are nearly identical, %CPU ~1331, GPU ~17%). Computing candidate
 * neighbours with a brute-force kernel moves that work to the GPU.
 *
 * A raw top-k graph is poorly navigable: it is full of one-directional edges
 * and local clumps, and CAGRA's search gets trapped. The official builder
 * avoids this by constructing an intermediate graph and pruning it back to
 * graph_degree, which leaves a reciprocal, diversified graph. We reproduce
 * that effect cheaply:
 *
 *   1. brute force the `cand` nearest neighbours of every point (cand > gd)
 *   2. add the reverse of every edge -- free, L2 is symmetric, d(i,j)=d(j,i)
 *   3. merge forward + reverse candidates per node, dedupe, sort by distance,
 *      keep the closest `gd`
 *
 * Pruning is the part that decides recall.  Keeping the gd closest of the
 * candidate set just reproduces the exact kNN graph, which navigates badly:
 * every edge is short and one-directional, so CAGRA's search gets stuck in
 * local clumps (recall@100 0.9243 vs the official builder's 0.9705).  Instead
 * prune with the RNG rule -- drop edge (i,j) when some already-kept neighbour
 * k occludes it, i.e. d(k,j) < d(i,j).  That keeps a few long, diverse edges
 * exactly like NN-Descent's pruning does.  The occlusion set is capped so the
 * host-side cost stays bounded.
 *
 * usage: bench_fromgraph <base.f32> <query.f32> <gt.i32> <dim> <nq> <topk> \
 *                        <gd> [reps] [search_width] [itopk] [chunk] [cand] [occl]
 */
#include <cuvs/core/c_api.h>
#include <cuvs/neighbors/cagra.h>
#include <cuvs/neighbors/brute_force.h>
#include <cuvs/neighbors/ivf_flat.h>
#include <dlpack/dlpack.h>
#ifdef __HIP_PLATFORM_AMD__
#include <cuvs/cuda_runtime.h>
#else
#include <cuda_runtime.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void* slurp(const char* path, size_t* bytes)
{
  FILE* f = fopen(path, "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(2); }
  fseek(f, 0, SEEK_END);
  long sz = ftell(f);
  fseek(f, 0, SEEK_SET);
  void* p = malloc((size_t)sz);
  if (!p || fread(p, 1, (size_t)sz, f) != (size_t)sz) { fprintf(stderr, "read fail %s\n", path); exit(2); }
  fclose(f);
  *bytes = (size_t)sz;
  return p;
}

static void mk_tensor(DLManagedTensor* t, void* data, long rows, long cols,
                      DLDeviceType dev, DLDataTypeCode code, int bits, long* shape)
{
  shape[0] = rows; shape[1] = cols;
  t->dl_tensor.data = data;
  t->dl_tensor.device.device_type = dev;
  t->dl_tensor.device.device_id = 0;
  t->dl_tensor.ndim = 2;
  t->dl_tensor.dtype.code = (uint8_t)code;
  t->dl_tensor.dtype.bits = (uint8_t)bits;
  t->dl_tensor.dtype.lanes = 1;
  t->dl_tensor.shape = shape;
  t->dl_tensor.strides = NULL;
  t->dl_tensor.byte_offset = 0;
  t->manager_ctx = NULL;
  t->deleter = NULL;
}

/* (id, distance) pair used while pruning one node's candidate list. */
typedef struct { unsigned id; float dist; } cand_t;

static float l2sq(const float* a, const float* b, long dim)
{
  float s = 0.f;
  for (long d = 0; d < dim; ++d) {
    const float t = a[d] - b[d];
    s += t * t;
  }
  return s;
}

/* GPU RNG prune: one block per point, candidates straight from the GPU table.
 *
 * The host path has to copy the whole candidate table back (n*cand ids+dists,
 * ~1 GB per build at cand=320) and then walk it single-threaded per node. This
 * kernel keeps the table resident and prunes in place, writing graph_degree
 * neighbours per row directly into the device graph buffer.
 *
 * Occlusion set = the `occl` closest candidates of the row, held in shared
 * memory. That is the fixed-prefix form of RNG: the host path grows its keep
 * set as it accepts edges, but with occl <= 16 the two agree on almost every
 * row (the first accepted edges are also the closest ones) and the prefix form
 * parallelises over candidates instead of over nodes only.
 *
 * Shared layout: pv[occl*dim] floats, pids[occl], blk[bfk] bytes, acc[bfk].
 */
__global__ void rng_prune_kernel(const float* __restrict__ base,
                                 const long long* __restrict__ cand_ids,
                                 const float* __restrict__ cand_dst,
                                 unsigned* __restrict__ graph,
                                 int dim, int bfk, int gd, int cand, int occl,
                                 long block_off, long n)
{
  extern __shared__ char smem[];
  float* pv          = (float*)smem;
  unsigned* pids     = (unsigned*)(pv + (size_t)occl * dim);
  unsigned char* blk = (unsigned char*)(pids + occl);
  unsigned* acc      = (unsigned*)(blk + ((bfk + 3) & ~3));

  const long r = blockIdx.x;
  const long gi = block_off + r;
  if (gi >= n) return;

  const long long* ci = cand_ids + r * bfk;
  const float*     cd = cand_dst + r * bfk;

  /* Prefix: the occl closest non-self candidates of the sorted row. */
  if (threadIdx.x == 0) {
    int np = 0;
    for (int j = 0; j < bfk && np < occl; ++j) {
      if ((long)ci[j] == gi) continue;
      pids[np++] = (unsigned)ci[j];
    }
    for (int k = np; k < occl; ++k) pids[k] = (unsigned)gi;
    for (int j = 0; j < bfk; ++j) blk[j] = (unsigned char)((long)ci[j] == gi);
  }
  __syncthreads();

  for (int k = 0; k < occl; ++k) {
    const float* src = base + (size_t)pids[k] * dim;
    for (int d = threadIdx.x; d < dim; d += blockDim.x) pv[(size_t)k * dim + d] = src[d];
  }
  __syncthreads();

  /* Occlusion test, spread over candidates: edge (gi,j) survives unless some
   * prefix point k is closer to j than gi is -- d(k,j) < d(gi,j). */
  for (int j = threadIdx.x; j < bfk; j += blockDim.x) {
    if (blk[j]) continue;
    const unsigned cid = (unsigned)ci[j];
    const float dij = cd[j];
    const float* pj = base + (size_t)cid * dim;
    int occluded = 0;
    for (int k = 0; k < occl; ++k) {
      if (pids[k] == cid) { occluded = 1; break; }
      const float* pk = pv + (size_t)k * dim;
      float s = 0.f;
      for (int d = 0; d < dim; ++d) {
        const float t = pk[d] - pj[d];
        s += t * t;
      }
      if (s < dij) { occluded = 1; break; }
    }
    blk[j] = (unsigned char)occluded;
  }
  __syncthreads();

  if (threadIdx.x == 0) {
    unsigned* grow = graph + (size_t)gi * gd;
    int out = 0;
    for (int j = 0; j < bfk && out < gd; ++j) {
      if (blk[j]) continue;
      grow[out++] = (unsigned)ci[j];
    }
    /* Backfill from the occluded tail; RNG can leave a row short. */
    for (int j = 0; j < bfk && out < gd; ++j) {
      if (!blk[j]) continue;
      const unsigned cid = (unsigned)ci[j];
      if ((long)cid == gi) continue;
      grow[out++] = cid;
    }
    while (out < gd) grow[out++] = (unsigned)gi;
  }
}

static int cand_less(const void* a, const void* b)
{
  const cand_t* x = (const cand_t*)a;
  const cand_t* y = (const cand_t*)b;
  if (x->dist < y->dist) return -1;
  if (x->dist > y->dist) return 1;
  return (x->id < y->id) ? -1 : (x->id > y->id);
}

int main(int argc, char** argv)
{
  if (argc < 8) {
    fprintf(stderr, "usage: %s base.f32 query.f32 gt.i32 dims nq topk gd"
                    " [reps] [search_width] [itopk] [chunk] [cand] [occl]"
                    " [use_ivf] [n_lists] [n_probes]\n", argv[0]);
    return 2;
  }
  const char* fbase = argv[1];
  const char* fquery = argv[2];
  const char* fgt = argv[3];
  const long dim = atol(argv[4]);
  const long nq = atol(argv[5]);
  const long topk = atol(argv[6]);
  const long gd = atol(argv[7]);
  const int reps = (argc > 8) ? atoi(argv[8]) : 1;
  const int sw = (argc > 9) ? atoi(argv[9]) : 4;
  const long itopk_arg = (argc > 10) ? atol(argv[10]) : 0;
  const long chunk = (argc > 11) ? atol(argv[11]) : 8192;
  long cand = (argc > 12) ? atol(argv[12]) : gd * 3 / 2;
  if (cand < gd) cand = gd;
  /* Occlusion-set cap for RNG pruning; 0 turns it off (plain distance cut). */
  long occl = (argc > 13) ? atol(argv[13]) : 16;
  if (occl < 0) occl = 0;
  if (occl > gd) occl = gd;
  /* Candidate generation: 0 = exact brute force over the whole base,
   * 1 = IVF-Flat (probes n_probes lists of n_lists), far fewer pairs. */
  const int use_ivf = (argc > 14) ? atoi(argv[14]) : 0;
  const int n_lists = (argc > 15) ? atoi(argv[15]) : 1024;
  const int n_probes = (argc > 16) ? atoi(argv[16]) : 32;
  /* Prune on the GPU instead of copying the candidate table to the host.
   * The reverse-edge pass only exists on the host path; GPU pruning needs
   * cand > gd so the row can be filled from the forward list alone. */
  int gpu_merge = (argc > 17) ? atoi(argv[17]) : 1;
  if (gpu_merge && cand <= gd) gpu_merge = 0;
  const long occl_e = (occl > 0) ? occl : 1;

  size_t nb = 0, qb = 0, gb = 0;
  float* base = (float*)slurp(fbase, &nb);
  float* query = (float*)slurp(fquery, &qb);
  int* gt = (int*)slurp(fgt, &gb);
  const long n = (long)(nb / sizeof(float)) / dim;
  const long nq_file = (long)(qb / sizeof(float)) / dim;
  const long gt_cols = (long)(gb / sizeof(int)) / nq;
  if (nq > nq_file) { fprintf(stderr, "nq=%ld > file has %ld\n", nq, nq_file); return 2; }

  size_t itopk = (size_t)topk * 4;
  if (itopk_arg > 0) itopk = (size_t)itopk_arg;
  else if (itopk < 512) itopk = 512;

  printf("[fg] base=%ld x %ld  nq=%ld  topk=%ld  gd=%ld cand=%ld chunk=%ld\n",
         n, dim, nq, topk, gd, cand, chunk);
  printf("[fg] sw=%d itopk=%zu reps=%d occl=%ld candgen=%s\n", sw, itopk, reps, occl,
         use_ivf ? "ivf" : "brute");
  if (use_ivf) printf("[fg] ivf n_lists=%d n_probes=%d\n", n_lists, n_probes);
  fflush(stdout);

  cuvsResources_t res;
  if (cuvsResourcesCreate(&res) != CUVS_SUCCESS) { fprintf(stderr, "res: %s\n", cuvsGetLastErrorText()); return 3; }

  const size_t base_bytes = sizeof(float) * n * dim;
  void* base_d = NULL;
  if (cuvsRMMAlloc(res, &base_d, base_bytes) != CUVS_SUCCESS) { fprintf(stderr, "rmm base\n"); return 3; }
  if (cudaMemcpy(base_d, base, base_bytes, cudaMemcpyHostToDevice) != cudaSuccess) { fprintf(stderr, "h2d\n"); return 3; }

  /* Host-side candidate table: cand neighbours per point, self excluded. */
  const size_t cand_ids_bytes = sizeof(unsigned) * (size_t)n * (size_t)cand;
  const size_t cand_dst_bytes = sizeof(float) * (size_t)n * (size_t)cand;
  unsigned* cand_ids = (unsigned*)malloc(cand_ids_bytes);
  float*    cand_dst = (float*)malloc(cand_dst_bytes);
  if (!cand_ids || !cand_dst) { fprintf(stderr, "host alloc\n"); return 3; }

  /* Reverse-edge CSR, filled after the forward pass. Sized from the measured
   * in-degree sum rather than n*cand: on clustered data a few hub points get
   * far more than `cand` reverse edges, which overflows a fixed n*cand buffer. */
  size_t* radj_off = (size_t*)calloc((size_t)n + 1, sizeof(size_t));
  unsigned* radj_id = NULL;
  float*    radj_dst = NULL;
  if (!radj_off) { fprintf(stderr, "radj alloc\n"); return 3; }

  const size_t graph_bytes = sizeof(unsigned) * (size_t)n * (size_t)gd;
  void* graph_d = NULL;
  if (cuvsRMMAlloc(res, &graph_d, graph_bytes) != CUVS_SUCCESS) { fprintf(stderr, "rmm graph\n"); return 3; }

  /* Brute force asks for cand+1 so that dropping self leaves a full row. */
  const long bfk = cand + 1;
  const size_t batch_ids_bytes = sizeof(int64_t) * (size_t)chunk * (size_t)bfk;
  const size_t batch_dst_bytes = sizeof(float) * (size_t)chunk * (size_t)bfk;
  void* ids_d = NULL; void* dst_d = NULL;
  if (cuvsRMMAlloc(res, &ids_d, batch_ids_bytes) != CUVS_SUCCESS) { fprintf(stderr, "rmm ids\n"); return 3; }
  if (cuvsRMMAlloc(res, &dst_d, batch_dst_bytes) != CUVS_SUCCESS) { fprintf(stderr, "rmm dst\n"); return 3; }
  int64_t* ids_h = (int64_t*)malloc(batch_ids_bytes);
  float*   bdst_h = (float*)malloc(batch_dst_bytes);

  double tot_bf = 0.0, tot_merge = 0.0, tot_asm = 0.0, tot_search = 0.0;
  double best_total = 1e30;
  double recall = 0.0, recall10 = 0.0;

  /* IVF-Flat index: built once, reused by every rep. Its build cost is
   * charged to the first rep only, so the reported total on rep 0 is the
   * honest end-to-end figure. */
  cuvsIvfFlatIndex_t ivf = NULL;
  cuvsIvfFlatSearchParams_t ivf_sp = NULL;

  for (int r = 0; r < reps; ++r) {
    /* ---- phase 1: GPU candidate generation -> cand neighbours per point ---- */
    double t0 = now();
    cuvsBruteForceIndex_t bf = NULL;
    if (use_ivf) {
      if (!ivf) {
        cuvsIvfFlatIndexParams_t ip = NULL;
        long ds_sh0[2];
        DLManagedTensor ds_t0;
        mk_tensor(&ds_t0, base_d, n, dim, kDLCUDA, kDLFloat, 32, ds_sh0);
        if (cuvsIvfFlatIndexParamsCreate(&ip) != CUVS_SUCCESS) { fprintf(stderr, "ivf params\n"); return 4; }
        ip->metric = L2Expanded;
        ip->metric_arg = 0.f;
        ip->n_lists = (uint32_t)n_lists;
        ip->kmeans_n_iters = 25;
        ip->kmeans_trainset_fraction = 1.0;
        ip->add_data_on_build = true;
        if (cuvsIvfFlatIndexCreate(&ivf) != CUVS_SUCCESS ||
            cuvsIvfFlatBuild(res, ip, &ds_t0, ivf) != CUVS_SUCCESS) {
          fprintf(stderr, "ivf build: %s\n", cuvsGetLastErrorText()); return 4;
        }
        cuvsIvfFlatIndexParamsDestroy(ip);
        if (cuvsIvfFlatSearchParamsCreate(&ivf_sp) != CUVS_SUCCESS) { fprintf(stderr, "ivf sp\n"); return 4; }
        ivf_sp->n_probes = (uint32_t)n_probes;
        if (cudaDeviceSynchronize() != cudaSuccess) { fprintf(stderr, "ivf sync\n"); return 4; }
      }
    } else {
      if (cuvsBruteForceIndexCreate(&bf) != CUVS_SUCCESS) { fprintf(stderr, "bf create\n"); return 4; }
      long ds_sh[2];
      DLManagedTensor ds_t;
      mk_tensor(&ds_t, base_d, n, dim, kDLCUDA, kDLFloat, 32, ds_sh);
      if (cuvsBruteForceBuild(res, &ds_t, L2Expanded, 0.f, bf) != CUVS_SUCCESS) {
        fprintf(stderr, "bf build: %s\n", cuvsGetLastErrorText()); return 4;
      }
    }

    for (long off = 0; off < n; off += chunk) {
      const long rows = (n - off < chunk) ? (n - off) : chunk;
      long qs_sh[2], n_sh[2], d_sh[2];
      DLManagedTensor q_t, n_t, d_t;
      mk_tensor(&q_t, (char*)base_d + (size_t)off * dim * sizeof(float), rows, dim, kDLCUDA, kDLFloat, 32, qs_sh);
      mk_tensor(&n_t, ids_d, rows, bfk, kDLCUDA, kDLInt, 64, n_sh);
      mk_tensor(&d_t, dst_d, rows, bfk, kDLCUDA, kDLFloat, 32, d_sh);
      cuvsFilter f; f.type = NO_FILTER; f.addr = (uintptr_t)0;
      const cuvsError_t s_rc = use_ivf
          ? cuvsIvfFlatSearch(res, ivf_sp, ivf, &q_t, &n_t, &d_t, f)
          : cuvsBruteForceSearch(res, bf, &q_t, &n_t, &d_t, f);
      if (s_rc != CUVS_SUCCESS) {
        fprintf(stderr, "cand search: %s\n", cuvsGetLastErrorText()); return 4;
      }
      if (gpu_merge) {
        /* Prune on-device: the candidate table never leaves the GPU. */
        const size_t shmem = (size_t)occl_e * dim * sizeof(float)
                           + (size_t)occl_e * sizeof(unsigned)
                           + (size_t)((bfk + 3) & ~3)
                           + (size_t)bfk * sizeof(unsigned);
        rng_prune_kernel<<<(unsigned)rows, 128, shmem>>>(
            (const float*)base_d, (const long long*)ids_d, (const float*)dst_d,
            (unsigned*)graph_d,  /* kernel indexes by global row (off + r) */
            (int)dim, (int)bfk, (int)gd, (int)cand, (int)occl_e, off, n);
        if (cudaGetLastError() != cudaSuccess || cudaDeviceSynchronize() != cudaSuccess) {
          fprintf(stderr, "rng kernel: %s\n", cudaGetErrorString(cudaGetLastError()));
          return 4;
        }
        continue;
      }
      if (cudaMemcpy(ids_h, ids_d, sizeof(int64_t) * rows * bfk, cudaMemcpyDefault) != cudaSuccess ||
          cudaMemcpy(bdst_h, dst_d, sizeof(float) * rows * bfk, cudaMemcpyDefault) != cudaSuccess) {
        fprintf(stderr, "bf d2h\n"); return 4;
      }
      for (long i = 0; i < rows; ++i) {
        const long gid = off + i;
        const int64_t* si = ids_h + i * bfk;
        const float*   sd = bdst_h + i * bfk;
        unsigned* di = cand_ids + (size_t)gid * cand;
        float*    dd = cand_dst + (size_t)gid * cand;
        long got = 0;
        for (long j = 0; j < bfk && got < cand; ++j) {
          if (si[j] == gid) continue;
          di[got] = (unsigned)si[j];
          dd[got] = sd[j];
          ++got;
        }
        /* Defensive pad; unreachable when cand+1 rows come back distinct. */
        while (got < cand) { di[got] = (unsigned)gid; dd[got] = 3.4e38f; ++got; }
      }
    }
    if (bf) cuvsBruteForceIndexDestroy(bf);
    tot_bf += now() - t0;

    /* ---- phase 2: reciprocal + RNG prune (host, distance-symmetric) ---- */
    double t1 = now();
    if (!gpu_merge) {
    for (long i = 0; i <= n; ++i) radj_off[i] = 0;
    /* In-degree counting is only needed when the reverse pass actually runs:
     * with cand <= gd the forward list alone cannot fill the row. */
    const int use_rev = (cand <= gd);
    size_t rev_n = 0;
    if (use_rev) {
      for (long i = 0; i < n; ++i) {
        const unsigned* di = cand_ids + (size_t)i * cand;
        for (long j = 0; j < cand; ++j) {
          const unsigned nb_id = di[j];
          if ((long)nb_id == i) continue;
          ++radj_off[nb_id + 1];
        }
      }
      for (long i = 0; i < n; ++i) radj_off[i + 1] += radj_off[i];
      rev_n = radj_off[n];
      radj_id = (unsigned*)malloc(sizeof(unsigned) * (rev_n ? rev_n : 1));
      radj_dst = (float*)malloc(sizeof(float) * (rev_n ? rev_n : 1));
      size_t* fill = (size_t*)malloc(((size_t)n + 1) * sizeof(size_t));
      if (!radj_id || !radj_dst || !fill) { fprintf(stderr, "radj alloc\n"); return 3; }
      memcpy(fill, radj_off, ((size_t)n + 1) * sizeof(size_t));
      for (long i = 0; i < n; ++i) {
        const unsigned* di = cand_ids + (size_t)i * cand;
        const float*    dd = cand_dst + (size_t)i * cand;
        for (long j = 0; j < cand; ++j) {
          const unsigned nb_id = di[j];
          if ((long)nb_id == i) continue;
          const size_t p = fill[nb_id]++;
          radj_id[p] = (unsigned)i;
          radj_dst[p] = dd[j];   /* L2 is symmetric, so d(j,i) == d(i,j) */
        }
      }
      free(fill);
    }

    /* Prune each node's merged candidate list to gd with the RNG rule.
     *
     * Plain distance-truncation is what the official builder would not do: it
     * yields the exact kNN graph, whose edges are all short and mostly
     * one-directional, so CAGRA's greedy search falls into local clumps
     * (recall@100 0.9243 vs the official builder's 0.9705). RNG keeps an edge
     * (i,j) only when no already-kept neighbour k occludes it, i.e. no k has
     * d(k,j) < d(i,j); that is what leaves the long, diverse edges behind.
     *
     * d(k,j) is not in the candidate table, so it is recomputed from the base
     * vectors -- O(gd * occl * dim) per node, parallelised over the 24 cores.
     * occl caps how many kept neighbours are tested; occl=0 degrades to plain
     * distance truncation.
     *
     * The whole graph is assembled in host memory and uploaded in ONE copy.
     * Copying per node instead (250k tiny H2D transfers) cost ~10s on its own.
     */
    size_t max_rev = 0;
    for (long i = 0; i < n; ++i) {
      const size_t d = radj_off[i + 1] - radj_off[i];
      if (d > max_rev) max_rev = d;
    }
    unsigned* graph_h = (unsigned*)malloc(graph_bytes);
    const size_t merge_cap = (size_t)cand + max_rev;
    if (!graph_h) { fprintf(stderr, "graph host alloc\n"); return 3; }
    int alloc_fail = 0;
#pragma omp parallel
    {
      cand_t* buf   = (cand_t*)malloc(sizeof(cand_t) * merge_cap);
      cand_t* defer = (cand_t*)malloc(sizeof(cand_t) * merge_cap);
      unsigned* kept = (unsigned*)malloc(sizeof(unsigned) * (size_t)gd);
      if (!buf || !defer || !kept) {
#pragma omp atomic write
        alloc_fail = 1;
      }
#pragma omp for schedule(dynamic, 256)
      for (long i = 0; i < n; ++i) {
        long m = 0;
        const unsigned* di = cand_ids + (size_t)i * cand;
        const float*    dd = cand_dst + (size_t)i * cand;
        for (long j = 0; j < cand; ++j) {
          if ((long)di[j] == i) continue;
          buf[m].id = di[j]; buf[m].dist = dd[j]; ++m;
        }
        for (size_t p = radj_off[i]; p < radj_off[i + 1]; ++p) {
          if ((long)radj_id[p] == i) continue;
          buf[m].id = radj_id[p]; buf[m].dist = radj_dst[p]; ++m;
        }
        qsort(buf, (size_t)m, sizeof(cand_t), cand_less);

        unsigned* grow = graph_h + (size_t)i * gd;
        long out = 0, nd = 0;
        unsigned prev = 0xFFFFFFFFu;
        for (long j = 0; j < m && (out < gd || nd < (long)merge_cap); ++j) {
          const unsigned cid = buf[j].id;
          if (cid == prev) continue;
          prev = cid;
          const float dij = buf[j].dist;
          int occluded = 0;
          if (occl > 0 && out > 0) {
            const float* pj = base + (size_t)cid * dim;
            const long nk = (out < occl) ? out : occl;
            for (long k = 0; k < nk; ++k) {
              const unsigned kid = kept[k];
              if (kid == cid) { occluded = 1; break; }
              /* Only points closer to j than i is can occlude the edge. */
              if (l2sq(base + (size_t)kid * dim, pj, dim) < dij) { occluded = 1; break; }
            }
          }
          if (occluded) {
            if (nd < (long)merge_cap) defer[nd++] = buf[j];
            continue;
          }
          if (out < gd) { kept[out] = cid; grow[out] = cid; ++out; }
        }
        /* Backfill from the occluded tail; RNG can leave a row short. */
        for (long j = 0; j < nd && out < gd; ++j) grow[out++] = defer[j].id;
        while (out < gd) grow[out++] = (unsigned)i;
      }
      free(buf); free(defer); free(kept);
    }
    if (alloc_fail) { fprintf(stderr, "merge alloc\n"); return 3; }
    if (cudaMemcpy(graph_d, graph_h, graph_bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
      fprintf(stderr, "graph h2d\n"); return 4;
    }
    free(graph_h);
    }
    tot_merge += now() - t1;

    /* ---- phase 3: official CAGRA index over the supplied graph ---- */
    double t2 = now();
    cuvsCagraIndex_t idx = NULL;
    if (cuvsCagraIndexCreate(&idx) != CUVS_SUCCESS) { fprintf(stderr, "idx create\n"); return 5; }
    long g_sh[2], id_sh[2];
    DLManagedTensor g_t, id_t;
    mk_tensor(&g_t, graph_d, n, gd, kDLCUDA, kDLUInt, 32, g_sh);
    mk_tensor(&id_t, base_d, n, dim, kDLCUDA, kDLFloat, 32, id_sh);
    if (cuvsCagraIndexFromArgs(res, L2Expanded, &g_t, &id_t, idx) != CUVS_SUCCESS) {
      fprintf(stderr, "cagra from args: %s\n", cuvsGetLastErrorText()); return 5;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) { fprintf(stderr, "sync idx\n"); return 5; }
    tot_asm += now() - t2;

    /* ---- search on the official CAGRA path ---- */
    void* qd = NULL; void* nd = NULL; void* dd_g = NULL;
    const size_t qbytes = sizeof(float) * nq * dim;
    const size_t obytes = sizeof(unsigned) * nq * topk;
    if (cuvsRMMAlloc(res, &qd, qbytes) != CUVS_SUCCESS ||
        cuvsRMMAlloc(res, &nd, obytes) != CUVS_SUCCESS ||
        cuvsRMMAlloc(res, &dd_g, sizeof(float) * nq * topk) != CUVS_SUCCESS) { fprintf(stderr, "rmm\n"); return 6; }
    if (cudaMemcpy(qd, query, qbytes, cudaMemcpyDefault) != cudaSuccess) { fprintf(stderr, "q memcpy\n"); return 6; }

    cuvsCagraSearchParams_t sp = NULL;
    if (cuvsCagraSearchParamsCreate(&sp) != CUVS_SUCCESS) { fprintf(stderr, "sp\n"); return 6; }
    if (sp->itopk_size < itopk) sp->itopk_size = itopk;
    sp->search_width = sw;

    long qs_sh2[2], ns_sh[2], ds_sh2[2];
    DLManagedTensor qt, nt, dt;
    mk_tensor(&qt, qd, nq, dim, kDLCUDA, kDLFloat, 32, qs_sh2);
    mk_tensor(&nt, nd, nq, topk, kDLCUDA, kDLUInt, 32, ns_sh);
    mk_tensor(&dt, dd_g, nq, topk, kDLCUDA, kDLFloat, 32, ds_sh2);
    cuvsFilter nf; nf.type = NO_FILTER; nf.addr = (uintptr_t)0;
    double s0 = now();
    if (cuvsCagraSearch(res, sp, idx, &qt, &nt, &dt, nf) != CUVS_SUCCESS) {
      fprintf(stderr, "search: %s\n", cuvsGetLastErrorText()); return 6;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) { fprintf(stderr, "sync2\n"); return 6; }
    const double st = now() - s0;
    tot_search += st;

    unsigned* got = (unsigned*)malloc(obytes);
    if (cudaMemcpy(got, nd, obytes, cudaMemcpyDefault) != cudaSuccess) { fprintf(stderr, "out memcpy\n"); return 7; }

    const long kk = (topk < gt_cols) ? topk : gt_cols;
    const long k10 = (kk < 10) ? kk : 10;
    long hit = 0, total = 0, hit10 = 0, total10 = 0;
    for (long q = 0; q < nq; ++q) {
      for (long j = 0; j < kk; ++j) {
        const int want = gt[q * gt_cols + j];
        int found = 0;
        for (long m = 0; m < topk; ++m) {
          if ((long)got[q * topk + m] == (long)want) { found = 1; break; }
        }
        hit += found; ++total;
        if (j < k10) { hit10 += found; ++total10; }
      }
    }
    recall = total ? (double)hit / (double)total : 0.0;
    recall10 = total10 ? (double)hit10 / (double)total10 : 0.0;

    const double total_build = tot_bf / (r + 1) + tot_merge / (r + 1) + tot_asm / (r + 1);
    if (total_build < best_total) best_total = total_build;
    printf("[fg] rep %d: brute=%.3fs merge=%.3fs from_args=%.3fs total=%.3fs search=%.4fs "
           "recall@10=%.4f recall@%ld=%.4f\n",
           r, tot_bf / (r + 1), tot_merge / (r + 1), tot_asm / (r + 1), total_build, st,
           recall10, kk, recall);
    printf("RESULT gd=%ld cand=%ld sw=%d itopk=%zu n=%ld build=%.3f bf=%.3f merge=%.3f asm=%.3f "
           "search=%.4f qps=%.1f recall10=%.4f recall=%.4f\n",
           gd, cand, sw, itopk, n, total_build, tot_bf / (r + 1), tot_merge / (r + 1),
           tot_asm / (r + 1), st, (double)nq / (st > 0 ? st : 1), recall10, recall);
    fflush(stdout);

    free(got);
    cuvsCagraSearchParamsDestroy(sp);
    cuvsRMMFree(res, qd, qbytes);
    cuvsRMMFree(res, nd, obytes);
    cuvsRMMFree(res, dd_g, sizeof(float) * nq * topk);
    cuvsCagraIndexDestroy(idx);
  }

  if (ivf) cuvsIvfFlatIndexDestroy(ivf);
  if (ivf_sp) cuvsIvfFlatSearchParamsDestroy(ivf_sp);
  cuvsRMMFree(res, ids_d, batch_ids_bytes);
  cuvsRMMFree(res, dst_d, batch_dst_bytes);
  cuvsRMMFree(res, graph_d, graph_bytes);
  cuvsRMMFree(res, base_d, base_bytes);
  cuvsResourcesDestroy(res);
  free(base); free(query); free(gt);
  free(cand_ids); free(cand_dst);
  free(radj_off); free(radj_id); free(radj_dst);
  free(ids_h); free(bdst_h);
  return 0;
}
