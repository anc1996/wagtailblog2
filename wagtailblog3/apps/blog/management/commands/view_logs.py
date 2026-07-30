"""Inspect registered project log files without duplicating path metadata."""

from collections import deque
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from observability.registry import LOG_DOMAIN_KEYS, find_log_file


class Command(BaseCommand):
	help = "查看已注册日志域的活动或错误日志"

	def add_arguments(self, parser):
		parser.add_argument(
			"--module",
			choices=sorted(LOG_DOMAIN_KEYS),
			default="system",
			help="日志域",
		)
		parser.add_argument(
			"--kind",
			choices=("activity", "error"),
			default="error",
			help="日志类型，默认 error",
		)
		parser.add_argument("--lines", type=int, default=50, help="显示最后 N 行")

	def handle(self, *args, **options):
		module = options["module"]
		kind = options["kind"]
		lines = options["lines"]
		if lines < 1:
			raise CommandError("--lines 必须大于 0")

		spec = find_log_file(module, kind)
		if spec is None:
			raise CommandError(f"{module} 没有 {kind} 日志")

		log_file = Path(settings.LOG_DIR) / spec.relative_path
		if not log_file.exists():
			self.stderr.write(f"日志尚未生成: {log_file}")
			return

		try:
			with log_file.open("r", encoding="utf-8", errors="replace") as stream:
				last_lines = deque(stream, lines)
		except OSError as exc:
			raise CommandError(f"读取日志失败: {exc}") from exc

		self.stdout.write(f"=== {module}/{kind} 最新 {lines} 行 ===")
		for line in last_lines:
			self.stdout.write(line, ending="")
