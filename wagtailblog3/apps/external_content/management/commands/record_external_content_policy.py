"""
Django 管理命令：显式备案并固化模型的正文外置存储策略基线。

用于：
1. 开发者为新模型定义 content_storage 后，显式执行本命令固化基线至 policy_baselines.json；
2. 模型升级 content_storage_schema 后，显式固化新版本基线；
3. 设置 requires_system_checks = [] 豁免全局系统检查，解除未备案模型因 E010 导致的自举启动死锁；
4. 命令内部对目标模型执行全量字段、Codec 与配置合法性校验后再行固化。
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from external_content.exceptions import FieldStorageConfigError
from external_content.mixins import ExternalContentPageMixin
from external_content.registry import get_storage_policy_for_model


class Command(BaseCommand):
    help = "显式固化并备案模型的正文外置存储策略至 policy_baselines.json"
    # 解除未备案模型因启动检查 E010 阻断的自举死锁，由命令内单独校验目标模型
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "model_labels",
            nargs="*",
            help="待备案的小写模型标识（如 guides.guidepage），留空配合 --all 备案所有外置模型",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="扫描并备案项目中所有继承了 ExternalContentPageMixin 且声明了 content_storage 的模型",
        )

    def handle(self, *args, **options):
        model_labels = options.get("model_labels") or []
        record_all = options.get("all", False)

        if not model_labels and not record_all:
            raise CommandError("请指定待备案的模型标识 (例如 guides.guidepage) 或使用 --all")

        targets = []
        if record_all:
            for model in apps.get_models():
                if issubclass(model, ExternalContentPageMixin) and getattr(model, "content_storage", None):
                    targets.append(model)
        else:
            for label in model_labels:
                try:
                    model = apps.get_model(label)
                    targets.append(model)
                except Exception as exc:
                    raise CommandError(f"未找到指定的模型 '{label}': {exc}") from exc

        if not targets:
            self.stdout.write("未找到需要备案的模型。")
            return

        for model in targets:
            try:
                policy = get_storage_policy_for_model(model, allow_record_new=True)
                if policy:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"[RECORDED] 模型 {policy.model_label} (v{policy.schema_version}) 策略已成功固化至基线，指纹: {policy.fingerprint()[:16]}..."
                        )
                    )
            except FieldStorageConfigError as exc:
                self.stderr.write(self.style.ERROR(f"[FAIL] 模型 {model} 策略备案失败: {exc}"))
                raise CommandError(str(exc)) from exc