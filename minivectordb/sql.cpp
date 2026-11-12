#include "sql.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>

namespace mvdb {

std::string lower(std::string text) {
  for (char &ch : text) {
    if (ch >= 'A' && ch <= 'Z') ch = static_cast<char>(ch + ('a' - 'A'));
  }
  return text;
}

size_t unicode_length(std::string_view text) {
  size_t count = 0;
  for (size_t i = 0; i < text.size();) {
    // ASCII spans are frequent in vector literals. memcpy is alignment safe;
    // non-ASCII still takes the same complete scalar-value validation below.
    while (text.size() - i >= sizeof(uint64_t)) {
      uint64_t word;
      std::memcpy(&word, text.data() + i, sizeof(word));
      if ((word & 0x8080808080808080ULL) != 0) break;
      i += sizeof(word); count += sizeof(word);
    }
    if (i == text.size()) break;
    ++count;
    const auto first = static_cast<unsigned char>(text[i++]);
    if (first < 0x80) continue;
    unsigned tail = 0;
    uint32_t value = 0;
    uint32_t minimum = 0;
    if (first >= 0xc2 && first <= 0xdf) {
      tail = 1; value = first & 0x1fU; minimum = 0x80;
    } else if (first >= 0xe0 && first <= 0xef) {
      tail = 2; value = first & 0x0fU; minimum = 0x800;
    } else if (first >= 0xf0 && first <= 0xf4) {
      tail = 3; value = first & 0x07U; minimum = 0x10000;
    } else {
      throw Error("invalid_unicode", "invalid UTF-8 leading byte");
    }
    require(i + tail <= text.size(), "invalid_unicode", "incomplete UTF-8 sequence");
    for (unsigned j = 0; j < tail; ++j) {
      const auto next = static_cast<unsigned char>(text[i++]);
      require((next & 0xc0U) == 0x80U, "invalid_unicode", "invalid UTF-8 continuation");
      value = (value << 6U) | (next & 0x3fU);
    }
    require(value >= minimum && value <= 0x10ffff &&
                !(value >= 0xd800 && value <= 0xdfff),
            "invalid_unicode", "invalid Unicode scalar value");
  }
  return count;
}

std::string json_string(std::string_view text) {
  constexpr char hex[] = "0123456789abcdef";
  std::string out = "\"";
  for (char raw : text) {
    const auto ch = static_cast<unsigned char>(raw);
    if (ch == '"' || ch == '\\') {
      out += '\\'; out += static_cast<char>(ch);
    } else if (ch < 0x20) {
      out += "\\u00"; out += hex[ch >> 4U]; out += hex[ch & 15U];
    } else {
      out += static_cast<char>(ch);
    }
  }
  out += '"';
  return out;
}

int64_t bigint(std::string_view text) {
  if (!text.empty() && text.front() == '+') {
    text.remove_prefix(1);
    require(!text.empty() && text.front() >= '0' && text.front() <= '9',
            "invalid_bigint", "expected signed 64-bit integer");
  }
  int64_t value = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
  require(!text.empty() && result.ec == std::errc{} &&
              result.ptr == text.data() + text.size(),
          "invalid_bigint", "expected signed 64-bit integer");
  return value;
}

namespace {
bool space(char ch) { return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n' || ch == '\f'; }
bool digit(char ch) { return ch >= '0' && ch <= '9'; }
bool letter(char ch) {
  return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch == '_';
}
bool number_syntax(std::string_view value) {
  size_t i = 0;
  if (i < value.size() && (value[i] == '-' || value[i] == '+')) ++i;
  size_t before = i;
  while (i < value.size() && digit(value[i])) ++i;
  bool has_digit = i > before;
  if (i < value.size() && value[i] == '.') {
    before = ++i;
    while (i < value.size() && digit(value[i])) ++i;
    has_digit = has_digit || i > before;
  }
  if (!has_digit) return false;
  if (i < value.size() && (value[i] == 'e' || value[i] == 'E')) {
    ++i;
    if (i < value.size() && (value[i] == '-' || value[i] == '+')) ++i;
    before = i;
    while (i < value.size() && digit(value[i])) ++i;
    if (i == before) return false;
  }
  return i == value.size();
}

template <typename Number>
Number vector_number(std::string_view token) {
  require(!token.empty(), "invalid_vector", "vector element must be a finite number");
  Number value = 0;
  if constexpr (requires { std::from_chars(token.data(), token.data() + token.size(), value); }) {
    auto number = token;
    if (number.front() == '+') {
      number.remove_prefix(1);
      require(!number.empty() && number.front() != '+' && number.front() != '-',
              "invalid_vector", "vector element must be a finite number");
    }
    const auto result = std::from_chars(number.data(), number.data() + number.size(), value);
    if (result.ec == std::errc{} && result.ptr == number.data() + number.size()) return value;
  }
  // Preserve strtod underflow rounding and support libraries without floating from_chars.
  // from_chars already validated a fully consumed decimal on the common path.
  // The fallback needs explicit grammar checks because strtod accepts hex.
  require(number_syntax(token), "invalid_vector", "vector element must be a finite number");
  const std::string copy(token);
  char *end = nullptr;
  value = std::strtod(copy.c_str(), &end);
  require(end == copy.c_str() + copy.size(),
          "invalid_vector", "float32 vector element overflow or non-finite value");
  return value;
}

struct Token {
  enum Tag { End, Word, Number, String, Symbol, Identifier } tag = End;
  std::string text;
};
class Parser {
 public:
  explicit Parser(std::string_view sql) : sql_(sql) { next(); }
  Statement statement() {
    Statement s;
    if (take("begin")) {
      s.kind = Kind::Begin;
      take("transaction");
    } else if (take("commit")) {
      s.kind = Kind::Commit;
    } else if (take("rollback")) {
      s.kind = Kind::Rollback;
    } else if (take("create")) {
      if (take("table")) {
        s.kind = Kind::Create; s.table = identifier(); expect("(");
        std::string table_pk;
        do {
          if (take("primary")) {
            require(table_pk.empty(), "invalid_schema", "duplicate PRIMARY KEY declaration");
            expect("key"); expect("("); table_pk = identifier(); expect(")");
          } else {
            Column c; c.name = identifier();
            if (take("bigint")) {
              c.type = Type::Bigint;
            } else if (take("varchar")) {
              c.type = Type::Varchar; c.size = size_parameter();
            } else if (take("vector")) {
              c.type = Type::Vector; c.size = size_parameter();
            } else {
              throw Error("unsupported", "unsupported column type");
            }
            if (take("primary")) { expect("key"); c.primary = true; }
            s.columns.push_back(c);
          }
        } while (take(","));
        expect(")");
        if (!table_pk.empty()) {
          bool found = false;
          for (auto &c : s.columns) {
            require(!c.primary, "invalid_schema", "duplicate PRIMARY KEY declaration");
            if (c.name == table_pk) { c.primary = true; found = true; }
          }
          require(found, "unknown_column", "primary key column does not exist");
        }
      } else {
        expect("vector"); expect("index"); s.kind = Kind::Index;
        s.index = identifier(); expect("on"); s.table = identifier();
        expect("("); s.names.push_back(identifier()); expect(")");
      }
    } else if (take("insert")) {
      s.kind = Kind::Insert; expect("into"); s.table = identifier();
      if (take("(")) {
        do { s.names.push_back(identifier()); } while (take(","));
        expect(")");
      }
      expect("values");
      do {
        expect("(");
        std::vector<Literal> row;
        do { row.push_back(literal()); } while (take(","));
        expect(")"); s.rows.push_back(std::move(row));
      } while (take(","));
    } else if (take("select")) {
      s.kind = Kind::Select;
      if (take("*")) { s.star = true; }
      else {
        do {
          Expr e = expression();
          if (take("as")) e.alias = identifier();
          s.projection.push_back(std::move(e));
        } while (take(","));
      }
      expect("from"); s.table = identifier(); predicates(s);
      if (take("order")) {
        expect("by"); s.ordered = true; s.order = expression();
        s.descending = take("desc");
        if (!s.descending) take("asc");
      }
      if (take("limit")) {
        require(current_.tag == Token::Number, "syntax", "expected LIMIT count");
        const auto n = bigint(current_.text);
        require(n >= 0, "invalid_limit", "negative LIMIT");
        s.limit = static_cast<uint64_t>(n); next();
      }
    } else if (take("update")) {
      s.kind = Kind::Update; s.table = identifier(); expect("set");
      do {
        s.names.push_back(identifier()); expect("="); s.values.push_back(literal());
      } while (take(","));
      predicates(s);
    } else if (take("delete")) {
      s.kind = Kind::Delete; expect("from"); s.table = identifier(); predicates(s);
    } else {
      throw Error("unsupported", "unsupported SQL statement");
    }
    take(";");
    require(current_.tag == Token::End, "syntax", "unexpected trailing SQL");
    return s;
  }
 private:
  std::string_view sql_;
  size_t pos_ = 0;
  Token current_;
  void next() {
    while (pos_ < sql_.size()) {
      if (space(sql_[pos_])) { ++pos_; continue; }
      if (sql_[pos_] == '#' || sql_.substr(pos_, 2) == "--") {
        while (pos_ < sql_.size() && sql_[pos_] != '\n') ++pos_;
        continue;
      }
      if (sql_.substr(pos_, 2) == "/*") {
        const auto end = sql_.find("*/", pos_ + 2);
        require(end != std::string_view::npos, "syntax", "unterminated comment");
        pos_ = end + 2; continue;
      }
      break;
    }
    current_ = {};
    if (pos_ == sql_.size()) return;
    const char ch = sql_[pos_++];
    if (ch == '\'' || ch == '"' || ch == '`') {
      current_.tag = ch == '`' ? Token::Identifier : Token::String;
      bool closed = false;
      const auto quote = sql_.find(ch, pos_);
      if (quote != std::string_view::npos &&
          (quote + 1 == sql_.size() || sql_[quote + 1] != ch)) {
        const auto plain = sql_.substr(pos_, quote - pos_);
        if (ch == '`' || plain.find('\\') == std::string_view::npos) {
          current_.text.assign(plain);
          pos_ = quote + 1;
          return;
        }
      }
      while (pos_ < sql_.size()) {
        char c = sql_[pos_++];
        if (c == ch) {
          if (pos_ < sql_.size() && sql_[pos_] == ch) { ++pos_; }
          else { closed = true; break; }
        } else if (c == '\\' && ch != '`') {
          require(pos_ < sql_.size(), "syntax", "unterminated escape");
          c = sql_[pos_++];
          if (c == 'n') c = '\n';
          else if (c == 'r') c = '\r';
          else if (c == 't') c = '\t';
          else if (c == '0') c = '\0';
          else if (c == 'b') c = '\b';
          else if (c == 'Z') c = 26;
        }
        current_.text += c;
      }
      require(closed, "syntax", "unterminated quoted token");
    } else if (letter(ch)) {
      current_.tag = Token::Word; current_.text = ch;
      while (pos_ < sql_.size() && (letter(sql_[pos_]) || digit(sql_[pos_]))) {
        current_.text += sql_[pos_++];
      }
    } else if (digit(ch) || ch == '-' || ch == '+' || ch == '.') {
      current_.tag = Token::Number; current_.text = ch;
      while (pos_ < sql_.size()) {
        const char c = sql_[pos_];
        if (digit(c) || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
          current_.text += c; ++pos_;
        } else break;
      }
    } else {
      current_.tag = Token::Symbol; current_.text = ch;
      if ((ch == '<' || ch == '>') && pos_ < sql_.size() && sql_[pos_] == '=') {
        current_.text += sql_[pos_++];
      }
    }
  }
  bool take(std::string_view text) {
    if ((current_.tag == Token::Word || current_.tag == Token::Symbol) &&
        lower(current_.text) == text) {
      next(); return true;
    }
    return false;
  }
  void expect(std::string_view text) {
    require(take(text), "syntax", "expected " + std::string(text));
  }
  std::string identifier() {
    require(current_.tag == Token::Word || current_.tag == Token::Identifier,
            "syntax", "expected identifier");
    require(!current_.text.empty(), "syntax", "empty identifier");
    std::string result = lower(current_.text); next(); return result;
  }
  uint32_t size_parameter() {
    expect("(");
    require(current_.tag == Token::Number, "syntax", "expected type dimension");
    auto n = bigint(current_.text);
    require(n > 0 && n <= 1048576, "invalid_schema", "type dimension outside local limits");
    next(); expect(")"); return static_cast<uint32_t>(n);
  }
  Literal literal() {
    Literal value;
    if (take("[")) {
      value.text = "[";
      do {
        require(current_.tag == Token::Number, "invalid_vector", "expected vector number");
        value.text += current_.text; next();
        if (!take(",")) break;
        value.text += ',';
      } while (true);
      expect("]"); value.text += ']'; return value;
    }
    require(current_.tag == Token::Number || current_.tag == Token::String ||
                (current_.tag == Token::Word &&
                 (lower(current_.text) == "nan" || lower(current_.text) == "infinity")),
            "syntax", "expected literal");
    value.quoted = current_.tag == Token::String;
    value.text = current_.text; next(); return value;
  }
  Expr expression() {
    Expr e;
    if (take("l2_distance")) {
      e.distance = true; expect("("); e.column = identifier(); expect(",");
      e.query = literal(); expect(")");
    } else e.column = identifier();
    return e;
  }
  void predicates(Statement &s) {
    if (!take("where")) return;
    do {
      Predicate p; p.column = identifier(); p.op = current_.text;
      require(current_.tag == Token::Symbol &&
                  (p.op == "=" || p.op == "<" || p.op == "<=" || p.op == ">" || p.op == ">="),
              "unsupported", "unsupported comparison");
      next(); p.value = literal(); s.where.push_back(std::move(p));
    } while (take("and"));
  }
};
}  // namespace

std::vector<float> vector_literal(std::string_view text, size_t dimension) {
  size_t pos = 0;
  const auto skip = [&] { while (pos < text.size() && space(text[pos])) ++pos; };
  skip();
  require(pos < text.size() && text[pos++] == '[', "invalid_vector", "expected vector array");
  std::vector<float> out;
  out.reserve(std::min(dimension, text.size() / 2 + 1));
  skip();
  if (pos < text.size() && text[pos] != ']') {
    while (true) {
      skip(); const size_t start = pos;
      while (pos < text.size() && text[pos] != ',' && text[pos] != ']' && !space(text[pos])) ++pos;
      const auto token = text.substr(start, pos - start);
      const double n = vector_number<double>(token);
      const float f = static_cast<float>(n);
      require(std::isfinite(n) && std::isfinite(f),
              "invalid_vector", "float32 vector element overflow or non-finite value");
      out.push_back(f);
      require(out.size() <= dimension, "invalid_vector", "vector dimension mismatch");
      skip();
      if (pos < text.size() && text[pos] == ',') { ++pos; continue; }
      break;
    }
  }
  skip();
  require(pos < text.size() && text[pos++] == ']', "invalid_vector", "unterminated vector");
  skip();
  require(pos == text.size() && out.size() == dimension, "invalid_vector", "vector dimension mismatch");
  return out;
}

Statement parse(std::string_view sql) {
  unicode_length(sql);
  return Parser(sql).statement();
}

}  // namespace mvdb
