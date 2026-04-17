# wagtailblog3/apps/blog/management/commands/rebuild_mongodb_indexes.py
# 最终修正版：修复了 pymongo 的 truthiness check 错误

from django.core.management.base import BaseCommand
from wagtailblog3.mongo import MongoManager
import logging

logger = logging.getLogger(__name__)


# 指令：python manage.py rebuild_mongodb_indexes
class Command(BaseCommand):
	help = '删除现有MongoDB全文索引并重建为中文优化的索引'
	
	def handle(self, *args, **options):
		mongo = MongoManager()
		
		# ===============================================================
		# 核心修正：将 `if not mongo.db:` 修改为 `if mongo.db is None:`
		# ===============================================================
		if mongo.db is None:
			self.stdout.write(self.style.ERROR('无法连接到MongoDB，请检查配置和服务状态。'))
			return
		
		self.stdout.write('开始重建MongoDB全文索引...')
		
		collection = mongo.blog_content
		
		try:
			indexes = list(collection.list_indexes())
			self.stdout.write(f'发现 {len(indexes)} 个现有索引')
			
			# 查找并删除所有现有的文本索引
			text_index_found = False
			for index in indexes:
				index_info = index.to_dict()
				# 文本索引的key中会包含 "_fts": "text"
				if "_fts" in index_info.get("key", {}):
					text_index_found = True
					index_name = index_info.get('name')
					self.stdout.write(f'删除旧的文本索引: {index_name}')
					collection.drop_index(index_name)
			
			if not text_index_found:
				self.stdout.write('未发现旧的文本索引，直接创建新索引。')
			
			# 创建新的、为中文优化的全文索引
			self.stdout.write('正在创建新的中文优化全文索引...')
			
			new_index_name = "full_text_search_index"
			
			collection.create_index(
				[
					("title_tokens", "text"),
					("intro_tokens", "text"),
					("body_text", "text")
				],
				weights={
					"title_tokens": 10,
					"intro_tokens": 5,
					"body_text": 1
				},
				name=new_index_name,
				# 关键：告诉MongoDB不要使用任何内置语言处理器
				default_language="none"
			)
			
			self.stdout.write(self.style.SUCCESS(f'成功重建MongoDB索引！新索引名为: {new_index_name}'))
		
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'重建索引时出错: {e}'))