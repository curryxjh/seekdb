#pragma once

#include <string>
#include <string_view>

namespace mvdb {
struct Request {
  // Keep the original JSON value, including integer precision and type.
  std::string id = "null";
  std::string op;
  std::string sql;
  std::string search_mode = "exact";
  bool has_id = false;
  bool has_sql = false;
};
Request parse_request(std::string_view line);
}  // namespace mvdb
