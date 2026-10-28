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

#define USING_LOG_PREFIX SQL_ENG
#include "sql/engine/expr/ob_expr_file_scan.h"

namespace oceanbase {
namespace sql {

ObExprFileScan::ObExprFileScan(common::ObIAllocator &alloc)
  : ObFuncExprOperator(alloc, T_FUN_SYS_FILE_SCAN, "read_csv", PARAM_NUM_UNKNOWN,
                       NOT_VALID_FOR_GENERATED_COL, NOT_ROW_DIMENSION)
{}

int ObExprFileScan::calc_result_typeN(ObExprResType &type, ObExprResType *types,
                                      int64_t param_num, common::ObExprTypeCtx &type_ctx) const
{
  UNUSED(types);
  UNUSED(param_num);
  UNUSED(type_ctx);
  type.set_int();
  return OB_SUCCESS;
}

int ObExprFileScan::cg_expr(ObExprCGCtx &expr_cg_ctx, const ObRawExpr &raw_expr,
                            ObExpr &rt_expr) const
{
  UNUSED(expr_cg_ctx);
  UNUSED(raw_expr);
  rt_expr.eval_func_ = eval_file_scan;
  return OB_SUCCESS;
}

int ObExprFileScan::eval_file_scan(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res)
{
  UNUSED(expr);
  UNUSED(ctx);
  UNUSED(res);
  LOG_USER_ERROR(OB_NOT_SUPPORTED, "file reader functions must be used in FROM");
  return OB_NOT_SUPPORTED;
}

} // namespace sql
} // namespace oceanbase
