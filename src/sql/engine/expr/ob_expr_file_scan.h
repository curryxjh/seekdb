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

#pragma once

#include "sql/engine/expr/ob_expr_operator.h"

namespace oceanbase {
namespace sql {

class ObExprFileScan : public ObFuncExprOperator
{
public:
  explicit ObExprFileScan(common::ObIAllocator &alloc);
  virtual ~ObExprFileScan() = default;
  int calc_result_typeN(ObExprResType &type, ObExprResType *types, int64_t param_num,
                        common::ObExprTypeCtx &type_ctx) const override;
  int cg_expr(ObExprCGCtx &expr_cg_ctx, const ObRawExpr &raw_expr,
              ObExpr &rt_expr) const override;
  static int eval_file_scan(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res);
private:
  DISALLOW_COPY_AND_ASSIGN(ObExprFileScan);
};

} // namespace sql
} // namespace oceanbase
