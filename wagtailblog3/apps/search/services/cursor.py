"""公开内容搜索的签名游标协议。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from django.conf import settings
from django.core import signing


CURSOR_SALT = "search.content.cursor.v1"
CURSOR_VERSION = 1
CURSOR_DIRECTIONS = frozenset({"next", "previous"})


class ContentSearchCursorError(ValueError):
    """游标无效、过期或与当前查询不匹配。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContentSearchCursor:
    direction: str
    sort: tuple
    pit_id: str | None = None


def build_cursor_query_hash(
    query_string,
    search_type,
    start_date=None,
    end_date=None,
    order_by=None,
    locale=None,
):
    """把影响结果集合或顺序的全部条件绑定到游标。"""

    payload = {
        "query": str(query_string or ""),
        "type": str(search_type or ""),
        "start_date": str(start_date or ""),
        "end_date": str(end_date or ""),
        "order_by": str(order_by or ""),
        "locale": str(locale or ""),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_content_search_cursor(cursor, query_hash):
    if cursor.direction not in CURSOR_DIRECTIONS or not cursor.sort:
        raise ContentSearchCursorError("cursor_payload_invalid")
    payload = {
        "v": CURSOR_VERSION,
        "q": query_hash,
        "d": cursor.direction,
        "s": list(cursor.sort),
    }
    if cursor.pit_id:
        payload["p"] = cursor.pit_id
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def decode_content_search_cursor(token, query_hash):
    if not token:
        return None
    try:
        payload = signing.loads(
            token,
            salt=CURSOR_SALT,
            max_age=max(int(settings.CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS), 1),
        )
    except signing.SignatureExpired as error:
        raise ContentSearchCursorError("cursor_expired") from error
    except signing.BadSignature as error:
        raise ContentSearchCursorError("cursor_invalid") from error
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ContentSearchCursorError("cursor_payload_invalid")
    if payload.get("q") != query_hash:
        raise ContentSearchCursorError("cursor_query_mismatch")
    direction = payload.get("d")
    sort = payload.get("s")
    pit_id = payload.get("p")
    if direction not in CURSOR_DIRECTIONS or not isinstance(sort, list) or not sort:
        raise ContentSearchCursorError("cursor_payload_invalid")
    if pit_id is not None and not isinstance(pit_id, str):
        raise ContentSearchCursorError("cursor_payload_invalid")
    return ContentSearchCursor(direction=direction, sort=tuple(sort), pit_id=pit_id)
