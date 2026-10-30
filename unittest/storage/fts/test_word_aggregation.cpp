/*
 * Copyright (c) 2025 OceanBase.
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
#include <atomic>
#include <thread>
#include <vector>
#include "lib/allocator/ob_allocator.h"
#include "lib/charset/ob_charset.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "data_plane/fts/ob_fts_parser_property.h"
#include "storage/fts/ik/ob_ik_char_util.h"

namespace oceanbase
{
namespace storage
{
using namespace common;

class TestWordAggregation : public ::testing::Test
{
protected:
  void SetUp() override
  {
    meta_.set_varchar();
    meta_.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
    flags_.set_groupby_word();
    ASSERT_EQ(OB_SUCCESS, words_.create(2, "FTSUnit", "FTSUnit"));
  }
  void TearDown() override { words_.destroy(); }
  int64_t count(const char *text)
  {
    int64_t value = -1;
    EXPECT_EQ(OB_SUCCESS, words_.get_refactored(ObFTWord(STRLEN(text), text, meta_), value));
    return value;
  }
  ObArenaAllocator alloc_;
  ObObjMeta meta_;
  ObFTParserProperty props_;
  ObAddWordFlag flags_;
  ObFTWordMap words_;
};

TEST_F(TestWordAggregation, preserves_first_count_and_repeated_frequency)
{
  ObAddWord add(props_, meta_, flags_, alloc_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("alpha", 5, 5, 7));
  EXPECT_EQ(1, count("alpha"));
  ASSERT_EQ(OB_SUCCESS, add.process_word("ALPHA", 5, 5, 3));
  EXPECT_EQ(4, count("alpha"));
  EXPECT_EQ(ObString::make_string("ALPHA"), words_.begin()->first.get_word().get_string());
  EXPECT_EQ(10, add.get_add_word_count());
  EXPECT_EQ(1, words_.size());
}

TEST_F(TestWordAggregation, collisions_reuse_and_invalid_input)
{
  ObAddWord add(props_, meta_, flags_, alloc_, words_);
  const char *terms[] = {"alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                         "数据库", "日本語", "café", "zulu"};
  for (int round = 0; round < 100; ++round) {
    for (const char *term : terms) {
      ASSERT_EQ(OB_SUCCESS, add.process_word(term, STRLEN(term), 3, 2));
    }
  }
  for (const char *term : terms) { EXPECT_EQ(199, count(term)); }
  EXPECT_EQ(OB_INVALID_ARGUMENT, add.process_word(nullptr, 1, 1, 1));
  EXPECT_EQ(OB_INVALID_ARGUMENT, add.process_word("", 0, 0, 1));
  EXPECT_EQ(OB_INVALID_ARGUMENT, add.process_word("alpha", 5, 5, 0));
  EXPECT_EQ(199, count("alpha"));
  ASSERT_EQ(OB_SUCCESS, words_.reuse());
  ASSERT_EQ(OB_SUCCESS, add.process_word("alpha", 5, 5, 1));
  EXPECT_EQ(1, count("alpha"));
}

TEST_F(TestWordAggregation, no_grouping_keeps_duplicate_error)
{
  flags_.clear_groupby_word();
  ObAddWord add(props_, meta_, flags_, alloc_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("alpha", 5, 5, 1));
  EXPECT_EQ(OB_HASH_EXIST, add.process_word("alpha", 5, 5, 1));
  EXPECT_EQ(1, count("alpha"));
  EXPECT_EQ(1, add.get_add_word_count());
}

TEST_F(TestWordAggregation, filtering_and_case_folding)
{
  flags_.set_min_max_word();
  flags_.set_casedown();
  props_.min_token_size_ = 3;
  props_.max_token_size_ = 84;
  ObAddWord add(props_, meta_, flags_, alloc_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("ab", 2, 2, 1));
  EXPECT_EQ(0, words_.size());
  ASSERT_EQ(OB_SUCCESS, add.process_word("ABC", 3, 3, 1));
  ASSERT_EQ(OB_SUCCESS, add.process_word("abc", 3, 3, 1));
  EXPECT_EQ(2, count("abc"));
  EXPECT_EQ(2, add.get_add_word_count());
}

TEST_F(TestWordAggregation, propagates_uninitialized_map_error)
{
  words_.destroy();
  ObAddWord add(props_, meta_, flags_, alloc_, words_);
  EXPECT_EQ(OB_NOT_INIT, add.process_word("alpha", 5, 5, 1));
  EXPECT_EQ(0, add.get_add_word_count());
}
TEST(StopWords, charset_conversion_and_dictionary_collation)
{
  ObStopWordChecker checker;
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_BIN);
  bool found = false;
  EXPECT_EQ(OB_NOT_INIT, checker.check_stopword(ObFTWord(3, "THE", meta), found));
  ASSERT_EQ(OB_SUCCESS, checker.init());
  const ObCollationType collations[] = {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI,
                                        CS_TYPE_UTF8MB4_UNICODE_CI,
                                        CS_TYPE_BINARY};
  for (const auto collation : collations) {
    SCOPED_TRACE(static_cast<int>(collation));
    meta.set_collation_type(collation);
    ASSERT_EQ(OB_SUCCESS, checker.check_stopword(ObFTWord(3, "THE", meta), found));
    EXPECT_TRUE(found);
    ASSERT_EQ(OB_SUCCESS, checker.check_stopword(ObFTWord(8, "database", meta), found));
    EXPECT_FALSE(found);
  }
  const char utf16_be[] = {0, 'T', 0, 'H', 0, 'E'};
  const char utf16_le[] = {'T', 0, 'H', 0, 'E', 0};
  meta.set_collation_type(CS_TYPE_UTF16_BIN);
  EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(6, utf16_be, meta), found));
  meta.set_collation_type(CS_TYPE_UTF16LE_BIN);
  EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(6, utf16_le, meta), found));
  meta.set_collation_type(CS_TYPE_INVALID);
  EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(3, "the", meta), found));
  // The upstream charset registry does not accept this collation here.
  meta.set_collation_type(CS_TYPE_LATIN1_SWEDISH_CI);
  EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(3, "the", meta), found));
  EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(), found));
}

TEST(StopWords, concurrent_readers_preserve_collation)
{
  ObStopWordChecker checker;
  ASSERT_EQ(OB_SUCCESS, checker.init());
  std::atomic<int> failures{0};
  std::vector<std::thread> readers;
  for (int i = 0; i < 8; ++i) {
    readers.emplace_back([&]() {
      ObObjMeta meta;
      meta.set_varchar();
      meta.set_collation_type(CS_TYPE_UTF8MB4_BIN);
      const char *terms[] = {"the", "THE", "thé", "with", "database", "数据库", "theory"};
      for (int round = 0; round < 2000; ++round) {
        for (int j = 0; j < 7; ++j) {
          bool found = false;
          if (OB_SUCCESS != checker.check_stopword(ObFTWord(STRLEN(terms[j]), terms[j], meta), found)
              || found != (j < 4)) {
            ++failures;
          }
        }
      }
    });
  }
  for (auto &reader : readers) { reader.join(); }
  EXPECT_EQ(0, failures.load());
}

TEST(IKCharacters, classification_boundaries_and_invalid_utf8)
{
  using Type = ObFTCharUtil::CharType;
  const struct { const char *text; Type type; } cases[] = {
      {"A", Type::ENGLISH_LETTER}, {"z", Type::ENGLISH_LETTER},
      {"Ａ", Type::ENGLISH_LETTER}, {"ｚ", Type::ENGLISH_LETTER},
      {"0", Type::ARABIC_LETTER}, {"９", Type::ARABIC_LETTER},
      {"中", Type::CHINESE}, {"㐀", Type::CHINESE}, {"豈", Type::CHINESE},
      {"あ", Type::OTHER_CJK}, {"カ", Type::OTHER_CJK}, {"한", Type::OTHER_CJK},
      {"＠", Type::OTHER_CJK}, {"é", Type::USELESS}, {"😀", Type::USELESS},
      {"𠀀", Type::USELESS}, {" ", Type::USELESS}, {"@", Type::USELESS}};
  for (const auto &item : cases) {
    Type actual = Type::USELESS;
    SCOPED_TRACE(item.text);
    ASSERT_EQ(OB_SUCCESS, ObFTCharUtil::classify_first_char(
        CS_TYPE_UTF8MB4_BIN, item.text, STRLEN(item.text), actual));
    EXPECT_EQ(item.type, actual);
  }
  Type type;
  const char truncated[] = {static_cast<char>(0xe4), static_cast<char>(0xb8)};
  EXPECT_EQ(OB_UNEXPECT_INTERNAL_ERROR, ObFTCharUtil::classify_first_char(
      CS_TYPE_UTF8MB4_BIN, truncated, sizeof(truncated), type));
  EXPECT_EQ(OB_INVALID_ARGUMENT, ObFTCharUtil::classify_first_char(
      CS_TYPE_UTF8MB4_BIN, nullptr, 1, type));
}

TEST(IKCharacters, chinese_number_membership)
{
  const std::u32string expected = U"一二两三四五六七八九十零壹贰叁肆伍陆柒捌玖拾百千万亿佰仟萬億兆卅廿";
  for (ob_wc_t code = 0; code <= 0xffff; ++code) {
    ASSERT_EQ(expected.find(code) != std::u32string::npos,
              ObUnicodeBlockUtils::is_unicode_cn_number(code)) << code;
  }
  EXPECT_FALSE(ObUnicodeBlockUtils::is_unicode_cn_number(0x20000));
}
} // namespace storage
} // namespace oceanbase

int main(int argc, char **argv)
{
  ::testing::InitGoogleTest(&argc, argv);
  if (oceanbase::common::ObCharset::init_charset() != oceanbase::common::OB_SUCCESS) { return 1; }
  return RUN_ALL_TESTS();
}
