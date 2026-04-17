# blog/management/commands/test_search.py
from django.core.management.base import BaseCommand
from wagtailblog3.mongo import MongoManager
import jieba

# 添加一个简单的调试命令来测试不同的搜索词：
# 指令：python manage.py test_search "大佬"
class Command(BaseCommand):
	help = '测试MongoDB中文搜索'
	
	def add_arguments(self, parser):
		parser.add_argument('query', type=str, help='搜索关键词')
	
	def handle(self, *args, **options):
		query = options['query']
		self.stdout.write(f'测试搜索: "{query}"')
		
		# 对查询进行分词
		seg_list = jieba.cut(query, cut_all=False)
		tokens = " ".join(seg_list)
		self.stdout.write(f'分词结果: {tokens}')
		
		# 执行搜索
		mongo = MongoManager()
		results = mongo.search_blog_content(query)
		
		# 显示结果
		self.stdout.write(f'找到 {len(results)} 个结果:')
		for i, doc in enumerate(results):
			self.stdout.write(f'  {i + 1}. 标题: {doc.get("title")}')
			self.stdout.write(f'     页面ID: {doc.get("page_id")}')
			self.stdout.write(f'     相关性分数: {doc.get("score", "N/A")}')
		
		# 尝试正则表达式搜索
		self.stdout.write('\n尝试正则表达式搜索:')
		try:
			word_list = list(jieba.cut(query, cut_all=False))
			regex_patterns = [f'.*{word}.*' for word in word_list]
			
			or_conditions = []
			for field in ['title', 'intro', 'body_text']:
				for pattern in regex_patterns:
					or_conditions.append({field: {'$regex': pattern, '$options': 'i'}})
			
			results = mongo.blog_content.find({'$or': or_conditions})
			
			result_list = []
			for doc in results:
				doc['_id'] = str(doc['_id'])
				result_list.append(doc)
			
			self.stdout.write(f'找到 {len(result_list)} 个结果:')
			for i, doc in enumerate(result_list):
				self.stdout.write(f'  {i + 1}. 标题: {doc.get("title")}')
				self.stdout.write(f'     页面ID: {doc.get("page_id")}')
		except Exception as e:
			self.stdout.write(f'正则搜索错误: {e}')