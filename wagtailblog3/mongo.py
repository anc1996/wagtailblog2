# wagtailblog3/mongo.py
# 基于MongoDB社区版的中文搜索优化实现
import re

import pymongo
import json, logging
from django.conf import settings
from bson import ObjectId, json_util
from datetime import datetime
import jieba  # 需要先 pip install jieba

# 设置日志记录器
logger = logging.getLogger(__name__)


class MongoManager:
	"""MongoDB 操作管理类，优化中文搜索支持"""
	
	_instance = None
	
	def __new__(cls):
		# 单例模式，确保只有一个MongoManager实例
		if cls._instance is None:
			cls._instance = super(MongoManager, cls).__new__(cls)
			cls._instance._connect()
		return cls._instance
	
	def _connect(self):
		"""连接到 MongoDB"""
		mongo_settings = settings.MONGO_DB
		try:
			client = pymongo.MongoClient(
				host=mongo_settings['HOST'],
				port=mongo_settings['PORT'],
				serverSelectionTimeoutMS=5000  # 添加服务器选择超时
			)
			
			# 验证连接
			client.server_info() # 触发连接异常，如果无法连接会抛出异常
			
			self.db = client[mongo_settings['NAME']]
			self.blog_content = self.db['blog_content']
			
			# 创建索引
			self._ensure_indexes()
			
			logger.info("MongoDB连接成功")
		except Exception as e:
			logger.error(f"MongoDB连接失败: {e}")
			raise
	
	def _ensure_indexes(self):
		"""确保必要的索引存在，优化中文搜索支持"""
		try:
			# 创建page_id索引
			self.blog_content.create_index("page_id", unique=True)
			
			# 创建全文搜索索引，使用分词后的字段
			# 我们显式地为它命名，并设置默认语言为 'none'
			self.blog_content.create_index([
				("title_tokens", pymongo.TEXT),
				("intro_tokens", pymongo.TEXT),
				("body_text", pymongo.TEXT)
			],
			name="full_text_search_index", # <--- 显式命名
			weights={
				"title_tokens": 10,
				"intro_tokens": 5,
				"body_text": 1
			},
			default_language="none" # <--- 设置为'none'，因为我们自己用jieba分词
			)
			
			logger.info("MongoDB索引创建成功")
		except Exception as e:
			logger.error(f"MongoDB索引创建失败: {e}")
	
	def inspect_indexes(self):
		"""检查并输出当前集合的索引配置"""
		try:
			indexes = list(self.blog_content.list_indexes())
			logger.info(f"MongoDB集合 blog_content 共有 {len(indexes)} 个索引:")
			
			for i, index in enumerate(indexes):
				logger.info(f"索引 {i + 1}: {index}")
			
			return indexes
		except Exception as e:
			logger.error(f"检查索引时出错: {e}")
			return []
	
	def _process_chinese_text(self, text):
		"""对中文文本进行分词处理，返回空格分隔的词语"""
		if not text or not isinstance(text, str):
			return ""
		
		# 使用jieba分词
		seg_list = jieba.cut_for_search(text)
		return " ".join(seg_list)
	
	def _prepare_for_mongo(self, data):
		"""
			将数据准备为可存储到MongoDB的格式
			处理StreamField的RawDataView对象和其他不可直接序列化的类型
		"""
		# 处理None值
		if data is None:
			return None
		
		# 处理数据类型
		if isinstance(data, dict):
			"""递归处理字典中的每个键值对，确保所有数据都是MongoDB可序列化的"""
			result = {}
			for key, value in data.items():
				result[key] = self._prepare_for_mongo(value)
			return result
		
		elif isinstance(data, list):
			return [self._prepare_for_mongo(item) for item in data]
		
		elif hasattr(data, 'isoformat') and callable(data.isoformat):
			# 处理日期时间对象
			return data.isoformat()
		
		elif hasattr(data, 'raw_data') and callable(getattr(data, 'raw_data')):
			# 处理StreamValue对象
			return self._prepare_for_mongo(data.raw_data)
		
		elif hasattr(data, '__iter__') and not isinstance(data, (str, bytes, dict)):
			# 处理其他可迭代对象
			return [self._prepare_for_mongo(item) for item in data]
		else:
			# 处理基本类型
			return data
	
	def save_blog_content(self, content_data, content_id=None):
		"""保存博客内容到 MongoDB，添加中文分词处理"""
		# 预处理数据，确保所有数据都是MongoDB可序列化的
		prepared_data = self._prepare_for_mongo(content_data)
		
		# 提取纯文本用于全文搜索
		body_text = self._extract_text_from_body(prepared_data.get('body', []))
		prepared_data['body_text'] = body_text
		
		# 对标题和介绍也进行分词处理
		if 'title' in prepared_data and prepared_data['title']:
			prepared_data['title_tokens'] = self._process_chinese_text(prepared_data['title'])
		
		if 'intro' in prepared_data and prepared_data['intro']:
			prepared_data['intro_tokens'] = self._process_chinese_text(prepared_data['intro'])
		
		# 添加时间戳
		prepared_data['updated_at'] = datetime.now().isoformat()
		
		try:
			if content_id:
				# 更新现有内容
				mongo_id = ObjectId(content_id)
				result = self.blog_content.update_one(
					{'_id': mongo_id},
					{'$set': prepared_data}
				)
				if result.matched_count == 0: # 如果没有匹配到内容
					logger.warning(f"未找到MongoDB内容ID: {content_id}, 创建新内容")
					result = self.blog_content.insert_one(prepared_data)
					return str(result.inserted_id)
				return content_id
			else:
				# 创建新内容
				result = self.blog_content.insert_one(prepared_data)
				return str(result.inserted_id)
		except Exception as e:
			logger.error(f"MongoDB保存内容错误: {e}")
			# 尝试更严格的JSON序列化
			try:
				json_str = json.dumps(prepared_data, default=json_util.default) # 使用json_util处理MongoDB特有类型,json_util.default表示处理ObjectId等类型
				clean_data = json.loads(json_str)
				result = self.blog_content.insert_one(clean_data)
				return str(result.inserted_id)
			except Exception as e2:
				logger.error(f"MongoDB二次尝试保存内容错误: {e2}")
				raise
	
	def _extract_text_from_body(self, body):
		"""从BlogPage的body中提取纯文本用于全文搜索，支持中文分词"""
		if not body or not isinstance(body, list):
			return ""
		
		text_parts = []
		
		for block in body:
			if not isinstance(block, dict):
				continue
			
			block_type = block.get('type')
			block_value = block.get('value')
			
			if not block_value:
				continue
			
			# 根据BlogPage中定义的块类型提取文本
			if block_type == 'rich_text':
				# 富文本块
				from bs4 import BeautifulSoup
				try:
					soup = BeautifulSoup(str(block_value), 'html.parser') # 使用BeautifulSoup解析HTML
					text_parts.append(soup.get_text(separator=' ', strip=True)) # 获取纯文本
				except:
					text_parts.append(str(block_value)) # 如果解析失败，直接转换为字符串
			
			elif block_type == 'code_block':
				# 代码块，提取代码本身用于搜索
				if isinstance(block_value, dict) and 'code' in block_value:
					text_parts.append(str(block_value['code']))
			
			elif block_type == 'markdown_block':
				# Markdown块
				# wagtail-markdown可能以不同方式存储内容
				if isinstance(block_value, dict) and 'raw' in block_value:
					text_parts.append(str(block_value['raw']))
				else:
					text_parts.append(str(block_value))
			
			elif block_type == 'embed_block':
				# 嵌入块 - 可能包含标题或描述
				if isinstance(block_value, dict):
					if 'title' in block_value:
						text_parts.append(str(block_value['title']))
					if 'description' in block_value:
						text_parts.append(str(block_value['description']))
				elif hasattr(block_value, 'title'):
					text_parts.append(str(block_value.title))
			
			elif block_type == 'table_block':
				# 表格块
				if isinstance(block_value, dict) and 'data' in block_value:
					for row in block_value['data']:
						row_text = ' '.join(str(cell) for cell in row)
						text_parts.append(row_text)
				elif isinstance(block_value, list):
					for row in block_value:
						if isinstance(row, list):
							row_text = ' '.join(str(cell) for cell in row)
							text_parts.append(row_text)
			
			elif block_type == 'raw_html':
				# 原始HTML块
				from bs4 import BeautifulSoup
				try:
					soup = BeautifulSoup(str(block_value), 'html.parser')
					text_parts.append(soup.get_text(separator=' ', strip=True))
				except:
					text_parts.append(str(block_value))
			
			elif block_type in ['document_block', 'image_block', 'audio_block', 'video_block']:
				# 对于媒体块，尝试获取标题或其他元数据
				if hasattr(block_value, 'title'):
					text_parts.append(str(block_value.title))
				elif isinstance(block_value, dict) and 'title' in block_value:
					text_parts.append(str(block_value['title']))
		
		# 提取文本并进行中文分词处理
		extracted_text = ' '.join(text_parts)
		return self._process_chinese_text(extracted_text)
	
	def get_blog_content(self, content_id):
		"""从 MongoDB 获取博客内容"""
		if not content_id:
			return None
		
		try:
			mongo_id = ObjectId(content_id)
			content = self.blog_content.find_one({'_id': mongo_id})
			
			# 如果找到文档，返回文档内容
			if content:
				# 转换ObjectId为字符串，便于JSON序列化
				content['_id'] = str(content['_id'])
				return content
			else:
				logger.warning(f"MongoDB中未找到内容ID: {content_id}")
				return None
		except Exception as e:
			logger.error(f"MongoDB获取内容错误: {e}")
			return None
	
	def delete_blog_content(self, content_id):
		"""从 MongoDB 删除博客内容"""
		if not content_id:
			return False
		
		try:
			mongo_id = ObjectId(content_id)
			result = self.blog_content.delete_one({'_id': mongo_id})
			deleted = result.deleted_count > 0
			
			if deleted:
				logger.info(f"成功删除MongoDB内容ID: {content_id}")
			else:
				logger.warning(f"MongoDB中未找到要删除的内容ID: {content_id}")
			
			return deleted
		except Exception as e:
			logger.error(f"MongoDB删除内容错误: {e}")
			return False
	
	def search_blog_content(self, query):
		"""
		【最终优化版】在 MongoDB 中搜索博客内容，采用三阶段搜索策略，兼顾精确、性能和召回率。
		"""
		
		try:
			# 对用户查询进行一次分词，后续流程将复用此结果
			processed_query = self._process_chinese_text(query)
			
			# =================================================================
			# 第一阶段：基于分词的“精确短语搜索”
			# =================================================================
			exact_phrase_query = f'"{processed_query}"'
			logger.info(f"阶段一：尝试精确短语搜索: {exact_phrase_query}")
			
			exact_results = list(self.blog_content.find(
				{'$text': {'$search': exact_phrase_query}},
				{'score': {'$meta': 'textScore'}}
			)) # 包含title_tokens, intro_tokens, body_text的全文索引
			
			if exact_results:
				logger.info(f"精确短语搜索找到 {len(exact_results)} 个结果，直接返回。")
				final_list = [json.loads(json_util.dumps(doc)) for doc in exact_results]
				final_list.sort(key=lambda x: x.get('score', 0), reverse=True)
				return final_list
			
			# =================================================================
			# 第二阶段：分词模糊搜索
			# =================================================================
			logger.info(f"阶段二：精确短语搜索未找到结果，开始分词模糊搜索。")
			
			# 复用已分词的 processed_query
			fuzzy_results = list(self.blog_content.find(
				{'$text': {'$search': processed_query}},  # 注意这里不再需要引号
				{'score': {'$meta': 'textScore'}}
			).sort([('score', {'$meta': 'textScore'})]))
			
			if fuzzy_results:
				logger.info(f"分词模糊搜索找到 {len(fuzzy_results)} 个结果。")
				return [json.loads(json_util.dumps(doc)) for doc in fuzzy_results]
			
			# =================================================================
			# 第三阶段：正则表达式回退搜索
			# =================================================================
			logger.info(f"阶段三：分词搜索未找到结果，尝试正则表达式搜索。")
			return self._regex_search_fallback(query)
		
		except Exception as e:
			logger.error(f"MongoDB搜索过程发生错误: {e}")
			logger.info("因发生错误，尝试正则表达式回退搜索。")
			return self._regex_search_fallback(query)
	
	def _regex_search_fallback(self, query):
		"""正则表达式搜索回退方法"""
		try:
			# 分词后进行正则匹配
			words = jieba.cut(query, cut_all=False)
			word_list = list(words)
			logger.info(f"搜索词分词结果: {word_list}")
			
			regex_patterns = [f'.*{word}.*' for word in word_list]
			
			or_conditions = []
			for field in ['title', 'intro', 'body_text']:
				for pattern in regex_patterns:
					or_conditions.append({field: {'$regex': pattern, '$options': 'i'}})
			
			results = self.blog_content.find({'$or': or_conditions})
			
			result_list = []
			for doc in results:
				doc['_id'] = str(doc['_id'])
				# 为正则搜索结果添加默认分数，以便与文本搜索结果合并
				if 'score' not in doc:
					doc['score'] = 0.5  # 给予一个默认的相关性分数
				result_list.append(doc)
			
			logger.info(f"MongoDB正则搜索 '{query}' 找到 {len(result_list)} 个结果")
			return result_list
		
		except Exception as e:
			logger.error(f"MongoDB备用搜索错误: {e}")
			return []