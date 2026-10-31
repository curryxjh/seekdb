/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#include <gtest/gtest.h>
#include "storage/blocksstable/encoding/ob_string_diff_decoder.h"
#include "storage/blocksstable/encoding/ob_bit_stream.h"
#include "lib/allocator/page_arena.h"
#include <string>
#include <vector>

namespace oceanbase
{
namespace blocksstable
{
using namespace common;

TEST(StringDiffBatchTest, gathered_rows_match_scalar_decoding_and_source_bytes)
{
  constexpr int rows = 31;
  for (int length : {1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 17, 24, 32, 127, 128, 129, 400}) {
    for (bool hex : {false, true}) {
      for (bool nullable : {false, true}) {
        std::vector<ObStringDiffHeader::DiffDesc> descs;
        int diff_bytes = 0;
        int common_bytes = 0;
        for (int pos = 0; pos < length;) {
          ObStringDiffHeader::DiffDesc desc;
          desc.diff_ = (descs.size() % 2) == 0;
          desc.count_ = std::min(length - pos, desc.diff_ ? 8 : 5);
          pos += desc.count_;
          (desc.diff_ ? diff_bytes : common_bytes) += desc.count_;
          descs.push_back(desc);
        }
        const int row_bytes = hex ? (diff_bytes + 1) / 2 : diff_bytes;
        const int meta_bytes = sizeof(ObStringDiffHeader) + descs.size() + (hex ? 16 : 0) + common_bytes;
        const int null_bytes = nullable ? (rows + 7) / 8 : 0;
        std::vector<char> encoded(meta_bytes + null_bytes + rows * row_bytes, 0);
        auto *header = new (encoded.data()) ObStringDiffHeader();
        header->string_size_ = length;
        header->length_ = row_bytes;
        header->diff_desc_cnt_ = descs.size();
        MEMCPY(header->diff_descs_, descs.data(), descs.size());
        header->hex_char_array_size_ = hex ? 16 : 0;
        if (hex) { MEMCPY(header->hex_char_array(), "0123456789abcdef", 16); }
        MEMSET(header->common_data(), 'X', common_bytes);
        auto *column_data = reinterpret_cast<unsigned char *>(encoded.data() + meta_bytes);
        ObBitStream nulls(column_data, null_bytes);
        std::vector<std::string> expected(rows, std::string(length, 'X'));
        for (int row = 0; row < rows; ++row) {
          if (nullable && row % 7 == 0) { ASSERT_EQ(OB_SUCCESS, nulls.set(row, 1, int64_t(STORED_NULL))); }
          auto *data = column_data + null_bytes + row * row_bytes;
          int pos = 0;
          int packed_pos = 0;
          for (const auto &desc : descs) {
            for (int j = 0; j < desc.count_; ++j, ++pos) {
              if (desc.diff_) {
                const unsigned char value = hex ? (row + pos) % 16 : (row * 37 + pos * 13) % 256;
                expected[row][pos] = hex ? "0123456789abcdef"[value] : value;
                if (hex) {
                  data[packed_pos / 2] |= value << (packed_pos % 2 == 0 ? 4 : 0);
                } else {
                  data[packed_pos] = value;
                }
                ++packed_pos;
              }
            }
          }
        }
        ObMicroBlockHeader micro;
        micro.row_count_ = rows;
        micro.extend_value_bit_ = 1;
        ObColumnHeader column;
        column.offset_ = 0;
        column.length_ = meta_bytes;
        ObArenaAllocator allocator;
        ObColumnDecoderCtx ctx;
        ctx.reset();
        ctx.micro_block_header_ = &micro;
        ctx.col_header_ = &column;
        ctx.allocator_ = &allocator;
        ctx.cache_attributes_[ObColumnHeader::FIX_LENGTH] = true;
        ctx.cache_attributes_[ObColumnHeader::HAS_EXTEND_VALUE] = nullable;
        ObStringDiffDecoder decoder;
        ASSERT_EQ(OB_SUCCESS, decoder.init(micro, column, encoded.data()));
        for (int count : {1, 7, 31, 63}) {
          std::vector<int32_t> ids(count);
          std::vector<ObDatum> batch(count);
          for (int i = 0; i < count; ++i) { ids[i] = (i * 17 + 5) % rows; }
          ASSERT_EQ(OB_SUCCESS, decoder.batch_decode(ctx, nullptr, ids.data(), nullptr, count, batch.data()));
          for (int i = 0; i < count; ++i) {
            ObDatum scalar;
            ObBitStream unused;
            ASSERT_EQ(OB_SUCCESS, decoder.decode(ctx, scalar, ids[i], unused, encoded.data(), encoded.size()));
            ASSERT_EQ(scalar.is_null(), batch[i].is_null());
            if (!scalar.is_null()) {
              EXPECT_EQ(expected[ids[i]], std::string(batch[i].ptr_, batch[i].len_));
              EXPECT_EQ(scalar.get_string(), batch[i].get_string());
            }
          }
        }
      }
    }
  }
}
} // namespace blocksstable
} // namespace oceanbase
