#include "protocol.h"
#include "sql.h"

#include <cmath>
#include <cstdlib>
#include <unordered_set>

namespace mvdb {
namespace {
class JsonParser {
 public:
  explicit JsonParser(std::string_view input) : input_(input) {}
  Request request() {
    unicode_length(input_);
    Request result;
    expect('{');
    std::unordered_set<std::string> keys;
    if (!take('}')) {
      do {
        const auto key = string();
        require(keys.insert(key).second, "protocol", "duplicate JSON key");
        expect(':');
        if (key == "id") {
          whitespace();
          const auto start = pos_;
          value(0);
          result.id = input_.substr(start, pos_ - start);
          result.has_id = true;
        } else if (key == "op") result.op = string();
        else if (key == "sql") { result.sql = string(); result.has_sql = true; }
        else if (key == "search_mode") result.search_mode = string();
        else value(0);
      } while (take(','));
      expect('}');
    }
    whitespace();
    require(pos_ == input_.size(), "protocol", "trailing JSON data");
    return result;
  }
 private:
  std::string_view input_;
  size_t pos_ = 0;
  void whitespace() {
    while (pos_ < input_.size() && (input_[pos_] == ' ' || input_[pos_] == '\t' ||
           input_[pos_] == '\r' || input_[pos_] == '\n')) ++pos_;
  }
  bool take(char ch) {
    whitespace();
    if (pos_ == input_.size() || input_[pos_] != ch) return false;
    ++pos_; return true;
  }
  void expect(char ch) { require(take(ch), "protocol", "invalid JSON syntax"); }
  bool digit() const { return pos_ < input_.size() && input_[pos_] >= '0' && input_[pos_] <= '9'; }
  uint32_t hex4() {
    uint32_t result = 0;
    for (unsigned i = 0; i < 4; ++i) {
      require(pos_ < input_.size(), "protocol", "incomplete Unicode escape");
      const char ch = input_[pos_++];
      uint32_t n = 0;
      if (ch >= '0' && ch <= '9') n = static_cast<uint32_t>(ch - '0');
      else if (ch >= 'a' && ch <= 'f') n = static_cast<uint32_t>(ch - 'a' + 10);
      else if (ch >= 'A' && ch <= 'F') n = static_cast<uint32_t>(ch - 'A' + 10);
      else throw Error("protocol", "invalid Unicode escape");
      result = (result << 4U) | n;
    }
    return result;
  }
  void unicode(std::string &out) {
    auto code = hex4();
    if (code >= 0xd800 && code <= 0xdbff) {
      require(pos_ + 2 <= input_.size() && input_.substr(pos_, 2) == "\\u",
              "protocol", "missing low surrogate");
      pos_ += 2;
      const auto tail = hex4();
      require(tail >= 0xdc00 && tail <= 0xdfff, "protocol", "invalid low surrogate");
      code = 0x10000 + ((code - 0xd800) << 10U) + tail - 0xdc00;
    }
    require(code < 0xdc00 || code > 0xdfff, "protocol", "unpaired low surrogate");
    if (code < 0x80) out += static_cast<char>(code);
    else if (code < 0x800) {
      out += static_cast<char>(0xc0U | (code >> 6U));
      out += static_cast<char>(0x80U | (code & 0x3fU));
    } else if (code < 0x10000) {
      out += static_cast<char>(0xe0U | (code >> 12U));
      out += static_cast<char>(0x80U | ((code >> 6U) & 0x3fU));
      out += static_cast<char>(0x80U | (code & 0x3fU));
    } else {
      out += static_cast<char>(0xf0U | (code >> 18U));
      out += static_cast<char>(0x80U | ((code >> 12U) & 0x3fU));
      out += static_cast<char>(0x80U | ((code >> 6U) & 0x3fU));
      out += static_cast<char>(0x80U | (code & 0x3fU));
    }
  }
  std::string string() {
    expect('"');
    const auto quote = input_.find('"', pos_);
    if (quote != std::string_view::npos) {
      const auto plain = input_.substr(pos_, quote - pos_);
      if (plain.find('\\') == std::string_view::npos) {
        unsigned controls = 0;
        for (char ch : plain) controls |= static_cast<unsigned char>(ch) < 0x20;
        require(controls == 0, "protocol", "unescaped control character");
        pos_ = quote + 1;
        return std::string(plain);
      }
    }
    std::string out;
    while (pos_ < input_.size()) {
      const char ch = input_[pos_++];
      if (ch == '"') return out;
      require(static_cast<unsigned char>(ch) >= 0x20, "protocol", "unescaped control character");
      if (ch != '\\') { out += ch; continue; }
      require(pos_ < input_.size(), "protocol", "incomplete JSON escape");
      switch (input_[pos_++]) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': unicode(out); break;
        default: throw Error("protocol", "invalid JSON escape");
      }
    }
    throw Error("protocol", "unterminated JSON string");
  }
  void number() {
    const auto start = pos_;
    if (pos_ < input_.size() && input_[pos_] == '-') ++pos_;
    require(digit(), "protocol", "invalid JSON number");
    if (input_[pos_] == '0') ++pos_;
    else while (digit()) ++pos_;
    bool fractional = false;
    if (pos_ < input_.size() && input_[pos_] == '.') {
      fractional = true; ++pos_;
      require(digit(), "protocol", "missing fraction");
      while (digit()) ++pos_;
    }
    if (pos_ < input_.size() && (input_[pos_] == 'e' || input_[pos_] == 'E')) {
      fractional = true; ++pos_;
      if (pos_ < input_.size() && (input_[pos_] == '+' || input_[pos_] == '-')) ++pos_;
      require(digit(), "protocol", "missing exponent");
      while (digit()) ++pos_;
    }
    if (fractional) {
      const std::string token(input_.substr(start, pos_ - start));
      require(std::isfinite(std::strtod(token.c_str(), nullptr)), "protocol", "non-finite JSON number");
    }
  }
  void value(unsigned depth) {
    require(depth < 128, "protocol", "JSON nesting limit exceeded");
    whitespace();
    require(pos_ < input_.size(), "protocol", "missing JSON value");
    if (input_[pos_] == '"') { string(); return; }
    if (take('{')) {
      std::unordered_set<std::string> keys;
      if (!take('}')) {
        do {
          require(keys.insert(string()).second, "protocol", "duplicate JSON key");
          expect(':'); value(depth + 1);
        } while (take(','));
        expect('}');
      }
    } else if (take('[')) {
      if (!take(']')) {
        do { value(depth + 1); } while (take(','));
        expect(']');
      }
    } else {
      for (const std::string_view literal : {"true", "false", "null"}) {
        if (input_.substr(pos_, literal.size()) == literal) { pos_ += literal.size(); return; }
      }
      number();
    }
  }
};
}  // namespace
Request parse_request(std::string_view line) { return JsonParser(line).request(); }
}  // namespace mvdb
