/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#include <gtest/gtest.h>
#include <string>
#include <vector>
#include "share/text_analysis/ob_token_stream.h"

namespace oceanbase
{
using namespace common;
namespace share
{
// Supply entire tokens so charset conversion is tested independently of the
// whitespace tokenizer, including tokens that outgrow the reusable buffer.
class NormalizerInput : public ObITokenStream
{
public:
  std::vector<std::string> tokens_;
  size_t pos_ = 0;
  int get_next(ObDatum &token, int64_t &freq) override
  {
    if (pos_ == tokens_.size()) { return OB_ITER_END; }
    const auto &s = tokens_[pos_++];
    token.set_string(ObString(s.size(), s.data()));
    freq = 3;
    return OB_SUCCESS;
  }
  void reset() override { pos_ = 0; }
  void reuse() override { reset(); }
  TO_STRING_KV(K_(pos));
};

TEST(EnglishNormalizerTest, charset_conversion_buffer_growth_and_lifecycle)
{
  for (auto coll : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI,
                    CS_TYPE_UTF8MB4_UNICODE_CI, CS_TYPE_BINARY}) {
    const auto *cs = ObCharset::get_charset(coll);
    NormalizerInput input;
    input.tokens_ = {"ABC", "aBc123", std::string(65, 'A'), "Z",
                     std::string(1024, 'B'), "hello"};
    if (coll != CS_TYPE_BINARY) {
      input.tokens_.push_back("I\xc4\xb0" "STANBUL");
    }
    ObBasicEnglishNormalizer norm;
    ObDatum out;
    int64_t freq = 0;
    EXPECT_EQ(OB_NOT_INIT, norm.get_next(out, freq));
    ASSERT_EQ(OB_SUCCESS, norm.init(cs, input));
    for (int pass = 0; pass < 3; ++pass) {
      input.reset();
      for (const auto &s : input.tokens_) {
        ObArenaAllocator reference_alloc;
        ObString expected;
        ASSERT_EQ(OB_SUCCESS, ObCharset::tolower(cs, ObString(s.size(), s.data()),
                                                expected, reference_alloc));
        ASSERT_EQ(OB_SUCCESS, norm.get_next(out, freq));
        EXPECT_EQ(expected, out.get_string());
        EXPECT_EQ(3, freq);
      }
      EXPECT_EQ(OB_ITER_END, norm.get_next(out, freq));
      if (pass == 0) {
        norm.reuse(); // reuse retains the initialized stream
      } else {
        norm.reset();
        ASSERT_EQ(OB_SUCCESS, norm.init(cs, input));
      }
    }
  }
}

TEST(EnglishNormalizerTest, trims_punctuation_and_skips_non_alphanumeric_tokens)
{
  NormalizerInput input;
  input.tokens_ = {"!!!", "...Hello?!", "__42--", "\t", "WorLD"};
  ObBasicEnglishNormalizer norm;
  ASSERT_EQ(OB_SUCCESS, norm.init(ObCharset::get_charset(CS_TYPE_UTF8MB4_BIN), input));
  for (const char *s : {"hello", "42", "world"}) {
    ObDatum out;
    int64_t freq = 0;
    ASSERT_EQ(OB_SUCCESS, norm.get_next(out, freq));
    EXPECT_EQ(ObString::make_string(s), out.get_string());
  }
  ObDatum out;
  int64_t freq;
  EXPECT_EQ(OB_ITER_END, norm.get_next(out, freq));
}

} // namespace share
} // namespace oceanbase
