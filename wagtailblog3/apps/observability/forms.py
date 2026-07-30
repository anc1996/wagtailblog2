"""系统日志筛选及清理确认表单。"""

import uuid
from datetime import timedelta

from django import forms
from django.utils import timezone

from .registry import LOG_DOMAIN_KEYS, LOG_FILE_SPECS


class LogFilterForm(forms.Form):
    """限制查询参数，避免视图直接接受未经校验的筛选值。"""
    domain = forms.ChoiceField(label="日志模块", required=False)
    kind = forms.ChoiceField(
        label="日志类型",
        required=False,
        choices=(("", "全部"), ("activity", "活动"), ("error", "错误")),
        initial="error",
    )
    level = forms.ChoiceField(
        label="级别",
        required=False,
        choices=(("", "全部"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR"), ("CRITICAL", "CRITICAL")),
    )
    period = forms.ChoiceField(
        label="时间范围",
        choices=(
            ("15m", "最近 15 分钟"),
            ("1h", "最近 1 小时"),
            ("24h", "最近 24 小时"),
            ("custom", "自定义"),
            ("all", "全部时间"),
        ),
        initial="24h",
    )
    custom_start = forms.DateTimeField(
        label="开始时间",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    custom_end = forms.DateTimeField(
        label="结束时间",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    keyword = forms.CharField(label="关键词", required=False, max_length=120)
    include_rotated = forms.BooleanField(label="包含轮转历史", required=False)
    page_size = forms.TypedChoiceField(
        label="每页数量",
        choices=((50, "50"), (100, "100"), (200, "200")),
        coerce=int,
        initial=100,
    )
    page = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=1_000_000,
        initial=1,
        widget=forms.HiddenInput,
    )
    page_session = forms.CharField(
        required=False, max_length=512, widget=forms.HiddenInput
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].choices = (("", "全部"),) + tuple((key, key) for key in LOG_DOMAIN_KEYS)

    def since(self):
        period = self.cleaned_data["period"]
        delta = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "24h": timedelta(hours=24)}.get(period)
        value = timezone.now() - delta if delta else self.cleaned_data.get("custom_start")
        return self._local_naive(value)

    def until(self):
        return self._local_naive(self.cleaned_data.get("custom_end"))

    @staticmethod
    def _local_naive(value):
        if value is None:
            return None
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.replace(tzinfo=None)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("period") == "custom":
            start = cleaned.get("custom_start")
            end = cleaned.get("custom_end")
            if not start or not end:
                self.add_error("custom_start", "自定义时间必须同时填写开始和结束时间")
            elif start > end:
                self.add_error("custom_end", "结束时间不能早于开始时间")
        return cleaned


def confirmation_text(target_type: str) -> str:
    return "清空全部日志" if target_type == "all" else "确认清理日志"


class LogClearSelectionForm(forms.Form):
    """只接受 catalog 文件、日志域和固定清理范围。"""

    TARGET_TYPES = (("file", "单个文件"), ("domain", "单个模块"), ("business", "全部业务日志"), ("all", "全部运行日志"))
    target_type = forms.ChoiceField(label="目标类型", choices=TARGET_TYPES)
    target = forms.ChoiceField(label="清理目标", required=False)
    kind = forms.ChoiceField(
        label="日志类型",
        required=False,
        choices=(("", "全部"), ("activity", "活动"), ("error", "错误")),
    )
    scope = forms.ChoiceField(
        label="清理范围",
        choices=(("current", "仅当前文件"), ("rotated", "仅轮转历史"), ("all", "当前及轮转历史")),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].choices = (
            ("", "全部"),
            ("模块", tuple((key, key) for key in LOG_DOMAIN_KEYS)),
            ("单个文件", tuple((spec.key, spec.label) for spec in LOG_FILE_SPECS)),
        )

    def clean(self):
        cleaned = super().clean()
        target_type = cleaned.get("target_type")
        target = cleaned.get("target", "")
        if target_type == "file" and target not in {spec.key for spec in LOG_FILE_SPECS}:
            self.add_error("target", "日志文件未注册")
        if target_type == "domain" and target not in LOG_DOMAIN_KEYS:
            self.add_error("target", "日志模块未注册")
        if target_type in {"business", "all"}:
            # 全量范围没有客户端 target，防止伪造值污染审计记录。
            cleaned["target"] = ""
        return cleaned


class LogClearForm(LogClearSelectionForm):
    """将执行目标绑定到一次预览，并要求加强确认文本。"""

    confirmation = forms.CharField(max_length=20)
    idempotency_key = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    preview_token = forms.CharField(widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        expected = confirmation_text(cleaned.get("target_type", ""))
        if cleaned.get("confirmation") != expected:
            self.add_error("confirmation", f"请输入“{expected}”完成确认")
        return cleaned
