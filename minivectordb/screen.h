#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace mvdb {

// Bounded, lossy *screening* data. Triangle-inequality error bounds make
// rejection conservative; every remaining candidate uses original float32 data.
// This is an exact-search accelerator, never an approximate result source.
class QuantizedScreen {
 public:
  struct Query {
    std::vector<int8_t> codes;
    int32_t norm = 0;
    double error = 0;
  };
  struct Interval { double lower, upper; };
  void build(std::span<const float> vectors, size_t dimension,
             size_t budget = 3U * 1024U * 1024U);
  bool empty() const { return norms_.empty(); }
  Query query(std::span<const float> vector) const;
  int32_t code_distance(size_t row, const Query &query) const;
  std::vector<int32_t> code_distances(std::span<const uint32_t> rows, const Query &query) const;
  double code_cutoff(const Query &query, int32_t kth, bool descending) const;
  Interval bounds(size_t row, const Query &query) const;
  bool rejects(size_t row, const Query &query, double worst, bool descending) const;
 private:
  size_t dimension_ = 0;
  double scale_ = 1;
  double maximum_error_ = 0;
  std::vector<int8_t> codes_;
  std::vector<int32_t> norms_;
  std::vector<double> errors_;
};

}  // namespace mvdb
