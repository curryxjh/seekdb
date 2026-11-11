#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace mvdb {

struct Error : std::runtime_error {
  std::string code;
  Error(std::string code_, const std::string &message)
      : std::runtime_error(message), code(std::move(code_)) {}
};
void require(bool condition, std::string_view code, std::string_view message);
std::string lower(std::string text);
size_t unicode_length(std::string_view text);
std::string json_string(std::string_view text);
int64_t bigint(std::string_view text);
std::vector<float> vector_literal(std::string_view text, size_t dimension);

enum class Type : uint8_t { Bigint, Varchar, Vector };
struct Column {
  std::string name;
  Type type = Type::Bigint;
  uint32_t size = 0;
  bool primary = false;
};
struct Literal {
  bool quoted = false;
  std::string text;
};
struct Expr {
  std::string column;
  bool distance = false;
  Literal query;
  std::string alias;
};
struct Predicate {
  std::string column;
  std::string op;
  Literal value;
};
enum class Kind { Create, Index, Insert, Select, Update, Delete, Begin, Commit, Rollback };
struct Statement {
  Kind kind = Kind::Select;
  std::string table;
  std::string index;
  std::vector<Column> columns;
  std::vector<std::string> names;
  std::vector<std::vector<Literal>> rows;
  std::vector<Expr> projection;
  std::vector<Predicate> where;
  std::vector<Literal> values;
  bool star = false;
  bool ordered = false;
  Expr order;
  bool descending = false;
  uint64_t limit = UINT64_MAX;
};
Statement parse(std::string_view sql);

}  // namespace mvdb
