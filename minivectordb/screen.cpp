#include "screen.h"

#include <algorithm>
#include <cmath>
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
template<class Integer>
Quantized quantize(std::span<const float> values, Integer *codes, double scale) {
  double norm = 0, error = 0;
  constexpr double eps = std::numeric_limits<double>::epsilon();
  for (size_t d = 0; d < values.size(); ++d) {
    const double value = values[d];
    const auto code = static_cast<Integer>(std::clamp(std::round(value / scale), -127.0, 127.0));
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
void distances_avx2(const int8_t *codes, const int32_t *norms, size_t dimension,
                    std::span<const uint32_t> rows, const QuantizedScreen::Query &q, int32_t *out) {
  for (size_t n = 0; n < rows.size(); ++n)
    out[n] = norms[rows[n]] + q.norm -
             2 * dot_avx2(codes + rows[n] * dimension, q.codes.data(), dimension);
}
#endif
}  // namespace

void QuantizedScreen::build(std::span<const float> vectors, size_t dimension) {
  codes_.clear(); norms_.clear(); errors_.clear(); dimension_ = 0; maximum_error_ = 0;
  if (dimension < 32 || dimension > 4096 || vectors.empty()) return;
  const size_t count = vectors.size() / dimension;
  if (vectors.size() + count * (sizeof(int32_t) + sizeof(double)) > 3U * 1024U * 1024U) return;
  dimension_ = dimension;
  double maximum = 0;
  for (float value : vectors) maximum = std::max(maximum, std::abs(static_cast<double>(value)));
  scale_ = maximum == 0 ? 1.0 : maximum / 127.0;
  codes_.resize(vectors.size()); norms_.reserve(count); errors_.reserve(count);
  for (size_t row = 0; row < count; ++row) {
    const auto p = quantize(vectors.subspan(row * dimension, dimension), codes_.data() + row * dimension, scale_);
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
