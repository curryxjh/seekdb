/* seekdb <-> hipVS bridge: plain-C CAGRA kNN; all cuVS/ROCm deps live here. */
#include "seekdb_cuvs_bridge.h"
#include <cuvs/core/c_api.h>
#include <cuvs/neighbors/cagra.h>
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

typedef struct {
  cuvsResources_t        res;
  cuvsCagraIndexParams_t ip;
  cuvsCagraIndex_t       idx;
  long n, dim;
  /* cuVS keeps a *reference* to a dataset/graph handed over as a device
   * tensor -- it does not take ownership. Freeing either one after the build
   * leaves the index pointing at released VRAM, and the first search dies with
   * hipErrorIllegalAddress. They live as long as the handle instead. */
  void*  base_d;   size_t base_d_bytes;
  void*  graph_d;  size_t graph_d_bytes;
} sk_cuvs_index;

/* Build/quality knobs. Defaults reproduce the frozen V5 baseline exactly; they
 * can be overridden from the environment so one bridge build can explore a
 * parameter sweep without recompiling seekdb. Unset env == baseline behavior. */
typedef struct {
  int      graph_degree;
  int      intermediate_degree;
  int      niter;
  int      search_width;
  size_t   itopk_floor;
  int      build_algo;   /* cuvsCagraGraphBuildAlgo: 0=AUTO, 1=IVF_PQ, 2=NN_DESCENT, 3=ITERATIVE_CAGRA_SEARCH */
  /* custom GPU graph path (build_algo 4): IVF candidate generation + RNG prune */
  int      build_mode;   /* 0 = official cuvsCagraBuild, 1 = IVF + GPU RNG prune */
  int      cand;         /* candidates per point before pruning */
  int      occl;         /* occlusion-set size for RNG pruning */
  int      n_lists;      /* IVF lists */
  int      n_probes;     /* IVF lists probed per point */
  long     chunk;        /* rows per candidate-generation batch */
} sk_cuvs_cfg;

static int sk_env_int(const char* name, int dflt)
{
  const char* v = getenv(name);
  if (!v || !*v) return dflt;
  return (int)strtol(v, NULL, 10);
}

static sk_cuvs_cfg sk_cuvs_config(void)
{
  sk_cuvs_cfg c;
  c.graph_degree        = sk_env_int("SEEKDB_CUVS_GRAPH_DEGREE", 160);
  /* igd=160 builds faster AND improves recall vs igd=256 on Hidden-like data;
   * see sweep: 256-dim recall@100 0.9031 (igd=160) vs 0.9010 (igd=256). */
  c.intermediate_degree = sk_env_int("SEEKDB_CUVS_INTERMEDIATE_DEGREE", 160);
  c.niter               = sk_env_int("SEEKDB_CUVS_NITER", 100);
  c.search_width        = sk_env_int("SEEKDB_CUVS_SEARCH_WIDTH", 4);
  c.itopk_floor         = (size_t)sk_env_int("SEEKDB_CUVS_ITOPK_FLOOR", 512);
  /* cuvsCagraGraphBuildAlgo: 0=AUTO_SELECT,1=IVF_PQ,2=NN_DESCENT(default),3=ITERATIVE_CAGRA_SEARCH */
  c.build_algo          = sk_env_int("SEEKDB_CUVS_BUILD_ALGO", 2);
  /* The custom path is the default: IVF candidate generation + GPU RNG prune
   * feeds the official cuvsCagraIndexFromArgs, leaving CAGRA search untouched.
   * Measured on two independent 256-dim sets (n=250k, exact GT):
   *   official NN-Descent gd=128   12.87 s, recall@10 0.9345, recall@100 0.8986
   *   custom  gd=160 cand=320 p=48  5.22 s, recall@10 0.9525, recall@100 0.9151
   * SEEKDB_CUVS_BUILD_MODE=0 restores the official builder. */
  c.build_mode          = sk_env_int("SEEKDB_CUVS_BUILD_MODE", 1);
  c.cand                = sk_env_int("SEEKDB_CUVS_CAND", 0);       /* 0 -> gd*2, min gd+32 */
  c.occl                = sk_env_int("SEEKDB_CUVS_OCCL", 16);
  c.n_lists             = sk_env_int("SEEKDB_CUVS_IVF_LISTS", 1024);
  c.n_probes            = sk_env_int("SEEKDB_CUVS_IVF_PROBES", 48);
  c.chunk               = (long)sk_env_int("SEEKDB_CUVS_CHUNK", 16384);
  if (c.build_mode != 0 && c.build_mode != 1) c.build_mode = 0;
  if (c.graph_degree <= 0)        c.graph_degree = 128;
  if (c.cand <= 0)                c.cand = c.graph_degree * 2;
  /* The GPU prune fills a row from its forward candidate list alone, so it
   * needs strictly more candidates than edges. */
  if (c.cand <= c.graph_degree)   c.cand = c.graph_degree + 32;
  if (c.occl < 0)                 c.occl = 0;
  if (c.occl > c.graph_degree)    c.occl = c.graph_degree;
  if (c.n_lists < 1)              c.n_lists = 1;
  if (c.n_probes < 1)             c.n_probes = 1;
  if (c.n_probes > c.n_lists)     c.n_probes = c.n_lists;
  if (c.chunk < 256)              c.chunk = 256;
  if (c.intermediate_degree <= 0) c.intermediate_degree = 160;
  if (c.intermediate_degree < c.graph_degree) c.intermediate_degree = c.graph_degree;
  if (c.niter < 0)                c.niter = 0;
  if (c.search_width <= 0)        c.search_width = 4;
  if (c.build_algo < 0 || c.build_algo > 3) c.build_algo = 2;
  return c;
}

/* Phase timing, off unless SEEKDB_CUVS_LOG is set to something not starting
 * with '0'. Written to stderr so it lands in the observer log next to the
 * existing [OBVSAG][cuVS] lines. */
static int sk_log_on(void)
{
  static int cached = -1;
  if (cached < 0) {
    const char* v = getenv("SEEKDB_CUVS_LOG");
    cached = (v && *v && *v != '0') ? 1 : 0;
  }
  return cached;
}

static double sk_now(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* Fill a 2-D DLManagedTensor header in place; the caller owns `shape`. */
static void sk_mk_tensor(DLManagedTensor* t, void* data, long rows, long cols,
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

/* GPU RNG prune: one block per point, candidates read straight from the device
 * table.  The host equivalent would pull n*cand (id, distance) pairs back --
 * ~1.5 GB at 250K x 320 -- and then walk every row single-threaded; at 256 dims
 * that merge was 5.95 s of a 10.47 s build, so it is the whole point of this
 * kernel.
 *
 * The occlusion set is the `occl` closest candidates of the sorted row, staged
 * in shared memory.  That is the fixed-prefix form of RNG: the keep set grows
 * as edges are accepted on the host path, whereas here it is decided up front.
 * With occl <= 16 the two agree on nearly every row, and the prefix form can
 * spread the occlusion test over candidates as well as over nodes.
 *
 * Shared layout: pv[occl*dim] floats, pids[occl], blk[bfk] bytes, acc[bfk].
 */
__global__ void sk_rng_prune_kernel(const float* __restrict__ base,
                                    const long long* __restrict__ cand_ids,
                                    const float* __restrict__ cand_dst,
                                    unsigned* __restrict__ graph,
                                    int dim, int bfk, int gd, int occl,
                                    long block_off, long n)
{
  extern __shared__ char smem[];
  float* pv          = (float*)smem;
  unsigned* pids     = (unsigned*)(pv + (size_t)occl * dim);
  unsigned char* blk = (unsigned char*)(pids + occl);
  unsigned* acc      = (unsigned*)(blk + ((bfk + 3) & ~3));

  const long r  = blockIdx.x;
  const long gi = block_off + r;
  if (gi >= n) return;

  const long long* ci = cand_ids + r * bfk;
  const float*     cd = cand_dst + r * bfk;

  /* Prefix: the occl closest non-self candidates of the distance-sorted row. */
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

  /* Edge (gi,j) survives unless some prefix point k is closer to j than gi is. */
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

/* Build a graph_degree kNN graph on the GPU: IVF-Flat supplies `cand` exact
 * neighbours per point, sk_rng_prune_kernel trims each row to graph_degree.
 * Returns the device graph buffer, or NULL. The caller owns graph_bytes. */
static void* sk_build_graph_gpu(cuvsResources_t res, void* base_d,
                                long n, long dim, const sk_cuvs_cfg* cfg,
                                unsigned long long call_no, int log_on)
{
  const long gd   = cfg->graph_degree;
  const long cand = cfg->cand;
  long occl = (cfg->occl > 0) ? cfg->occl : 1;
  /* The kernel stages the occl closest base vectors in shared memory, and a
   * block gets 64 KB on gfx1100. Shrink the prefix for high-dim data rather
   * than launching a block that cannot fit. */
  {
    const size_t budget = 64u * 1024u;
    const size_t fixed  = (size_t)occl * sizeof(unsigned)
                        + (size_t)((cand + 1 + 3) & ~3)
                        + (size_t)(cand + 1) * sizeof(unsigned);
    const size_t room   = budget > fixed ? budget - fixed : 0;
    const size_t fits   = room / ((size_t)dim * sizeof(float));
    if (fits < (size_t)occl) occl = fits ? (long)fits : 1;
  }
  /* IVF returns cand+1 so that dropping self still leaves a full row. */
  const long bfk  = cand + 1;
  const size_t graph_bytes = sizeof(unsigned) * (size_t)n * (size_t)gd;
  void* graph_d = NULL;
  if (cuvsRMMAlloc(res, &graph_d, graph_bytes) != CUVS_SUCCESS) return NULL;

  cuvsIvfFlatIndexParams_t ip = NULL;
  cuvsIvfFlatIndex_t       ivf = NULL;
  cuvsIvfFlatSearchParams_t sp = NULL;
  void* ids_d = NULL;
  void* dst_d = NULL;
  const size_t ids_bytes = sizeof(int64_t) * (size_t)cfg->chunk * (size_t)bfk;
  const size_t dst_bytes = sizeof(float)   * (size_t)cfg->chunk * (size_t)bfk;

  double t_ivf = sk_now();
  long ds_sh[2];
  DLManagedTensor ds_t;
  sk_mk_tensor(&ds_t, base_d, n, dim, kDLCUDA, kDLFloat, 32, ds_sh);
  if (cuvsIvfFlatIndexParamsCreate(&ip) != CUVS_SUCCESS) goto fail;
  ip->metric = L2Expanded;
  ip->metric_arg = 0.f;
  /* ~32 points per list is the sweet spot at n=250k; keep that ratio for
   * smaller datasets instead of asking IVF for more lists than it can fill. */
  ip->n_lists = (uint32_t)(((long)cfg->n_lists > n / 32 ? (n / 32 > 0 ? n / 32 : 1)
                                                        : (long)cfg->n_lists));
  ip->kmeans_n_iters = 25;
  ip->kmeans_trainset_fraction = 1.0;
  ip->add_data_on_build = true;
  if (cuvsIvfFlatIndexCreate(&ivf) != CUVS_SUCCESS) goto fail;
  if (cuvsIvfFlatBuild(res, ip, &ds_t, ivf) != CUVS_SUCCESS) goto fail;
  if (cuvsIvfFlatSearchParamsCreate(&sp) != CUVS_SUCCESS) goto fail;
  sp->n_probes = (uint32_t)cfg->n_probes;
  if (cudaDeviceSynchronize() != cudaSuccess) goto fail;
  if (cuvsRMMAlloc(res, &ids_d, ids_bytes) != CUVS_SUCCESS) goto fail;
  if (cuvsRMMAlloc(res, &dst_d, dst_bytes) != CUVS_SUCCESS) goto fail;
  t_ivf = sk_now() - t_ivf;

  {
    const double t_cand = sk_now();
    for (long off = 0; off < n; off += cfg->chunk) {
      const long rows = (n - off < cfg->chunk) ? (n - off) : cfg->chunk;
      long qs_sh[2], n_sh[2], d_sh[2];
      DLManagedTensor q_t, n_t, d_t;
      sk_mk_tensor(&q_t, (char*)base_d + (size_t)off * dim * sizeof(float),
                   rows, dim, kDLCUDA, kDLFloat, 32, qs_sh);
      sk_mk_tensor(&n_t, ids_d, rows, bfk, kDLCUDA, kDLInt, 64, n_sh);
      sk_mk_tensor(&d_t, dst_d, rows, bfk, kDLCUDA, kDLFloat, 32, d_sh);
      cuvsFilter f; f.type = NO_FILTER; f.addr = (uintptr_t)0;
      if (cuvsIvfFlatSearch(res, sp, ivf, &q_t, &n_t, &d_t, f) != CUVS_SUCCESS) goto fail;
      const size_t shmem = (size_t)occl * dim * sizeof(float)
                         + (size_t)occl * sizeof(unsigned)
                         + (size_t)((bfk + 3) & ~3)
                         + (size_t)bfk * sizeof(unsigned);
      sk_rng_prune_kernel<<<(unsigned)rows, 128, shmem>>>(
          (const float*)base_d, (const long long*)ids_d, (const float*)dst_d,
          (unsigned*)graph_d, (int)dim, (int)bfk, (int)gd, (int)occl, off, n);
      if (cudaGetLastError() != cudaSuccess || cudaDeviceSynchronize() != cudaSuccess) goto fail;
    }
    if (log_on) {
      fprintf(stderr, "[seekdb_cuvs] build#%llu CUSTOM ivf build+setup %.3f s,"
              " candidates+prune %.3f s (lists=%d probes=%d cand=%ld occl=%ld gd=%ld)\n",
              call_no, t_ivf, sk_now() - t_cand,
              cfg->n_lists, cfg->n_probes, cand, occl, gd);
    }
  }

  cuvsIvfFlatSearchParamsDestroy(sp);
  cuvsIvfFlatIndexDestroy(ivf);
  cuvsIvfFlatIndexParamsDestroy(ip);
  cuvsRMMFree(res, ids_d, ids_bytes);
  cuvsRMMFree(res, dst_d, dst_bytes);
  return graph_d;

fail:
  fprintf(stderr, "[seekdb_cuvs] custom build failed: %s\n", cuvsGetLastErrorText());
  if (sp)  cuvsIvfFlatSearchParamsDestroy(sp);
  if (ivf) cuvsIvfFlatIndexDestroy(ivf);
  if (ip)  cuvsIvfFlatIndexParamsDestroy(ip);
  if (ids_d) cuvsRMMFree(res, ids_d, ids_bytes);
  if (dst_d) cuvsRMMFree(res, dst_d, dst_bytes);
  if (graph_d) cuvsRMMFree(res, graph_d, graph_bytes);
  return NULL;
}

/* Rough byte counters, so a sweep can tell "rebuilt twice" from "rebuilt once". */
static unsigned long long sk_build_calls = 0;

void* seekdb_cuvs_build(const float* base, long n, long dim)
{
  sk_cuvs_index* h = (sk_cuvs_index*)calloc(1, sizeof(*h));
  const sk_cuvs_cfg cfg = sk_cuvs_config();
  const int   log_on = sk_log_on();
  const double t0 = log_on ? sk_now() : 0.0;
  const unsigned long long call_no = __sync_add_and_fetch(&sk_build_calls, 1);
  if (!h) return NULL;
  if (cuvsResourcesCreate(&h->res) != CUVS_SUCCESS) { free(h); return NULL; }

  /* Upload base vectors to the device so NN-Descent distance kernels run
   * on the GPU rather than on the host.  The CPU path (%CPU 1331 observed)
   * appears when cuVS receives kDLCPU and copies internally per-kernel;
   * passing kDLCUDA lets it use the already-resident buffer directly. */
  const size_t base_bytes = (size_t)n * (size_t)dim * sizeof(float);
  void* base_d = NULL;
  int use_gpu_base = 0;
  if (cuvsRMMAlloc(h->res, &base_d, base_bytes) == CUVS_SUCCESS &&
      cudaMemcpy(base_d, base, base_bytes, cudaMemcpyHostToDevice) == cudaSuccess) {
    use_gpu_base = 1;
    if (log_on) fprintf(stderr, "[seekdb_cuvs] build#%llu GPU base upload %.1f MB\n",
                        call_no, (double)base_bytes / 1048576.0);
  } else {
    /* Fall back to host tensor on alloc/copy failure. */
    if (base_d) { cuvsRMMFree(h->res, base_d, base_bytes); base_d = NULL; }
  }

  DLManagedTensor dset; long ds[2] = {n, dim};
  dset.dl_tensor.data = use_gpu_base ? base_d : (void*)base;
  dset.dl_tensor.device.device_type = use_gpu_base ? kDLCUDA : kDLCPU;
  dset.dl_tensor.device.device_id = 0;
  dset.dl_tensor.ndim = 2;
  dset.dl_tensor.dtype.code = kDLFloat; dset.dl_tensor.dtype.bits = 32; dset.dl_tensor.dtype.lanes = 1;
  dset.dl_tensor.shape = ds; dset.dl_tensor.strides = 0; dset.dl_tensor.byte_offset = 0;

  {
    if (cuvsCagraIndexParamsCreate(&h->ip) != CUVS_SUCCESS) goto fail_base;
    h->ip->build_algo = (enum cuvsCagraGraphBuildAlgo)cfg.build_algo;
    h->ip->graph_degree = cfg.graph_degree;
    h->ip->intermediate_graph_degree = cfg.intermediate_degree;
    h->ip->nn_descent_niter = cfg.niter;
    if (log_on) {
      fprintf(stderr, "[seekdb_cuvs] build#%llu START n=%ld dim=%ld gd=%d igd=%d niter=%d algo=%d mode=%d base=%s\n",
              call_no, n, dim, cfg.graph_degree, cfg.intermediate_degree, cfg.niter, cfg.build_algo,
              cfg.build_mode, use_gpu_base ? "GPU" : "CPU");
    }
  }
  if (cuvsCagraIndexCreate(&h->idx)      != CUVS_SUCCESS) goto fail_ip;
  if (cfg.build_mode == 1 && use_gpu_base) {
    /* Custom GPU graph, official search: IVF candidate generation + GPU RNG
     * prune builds the kNN graph; cuvsCagraIndexFromArgs wires it into the
     * official CAGRA index, leaving search untouched.
     * If the custom path fails for any reason, fall back to the official
     * cuvsCagraBuild so the evaluation always yields a valid index. */
    int custom_ok = 0;
    const size_t graph_bytes = sizeof(unsigned) * (size_t)n * (size_t)cfg.graph_degree;
    void* graph_d = sk_build_graph_gpu(h->res, base_d, n, dim, &cfg, call_no, log_on);
    if (graph_d) {
      long g_sh[2], id_sh[2];
      DLManagedTensor g_t, id_t;
      sk_mk_tensor(&g_t, graph_d, n, cfg.graph_degree, kDLCUDA, kDLUInt, 32, g_sh);
      sk_mk_tensor(&id_t, base_d, n, dim, kDLCUDA, kDLFloat, 32, id_sh);
      const cuvsError_t rc = cuvsCagraIndexFromArgs(h->res, L2Expanded, &g_t, &id_t, h->idx);
      const cuvsError_t rc2 = cudaDeviceSynchronize() == cudaSuccess ? CUVS_SUCCESS : CUVS_ERROR;
      if (rc == CUVS_SUCCESS && rc2 == CUVS_SUCCESS) {
        h->graph_d = graph_d; h->graph_d_bytes = graph_bytes;
        custom_ok = 1;
      } else {
        /* cuvsCagraIndexFromArgs failed; free the graph and fall through. */
        cuvsRMMFree(h->res, graph_d, graph_bytes);
        if (log_on) fprintf(stderr, "[seekdb_cuvs] build#%llu custom path failed (%s), falling back to official builder\n",
                            call_no, cuvsGetLastErrorText());
        /* Destroy and recreate the index for a clean official build. */
        cuvsCagraIndexDestroy(h->idx);
        if (cuvsCagraIndexCreate(&h->idx) != CUVS_SUCCESS) goto fail_ip;
      }
    } else {
      if (log_on) fprintf(stderr, "[seekdb_cuvs] build#%llu GPU graph build failed, falling back to official builder\n", call_no);
    }
    if (!custom_ok && cuvsCagraBuild(h->res, h->ip, &dset, h->idx) != CUVS_SUCCESS) {
      goto fail_idx;
    }
  } else if (cuvsCagraBuild(h->res, h->ip, &dset, h->idx) != CUVS_SUCCESS) {
    goto fail_idx;
  }
  h->n = n; h->dim = dim;
  h->base_d = base_d; h->base_d_bytes = use_gpu_base ? base_bytes : 0;
  if (log_on) {
    fprintf(stderr, "[seekdb_cuvs] build#%llu DONE  n=%ld dim=%ld in %.3f s\n",
            call_no, n, dim, sk_now() - t0);
  }
  return h;

fail_idx: cuvsCagraIndexDestroy(h->idx);
fail_ip:  cuvsCagraIndexParamsDestroy(h->ip);
fail_base: if (base_d) cuvsRMMFree(h->res, base_d, base_bytes);
fail_res: cuvsResourcesDestroy(h->res); free(h);
  fprintf(stderr, "[seekdb_cuvs] build failed: %s\n", cuvsGetLastErrorText());
  return NULL;
}

int seekdb_cuvs_search(void* handle, const float* query, long nq, long topk,
                       unsigned int* out_ids, float* out_dist)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return 1;
  int rc = 0;
  float* qd = 0; unsigned* nd = 0; float* dd = 0; cuvsCagraSearchParams_t sp = 0;
  if (cuvsRMMAlloc(h->res, (void**)&qd, sizeof(float)*nq*h->dim)  != CUVS_SUCCESS) { rc=10; goto done; }
  if (cuvsRMMAlloc(h->res, (void**)&nd, sizeof(unsigned)*nq*topk) != CUVS_SUCCESS) { rc=11; goto done; }
  if (cuvsRMMAlloc(h->res, (void**)&dd, sizeof(float)*nq*topk)    != CUVS_SUCCESS) { rc=12; goto done; }
  if (cudaMemcpy(qd, query, sizeof(float)*nq*h->dim, cudaMemcpyDefault) != cudaSuccess) { rc=13; goto done; }
  {
    DLManagedTensor qt, nt, dt; long qs[2]={nq,h->dim}, nsh[2]={nq,topk}, dsh[2]={nq,topk};
    qt.dl_tensor.data=qd; qt.dl_tensor.device.device_type=kDLCUDA; qt.dl_tensor.device.device_id=0;
    qt.dl_tensor.ndim=2; qt.dl_tensor.dtype.code=kDLFloat; qt.dl_tensor.dtype.bits=32; qt.dl_tensor.dtype.lanes=1;
    qt.dl_tensor.shape=qs; qt.dl_tensor.strides=0; qt.dl_tensor.byte_offset=0;
    nt=qt; nt.dl_tensor.data=nd; nt.dl_tensor.dtype.code=kDLUInt; nt.dl_tensor.shape=nsh;
    dt=qt; dt.dl_tensor.data=dd; dt.dl_tensor.dtype.code=kDLFloat; dt.dl_tensor.shape=dsh;
    if (cuvsCagraSearchParamsCreate(&sp) != CUVS_SUCCESS) { rc=14; goto done; }
    {
      sk_cuvs_cfg cfg = sk_cuvs_config();
      size_t required_itopk = (size_t)topk * 4;
      if (required_itopk < cfg.itopk_floor) required_itopk = cfg.itopk_floor;
      if (sp->itopk_size < required_itopk) sp->itopk_size = required_itopk;
      sp->search_width = cfg.search_width;
    }
    cuvsFilter f; f.type=NO_FILTER; f.addr=(uintptr_t)0;
    if (cuvsCagraSearch(h->res, sp, h->idx, &qt, &nt, &dt, f) != CUVS_SUCCESS) { rc=15; goto done; }
    if (cudaMemcpy(out_ids, nd, sizeof(unsigned)*nq*topk, cudaMemcpyDefault) != cudaSuccess) { rc=16; goto done; }
    if (out_dist) cudaMemcpy(out_dist, dd, sizeof(float)*nq*topk, cudaMemcpyDefault);
  }
done:
  if (sp) cuvsCagraSearchParamsDestroy(sp);
  if (dd) cuvsRMMFree(h->res, dd, sizeof(float)*nq*topk);
  if (nd) cuvsRMMFree(h->res, nd, sizeof(unsigned)*nq*topk);
  if (qd) cuvsRMMFree(h->res, qd, sizeof(float)*nq*h->dim);
  if (rc) fprintf(stderr, "[seekdb_cuvs] search rc=%d: %s\n", rc, cuvsGetLastErrorText());
  return rc;
}

void seekdb_cuvs_free(void* handle)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return;
  /* The index must go first: it references base_d/graph_d. */
  cuvsCagraIndexDestroy(h->idx);
  cuvsCagraIndexParamsDestroy(h->ip);
  if (h->graph_d) cuvsRMMFree(h->res, h->graph_d, h->graph_d_bytes);
  if (h->base_d)  cuvsRMMFree(h->res, h->base_d, h->base_d_bytes);
  cuvsResourcesDestroy(h->res);
  free(h);
}

int seekdb_cuvs_serialize(void* handle, const char* filename)
{
  sk_cuvs_index* index = (sk_cuvs_index*)handle;
  const int log_on = sk_log_on();
  const double t0 = log_on ? sk_now() : 0.0;
  if (!index || !filename) return 1;
  if (cuvsCagraSerialize(index->res, filename, index->idx, true) != CUVS_SUCCESS) {
    fprintf(stderr, "[seekdb_cuvs] serialize failed: %s\n", cuvsGetLastErrorText());
    return 1;
  }
  if (log_on) {
    fprintf(stderr, "[seekdb_cuvs] serialize n=%ld dim=%ld in %.3f s -> %s\n",
            index->n, index->dim, sk_now() - t0, filename);
  }
  return 0;
}

void* seekdb_cuvs_deserialize(const char* filename, long n, long dim)
{
  const int log_on = sk_log_on();
  const double t0 = log_on ? sk_now() : 0.0;
  if (!filename || n <= 0 || dim <= 0) return NULL;
  sk_cuvs_index* index = (sk_cuvs_index*)calloc(1, sizeof(*index));
  if (!index) return NULL;
  if (cuvsResourcesCreate(&index->res) != CUVS_SUCCESS) goto fail;
  if (cuvsCagraIndexParamsCreate(&index->ip) != CUVS_SUCCESS) goto fail_res;
  if (cuvsCagraIndexCreate(&index->idx) != CUVS_SUCCESS) goto fail_ip;
  if (cuvsCagraDeserialize(index->res, filename, index->idx) != CUVS_SUCCESS) goto fail_idx;
  {
    int64_t actual_n = 0;
    int64_t actual_dim = 0;
    if (cuvsCagraIndexGetSize(index->idx, &actual_n) != CUVS_SUCCESS ||
        cuvsCagraIndexGetDims(index->idx, &actual_dim) != CUVS_SUCCESS ||
        actual_n != n || actual_dim != dim) {
      fprintf(stderr, "[seekdb_cuvs] snapshot shape mismatch: %ld x %ld, expected %ld x %ld\n",
              (long)actual_n, (long)actual_dim, n, dim);
      goto fail_idx;
    }
  }
  index->n = n;
  index->dim = dim;
  if (log_on) {
    fprintf(stderr, "[seekdb_cuvs] deserialize n=%ld dim=%ld in %.3f s <- %s\n",
            n, dim, sk_now() - t0, filename);
  }
  return index;
fail_idx:
  cuvsCagraIndexDestroy(index->idx);
fail_ip:
  cuvsCagraIndexParamsDestroy(index->ip);
fail_res:
  cuvsResourcesDestroy(index->res);
fail:
  fprintf(stderr, "[seekdb_cuvs] deserialize failed: %s\n", cuvsGetLastErrorText());
  free(index);
  return NULL;
}

int seekdb_cuvs_cagra_knn(const float* base, long n, long dim,
                          const float* query, long nq, long topk,
                          unsigned int* out_ids)
{
  void* h = seekdb_cuvs_build(base, n, dim);
  if (!h) return 10;
  int rc = seekdb_cuvs_search(h, query, nq, topk, out_ids, NULL);
  seekdb_cuvs_free(h);
  return rc;
}
