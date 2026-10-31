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

#define USING_LOG_PREFIX STORAGE

#include "ob_text_daat_iter.h"

namespace oceanbase
{
namespace storage
{
int ObTextDaaTIter::init(const ObTextDaaTParam &param)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(param.base_param_) || OB_ISNULL(param.dim_iters_) || OB_ISNULL(param.allocator_)
      || OB_ISNULL(param.relevance_collector_)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ObSRDaaTIterImpl::init(*param.base_param_, *param.dim_iters_,
                                            *param.allocator_, *param.relevance_collector_))) {
  } else if (param.base_param_->need_project_relevance()
             && OB_FAIL(bm25_param_estimator_.init(param.bm25_param_est_ctx_))) {
  } else {
    mode_flag_ = param.mode_flag_;
    function_lookup_mode_ = param.function_lookup_mode_;
    const auto &id_meta = iter_param_->id_proj_expr_->datum_meta_;
    batch_merge_ = sql::NATURAL_LANGUAGE_MODE == mode_flag_ && !function_lookup_mode_
        && iter_param_->eval_ctx_->is_vectorized()
        && dim_iters_->count() <= ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT
        && ObVarcharType == id_meta.type_ && CS_TYPE_BINARY == id_meta.cs_type_;
  }
  return ret;
}

void ObTextDaaTIter::reset()
{
  batch_merge_ = false;
  for (auto &cursor : batch_cursors_) { cursor = BatchCursor(); }
  bm25_param_estimator_.reset();
  // this class can know the type of dim_iters_, but ObSRDaaTIterImpl maybe not
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i))->reset();
    }
  }
  ObSRDaaTIterImpl::reset();
}

void ObTextDaaTIter::reuse(const bool switch_tablet)
{
  for (auto &cursor : batch_cursors_) { cursor = BatchCursor(); }
  if ((OB_NOT_NULL(dim_iters_) && 0 == dim_iters_->count())) {
    // do nothing
  } else {
    bm25_param_estimator_.reuse(switch_tablet);
  }
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i))->reuse();
    }
  }
  ObSRDaaTIterImpl::reuse(switch_tablet);
}

int ObTextDaaTIter::get_next_rows(const int64_t capacity, int64_t &count)
{
  if (!batch_merge_) {
    return ObSRDaaTIterImpl::get_next_rows(capacity, count);
  }
  int ret = OB_SUCCESS;
  count = 0;
  const auto *limit = iter_param_->limit_param_;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (capacity <= 0) {
  } else if (dim_iters_->count() == 0
      || (limit->is_valid() && output_row_cnt_ >= limit->limit_)) {
    ret = OB_ITER_END;
  } else if (OB_FAIL(pre_process())) {
  } else {
    const int64_t dim_count = dim_iters_->count();
    const int64_t real_capacity = MIN(capacity, iter_param_->max_batch_size_);
    const int64_t should_match = static_cast<ObSRDaaTInnerProductRelevanceCollector *>(
        relevance_collector_)->minimum_should_match();
    const bool simple_output = !iter_param_->need_filter() && !limit->is_valid();
    const bool need_scores = iter_param_->need_project_relevance();
    ObDocIdExt *output_ids = buffered_domain_ids_.get_data();
    double *output_scores = buffered_relevances_.get_data();
    while (OB_SUCC(ret) && count < real_capacity) {
      const ObDatum *minimum = nullptr;
      uint64_t equal_dims = 0;
      for (int64_t i = 0; OB_SUCC(ret) && i < dim_count; ++i) {
        BatchCursor &cursor = batch_cursors_[i];
        if (!cursor.ended_ && cursor.pos_ == cursor.count_) {
          auto *dim = static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i));
          if (OB_FAIL(dim->next_batch_view(cursor.ids_, cursor.scores_, cursor.count_))) {
            if (OB_ITER_END == ret) {
              cursor.ended_ = true;
              ret = OB_SUCCESS;
            }
          }
          cursor.pos_ = 0;
        }
        if (OB_SUCC(ret) && !cursor.ended_) {
          const ObDatum &id = cursor.ids_[cursor.pos_].get_datum();
          int cmp = -1;
          if (nullptr != minimum) {
            compare_binary_doc_ids(id, *minimum, cmp, nullptr);
          }
          if (cmp < 0) {
            minimum = &id;
            equal_dims = uint64_t(1) << i;
          } else if (cmp == 0) {
            equal_dims |= uint64_t(1) << i;
          }
        }
      }
      if (OB_FAIL(ret)) {
      } else if (nullptr == minimum) {
        ret = OB_ITER_END;
      } else if (simple_output && (equal_dims & (equal_dims - 1)) == 0) {
        // Emit a run from one posting list until another list's head can
        // match it. No cursor is refilled here, so all borrowed batch data
        // and the competing head stay valid throughout the run.
        const int64_t selected = __builtin_ctzll(equal_dims);
        BatchCursor &cursor = batch_cursors_[selected];
        const ObDatum *boundary = nullptr;
        for (int64_t i = 0; i < dim_count; ++i) {
          if (i != selected && !batch_cursors_[i].ended_) {
            const auto &other = batch_cursors_[i];
            const ObDatum &id = other.ids_[other.pos_].get_datum();
            int cmp = -1;
            if (nullptr != boundary) { compare_binary_doc_ids(id, *boundary, cmp, nullptr); }
            if (cmp < 0) { boundary = &id; }
          }
        }
        while (OB_SUCC(ret) && count < real_capacity && cursor.pos_ < cursor.count_) {
          const ObDatum &id = cursor.ids_[cursor.pos_].get_datum();
          int cmp = -1;
          if (nullptr != boundary) { compare_binary_doc_ids(id, *boundary, cmp, nullptr); }
          if (cmp >= 0) { break; }
          if (should_match <= 1) {
            double relevance = 0.0;
            if (need_scores) {
              double score = nullptr == cursor.scores_ ? 0.0 : cursor.scores_[cursor.pos_];
              if (nullptr != iter_param_->dim_weights_) {
                score = score * iter_param_->field_boost_ * iter_param_->dim_weights_->at(selected);
              }
              relevance += score;
            }
            if (OB_SUCC(ret = output_ids[count].from_datum(id))) {
              output_scores[count++] = relevance;
              ++input_row_cnt_;
              ++output_row_cnt_;
            }
          }
          ++cursor.pos_;
        }
      } else {
        double relevance = 0.0;
        int64_t matched = 0;
        // Preserve dimension order and multiplication order for exact scores.
        for (int64_t i = 0; i < dim_count; ++i) {
          if (equal_dims & (uint64_t(1) << i)) {
            BatchCursor &cursor = batch_cursors_[i];
            if (need_scores) {
              double score = nullptr == cursor.scores_ ? 0.0 : cursor.scores_[cursor.pos_];
              if (nullptr != iter_param_->dim_weights_) {
                score = score * iter_param_->field_boost_ * iter_param_->dim_weights_->at(i);
              }
              relevance += score;
            }
            ++matched;
            ++cursor.pos_;
          }
        }
        bool need_project = matched >= should_match;
        if (simple_output) {
          if (need_project && OB_SUCC(ret = output_ids[count].from_datum(*minimum))) {
            output_scores[count++] = relevance;
            ++input_row_cnt_;
            ++output_row_cnt_;
          }
        } else if (need_project && OB_FAIL(filter_on_demand(count, relevance, need_project))) {
        } else if (need_project && OB_FAIL(cache_result(count, *minimum, relevance))) {
        }
      }
    }
    if ((OB_SUCC(ret) || OB_ITER_END == ret) && count > 0) {
      // Match the base iterator contract: a nonempty final batch succeeds;
      // the following call reports OB_ITER_END, including row consumers.
      if (OB_FAIL(project_results(count))) {
      }
    }
  }
  return ret;
}

int ObTextDaaTIter::pre_process()
{
  int ret = OB_SUCCESS;
  if (dim_iters_->count() == 0) {
    ret = OB_ITER_END;
  } else if (iter_param_->need_project_relevance()) {
    if (OB_FAIL(bm25_param_estimator_.do_estimation(*iter_param_->eval_ctx_))) {
    }
  }
  return ret;
}

int ObTextDaaTIter::do_one_merge_round(int64_t &count)
{
  const int64_t dim_count = dim_iters_->count();
  if (dim_count > ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT) {
    return ObSRDaaTIterImpl::do_one_merge_round(count);
  }
  // For a short query, select the smallest document directly. The generic
  // merger otherwise pushes and pops the same few items through virtual
  // interfaces for every document. Keep equal-ID accumulation in dimension
  // order, as the heap does, so floating-point scores remain identical.
  int ret = OB_SUCCESS;
  for (int64_t i = 0; OB_SUCC(ret) && i < next_round_cnt_; ++i) {
    const int64_t idx = next_round_iter_idxes_[i];
    auto *dim = static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(idx));
    if (OB_ISNULL(dim)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(dim->get_next_row())) {
      if (OB_ITER_END == ret) {
        iter_domain_ids_[idx] = nullptr;
        ret = OB_SUCCESS;
      }
    } else if (OB_FAIL(dim->get_curr_id(iter_domain_ids_[idx]))) {
    }
  }
  next_round_cnt_ = 0;
  ObSRMergeItem minimum;
  uint64_t equal_dims = 0;
  for (int64_t i = 0; OB_SUCC(ret) && i < dim_count; ++i) {
    if (nullptr != iter_domain_ids_[i]) {
      ObSRMergeItem item;
      item.iter_idx_ = i;
      int64_t cmp = -1;
      if (equal_dims != 0 && OB_FAIL(merge_cmp_.cmp(item, minimum, cmp))) {
      } else if (cmp < 0) {
        minimum = item;
        equal_dims = uint64_t(1) << i;
      } else if (cmp == 0) {
        equal_dims |= uint64_t(1) << i;
      }
    }
  }
  if (OB_SUCC(ret) && equal_dims == 0) {
    ret = OB_ITER_END;
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < dim_count; ++i) {
    if ((equal_dims & (uint64_t(1) << i)) != 0) {
      auto *dim = static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i));
      double relevance = 0.0;
      if (OB_FAIL(dim->get_curr_score(relevance))) {
      } else {
        if (nullptr != iter_param_->dim_weights_) {
          relevance = relevance * iter_param_->field_boost_ * iter_param_->dim_weights_->at(i);
        }
        if (OB_FAIL(relevance_collector_->collect_one_dim(i, relevance))) {
        } else {
          next_round_iter_idxes_[next_round_cnt_++] = i;
        }
      }
    }
  }
  double relevance = 0.0;
  bool need_project = false;
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(relevance_collector_->get_result(relevance, need_project))) {
  } else if (need_project && OB_FAIL(filter_on_demand(count, relevance, need_project))) {
  } else if (need_project && OB_FAIL(cache_result(count, *iter_domain_ids_[minimum.iter_idx_], relevance))) {
  }
  return ret;
}

ObTextBMWIter::ObTextBMWIter()
  : ObSRBMWIterImpl(),
    bm25_param_estimator_() {}

void ObTextBMWIter::reuse(const bool switch_tablet)
{
  bm25_param_estimator_.reuse(switch_tablet);
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i))->reuse();
    }
  }
  ObSRBMWIterImpl::reuse(switch_tablet);
}

void ObTextBMWIter::reset()
{
  batch_top_k_ = false;
  bm25_param_estimator_.reset();
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i))->reset();
    }
  }
  ObSRBMWIterImpl::reset();
}

int ObTextBMWIter::init(const ObTextDaaTParam &param)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(param.base_param_) || OB_ISNULL(param.dim_iters_) || OB_ISNULL(param.allocator_)
      || OB_ISNULL(param.relevance_collector_)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ObSRBMWIterImpl::init(*param.base_param_, *param.dim_iters_,
                                           *param.allocator_, *param.relevance_collector_))) {
  } else if (OB_FAIL(bm25_param_estimator_.init(param.bm25_param_est_ctx_))) {
  } else {
    const auto &meta = iter_param_->id_proj_expr_->datum_meta_;
    batch_top_k_ = sql::NATURAL_LANGUAGE_MODE == param.mode_flag_
        && !param.function_lookup_mode_ && iter_param_->eval_ctx_->is_vectorized()
        && dim_iters_->count() <= ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT
        && ObVarcharType == meta.type_ && CS_TYPE_BINARY == meta.cs_type_;
  }
  return ret;
}

int ObTextBMWIter::get_next_rows(const int64_t capacity, int64_t &count)
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (dim_iters_->count() == 0) {
    ret = OB_ITER_END;
  } else if (OB_FAIL(bm25_param_estimator_.do_estimation(*iter_param_->eval_ctx_))) {
  }

  // For a small corpus, a streaming merge avoids the per-document WAND state
  // machine and block-bound probes. Large corpora retain block-max pruning.
  if (OB_SUCC(ret) && batch_top_k_ && BMWStatus::MAX_STATUS == status_
      && bm25_param_estimator_.get_total_doc_cnt() <= 64 * 1024) {
    if (OB_FAIL(scan_batches_for_top_k())) {
    } else {
      status_ = BMWStatus::FINISHED;
    }
  }

  if (FAILEDx(ObSRBMWIterImpl::get_next_rows(capacity, count))) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    }
  }
  return ret;
}

int ObTextBMWIter::scan_batches_for_top_k()
{
  int ret = OB_SUCCESS;
  struct Cursor {
    const ObDocIdExt *ids_ = nullptr;
    const double *scores_ = nullptr;
    int64_t pos_ = 0;
    int64_t count_ = 0;
    bool ended_ = false;
  } cursors[ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT];
  const int64_t dim_count = dim_iters_->count();
  const int64_t should_match = static_cast<ObSRDaaTInnerProductRelevanceCollector *>(
      relevance_collector_)->minimum_should_match();
  bool done = false;
  while (OB_SUCC(ret) && !done) {
    const ObDatum *minimum = nullptr;
    uint64_t equal_dims = 0;
    for (int64_t i = 0; OB_SUCC(ret) && i < dim_count; ++i) {
      Cursor &cursor = cursors[i];
      if (!cursor.ended_ && cursor.pos_ == cursor.count_) {
        auto *dim = static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i));
        if (OB_FAIL(dim->next_batch_view(cursor.ids_, cursor.scores_, cursor.count_))) {
          if (OB_ITER_END == ret) {
            cursor.ended_ = true;
            ret = OB_SUCCESS;
          }
        }
        cursor.pos_ = 0;
      }
      if (OB_SUCC(ret) && !cursor.ended_) {
        const ObDatum &id = cursor.ids_[cursor.pos_].get_datum();
        int cmp = -1;
        if (nullptr != minimum) {
          compare_binary_doc_ids(id, *minimum, cmp, nullptr);
        }
        if (cmp < 0) {
          minimum = &id;
          equal_dims = uint64_t(1) << i;
        } else if (cmp == 0) {
          equal_dims |= uint64_t(1) << i;
        }
      }
    }
    if (OB_FAIL(ret)) {
    } else if (nullptr == minimum) {
      done = true;
    } else {
      double relevance = 0.0;
      int64_t matched = 0;
      for (int64_t i = 0; i < dim_count; ++i) {
        if (equal_dims & (uint64_t(1) << i)) {
          Cursor &cursor = cursors[i];
          double score = nullptr == cursor.scores_ ? 0.0 : cursor.scores_[cursor.pos_];
          if (nullptr != iter_param_->dim_weights_) {
            score = score * iter_param_->field_boost_ * iter_param_->dim_weights_->at(i);
          }
          relevance += score;
          ++matched;
          ++cursor.pos_;
        }
      }
      if (matched >= should_match && OB_FAIL(process_collected_row(*minimum, relevance))) {
      }
    }
  }
  return ret;
}

int ObTextBMWIter::init_before_wand_process()
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!bm25_param_estimator_.is_estimated())) {
    ret = OB_ERR_UNEXPECTED;
  }

  for (int64_t i = 0; OB_SUCC(ret) && i < dim_iters_->count(); ++i) {
    ObTextRetrievalBlockMaxIter *block_max_iter = static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i));
    if (OB_FAIL(block_max_iter->init_block_max_iter(
        bm25_param_estimator_.get_total_doc_cnt(), bm25_param_estimator_.get_avg_doc_token_cnt()))) {
    }
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase
