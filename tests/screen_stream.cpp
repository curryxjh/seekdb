// Include the implementation in this test TU so all runtime-dispatched kernels
// can be compared explicitly, without adding a production override or API.
#include "../minivectordb/screen.cpp"

#include <cassert>
#include <iostream>
#include <numeric>
#include <random>

int main() {
  std::mt19937 random(20261114);
  size_t checks = 0;
  for (size_t dim : {32U, 33U, 63U, 64U, 65U, 127U, 128U, 129U, 255U, 1024U, 4095U, 4096U}) {
    constexpr size_t count = 71;
    std::vector<float> vectors(count * dim), query(dim);
    std::vector<int8_t> codes(count * dim);
    std::vector<int32_t> norms(count);
    mvdb::QuantizedScreen::Query q;
    q.codes.resize(dim);
    for (size_t d = 0; d < dim; ++d) {
      q.codes[d] = static_cast<int8_t>(static_cast<int>(random() % 255) - 127);
      q.norm += static_cast<int32_t>(q.codes[d]) * q.codes[d];
      query[d] = static_cast<float>(static_cast<int>(random() % 2001) - 1000) / 71;
    }
    for (size_t row = 0; row < count; ++row) for (size_t d = 0; d < dim; ++d) {
      const auto i = row * dim + d;
      codes[i] = static_cast<int8_t>(static_cast<int>(random() % 255) - 127);
      norms[row] += static_cast<int32_t>(codes[i]) * codes[i];
      vectors[i] = static_cast<float>(codes[i]) / 11;
    }
    std::vector<uint32_t> rows(count);
    std::iota(rows.begin(), rows.end(), 0);
    std::shuffle(rows.begin(), rows.end(), random);
    for (size_t amount : {0U, 1U, 3U, 4U, 5U, 17U, 71U}) {
      const std::span<const uint32_t> subset(rows.data(), amount);
      std::vector<int32_t> expected(amount), actual(amount);
      mvdb::distances_scalar(codes.data(), norms.data(), dim, subset, q, expected.data());
#if defined(__x86_64__)
      if (__builtin_cpu_supports("avx2")) {
        mvdb::distances_avx2(codes.data(), norms.data(), dim, subset, q, actual.data());
        assert(actual == expected); checks += amount;
      }
      if (__builtin_cpu_supports("avx512f") && __builtin_cpu_supports("avx512bw") &&
          __builtin_cpu_supports("avx512vnni")) {
        mvdb::distances_vnni(codes.data(), norms.data(), dim, subset, q, actual.data());
        assert(actual == expected); checks += amount;
      }
#endif
    }
    mvdb::QuantizedScreen contiguous, streaming;
    constexpr size_t budget = 1024 * 1024;
    contiguous.build(vectors, dim, budget);
    streaming.build(count, dim, [&](size_t row) -> std::span<const float> {
      return std::span(vectors).subspan(row * dim, dim);
    }, budget);
    const auto a = contiguous.query(query), b = streaming.query(query);
    assert(a.codes == b.codes && a.norm == b.norm && a.error == b.error);
    assert(contiguous.code_distances(rows, a) == streaming.code_distances(rows, b));
    for (size_t row = 0; row < count; ++row) {
      const auto interval = streaming.bounds(row, b);
      long double square = 0;
      for (size_t d = 0; d < dim; ++d) {
        const long double delta = static_cast<long double>(vectors[row * dim + d]) - query[d];
        square += delta * delta;
      }
      const auto exact = std::sqrt(square);
      assert(interval.lower <= exact && exact <= interval.upper);
      const auto code = streaming.code_distance(row, b);
      for (double worst : {0.0, static_cast<double>(exact) * 0.9,
                           static_cast<double>(exact), static_cast<double>(exact) * 1.1}) {
        if (exact <= worst) assert(code <= streaming.refined_cutoff(b, worst, false));
        if (exact >= worst) assert(code >= streaming.refined_cutoff(b, worst, true));
        ++checks;
      }
    }
    streaming.build(count, dim, [&](size_t row) -> std::span<const float> {
      return std::span(vectors).subspan(row * dim, dim);
    }, 1);
    assert(streaming.empty()); ++checks;
  }
  std::cout << "PASS streaming quantization, exact-seed cutoffs and scalar/AVX2/VNNI kernels: "
            << checks << " checks; seed=20261114\n";
}
