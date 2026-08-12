"""生产独立内容搜索的运行时命名空间校验。"""

from __future__ import annotations


def validate_production_content_search_runtime_namespace(
    *,
    environment,
    feature_flags,
    runtime_connection_name,
    runtime_index_prefix,
    runtime_read_alias,
    production_connection_name,
    production_index_prefix,
):
    """启用生产独立搜索时，阻止通用运行时键回退至测试默认值。"""

    if environment != "production" or not any(feature_flags):
        return
    if not production_connection_name:
        raise ValueError("生产内容搜索生产连接名不能为空")
    if not production_index_prefix or "prod" not in production_index_prefix.split("-"):
        raise ValueError("生产内容搜索生产索引前缀必须包含 prod 标识")
    if runtime_connection_name != production_connection_name:
        raise ValueError("生产内容搜索运行时连接名必须与生产连接名一致")
    if runtime_index_prefix != production_index_prefix:
        raise ValueError("生产内容搜索运行时索引前缀必须与生产索引前缀一致")
    if runtime_read_alias != f"{production_index_prefix}-read":
        raise ValueError("生产内容搜索运行时 read alias 必须与生产索引前缀一致")
