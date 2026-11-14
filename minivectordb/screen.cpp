#include "screen.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#if defined(__x86_64__)
#include <immintrin.h>
#endif
#if defined(__aarch64__) && defined(__ARM_FEATURE_DOTPROD)
#include <arm_neon.h>
#endif

namespace mvdb {
namespace {
struct Quantized { double scale, norm, error; };
Quantized quantize_scalar(std::span<const float> values, int8_t *codes, double scale) {
  double norm = 0, error = 0;
  constexpr double eps = std::numeric_limits<double>::epsilon();
  for (size_t d = 0; d < values.size(); ++d) {
    const double value = values[d];
    const auto code = static_cast<int8_t>(std::clamp(std::round(value / scale), -127.0, 127.0));
    codes[d] = code;
    norm += static_cast<double>(code) * code;
    // Includes the error of reconstructing code*scale in double, rather than
    // assuming exact real arithmetic in the quantizer.
    const double delta = std::abs(value - static_cast<double>(code) * scale) + std::abs(value) * 16 * eps;
    error += delta * delta;
  }
  error = std::nextafter(std::sqrt(error) * (1.0 + 64.0 * static_cast<double>(values.size()) * eps),
                         std::numeric_limits<double>::infinity());
  return {scale, norm, error};
}
double maximum_scalar(std::span<const float> values) {
  double maximum = 0;
  for (float value : values) maximum = std::max(maximum, std::abs(static_cast<double>(value)));
  return maximum;
}
#if defined(__x86_64__)
__attribute__((target("avx2")))
double maximum_avx2(std::span<const float> values) {
  const auto magnitude = _mm256_castsi256_ps(_mm256_set1_epi32(0x7fffffff));
  auto maximum = _mm256_setzero_ps();
  size_t d = 0;
  for (; d + 8 <= values.size(); d += 8)
    maximum = _mm256_max_ps(maximum, _mm256_and_ps(_mm256_loadu_ps(values.data() + d), magnitude));
  float lanes[8]; _mm256_storeu_ps(lanes, maximum);
  double result = 0;
  for (float value : lanes) result = std::max(result, static_cast<double>(value));
  for (; d < values.size(); ++d) result = std::max(result, std::abs(static_cast<double>(values[d])));
  return result;
}
__attribute__((target("avx2")))
Quantized quantize_avx2(std::span<const float> values, int8_t *codes, double scale) {
  constexpr double eps = std::numeric_limits<double>::epsilon();
  const auto sign = _mm256_set1_pd(-0.0), unit = _mm256_set1_pd(1.0);
  const auto scale_v = _mm256_set1_pd(scale), limit = _mm256_set1_pd(127.0);
  const auto half = _mm256_set1_pd(0.5), slack = _mm256_set1_pd(16 * eps);
  auto norms = _mm256_setzero_pd(), errors = norms;
  size_t d = 0;
  for (; d + 4 <= values.size(); d += 4) {
    const auto value = _mm256_cvtps_pd(_mm_loadu_ps(values.data() + d));
    const auto absolute = _mm256_andnot_pd(sign, value);
    const auto ratio = _mm256_min_pd(_mm256_div_pd(absolute, scale_v), limit);
    const auto integral = _mm256_round_pd(ratio, _MM_FROUND_TO_ZERO | _MM_FROUND_NO_EXC);
    // Preserve std::round's half-away-from-zero rule. Adding 0.5 before
    // truncation would incorrectly round the representable value below 0.5.
    const auto increment = _mm256_and_pd(
        _mm256_cmp_pd(_mm256_sub_pd(ratio, integral), half, _CMP_GE_OQ), unit);
    const auto code = _mm256_xor_pd(_mm256_add_pd(integral, increment), _mm256_and_pd(value, sign));
    const auto integers = _mm256_cvttpd_epi32(code);
    const auto words = _mm_packs_epi32(integers, _mm_setzero_si128());
    const int32_t packed = _mm_cvtsi128_si32(_mm_packs_epi16(words, _mm_setzero_si128()));
    std::memcpy(codes + d, &packed, sizeof(packed));
    norms = _mm256_add_pd(norms, _mm256_mul_pd(code, code));
    const auto residual = _mm256_andnot_pd(sign, _mm256_sub_pd(value, _mm256_mul_pd(code, scale_v)));
    const auto delta = _mm256_add_pd(residual, _mm256_mul_pd(absolute, slack));
    errors = _mm256_add_pd(errors, _mm256_mul_pd(delta, delta));
  }
  double norm_lanes[4], error_lanes[4];
  _mm256_storeu_pd(norm_lanes, norms); _mm256_storeu_pd(error_lanes, errors);
  double norm = (norm_lanes[0] + norm_lanes[1]) + (norm_lanes[2] + norm_lanes[3]);
  double error = (error_lanes[0] + error_lanes[1]) + (error_lanes[2] + error_lanes[3]);
  for (; d < values.size(); ++d) {
    const double value = values[d];
    const auto code = static_cast<int8_t>(std::clamp(std::round(value / scale), -127.0, 127.0));
    codes[d] = code; norm += static_cast<double>(code) * code;
    const double delta = std::abs(value - static_cast<double>(code) * scale) + std::abs(value) * 16 * eps;
    error += delta * delta;
  }
  // Positive partial sums have a shorter rounding path than the scalar
  // sum; the same 64*D*epsilon inflation covers both accumulation orders.
  error = std::nextafter(std::sqrt(error) * (1.0 + 64.0 * static_cast<double>(values.size()) * eps),
                         std::numeric_limits<double>::infinity());
  return {scale, norm, error};
}
#endif
using Quantize = Quantized (*)(std::span<const float>, int8_t *, double);
Quantize quantize_kernel() {
#if defined(__x86_64__)
  if (__builtin_cpu_supports("avx2")) return quantize_avx2;
#endif
  return quantize_scalar;
}
using Maximum = double (*)(std::span<const float>);
Maximum maximum_kernel() {
#if defined(__x86_64__)
  if (__builtin_cpu_supports("avx2")) return maximum_avx2;
#endif
  return maximum_scalar;
}
Quantized quantize(std::span<const float> values, int8_t *codes, double scale) {
  static const auto kernel = quantize_kernel();
  return kernel(values, codes, scale);
}
int32_t dot_scalar(const int8_t *a, const int8_t *b, size_t dimension) {
  int32_t sum = 0;
  for (size_t d = 0; d < dimension; ++d) sum += static_cast<int32_t>(a[d]) * b[d];
  return sum;
}
#if defined(__x86_64__)
__attribute__((target("avx2")))
int32_t dot_avx2(const int8_t *a, const int8_t *b, size_t dimension) {
  __m256i sums = _mm256_setzero_si256();
  size_t d = 0;
  const auto ones = _mm256_set1_epi16(1);
  for (; d + 32 <= dimension; d += 32) {
    const auto x = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(a + d));
    const auto y = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(b + d));
    // Excluding -128 guarantees abs/sign cannot overflow. Pair products
    // cannot saturate maddubs: 2*127*127=32258 < INT16_MAX.
    const auto pairs = _mm256_maddubs_epi16(_mm256_abs_epi8(x), _mm256_sign_epi8(y, x));
    sums = _mm256_add_epi32(sums, _mm256_madd_epi16(pairs, ones));
  }
  int32_t lanes[8]; _mm256_storeu_si256(reinterpret_cast<__m256i *>(lanes), sums);
  int32_t sum = 0;
  for (auto value : lanes) sum += value;
  for (; d < dimension; ++d) sum += static_cast<int32_t>(a[d]) * b[d];
  return sum;
}
#endif
#if defined(__aarch64__) && defined(__ARM_FEATURE_DOTPROD)
int32_t dot_neon(const int8_t *a, const int8_t *b, size_t dimension) {
  int32x4_t s0 = vdupq_n_s32(0), s1 = s0, s2 = s0, s3 = s0;
  size_t d = 0;
  for (; d + 64 <= dimension; d += 64) {
    s0 = vdotq_s32(s0, vld1q_s8(a + d), vld1q_s8(b + d));
    s1 = vdotq_s32(s1, vld1q_s8(a + d + 16), vld1q_s8(b + d + 16));
    s2 = vdotq_s32(s2, vld1q_s8(a + d + 32), vld1q_s8(b + d + 32));
    s3 = vdotq_s32(s3, vld1q_s8(a + d + 48), vld1q_s8(b + d + 48));
  }
  int32x4_t sums = vaddq_s32(vaddq_s32(s0, s1), vaddq_s32(s2, s3));
  for (; d + 16 <= dimension; d += 16) sums = vdotq_s32(sums, vld1q_s8(a + d), vld1q_s8(b + d));
  int32_t sum = vaddvq_s32(sums);
  for (; d < dimension; ++d) sum += static_cast<int32_t>(a[d]) * b[d];
  return sum;
}
#endif
using Dot = int32_t (*)(const int8_t *, const int8_t *, size_t);
Dot dot_kernel() {
#if defined(__x86_64__)
  if (__builtin_cpu_supports("avx2")) return dot_avx2;
#endif
#if defined(__aarch64__) && defined(__ARM_FEATURE_DOTPROD)
  return dot_neon;
#endif
  return dot_scalar;
}
void distances_scalar(const int8_t *codes, const int32_t *norms, size_t dimension,
                      std::span<const uint32_t> rows, const QuantizedScreen::Query &q, int32_t *out) {
  for (size_t n = 0; n < rows.size(); ++n) {
#if defined(__aarch64__) && defined(__ARM_FEATURE_DOTPROD)
    const int32_t dot = dot_neon(codes + rows[n] * dimension, q.codes.data(), dimension);
#else
    const int32_t dot = dot_scalar(codes + rows[n] * dimension, q.codes.data(), dimension);
#endif
    out[n] = norms[rows[n]] + q.norm - 2 * dot;
  }
}
#if defined(__x86_64__)
__attribute__((target("avx2")))
int32_t horizontal_avx2(__m256i sums) {
  const auto halves = _mm_add_epi32(_mm256_castsi256_si128(sums), _mm256_extracti128_si256(sums, 1));
  const auto pairs = _mm_add_epi32(halves, _mm_srli_si128(halves, 8));
  return _mm_cvtsi128_si32(_mm_add_epi32(pairs, _mm_srli_si128(pairs, 4)));
}
__attribute__((target("avx2")))
void distances_avx2(const int8_t *codes, const int32_t *norms, size_t dimension,
                    std::span<const uint32_t> rows, const QuantizedScreen::Query &q, int32_t *out) {
  const auto ones = _mm256_set1_epi16(1);
  size_t n = 0;
  for (; n + 4 <= rows.size(); n += 4) {
    const auto *r0 = codes + rows[n] * dimension;
    const auto *r1 = codes + rows[n + 1] * dimension;
    const auto *r2 = codes + rows[n + 2] * dimension;
    const auto *r3 = codes + rows[n + 3] * dimension;
    auto s0 = _mm256_setzero_si256(), s1 = s0, s2 = s0, s3 = s0;
    size_t d = 0;
    for (; d + 32 <= dimension; d += 32) {
      const auto qv = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(q.codes.data() + d));
      const auto x0 = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(r0 + d));
      const auto x1 = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(r1 + d));
      const auto x2 = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(r2 + d));
      const auto x3 = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(r3 + d));
      s0 = _mm256_add_epi32(s0, _mm256_madd_epi16(_mm256_maddubs_epi16(_mm256_abs_epi8(x0), _mm256_sign_epi8(qv, x0)), ones));
      s1 = _mm256_add_epi32(s1, _mm256_madd_epi16(_mm256_maddubs_epi16(_mm256_abs_epi8(x1), _mm256_sign_epi8(qv, x1)), ones));
      s2 = _mm256_add_epi32(s2, _mm256_madd_epi16(_mm256_maddubs_epi16(_mm256_abs_epi8(x2), _mm256_sign_epi8(qv, x2)), ones));
      s3 = _mm256_add_epi32(s3, _mm256_madd_epi16(_mm256_maddubs_epi16(_mm256_abs_epi8(x3), _mm256_sign_epi8(qv, x3)), ones));
    }
    int32_t dots[4]{horizontal_avx2(s0), horizontal_avx2(s1), horizontal_avx2(s2), horizontal_avx2(s3)};
    const int8_t *data[4]{r0, r1, r2, r3};
    for (size_t i = 0; i < 4; ++i) {
      for (size_t tail = d; tail < dimension; ++tail) dots[i] += static_cast<int32_t>(data[i][tail]) * q.codes[tail];
      out[n + i] = norms[rows[n + i]] + q.norm - 2 * dots[i];
    }
  }
  for (; n < rows.size(); ++n)
    out[n] = norms[rows[n]] + q.norm -
             2 * dot_avx2(codes + rows[n] * dimension, q.codes.data(), dimension);
}

// VNNI consumes unsigned*signed bytes. Shifting each stored code by 128 adds
// the same 128*sum(query) to every dot product; subtract it exactly below.
// The existing int32 bounds still hold, including intermediate accumulation.
__attribute__((target("avx512f,avx512bw,avx512vnni")))
void distances_vnni(const int8_t *codes, const int32_t *norms, size_t dimension,
                    std::span<const uint32_t> rows, const QuantizedScreen::Query &q, int32_t *out) {
  int32_t query_sum = 0;
  for (int8_t value : q.codes) query_sum += value;
  const int32_t correction = 256 * query_sum;
  const auto bias = _mm512_set1_epi8(static_cast<char>(-128));
  if (dimension == 128) {
    const auto q0 = _mm512_loadu_si512(q.codes.data());
    const auto q1 = _mm512_loadu_si512(q.codes.data() + 64);
    const int32_t adjust = q.norm + correction;
    const auto adjust4 = _mm_set1_epi32(adjust);
    size_t i = 0;
    for (; i + 4 <= rows.size(); i += 4) {
      const auto *r0 = codes + static_cast<size_t>(rows[i + 0]) * 128;
      auto s0 = _mm512_dpbusd_epi32(_mm512_setzero_si512(),
          _mm512_xor_si512(_mm512_loadu_si512(r0), bias), q0);
      const auto *r1 = codes + static_cast<size_t>(rows[i + 1]) * 128;
      auto s1 = _mm512_dpbusd_epi32(_mm512_setzero_si512(),
          _mm512_xor_si512(_mm512_loadu_si512(r1), bias), q0);
      const auto *r2 = codes + static_cast<size_t>(rows[i + 2]) * 128;
      auto s2 = _mm512_dpbusd_epi32(_mm512_setzero_si512(),
          _mm512_xor_si512(_mm512_loadu_si512(r2), bias), q0);
      const auto *r3 = codes + static_cast<size_t>(rows[i + 3]) * 128;
      auto s3 = _mm512_dpbusd_epi32(_mm512_setzero_si512(),
          _mm512_xor_si512(_mm512_loadu_si512(r3), bias), q0);
      s0 = _mm512_dpbusd_epi32(s0, _mm512_xor_si512(_mm512_loadu_si512(r0 + 64), bias), q1);
      s1 = _mm512_dpbusd_epi32(s1, _mm512_xor_si512(_mm512_loadu_si512(r1 + 64), bias), q1);
      s2 = _mm512_dpbusd_epi32(s2, _mm512_xor_si512(_mm512_loadu_si512(r2 + 64), bias), q1);
      s3 = _mm512_dpbusd_epi32(s3, _mm512_xor_si512(_mm512_loadu_si512(r3 + 64), bias), q1);
      const auto m0 = _mm256_add_epi32(_mm512_castsi512_si256(s0), _mm512_extracti64x4_epi64(s0, 1));
      const auto n0 = _mm_add_epi32(_mm256_castsi256_si128(m0), _mm256_extracti128_si256(m0, 1));
      const auto m1 = _mm256_add_epi32(_mm512_castsi512_si256(s1), _mm512_extracti64x4_epi64(s1, 1));
      const auto n1 = _mm_add_epi32(_mm256_castsi256_si128(m1), _mm256_extracti128_si256(m1, 1));
      const auto m2 = _mm256_add_epi32(_mm512_castsi512_si256(s2), _mm512_extracti64x4_epi64(s2, 1));
      const auto n2 = _mm_add_epi32(_mm256_castsi256_si128(m2), _mm256_extracti128_si256(m2, 1));
      const auto m3 = _mm256_add_epi32(_mm512_castsi512_si256(s3), _mm512_extracti64x4_epi64(s3, 1));
      const auto n3 = _mm_add_epi32(_mm256_castsi256_si128(m3), _mm256_extracti128_si256(m3, 1));
      const auto dots4 = _mm_hadd_epi32(_mm_hadd_epi32(n0, n1), _mm_hadd_epi32(n2, n3));
      const auto norms4 = _mm_setr_epi32(norms[rows[i]], norms[rows[i + 1]], norms[rows[i + 2]], norms[rows[i + 3]]);
      const auto result4 = _mm_sub_epi32(_mm_add_epi32(norms4, adjust4), _mm_slli_epi32(dots4, 1));
      _mm_storeu_si128(reinterpret_cast<__m128i *>(out + i), result4);
    }
    for (; i < rows.size(); ++i) {
      const auto *data = codes + static_cast<size_t>(rows[i]) * 128;
      auto sum = _mm512_dpbusd_epi32(_mm512_setzero_si512(),
          _mm512_xor_si512(_mm512_loadu_si512(data), bias), q0);
      sum = _mm512_dpbusd_epi32(sum,
          _mm512_xor_si512(_mm512_loadu_si512(data + 64), bias), q1);
      out[i] = norms[rows[i]] + adjust - 2 * _mm512_reduce_add_epi32(sum);
    }
    return;
  }
  size_t n = 0;
  for (; n + 4 <= rows.size(); n += 4) {
    const auto *r0 = codes + rows[n] * dimension;
    const auto *r1 = codes + rows[n + 1] * dimension;
    const auto *r2 = codes + rows[n + 2] * dimension;
    const auto *r3 = codes + rows[n + 3] * dimension;
    auto s0 = _mm512_setzero_si512(), s1 = s0, s2 = s0, s3 = s0;
    size_t d = 0;
    for (; d + 64 <= dimension; d += 64) {
      const auto b = _mm512_loadu_si512(q.codes.data() + d);
      s0 = _mm512_dpbusd_epi32(s0, _mm512_xor_si512(_mm512_loadu_si512(r0 + d), bias), b);
      s1 = _mm512_dpbusd_epi32(s1, _mm512_xor_si512(_mm512_loadu_si512(r1 + d), bias), b);
      s2 = _mm512_dpbusd_epi32(s2, _mm512_xor_si512(_mm512_loadu_si512(r2 + d), bias), b);
      s3 = _mm512_dpbusd_epi32(s3, _mm512_xor_si512(_mm512_loadu_si512(r3 + d), bias), b);
    }
    if (d < dimension) {
      const auto mask = static_cast<__mmask64>((uint64_t{1} << (dimension - d)) - 1);
      const auto b = _mm512_maskz_loadu_epi8(mask, q.codes.data() + d);
      s0 = _mm512_dpbusd_epi32(s0, _mm512_xor_si512(_mm512_maskz_loadu_epi8(mask, r0 + d), bias), b);
      s1 = _mm512_dpbusd_epi32(s1, _mm512_xor_si512(_mm512_maskz_loadu_epi8(mask, r1 + d), bias), b);
      s2 = _mm512_dpbusd_epi32(s2, _mm512_xor_si512(_mm512_maskz_loadu_epi8(mask, r2 + d), bias), b);
      s3 = _mm512_dpbusd_epi32(s3, _mm512_xor_si512(_mm512_maskz_loadu_epi8(mask, r3 + d), bias), b);
    }
    int32_t dots[4]{_mm512_reduce_add_epi32(s0), _mm512_reduce_add_epi32(s1),
                     _mm512_reduce_add_epi32(s2), _mm512_reduce_add_epi32(s3)};
    for (size_t i = 0; i < 4; ++i)
      out[n + i] = norms[rows[n + i]] + q.norm + correction - 2 * dots[i];
  }
  for (; n < rows.size(); ++n) {
    const auto *data = codes + rows[n] * dimension;
    auto sums = _mm512_setzero_si512();
    size_t d = 0;
    for (; d + 64 <= dimension; d += 64) {
      const auto a = _mm512_xor_si512(_mm512_loadu_si512(data + d), bias);
      const auto b = _mm512_loadu_si512(q.codes.data() + d);
      sums = _mm512_dpbusd_epi32(sums, a, b);
    }
    if (d < dimension) {
      const auto mask = static_cast<__mmask64>((uint64_t{1} << (dimension - d)) - 1);
      const auto a = _mm512_xor_si512(_mm512_maskz_loadu_epi8(mask, data + d), bias);
      const auto b = _mm512_maskz_loadu_epi8(mask, q.codes.data() + d);
      sums = _mm512_dpbusd_epi32(sums, a, b);
    }
    const int32_t dot = _mm512_reduce_add_epi32(sums);
    out[n] = norms[rows[n]] + q.norm + correction - 2 * dot;
  }
}
#endif
}  // namespace

void QuantizedScreen::build(std::span<const float> vectors, size_t dimension, size_t budget) {
  if (dimension == 0) { *this = {}; return; }
  build(vectors.size() / dimension, dimension, [&](size_t row) {
    return vectors.subspan(row * dimension, dimension);
  }, budget);
}

void QuantizedScreen::build(size_t count, size_t dimension,
                            const std::function<std::span<const float>(size_t)> &read,
                            size_t budget) {
  codes_.clear(); norms_.clear(); errors_.clear(); dimension_ = 0; maximum_error_ = 0;
  if (dimension < 32 || dimension > 4096 || count == 0) return;
  if (count > budget / (dimension + sizeof(int32_t) + sizeof(double))) return;
  dimension_ = dimension;
  static const auto maximum_absolute = maximum_kernel();
  double maximum = 0;
  // Scale selection affects selectivity, never correctness: quantize records
  // the actual reconstruction error, including every clipped component.
  // A bounded, evenly spaced sample avoids a second full vector scan.
  const size_t sample_count = std::min(count, size_t{1024});
  for (size_t i = 0; i < sample_count; ++i) {
    const size_t row = sample_count == 1 ? 0 : i * (count - 1) / (sample_count - 1);
    maximum = std::max(maximum, maximum_absolute(read(row)));
  }
  if (sample_count < count) maximum *= 1.25;
  scale_ = maximum == 0 ? 1.0 : maximum / 127.0;
  codes_.resize(count * dimension); norms_.reserve(count); errors_.reserve(count);
  for (size_t row = 0; row < count; ++row) {
    const auto p = quantize(read(row), codes_.data() + row * dimension, scale_);
    norms_.push_back(static_cast<int32_t>(p.norm)); errors_.push_back(p.error);
    maximum_error_ = std::max(maximum_error_, p.error);
  }
}
QuantizedScreen::Query QuantizedScreen::query(std::span<const float> vector) const {
  Query q;
  if (empty()) return q;
  q.codes.resize(vector.size());
  const auto p = quantize(vector, q.codes.data(), scale_);
  q.norm = static_cast<int32_t>(p.norm); q.error = p.error;
  return q;
}
int32_t QuantizedScreen::code_distance(size_t row, const Query &q) const {
  static const auto dot = dot_kernel();
  return norms_[row] + q.norm -
         2 * dot(codes_.data() + row * dimension_, q.codes.data(), dimension_);
}
std::vector<int32_t> QuantizedScreen::code_distances(std::span<const uint32_t> rows, const Query &q) const {
  std::vector<int32_t> result(rows.size());
#if defined(__x86_64__)
  if (__builtin_cpu_supports("avx512f") && __builtin_cpu_supports("avx512bw") &&
      __builtin_cpu_supports("avx512vnni")) {
    distances_vnni(codes_.data(), norms_.data(), dimension_, rows, q, result.data());
    return result;
  }
  if (__builtin_cpu_supports("avx2")) {
    distances_avx2(codes_.data(), norms_.data(), dimension_, rows, q, result.data());
    return result;
  }
#endif
  distances_scalar(codes_.data(), norms_.data(), dimension_, rows, q, result.data());
  return result;
}
double QuantizedScreen::code_cutoff(const Query &q, int32_t kth, bool descending) const {
  // Every approximate distance is at most E from the original. K approximate
  // winners therefore bound the true cutoff, and candidates more than 2E
  // beyond that approximate cutoff cannot win. Compute two square roots per
  // query rather than per record. One integer unit covers final rounding.
  const double error = 2 * (maximum_error_ + q.error) / scale_;
  const double root = std::sqrt(static_cast<double>(kth));
  if (descending) {
    const double lower = std::max(0.0, root - error);
    return std::max(0.0, lower * lower * (1 - 128 * std::numeric_limits<double>::epsilon()) - 1);
  }
  const double upper = root + error;
  return upper * upper * (1 + 128 * std::numeric_limits<double>::epsilon()) + 1;
}
bool QuantizedScreen::rejects(size_t row, const Query &q, double worst, bool descending) const {
  static const auto dot = dot_kernel();
  // With one scale, norm+norm-2*dot is an exact integer <=4096*254^2.
  const double estimate = norms_[row] + q.norm - 2.0 * dot(codes_.data() + row * dimension_, q.codes.data(), dimension_);
  const double error = (errors_[row] + q.error) * (1 + 16 * std::numeric_limits<double>::epsilon());
  if (descending) {
    const double lower = std::max(0.0, worst - error) / scale_;
    return estimate * (1 + 64 * std::numeric_limits<double>::epsilon()) < lower * lower;
  }
  const double upper = (worst + error) / scale_;
  return estimate * (1 - 64 * std::numeric_limits<double>::epsilon()) > upper * upper;
}

double QuantizedScreen::refined_cutoff(const Query &q, double worst, bool descending) const {
  // The heap contains K actual candidates. Only one reconstruction error is
  // needed to compare each remaining code distance against that exact cutoff.
  constexpr double eps = std::numeric_limits<double>::epsilon();
  const double error = (maximum_error_ + q.error) * (1 + 16 * eps);
  if (descending) {
    const double lower = std::max(0.0, std::nextafter(worst, -INFINITY) - error) / scale_;
    return std::max(0.0, lower * lower / (1 + 64 * eps) - 1);
  }
  const double upper = (std::nextafter(worst, INFINITY) + error) / scale_;
  return upper * upper / (1 - 64 * eps) + 1;
}

QuantizedScreen::Interval QuantizedScreen::bounds(size_t row, const Query &q) const {
  static const auto dot = dot_kernel();
  const double estimate = norms_[row] + q.norm - 2.0 * dot(codes_.data() + row * dimension_, q.codes.data(), dimension_);
  const double error = (errors_[row] + q.error) * (1 + 16 * std::numeric_limits<double>::epsilon());
  const double lower = std::sqrt(std::max(0.0, estimate)) * scale_;
  const double upper = lower;
  const double rounding = upper * 32 * std::numeric_limits<double>::epsilon();
  return {std::max(0.0, lower - error - rounding), upper + error + rounding};
}

}  // namespace mvdb
