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

#include <gtest/gtest.h>

#include <fstream>

#include "sql/engine/basic/file_copy_utils.h"
#include "sql/engine/basic/file_scan_utils.h"

namespace oceanbase {
namespace sql {
namespace file_sql {
class FileScanUtilsTest : public testing::Test {
 protected:
  void SetUp() override {
    char pattern[] = "/tmp/seekdb-file-scan-XXXXXX";
    char* p = ::mkdtemp(pattern);
    ASSERT_NE(nullptr, p);
    ASSERT_TRUE(canonical(p, dir));
  }
  void TearDown() override {
    for (const auto& p : paths) ::unlink(p.c_str());
    ::rmdir(dir.c_str());
  }
  std::string write(const std::string& name, const std::string& data) {
    std::string p = dir + "/" + name;
    std::ofstream f(p, std::ios::binary);
    f.write(data.data(), data.size());
    f.close();
    paths.push_back(p);
    return p;
  }
  Options opts(const std::string& name, const std::string& data,
               const std::string& format = "read_csv") {
    Options o;
    o.root = dir;
    o.path = write(name, data);
    o.format = format;
    return o;
  }
  std::string fixture(const char* name) {
    std::string path = __FILE__;
    path.resize(path.find_last_of('/'));
    path += "/data/";
    path += name;
    std::ifstream f(path);
    EXPECT_TRUE(f.is_open());
    std::string encoded((std::istreambuf_iterator<char>(f)), {}), bytes;
    const std::string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    uint32_t value = 0;
    int bits = 0;
    for (char c : encoded) {
      size_t d = alphabet.find(c);
      if (d == std::string::npos) continue;
      value = (value << 6) | d;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        bytes.push_back(value >> bits);
      }
    }
    return bytes;
  }
  std::string dir;
  std::vector<std::string> paths;
};
TEST_F(FileScanUtilsTest, BatchesRescanAndBufferedMutation) {
  std::string data = "id\n";
  for (int i = 0; i < 150; ++i) data += std::to_string(i) + "\n";
  auto options = opts("batch.csv", data);
  Scanner scanner;
  ASSERT_TRUE(scanner.open(options)) << scanner.error;
  std::vector<std::vector<Cell>> rows;
  int total = 0;
  for (size_t expected : {64, 64, 22}) {
    ASSERT_EQ(1, scanner.next_batch({0}, 1000, rows)) << scanner.error;
    ASSERT_EQ(expected, rows.size());
    for (const auto& row : rows) EXPECT_EQ(std::to_string(total++), row[0].text);
  }
  EXPECT_EQ(0, scanner.next_batch({0}, 64, rows));
  EXPECT_EQ(150, total);
  ASSERT_TRUE(scanner.rewind());
  std::vector<Cell> row;
  ASSERT_EQ(1, scanner.next_buffered({0}, row));
  EXPECT_EQ("0", row[0].text);
  write("batch.csv", "id\n999\n");
  EXPECT_EQ(-1, scanner.next_buffered({0}, row));
  EXPECT_FALSE(scanner.rewind());
}
TEST_F(FileScanUtilsTest, CancellationDuringDiscoveryAndExecution) {
  std::string data = "id\n";
  for (int i = 0; i < 1000; ++i) data += "1\n";
  auto options = opts("cancel.csv", data);
  int calls = 0;
  Scanner scanner;
  scanner.set_status_check([](void* p) { return ++*static_cast<int*>(p) > 20 ? -4012 : 0; },
                           &calls);
  EXPECT_FALSE(scanner.open(options));
  EXPECT_EQ(-4012, scanner.status_code());
  EXPECT_LT(calls, 1000);
  Scanner execution;
  ASSERT_TRUE(execution.open(options));
  execution.set_status_check([](void*) { return -4012; }, nullptr);
  std::vector<std::vector<Cell>> rows;
  EXPECT_EQ(-1, execution.next_batch({0}, 64, rows));
  EXPECT_TRUE(rows.empty());
  EXPECT_FALSE(execution.rewind());
}
TEST_F(FileScanUtilsTest, CsvBomBeforeQuotedHeaderAndRescan) {
  Scanner scanner;
  Options options = opts("bom.csv", "\xef\xbb\xbf\"id\",name\r\n1,中文\r\n");
  ASSERT_TRUE(scanner.open(options)) << scanner.error;
  ASSERT_EQ(2u, scanner.columns.size());
  EXPECT_EQ("id", scanner.columns[0].name);
  std::vector<Cell> row;
  ASSERT_EQ(1, scanner.next({0, 1}, row));
  EXPECT_EQ("1", row[0].text);
  EXPECT_EQ("中文", row[1].text);
  ASSERT_TRUE(scanner.rewind());
  ASSERT_EQ(1, scanner.next({0}, row));
  EXPECT_EQ("1", row[0].text);
  options = opts("bom-no-header.csv", "\xef\xbb\xbf\"1\"\n");
  options.header = false;
  ASSERT_TRUE(scanner.open(options)) << scanner.error;
  EXPECT_EQ(Type::BIGINT, scanner.columns[0].type);
  ASSERT_EQ(1, scanner.next({0}, row));
  EXPECT_EQ("1", row[0].text);
  options = opts("ordinary-utf8.csv", "！\nvalue\n");
  ASSERT_TRUE(scanner.open(options)) << scanner.error;
  EXPECT_EQ("！", scanner.columns[0].name);
}
TEST_F(FileScanUtilsTest, GlobSortedAndChangesInvalidateSnapshot) {
  write("b.csv", "id\n2\n");
  write("a.csv", "id\n1\n");
  Options options;
  options.root = dir;
  options.path = dir + "/*.csv";
  options.format = "glob";
  Scanner scanner;
  ASSERT_TRUE(scanner.open(options)) << scanner.error;
  std::vector<Cell> row;
  ASSERT_EQ(1, scanner.next({0}, row));
  EXPECT_EQ(dir + "/a.csv", row[0].text);
  ASSERT_EQ(1, scanner.next({0}, row));
  EXPECT_EQ(dir + "/b.csv", row[0].text);
  EXPECT_EQ(0, scanner.next({0}, row));
  ASSERT_TRUE(scanner.rewind());
  write("c.csv", "id\n3\n");
  EXPECT_EQ(-1, scanner.next({0}, row));
  Scanner replacement;
  ASSERT_TRUE(replacement.open(options));
  EXPECT_NE(scanner.signature(), replacement.signature());
  write("b.csv", "id\n12345\n");
  EXPECT_FALSE(replacement.rewind());
}
TEST_F(FileScanUtilsTest, CopyReuseDoesNotUnlinkUnownedTemporaryName) {
  Options options;
  options.root = dir;
  options.path = dir + "/output.csv";
  std::string old_temp;
  {
    CsvWriter writer;
    ASSERT_TRUE(writer.open(options));
    DIR* directory = ::opendir(dir.c_str());
    ASSERT_NE(nullptr, directory);
    while (auto* entry = ::readdir(directory)) {
      if (std::string(entry->d_name).find(".seekdb-copy-") == 0) old_temp = entry->d_name;
    }
    ::closedir(directory);
    ASSERT_FALSE(old_temp.empty());
    options.path = write("existing.csv", "keep");
    ASSERT_FALSE(writer.open(options));
    write(old_temp, "belongs to someone else");
  }
  EXPECT_EQ(0, ::access((dir + "/" + old_temp).c_str(), F_OK));
}
TEST_F(FileScanUtilsTest, CopyBoundsEncodedRecordAndRejectsInvalidUtf8) {
  Options options;
  options.root = dir;
  options.path = dir + "/oversized.csv";
  {
    CsvWriter writer;
    ASSERT_TRUE(writer.open(options));
    EXPECT_FALSE(writer.row({Cell(std::string(MAX_RECORD / 2, '\"'))}));
  }
  EXPECT_NE(0, ::access(options.path.c_str(), F_OK));
  {
    CsvWriter writer;
    ASSERT_TRUE(writer.open(options));
    EXPECT_FALSE(writer.row({Cell(std::string(1, char(0xff)))}));
  }
  DIR* directory = ::opendir(dir.c_str());
  ASSERT_NE(nullptr, directory);
  while (auto* entry = ::readdir(directory))
    EXPECT_NE(0, std::string(entry->d_name).find(".seekdb-copy-"));
  ::closedir(directory);
}
TEST_F(FileScanUtilsTest, ParquetUnsignedBoundariesAndNullLogicalType) {
  for (const char* name : {"unsigned-null-v1.0.parquet.b64", "unsigned-null-v2.0.parquet.b64"}) {
    Scanner scanner;
    ASSERT_TRUE(scanner.open(opts("unsigned.parquet", fixture(name), "read_parquet")))
        << scanner.error;
    ASSERT_EQ(3, scanner.columns.size());
    EXPECT_EQ(Type::BIGINT, scanner.columns[0].type);
    EXPECT_EQ(Type::VARCHAR, scanner.columns[1].type);
    EXPECT_EQ(Type::NIL, scanner.columns[2].type);
    std::vector<Cell> row;
    ASSERT_EQ(1, scanner.next({0, 1, 2}, row));
    EXPECT_EQ("0", row[0].text);
    EXPECT_EQ("0", row[1].text);
    EXPECT_TRUE(row[2].null);
    ASSERT_EQ(1, scanner.next({0, 1, 2}, row));
    EXPECT_EQ("4294967295", row[0].text);
    EXPECT_EQ("18446744073709551615", row[1].text);
    ASSERT_EQ(1, scanner.next({0, 1, 2}, row));
    for (const auto& cell : row) EXPECT_TRUE(cell.null);
    EXPECT_EQ(0, scanner.next({0, 1, 2}, row));
  }
}
TEST_F(FileScanUtilsTest, MalformedParquetAndCompressionDoNotReadOutOfBounds) {
  const std::string original = fixture("simple.parquet.b64");
  for (size_t size = 0; size < original.size(); ++size) {
    Scanner scanner;
    if (scanner.open(opts("truncated.parquet", original.substr(0, size), "read_parquet"))) {
      std::vector<Cell> row;
      int result;
      size_t count = 0;
      while ((result = scanner.next({0}, row)) > 0) ASSERT_LT(++count, 100);
      EXPECT_EQ(-1, result);
    }
  }
  std::vector<uint64_t> values;
  size_t offset = 100;
  EXPECT_FALSE(unpack("", offset, 1, 1, values));
  for (const std::string& bytes :
       {std::string("\x80", 1), std::string("\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x02", 11)}) {
    offset = 0;
    uint64_t value;
    EXPECT_FALSE(varint(bytes, offset, value));
    std::string decoded;
    EXPECT_FALSE(snappy(bytes, decoded, 5));
  }
}
TEST_F(FileScanUtilsTest, CompactRejectsOutOfRangeAndDuplicateFieldIds) {
  for (const std::string& bytes :
       {std::string("\x05\x80\x80\x04\x00\x00", 6), std::string("\x15\x00\x05\x02\x00\x00", 6)}) {
    Meta metadata;
    EXPECT_FALSE(Compact(bytes).object(metadata));
  }
}

TEST_F(FileScanUtilsTest, CsvEscapesCountTowardEncodedRecordLimit) {
  const std::string data = "a\n\"" + std::string(MAX_RECORD, '\"') + "\"\n";
  Scanner scanner;
  EXPECT_FALSE(scanner.open(opts("escaped-limit.csv", data)));
  EXPECT_NE(std::string::npos, scanner.error.find("resource limit"));
}

TEST_F(FileScanUtilsTest, CsvMultilineCrLfDuplicateHeadersAndRescan) {
  auto o = opts("data.csv",
                "id,id,flag,day,ts,text\r\n1,2,true,2024-02-29,2026-08-01T12:13:14,\"hello,"
                "\nworld\"\r\n3,4,false,,,\"say \"\"hi\"\"\"\r\n");
  Scanner s;
  ASSERT_TRUE(s.open(o)) << s.error;
  ASSERT_EQ(6, s.columns.size());
  EXPECT_EQ("id__2", s.columns[1].name);
  EXPECT_EQ(Type::BOOLEAN, s.columns[2].type);
  EXPECT_EQ(Type::DATE, s.columns[3].type);
  EXPECT_EQ(Type::DATETIME, s.columns[4].type);
  EXPECT_TRUE(s.columns[3].nullable);
  std::vector<Cell> r;
  ASSERT_EQ(1, s.next({5, 0}, r));
  EXPECT_EQ("hello,\nworld", r[0].text);
  EXPECT_EQ("1", r[1].text);
  ASSERT_EQ(1, s.next({5, 3}, r));
  EXPECT_EQ("say \"hi\"", r[0].text);
  EXPECT_TRUE(r[1].null);
  EXPECT_EQ(0, s.next({0}, r));
  ASSERT_TRUE(s.rewind());
  ASSERT_EQ(1, s.next({1}, r));
  EXPECT_EQ("2", r[0].text);
}
TEST_F(FileScanUtilsTest, FullInferenceAndBoundaries) {
  std::string data = "value\n";
  for (int i = 0; i < 5000; ++i) data += "1\n";
  data += "wide\n";
  Scanner s;
  ASSERT_TRUE(s.open(opts("wide.csv", data))) << s.error;
  EXPECT_EQ(Type::VARCHAR, s.columns[0].type);
  EXPECT_EQ(Type::BIGINT, infer("9223372036854775807"));
  EXPECT_EQ(Type::BIGINT, infer("-9223372036854775808"));
  EXPECT_EQ(Type::VARCHAR, infer("9223372036854775808"));
  EXPECT_EQ(Type::VARCHAR, infer("2025-02-29"));
  EXPECT_EQ(Type::VARCHAR, infer("2026-01-01T24:00:00"));
  EXPECT_EQ(Type::DOUBLE, widen(Type::BIGINT, Type::DOUBLE));
  EXPECT_EQ(Type::VARCHAR, infer("0x1.2p3"));
  EXPECT_EQ(Type::VARCHAR, infer(" 1.5"));
  EXPECT_EQ(Type::DOUBLE, infer("-1.2e+3"));
}
TEST_F(FileScanUtilsTest, JsonUnionMissingNullAndUnicode) {
  Scanner s;
  auto o = opts("data.jsonl",
                "{\"id\":1,\"text\":\"\\u4e2d\\u6587\\n\\ud83d\\ude00\"}\n{\"extra\":true,\"id\":2,"
                "\"text\":null}\n",
                "read_ndjson");
  ASSERT_TRUE(s.open(o)) << s.error;
  ASSERT_EQ(3, s.columns.size());
  EXPECT_TRUE(s.columns[2].nullable);
  std::vector<Cell> r;
  ASSERT_EQ(1, s.next({1, 2}, r));
  EXPECT_EQ("中文\n😀", r[0].text);
  EXPECT_TRUE(r[1].null);
  ASSERT_EQ(1, s.next({0, 1, 2}, r));
  EXPECT_EQ("2", r[0].text);
  EXPECT_TRUE(r[1].null);
  EXPECT_EQ("true", r[2].text);
  for (const char* bad : {"{\"x\":{}}\n", "{\"x\":[]}\n", "{\"x\":1} trailing\n",
                          "{\"x\":\"\\ud800\"}\n", "{\"x\":1,\"x\":2}\n"}) {
    Scanner broken;
    EXPECT_FALSE(broken.open(opts("bad.jsonl", bad, "read_json")));
  }
}
TEST_F(FileScanUtilsTest, EmptyStringIsDistinctFromNullAndMalformedCsvFails) {
  Scanner s;
  ASSERT_TRUE(s.open(opts("null.csv", "a,b,c\n,\"\",NULL\n"))) << s.error;
  std::vector<Cell> r;
  ASSERT_EQ(1, s.next({0, 1, 2}, r));
  EXPECT_TRUE(r[0].null);
  EXPECT_FALSE(r[1].null);
  EXPECT_TRUE(r[1].text.empty());
  EXPECT_EQ("NULL", r[2].text);
  for (const char* bad : {"a,b\n1\n", "a\n\"unterminated\n", "a\n\"quoted\"x\n"}) {
    Scanner broken;
    EXPECT_FALSE(broken.open(opts("bad.csv", bad)));
  }
}
TEST_F(FileScanUtilsTest, PathsAndFileChanges) {
  auto o = opts("change.csv", "id\n1\n");
  Scanner s;
  ASSERT_TRUE(s.open(o));
  write("change.csv", "id\n1234\n");
  std::vector<Cell> r;
  EXPECT_EQ(-1, s.next({0}, r));
  o.path = "/etc/passwd";
  Scanner escaped;
  EXPECT_FALSE(escaped.open(o));
  std::string link = dir + "/link.csv";
  paths.push_back(link);
  ASSERT_EQ(0, ::symlink("/etc/passwd", link.c_str()));
  o.path = link;
  EXPECT_FALSE(escaped.open(o));
  o.path = dir + "/*";
  o.format = "glob";
  EXPECT_FALSE(escaped.open(o));
}
TEST_F(FileScanUtilsTest, SnappyAndParquetFooter) {
  std::string out;
  EXPECT_TRUE(snappy(std::string("\x05\x10hello", 7), out, 5));
  EXPECT_EQ("hello", out);
  EXPECT_FALSE(snappy(std::string("\x05\x01\x00", 3), out, 5));
  std::string bytes = fixture("simple.parquet.b64");
  Scanner s;
  auto o = opts("generic.parquet", bytes, "read_parquet");
  ASSERT_TRUE(s.open(o)) << s.error;
  ASSERT_EQ(1, s.columns.size());
  EXPECT_EQ(Type::BIGINT, s.columns[0].type);
  std::vector<Cell> r;
  int64_t count = 0, sum = 0;
  int status;
  while ((status = s.next({0}, r)) > 0) {
    ASSERT_FALSE(r[0].null);
    sum += std::strtoll(r[0].text.c_str(), nullptr, 10);
    ++count;
  }
  EXPECT_EQ(0, status) << s.error;
  EXPECT_EQ(7, count);
  EXPECT_EQ(21, sum);
  ASSERT_TRUE(s.rewind());
  ASSERT_EQ(1, s.next({0}, r));
  EXPECT_EQ("0", r[0].text);
  bytes[0] = 'X';
  Scanner bad;
  EXPECT_FALSE(bad.open(opts("broken.parquet", bytes, "read_parquet")));
}
TEST_F(FileScanUtilsTest, CopyRoundTripAndNoOverwrite) {
  Options o;
  o.root = dir;
  o.path = dir + "/export.csv";
  o.delimiter = '|';
  o.quote = '\'';
  o.escape = '\\';
  o.nullstr = "NULL";
  paths.push_back(o.path);
  {
    CsvWriter writer;
    ASSERT_TRUE(writer.open(o)) << writer.error;
    ASSERT_TRUE(writer.row({Cell("id"), Cell("text"), Cell("empty"), Cell("nil")}));
    ASSERT_TRUE(writer.row({Cell("1"), Cell("a|b\n'\\中文"), Cell(""), Cell()}));
    ASSERT_TRUE(writer.publish()) << writer.error;
  }
  Scanner scanner;
  ASSERT_TRUE(scanner.open(o)) << scanner.error;
  std::vector<Cell> row;
  ASSERT_EQ(1, scanner.next({0, 1, 2, 3}, row));
  EXPECT_EQ("a|b\n'\\中文", row[1].text);
  EXPECT_FALSE(row[2].null);
  EXPECT_TRUE(row[2].text.empty());
  EXPECT_TRUE(row[3].null);
  CsvWriter again;
  EXPECT_FALSE(again.open(o));
  o.path = "/etc/seekdb-copy-outside.csv";
  CsvWriter outside;
  EXPECT_FALSE(outside.open(o));
}
TEST_F(FileScanUtilsTest, CopyAbortAndConcurrentPublication) {
  Options o;
  o.root = dir;
  o.path = dir + "/race.csv";
  paths.push_back(o.path);
  {
    CsvWriter aborted;
    ASSERT_TRUE(aborted.open(o));
    ASSERT_TRUE(aborted.row({Cell("unpublished")}));
  }
  EXPECT_NE(0, ::access(o.path.c_str(), F_OK));
  CsvWriter first, second;
  ASSERT_TRUE(first.open(o));
  ASSERT_TRUE(second.open(o));
  ASSERT_TRUE(first.row({Cell("first")}));
  ASSERT_TRUE(second.row({Cell("second")}));
  ASSERT_TRUE(first.publish());
  EXPECT_FALSE(second.publish());
  std::ifstream file(o.path);
  std::string line;
  std::getline(file, line);
  EXPECT_EQ("\"first\"", line);
}
TEST_F(FileScanUtilsTest, JsonStringTypesAllNullAndSpecialFiles) {
  Scanner s;
  ASSERT_TRUE(s.open(
      opts("typed.jsonl",
           "{\"n\":null,\"id\":\"123\",\"bool\":\"true\"}\n{\"n\":null,\"id\":456,\"bool\":true}\n",
           "read_json")))
      << s.error;
  EXPECT_EQ(Type::NIL, s.columns[0].type);
  EXPECT_TRUE(s.columns[0].nullable);
  EXPECT_EQ(Type::VARCHAR, s.columns[1].type);
  EXPECT_EQ(Type::VARCHAR, s.columns[2].type);
  std::string fifo = dir + "/fifo";
  paths.push_back(fifo);
  ASSERT_EQ(0, ::mkfifo(fifo.c_str(), 0600));
  Options o;
  o.root = dir;
  o.path = fifo;
  Scanner blocked;
  EXPECT_FALSE(blocked.open(o));
}
TEST_F(FileScanUtilsTest, ParquetMultiGroupNullableV2DeltaDictionaryAndProjection) {
  for (const char* name : {"delta.parquet.b64", "dictionary.parquet.b64"}) {
    Scanner s;
    ASSERT_TRUE(s.open(opts(name, fixture(name), "read_parquet"))) << s.error;
    ASSERT_EQ(6, s.columns.size());
    EXPECT_EQ(Type::DATETIME, s.columns[5].type);
    EXPECT_EQ(1000, s.timestamp_divisor(5));
    std::vector<Cell> row;
    for (int i = 0; i < 300; ++i) {
      ASSERT_EQ(1, s.next({1, 0, 2, 3, 4}, row)) << name << " row " << i << ": " << s.error;
      EXPECT_EQ(i % 5 == 0, row[0].null);
      if (i % 5) EXPECT_EQ("中文_" + std::to_string(i) + "_suffix", row[0].text);
      EXPECT_EQ(i % 7 == 0, row[1].null);
      if (i % 7) EXPECT_EQ(std::to_string(i - 150), row[1].text);
      EXPECT_EQ(i % 3 == 0, row[2].null);
      if (i % 3) EXPECT_EQ(i % 2 ? "true" : "false", row[2].text);
      EXPECT_EQ(i % 9 == 0, row[3].null);
      if (i % 9) EXPECT_DOUBLE_EQ(i * 0.25, std::strtod(row[3].text.c_str(), nullptr));
      EXPECT_TRUE(row[4].null);
    }
    EXPECT_EQ(0, s.next({0}, row));
    ASSERT_TRUE(s.rewind());
    ASSERT_EQ(1, s.next({}, row));
    EXPECT_TRUE(row.empty());
  }
}
}  // namespace file_sql
}  // namespace sql
}  // namespace oceanbase

#ifdef FILE_SQL_STANDALONE_TEST_MAIN
int main(int argc, char** argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
#endif
