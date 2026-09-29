"""
Wagtail Page 正文外置通用协议 - 管理命令 (Management Commands) 定向测试。

验证：
1. record_external_content_policy 命令解除 E010 自举启动死锁，成功为未备案模型完成首次基线登记；
2. 登记完成后，纯只读模式 (allow_record_new=False) 校验成功通过；
3. init_external_content_indexes 索引初始化与 --check 校验命令正常执行。
"""

import io
import json
import os
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from wagtail import blocks
from wagtail.fields import StreamField
from wagtail.models import Page

from external_content.exceptions import FieldStorageConfigError
from external_content.mixins import ExternalContentPageMixin
from external_content.registry import (
    get_storage_policy_for_model,
    reset_active_baseline_file,
    set_active_baseline_file,
)


class CommandBootstrapTests(TestCase):
    """管理命令执行与自举解死锁测试。"""

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

    def test_record_external_content_policy_command_success(self) -> None:
        """测试 record_external_content_policy 成功为未备案模型完成基线登记，且解除系统检查死锁。"""
        class UnrecordedCommandModel(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())], use_json_field=True)
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "test_cmd_bootstrap"

        model_label = "test_cmd_bootstrap.unrecordedcommandmodel"

        # 1. 运行前只读解析必须失败（尚未在基线中固化）
        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(UnrecordedCommandModel, allow_record_new=False)

        # 2. 执行管理命令登记策略（模拟 apps.get_model 返回该模型）
        out = io.StringIO()
        with patch("django.apps.apps.get_model", return_value=UnrecordedCommandModel):
            call_command("record_external_content_policy", model_label, stdout=out)

        output_str = out.getvalue()
        self.assertIn("[RECORDED]", output_str)
        self.assertIn(model_label, output_str)

        # 3. 登记完成后只读解析成功通过，无须任何 allow_record_new 宽松参数
        policy = get_storage_policy_for_model(UnrecordedCommandModel, allow_record_new=False)
        self.assertIsNotNone(policy)
        self.assertEqual(policy.model_label, model_label)
        self.assertEqual(policy.schema_version, 1)

    def test_init_external_content_indexes_command(self) -> None:
        """测试 init_external_content_indexes 建立索引及 --check 校验。"""
        out = io.StringIO()
        call_command("init_external_content_indexes", stdout=out)
        self.assertIn("[SUCCESS]", out.getvalue())

        check_out = io.StringIO()
        call_command("init_external_content_indexes", "--check", stdout=check_out)
        self.assertIn("[OK]", check_out.getvalue())