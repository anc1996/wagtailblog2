# wagtailblog3/mongo.py
# 彻底移除 jieba 分词与 $text 搜索，仅保留纯净的 KV 存储与 Wagtail 原生网关对接
import pymongo
import json, logging
from django.conf import settings
from bson import ObjectId, json_util
from datetime import datetime
from django.utils import timezone

# 设置日志记录器
logger = logging.getLogger(__name__)


class MongoManager:
	
	"""MongoDB 操作管理类（仅做纯数据落盘，不再干预搜索）"""
	_instance = None
	
	def __new__(cls):
		# 单例模式
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
				serverSelectionTimeoutMS=5000
			)
			# 验证连接
			client.server_info()
			
			self.db = client[mongo_settings['NAME']]
			self.blog_content = self.db['blog_content']
			self.blog_revisions = self.db['blog_page_revision_bodies']
			
			# 创建索引
			self._ensure_indexes()
			logger.info("MongoDB连接成功（已剥离 jieba 搜索包袱）")
		except Exception as e:
			logger.error(f"MongoDB连接失败: {e}")
			raise
	
	def _ensure_indexes(self):
		"""确保必要的索引存在"""
		try:
			self.blog_content.create_index("page_id", unique=True)
			# 尝试删除旧的文本搜索索引（如果存在的话）
			try:
				self.blog_content.drop_index("full_text_search_index")
			except pymongo.errors.OperationFailure:
				pass  # 如果不存在就忽略
		except Exception as e:
			logger.error(f"MongoDB索引创建失败: {e}")
	
	def inspect_indexes(self):
		try:
			indexes = list(self.blog_content.list_indexes())
			
			logger.info(f"MongoDB集合 blog_content 共有 {len(indexes)} 个索引:")
			for i, index in enumerate(indexes):
				logger.info(f"索引 {i + 1}: {index}")
			
			return indexes
		except Exception as e:
			logger.error(f"检查索引时出错: {e}")
			return []
	
	def _prepare_for_mongo(self, data):
		"""
			将数据准备为可存储到MongoDB的格式
			处理StreamField的RawDataView对象和其他不可直接序列化的类型
		"""
		if data is None:
			return None
		
		
		if isinstance(data, dict):
			"""递归处理字典中的每个键值对，确保所有数据都是MongoDB可序列化的"""
			result = {}
			for key, value in data.items():
				result[key] = self._prepare_for_mongo(value)
			return result
		
		elif isinstance(data, list):
			# 递归处理列表中的每个元素
			return [self._prepare_for_mongo(item) for item in data]
		
		elif hasattr(data, 'isoformat') and callable(data.isoformat):
			# 处理 datetime 对象
			return data.isoformat()
		
		elif hasattr(data, 'raw_data') and callable(getattr(data, 'raw_data')):
			# 处理 StreamField 的 RawDataView 对象
			return self._prepare_for_mongo(data.raw_data)
		
		elif hasattr(data, '__iter__') and not isinstance(data, (str, bytes, dict)):
			# 处理其他可迭代对象（如生成器）
			return [self._prepare_for_mongo(item) for item in data]
		else:
			# 返回基本类型
			return data
	
	
	def save_blog_content(self, content_data, content_id=None):
		"""【修复点】：恢复了原有签名，移除了所有 jieba.cut 和 token 拼接"""
		prepared_data = self._prepare_for_mongo(content_data)
		
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
				if result.matched_count == 0: #  如果没有匹配到，则插入新数据
					result = self.blog_content.insert_one(prepared_data)
					return str(result.inserted_id)
				return content_id
			else:
				result = self.blog_content.insert_one(prepared_data)
				return str(result.inserted_id)
		except Exception as e:
			logger.error(f"MongoDB保存内容错误: {e}")
			try:
				json_str = json.dumps(prepared_data, default=json_util.default) # 使用 json_util 处理 ObjectId
				clean_data = json.loads(json_str)
				result = self.blog_content.insert_one(clean_data)
				return str(result.inserted_id)
			except Exception as e2:
				logger.error(f"MongoDB二次尝试保存内容错误: {e2}")
				raise
	
	
	# =============================================================================
	# 异构草稿/历史快照专属持久化网关（无 page_id 唯一索引限制）
	# =============================================================================
	def save_blog_revision_body(self, page_id, body_data):
		"""将草稿/历史版本的 StreamField 原始数据，存入专属的历史大文本集合中"""
		# 预处理数据，确保所有特殊类型数据都是 MongoDB 可序列化的
		prepared_body = self._prepare_for_mongo(body_data)
		document = {
			'page_id': page_id,
			'body': prepared_body,
			'created_at': datetime.now().isoformat()
		}
		try:
			# 核心隔离：写入 blog_revisions 集合，规避主表的 unique page_id 限制
			result = self.blog_revisions.insert_one(document)
			logger.info(f"MongoDB 历史草稿落盘成功，生成快照指针 OID: {result.inserted_id}")
			return str(result.inserted_id)
		except Exception as e:
			logger.error(f"MongoDB 持久化历史草稿错误: {e}")
			raise
	
	def get_blog_revision_body(self, content_id):
		"""从专属历史大文本集合中，通过快照指针捞回草稿内容"""
		if not content_id:
			return None
		try:
			mongo_id = ObjectId(content_id)
			content = self.blog_revisions.find_one({'_id': mongo_id})
			if content:
				content['_id'] = str(content['_id'])
				return content
			else:
				logger.warning(f"MongoDB 草稿集合中未找到指定的指针 ID: {content_id}")
				return None
		except Exception as e:
			logger.error(f"MongoDB 读取历史草稿失败: {e}")
			return None
	
	def delete_single_revision(self, pointer_id):
		"""从 MongoDB 删除单条历史草稿快照"""
		# 🚨 修复点：PyMongo 强制要求使用 is None
		if not pointer_id or self.blog_revisions is None:
			return False
		
		try:
			result = self.blog_revisions.delete_one({'_id': pointer_id})
			return result.deleted_count > 0
		except Exception as e:
			logger.error(f"MongoDB删除单条快照错误: {e}")
			return False
	
	def save_page_revision(self, pointer_id, page_id, body_data):
		"""保存单条草稿历史快照到 MongoDB"""
		# 🚨 修复点：PyMongo 强制要求使用 is None
		if self.blog_revisions is None:
			return False
		
		try:
			self.blog_revisions.insert_one({
				'_id': pointer_id,
				'page_id': page_id,
				'body': body_data,
				'created_at': timezone.now().isoformat()
			})
			return True
		except Exception as e:
			logger.error(f"MongoDB保存历史快照失败: {e}")
			return False
	
	def get_blog_content(self, content_id):
		if not content_id:
			return None
		try:
			mongo_id = ObjectId(content_id)
			content = self.blog_content.find_one({'_id': mongo_id})
			if content:
				content['_id'] = str(content['_id'])
				return content
			return None
		except Exception as e:
			logger.error(f"MongoDB获取内容错误: {e}")
			return None
	
	def delete_blog_content(self, content_id):
		if not content_id:
			return False
		try:
			mongo_id = ObjectId(content_id)
			result = self.blog_content.delete_one({'_id': mongo_id})
			return result.deleted_count > 0
		except Exception as e:
			logger.error(f"MongoDB删除内容错误: {e}")
			return False
	
	def delete_page_revisions(self, page_id):
		if not page_id or self.blog_revisions is None:
			return 0
		try:
			result = self.blog_revisions.delete_many({'page_id': page_id})
			return result.deleted_count
		except Exception as e:
			logger.error(f"MongoDB批量删除历史快照错误: {e}")
			return 0