/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

// Local file readers used by SQL table functions. Only schema and the current
// record/page are retained; relational operations belong to the SQL executor.
#include <dirent.h>
#include <fcntl.h>
#include <fnmatch.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace oceanbase {
namespace sql {
namespace file_sql {

constexpr size_t MAX_RECORD = 16 * 1024 * 1024;
constexpr size_t MAX_PAGE = 64 * 1024 * 1024;
constexpr size_t MAX_COLUMNS = 4096;
constexpr size_t MAX_FILES = 10000;
enum class Type { NIL, BIGINT, DOUBLE, BOOLEAN, VARCHAR, DATE, DATETIME };
inline const char* type_name(Type t) {
  static const char* names[] = {"NULL",    "BIGINT", "DOUBLE",  "BOOLEAN",
                                "VARCHAR", "DATE",   "DATETIME"};
  return names[static_cast<int>(t)];
}
inline std::string lower(std::string s) {
  for (char& c : s)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return s;
}
inline bool utf8(const std::string& s) {
  size_t p = 0;
  while (p < s.size()) {
    unsigned char c = s[p++];
    if (c < 0x80) continue;
    int n = c >= 0xc2 && c <= 0xdf   ? 1
            : c >= 0xe0 && c <= 0xef ? 2
            : c >= 0xf0 && c <= 0xf4 ? 3
                                     : -1;
    if (n < 0 || p + n > s.size()) return false;
    unsigned char b = s[p];
    if ((c == 0xe0 && b < 0xa0) || (c == 0xed && b >= 0xa0) || (c == 0xf0 && b < 0x90) ||
        (c == 0xf4 && b >= 0x90))
      return false;
    while (n--)
      if ((static_cast<unsigned char>(s[p++]) & 0xc0) != 0x80) return false;
  }
  return true;
}
inline bool date(const std::string& s) {
  if (s.size() != 10 || s[4] != '-' || s[7] != '-') return false;
  for (size_t i = 0; i < s.size(); ++i)
    if (i != 4 && i != 7 && (s[i] < '0' || s[i] > '9')) return false;
  int y = std::atoi(s.substr(0, 4).c_str()), m = std::atoi(s.substr(5, 2).c_str()),
      d = std::atoi(s.substr(8, 2).c_str());
  static const int days[] = {0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  return y > 0 && m >= 1 && m <= 12 && d >= 1 &&
         d <= days[m] + (m == 2 && y % 4 == 0 && (y % 100 != 0 || y % 400 == 0));
}
inline bool datetime(const std::string& s) {
  if (s.size() < 19 || !date(s.substr(0, 10)) || (s[10] != ' ' && s[10] != 'T') || s[13] != ':' ||
      s[16] != ':')
    return false;
  for (size_t i = 11; i < 19; ++i)
    if (i != 13 && i != 16 && (s[i] < '0' || s[i] > '9')) return false;
  if (s.substr(11, 2) > "23" || s.substr(14, 2) > "59" || s.substr(17, 2) > "59") return false;
  if (s.size() > 19) {
    if (s[19] != '.' || s.size() == 20 || s.size() > 26) return false;
    for (size_t i = 20; i < s.size(); ++i)
      if (s[i] < '0' || s[i] > '9') return false;
  }
  return true;
}
inline Type infer(const std::string& s) {
  const std::string l = lower(s);
  if (l == "true" || l == "false") return Type::BOOLEAN;
  bool integer = !s.empty();
  size_t p = (!s.empty() && (s[0] == '-' || s[0] == '+')) ? 1 : 0;
  integer &= p < s.size();
  for (; p < s.size(); ++p) integer &= s[p] >= '0' && s[p] <= '9';
  if (integer) {
    errno = 0;
    char* e = nullptr;
    std::strtoll(s.c_str(), &e, 10);
    // Integers outside signed BIGINT are preserved as strings, avoiding rounding.
    return errno == 0 ? Type::BIGINT : Type::VARCHAR;
  }
  if (date(s)) return Type::DATE;
  if (datetime(s)) return Type::DATETIME;
  // Accept decimal notation only; strtod also accepts hexadecimal floats and
  // leading whitespace, which should remain strings during CSV inference.
  size_t q = !s.empty() && (s[0] == '+' || s[0] == '-') ? 1 : 0;
  size_t digits = 0;
  while (q < s.size() && s[q] >= '0' && s[q] <= '9') {
    ++q;
    ++digits;
  }
  if (q < s.size() && s[q] == '.') {
    ++q;
    while (q < s.size() && s[q] >= '0' && s[q] <= '9') {
      ++q;
      ++digits;
    }
  }
  bool decimal = digits > 0;
  if (q < s.size() && (s[q] == 'e' || s[q] == 'E')) {
    ++q;
    if (q < s.size() && (s[q] == '+' || s[q] == '-')) ++q;
    size_t start = q;
    while (q < s.size() && s[q] >= '0' && s[q] <= '9') ++q;
    decimal = decimal && q > start;
  }
  if (decimal && q == s.size()) {
    errno = 0;
    char* e = nullptr;
    double d = std::strtod(s.c_str(), &e);
    if (e == s.c_str() + s.size() && errno == 0 && std::isfinite(d)) return Type::DOUBLE;
  }
  return Type::VARCHAR;
}
inline Type widen(Type a, Type b) {
  if (a == Type::NIL) return b;
  if (b == Type::NIL || a == b) return a;
  if ((a == Type::BIGINT && b == Type::DOUBLE) || (a == Type::DOUBLE && b == Type::BIGINT))
    return Type::DOUBLE;
  if ((a == Type::DATE && b == Type::DATETIME) || (a == Type::DATETIME && b == Type::DATE))
    return Type::DATETIME;
  return Type::VARCHAR;
}
struct Cell {
  std::string text;
  bool null = true;
  Type observed = Type::NIL;
  Cell() = default;
  explicit Cell(std::string s) : text(std::move(s)), null(false) {}
};
struct Column {
  std::string name;
  Type type = Type::NIL;
  bool nullable = false;
};
struct Options {
  std::string format = "read_csv", path, root, nullstr;
  bool header = true;
  char delimiter = ',', quote = '"', escape = '"';
  bool set(const std::string& key, const std::string& val, std::string& error) {
    std::string k = lower(key), v = lower(val);
    if (k == "sample_size") {
      if (val != "-1") {
        error = "sample_size only supports -1";
        return false;
      }
    } else if (k == "header") {
      if (v != "true" && v != "false" && v != "1" && v != "0") {
        error = "header must be boolean";
        return false;
      }
      header = v == "true" || v == "1";
    } else if (k == "nullstr" || k == "null")
      nullstr = val;
    else if (k == "delimiter" || k == "delim" || k == "sep" || k == "quote" || k == "escape") {
      if (val.size() != 1 || val[0] == '\n' || val[0] == '\r' || val[0] == 0) {
        error = "CSV options must be one non-newline byte";
        return false;
      }
      if (k == "quote")
        quote = val[0];
      else if (k == "escape")
        escape = val[0];
      else
        delimiter = val[0];
    } else if (k == "format") {
      if ((format == "read_parquet" && v != "parquet") || (format == "glob") ||
          (format == "read_csv" && v != "csv") ||
          ((format == "read_json" || format == "read_ndjson") && v != "newline_delimited" &&
           v != "ndjson")) {
        error = "unsupported file format";
        return false;
      }
    } else {
      error = "unknown file option: " + key;
      return false;
    }
    return true;
  }
  std::string encode() const {
    std::string s;
    for (const auto& v :
         {format, path, root, nullstr, std::string(1, delimiter), std::string(1, quote),
          std::string(1, escape), std::string(header ? "1" : "0")}) {
      s += std::to_string(v.size()) + ":" + v;
    }
    return s;
  }
  bool decode(const std::string& s) {
    std::vector<std::string> v;
    size_t p = 0;
    while (p < s.size() && v.size() < 8) {
      size_t e = s.find(':', p);
      if (e == std::string::npos || e - p > 10) return false;
      size_t n = 0;
      for (size_t i = p; i < e; ++i) {
        if (s[i] < '0' || s[i] > '9') return false;
        n = n * 10 + s[i] - '0';
      }
      p = e + 1;
      if (n > s.size() - p) return false;
      v.push_back(s.substr(p, n));
      p += n;
    }
    if (p != s.size() || v.size() != 8 || v[4].size() != 1 || v[5].size() != 1 || v[6].size() != 1)
      return false;
    format = v[0];
    path = v[1];
    root = v[2];
    nullstr = v[3];
    delimiter = v[4][0];
    quote = v[5][0];
    escape = v[6][0];
    header = v[7] == "1";
    return true;
  }
};
inline bool within(const std::string& root, const std::string& path) {
  return root == "/" || (path.compare(0, root.size(), root) == 0 &&
                         (path.size() == root.size() || path[root.size()] == '/'));
}
inline bool canonical(const std::string& path, std::string& out) {
  if (path.empty() || path.find('\0') != std::string::npos || path.size() >= 4096) return false;
  char buf[4096];
  if (::realpath(path.c_str(), buf) == nullptr) return false;
  out = buf;
  return true;
}
inline timespec modification_time(const struct stat& s) {
#ifdef __APPLE__
  return s.st_mtimespec;
#else
  return s.st_mtim;
#endif
}
inline timespec change_time(const struct stat& s) {
#ifdef __APPLE__
  return s.st_ctimespec;
#else
  return s.st_ctim;
#endif
}
inline bool same_snapshot(const struct stat& a, const struct stat& b) {
  const timespec am = modification_time(a), bm = modification_time(b);
  const timespec ac = change_time(a), bc = change_time(b);
  return a.st_dev == b.st_dev && a.st_ino == b.st_ino && a.st_size == b.st_size &&
         am.tv_sec == bm.tv_sec && am.tv_nsec == bm.tv_nsec && ac.tv_sec == bc.tv_sec &&
         ac.tv_nsec == bc.tv_nsec;
}
inline std::string identity(const struct stat& s) {
  const timespec m = modification_time(s), c = change_time(s);
  return std::to_string(s.st_dev) + ":" + std::to_string(s.st_ino) + ":" +
         std::to_string(s.st_size) + ":" + std::to_string(m.tv_sec) + ":" +
         std::to_string(m.tv_nsec) + ":" + std::to_string(c.tv_sec) + ":" +
         std::to_string(c.tv_nsec);
}
inline int open_directory(const std::string& real) {
  int fd = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  for (size_t p = 1; fd >= 0 && p < real.size();) {
    size_t end = real.find('/', p);
    if (end == std::string::npos) end = real.size();
    int next = ::openat(fd, real.substr(p, end - p).c_str(),
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    ::close(fd);
    fd = next;
    p = end + 1;
  }
  return fd;
}
// Resolve first and then walk the canonical path without following any symlinks.
// In particular, a renamed directory cannot redirect an open outside the root.
inline int safe_open(const std::string& path, const std::string& root, std::string& error) {
  std::string real, allowed;
  if (!canonical(root, allowed) || !canonical(path, real) || !within(allowed, real)) {
    error = "path rejected by secure_file_priv";
    return -1;
  }
  int fd = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  size_t p = 1;
  while (fd >= 0 && p < real.size()) {
    size_t e = real.find('/', p);
    bool last = e == std::string::npos;
    if (last) e = real.size();
    int next = ::openat(fd, real.substr(p, e - p).c_str(),
                        O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC | (last ? 0 : O_DIRECTORY));
    ::close(fd);
    fd = next;
    p = e + 1;
  }
  struct stat st;
  if (fd < 0 || ::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
    if (fd >= 0) ::close(fd);
    error = "expected a readable regular file";
    return -1;
  }
  return fd;
}
class Input {
 public:
  Input() = default;
  ~Input() { close(); }
  Input(const Input&) = delete;
  Input& operator=(const Input&) = delete;
  bool open(const Options& o, std::string& error) {
    close();
    int fd = safe_open(o.path, o.root, error);
    if (fd < 0) return false;
    file_ = ::fdopen(fd, "rb");
    if (!file_) {
      ::close(fd);
      error = "fdopen failed";
      return false;
    }
    if (::fstat(fd, &snapshot_) != 0 || !canonical(o.path, path_)) {
      error = "file identity unavailable during open";
      close();
      return false;
    }
    if (!unchanged(error)) {
      close();
      return false;
    }
    return true;
  }
  void close() {
    if (file_) {
      ::fclose(file_);
      file_ = nullptr;
    }
  }
  bool unchanged(std::string& error) const {
    struct stat a, b;
    if (!file_ || ::fstat(::fileno(file_), &a) != 0 || ::stat(path_.c_str(), &b) != 0 ||
        !same_snapshot(a, snapshot_) || !same_snapshot(b, snapshot_)) {
      error = "file changed during SQL scan";
      return false;
    }
    return true;
  }
  int get() { return ::fgetc(file_); }
  void unget(int c) {
    if (c != EOF) ::ungetc(c, file_);
  }
  bool failed() const { return ::ferror(file_) != 0; }
  bool skip_utf8_bom(std::string& error) {
    if (::ftello(file_) != 0) return true;
    unsigned char bytes[3];
    const size_t size = ::fread(bytes, 1, sizeof(bytes), file_);
    if (::ferror(file_)) {
      error = "file I/O error while checking UTF-8 BOM";
      return false;
    }
    if (size != 3 || bytes[0] != 0xef || bytes[1] != 0xbb || bytes[2] != 0xbf) {
      if (::fseeko(file_, 0, SEEK_SET) != 0) {
        error = "cannot rewind after UTF-8 BOM check";
        return false;
      }
    }
    ::clearerr(file_);
    return true;
  }
  bool rewind(std::string& error) {
    if (!unchanged(error)) return false;
    if (::fseeko(file_, 0, SEEK_SET) != 0) {
      error = "file rewind failed";
      return false;
    }
    ::clearerr(file_);
    return true;
  }
  int64_t size() const { return snapshot_.st_size; }
  std::string identity() const { return file_sql::identity(snapshot_); }
  bool read(int64_t off, size_t n, std::string& out, std::string& error) {
    if (n > MAX_PAGE || off < 0 || off > size() || static_cast<uint64_t>(size() - off) < n ||
        !unchanged(error)) {
      error = "invalid or changed file range";
      return false;
    }
    out.resize(n);
    size_t got = 0;
    while (got < n) {
      ssize_t k = ::pread(::fileno(file_), &out[got], n - got, off + got);
      if (k < 0 && errno == EINTR) continue;
      if (k <= 0) {
        error = "short file read";
        return false;
      }
      got += k;
    }
    return true;
  }

 private:
  FILE* file_ = nullptr;
  struct stat snapshot_{};
  std::string path_;
};

// JSONL parser deliberately accepts a flat object only. It decodes escapes,
// checks surrogate pairs and consumes the entire record.
class FlatJson {
 public:
  explicit FlatJson(const std::string& s) : s_(s) {}
  bool parse(std::vector<std::pair<std::string, Cell>>& out) {
    out.clear();
    space();
    if (!take('{')) return false;
    space();
    if (take('}')) {
      space();
      return p_ == s_.size();
    }
    std::set<std::string> keys;
    do {
      space();
      std::string key;
      if (!string(key) || !keys.insert(lower(key)).second) return false;
      space();
      if (!take(':')) return false;
      space();
      Cell c;
      if (p_ < s_.size() && s_[p_] == '"') {
        c.null = false;
        if (!string(c.text)) return false;
        c.observed = date(c.text) ? Type::DATE : datetime(c.text) ? Type::DATETIME : Type::VARCHAR;
      } else {
        size_t b = p_;
        while (p_ < s_.size() && s_[p_] != ',' && s_[p_] != '}' && s_[p_] != ' ' &&
               s_[p_] != '\t' && s_[p_] != '\r')
          ++p_;
        std::string v = s_.substr(b, p_ - b);
        if (v == "null") {
        } else if (v == "true" || v == "false" || number(v))
          c = Cell(v);
        else
          return false;
      }
      out.emplace_back(key, std::move(c));
      if (out.size() > MAX_COLUMNS) return false;
      space();
      if (take('}')) {
        space();
        return p_ == s_.size();
      }
    } while (take(','));
    return false;
  }

 private:
  void space() {
    while (p_ < s_.size() && (s_[p_] == ' ' || s_[p_] == '\t' || s_[p_] == '\r')) ++p_;
  }
  bool take(char c) {
    if (p_ < s_.size() && s_[p_] == c) {
      ++p_;
      return true;
    }
    return false;
  }
  bool number(const std::string& s) {
    size_t p = 0;
    if (p < s.size() && s[p] == '-') ++p;
    if (p == s.size()) return false;
    if (s[p] == '0')
      ++p;
    else {
      if (s[p] < '1' || s[p] > '9') return false;
      while (p < s.size() && s[p] >= '0' && s[p] <= '9') ++p;
    }
    if (p < s.size() && s[p] == '.') {
      size_t b = ++p;
      while (p < s.size() && s[p] >= '0' && s[p] <= '9') ++p;
      if (b == p) return false;
    }
    if (p < s.size() && (s[p] == 'e' || s[p] == 'E')) {
      ++p;
      if (p < s.size() && (s[p] == '+' || s[p] == '-')) ++p;
      size_t b = p;
      while (p < s.size() && s[p] >= '0' && s[p] <= '9') ++p;
      if (b == p) return false;
    }
    return p == s.size();
  }
  bool hex(uint32_t& v) {
    if (p_ + 4 > s_.size()) return false;
    v = 0;
    for (int i = 0; i < 4; ++i) {
      char c = s_[p_++];
      int d = c >= '0' && c <= '9'   ? c - '0'
              : c >= 'a' && c <= 'f' ? c - 'a' + 10
              : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                     : -1;
      if (d < 0) return false;
      v = v * 16 + d;
    }
    return true;
  }
  void codepoint(std::string& o, uint32_t cp) {
    if (cp < 0x80)
      o.push_back(cp);
    else if (cp < 0x800) {
      o.push_back(0xc0 | (cp >> 6));
      o.push_back(0x80 | (cp & 63));
    } else if (cp < 0x10000) {
      o.push_back(0xe0 | (cp >> 12));
      o.push_back(0x80 | ((cp >> 6) & 63));
      o.push_back(0x80 | (cp & 63));
    } else {
      o.push_back(0xf0 | (cp >> 18));
      o.push_back(0x80 | ((cp >> 12) & 63));
      o.push_back(0x80 | ((cp >> 6) & 63));
      o.push_back(0x80 | (cp & 63));
    }
  }
  bool string(std::string& o) {
    if (!take('"')) return false;
    while (p_ < s_.size()) {
      unsigned char c = s_[p_++];
      if (c == '"') return utf8(o);
      if (c < 32) return false;
      if (c != '\\') {
        o.push_back(c);
        continue;
      }
      if (p_ == s_.size()) return false;
      char e = s_[p_++];
      switch (e) {
        case '"':
        case '\\':
        case '/':
          o.push_back(e);
          break;
        case 'b':
          o.push_back('\b');
          break;
        case 'f':
          o.push_back('\f');
          break;
        case 'n':
          o.push_back('\n');
          break;
        case 'r':
          o.push_back('\r');
          break;
        case 't':
          o.push_back('\t');
          break;
        case 'u': {
          uint32_t cp;
          if (!hex(cp)) return false;
          if (cp >= 0xd800 && cp <= 0xdbff) {
            uint32_t lo;
            if (!take('\\') || !take('u') || !hex(lo) || lo < 0xdc00 || lo > 0xdfff) return false;
            cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00);
          } else if (cp >= 0xdc00 && cp <= 0xdfff)
            return false;
          codepoint(o, cp);
          break;
        }
        default:
          return false;
      }
    }
    return false;
  }
  const std::string& s_;
  size_t p_ = 0;
};

// Compact Thrift is the Parquet wire metadata format. Decode it with strict
// bounds rather than depending on an external SQL engine or a runtime helper.
struct Meta {
  int64_t number = 0;
  std::string bytes;
  std::map<int, Meta> fields;
  std::vector<Meta> items;
  const Meta& at(int n) const {
    static const Meta empty;
    auto i = fields.find(n);
    return i == fields.end() ? empty : i->second;
  }
  int64_t num(int n, int64_t def = 0) const {
    auto i = fields.find(n);
    return i == fields.end() ? def : i->second.number;
  }
};
class Compact {
 public:
  Compact(const std::string& s, size_t start = 0) : s_(s), p_(start) {}
  size_t position() const { return p_; }
  bool object(Meta& m, int depth = 0) {
    if (depth > 16) return false;
    int last = 0;
    while (p_ < s_.size()) {
      uint8_t h = byte();
      if (!h) return good_;
      int id = h >> 4;
      if (id)
        id += last;
      else {
        uint64_t v;
        if (!var(v)) return false;
        const int64_t field_id = zig(v);
        if (field_id <= 0 || field_id > INT16_MAX) return false;
        id = static_cast<int>(field_id);
      }
      last = id;
      if (id <= 0 || id > INT16_MAX || m.fields.size() > MAX_COLUMNS * 8) return false;
      Meta v;
      if (!value(h & 15, v, depth + 1)) return false;
      if (!m.fields.emplace(id, std::move(v)).second) return false;
    }
    return false;
  }

 private:
  uint8_t byte() {
    if (p_ >= s_.size()) {
      good_ = false;
      return 0;
    }
    return static_cast<uint8_t>(s_[p_++]);
  }
  bool var(uint64_t& v) {
    v = 0;
    for (unsigned i = 0; i < 10; ++i) {
      uint8_t c = byte();
      if (!good_ || (i == 9 && c > 1)) return false;
      v |= uint64_t(c & 127) << (7 * i);
      if (!(c & 128)) return true;
    }
    return false;
  }
  static int64_t zig(uint64_t v) {
    return static_cast<int64_t>((v >> 1) ^ uint64_t(-static_cast<int64_t>(v & 1)));
  }
  bool value(int t, Meta& m, int d) {
    if (d > 16 || ++nodes_ > 100000) return false;
    uint64_t v = 0;
    switch (t) {
      case 1:
        m.number = 1;
        return true;
      case 2:
        m.number = 0;
        return true;
      case 3:
        m.number = static_cast<int8_t>(byte());
        return good_;
      case 4:
      case 5:
      case 6:
        if (!var(v)) return false;
        m.number = zig(v);
        return true;
      case 7:
        if (p_ + 8 > s_.size()) return false;
        m.bytes = s_.substr(p_, 8);
        p_ += 8;
        return true;
      case 8:
        if (!var(v) || v > MAX_RECORD || v > s_.size() - p_) return false;
        m.bytes = s_.substr(p_, v);
        p_ += v;
        return true;
      case 9:
      case 10: {
        uint8_t h = byte();
        size_t n = h >> 4;
        if (n == 15) {
          if (!var(v)) return false;
          n = v;
        }
        if (n > 100000) return false;
        for (size_t i = 0; i < n; ++i) {
          Meta e;
          if (!value(h & 15, e, d + 1)) return false;
          m.items.push_back(std::move(e));
        }
        return good_;
      }
      case 11:
        if (!var(v) || v > 100000) return false;
        if (v) {
          uint8_t h = byte();
          for (size_t i = 0; i < v; ++i) {
            Meta k, e;
            if (!value(h >> 4, k, d + 1) || !value(h & 15, e, d + 1)) return false;
          }
        }
        return good_;
      case 12:
        return object(m, d + 1);
      default:
        return false;
    }
  }
  const std::string& s_;
  size_t p_;
  bool good_ = true;
  size_t nodes_ = 0;
};
inline uint32_t le32(const char* p) {
  return uint32_t(uint8_t(p[0])) | (uint32_t(uint8_t(p[1])) << 8) |
         (uint32_t(uint8_t(p[2])) << 16) | (uint32_t(uint8_t(p[3])) << 24);
}
inline uint64_t le64(const char* p) { return le32(p) | (uint64_t(le32(p + 4)) << 32); }
inline bool varint(const std::string& s, size_t& p, uint64_t& v) {
  v = 0;
  for (int i = 0; i < 10; ++i) {
    if (p >= s.size()) return false;
    uint8_t b = s[p++];
    if (i == 9 && b > 1) return false;
    v |= uint64_t(b & 127) << (i * 7);
    if (!(b & 128)) return true;
  }
  return false;
}
inline bool snappy(const std::string& in, std::string& out, size_t expected) {
  size_t p = 0;
  uint64_t size;
  if (!varint(in, p, size) || size != expected || size > MAX_PAGE) return false;
  out.clear();
  out.reserve(size);
  while (p < in.size() && out.size() < size) {
    uint8_t tag = in[p++];
    size_t n = 0, off = 0;
    if ((tag & 3) == 0) {
      n = tag >> 2;
      if (n < 60)
        ++n;
      else {
        size_t bytes = n - 59;
        if (bytes > 4 || p + bytes > in.size()) return false;
        n = 0;
        for (size_t i = 0; i < bytes; ++i) n |= size_t(uint8_t(in[p++])) << (8 * i);
        ++n;
      }
      if (n > size - out.size() || n > in.size() - p) return false;
      out.append(in, p, n);
      p += n;
    } else {
      switch (tag & 3) {
        case 1:
          if (p >= in.size()) return false;
          n = 4 + ((tag >> 2) & 7);
          off = ((tag & 0xe0) << 3) | uint8_t(in[p++]);
          break;
        case 2:
          if (p + 2 > in.size()) return false;
          n = 1 + (tag >> 2);
          off = uint8_t(in[p]) | (size_t(uint8_t(in[p + 1])) << 8);
          p += 2;
          break;
        default:
          if (p + 4 > in.size()) return false;
          n = 1 + (tag >> 2);
          off = le32(in.data() + p);
          p += 4;
          break;
      }
      if (off == 0 || off > out.size() || n > size - out.size()) return false;
      for (size_t i = 0; i < n; ++i) out.push_back(out[out.size() - off]);
    }
  }
  return p == in.size() && out.size() == size;
}
inline bool unpack(const std::string& s, size_t& p, int bits, size_t n,
                   std::vector<uint64_t>& out) {
  if (p > s.size() || bits < 0 || bits > 64 || n > MAX_PAGE / 8 ||
      n > (std::numeric_limits<size_t>::max() - 7) / 64)
    return false;
  size_t bytes = (n * bits + 7) / 8;
  if (bytes > s.size() - p) return false;
  for (size_t i = 0; i < n; ++i) {
    uint64_t v = 0;
    for (int b = 0; b < bits; ++b) {
      size_t off = i * bits + b;
      v |= uint64_t((uint8_t(s[p + off / 8]) >> (off % 8)) & 1) << b;
    }
    out.push_back(v);
  }
  p += bytes;
  return true;
}
inline bool hybrid(const std::string& s, size_t& p, size_t end, int bits, size_t count,
                   std::vector<uint64_t>& out) {
  if (end > s.size() || p > end || bits < 0 || bits > 64 || count > MAX_PAGE / 8) return false;
  out.clear();
  while (out.size() < count) {
    uint64_t h;
    if (!varint(s, p, h) || h == 0 || p > end) return false;
    size_t n = h >> 1;
    if (h & 1) {
      if (n > MAX_PAGE / 64) return false;
      n *= 8;
      std::vector<uint64_t> tmp;
      if (!unpack(s, p, bits, n, tmp) || p > end) return false;
      out.insert(out.end(), tmp.begin(), tmp.begin() + std::min(n, count - out.size()));
    } else {
      size_t bytes = (bits + 7) / 8;
      if (bytes > end - p || n > count - out.size()) return false;
      uint64_t v = 0;
      for (size_t i = 0; i < bytes; ++i) v |= uint64_t(uint8_t(s[p++])) << (8 * i);
      out.insert(out.end(), n, v);
    }
  }
  return true;
}
inline std::string real_string(double d) {
  char b[64];
  std::snprintf(b, sizeof(b), "%.17g", d);
  return b;
}
inline bool delta_integers(const std::string& s, size_t& p, std::vector<int64_t>& out) {
  uint64_t block, mini, total, first;
  if (!varint(s, p, block) || !varint(s, p, mini) || !varint(s, p, total) || !varint(s, p, first) ||
      block == 0 || block > 65536 || block % 128 || mini == 0 || mini > block / 32 ||
      block % mini || total > MAX_PAGE / 8)
    return false;
  out.clear();
  if (total == 0) return false;
  uint64_t previous = (first >> 1) ^ uint64_t(-static_cast<int64_t>(first & 1));
  out.push_back(static_cast<int64_t>(previous));
  while (out.size() < total) {
    uint64_t encoded;
    if (!varint(s, p, encoded) || mini > s.size() - p) return false;
    uint64_t minimum = (encoded >> 1) ^ uint64_t(-static_cast<int64_t>(encoded & 1));
    std::string widths = s.substr(p, mini);
    p += mini;
    for (size_t m = 0; m < mini; ++m) {
      std::vector<uint64_t> packed;
      if (!unpack(s, p, uint8_t(widths[m]), block / mini, packed)) return false;
      for (uint64_t v : packed)
        if (out.size() < total) {
          previous += minimum + v;
          out.push_back(static_cast<int64_t>(previous));
        }
    }
  }
  return true;
}

class Parquet {
  struct PageColumn {
    size_t group = 0, index = 0;
    int64_t offset = 0, limit = 0, remaining = 0;
    std::vector<Cell> cells, dictionary;
  };

 public:
  bool init(Input& in, std::vector<Column>& cols, std::string& error) {
    std::string head, tail, footer;
    if (in.size() < 12 || !in.read(0, 4, head, error) || head != "PAR1" ||
        !in.read(in.size() - 8, 8, tail, error) || tail.substr(4) != "PAR1") {
      error = "invalid Parquet magic";
      return false;
    }
    size_t n = le32(tail.data());
    if (n > MAX_RECORD || n > static_cast<uint64_t>(in.size() - 12) ||
        !in.read(in.size() - 8 - n, n, footer, error)) {
      error = "invalid Parquet footer length";
      return false;
    }
    footer_start_ = in.size() - 8 - n;
    Compact c(footer);
    meta_ = Meta();
    if (!c.object(meta_) || c.position() != footer.size() || !meta_.fields.count(1) ||
        !meta_.fields.count(2) || !meta_.fields.count(3) || !meta_.fields.count(4)) {
      error = "invalid Parquet compact footer";
      return false;
    }
    const auto& schema = meta_.at(2).items;
    if (schema.size() < 2 || schema.size() > MAX_COLUMNS + 1 ||
        schema[0].num(5) != static_cast<int64_t>(schema.size() - 1)) {
      error = "only flat Parquet schema is supported";
      return false;
    }
    cols.clear();
    physical_.clear();
    converted_.clear();
    unsigned_.clear();
    timestamp_units_.clear();
    optional_.clear();
    for (size_t i = 1; i < schema.size(); ++i) {
      const auto& s = schema[i];
      int64_t p = s.num(1, -1), cv = s.num(6, -1), rep = s.num(3, -1);
      if (rep < 0 || rep > 1 || s.num(5) != 0 || p < 0 || p > 7 || p == 3 || cv < -1 || cv > 21) {
        error = "unsupported nested/repeated Parquet column";
        return false;
      }
      Column col;
      col.name = s.at(4).bytes;
      col.nullable = rep == 1;
      col.type = p == 0             ? Type::BOOLEAN
                 : p == 1 || p == 2 ? Type::BIGINT
                 : p == 4 || p == 5 ? Type::DOUBLE
                                    : Type::VARCHAR;
      if (cv == 6 || s.at(10).fields.count(6)) col.type = Type::DATE;
      if (cv == 9 || cv == 10 || s.at(10).fields.count(8)) col.type = Type::DATETIME;
      const Meta& logical = s.at(10);
      bool is_unsigned = cv >= 11 && cv <= 14;
      if (logical.fields.size() > 1) {
        error = "invalid Parquet logical type union";
        return false;
      }
      if (logical.fields.count(10)) {
        const int64_t bits = logical.at(10).num(1);
        if ((bits != 8 && bits != 16 && bits != 32 && bits != 64) ||
            (bits == 64 ? p != 2 : p != 1)) {
          error = "invalid Parquet integer annotation";
          return false;
        }
        if (!logical.at(10).fields.count(2) || logical.at(10).num(2) < 0 ||
            logical.at(10).num(2) > 1) {
          error = "invalid Parquet integer signedness";
          return false;
        }
        is_unsigned = logical.at(10).num(2) == 0;
      }
      if (is_unsigned && p != 1 && p != 2) {
        error = "invalid unsigned Parquet type";
        return false;
      }
      if (is_unsigned && p == 2) col.type = Type::VARCHAR;
      if (logical.fields.count(11)) col.type = Type::NIL;
      for (const auto& annotation : logical.fields) {
        if (annotation.first != 1 && annotation.first != 4 && annotation.first != 6 &&
            annotation.first != 8 && annotation.first != 10 && annotation.first != 11 &&
            annotation.first != 12) {
          error = "unsupported Parquet logical type";
          return false;
        }
      }
      if (cv == 7 || cv == 8) {
        error = "Parquet TIME is not a supported SQL file type";
        return false;
      }
      int unit = cv == 9 ? 1 : 2;
      if (logical.fields.count(8)) {
        const Meta& time_unit = logical.at(8).at(2);
        unit = time_unit.fields.count(1)   ? 1
               : time_unit.fields.count(2) ? 2
               : time_unit.fields.count(3) ? 3
                                           : 0;
      }
      if ((col.type == Type::DATE && p != 1) ||
          (col.type == Type::DATETIME && (p != 2 || unit == 0)) || cv == 5 ||
          logical.fields.count(5) || p == 7) {
        error = "unsupported Parquet logical or fixed-length type";
        return false;
      }
      if (col.name.empty() || !utf8(col.name)) {
        error = "invalid Parquet column name";
        return false;
      }
      for (const auto& existing : cols)
        if (lower(existing.name) == lower(col.name)) {
          error = "duplicate Parquet column name";
          return false;
        }
      cols.push_back(col);
      physical_.push_back(p);
      converted_.push_back(cv);
      unsigned_.push_back(is_unsigned);
      timestamp_units_.push_back(unit);
      optional_.push_back(rep == 1);
    }
    groups_ = &meta_.at(4).items;
    int64_t rows = 0;
    for (const auto& g : *groups_) {
      if (g.num(3) < 0 || g.at(1).items.size() != cols.size() || g.num(3) > INT64_MAX - rows) {
        error = "invalid Parquet row group";
        return false;
      }
      rows += g.num(3);
    }
    if (rows != meta_.num(3) || rows < 0) {
      error = "Parquet row count mismatch";
      return false;
    }
    columns_ = cols;
    total_ = rows;
    row_ = 0;
    states_.clear();
    states_.resize(cols.size());
    return true;
  }
  bool reset() {
    row_ = 0;
    states_.clear();
    states_.resize(columns_.size());
    return true;
  }
  int64_t timestamp_multiplier(size_t col) const { return timestamp_units_[col] == 1 ? 1000 : 1; }
  int64_t timestamp_divisor(size_t col) const { return timestamp_units_[col] == 3 ? 1000 : 1; }
  int next(Input& in, const std::vector<size_t>& projection, std::vector<Cell>& row,
           std::string& error) {
    if (row_ >= total_) return 0;
    row.clear();
    for (size_t col : projection) {
      if (col >= states_.size()) {
        error = "invalid Parquet projection";
        return -1;
      }
      Cell v;
      if (!value(in, col, v, error)) return -1;
      row.push_back(std::move(v));
    }
    ++row_;
    return 1;
  }

 private:
  bool plain(const std::string& s, size_t& p, int type, size_t count, std::vector<Cell>& out,
             std::string& error, bool is_unsigned = false) {
    if (p > s.size() || count > MAX_PAGE / sizeof(Cell)) return false;
    out.clear();
    size_t bytes = type == 1 || type == 4 ? 4 : type == 2 || type == 5 ? 8 : 0;
    size_t decoded = count * sizeof(Cell);
    if (bytes && count > (s.size() - p) / bytes) return false;
    for (size_t i = 0; i < count; ++i) {
      std::string v;
      if (type == 0) {
        size_t at = p + i / 8;
        if (at >= s.size()) return false;
        v = (uint8_t(s[at]) >> (i % 8)) & 1 ? "true" : "false";
      } else if (type == 1) {
        v = is_unsigned ? std::to_string(le32(s.data() + p))
                        : std::to_string(static_cast<int32_t>(le32(s.data() + p)));
        p += 4;
      } else if (type == 2) {
        v = is_unsigned ? std::to_string(le64(s.data() + p))
                        : std::to_string(static_cast<int64_t>(le64(s.data() + p)));
        p += 8;
      } else if (type == 4) {
        uint32_t b = le32(s.data() + p);
        float f;
        std::memcpy(&f, &b, 4);
        if (!std::isfinite(f)) {
          error = "non-finite Parquet float";
          return false;
        }
        v = real_string(f);
        p += 4;
      } else if (type == 5) {
        uint64_t b = le64(s.data() + p);
        double d;
        std::memcpy(&d, &b, 8);
        if (!std::isfinite(d)) {
          error = "non-finite Parquet double";
          return false;
        }
        v = real_string(d);
        p += 8;
      } else if (type == 6) {
        if (s.size() - p < 4) return false;
        size_t n = le32(s.data() + p);
        p += 4;
        if (n > s.size() - p || n > MAX_RECORD) return false;
        v = s.substr(p, n);
        p += n;
        if (!utf8(v)) {
          error = "invalid UTF-8 Parquet string";
          return false;
        }
      } else {
        error = "unsupported Parquet physical type";
        return false;
      }
      if (v.size() > MAX_PAGE - decoded) {
        error = "Parquet decoded page exceeds resource limit";
        return false;
      }
      decoded += v.size();
      out.emplace_back(std::move(v));
    }
    if (type == 0) p += (count + 7) / 8;
    return true;
  }
  bool value(Input& in, size_t col, Cell& out, std::string& error) {
    PageColumn& st = states_[col];
    while (st.index >= st.cells.size()) {
      if (st.remaining == 0) {
        while (st.group < groups_->size() && groups_->at(st.group).num(3) == 0) ++st.group;
        if (st.group >= groups_->size()) {
          error = "short Parquet column";
          return false;
        }
        const Meta& cm = groups_->at(st.group).at(1).items[col].at(3);
        st.remaining = cm.num(5);
        st.offset = cm.num(11, cm.num(9));
        st.dictionary.clear();
        const int64_t length = cm.num(7, -1);
        if (st.offset < 4 || st.offset >= footer_start_ || length < 0 ||
            length > footer_start_ - st.offset || cm.at(3).items.size() != 1 ||
            cm.at(3).items[0].bytes != columns_[col].name) {
          error = "invalid Parquet column chunk bounds or path";
          return false;
        }
        st.limit = st.offset + length;
        if (st.remaining != groups_->at(st.group).num(3) || cm.num(1) != physical_[col] ||
            !groups_->at(st.group).at(1).items[col].at(1).bytes.empty()) {
          error = "invalid or external Parquet column";
          return false;
        }
      }
      const Meta& cm = groups_->at(st.group).at(1).items[col].at(3);
      std::string h;
      if (st.offset < 4 || st.offset >= st.limit) {
        error = "truncated Parquet column chunk";
        return false;
      }
      size_t cap = static_cast<size_t>(std::min<int64_t>(65536, st.limit - st.offset));
      if (!in.read(st.offset, cap, h, error)) return false;
      Meta ph;
      Compact cp(h);
      if (!cp.object(ph)) {
        error = "invalid Parquet page header";
        return false;
      }
      int64_t kind = ph.num(1, -1);
      int64_t compressed = ph.num(3, -1), uncompressed = ph.num(2, -1);
      if (compressed < 0 || uncompressed < 0 || compressed > MAX_PAGE || uncompressed > MAX_PAGE) {
        error = "Parquet page exceeds resource limit";
        return false;
      }
      std::string body, data;
      st.offset += cp.position();
      if (compressed > st.limit - st.offset) {
        error = "Parquet page exceeds column chunk";
        return false;
      }
      if (!in.read(st.offset, compressed, body, error)) return false;
      st.offset += compressed;
      if (kind != 0 && kind != 2 && kind != 3) {
        error = "unsupported Parquet page type";
        return false;
      }
      int64_t codec = cm.num(4, -1);
      size_t prefix = 0;
      if (kind == 3) {
        int64_t r = ph.at(8).num(6), d = ph.at(8).num(5);
        if (r < 0 || d < 0 || r > static_cast<int64_t>(MAX_PAGE) ||
            d > static_cast<int64_t>(MAX_PAGE) - r) {
          error = "bad Parquet v2 levels";
          return false;
        }
        prefix = r + d;
        if (prefix > body.size() || prefix > static_cast<size_t>(uncompressed)) return false;
      }
      bool compressed_page = kind != 3 || ph.at(8).num(7, 1);
      if (codec == 0 || !compressed_page) {
        data = body;
        if (data.size() != static_cast<size_t>(uncompressed)) {
          error = "Parquet page length mismatch";
          return false;
        }
      } else if (codec == 1) {
        std::string inflated;
        if (!snappy(body.substr(prefix), inflated, uncompressed - prefix)) {
          error = "invalid Snappy page";
          return false;
        }
        data = body.substr(0, prefix) + inflated;
      } else {
        error = "unsupported Parquet compression codec";
        return false;
      }
      size_t pos = 0;
      int64_t encoding = kind == 2   ? ph.at(7).num(2)
                         : kind == 3 ? ph.at(8).num(4)
                                     : ph.at(5).num(2);
      int64_t n = kind == 2 ? ph.at(7).num(1) : kind == 3 ? ph.at(8).num(1) : ph.at(5).num(1);
      if (n < 0 || n > MAX_PAGE / sizeof(Cell) || (kind != 2 && n > st.remaining)) {
        error = "invalid Parquet page value count";
        return false;
      }
      if (kind == 3) {
        const Meta& v2 = ph.at(8);
        if (v2.num(3, -1) != n || v2.num(2, -1) < 0 || v2.num(2) > n || v2.num(6, -1) != 0 ||
            v2.num(5, -1) < 0 || (!optional_[col] && v2.num(5) != 0)) {
          error = "invalid flat Parquet v2 row counts or levels";
          return false;
        }
      } else if (kind == 0 && optional_[col] && ph.at(5).num(3, -1) != 3) {
        error = "unsupported Parquet definition level encoding";
        return false;
      }
      if (kind == 2) {
        if (encoding != 0 ||
            !plain(data, pos, physical_[col], n, st.dictionary, error, unsigned_[col]) ||
            !check_memory(error)) {
          error = "invalid or oversized Parquet dictionary";
          return false;
        }
        continue;
      }
      std::vector<uint64_t> levels;
      size_t nonnull = n;
      if (optional_[col]) {
        size_t end = 0;
        if (kind == 3) {
          if (ph.at(8).num(6) != 0) return false;
          end = ph.at(8).num(5);
        } else {
          if (data.size() < 4) return false;
          end = 4 + le32(data.data());
          pos = 4;
        }
        if (!hybrid(data, pos, end, 1, n, levels)) {
          error = "invalid Parquet definition levels";
          return false;
        }
        pos = end;
        nonnull = 0;
        for (uint64_t l : levels) {
          if (l > 1) return false;
          nonnull += l;
        }
      }
      if (kind == 3 && static_cast<size_t>(ph.at(8).num(2)) != static_cast<size_t>(n) - nonnull) {
        error = "Parquet v2 null count mismatch";
        return false;
      }
      std::vector<Cell> values;
      if (columns_[col].type == Type::NIL && nonnull != 0) {
        error = "non-null value in Parquet NULL column";
        return false;
      }
      if (nonnull == 0) { /* All-null pages have no encoded value payload. */
      } else if (encoding == 0) {
        if (!plain(data, pos, physical_[col], nonnull, values, error, unsigned_[col])) {
          error = "invalid Parquet plain values";
          return false;
        }
      } else if (encoding == 2 || encoding == 8) {
        if (pos >= data.size()) return false;
        int bits = uint8_t(data[pos++]);
        std::vector<uint64_t> ids;
        if (!hybrid(data, pos, data.size(), bits, nonnull, ids)) {
          error = "invalid Parquet dictionary indices";
          return false;
        }
        size_t decoded = nonnull * sizeof(Cell);
        for (uint64_t id : ids) {
          if (id >= st.dictionary.size()) {
            error = "Parquet dictionary index out of range";
            return false;
          }
          if (st.dictionary[id].text.size() > MAX_PAGE - decoded) {
            error = "Parquet dictionary expansion exceeds resource limit";
            return false;
          }
          decoded += st.dictionary[id].text.size();
          values.push_back(st.dictionary[id]);
        }
      } else if (encoding == 5 && (physical_[col] == 1 || physical_[col] == 2)) {
        std::vector<int64_t> numbers;
        if (!delta_integers(data, pos, numbers) || numbers.size() != nonnull) {
          error = "invalid Parquet delta integers";
          return false;
        }
        for (int64_t v : numbers) {
          if (unsigned_[col])
            values.emplace_back(std::to_string(physical_[col] == 1
                                                   ? static_cast<uint64_t>(static_cast<uint32_t>(v))
                                                   : static_cast<uint64_t>(v)));
          else
            values.emplace_back(std::to_string(
                physical_[col] == 1 ? static_cast<int64_t>(static_cast<int32_t>(v)) : v));
        }
      } else if ((encoding == 6 || encoding == 7) && physical_[col] == 6) {
        std::vector<int64_t> prefixes, lengths;
        if (encoding == 7 && (!delta_integers(data, pos, prefixes) || prefixes.size() != nonnull))
          return false;
        if (!delta_integers(data, pos, lengths) || lengths.size() != nonnull) return false;
        std::string previous;
        size_t decoded = nonnull * sizeof(Cell);
        for (size_t i = 0; i < nonnull; ++i) {
          int64_t prefix = encoding == 7 ? prefixes[i] : 0, length = lengths[i];
          if (prefix < 0 || static_cast<uint64_t>(prefix) > previous.size() || length < 0 ||
              static_cast<uint64_t>(length) > data.size() - pos ||
              static_cast<uint64_t>(prefix + length) > MAX_RECORD)
            return false;
          if (static_cast<size_t>(prefix + length) > MAX_PAGE - decoded) {
            error = "Parquet delta expansion exceeds resource limit";
            return false;
          }
          decoded += prefix + length;
          previous = previous.substr(0, prefix) + data.substr(pos, length);
          pos += length;
          if (!utf8(previous)) return false;
          values.emplace_back(previous);
        }
      } else if (encoding == 9 && (physical_[col] == 4 || physical_[col] == 5)) {
        size_t width = physical_[col] == 4 ? 4 : 8;
        if (nonnull > (data.size() - pos) / width) return false;
        std::string interleaved(nonnull * width, 0);
        for (size_t i = 0; i < nonnull; ++i)
          for (size_t j = 0; j < width; ++j)
            interleaved[i * width + j] = data[pos + j * nonnull + i];
        size_t start = 0;
        if (!plain(interleaved, start, physical_[col], nonnull, values, error)) return false;
        pos += nonnull * width;
      } else if (encoding == 3 && physical_[col] == 0) {
        if (data.size() - pos < 4) return false;
        size_t length = le32(data.data() + pos);
        pos += 4;
        if (length > data.size() - pos) return false;
        std::vector<uint64_t> booleans;
        if (!hybrid(data, pos, pos + length, 1, nonnull, booleans)) return false;
        for (uint64_t v : booleans) {
          if (v > 1) return false;
          values.emplace_back(v ? "true" : "false");
        }
      } else {
        error = "unsupported Parquet encoding " + std::to_string(encoding) + " in column " +
                columns_[col].name;
        return false;
      }
      st.cells.clear();
      size_t vi = 0;
      for (int64_t i = 0; i < n; ++i)
        st.cells.push_back(!optional_[col] || levels[i] ? values[vi++] : Cell());
      st.index = 0;
      st.remaining -= n;
      if (!check_memory(error)) return false;
      if (st.remaining == 0) ++st.group;
      if (n == 0) {
        error = "empty Parquet data page";
        return false;
      }
    }
    out = st.cells[st.index++];
    return true;
  }
  bool check_memory(std::string& error) const {
    size_t bytes = 0;
    for (const auto& state : states_)
      for (const auto* list : {&state.cells, &state.dictionary}) {
        size_t overhead = list->capacity() * sizeof(Cell);
        if (overhead > 2 * MAX_PAGE - bytes) {
          error = "Parquet scan exceeds decoded memory limit";
          return false;
        }
        bytes += overhead;
        for (const Cell& cell : *list) {
          size_t cost = cell.text.capacity();
          if (cost > 2 * MAX_PAGE - bytes) {
            error = "Parquet scan exceeds decoded memory limit";
            return false;
          }
          bytes += cost;
        }
      }
    return true;
  }
  Meta meta_;
  const std::vector<Meta>* groups_ = nullptr;
  std::vector<int> physical_, converted_, timestamp_units_;
  std::vector<bool> optional_, unsigned_;
  std::vector<Column> columns_;
  std::vector<PageColumn> states_;
  int64_t total_ = 0, row_ = 0, footer_start_ = 0;
};

class Scanner {
 public:
  std::string error;
  std::vector<Column> columns;
  Options options;
  void set_status_check(int (*check)(void*), void* context) {
    status_check_ = check;
    status_context_ = context;
  }
  int status_code() const { return status_code_; }
  bool open(const Options& o, bool discover = true) {
    options = o;
    error.clear();
    status_code_ = 0;
    columns.clear();
    line_ = 0;
    records_ = 0;
    files_.clear();
    file_ids_.clear();
    glob_directory_.clear();
    glob_signature_.clear();
    file_index_ = 0;
    batch_.clear();
    batch_index_ = 0;
    if (!check_status()) return false;
    if (o.delimiter == o.quote) {
      error = "delimiter and quote must differ";
      return false;
    }
    if (o.format == "glob") return discover_glob();
    if (!input_.open(o, error)) return false;
    if (o.format == "read_parquet") return parquet_.init(input_, columns, error);
    if (o.format != "read_csv" && o.format != "read_json" && o.format != "read_ndjson") {
      error = "unknown file table function";
      return false;
    }
    if (!discover) return true;
    std::vector<Cell> cells;
    std::vector<std::pair<std::string, Cell>> obj;
    if (o.format == "read_csv") {
      int r = csv(cells);
      if (r < 0) return false;
      if (r > 0) {
        for (size_t i = 0; i < cells.size(); ++i) {
          Column c;
          c.name = o.header ? cells[i].text : "column" + std::to_string(i);
          if (c.name.empty()) c.name = "column" + std::to_string(i);
          columns.push_back(c);
        }
        unique_names();
        if (!o.header && !infer_row(cells)) return false;
      }
      while ((r = csv(cells)) > 0)
        if (!infer_row(cells)) return false;
      if (r < 0) return false;
    } else {
      int r;
      while ((r = json(obj)) > 0) {
        for (const auto& kv : obj) {
          size_t i = find(kv.first);
          if (i == columns.size()) {
            Column c;
            c.name = kv.first;
            c.nullable = records_ > 0;
            columns.push_back(c);
            if (columns.size() > MAX_COLUMNS) {
              error = "too many JSON fields";
              return false;
            }
          }
        }
        cells.assign(columns.size(), Cell());
        for (const auto& kv : obj) cells[find(kv.first)] = kv.second;
        if (!infer_row(cells)) return false;
      }
      if (r < 0) return false;
    }
    for (auto& column : columns)
      if (column.type == Type::NIL) column.nullable = true;
    return input_.unchanged(error) && rewind();
  }
  bool rewind() {
    line_ = 0;
    records_ = 0;
    file_index_ = 0;
    batch_.clear();
    batch_index_ = 0;
    if (!check_status()) return false;
    if (options.format == "glob") return glob_unchanged(true);
    if (!input_.rewind(error)) return false;
    if (options.format == "read_parquet") return parquet_.reset();
    if (options.format == "read_csv" && options.header) {
      std::vector<Cell> header;
      return csv(header) >= 0;
    }
    return true;
  }
  bool unchanged() {
    return options.format == "glob" ? glob_unchanged(false) : input_.unchanged(error);
  }
  int64_t timestamp_multiplier(size_t col) const { return parquet_.timestamp_multiplier(col); }
  int64_t timestamp_divisor(size_t col) const { return parquet_.timestamp_divisor(col); }
  std::string signature() const {
    std::string s = options.format == "glob" ? glob_signature_ : input_.identity();
    for (const auto& c : columns)
      s += ";" + std::to_string(c.name.size()) + ":" + c.name + ":" +
           std::to_string(static_cast<int>(c.type)) + ":" + (c.nullable ? "1" : "0");
    return s;
  }
  int next_batch(const std::vector<size_t>& projection, size_t max_rows,
                 std::vector<std::vector<Cell>>& rows) {
    rows.clear();
    if (max_rows == 0) {
      error = "batch size must be positive";
      return -1;
    }
    // Stop after a small decoded-byte budget, rather than filling a batch of
    // arbitrarily wide rows. Only one bounded record can cross this threshold.
    size_t bytes = 0;
    while (rows.size() < std::min<size_t>(max_rows, 64) && bytes < MAX_RECORD) {
      std::vector<Cell> row;
      int result = next(projection, row);
      if (result < 0) {
        rows.clear();
        return -1;
      }
      if (result == 0) break;
      bytes += row.capacity() * sizeof(Cell);
      for (const auto& cell : row) bytes += cell.text.capacity();
      rows.push_back(std::move(row));
    }
    return rows.empty() ? 0 : 1;
  }
  int next_buffered(const std::vector<size_t>& projection, std::vector<Cell>& row) {
    if (!check_status() || !unchanged()) return -1;
    if (batch_index_ == batch_.size()) {
      batch_index_ = 0;
      int result = next_batch(projection, 64, batch_);
      if (result <= 0) return result;
    }
    row = std::move(batch_[batch_index_++]);
    return 1;
  }
  int next(const std::vector<size_t>& projection, std::vector<Cell>& out) {
    if (!check_status() || !unchanged()) return -1;
    if (options.format == "read_parquet") {
      int result = parquet_.next(input_, projection, out, error);
      return result >= 0 && !unchanged() ? -1 : result;
    }
    if (options.format == "glob") {
      if (file_index_ == 0 && !glob_unchanged(true)) return -1;
      if (file_index_ == files_.size()) return glob_unchanged(true) ? 0 : -1;
      struct stat st;
      if (::stat(files_[file_index_].c_str(), &st) != 0 ||
          file_sql::identity(st) != file_ids_[file_index_]) {
        error = "glob file changed during scan";
        return -1;
      }
      out.clear();
      for (size_t p : projection) {
        if (p != 0) {
          error = "bad glob projection";
          return -1;
        }
        out.emplace_back(files_[file_index_]);
      }
      ++file_index_;
      return 1;
    }
    std::vector<Cell> cells;
    int r = 0;
    if (options.format == "read_csv") {
      r = csv(cells);
      if (r > 0 && cells.size() != columns.size()) {
        error = "CSV field count changed at record " + std::to_string(line_);
        return -1;
      }
    } else {
      std::vector<std::pair<std::string, Cell>> obj;
      r = json(obj);
      if (r > 0) {
        cells.assign(columns.size(), Cell());
        for (auto& kv : obj) {
          size_t i = find(kv.first);
          if (i == columns.size()) {
            error = "JSON schema changed";
            return -1;
          }
          cells[i] = std::move(kv.second);
        }
      }
    }
    if (!unchanged()) return -1;
    if (r <= 0) return r;
    out.clear();
    for (size_t p : projection) {
      if (p >= cells.size()) {
        error = "invalid projection";
        return -1;
      }
      out.push_back(std::move(cells[p]));
    }
    return 1;
  }

 private:
  bool check_status() {
    if (status_check_ && (status_code_ = status_check_(status_context_)) != 0) {
      error = "file scan cancelled";
      return false;
    }
    return true;
  }
  size_t find(const std::string& n) const {
    for (size_t i = 0; i < columns.size(); ++i)
      if (lower(columns[i].name) == lower(n)) return i;
    return columns.size();
  }
  void unique_names() {
    std::set<std::string> used;
    for (auto& c : columns) {
      std::string base = c.name;
      int suffix = 2;
      while (!used.insert(lower(c.name)).second) c.name = base + "__" + std::to_string(suffix++);
    }
  }
  bool infer_row(const std::vector<Cell>& v) {
    if (v.size() != columns.size()) {
      error = "CSV field count mismatch at record " + std::to_string(line_);
      return false;
    }
    for (size_t i = 0; i < v.size(); ++i) {
      if (v[i].null)
        columns[i].nullable = true;
      else
        columns[i].type =
            widen(columns[i].type, v[i].observed == Type::NIL ? infer(v[i].text) : v[i].observed);
    }
    ++records_;
    return true;
  }
  int csv(std::vector<Cell>& out) {
    if (!check_status()) return -1;
    if (line_ == 0 && !input_.skip_utf8_bom(error)) return -1;
    out.clear();
    std::string text;
    bool quoted = false, closed = false, was_quoted = false, started = false;
    size_t size = 0;
    auto field = [&]() {
      Cell c(text);
      c.null = !was_quoted && text == options.nullstr;
      out.push_back(std::move(c));
      text.clear();
      was_quoted = false;
      closed = false;
      started = false;
    };
    while (true) {
      int c = input_.get();
      if (c == EOF) {
        if (input_.failed()) {
          error = "file I/O error";
          return -1;
        }
        if (quoted) {
          error = "unterminated CSV quote at record " + std::to_string(line_ + 1);
          return -1;
        }
        if (out.empty() && !started && !closed && text.empty()) return 0;
        field();
        ++line_;
        return validate(out);
      }
      if (++size > MAX_RECORD) {
        error = "CSV record exceeds resource limit";
        return -1;
      }
      if (quoted) {
        if (c == options.escape) {
          int n = input_.get();
          if (options.escape == options.quote) {
            if (n == options.quote) {
              if (++size > MAX_RECORD) {
                error = "CSV record exceeds resource limit";
                return -1;
              }
              text.push_back(n);
            } else {
              input_.unget(n);
              quoted = false;
              closed = true;
            }
          } else if (n == options.quote || n == options.escape) {
            if (++size > MAX_RECORD) {
              error = "CSV record exceeds resource limit";
              return -1;
            }
            text.push_back(n);
          } else {
            error = "invalid CSV escape at record " + std::to_string(line_ + 1);
            return -1;
          }
        } else if (c == options.quote) {
          quoted = false;
          closed = true;
        } else
          text.push_back(c);
        continue;
      }
      if (c == options.delimiter) {
        field();
        if (out.size() >= MAX_COLUMNS) {
          error = "too many CSV columns";
          return -1;
        }
      } else if (c == '\n' || c == '\r') {
        if (c == '\r') {
          int n = input_.get();
          if (n != '\n')
            input_.unget(n);
          else if (++size > MAX_RECORD) {
            error = "CSV record exceeds resource limit";
            return -1;
          }
        }
        field();
        ++line_;
        return validate(out);
      } else if (c == options.quote && !started) {
        quoted = true;
        was_quoted = true;
        started = true;
      } else if (closed || c == options.quote) {
        error = "unexpected character after CSV quote at record " + std::to_string(line_ + 1);
        return -1;
      } else {
        started = true;
        text.push_back(c);
      }
    }
  }
  int validate(std::vector<Cell>& v) {
    for (auto& c : v)
      if (!utf8(c.text)) {
        error = "invalid UTF-8 at CSV record " + std::to_string(line_);
        return -1;
      }
    return 1;
  }
  int json(std::vector<std::pair<std::string, Cell>>& obj) {
    if (!check_status()) return -1;
    std::string text;
    while (true) {
      int c = input_.get();
      if (c == EOF) {
        if (input_.failed()) {
          error = "JSONL I/O error";
          return -1;
        }
        if (text.empty()) return 0;
        break;
      }
      if (c == '\n') break;
      text.push_back(c);
      if (text.size() > MAX_RECORD) {
        error = "JSONL record exceeds resource limit";
        return -1;
      }
    }
    ++line_;
    if (line_ == 1 && text.compare(0, 3, "\xef\xbb\xbf") == 0) text.erase(0, 3);
    if (!utf8(text) || !FlatJson(text).parse(obj)) {
      error = "invalid flat JSON object at line " + std::to_string(line_);
      return -1;
    }
    return 1;
  }
  bool discover_glob() {
    size_t slash = options.path.find_last_of('/');
    std::string parent = slash == std::string::npos ? "."
                         : slash == 0               ? "/"
                                                    : options.path.substr(0, slash),
                real, root;
    if (parent.find_first_of("*?[") != std::string::npos || !canonical(parent, real) ||
        !canonical(options.root, root) || !within(root, real)) {
      error = "glob path rejected by secure_file_priv";
      return false;
    }
    int fd = open_directory(real);
    DIR* directory = fd < 0 ? nullptr : ::fdopendir(fd);
    if (directory == nullptr) {
      if (fd >= 0) ::close(fd);
    }
    if (directory == nullptr) {
      error = "cannot open glob directory";
      return false;
    }
    glob_directory_ = real;
    if (::fstat(::dirfd(directory), &glob_snapshot_) != 0) {
      ::closedir(directory);
      error = "cannot inspect glob directory";
      return false;
    }
    std::string pattern =
        slash == std::string::npos ? options.path : options.path.substr(slash + 1);
    struct dirent* entry = nullptr;
    bool ok = true;
    errno = 0;
    while ((entry = ::readdir(directory)) != nullptr) {
      if (::fnmatch(pattern.c_str(), entry->d_name, FNM_PERIOD) != 0) continue;
      if (files_.size() >= MAX_FILES) {
        error = "glob exceeds file limit";
        ok = false;
        break;
      }
      std::string p;
      if (!canonical(real + "/" + entry->d_name, p) || !within(root, p)) {
        error = "glob match rejected by secure_file_priv";
        ok = false;
        break;
      }
      struct stat st;
      if (::stat(p.c_str(), &st) != 0) {
        error = "glob file changed during discovery";
        ok = false;
        break;
      }
      if (S_ISREG(st.st_mode)) {
        if (!utf8(p)) {
          error = "glob path is not UTF-8";
          ok = false;
          break;
        }
        files_.push_back(p);
      }
      errno = 0;
    }
    if (entry == nullptr && errno != 0) {
      error = "glob directory read failed";
      ok = false;
    }
    ::closedir(directory);
    if (!ok) return false;
    std::sort(files_.begin(), files_.end());
    glob_signature_ = file_sql::identity(glob_snapshot_);
    for (const auto& path : files_) {
      struct stat st;
      if (::stat(path.c_str(), &st) != 0) {
        error = "glob file changed during discovery";
        return false;
      }
      file_ids_.push_back(file_sql::identity(st));
      glob_signature_ += ";" + std::to_string(path.size()) + ":" + path + ":" + file_ids_.back();
      if (glob_signature_.size() > MAX_RECORD) {
        error = "glob metadata exceeds resource limit";
        return false;
      }
    }
    if (!glob_unchanged(true)) return false;
    Column c;
    c.name = "file";
    c.type = Type::VARCHAR;
    columns.push_back(c);
    return true;
  }
  bool glob_unchanged(bool check_files) {
    struct stat st;
    if (::stat(glob_directory_.c_str(), &st) != 0 || !same_snapshot(st, glob_snapshot_)) {
      error = "glob directory changed during SQL scan";
      return false;
    }
    if (check_files) {
      // Some filesystems coalesce directory timestamps. Verify membership at
      // scan boundaries as well, so a same-tick creation cannot go unnoticed.
      int fd = open_directory(glob_directory_);
      DIR* directory = fd < 0 ? nullptr : ::fdopendir(fd);
      if (directory == nullptr) {
        if (fd >= 0) ::close(fd);
        error = "cannot recheck glob directory";
        return false;
      }
      const size_t slash = options.path.find_last_of('/');
      const std::string pattern = slash == std::string::npos
                                      ? options.path : options.path.substr(slash + 1);
      std::vector<std::string> current;
      struct dirent* entry = nullptr;
      bool ok = true;
      errno = 0;
      while ((entry = ::readdir(directory)) != nullptr) {
        if (::fnmatch(pattern.c_str(), entry->d_name, FNM_PERIOD) == 0) {
          std::string path;
          if (!canonical(glob_directory_ + "/" + entry->d_name, path) ||
              ::stat(path.c_str(), &st) != 0) {
            ok = false;
            break;
          }
          if (S_ISREG(st.st_mode)) current.push_back(path);
          if (current.size() > MAX_FILES) {
            ok = false;
            break;
          }
        }
        errno = 0;
      }
      if (entry == nullptr && errno != 0) ok = false;
      ::closedir(directory);
      std::sort(current.begin(), current.end());
      if (!ok || current != files_) {
        error = "glob file set changed during SQL scan";
        return false;
      }
      for (size_t i = 0; i < files_.size(); ++i) {
        if (::stat(files_[i].c_str(), &st) != 0 || file_sql::identity(st) != file_ids_[i]) {
          error = "glob file changed during SQL scan";
          return false;
        }
      }
    }
    return true;
  }
  std::string glob_directory_, glob_signature_;
  struct stat glob_snapshot_{};
  std::vector<std::string> file_ids_;
  Input input_;
  Parquet parquet_;
  size_t line_ = 0, records_ = 0, file_index_ = 0;
  std::vector<std::string> files_;
  std::vector<std::vector<Cell>> batch_;
  size_t batch_index_ = 0;
  int (*status_check_)(void*) = nullptr;
  void* status_context_ = nullptr;
  int status_code_ = 0;
};

}  // namespace file_sql
}  // namespace sql
}  // namespace oceanbase
