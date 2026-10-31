#!/usr/bin/env python3
"""Run the actual pruning body serially under AddressSanitizer.

This catches candidate-ID memory errors without HIP hardware. It does not test
GPU scheduling, synchronization, compilation or recall; run those on ROCm too.
Usage: python3 -I tools/hipvs_bridge/test_prune_bounds.py
Requires clang++ (or CXX) with AddressSanitizer.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def main():
    source = Path(__file__).with_name("seekdb_cuvs_bridge.c").read_text()
    start = source.index("__global__ void sk_rng_prune_kernel")
    end = source.index("/* Build a graph_degree kNN graph", start)
    kernel = source[start:end].replace(
        "extern __shared__ char smem[];", "alignas(16) char smem[65536] = {};")
    prefix = r'''
#include <cstddef>
#include <cstdint>
#include <climits>
#include <cstdio>
#include <vector>
#define __global__
#define __syncthreads() ((void)0)
struct Dim { int x; };
static const Dim blockIdx{0}, threadIdx{0}, blockDim{1};
'''
    driver = r'''
static bool run(const char* name, const std::vector<long long>& ids,
                long row, const std::vector<unsigned>& expected, int dim = 2) {
  std::vector<float> base(8 * dim);
  if (dim == 8) {
    // Equidistant orthogonal neighbors cannot occlude each other.
    for (int i = 1; i < 8; ++i) base[i * dim + i] = 1.0f;
  } else {
    for (size_t i = 0; i < base.size(); ++i) base[i] = (float)i / 8;
  }
  std::vector<float> distances(ids.size());
  for (size_t i = 0; i < ids.size(); ++i) {
    if (ids[i] < 0 || ids[i] >= 8) continue;
    for (int d = 0; d < dim; ++d) {
      float delta = base[ids[i] * dim + d] - base[row * dim + d];
      distances[i] += delta * delta;
    }
  }
  std::vector<unsigned> graph_storage(8 * 3, UINT_MAX);
  sk_rng_prune_kernel(base.data(), ids.data(), distances.data(), graph_storage.data(),
                      dim, (int)ids.size(), 3, 3, row, 8);
  for (int r = 0; r < 8; ++r) {
    if (r == row) continue;
    for (int j = 0; j < 3; ++j) {
      if (graph_storage[r * 3 + j] != UINT_MAX) {
        std::fprintf(stderr, "%s: overwrote untouched graph row %d\n", name, r);
        return false;
      }
    }
  }
  const unsigned* graph = graph_storage.data() + row * 3;
  for (int i = 0; i < 3; ++i) {
    if (graph[i] >= 8 || (!expected.empty() && graph[i] != expected[i])) {
      std::fprintf(stderr, "%s: invalid graph[%d]=%u\n", name, i, graph[i]);
      return false;
    }
  }
  std::printf("PASS %s\n", name);
  return true;
}
int main() {
  bool ok = true;
  ok &= run("valid_candidates", {0,1,2,3,4,5,6,7}, 0, {});
  ok &= run("negative_and_uint32_wrap", {0,-1,1,2,LLONG_MAX,3,4,5}, 0, {});
  ok &= run("out_of_range_prefix", {8,9,4294967296LL,LLONG_MIN,1,2,3,0}, 0, {});
  ok &= run("invalid_tail_backfill", {0,1,-1,8,LLONG_MAX,2,3,4}, 0, {});
  ok &= run("all_missing", {-1,8,LLONG_MAX,LLONG_MIN,0}, 0, {0,0,0});
  ok &= run("one_valid_neighbor", {-1,8,3,LLONG_MAX,0}, 0, {3,0,0});
  ok &= run("nonzero_row_offset", {7,-1,0,8,2,LLONG_MAX,4}, 7, {});
  ok &= run("rng_prune_separates", {1,2,3,4,5,6,7}, 0, {1,2,3}, 8);
  return ok ? 0 : 1;
}
'''
    compiler = shlex.split(os.environ.get("CXX", "clang++"))
    with tempfile.TemporaryDirectory(prefix="seekdb-prune-bounds-") as directory:
        directory = Path(directory)
        cpp = directory / "prune_bounds.cpp"
        binary = directory / "prune_bounds"
        cpp.write_text(prefix + kernel + driver)
        subprocess.run(compiler + ["-std=c++17", "-O1", "-g",
                       "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                       str(cpp), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("PASS serial kernel bounds; HIP/device validation still required")


if __name__ == "__main__":
    main()
