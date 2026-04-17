# blog/management/commands/check_mongodb_search.py
from django.core.management.base import BaseCommand
from wagtailblog3.mongo import MongoManager
import jieba

# 指令：python manage.py check_mongodb_search
class Command(BaseCommand):
	help = '检查MongoDB中文搜索配置'
	
	def handle(self, *args, **options):
		mongo = MongoManager()
		
		# 检查索引
		self.stdout.write('MongoDB索引:')
		indexes = list(mongo.blog_content.list_indexes())
		for i, index in enumerate(indexes):
			self.stdout.write(f'  {i + 1}. {index}')
		
		# 取一个示例文档检查
		self.stdout.write('\n示例文档:')
		sample = mongo.blog_content.find_one({})
		if sample:
			self.stdout.write(f'  ID: {sample.get("_id")}')
			self.stdout.write(f'  标题: {sample.get("title")}')
			self.stdout.write(f'  标题分词: {sample.get("title_tokens")}')
			self.stdout.write(f'  简介: {sample.get("intro")}')
			self.stdout.write(f'  简介分词: {sample.get("intro_tokens")}')
			self.stdout.write(f'  内容文本长度: {len(str(sample.get("body_text", "")))}')
		else:
			self.stdout.write('  未找到文档')
		
		# 测试搜索
		if sample and sample.get("title"):
			# 从标题中提取一个词进行测试搜索
			sample_word = list(jieba.cut(sample.get("title")))[0]
			self.stdout.write(f'\n测试搜索词: {sample_word}')
			results = mongo.search_blog_content(sample_word)
			self.stdout.write(f'  找到 {len(results)} 个结果')