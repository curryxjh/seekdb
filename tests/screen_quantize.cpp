// Exercise each implementation directly; production dispatch has no override.
#include "../minivectordb/screen.cpp"

#include <bit>
#include <cassert>
#include <iostream>
#include <random>

int main() {
  std::mt19937 random(20261114);
  size_t checks = 0, avx2_checks = 0;
  for (size_t dimension : {1U, 2U, 3U, 4U, 5U, 7U, 8U, 31U, 32U, 33U,
                           63U, 127U, 128U, 129U, 257U, 1024U, 4095U, 4096U}) {
    std::vector<float> values(dimension);
    std::vector<int8_t> scalar(dimension), actual(dimension);
    for (size_t trial = 0; trial < 128; ++trial) {
      double scale = 1;
      for (auto &value : values) {
        switch (trial % 8) {
          case 0: value = std::bit_cast<float>(static_cast<uint32_t>(random()) & 0xff7fffffU); break;
          case 1: value = std::ldexp(static_cast<float>(static_cast<int>(random() % 65537) - 32768), -20); break;
          case 2: value = static_cast<float>(static_cast<int>(random() % 511) - 255) *
                          std::numeric_limits<float>::denorm_min(); break;
          case 3: value = (random() % 2 == 0 ? 1 : -1) * std::numeric_limits<float>::max(); break;
          case 4: value = (random() % 2 == 0 ? 0.0f : -0.0f); break;
          default: {
            // Half-integers, plus their immediate float32 neighbours, with
            // power-of-two scaling so the intended ties are representable.
            const float half = static_cast<float>(static_cast<int>(random() % 511) - 255) + 0.5f;
            const int direction = static_cast<int>(random() % 3) - 1;
            value = direction == 0 ? half : std::nextafter(half, direction < 0 ?
                -std::numeric_limits<float>::infinity() : std::numeric_limits<float>::infinity());
            value = std::ldexp(value, static_cast<int>(trial) - 64);
          }
        }
      }
      const double maximum = mvdb::maximum_scalar(values);
      if (trial % 8 >= 5) scale = std::ldexp(1.0, static_cast<int>(trial) - 64);
      else if (trial % 3 == 0) scale = maximum == 0 ? 1 : maximum / 127;
      else if (trial % 3 == 1) scale = std::numeric_limits<float>::denorm_min() / 127.0;
      else scale = std::numeric_limits<float>::max() / 127.0;
      const auto expected = mvdb::quantize_scalar(values, scalar.data(), scale);
      const auto check = [&](const auto &result, const auto &codes) {
        assert(codes == scalar && result.norm == expected.norm && result.scale == scale);
        long double square = 0;
        int32_t norm = 0;
        for (size_t d = 0; d < dimension; ++d) {
          assert(codes[d] >= -127 && codes[d] <= 127);
          const long double residual = static_cast<long double>(values[d]) -
                                       static_cast<long double>(codes[d]) * scale;
          square += residual * residual;
          norm += static_cast<int32_t>(codes[d]) * codes[d];
        }
        assert(result.norm == norm);
        assert(std::sqrt(square) <= result.error);
      };
      check(expected, scalar); ++checks;
#if defined(__x86_64__)
      if (__builtin_cpu_supports("avx2")) {
        assert(mvdb::maximum_avx2(values) == maximum);
        const auto result = mvdb::quantize_avx2(values, actual.data(), scale);
        check(result, actual); ++avx2_checks;
      }
#endif
    }
  }
  // 1.5/nextafter(3,+inf) is the double immediately below one half:
  // adding 0.5 rounds it to 1 before truncation. Exercise it in a SIMD lane.
  const double below_half_scale = std::nextafter(3.0, 4.0);
  assert(1.5 / below_half_scale == std::nextafter(0.5, 0.0));
  assert(1.5 / below_half_scale + 0.5 == 1.0);
  for (double scale : {std::nextafter(2.0, 3.0), 2.0, below_half_scale}) {
    const std::vector<float> values{1, -1, 1.5f, -1.5f, 127, -127, 0, -0.0f, 3};
    std::vector<int8_t> scalar(values.size()), actual(values.size());
    const auto expected = mvdb::quantize_scalar(values, scalar.data(), scale);
#if defined(__x86_64__)
    if (__builtin_cpu_supports("avx2")) {
      const auto result = mvdb::quantize_avx2(values, actual.data(), scale);
      assert(actual == scalar && result.norm == expected.norm); ++avx2_checks;
    }
#else
    (void)expected;
#endif
    ++checks;
  }
  std::cout << "PASS quantization: scalar=" << checks << " AVX2=" << avx2_checks
            << "; seed=20261114, half ties/neighbours, clipping, finite float bits, subnormals, extremes, tails\n";
}
