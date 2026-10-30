/* Standalone CAGRA build/search benchmark over the contest dataset format.
 *
 * Purpose: sweep the build-quality knobs that the seekdb bridge exposes, and
 * report build time + Recall@k in the same units the contest scores, without
 * having to rebuild seekdb. Uses only the public hipVS/cuVS C API.
 *
 * usage: bench_cagra <base.f32> <query.f32> <gt.i32> <dims> <nq> <topk> \
 *                    <graph_degree> <intermediate_degree> <niter> \
 *                    [reps] [search_width] [itopk_size] [build_algo]
 * itopk_size = 0 means the bridge default: max(topk*4, 512).
 * build_algo: 0=AUTO_SELECT, 1=IVF_PQ, 2=NN_DESCENT (bridge default),
 *             3=ITERATIVE_CAGRA_SEARCH. Defaults to 2, matching the bridge.
 */
#include <cuvs/core/c_api.h>
#include <cuvs/neighbors/cagra.h>
#include <dlpack/dlpack.h>
#ifdef __HIP_PLATFORM_AMD__
#include <cuvs/cuda_runtime.h>
#else
#include <cuda_runtime.h>
#endif
#include <stdio.h>
#include <stdlib.h>
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

int main(int argc, char** argv)
{
  if (argc < 10) {
    fprintf(stderr, "usage: %s base.f32 query.f32 gt.i32 dims nq topk gd igd niter"
                    " [reps] [search_width] [itopk_size]\n", argv[0]);
    return 2;
  }
  const char* fbase = argv[1];
  const char* fquery = argv[2];
  const char* fgt = argv[3];
  const long dim = atol(argv[4]);
  const long nq = atol(argv[5]);
  const long topk = atol(argv[6]);
  const int gd = atoi(argv[7]);
  const int igd = atoi(argv[8]);
  const int niter = atoi(argv[9]);
  const int reps = (argc > 10) ? atoi(argv[10]) : 1;
  const int sw = (argc > 11) ? atoi(argv[11]) : 4;
  const long itopk_arg = (argc > 12) ? atol(argv[12]) : 0;
  const int algo = (argc > 13) ? atoi(argv[13]) : 2;  /* NN_DESCENT, bridge default */

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

  printf("[bench] base=%ld x %ld  nq=%ld  topk=%ld  gt_cols=%ld\n", n, dim, nq, topk, gt_cols);
  printf("[bench] gd=%d igd=%d niter=%d sw=%d itopk=%zu reps=%d\n", gd, igd, niter, sw, itopk, reps);
  fflush(stdout);

  /* Device buffers for queries + results, allocated once. */
  cuvsResources_t res;
  if (cuvsResourcesCreate(&res) != CUVS_SUCCESS) { fprintf(stderr, "res create: %s\n", cuvsGetLastErrorText()); return 3; }
  void *qd = NULL, *nd = NULL, *dd = NULL;
  size_t qbytes = sizeof(float) * nq * dim;
  size_t obytes = sizeof(unsigned) * nq * topk;
  if (cuvsRMMAlloc(res, &qd, qbytes) != CUVS_SUCCESS ||
      cuvsRMMAlloc(res, &nd, obytes) != CUVS_SUCCESS ||
      cuvsRMMAlloc(res, &dd, sizeof(float) * nq * topk) != CUVS_SUCCESS) {
    fprintf(stderr, "rmm alloc: %s\n", cuvsGetLastErrorText()); return 3;
  }
  if (cudaMemcpy(qd, query, qbytes, cudaMemcpyDefault) != cudaSuccess) { fprintf(stderr, "q memcpy\n"); return 3; }

  double best_build = 1e30, sum_build = 0.0;
  double best_search = 1e30, sum_search = 0.0;
  double recall = 0.0, recall10 = 0.0;
  for (int r = 0; r < reps; ++r) {
    cuvsCagraIndexParams_t ip = NULL;
    cuvsCagraIndex_t idx = NULL;
    if (cuvsCagraIndexParamsCreate(&ip) != CUVS_SUCCESS) { fprintf(stderr, "ip: %s\n", cuvsGetLastErrorText()); return 4; }
    ip->build_algo = (enum cuvsCagraGraphBuildAlgo)algo;
    ip->graph_degree = gd;
    ip->intermediate_graph_degree = igd;
    ip->nn_descent_niter = niter;
    if (cuvsCagraIndexCreate(&idx) != CUVS_SUCCESS) { fprintf(stderr, "idx: %s\n", cuvsGetLastErrorText()); return 4; }

    /* dataset tensor on host: cuVS copies it to the device during build */
    DLManagedTensor dset;
    long ds[2] = { n, dim };
    dset.dl_tensor.data = base;
    dset.dl_tensor.device.device_type = kDLCPU; dset.dl_tensor.device.device_id = 0;
    dset.dl_tensor.ndim = 2;
    dset.dl_tensor.dtype.code = kDLFloat; dset.dl_tensor.dtype.bits = 32; dset.dl_tensor.dtype.lanes = 1;
    dset.dl_tensor.shape = ds; dset.dl_tensor.strides = NULL; dset.dl_tensor.byte_offset = 0;

    double t0 = now();
    if (cuvsCagraBuild(res, ip, &dset, idx) != CUVS_SUCCESS) { fprintf(stderr, "build: %s\n", cuvsGetLastErrorText()); return 5; }
    if (cudaDeviceSynchronize() != cudaSuccess) { fprintf(stderr, "sync\n"); return 5; }
    double bt = now() - t0;
    if (bt < best_build) best_build = bt;
    sum_build += bt;

    /* search */
    cuvsCagraSearchParams_t sp = NULL;
    if (cuvsCagraSearchParamsCreate(&sp) != CUVS_SUCCESS) { fprintf(stderr, "sp\n"); return 6; }
    if (sp->itopk_size < itopk) sp->itopk_size = itopk;
    sp->search_width = sw;

    DLManagedTensor qt, nt, dt;
    long qs[2] = { nq, dim }, nsh[2] = { nq, topk }, dsh[2] = { nq, topk };
    qt.dl_tensor.data = qd; qt.dl_tensor.device.device_type = kDLCUDA; qt.dl_tensor.device.device_id = 0;
    qt.dl_tensor.ndim = 2; qt.dl_tensor.dtype.code = kDLFloat; qt.dl_tensor.dtype.bits = 32; qt.dl_tensor.dtype.lanes = 1;
    qt.dl_tensor.shape = qs; qt.dl_tensor.strides = NULL; qt.dl_tensor.byte_offset = 0;
    nt = qt; nt.dl_tensor.data = nd; nt.dl_tensor.dtype.code = kDLUInt; nt.dl_tensor.shape = nsh;
    dt = qt; dt.dl_tensor.data = dd; dt.dl_tensor.dtype.code = kDLFloat; dt.dl_tensor.shape = dsh;

    cuvsFilter f; f.type = NO_FILTER; f.addr = (uintptr_t)0;
    double s0 = now();
    if (cuvsCagraSearch(res, sp, idx, &qt, &nt, &dt, f) != CUVS_SUCCESS) { fprintf(stderr, "search: %s\n", cuvsGetLastErrorText()); return 6; }
    if (cudaDeviceSynchronize() != cudaSuccess) { fprintf(stderr, "sync2\n"); return 6; }
    double st = now() - s0;
    if (st < best_search) best_search = st;
    sum_search += st;
    if (cuvsCagraSearchParamsDestroy(sp) != CUVS_SUCCESS) { return 6; }

    unsigned* got = (unsigned*)malloc(obytes);
    if (cudaMemcpy(got, nd, obytes, cudaMemcpyDefault) != cudaSuccess) { fprintf(stderr, "out memcpy\n"); return 7; }

    /* Recall@k against the supplied ground truth (first k gt columns). The
     * contest gates at BOTH k=10 and k=topk, so report both. */
    const long kk = (topk < gt_cols) ? topk : gt_cols;
    const long k10 = (kk < 10) ? kk : 10;
    long hit = 0, total = 0, hit10 = 0, total10 = 0;
    for (long q = 0; q < nq; ++q) {
      for (long j = 0; j < kk; ++j) {
        int want = gt[q * gt_cols + j];
        int found = 0;
        for (long m = 0; m < topk; ++m) {
          if ((long)got[q * topk + m] == (long)want) { found = 1; break; }
        }
        hit += found;
        ++total;
        if (j < k10) { hit10 += found; ++total10; }
      }
    }
    recall = total ? (double)hit / (double)total : 0.0;
    recall10 = total10 ? (double)hit10 / (double)total10 : 0.0;
    free(got);

    if (reps > 1) {
      printf("[bench] rep %d: build=%.3fs search=%.4fs recall@10=%.4f recall@%ld=%.4f\n",
             r, bt, st, recall10, kk, recall);
      fflush(stdout);
    }
    cuvsCagraIndexDestroy(idx);
    cuvsCagraIndexParamsDestroy(ip);
  }

  double avg_build = sum_build / reps;
  double avg_search = sum_search / reps;
  printf("RESULT gd=%d igd=%d niter=%d sw=%d itopk=%zu n=%ld build_best=%.3f build_avg=%.3f "
         "search_best=%.4f search_avg=%.4f qps=%.1f recall10=%.4f recall=%.4f\n",
         gd, igd, niter, sw, itopk, n, best_build, avg_build, best_search, avg_search,
         (double)nq / (avg_search > 0 ? avg_search : 1), recall10, recall);
  return 0;
}
