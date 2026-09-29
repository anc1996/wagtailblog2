"""
Wagtail Page 正文外置通用协议 - Django 系统启动检查。

在 Django 系统检查阶段静态验证所有声明了 content_storage 或继承了 ExternalContentPageMixin 的模型配置。
红线约束：
本模块仅执行静态元数据分析，绝对禁止连接 MongoDB 或创建索引。
"""

from typing import Any, List

from django.apps import apps
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.exceptions import FieldDoesNotExist
from wagtail.models import Page

from external_content.exceptions import CodecNotFoundError, FieldStorageConfigError
from external_content.mixins import ExternalContentPageMixin
from external_content.registry import (
    FORBIDDEN_EXTERNAL_FIELDS,
    SUPPORTED_BACKENDS,
    default_codec_registry,
    get_storage_policy_for_model,
)


@register(Tags.models)
def check_external_content_models(app_configs: Any, **kwargs: Any) -> List[CheckMessage]:
    """
    检查所有模型的外置存储声明与 Mixin 继承契约。

    验证项：
    1. external_content.E001: content_storage 必须为 dict；
    2. external_content.E002: content_storage_schema 必须为正整数；
    3. external_content.E003: 声明字段在模型中必须存在；
    4. external_content.E004: 声明字段不得为核心/关系保留字段；
    5. external_content.E005: 存储后端必须受支持（如 mongo）；
    6. external_content.E006: 字段类型必须匹配已注册的 Codec；
    7. external_content.E007: 声明了 content_storage 的模型必须显式继承 ExternalContentPageMixin；
    8. external_content.E008: 继承了 ExternalContentPageMixin 的模型必须显式定义 content_storage；
    9. external_content.E009: ExternalContentPageMixin 只能被 Wagtail Page 子类继承；
    10. external_content.E010: 模型存储策略解析或同版本防漂移校验失败。
    """
    errors: List[CheckMessage] = []

    all_models = apps.get_models()

    for model in all_models:
        has_storage_attr = hasattr(model, "content_storage") and getattr(model, "content_storage") is not None
        is_mixin_subclass = issubclass(model, ExternalContentPageMixin)

        # 既无声明也未继承 Mixin 的普通模型（如 BlogPage、HomePage、PortfolioPage）直接跳过
        if not has_storage_attr and not is_mixin_subclass:
            continue

        model_label = getattr(model._meta, "label", model.__name__)

        # E007: 声明了 content_storage 但未继承 ExternalContentPageMixin
        if has_storage_attr and not is_mixin_subclass:
            errors.append(
                Error(
                    f"模型 {model_label} 声明了 content_storage，但未继承 ExternalContentPageMixin。",
                    hint="所有外置正文页面必须显式继承 ExternalContentPageMixin 准入基类。",
                    obj=model,
                    id="external_content.E007",
                )
            )

        # E008: 继承了 ExternalContentPageMixin 但未声明 content_storage
        if is_mixin_subclass and not has_storage_attr:
            errors.append(
                Error(
                    f"模型 {model_label} 继承了 ExternalContentPageMixin，但未声明 content_storage 字典。",
                    hint="必须显式声明模型级 content_storage = {'body': 'mongo'}，明确选择外置字段。",
                    obj=model,
                    id="external_content.E008",
                )
            )
            continue

        # E009: 必须是 Wagtail Page 子类
        if is_mixin_subclass and not issubclass(model, Page):
            errors.append(
                Error(
                    f"模型 {model_label} 继承了 ExternalContentPageMixin，但并非 Wagtail Page 子类。",
                    hint="ExternalContentPageMixin 仅支持挂载在 Wagtail Page 模型上。",
                    obj=model,
                    id="external_content.E009",
                )
            )

        content_storage = getattr(model, "content_storage", None)

        # E001: content_storage 必须是 dict
        if not isinstance(content_storage, dict):
            errors.append(
                Error(
                    f"模型 {model_label} 的 content_storage 必须为 dict 类型，当前为 {type(content_storage).__name__}。",
                    hint="请将 content_storage 定义为形如 {'body': 'mongo'} 的字典。",
                    obj=model,
                    id="external_content.E001",
                )
            )
            continue

        # E002: content_storage_schema 必须是正整数
        schema_version = getattr(model, "content_storage_schema", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
        ):
            errors.append(
                Error(
                    f"模型 {model_label} 的 content_storage_schema 必须为正整数 (>=1)，当前为 {repr(schema_version)}。",
                    hint="请将 content_storage_schema 设置为 >= 1 的整数版本号。",
                    obj=model,
                    id="external_content.E002",
                )
            )

        # 检查具体声明的字段
        for field_name, backend in content_storage.items():
            # E004: 禁止核心保留字段
            if field_name in FORBIDDEN_EXTERNAL_FIELDS:
                errors.append(
                    Error(
                        f"模型 {model_label} 的字段 '{field_name}' 属于系统核心保留字段，严禁外置。",
                        hint=f"禁止外置以下字段: {sorted(FORBIDDEN_EXTERNAL_FIELDS)}",
                        obj=model,
                        id="external_content.E004",
                    )
                )

            # E005: 校验后端类型
            if backend not in SUPPORTED_BACKENDS:
                errors.append(
                    Error(
                        f"模型 {model_label} 的字段 '{field_name}' 声明了不受支持的后端 '{backend}'。",
                        hint=f"当前受支持的后端集合: {sorted(SUPPORTED_BACKENDS)}",
                        obj=model,
                        id="external_content.E005",
                    )
                )

            # E003: 字段是否存在
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                errors.append(
                    Error(
                        f"模型 {model_label} 声明了外置字段 '{field_name}'，但该字段在模型中不存在。",
                        hint="请检查字段名称拼写，确保其已在模型中正确定义。",
                        obj=model,
                        id="external_content.E003",
                    )
                )
                continue

            # E004: 禁止关系字段
            if getattr(field, "is_relation", False):
                errors.append(
                    Error(
                        f"模型 {model_label} 的字段 '{field_name}' 为关系字段，严禁外置。",
                        hint="ForeignKey、ManyToManyField 与 OneToOneField 必须由关系型数据库管理。",
                        obj=model,
                        id="external_content.E004",
                    )
                )

            # E006: 查找兼容 Codec
            try:
                default_codec_registry.get_codec_for_field(field)
            except CodecNotFoundError:
                errors.append(
                    Error(
                        f"模型 {model_label} 的字段 '{field_name}'（类型 {type(field).__name__}）未找到已注册的兼容 Codec。",
                        hint="请先在 CodecRegistry 中注册该 Field 类型的 Codec，或改用已支持的字段类型（如 StreamField）。",
                        obj=model,
                        id="external_content.E006",
                    )
                )

        # 尝试完整解析策略并校验指纹防漂移（E010）
        try:
            get_storage_policy_for_model(model, allow_record_new=False)
        except FieldStorageConfigError as exc:
            errors.append(
                Error(
                    f"模型 {model_label} 存储策略验证失败: {exc}",
                    hint="请检查 content_storage 配置及 schema 版本号，避免同版本下篡改外置策略。",
                    obj=model,
                    id="external_content.E010",
                )
            )

    return errors