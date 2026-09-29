"""
Wagtail Page 正文外置通用协议 - 系统启动检查 (System Checks) 定向测试。

测试覆盖：
- E001: content_storage 非字典
- E002: content_storage_schema 非合法正整数
- E003: 声明的外置字段在模型中不存在
- E004: 声明了黑名单中的核心关系/主键/状态字段
- E005: 声明了不支持的存储后端 (非 mongo)
- E006: 声明字段对应的类型没有可用的 Codec
- E007: 声明了 content_storage 但未继承 ExternalContentPageMixin
- E008: 继承了 ExternalContentPageMixin 但未声明有效 content_storage
- E009: 继承了 ExternalContentPageMixin 但不是 Wagtail Page 的子类
- E010: 同版本策略篡改检测（持久化基线指纹比对防漂移，未备案策略只读阻断）
- 隔离红线: 启动检查全过程绝对不连接 MongoDB 或创建索引
"""

import json
import os
import tempfile
from unittest.mock import patch

from django.core.checks import Error
from django.db import models
from django.test import SimpleTestCase
from wagtail.models import Page
from wagtail.fields import StreamField
from wagtail import blocks

from external_content.checks import check_external_content_models
from external_content.mixins import ExternalContentPageMixin
from external_content.registry import (
    get_storage_policy_for_model,
    reset_active_baseline_file,
    set_active_baseline_file,
)


class SystemChecksTests(SimpleTestCase):
    """验证启动检查各错误码与安全门禁。"""

    def setUp(self) -> None:
        self.temp_baseline = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_baseline.close()
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "policies": {}}, f)
        set_active_baseline_file(self.temp_baseline.name)

    def tearDown(self) -> None:
        reset_active_baseline_file()
        if os.path.exists(self.temp_baseline.name):
            os.remove(self.temp_baseline.name)
        lock_file = self.temp_baseline.name + ".lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)

    def test_valid_page_check_passes(self) -> None:
        """合法外置模型（继承 ExternalContentPageMixin + Page 且策略已备案）不产生任何 Check Error。"""
        class ValidCheckPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "check_test_valid"

        # 显式备案基线
        get_storage_policy_for_model(ValidCheckPage, allow_record_new=True)

        with patch("django.apps.apps.get_models", return_value=[ValidCheckPage]):
            errors = check_external_content_models(None)
            self.assertEqual(errors, [])

    def test_unrecorded_policy_produces_e010(self) -> None:
        """模型声明了 content_storage 但尚未在基线中固化时，启动检查报 E010 拒绝启动。"""
        class UnrecordedCheckPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "check_test_unrecorded"

        with patch("django.apps.apps.get_models", return_value=[UnrecordedCheckPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E010" for e in errors))

    def test_missing_mixin_inheritance_produces_e007(self) -> None:
        """声明 content_storage 但未继承 ExternalContentPageMixin 产生 E007。"""
        class MissingMixinPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "check_test_nomixin"

        with patch("django.apps.apps.get_models", return_value=[MissingMixinPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E007" for e in errors))

    def test_missing_content_storage_produces_e008(self) -> None:
        """继承 ExternalContentPageMixin 但未声明有效 content_storage 字典产生 E008。"""
        class EmptyMixinPage(ExternalContentPageMixin, Page):
            class Meta:
                app_label = "check_test_emptymixin"

        with patch("django.apps.apps.get_models", return_value=[EmptyMixinPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E008" for e in errors))

    def test_non_page_subclass_produces_e009(self) -> None:
        """继承 ExternalContentPageMixin 的类不是 Wagtail Page 子类产生 E009。"""
        class NonPageModel(ExternalContentPageMixin, models.Model):
            content_storage = {"body": "mongo"}

            class Meta:
                app_label = "check_test_nonpage"

        with patch("django.apps.apps.get_models", return_value=[NonPageModel]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E009" for e in errors))

    def test_invalid_storage_type_error_code_e001(self) -> None:
        """content_storage 非 dict 产生 E001。"""
        class BadTypePage(ExternalContentPageMixin, Page):
            content_storage = "body:mongo"

            class Meta:
                app_label = "check_test_e001"

        with patch("django.apps.apps.get_models", return_value=[BadTypePage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E001" for e in errors))

    def test_invalid_schema_version_error_code_e002(self) -> None:
        """content_storage_schema 非正整数产生 E002。"""
        class BadSchemaPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = -1

            class Meta:
                app_label = "check_test_e002"

        with patch("django.apps.apps.get_models", return_value=[BadSchemaPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E002" for e in errors))

    def test_missing_field_error_code_e003(self) -> None:
        """声明的模型字段不存在产生 E003。"""
        class MissingFieldPage(ExternalContentPageMixin, Page):
            content_storage = {"not_existing": "mongo"}

            class Meta:
                app_label = "check_test_e003"

        with patch("django.apps.apps.get_models", return_value=[MissingFieldPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E003" for e in errors))

    def test_forbidden_field_error_code_e004(self) -> None:
        """声明外置保留黑名单字段产生 E004。"""
        class ForbiddenFieldPage(ExternalContentPageMixin, Page):
            content_storage = {"title": "mongo"}

            class Meta:
                app_label = "check_test_e004"

        with patch("django.apps.apps.get_models", return_value=[ForbiddenFieldPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E004" for e in errors))

    def test_unsupported_backend_error_code_e005(self) -> None:
        """声明不受支持的存储后端产生 E005。"""
        class BadBackendPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "redis"}

            class Meta:
                app_label = "check_test_e005"

        with patch("django.apps.apps.get_models", return_value=[BadBackendPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E005" for e in errors))

    def test_unregistered_codec_error_code_e006(self) -> None:
        """声明字段没有匹配的 Codec 产生 E006。"""
        class UnregisteredFieldPage(ExternalContentPageMixin, Page):
            summary = models.TextField()
            content_storage = {"summary": "mongo"}

            class Meta:
                app_label = "check_test_e006"

        with patch("django.apps.apps.get_models", return_value=[UnregisteredFieldPage]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E006" for e in errors))

    def test_persistent_policy_drift_produces_e010_across_restarts(self) -> None:
        """同版本下策略篡改跨进程/重启检测出指纹不一致触发 E010 启动失败关闭。"""
        class DriftModel(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            sidebar = StreamField([("text", blocks.TextBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "check_test_drift_persistent"

        # 1. 首次登记并持久化基线
        get_storage_policy_for_model(DriftModel, allow_record_new=True)

        # 2. 模拟运行期在内存中篡改策略（未递增 schema）
        DriftModel.content_storage = {"body": "mongo", "sidebar": "mongo"}

        with patch("django.apps.apps.get_models", return_value=[DriftModel]):
            errors = check_external_content_models(None)
            self.assertTrue(any(e.id == "external_content.E010" for e in errors))

    def test_checks_do_not_connect_to_mongo(self) -> None:
        """门禁红线：启动检查严禁连接 MongoDB 或操作索引。"""
        class MongoIsolationPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "check_test_mongo_isolation"

        get_storage_policy_for_model(MongoIsolationPage, allow_record_new=True)

        with patch("pymongo.MongoClient") as mock_client:
            with patch("django.apps.apps.get_models", return_value=[MongoIsolationPage]):
                check_external_content_models(None)
            mock_client.assert_not_called()