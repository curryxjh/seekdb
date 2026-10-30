/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#include <gtest/gtest.h>
#include "storage/fts/ik/ob_ik_char_util.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "data_plane/fts/ob_fts_parser_property.h"

namespace oceanbase
{
using namespace common;
namespace storage
{

TEST(FtsHotPathTest, character_classes_and_encoding_boundaries)
{
  using Type = ObFTCharUtil::CharType;
  struct Case { int32_t codepoint; Type type; };
  const Case cases[] = {
    {0, Type::USELESS}, {0x20, Type::USELESS}, {'0', Type::ARABIC_LETTER},
    {'9', Type::ARABIC_LETTER}, {'A', Type::ENGLISH_LETTER}, {'Z', Type::ENGLISH_LETTER},
    {'a', Type::ENGLISH_LETTER}, {'z', Type::ENGLISH_LETTER}, {'[', Type::USELESS},
    {0x4e00, Type::CHINESE}, {0x9fff, Type::CHINESE}, {0x3400, Type::CHINESE},
    {0x4dbf, Type::CHINESE}, {0xf900, Type::CHINESE}, {0xfaff, Type::CHINESE},
    {0x3042, Type::OTHER_CJK}, {0x30a2, Type::OTHER_CJK}, {0xac00, Type::OTHER_CJK},
    {0xff10, Type::ARABIC_LETTER}, {0xff19, Type::ARABIC_LETTER},
    {0xff21, Type::ENGLISH_LETTER}, {0xff3a, Type::ENGLISH_LETTER},
    {0xff41, Type::ENGLISH_LETTER}, {0xff5a, Type::ENGLISH_LETTER},
    {0x1f600, Type::USELESS}, {0x20000, Type::USELESS}, {0x10ffff, Type::USELESS}
  };
  for (auto coll : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI,
                    CS_TYPE_UTF8MB4_UNICODE_CI}) {
    for (const auto &c : cases) {
      char bytes[8] = {};
      int32_t len = 0;
      ASSERT_EQ(OB_SUCCESS, ObCharset::wc_mb(coll, c.codepoint, bytes, sizeof(bytes), len))
          << "collation=" << coll << " codepoint=" << c.codepoint;
      Type actual = Type::USELESS;
      ASSERT_EQ(OB_SUCCESS, ObFTCharUtil::classify_first_char(coll, bytes, len, actual));
      EXPECT_EQ(c.type, actual) << "collation=" << coll << " codepoint=" << c.codepoint;
    }
  }
  Type actual;
  EXPECT_EQ(OB_INVALID_ARGUMENT,
            ObFTCharUtil::classify_first_char(CS_TYPE_UTF8MB4_BIN, nullptr, 1, actual));
  const char truncated[] = {static_cast<char>(0xe4), static_cast<char>(0xb8)};
  EXPECT_NE(OB_SUCCESS,
            ObFTCharUtil::classify_first_char(CS_TYPE_UTF8MB4_BIN, truncated, 2, actual));
  // seekdb registers UTF8MB4 and binary; binary is not an IK charset.
  EXPECT_EQ(OB_NOT_SUPPORTED,
            ObFTCharUtil::classify_first_char(CS_TYPE_BINARY, "A", 1, actual));
}

TEST(FtsHotPathTest, grouped_frequency_preserves_first_occurrence_and_errors)
{
  ObArenaAllocator allocator;
  ObFTWordMap map;
  ASSERT_EQ(OB_SUCCESS, map.create(8, "FtsHotPathTest"));
  ObFTParserProperty property;
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_BIN);
  ObAddWordFlag flags;
  flags.set_groupby_word();
  ObAddWord add(property, meta, flags, allocator, map);
  ASSERT_EQ(OB_SUCCESS, add.process_word("alpha", 5, 5, 7));
  ASSERT_EQ(OB_SUCCESS, add.process_word("alpha", 5, 5, 3));
  ASSERT_EQ(OB_SUCCESS, add.process_word("beta", 4, 4, 1));
  int64_t count = 0;
  ASSERT_EQ(OB_SUCCESS, map.get_refactored(ObFTWord(5, "alpha", meta), count));
  EXPECT_EQ(4, count); // first occurrence is one, following occurrences add their frequency
  EXPECT_EQ(11, add.get_add_word_count());
  EXPECT_EQ(2, map.size());
  EXPECT_EQ(OB_INVALID_ARGUMENT, add.process_word("alpha", 5, 5, 0));
  map.destroy();
  EXPECT_EQ(OB_NOT_INIT, add.process_word("alpha", 5, 5, 1));
}

TEST(FtsHotPathTest, collation_equivalent_words_retain_latest_spelling)
{
  ObArenaAllocator allocator;
  ObFTWordMap map;
  ASSERT_EQ(OB_SUCCESS, map.create(8, "FtsHotPathTest"));
  ObFTParserProperty property;
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
  ObAddWordFlag flags;
  flags.set_groupby_word();
  ObAddWord add(property, meta, flags, allocator, map);
  ASSERT_EQ(OB_SUCCESS, add.process_word("Alpha", 5, 5, 1));
  ASSERT_EQ(OB_SUCCESS, add.process_word("ALPHA", 5, 5, 2));
  ASSERT_EQ(1, map.size());
  const auto it = map.begin();
  EXPECT_EQ(3, it->second);
  EXPECT_EQ(ObString::make_string("ALPHA"), it->first.get_word().get_string());
  map.destroy();
}

} // namespace storage
} // namespace oceanbase
