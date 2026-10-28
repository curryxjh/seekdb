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
#include "sql/parser/ob_parser.h"

namespace oceanbase { namespace sql {
using namespace common;
class FileSqlParserTest : public testing::Test {
protected:
  int parse(const char *text) {
    ObParser parser(allocator_, SMO_DEFAULT);
    return parser.parse(ObString::make_string(text), result_);
  }
  ParseNode *find(ParseNode *node, ObItemType type) {
    if (!node || node->type_ == type) return node;
    for (int i = 0; i < node->num_child_; ++i) { ParseNode *n = find(node->children_[i], type); if (n) return n; }
    return nullptr;
  }
  ObArenaAllocator allocator_;
  ParseResult result_ = {};
};
TEST_F(FileSqlParserTest, ReadersAliasesOptionsAndJoin) {
  for (const char *sql : {
      "SELECT * FROM read_csv('/data/a.csv')",
      "SELECT f.id FROM READ_CSV('/data/a.csv', header=true, sample_size=-1) AS f",
      "SELECT * FROM read_json('/data/a.jsonl', format='newline_delimited') f",
      "SELECT * FROM read_ndjson('/data/a.jsonl')",
      "SELECT * FROM read_parquet('/data/a.parquet') p",
      "SELECT file FROM glob('/data/*.csv') ORDER BY file",
      "SELECT a.id,b.name FROM read_csv('/data/a.csv') a LEFT JOIN read_json('/data/b.jsonl') b ON a.id=b.id"}) {
    ASSERT_EQ(OB_SUCCESS, parse(sql)) << sql;
    ASSERT_NE(nullptr, find(result_.result_tree_, T_TABLE_COLLECTION_EXPRESSION));
  }
}
TEST_F(FileSqlParserTest, DescribeDiffersFromExplain) {
  ASSERT_EQ(OB_SUCCESS, parse("DESCRIBE SELECT * FROM read_csv('/data/a.csv')"));
  ParseNode *n = find(result_.result_tree_, T_EXPLAIN); ASSERT_NE(nullptr, n); ASSERT_NE(nullptr,n->children_[0]); EXPECT_EQ(T_INT,n->children_[0]->type_);
  ASSERT_EQ(OB_SUCCESS, parse("EXPLAIN SELECT * FROM read_csv('/data/a.csv')"));
  n = find(result_.result_tree_, T_EXPLAIN); ASSERT_NE(nullptr, n); EXPECT_EQ(nullptr, n->children_[0]);
}
TEST_F(FileSqlParserTest, CopyUsesNativeSelectInto) {
  for (const char *sql : {
      "COPY docs TO '/data/docs.csv'",
      "COPY test.docs TO '/data/docs.csv' (FORMAT csv, HEADER, NULLSTR 'NULL')",
      "COPY (SELECT id+1 AS id FROM docs ORDER BY id LIMIT 3) TO '/data/a.csv' (HEADER false, DELIMITER '|', QUOTE '\"', ESCAPE '\"')",
      "COPY (SELECT * FROM read_csv('/data/a.csv')) TO '/data/b.csv' (FORMAT='csv', HEADER=true)"}) {
    ASSERT_EQ(OB_SUCCESS, parse(sql)) << sql;
    ParseNode *n = find(result_.result_tree_, T_INTO_OUTFILE); ASSERT_NE(nullptr, n); EXPECT_EQ(2, n->num_child_);
    ASSERT_NE(nullptr, find(result_.result_tree_, T_SELECT));
  }
  EXPECT_NE(OB_SUCCESS, parse("COPY docs TO"));
  EXPECT_NE(OB_SUCCESS, parse("COPY (SELECT 1 INTO OUTFILE '/data/a') TO '/data/b'"));
  EXPECT_NE(OB_SUCCESS, parse("SELECT * FROM read_csv('/data/a.csv',)"));
}
}} // namespace oceanbase::sql
