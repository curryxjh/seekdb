#include "database.h"

#include <algorithm>
#include <bit>
#include <charconv>
#include <cmath>
#include <cstring>
#include <limits>
#include <queue>
#include <set>
#include <unistd.h>

namespace mvdb {
namespace {
void checked(bool ok) { require(ok, "io_error", "temporary result I/O failed"); }
std::string decimal(double value) {
  char text[64];
  auto result = std::to_chars(text, text + sizeof(text), value, std::chars_format::general,
                              std::numeric_limits<double>::max_digits10);
  require(result.ec == std::errc{}, "internal", "float formatting failed");
  return {text, result.ptr};
}
int compare(const Value &a, const Value &b) {
  require(a.index() == b.index(), "type_mismatch", "incompatible comparison types");
  if (auto n = std::get_if<int64_t>(&a)) {
    auto m = std::get<int64_t>(b); return (*n > m) - (*n < m);
  }
  if (auto str = std::get_if<std::string>(&a)) {
    // Valid UTF-8 is lexicographically ordered by Unicode scalar value.
    auto &other = std::get<std::string>(b);
    const int result = str->compare(other); return (result > 0) - (result < 0);
  }
  if (auto n = std::get_if<double>(&a)) {
    auto m = std::get<double>(b); return (*n > m) - (*n < m);
  }
  throw Error("unsupported", "vector comparison requires L2_DISTANCE");
}
struct BoundPredicate { uint32_t column; std::string op; Value value; };
bool matches(const Row &row, const std::vector<BoundPredicate> &where) {
  for (const auto &p : where) {
    const int result = compare(row[p.column], p.value);
    if (p.op == "=" && result != 0) return false;
    if (p.op == "<" && result >= 0) return false;
    if (p.op == "<=" && result > 0) return false;
    if (p.op == ">" && result <= 0) return false;
    if (p.op == ">=" && result < 0) return false;
  }
  return true;
}
double distance(const std::vector<float> &a, const std::vector<float> &b) {
  double sum = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double delta = static_cast<double>(a[i]) - static_cast<double>(b[i]);
    sum += delta * delta;
  }
  return std::sqrt(sum);
}
struct BoundExpr {
  uint32_t column;
  bool distance;
  std::vector<float> query;
  std::string label;
  Value eval(const Row &row) const {
    if (!distance) return row[column];
    return mvdb::distance(std::get<std::vector<float>>(row[column]), query);
  }
};
struct Candidate {
  Value key;
  int64_t id = 0;
  uint64_t offset = 0;
};
class Sorter {
 public:
  Sorter(const std::string &directory, bool descending)
      : directory_(directory), descending_(descending) {}
  bool better(const Candidate &a, const Candidate &b) const {
    int order = compare(a.key, b.key);
    if (order != 0) return descending_ ? order > 0 : order < 0;
    return a.id < b.id;
  }
  void add(Candidate value) {
    size_t amount = sizeof(Candidate);
    if (auto str = std::get_if<std::string>(&value.key)) amount += str->size();
    memory_ += amount; buffer_.push_back(std::move(value));
    if (memory_ >= 4U * 1024U * 1024U) flush();
  }
  void finish(uint64_t limit, const std::function<void(const Candidate &)> &visit) {
    if (runs_.empty()) {
      std::sort(buffer_.begin(), buffer_.end(), [&](const auto &a, const auto &b) { return better(a, b); });
      for (const auto &value : buffer_) { if (limit-- == 0) break; visit(value); }
      return;
    }
    flush();
    std::unique_ptr<Spool> result;
    for (auto &run : runs_) {
      if (!run) continue;
      if (!result) result = std::move(run);
      else result = merge(*result, *run);
    }
    if (!result) return;
    result->start_read();
    Candidate value;
    while (limit != 0 && read(*result, value)) { visit(value); --limit; }
  }
 private:
  std::string directory_;
  bool descending_;
  size_t memory_ = 0;
  std::vector<Candidate> buffer_;
  std::vector<std::unique_ptr<Spool>> runs_;
  static void write(Spool &file, const Candidate &value) {
    std::string out;
    append_u64(out, std::bit_cast<uint64_t>(value.id)); append_u64(out, value.offset);
    out += static_cast<char>(value.key.index());
    if (auto n = std::get_if<int64_t>(&value.key)) append_u64(out, std::bit_cast<uint64_t>(*n));
    else if (auto str = std::get_if<std::string>(&value.key)) append_string(out, *str);
    else append_u64(out, std::bit_cast<uint64_t>(std::get<double>(value.key)));
    std::string frame; append_u32(frame, static_cast<uint32_t>(out.size())); frame += out;
    file.write(frame);
  }
  static bool read(Spool &file, Candidate &value) {
    std::array<char, 4> header{};
    if (!file.read(header.data(), header.size())) return false;
    std::string_view view(header.data(), header.size());
    const auto length = read_u32(view);
    std::string body(length, '\0'); checked(file.read(body.data(), body.size()));
    view = body; value.id = std::bit_cast<int64_t>(read_u64(view)); value.offset = read_u64(view);
    const auto type = static_cast<unsigned char>(read_bytes(view, 1).front());
    if (type == 0) value.key = std::bit_cast<int64_t>(read_u64(view));
    else if (type == 1) value.key = read_string(view);
    else { require(type == 3, "internal", "invalid sort key"); value.key = std::bit_cast<double>(read_u64(view)); }
    require(view.empty(), "internal", "invalid sort run");
    return true;
  }
  std::unique_ptr<Spool> merge(Spool &a, Spool &b) {
    auto out = std::make_unique<Spool>(directory_);
    a.start_read(); b.start_read(); Candidate av, bv;
    bool have_a = read(a, av), have_b = read(b, bv);
    while (have_a || have_b) {
      if (!have_b || (have_a && better(av, bv))) { write(*out, av); have_a = read(a, av); }
      else { write(*out, bv); have_b = read(b, bv); }
    }
    return out;
  }
  void flush() {
    if (buffer_.empty()) return;
    std::sort(buffer_.begin(), buffer_.end(), [&](const auto &a, const auto &b) { return better(a, b); });
    auto run = std::make_unique<Spool>(directory_);
    for (const auto &value : buffer_) write(*run, value);
    buffer_.clear(); memory_ = 0;
    size_t level = 0;
    while (level < runs_.size() && runs_[level]) {
      run = merge(*runs_[level], *run); runs_[level].reset(); ++level;
    }
    if (level == runs_.size()) runs_.push_back(nullptr);
    runs_[level] = std::move(run);
  }
};
}  // namespace

Spool::Spool(const std::string &directory) {
  std::string pattern = directory + "/spool-XXXXXX";
  const int fd = ::mkstemp(pattern.data());
  require(fd >= 0, "io_error", "cannot create temporary result");
  if (::unlink(pattern.c_str()) != 0) { ::close(fd); throw Error("io_error", "cannot unlink temporary result"); }
  file_ = ::fdopen(fd, "w+b");
  if (file_ == nullptr) { ::close(fd); throw Error("io_error", "cannot open temporary result"); }
}
Spool::~Spool() { if (file_ != nullptr) std::fclose(file_); }
Spool::Spool(Spool &&other) noexcept : file_(other.file_) { other.file_ = nullptr; }
Spool &Spool::operator=(Spool &&other) noexcept {
  if (this != &other) {
    if (file_ != nullptr) std::fclose(file_);
    file_ = other.file_; other.file_ = nullptr;
  }
  return *this;
}
void Spool::write(std::string_view value) {
  checked(std::fwrite(value.data(), 1, value.size(), file_) == value.size());
}
void Spool::start_read() { checked(std::fseek(file_, 0, SEEK_SET) == 0); }
bool Spool::read(void *destination, size_t size) {
  const auto n = std::fread(destination, 1, size, file_);
  if (n == 0 && std::feof(file_)) return false;
  checked(n == size); return true;
}
void Spool::output(FILE *stream) {
  start_read(); std::array<char, 16384> buffer{};
  while (true) {
    const auto n = std::fread(buffer.data(), 1, buffer.size(), file_);
    checked(std::ferror(file_) == 0);
    if (n == 0) break;
    checked(std::fwrite(buffer.data(), 1, n, stream) == n);
  }
}

std::string value_json(const Value &value) {
  if (auto n = std::get_if<int64_t>(&value)) return std::to_string(*n);
  if (auto str = std::get_if<std::string>(&value)) return json_string(*str);
  if (auto n = std::get_if<double>(&value)) return decimal(*n);
  const auto &vector = std::get<std::vector<float>>(value);
  std::string out = "[";
  for (size_t i = 0; i < vector.size(); ++i) { if (i != 0) out += ','; out += decimal(vector[i]); }
  out += ']'; return out;
}

Database::Database(const std::string &directory) : storage_(directory) {
  catalog_ = storage_.load(); committed_ = catalog_; committed_end_ = storage_.end();
}
Table &Database::table(const std::string &name) {
  auto it = catalog_.find(name);
  require(it != catalog_.end(), "unknown_table", "table does not exist: " + name); return it->second;
}
uint32_t Database::column(const Table &t, const std::string &name) const {
  for (size_t i = 0; i < t.columns.size(); ++i) {
    if (t.columns[i].name == name) return static_cast<uint32_t>(i);
  }
  throw Error("unknown_column", "column does not exist: " + name);
}
Value Database::convert(const Column &c, const Literal &value) const {
  if (c.type == Type::Bigint) {
    require(!value.quoted, "type_mismatch", "BIGINT requires integer literal");
    return bigint(value.text);
  }
  if (c.type == Type::Varchar) {
    require(value.quoted, "type_mismatch", "VARCHAR requires string literal");
    require(unicode_length(value.text) <= c.size, "value_too_long", "VARCHAR character limit exceeded");
    return value.text;
  }
  return vector_literal(value.text, c.size);
}
Row Database::decode(const Table &t, uint64_t offset) {
  auto bytes = storage_.blob(offset);
  Row row; row.reserve(t.columns.size());
  for (const auto &c : t.columns) {
    if (c.type == Type::Bigint) row.emplace_back(std::bit_cast<int64_t>(read_u64(bytes)));
    else if (c.type == Type::Varchar) row.emplace_back(read_string(bytes));
    else {
      std::vector<float> vector; vector.reserve(c.size);
      for (uint32_t i = 0; i < c.size; ++i) vector.push_back(std::bit_cast<float>(read_u32(bytes)));
      row.emplace_back(std::move(vector));
    }
  }
  require(bytes.empty(), "corrupt_data", "trailing row data"); return row;
}
uint64_t Database::encode(const Table &t, const Row &row) {
  std::string out;
  for (size_t i = 0; i < row.size(); ++i) {
    if (t.columns[i].type == Type::Bigint) append_u64(out, std::bit_cast<uint64_t>(std::get<int64_t>(row[i])));
    else if (t.columns[i].type == Type::Varchar) append_string(out, std::get<std::string>(row[i]));
    else for (float f : std::get<std::vector<float>>(row[i])) append_u32(out, std::bit_cast<uint32_t>(f));
  }
  return storage_.blob(out);
}
void Database::put_row(Table &t, const Row &row, uint64_t boundary) {
  const auto id = std::get<int64_t>(row[t.primary]);
  const auto offset = encode(t, row);
  t.root = storage_.put(t.root, id, offset, boundary);
  if (!t.index_name.empty()) {
    std::string posting; append_u64(posting, offset);
    for (float f : std::get<std::vector<float>>(row[static_cast<size_t>(t.vector)])) {
      append_u32(posting, std::bit_cast<uint32_t>(f));
    }
    t.vector_root = storage_.put(t.vector_root, id, storage_.blob(posting), boundary);
  }
}
void Database::commit() {
  storage_.publish(catalog_);
  // Allocation failure here cannot be reported as a failed commit after publication.
  try { committed_ = catalog_; committed_end_ = storage_.end(); transaction_ = false; }
  catch (...) { ::_exit(74); }
}
void Database::shutdown() {
  if (transaction_) { catalog_ = committed_; storage_.rewind(committed_end_); transaction_ = false; }
}

Spool Database::execute(const Statement &s) {
  if (s.kind == Kind::Select) return select(s, table(s.table));
  Spool result(directory());
  if (s.kind == Kind::Begin) {
    require(!transaction_, "transaction_active", "nested transaction is not supported");
    result.write("{\"ok\":true}"); transaction_ = true; return result;
  }
  if (s.kind == Kind::Commit) {
    result.write("{\"ok\":true}"); if (transaction_) commit(); return result;
  }
  if (s.kind == Kind::Rollback) {
    result.write("{\"ok\":true}"); shutdown(); return result;
  }
  const Catalog savepoint = catalog_;
  const uint64_t boundary = storage_.end();
  uint64_t affected = 0;
  try {
    if (s.kind == Kind::Create) {
      require(!catalog_.contains(s.table), "table_exists", "table already exists");
      require(catalog_.size() < 4096 && !s.columns.empty() && s.columns.size() <= 1024,
              "resource_limit", "local schema count limit exceeded");
      Table t; t.columns = s.columns;
      unsigned primary_count = 0, vector_count = 0;
      std::set<std::string> seen;
      for (size_t i = 0; i < t.columns.size(); ++i) {
        const auto &c = t.columns[i];
        require(seen.insert(c.name).second, "duplicate_column", "duplicate column");
        if (c.primary) {
          require(c.type == Type::Bigint, "invalid_schema", "primary key must be BIGINT");
          ++primary_count; t.primary = static_cast<uint32_t>(i);
        }
        if (c.type == Type::Vector) { ++vector_count; t.vector = static_cast<int32_t>(i); }
      }
      require(primary_count == 1 && vector_count <= 1, "invalid_schema", "requires one primary key and at most one vector");
      catalog_.emplace(s.table, std::move(t));
    } else {
      Table &t = table(s.table);
      if (s.kind == Kind::Index) {
        const auto c = column(t, s.names.front());
        require(t.columns[c].type == Type::Vector, "invalid_schema", "index requires VECTOR column");
        require(t.index_name.empty(), "index_exists", "table already has a vector index");
        for (const auto &[name, other] : catalog_) {
          (void)name;
          require(other.index_name != s.index, "index_exists", "index name already exists");
        }
        t.index_name = s.index;
        storage_.scan(t.root, [&](int64_t id, uint64_t offset) {
          Row row = decode(t, offset);
          std::string posting; append_u64(posting, offset);
          for (float f : std::get<std::vector<float>>(row[c])) append_u32(posting, std::bit_cast<uint32_t>(f));
          t.vector_root = storage_.put(t.vector_root, id, storage_.blob(posting), boundary);
          return true;
        });
      } else if (s.kind == Kind::Insert) {
        std::vector<uint32_t> mapping;
        if (s.names.empty()) {
          for (size_t i = 0; i < t.columns.size(); ++i) mapping.push_back(static_cast<uint32_t>(i));
        } else for (const auto &name : s.names) mapping.push_back(column(t, name));
        std::set<uint32_t> seen(mapping.begin(), mapping.end());
        require(mapping.size() == t.columns.size() && seen.size() == t.columns.size(),
                "invalid_columns", "INSERT must supply every column exactly once");
        for (const auto &literals : s.rows) {
          require(literals.size() == mapping.size(), "invalid_columns", "INSERT value count mismatch");
          Row row(t.columns.size());
          for (size_t i = 0; i < literals.size(); ++i) row[mapping[i]] = convert(t.columns[mapping[i]], literals[i]);
          const auto id = std::get<int64_t>(row[t.primary]);
          require(storage_.find(t.root, id) == 0, "duplicate_key", "duplicate primary key");
          put_row(t, row, boundary); ++affected;
        }
      } else {
        std::vector<BoundPredicate> where;
        for (const auto &p : s.where) {
          auto c = column(t, p.column);
          require(t.columns[c].type != Type::Vector, "unsupported", "direct vector predicate");
          Column type = t.columns[c];
          if (type.type == Type::Varchar) type.size = UINT32_MAX;
          where.push_back({c, p.op, convert(type, p.value)});
        }
        std::vector<std::pair<uint32_t, Value>> updates;
        std::set<uint32_t> seen;
        for (size_t i = 0; i < s.names.size(); ++i) {
          const auto c = column(t, s.names[i]);
          require(c != t.primary, "unsupported", "primary key updates are not supported");
          require(seen.insert(c).second, "duplicate_column", "duplicate assignment");
          updates.emplace_back(c, convert(t.columns[c], s.values[i]));
        }
        const uint64_t old_root = t.root;
        storage_.scan(old_root, [&](int64_t id, uint64_t offset) {
          Row row = decode(t, offset);
          if (!matches(row, where)) return true;
          if (s.kind == Kind::Delete) {
            t.root = storage_.put(t.root, id, 0, boundary);
            if (!t.index_name.empty()) t.vector_root = storage_.put(t.vector_root, id, 0, boundary);
          } else {
            for (const auto &[c, value] : updates) row[c] = value;
            put_row(t, row, boundary);
          }
          ++affected; return true;
        });
      }
    }
    if (s.kind == Kind::Create || s.kind == Kind::Index) result.write("{\"ok\":true}");
    else result.write("{\"ok\":true,\"affected_rows\":" + std::to_string(affected) + "}");
    if (!transaction_) commit();
  } catch (...) {
    catalog_ = savepoint; storage_.rewind(boundary); throw;
  }
  return result;
}

Spool Database::select(const Statement &s, const Table &t) {
  const auto bind = [&](const Expr &e) -> BoundExpr {
    const auto c = column(t, e.column);
    BoundExpr out{c, e.distance, {}, e.alias.empty() ? e.column : e.alias};
    if (e.distance) {
      require(t.columns[c].type == Type::Vector, "type_mismatch", "L2_DISTANCE requires VECTOR");
      out.query = vector_literal(e.query.text, t.columns[c].size);
      if (e.alias.empty()) out.label = "L2_DISTANCE(" + e.column + ", " + e.query.text + ")";
    }
    return out;
  };
  std::vector<BoundExpr> projection;
  if (s.star) {
    for (uint32_t c = 0; c < t.columns.size(); ++c) projection.push_back({c, false, {}, t.columns[c].name});
  } else for (const auto &e : s.projection) projection.push_back(bind(e));
  BoundExpr order{};
  if (s.ordered) {
    bool alias = false;
    if (!s.order.distance) {
      for (const auto &e : projection) {
        if (e.label == s.order.column) { order = e; alias = true; break; }
      }
    }
    if (!alias) order = bind(s.order);
    require(order.distance || t.columns[order.column].type != Type::Vector,
            "unsupported", "direct vector ordering is unsupported");
  }
  std::vector<BoundPredicate> where;
  for (const auto &p : s.where) {
    const auto c = column(t, p.column);
    require(t.columns[c].type != Type::Vector, "unsupported", "direct vector predicate");
    Column type = t.columns[c];
    if (type.type == Type::Varchar) type.size = UINT32_MAX;
    where.push_back({c, p.op, convert(type, p.value)});
  }
  Spool output(directory());
  output.write("{\"ok\":true,\"columns\":[");
  for (size_t i = 0; i < projection.size(); ++i) {
    if (i != 0) output.write(",");
    output.write(json_string(projection[i].label));
  }
  output.write("],\"rows\":[");
  bool first = true;
  const auto emit = [&](const Candidate &value) {
    const Row row = decode(t, value.offset);
    if (!first) output.write(",");
    first = false;
    output.write("[");
    for (size_t i = 0; i < projection.size(); ++i) {
      if (i != 0) output.write(",");
      output.write(value_json(projection[i].eval(row)));
    }
    output.write("]");
  };
  if (s.limit == 0) { output.write("]}"); return output; }
  Sorter sorter(directory(), s.descending);
  const bool heap = s.ordered && s.limit <= 16384 &&
                    (order.distance || t.columns[order.column].type == Type::Bigint);
  const auto better = [&](const Candidate &a, const Candidate &b) { return sorter.better(a, b); };
  std::priority_queue<Candidate, std::vector<Candidate>, decltype(better)> top(better);
  uint64_t remaining = s.limit;
  const auto accept = [&](Candidate value) {
    if (!s.ordered) { emit(value); return --remaining != 0; }
    if (heap) {
      if (top.size() < s.limit) top.push(std::move(value));
      else if (better(value, top.top())) { top.pop(); top.push(std::move(value)); }
    } else sorter.add(std::move(value));
    return true;
  };
  const auto visit = [&](int64_t id, uint64_t offset) {
    Row row = decode(t, offset);
    if (!matches(row, where)) return true;
    Candidate value; value.id = id; value.offset = offset;
    value.key = s.ordered ? order.eval(row) : Value(id);
    return accept(std::move(value));
  };
  bool direct = false;
  for (const auto &p : where) {
    if (p.column == t.primary && p.op == "=") {
      direct = true;
      const auto id = std::get<int64_t>(p.value);
      const auto offset = storage_.find(t.root, id);
      if (offset != 0) visit(id, offset);
      break;
    }
  }
  if (!direct && s.ordered && order.distance && where.empty() && !t.index_name.empty()) {
    // Physical, transactionally maintained vector postings. Only winning rows
    // need ordinary-column decoding; ann deliberately retains exact recall.
    storage_.scan(t.vector_root, [&](int64_t id, uint64_t offset) {
      auto bytes = storage_.blob(offset);
      Candidate value; value.id = id; value.offset = read_u64(bytes);
      double sum = 0;
      for (float q : order.query) {
        const double delta = static_cast<double>(std::bit_cast<float>(read_u32(bytes))) - q;
        sum += delta * delta;
      }
      value.key = std::sqrt(sum); return accept(std::move(value));
    });
  } else if (!direct) storage_.scan(t.root, visit);
  if (s.ordered) {
    if (heap) {
      std::vector<Candidate> values;
      while (!top.empty()) { values.push_back(top.top()); top.pop(); }
      std::sort(values.begin(), values.end(), better);
      for (const auto &value : values) emit(value);
    } else sorter.finish(s.limit, emit);
  }
  output.write("]}"); return output;
}

}  // namespace mvdb
