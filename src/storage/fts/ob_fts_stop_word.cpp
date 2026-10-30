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

#include "object/ob_object.h"
#define USING_LOG_PREFIX STORAGE_FTS

#include "share/rc/ob_server_runtime.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "storage/fts/ob_fts_parser_helper.h"
#include "storage/fts/ob_fts_parser_property.h"

namespace oceanbase
{
namespace storage
{

using ObFTWordMapPair = common::hash::HashMapPair<ObFTWord, int64_t>;

// ObHashMap::get() returns &pair.second. Recover the enclosing pair so the key can
// be refreshed together with the counter. The member offset is taken from a real
// object of the same type, so it matches the layout used by the hash nodes.
static OB_INLINE ObFTWordMapPair *word_pair_of(int64_t *count)
{
  static const ptrdiff_t second_offset = [] {
    ObFTWordMapPair probe;
    return reinterpret_cast<char *>(&probe.second) - reinterpret_cast<char *>(&probe);
  }();
  return reinterpret_cast<ObFTWordMapPair *>(reinterpret_cast<char *>(count) - second_offset);
}

////////////////////////////////////////////////////////////////////////////////
// class ObStopWordChecker
ObStopWordChecker::~ObStopWordChecker()
{
  destroy();
}

int ObStopWordChecker::init()
{
  int ret = OB_SUCCESS;
  
  

  if (inited_) {
    ret = OB_INIT_TWICE;
  } else if (OB_FAIL(stopword_set_.create(DEFAULT_STOPWORD_BUCKET_NUM, "StopWordSet", "StopWordSet"))) {
  } else {
    ObObjMeta stop_meta;
    stop_meta.set_varchar();
    stop_meta.set_collation_type(ObCollationType::CS_TYPE_UTF8MB4_GENERAL_CI);

    stopword_type_.set_meta(stop_meta);
    const int64_t stopword_count = sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]);
    for (int64_t i = 0; OB_SUCC(ret) && i < stopword_count; ++i) {
      ObFTWord stopword(STRLEN(ob_stop_word_list[i]), ob_stop_word_list[i], stopword_type_);
      if (OB_FAIL(stopword_set_.set_refactored(stopword))) {
      }
    }

    if (OB_SUCC(ret)) {
      inited_ = true;
    }
  }
  return ret;
}

void ObStopWordChecker::destroy()
{
  if (inited_) {
    stopword_set_.destroy();
    inited_ = false;
  }
}

int ObStopWordChecker::check_stopword(const ObFTWord &word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  bool decided = false;
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else if (OB_UNLIKELY(word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(check_ascii_stopword(word, is_stopword, decided))) {
  } else if (decided) {
    // fast path answered.
  } else {
    common::ObArenaAllocator allocator(lib::ObMemAttr("ChkStopWord"));
    common::ObString cmp_str;
    // do nothing set out with in if type is the same.
    if (OB_FAIL(common::ObCharset::charset_convert(
                                       allocator,
                                       word.get_word().get_string(),
                                       word.get_collation_type(),
                                       stopword_type_.get_collation_type(),
                                       cmp_str))) {
    } else {
      ObFTWord converted(cmp_str.length(), cmp_str.ptr(), stopword_type_);
      ret = stopword_set_.exist_refactored(converted);
      if (OB_HASH_NOT_EXIST == ret) {
        is_stopword = false;
        ret = OB_SUCCESS;
      } else if (OB_HASH_EXIST == ret) {
        is_stopword = true;
        ret = OB_SUCCESS;
      } else if (OB_SUCC(ret)) {
        ret = OB_ERR_UNEXPECTED;
      } else {
      }
    }
  }
  return ret;
}

// The stop word set is compared with utf8mb4_general_ci (PAD SPACE). For a pure
// 7-bit ASCII word stored in a utf8mb4 collation the conversion is the identity
// and general_ci equality degenerates to: ignore trailing spaces, fold A-Z to a-z,
// compare bytes. Every entry of ob_stop_word_list is lowercase ASCII, so such words
// can be answered without the arena, the charset conversion and the locked hash
// set. Anything else (non-ASCII bytes, other charsets) keeps the original path.
int ObStopWordChecker::check_ascii_stopword(const ObFTWord &word, bool &is_stopword, bool &decided) const
{
  int ret = OB_SUCCESS;
  decided = false;
  const ObCollationType coll = word.get_collation_type();
  if (CHARSET_UTF8MB4 == ObCharset::charset_type_by_coll(coll)) {
    const ObString str = word.get_word().get_string();
    const char *ptr = str.ptr();
    int64_t len = str.length();
    bool is_ascii = true;
    for (int64_t i = 0; is_ascii && i < len; ++i) {
      is_ascii = (0 == (static_cast<unsigned char>(ptr[i]) & 0x80));
    }
    if (is_ascii) {
      while (len > 0 && ' ' == ptr[len - 1]) {
        --len;
      }
      decided = true;
      is_stopword = false;
      if (len > 0 && len < FTS_STOP_WORD_MAX_LENGTH) {
        char lower[FTS_STOP_WORD_MAX_LENGTH];
        for (int64_t i = 0; i < len; ++i) {
          const char c = ptr[i];
          lower[i] = (c >= 'A' && c <= 'Z') ? static_cast<char>(c + ('a' - 'A')) : c;
        }
        const int64_t stopword_count = sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]);
        for (int64_t i = 0; !is_stopword && i < stopword_count; ++i) {
          const char *sw = ob_stop_word_list[i];
          if (sw[0] == lower[0] && static_cast<int64_t>(STRLEN(sw)) == len
              && 0 == MEMCMP(sw, lower, len)) {
            is_stopword = true;
          }
        }
      }
    }
  }
  return ret;
}

////////////////////////////////////////////////////////////////////////////////
// class ObAddWord
ObAddWord::ObAddWord(
    const ObFTParserProperty &property,
    const ObObjMeta &meta,
    const ObAddWordFlag &flag,
    common::ObIAllocator &allocator,
    ObFTWordMap &word_map)
  : word_meta_(meta),
    allocator_(allocator),
    word_map_(word_map),
    min_max_word_cnt_(0),
    non_stopword_cnt_(0),
    stopword_cnt_(0),
    min_token_size_(property.min_token_size_),
    max_token_size_(property.max_token_size_),
    flag_(flag)
{
}

int ObAddWord::process_word(
    const char *word,
    const int64_t word_len,
    const int64_t char_cnt,
    const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  bool is_stopword = false;
  ObFTWord src_word(word_len, word, word_meta_);
  ObFTWord dst_word;
  if (OB_ISNULL(word) || OB_UNLIKELY(0 >= word_len || 0 >= char_cnt || 0 >= word_freq)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (is_min_max_word(char_cnt)) {
    ++min_max_word_cnt_;
  } else if (OB_FAIL(casedown_word(src_word, dst_word))) {
  } else if (OB_FAIL(check_stopword(dst_word, is_stopword))) {
  } else if (OB_UNLIKELY(is_stopword)) {
    ++stopword_cnt_;
  } else if (OB_FAIL(groupby_word(dst_word, word_freq))) {
  } else {
    non_stopword_cnt_ += word_freq;
  }
  return ret;
}

bool ObAddWord::is_min_max_word(const int64_t c_len) const
{
  return flag_.min_max_word() && (c_len < min_token_size_ || c_len > max_token_size_);
}

int ObAddWord::casedown_word(const ObFTWord &src, ObFTWord &dst)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(src.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.casedown()) {
    ObString dst_str;
    if (OB_FAIL(ObCharset::tolower(
                    word_meta_.get_collation_type(),
                    src.get_word().get_string(),
                    dst_str,
                    allocator_))) {
    } else {
      ObFTWord tmp(dst_str.length(), dst_str.ptr(), word_meta_);
      dst = tmp;
    }
  } else {
    dst = src;
  }
  return ret;
}

int ObAddWord::check_stopword(const ObFTWord &ft_word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(ft_word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.stopword()) {
    ObStopWordChecker *stop_word_checker = ObFTParseData::instance().stop_word_checker();
    if (OB_ISNULL(stop_word_checker)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(stop_word_checker->check_stopword(ft_word, is_stopword))) {
    }
  }
  return ret;
}

int ObAddWord::groupby_word(const ObFTWord &word, const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(word.empty() || word_freq <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (!flag_.groupby_word()) {
    if (OB_FAIL(word_map_.set_refactored(word, 1/*word count*/))) {
    }
  } else {
    // The map is private to this segment() call, so an existing entry is updated
    // in place with a single lookup. The original get + set_refactored(overwrite)
    // replaced the node at the same chain position with {latest word, count + freq};
    // writing both the key and the counter of the found pair is exactly that.
    // (Keeping the latest key matters: words equal under the collation, e.g. accent
    // variants in *_general_ci, are reported with the last spelling seen.)
    int64_t *word_count = word_map_.get(word);
    if (OB_NOT_NULL(word_count)) {
      ObFTWordMapPair *pair = word_pair_of(word_count);
      pair->first = word;
      *word_count += word_freq;
    } else if (OB_FAIL(word_map_.set_refactored(word, 1/*word count*/))) {
    }
  }
  return ret;
}

} // end namespace storage
} // end namespace oceanbase
