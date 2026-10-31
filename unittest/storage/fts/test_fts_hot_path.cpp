/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#include <gtest/gtest.h>
#include "storage/fts/ik/ob_ik_char_util.h"
#include "storage/fts/ik/ob_ik_arbitrator.h"
#include "storage/fts/ik/ob_ik_processor.h"
#include "storage/fts/dict/ob_ft_dat_dict.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "data_plane/fts/ob_fts_parser_property.h"
#include "data_plane/fts/ob_fts_parser_helper.h"
#include "storage/retrieval/ob_sparse_daat_iter.h"
#include "storage/retrieval/ob_sparse_retrieval_util.h"
#include <string>
#include <vector>

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

TEST(FtsHotPathTest, arbitration_keeps_overlapping_and_adjacent_chains)
{
  ObArenaAllocator allocator;
  const char *text = "abcdefg hij";
  TokenizeContext ctx(CS_TYPE_UTF8MB4_BIN, allocator, text, 11, false);
  ASSERT_EQ(OB_SUCCESS, ctx.init());
  for (const auto &range : std::vector<std::pair<int64_t, int64_t>>{
           {0, 4}, {1, 2}, {4, 1}, {8, 3}}) {
    ASSERT_EQ(OB_SUCCESS, ctx.add_token(text, range.first, range.second,
                                      range.second, ObIKTokenType::IK_ENGLISH_TOKEN));
  }
  ObIKArbitrator arbitrator;
  ASSERT_EQ(OB_SUCCESS, arbitrator.process(ctx));
  ASSERT_EQ(OB_SUCCESS, arbitrator.output_result(ctx));
  const int64_t offsets[] = {0, 1, 4, 8};
  const int64_t lengths[] = {4, 2, 1, 3};
  int64_t i = 0;
  for (const auto &token : ctx.result_list()) {
    ASSERT_LT(i, 4);
    EXPECT_EQ(offsets[i], token.offset_);
    EXPECT_EQ(lengths[i], token.length_);
    ++i;
  }
  EXPECT_EQ(4, i);
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

TEST(FtsHotPathTest, dictionary_code_lookup_preserves_bytes_and_lengths)
{
  ObArenaAllocator allocator;
  const std::vector<std::string> words = {
      "a", "ab", "abc", "abcd", "A", "0", " ", std::string("a\0", 2),
      std::string("a\0b", 3), "中", "文", "😀", "𠀀"};
  void *buffer = allocator.alloc(ObArrayHashMap::estimate_size(words.size()));
  ASSERT_NE(nullptr, buffer);
  auto *map = new (buffer) ObArrayHashMap();
  ASSERT_EQ(OB_SUCCESS, map->init(words.size()));
  for (size_t i = 0; i < words.size(); ++i) {
    ASSERT_EQ(OB_SUCCESS, map->insert(ObString(words[i].size(), words[i].data()), i + 1));
  }
  for (size_t i = 0; i < words.size(); ++i) {
    ObFTSingleWord word;
    ASSERT_EQ(OB_SUCCESS, word.set_word(words[i].data(), words[i].size()));
    ObFTWordCode code = -1;
    ASSERT_EQ(OB_SUCCESS, map->find(word, code));
    EXPECT_EQ(i + 1, code);
  }
  for (const char *missing : {"z", "abce", "国", "😁"}) {
    ObFTSingleWord word;
    ASSERT_EQ(OB_SUCCESS, word.set_word(missing, strlen(missing)));
    ObFTWordCode code = -1;
    EXPECT_EQ(OB_ENTRY_NOT_EXIST, map->find(word, code));
  }
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

TEST(FtsHotPathTest, binary_id_comparison_matches_datum_comparator)
{
  ObArenaAllocator allocator;
  std::vector<std::string> bytes = {"", "a", "a ", "a  ", "b", std::string("a\0", 2),
                                  std::string("\0\xff", 2), std::string(16, '\xff')};
  // Exercise each differing byte of the fixed-size comparison, including
  // high-bit bytes, embedded NULs and equal prefixes of different lengths.
  for (int i = 0; i < 16; ++i) {
    std::string id(16, '\0');
    id[i] = static_cast<char>(0x80);
    bytes.push_back(id);
    bytes.push_back(id + " ");
  }
  std::vector<ObDatum> datums(bytes.size() + 1);
  datums[0].set_null();
  for (size_t i = 0; i < bytes.size(); ++i) {
    datums[i + 1].set_string(bytes[i].data(), bytes[i].size());
  }
  ObFixedArray<const ObDatum *, ObIAllocator> ids(&allocator);
  ASSERT_EQ(OB_SUCCESS, ids.init(datums.size()));
  for (const auto &datum : datums) { ASSERT_EQ(OB_SUCCESS, ids.push_back(&datum)); }
  for (auto coll : {CS_TYPE_BINARY, CS_TYPE_UTF8MB4_BIN}) {
    sql::ObDatumMeta meta;
    meta.type_ = ObVarcharType;
    meta.cs_type_ = coll;
    ObSRMergeCmp cmp;
    ASSERT_EQ(OB_SUCCESS, cmp.init(meta, &ids));
    ObObjMeta obj_meta;
    obj_meta.set_varchar();
    obj_meta.set_collation_type(coll);
    ObDomainIdCmp domain_cmp;
    ASSERT_EQ(OB_SUCCESS, domain_cmp.init(obj_meta));
    const auto ref_cmp = ObDatumFuncs::get_basic_func(meta.type_, coll)->null_first_cmp_;
    for (size_t i = 0; i < datums.size(); ++i) {
      for (size_t j = 0; j < datums.size(); ++j) {
        ObSRMergeItem left, right;
        left.iter_idx_ = i;
        right.iter_idx_ = j;
        int expected = 0;
        int64_t actual = 0;
        ASSERT_EQ(OB_SUCCESS, ref_cmp(datums[i], datums[j], expected, nullptr));
        ASSERT_EQ(OB_SUCCESS, cmp.cmp(left, right, actual));
        EXPECT_EQ((expected > 0) - (expected < 0), (actual > 0) - (actual < 0));
        int domain_actual = 0;
        ASSERT_EQ(OB_SUCCESS, domain_cmp.compare(datums[i], datums[j], domain_actual));
        EXPECT_EQ(expected, domain_actual);
      }
    }
    ObObj minimum_obj, maximum_obj;
    minimum_obj.set_min_value();
    maximum_obj.set_max_value();
    ObDatum minimum, maximum;
    minimum.ptr_ = reinterpret_cast<const char *>(&minimum_obj);
    maximum.ptr_ = reinterpret_cast<const char *>(&maximum_obj);
    minimum.set_ext();
    maximum.set_ext();
    int actual = 0;
    ASSERT_EQ(OB_SUCCESS, domain_cmp.compare(minimum, datums[1], actual));
    EXPECT_LT(actual, 0);
    ASSERT_EQ(OB_SUCCESS, domain_cmp.compare(maximum, datums[1], actual));
    EXPECT_GT(actual, 0);
    ASSERT_EQ(OB_SUCCESS, domain_cmp.compare(minimum, maximum, actual));
    EXPECT_LT(actual, 0);
  }
}

TEST(FtsHotPathTest, parsed_properties_match_json_roundtrip)
{
  for (const char *name : {"ik", "beng", "space", "ngram", "ngram2"}) {
    ObFTParser parser;
    ASSERT_EQ(OB_SUCCESS, parser.init(ObString::make_string(name)));
    char internal_name[OB_FT_PARSER_NAME_LENGTH];
    ASSERT_EQ(OB_SUCCESS, parser.serialize_to_str(internal_name, sizeof(internal_name)));
    ObFTParserJsonProps props;
    ASSERT_EQ(OB_SUCCESS, props.init());
    ASSERT_EQ(OB_SUCCESS, props.rebuild_props_for_ddl(ObString::make_string(internal_name),
                                                    CS_TYPE_UTF8MB4_BIN, false));
    ObArenaAllocator allocator;
    ObString json;
    ASSERT_EQ(OB_SUCCESS, props.to_format_json(allocator, json));
    ObFTParserProperty roundtrip, direct;
    ASSERT_EQ(OB_SUCCESS, roundtrip.parse_for_parser_helper(parser, json));
    ASSERT_EQ(OB_SUCCESS, direct.parse_for_parser_helper(parser, props));
    EXPECT_TRUE(roundtrip.is_equal(direct));
  }
}

TEST(FtsHotPathTest, ik_case_folding_matches_charset_conversion)
{
  for (auto coll : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI,
                    CS_TYPE_UTF8MB4_UNICODE_CI, CS_TYPE_BINARY}) {
    for (const std::string &input : std::vector<std::string>{
        "unchanged", "ABC", "数据库", "中文AbC", "\xc4\xb0STANBUL", "\xce\xa3\xcf\x82",
        std::string(256, 'a'), std::string(257, 'B')}) {
      ObArenaAllocator allocator, reference_allocator;
      ObObjMeta meta;
      meta.set_varchar();
      meta.set_collation_type(coll);
      ObString reference;
      ASSERT_EQ(OB_SUCCESS, ObCharset::tolower(coll,
          ObString(input.size(), input.data()), reference, reference_allocator));
      ObFTWordMap words;
      ASSERT_EQ(OB_SUCCESS, words.create(17, ObMemAttr("FTCaseTest")));
      ObFTParserProperty property;
      ObAddWordFlag flags;
      flags.set_casedown();
      flags.set_groupby_word();
      ObAddWord add(property, meta, flags, allocator, words);
      ASSERT_EQ(OB_SUCCESS, add.process_word(input.data(), input.size(), 1, 1));
      ASSERT_EQ(1, words.size());
      EXPECT_EQ(reference, words.begin()->first.get_word().get_string());
      EXPECT_EQ(1, words.begin()->second);
      words.destroy();
    }
  }
}

} // namespace storage
} // namespace oceanbase
