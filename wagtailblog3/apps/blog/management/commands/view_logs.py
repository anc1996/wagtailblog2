"""查看已注册的项目日志文件，避免重复维护日志路径。"""

from collections import deque
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from observability.registry import LOG_DOMAIN_KEYS, find_log_file


class Command(BaseCommand):
	"""从日志注册表定位文件，并只读取末尾指定行数。"""
	help = "查看已注册日志域的活动或错误日志"

	def add_arguments(self, parser):
		"""声明日志域、日志类型和尾部行数参数。"""
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
		"""校验注册项后输出日志文件尾部，避免加载整个文件。"""
		# 先通过日志注册表解析安全路径，再读取文件尾部，避免一次性加载大日志。
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
