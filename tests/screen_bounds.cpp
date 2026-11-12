#include "../minivectordb/screen.h"

#include <cassert>
#include <algorithm>
#include <cmath>
#include <iostream>
#include <limits>
#include <random>

int main() {
#if defined(__x86_64__)
  std::cout << "CPU x86_64 avx2=" << static_cast<bool>(__builtin_cpu_supports("avx2"))
            << " fma=" << static_cast<bool>(__builtin_cpu_supports("fma")) << '\n';
#endif
  std::mt19937 rng(20261112);
  std::normal_distribution<float> normal;
  size_t checks = 0;
  for (size_t dimension : {32U, 33U, 127U, 128U, 257U, 1024U, 4096U}) {
    for (float scale : {1.0f, 1e-38f, 1e-20f, 1e20f, 1e37f}) {
      std::vector<float> data(dimension * 65);
      for (auto &value : data) value = normal(rng) * scale;
      mvdb::QuantizedScreen screen;
      screen.build(data, dimension);
      assert(!screen.empty());
      for (size_t trial = 0; trial < 12; ++trial) {
        std::vector<float> query(dimension);
        for (auto &value : query) value = normal(rng) * scale * (trial % 3 == 0 ? 10 : 1);
        const auto q = screen.query(query);
        std::vector<uint32_t> selected;
        for (uint32_t row = 0; row < 65; ++row) selected.push_back(row);
        const auto bulk = screen.code_distances(selected, q);
        std::vector<std::pair<long double, size_t>> truth;
        std::vector<int32_t> approximate;
        for (size_t row = 0; row < 65; ++row) {
          long double sum = 0;
          for (size_t d = 0; d < dimension; ++d) {
            const long double delta = static_cast<long double>(data[row * dimension + d]) - query[d];
            sum += delta * delta;
          }
          const long double exact = std::sqrt(sum);
          truth.push_back({exact, row});
          approximate.push_back(screen.code_distance(row, q));
          assert(bulk[row] == approximate.back());
          const auto interval = screen.bounds(row, q);
          assert(interval.lower <= exact && exact <= interval.upper);
          // A candidate at the cutoff (including its rounded tie) must never
          // be rejected, in either ordering direction.
          const double tied = static_cast<double>(exact);
          assert(!screen.rejects(row, q, tied, false));
          assert(!screen.rejects(row, q, tied, true));
          for (double cutoff : {tied * 0.5, tied * 2}) {
            if (screen.rejects(row, q, cutoff, false)) assert(exact > cutoff);
            if (screen.rejects(row, q, cutoff, true)) assert(exact < cutoff);
          }
          ++checks;
        }
        std::sort(truth.begin(), truth.end());
        auto ranked = approximate;
        std::sort(ranked.begin(), ranked.end());
        const double near_cutoff = screen.code_cutoff(q, ranked[9], false);
        const double far_cutoff = screen.code_cutoff(q, ranked[ranked.size()-10], true);
        for (size_t i = 0; i < 10; ++i) {
          assert(approximate[truth[i].second] <= near_cutoff);
          assert(approximate[truth[truth.size()-1-i].second] >= far_cutoff);
        }
      }
    }
  }
  // Worst signed code products and padding-free tails, not just Gaussian data.
  for (size_t dimension : {32U, 33U, 127U, 128U, 4096U}) {
    std::vector<float> data(dimension * 3, 1), query(dimension, -1);
    mvdb::QuantizedScreen screen;
    screen.build(data, dimension);
    const auto q = screen.query(query);
    const auto interval = screen.bounds(0, q);
    const double exact = 2 * std::sqrt(static_cast<double>(dimension));
    assert(interval.lower <= exact && exact <= interval.upper);
    assert(!screen.rejects(0, q, exact, false));
    assert(!screen.rejects(0, q, exact, true));
    ++checks;
  }
  std::cout << "PASS quantized intervals: " << checks
            << " cases; seed=20261112, mixed scales, clipped queries, tails, ties, ASC/DESC\n";
}
