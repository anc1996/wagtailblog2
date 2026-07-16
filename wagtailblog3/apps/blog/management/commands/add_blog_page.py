# blog/management/commands/add_blog_page.py
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape, linebreaks
from django.utils.text import slugify

from blog.models import BlogIndexPage, BlogPage


class Command(BaseCommand):
	help = "交互式添加博客页面，并把正文作为 BlogPage.body 的 markdown_block 保存"

	def add_arguments(self, parser):
		parser.add_argument("--parent-id", type=int, help="博客索引页 ID；不传则交互式选择")
		parser.add_argument("--title", help="博客标题；不传则交互式输入")
		parser.add_argument("--slug", help="页面 slug；不传则由标题自动生成")
		parser.add_argument("--tags", help="标签，多个标签用英文逗号分隔")
		parser.add_argument("--intro", help="简介；不传则交互式输入")
		parser.add_argument("--body-file", help="Markdown 正文文件路径；不传则从终端粘贴输入")
		parser.add_argument(
			"--draft",
			action="store_true",
			help="只保存草稿，不发布；默认会发布页面",
		)

	def handle(self, *args, **options):
		parent = self._get_parent(options.get("parent_id"))
		title = self._get_value(options.get("title"), "请输入博客标题: ").strip()
		if not title:
			raise CommandError("标题不能为空")

		slug = (options.get("slug") or slugify(title, allow_unicode=True)).strip()
		if not slug:
			raise CommandError("无法从标题生成 slug，请使用 --slug 指定")
		slug = self._make_unique_slug(parent, slug)

		tags = self._parse_tags(options.get("tags"))
		if options.get("tags") is None:
			tags = self._parse_tags(input("请输入标签（多个用英文逗号分隔，可留空）: "))

		intro = self._get_value(options.get("intro"), "请输入 intro/简介: ").strip()
		if not intro:
			raise CommandError("intro 不能为空")

		body = self._get_body(options.get("body_file"))
		if not body.strip():
			raise CommandError("Markdown 正文不能为空")

		page = BlogPage(
			title=title,
			slug=slug,
			date=timezone.localdate(),
			intro=self._to_rich_text(intro),
			body=[("markdown_block", body)],
		)

		with transaction.atomic():
			parent.add_child(instance=page)
			if tags:
				page.tags.set(*tags)
			# 重新赋值一次 body，避免后续保存/发布流程拿到被模型 save() 清空后的内存值。
			page.body = [("markdown_block", body)]
			revision = page.save_revision()
			if not options.get("draft"):
				revision.publish()

		status = "草稿已保存" if options.get("draft") else "已发布"
		self.stdout.write(self.style.SUCCESS(f"博客页面创建成功：{status}"))
		self.stdout.write(f"ID: {page.id}")
		self.stdout.write(f"标题: {page.title}")
		self.stdout.write(f"Slug: {page.slug}")
		self.stdout.write(f"父级索引页: #{parent.id} {parent.title}")
		self.stdout.write(f"标签: {', '.join(tags) if tags else '(无)'}")

	def _get_parent(self, parent_id):
		if parent_id:
			try:
				return BlogIndexPage.objects.get(id=parent_id)
			except BlogIndexPage.DoesNotExist as exc:
				raise CommandError(f"未找到 ID={parent_id} 的博客索引页") from exc

		pages = list(BlogIndexPage.objects.all().order_by("path"))
		if not pages:
			raise CommandError("没有找到任何 BlogIndexPage，请先在 Wagtail 后台创建博客索引页")

		self.stdout.write("可选博客索引页：")
		for index, page in enumerate(pages, start=1):
			live_flag = "live" if page.live else "draft"
			self.stdout.write(f"  {index}. ID={page.id} [{live_flag}] {page.title}  {page.url_path}")

		while True:
			choice = input("请选择博客索引页序号或 ID: ").strip()
			if not choice:
				continue
			if not choice.isdigit():
				self.stdout.write(self.style.ERROR("请输入数字序号或页面 ID"))
				continue
			value = int(choice)
			if 1 <= value <= len(pages):
				return pages[value - 1]
			for page in pages:
				if page.id == value:
					return page
			self.stdout.write(self.style.ERROR("未匹配到该序号或页面 ID，请重新输入"))

	def _get_value(self, value, prompt):
		if value is not None:
			return value
		return input(prompt)

	def _get_body(self, body_file):
		if body_file:
			path = Path(body_file)
			if not path.exists():
				raise CommandError(f"正文文件不存在：{body_file}")
			return path.read_text(encoding="utf-8")

		self.stdout.write("请输入 Markdown 正文。粘贴完成后，单独输入一行 END 结束：")
		lines = []
		for line in sys.stdin:
			if line.rstrip("\n\r") == "END":
				break
			lines.append(line)
		return "".join(lines).rstrip()

	def _parse_tags(self, raw_tags):
		if not raw_tags:
			return []
		return [tag.strip() for tag in raw_tags.split(",") if tag.strip()]

	def _make_unique_slug(self, parent, slug):
		base_slug = slug
		counter = 2
		while parent.get_children().filter(slug=slug).exists():
			slug = f"{base_slug}-{counter}"
			counter += 1
		return slug

	def _to_rich_text(self, text):
		return linebreaks(escape(text))
