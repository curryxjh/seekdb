#pragma once

#include "storage.h"

#include <cstdio>
#include <memory>
#include <variant>

namespace mvdb {

using Value = std::variant<int64_t, std::string, std::vector<float>, double>;
using Row = std::vector<Value>;

// Complete responses before emission; spill larger results to bounded disk runs.
class Spool {
 public:
  explicit Spool(const std::string &directory);
  ~Spool();
  Spool(Spool &&other) noexcept;
  Spool &operator=(Spool &&other) noexcept;
  Spool(const Spool &) = delete;
  void write(std::string_view bytes);
  void start_read();
  bool read(void *destination, size_t size);
  void output(FILE *stream, std::string_view id);
 private:
  std::string directory_;
  std::string buffer_;
  size_t position_ = 0;
  FILE *file_ = nullptr;
};

class Database {
 public:
  explicit Database(const std::string &directory);
  Spool execute(const Statement &s);
  void shutdown();
  const std::string &directory() const { return storage_.directory(); }
 private:
  Storage storage_;
  Catalog catalog_;
  Catalog committed_;
  uint64_t committed_end_ = 16;
  bool transaction_ = false;
  struct ScanRow { int64_t id; uint64_t offset; };
  // One table's prefix, at most 8 MiB; larger tables still stream their tail.
  std::vector<ScanRow> scan_rows_;
  std::vector<float> scan_vectors_;
  struct ScanFilter { std::string key; std::vector<uint64_t> bits; };
  // Four packed predicate masks, bounded by the cached prefix, not table size.
  std::vector<ScanFilter> scan_filters_;
  size_t scan_filter_next_ = 0;
  uint64_t scan_root_ = 0;
  bool scan_complete_ = false;
  Table &table(const std::string &name);
  uint32_t column(const Table &t, const std::string &name) const;
  Value convert(const Column &c, const Literal &value) const;
  Row decode(const Table &t, uint64_t offset);
  uint64_t encode(const Table &t, const Row &row);
  void put_row(Table &t, const Row &row, uint64_t boundary);
  Spool select(const Statement &s, const Table &t);
  void commit();
};

std::string value_json(const Value &value);

}  // namespace mvdb
