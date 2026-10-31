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

#ifndef OB_SPARSE_RETRIEVAL_UTIL_H_
#define OB_SPARSE_RETRIEVAL_UTIL_H_

#include "common/datum/ob_datum.h"
#include "share/datum/ob_datum_funcs.h"

namespace oceanbase
{
namespace storage
{

inline int compare_binary_doc_ids(const ObDatum &lhs, const ObDatum &rhs, int &cmp_ret,
                                  const ObDatumAccessContext *)
{
  if (lhs.is_null() || rhs.is_null()) {
    cmp_ret = lhs.is_null() ? (rhs.is_null() ? 0 : -1) : 1;
  } else {
    const int64_t common_len = MIN(lhs.len_, rhs.len_);
    // Fixed-size document IDs are common. A constant length lets the compiler
    // compare them inline rather than calling the general memcmp routine.
    int cmp = 0;
    if (common_len == 16) {
      for (int64_t offset = 0; cmp == 0 && offset < 16; offset += 8) {
        uint64_t left = 0, right = 0;
        MEMCPY(&left, lhs.ptr_ + offset, 8);
        MEMCPY(&right, rhs.ptr_ + offset, 8);
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        left = __builtin_bswap64(left);
        right = __builtin_bswap64(right);
#endif
        cmp = (left > right) - (left < right);
      }
    } else if (common_len > 0) {
      cmp = MEMCMP(lhs.ptr_, rhs.ptr_, common_len);
    }
    cmp_ret = cmp != 0 ? (cmp > 0 ? 1 : -1) : (lhs.len_ > rhs.len_) - (lhs.len_ < rhs.len_);
  }
  return OB_SUCCESS;
}

class ObDomainIdCmp
{
public:
  ObDomainIdCmp() : cmp_func_(nullptr) {}
  ~ObDomainIdCmp() {}
  inline int init(const ObObjMeta &obj_meta);
  inline int compare(const ObDatum &lhs, const ObDatum &rhs, int &cmp_ret) const;
  inline void reset() { cmp_func_ = nullptr; }
private:
  common::ObDatumCmpFuncType cmp_func_;
  DISALLOW_COPY_AND_ASSIGN(ObDomainIdCmp);
};

int ObDomainIdCmp::init(const ObObjMeta &obj_meta)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!obj_meta.is_valid())) {
    ret = OB_INVALID_ARGUMENT;
    STORAGE_LOG(WARN,"invalid argument", K(ret), K(obj_meta));
  } else {
    common::ObDatumBasicFuncs *id_basic_funcs = ObDatumFuncs::get_basic_func(
      obj_meta.get_type(), obj_meta.get_collation_type());
    if (OB_ISNULL(id_basic_funcs)) {
      ret = OB_ERR_UNEXPECTED;
      STORAGE_LOG(WARN,"failed to get basic functions", K(ret), K(obj_meta));
    } else {
      cmp_func_ = ObVarcharType == obj_meta.get_type() && CS_TYPE_BINARY == obj_meta.get_collation_type()
                  ? compare_binary_doc_ids : id_basic_funcs->null_first_cmp_;
    }
  }
  return ret;
}

int ObDomainIdCmp::compare(const ObDatum &lhs, const ObDatum &rhs, int &cmp_ret) const
{
  int ret = OB_SUCCESS;
  const bool lmax = lhs.is_max();
  const bool lmin = lhs.is_min();
  const bool rmax = rhs.is_max();
  const bool rmin = rhs.is_min();
  if (OB_UNLIKELY(nullptr == cmp_func_)) {
    ret = OB_NOT_INIT;
    STORAGE_LOG(WARN,"not init", K(ret));
  } else if (lhs.is_ext() || rhs.is_ext()) {
    if (OB_UNLIKELY(!(lmax || lmin || rmax || rmin))) {
      ret = OB_ERR_UNEXPECTED;
      STORAGE_LOG(WARN,"unexpected ext datum", K(ret), K(lhs), K(rhs));
    } else if (lmax || rmax) {
      cmp_ret = lmax - rmax;
    } else if (lmin || rmin) {
      cmp_ret = -(lmin - rmin);
    } else {
      ret = OB_ERR_UNEXPECTED;
    }
  } else {
    ret = cmp_func_(lhs, rhs, cmp_ret, nullptr);
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase

#endif
