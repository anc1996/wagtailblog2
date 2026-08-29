"""公开 BlogPage 的搜索 State 初始化，不触发索引写入。"""

from __future__ import annotations

from django.db import transaction
from wagtail.models import Page

from blog.models import BlogPage
from search.models import ContentSearchOperation, ContentSearchState
from search.services.document import build_formal_content_snapshot


def bootstrap_content_search_states(
    after_page_id: int = 0,
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, int]:
    """按 Page 主键游标初始化缺失 State；已有 State 永远不被本命令覆盖。"""

    page_ids = list(
        BlogPage.objects.live()
        .public()
        .filter(pk__gt=after_page_id)
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    result = {
        "scanned": len(page_ids),
        "created": 0,
        "existing": 0,
        "missing_formal_content": 0,
        "no_longer_public": 0,
        "next_after_page_id": page_ids[-1] if page_ids else after_page_id,
    }
    for page_id in page_ids:
        with transaction.atomic():
            # 与发布路径共用 Page 行锁，防止 bootstrap 用旧正文创建初始 State。
            Page.objects.select_for_update().only("pk").get(pk=page_id)
            if ContentSearchState.objects.filter(page_id=page_id).exists():
                result["existing"] += 1
                continue
            page = BlogPage.objects.live().public().filter(pk=page_id).first()
            if page is None:
                result["no_longer_public"] += 1
                continue
            snapshot = build_formal_content_snapshot(page)
            if snapshot is None:
                result["missing_formal_content"] += 1
                continue
            if dry_run:
                result["created"] += 1
                continue
            try:
                from blog.models import BlogPublicationState

                publication_state = BlogPublicationState.objects.filter(page_id=page_id).first()
            except Exception:
                publication_state = None
            body_version_id = getattr(publication_state, "published_body_version_id", None)
            publication_generation = getattr(publication_state, "publication_generation", None)
            ContentSearchState.objects.create(
                page_id=page_id,
                content_version=1,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash=snapshot.content_hash,
                mongo_content_id=snapshot.mongo_content_id,
                body_version_id=body_version_id or None,
                publication_generation=(
                    publication_generation if publication_generation and publication_generation > 0 else None
                ),
            )
            result["created"] += 1
    return result
