# blog/management/commands/update_mongodb_tokens.py
# 用于更新现有MongoDB文档，添加中文分词支持

from django.core.management.base import BaseCommand
from wagtailblog3.mongo import MongoManager

#
# 更新管理命令更新历史数据：
class Command(BaseCommand):
	help = '更新MongoDB中的文档，添加中文分词字段'
	
	def handle(self, *args, **options):
		mongo = MongoManager()
		
		# 获取所有文档
		# docs = mongo.blog_content.find({})
		
		# 获取所有没有分词字段的文档
		docs = mongo.blog_content.find({
			'$or': [
				{'title_tokens': {'$exists': False}},
				{'intro_tokens': {'$exists': False}}
			]
		})

		count = 0
		for doc in docs:
			updates = {}
			
			# 为标题添加分词
			if 'title' in doc and doc['title'] and ('title_tokens' not in doc or not doc['title_tokens']):
				updates['title_tokens'] = mongo._process_chinese_text(doc['title'])
			
			# 为简介添加分词
			if 'intro' in doc and doc['intro'] and ('intro_tokens' not in doc or not doc['intro_tokens']):
				updates['intro_tokens'] = mongo._process_chinese_text(doc['intro'])
			
			# 更新body_text
			if 'body' in doc and ('body_text' not in doc or not doc['body_text']):
				updates['body_text'] = mongo._extract_text_from_body(doc['body'])
			
			if updates:
				mongo.blog_content.update_one(
					{'_id': doc['_id']},
					{'$set': updates}
				)
				count += 1
		
		self.stdout.write(self.style.SUCCESS(f'成功更新 {count} 个文档'))