"""MongoDB 孤儿正文数据治理与可视化审计后台视图。

仅供系统超级管理员（Superuser）访问，提供：
1. 孤儿候选列表报表面板（支持按集合、分类筛选及分页）；
2. 正文底层 Body 异步反解析预览 API（供抽屉展开 Markdown、字数及 StreamField 结构）；
3. 具备 Fencing Token 强阻断校验的安全清理 POST API。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from blog.services.mongo_orphan import CATEGORY_LABELS, TARGET_COLLECTIONS, MongoOrphanService

logger = logging.getLogger(__name__)


def _is_superuser_check(user: Any) -> bool:
    """严格超管权限判断；普通后台用户即使具备编辑权限也无权操作底层正文清理。"""
    return bool(user and user.is_authenticated and user.is_active and user.is_superuser)


@user_passes_test(_is_superuser_check, login_url="wagtailadmin_login")
@require_GET
def mongo_orphans_report_view(request: HttpRequest) -> HttpResponse:
    """Mongo 孤儿正文治理主面板视图。

    查询 Mongo 正文集合元数据并与 MySQL 活跃上下文交叉对比，展示统计指标与分页列表。
    """
    coll_filter = request.GET.get("collection", "").strip() or None
    cat_filter = request.GET.get("category", "").strip() or None
    page_id_str = request.GET.get("page_id", "").strip()
    page_filter: int | None = None
    if page_id_str.isdigit():
        page_filter = int(page_id_str)

    # 扫描孤儿数据（默认上限 2000 条进行管理展示）
    scan_result = MongoOrphanService.scan_orphans(
        limit=2000,
        page_filter=page_filter,
        collection_filter=coll_filter,
    )

    all_candidates = scan_result.get("candidates", [])

    # 若有分类筛选则在结果集中二次过滤
    if cat_filter:
        filtered_candidates = [c for c in all_candidates if c.get("category") == cat_filter]
    else:
        filtered_candidates = all_candidates

    # 分页展示（每页 15 条）
    page_num = request.GET.get("page", "1")
    paginator = Paginator(filtered_candidates, 15)
    page_obj = paginator.get_page(page_num)

    context: dict[str, Any] = {
        "title": "Mongo 孤儿正文治理",
        "page_obj": page_obj,
        "total_candidates": scan_result.get("candidate_count", 0),
        "collections_summary": scan_result.get("collections", {}),
        "category_counts": scan_result.get("category_counts", {}),
        "category_labels": CATEGORY_LABELS,
        "target_collections": TARGET_COLLECTIONS,
        "selected_collection": coll_filter or "",
        "selected_category": cat_filter or "",
        "selected_page_id": page_id_str,
        "mongo_error": scan_result.get("mongo_error"),
    }
    return render(request, "wagtailadmin/reports/mongo_orphans.html", context)


@user_passes_test(_is_superuser_check, login_url="wagtailadmin_login")
@require_GET
def mongo_orphan_preview_api(request: HttpRequest) -> JsonResponse:
    """异步拉取单个 Mongo 正文的富文本反解析内容与元数据。"""
    collection = request.GET.get("collection", "").strip()
    mongo_id = request.GET.get("mongo_id", "").strip()

    if not collection or not mongo_id:
        return JsonResponse({"error": "缺少必要参数 collection 或 mongo_id"}, status=400)

    if collection not in TARGET_COLLECTIONS:
        return JsonResponse({"error": f"非法集合名称: {collection}"}, status=400)

    try:
        preview_data = MongoOrphanService.get_orphan_body_preview(collection, mongo_id)
        return JsonResponse(preview_data)
    except FileNotFoundError as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    except Exception as exc:
        logger.error("读取孤儿正文预览失败: %s", exc, exc_info=True)
        return JsonResponse({"error": f"读取正文失败: {exc}"}, status=500)


@user_passes_test(_is_superuser_check, login_url="wagtailadmin_login")
@require_POST
def mongo_orphan_cleanup_api(request: HttpRequest) -> JsonResponse:
    """执行单条或批量 Mongo 孤儿正文的物理安全清理。

    包含强阻断 Fencing Token 瞬时校验；若被活跃页面或删除任务并发引用立即抛出 403 阻断。
    支持单条清理（参数 collection, mongo_id）与批量清理（参数 items: [{"collection": ..., "mongo_id": ...}]）。
    """
    items_to_delete: list[dict[str, str]] = []

    # 优先检查 JSON Body
    try:
        if request.body:
            body_data = json.loads(request.body)
            if isinstance(body_data, dict):
                raw_items = body_data.get("items")
                if isinstance(raw_items, list):
                    for it in raw_items:
                        if isinstance(it, dict) and it.get("collection") and it.get("mongo_id"):
                            items_to_delete.append({
                                "collection": str(it["collection"]).strip(),
                                "mongo_id": str(it["mongo_id"]).strip(),
                            })
                elif body_data.get("collection") and body_data.get("mongo_id"):
                    items_to_delete.append({
                        "collection": str(body_data["collection"]).strip(),
                        "mongo_id": str(body_data["mongo_id"]).strip(),
                    })
    except Exception:
        pass

    # 兼容常规 Form POST 数据
    if not items_to_delete:
        form_coll = request.POST.get("collection", "").strip()
        form_id = request.POST.get("mongo_id", "").strip()
        raw_items_json = request.POST.get("items", "").strip()
        if raw_items_json:
            try:
                parsed_list = json.loads(raw_items_json)
                if isinstance(parsed_list, list):
                    for it in parsed_list:
                        if isinstance(it, dict) and it.get("collection") and it.get("mongo_id"):
                            items_to_delete.append({
                                "collection": str(it["collection"]).strip(),
                                "mongo_id": str(it["mongo_id"]).strip(),
                            })
            except Exception:
                pass
        elif form_coll and form_id:
            items_to_delete.append({
                "collection": form_coll,
                "mongo_id": form_id,
            })

    if not items_to_delete:
        return JsonResponse({"error": "缺少必要的目标集合或文档 ID 参数"}, status=400)

    # 执行清理处理
    deleted_records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for item in items_to_delete:
        coll = item["collection"]
        m_id = item["mongo_id"]
        try:
            res = MongoOrphanService.delete_orphan_document(
                collection_name=coll,
                mongo_id=m_id,
                actor=request.user,
            )
            deleted_records.append(res)
        except PermissionError as exc:
            logger.warning("孤儿清理并发防护拦截: %s", exc)
            errors.append({"collection": coll, "mongo_id": m_id, "error": str(exc), "code": "fencing_blocked"})
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            errors.append({"collection": coll, "mongo_id": m_id, "error": str(exc), "code": "validation_failed"})
        except Exception as exc:
            logger.error("孤儿清理未知异常: %s", exc, exc_info=True)
            errors.append({"collection": coll, "mongo_id": m_id, "error": f"系统错误: {exc}", "code": "system_error"})

    # 若单条操作且遭遇 Fencing 拦截，返回 403 明确提示
    if len(items_to_delete) == 1 and errors:
        err = errors[0]
        status_code = 403 if err.get("code") == "fencing_blocked" else 400
        return JsonResponse({"error": err["error"]}, status=status_code)

    return JsonResponse({
        "success": len(deleted_records) > 0 or len(errors) == 0,
        "message": f"成功清理 {len(deleted_records)} 条孤儿正文" + (f"，{len(errors)} 条失败" if errors else ""),
        "total": len(items_to_delete),
        "deleted_count": len(deleted_records),
        "deleted_records": deleted_records,
        "errors": errors,
    })
