#pragma once

#include "storage.h"

#include <cstdio>
#include <memory>
#include <variant>

namespace mvdb {

using Value = std::variant<int64_t, std::string, std::vector<float>, double>;
using Row = std::vector<Value>;

// Results are completed on disk before any successful response is emitted.
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
  void output(FILE *stream);
 private:
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
