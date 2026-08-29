# wagtailblog3/mongo.py
# 彻底移除 jieba 分词与 $text 搜索，仅保留纯净的 KV 存储与 Wagtail 原生网关对接
import hashlib
import uuid

import pymongo
import json, logging
from django.conf import settings
from bson import ObjectId, json_util
from datetime import datetime
from django.utils import timezone

# 设置日志记录器
logger = logging.getLogger(__name__)


class MongoRevisionReadError(Exception):
	"""Mongo 历史正文读取失败的基类，调用方可据此阻止静默回退。"""
	code = "revision_body_unavailable"
	retryable = False


class MongoRevisionPointerError(MongoRevisionReadError):
	"""历史正文指针为空或不是支持的指针类型。"""

	code = "revision_pointer_invalid"


class MongoRevisionNotFoundError(MongoRevisionReadError):
	"""历史正文指针格式有效，但 MongoDB 中不存在对应快照。"""

	code = "revision_snapshot_missing"


class MongoRevisionBodyError(MongoRevisionReadError):
	"""Mongo 快照存在但缺少可恢复的 ``body`` 字段。"""

	code = "revision_body_invalid"


class MongoRevisionUnavailableError(MongoRevisionReadError):
	"""读取历史正文时 MongoDB 或网络暂时不可用。"""

	code = "revision_store_unavailable"
	retryable = True


class MongoBodyVersionPointerError(MongoRevisionReadError):
	"""不可变正文版本标识或聚合身份无效。"""

	code = "body_version_pointer_invalid"


class MongoBodyVersionNotFoundError(MongoRevisionReadError):
	"""不可变正文版本不存在，不能以其他正文替代。"""

	code = "body_version_missing"


class MongoBodyVersionBodyError(MongoRevisionReadError):
	"""不可变正文版本的身份、哈希、模式或正文格式不可信。"""

	code = "body_version_invalid"


class MongoBodyVersionUnavailableError(MongoRevisionReadError):
	"""MongoDB 暂时不可用，读取不可变正文版本可稍后重试。"""

	code = "body_version_store_unavailable"
	retryable = True


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
			self.content_body_versions = self.db['content_body_versions']
			
			# 创建索引
			self._ensure_indexes()
			logger.info("MongoDB连接成功（已剥离 jieba 搜索包袱）")
		except Exception as e:
			logger.error(f"MongoDB连接失败: {e}", exc_info=True)
			raise
	
	def _ensure_indexes(self):
		"""确保必要的索引存在"""
		try:
			self.blog_content.create_index("page_id", unique=True)
			# 同一聚合的相同规范化正文只能拥有一个不可变版本，重复写入只复用既存记录。
			self.content_body_versions.create_index(
				[("aggregate_type", pymongo.ASCENDING), ("aggregate_id", pymongo.ASCENDING), ("body_sha256", pymongo.ASCENDING), ("body_schema_version", pymongo.ASCENDING)],
				unique=True,
				name="content_body_versions_aggregate_hash_unique",
			)
			self.content_body_versions.create_index(
				"body_version_id",
				unique=True,
				name="content_body_versions_id_unique",
			)
			# 尝试删除旧的文本搜索索引（如果存在的话）
			try:
				self.blog_content.drop_index("full_text_search_index")
			except pymongo.errors.OperationFailure:
				pass  # 如果不存在就忽略
		except Exception as e:
			logger.error(f"MongoDB索引创建失败: {e}", exc_info=True)
	
	def inspect_indexes(self):
		try:
			indexes = list(self.blog_content.list_indexes())
			
			logger.info(f"MongoDB集合 blog_content 共有 {len(indexes)} 个索引:")
			for i, index in enumerate(indexes):
				logger.info(f"索引 {i + 1}: {index}")
			
			return indexes
		except Exception as e:
			logger.error(f"检查索引时出错: {e}", exc_info=True)
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
			logger.error(f"MongoDB保存内容错误: {e}", exc_info=True)
			try:
				json_str = json.dumps(prepared_data, default=json_util.default) # 使用 json_util 处理 ObjectId
				clean_data = json.loads(json_str)
				result = self.blog_content.insert_one(clean_data)
				return str(result.inserted_id)
			except Exception as e2:
				logger.error(f"MongoDB二次尝试保存内容错误: {e2}", exc_info=True)
				raise
	
	def _generate_body_hash(self, prepared_body):
		"""
		【新增】辅助方法：计算标准化正文的 MD5 Hash 值
		用于精准比对 MongoDB 中的大文本是否发生实质性修改
		"""
		try:
			# sort_keys=True 保证字典无序性带来的 Hash 差异被抹平
			hash_str = json.dumps(prepared_body, sort_keys=True)
			return hashlib.md5(hash_str.encode('utf-8')).hexdigest()
		except Exception as e:
			logger.error(f"Hash 计算失败: {e}", exc_info=True)
			return None
	
	# =============================================================================
	# 异构草稿/历史快照专属持久化网关（无 page_id 唯一索引限制）
	# =============================================================================
	def save_blog_revision_body(self, page_id, body_data):
		"""将草稿/历史版本的 StreamField 原始数据，存入专属的历史大文本集合中"""
		# 预处理数据，确保所有特殊类型数据都是 MongoDB 可序列化的
		prepared_body = self._prepare_for_mongo(body_data)
		
		# =====================================================================
		# 🚀 核心升级：底层防线 - MongoDB 级别的草稿去重拦截
		# =====================================================================
		try:
			current_hash = self._generate_body_hash(prepared_body)
			
			if current_hash:
				# 倒序查找该 page_id 在 MongoDB 中的最后一次草稿快照
				latest_draft = self.blog_revisions.find_one(
					{'page_id': page_id},
					sort=[('created_at', pymongo.DESCENDING)]
				)
				
				if latest_draft and 'body' in latest_draft:
					latest_hash = self._generate_body_hash(latest_draft['body'])
					
					# 【拦截生效】：如果本次正文跟上一次在 MongoDB 里的正文 Hash 完全一致
					if current_hash == latest_hash:
						reused_oid = str(latest_draft['_id'])
						logger.info(
							f"🛡️ MongoDB 存储引擎拦截: Page [{page_id}] 正文未变，拒绝冗余写入，复用历史指针 OID [{reused_oid}]")
						# 欺骗上层视图，假装存成功了，把旧的 OID 还给它
						return reused_oid
		except Exception as e:
			logger.warning(f"草稿去重 Hash 比对异常，降级为常规插入: {e}")
		# =====================================================================
		
		# 如果没有拦截（全新内容，或者 Hash 发生改变），则正常插入 MongoDB
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
			logger.error(f"MongoDB 持久化历史草稿错误: {e}", exc_info=True)
			raise
	
	def get_blog_revision_body(self, content_id: str | ObjectId | None) -> dict[str, object]:
		"""按快照指针读取历史正文，并保留可区分的失败语义。

		参数：``content_id`` 可以是 BSON ``ObjectId``、24 位十六进制字符串，
		也可以是早期实现生成的 ``rev_<page>_<uuid>`` 字符串 ``_id``。
		返回：包含 ``page_id``、``body`` 等字段的 Mongo 文档，``_id`` 统一转为字符串。
		异常：空指针或不支持的类型抛出 :class:`MongoRevisionPointerError`；
		指针格式有效但快照不存在抛出 :class:`MongoRevisionNotFoundError`；
		快照存在但缺少 ``body`` 字段抛出 :class:`MongoRevisionBodyError`；
		MongoDB 查询失败抛出 :class:`MongoRevisionUnavailableError`。
		副作用：只执行一次 ``find_one`` 读取，不写入或删除任何数据。
		"""
		if content_id is None or content_id == "":
			raise MongoRevisionPointerError("历史正文指针为空")

		if isinstance(content_id, ObjectId):
			query_id: ObjectId | str = content_id
		elif isinstance(content_id, str):
			pointer = content_id.strip()
			if not pointer:
				raise MongoRevisionPointerError("历史正文指针为空")
			# 新版本使用 BSON ObjectId；旧版本的 rev_<page>_<uuid> 保持字符串 _id。
			query_id = ObjectId(pointer) if ObjectId.is_valid(pointer) else pointer
		else:
			raise MongoRevisionPointerError("历史正文指针类型不受支持")

		revision_collection = getattr(self, "blog_revisions", None)
		if revision_collection is None:
			raise MongoRevisionUnavailableError("历史正文集合未初始化")

		try:
			content = revision_collection.find_one({"_id": query_id})
		except Exception as exc:
			logger.error("MongoDB 读取历史草稿失败，错误类型=%s", type(exc).__name__, exc_info=True)
			raise MongoRevisionUnavailableError("MongoDB 历史正文读取失败") from exc

		if content is None:
			logger.warning("MongoDB 草稿集合中未找到指定的历史正文快照")
			raise MongoRevisionNotFoundError("MongoDB 中不存在对应历史正文快照")
		if not isinstance(content, dict) or "body" not in content or content["body"] is None:
			logger.error("MongoDB 历史草稿快照缺少 body 字段")
			raise MongoRevisionBodyError("MongoDB 历史正文快照缺少 body")

		body_data = content["body"]
		if isinstance(body_data, str):
			try:
				body_data = json.loads(body_data)
			except json.JSONDecodeError as exc:
				logger.error("MongoDB 历史草稿正文不是有效 JSON")
				raise MongoRevisionBodyError("MongoDB 历史正文 JSON 无效") from exc
			content["body"] = body_data

		if not isinstance(body_data, list):
			logger.error("MongoDB 历史草稿正文不是列表")
			raise MongoRevisionBodyError("MongoDB 历史正文格式无效")

		content["_id"] = str(content["_id"])
		return content
	
	def delete_single_revision(self, pointer_id, *, raise_on_error: bool = False):
		"""删除单条历史快照，并兼容 ObjectId 与早期字符串主键。

		参数：``raise_on_error`` 仅供清理任务区分存储故障与不存在；既有调用保持
		返回 ``False`` 的兼容语义。返回：找到并删除时为 ``True``，不存在时为 ``False``。
		"""
		# 🚨 修复点：PyMongo 强制要求使用 is None
		if not pointer_id or self.blog_revisions is None:
			return False

		try:
			pointer_text = str(pointer_id)
			mongo_id = pointer_id if isinstance(pointer_id, ObjectId) else (
				ObjectId(pointer_text) if ObjectId.is_valid(pointer_text) else pointer_text
			)
			result = self.blog_revisions.delete_one({'_id': mongo_id})
			return result.deleted_count > 0
		except Exception as e:
			logger.error(f"MongoDB删除单条快照错误: {e}", exc_info=True)
			if raise_on_error:
				raise
			return False
	
	# =============================================================================
	# ⚠️ 运维/灾备专属工具方法 (非业务常规逻辑)
	# =============================================================================
	def save_page_revision(self, pointer_id, page_id, body_data):
		"""
		【警告：这不是 Wagtail 正常的保存/发布流程调用的方法！】

		常规的保存草稿逻辑走的是 `save_blog_revision_body`（MongoDB 自动生成新 OID）。
		这个方法的作用是：被动接收一个已经存在的 `pointer_id`，并强制写入 MongoDB。

		主要适用场景（运维与灾备）：
		1. 数据恢复 (Data Restoration)：当 MongoDB 数据丢失，需要从 MySQL 备份的
		   wagtailcore_revision 表中提取出历史 OID，并重新把内容灌回 MongoDB 时。
		2. 数据迁移 (Migration)：跨集群同步时，需要保持两侧草稿 ID 绝对一致。
		3. 脏数据修复脚本 (Management Commands)：用于编写后台脚本修复断链的草稿。
		"""
		# 🚨 修复点：PyMongo 强制要求使用 is None
		if self.blog_revisions is None:
			return False
		
		try:
			# 使用强行指定的 pointer_id 作为 _id 进行插入
			self.blog_revisions.insert_one({
				'_id': pointer_id,
				'page_id': page_id,
				'body': body_data,
				'created_at': timezone.now().isoformat(),
				'is_restored': True  # 可选：打上一个恢复标记，方便日后排查
			})
			logger.info(f"🔧 运维操作：成功强制写入/恢复历史快照，指定 OID [{pointer_id}]")
			return True
		except Exception as e:
			logger.error(f"🔧 运维操作：强制保存历史快照失败，指定 OID [{pointer_id}]: {e}", exc_info=True)
			return False
	
	def get_blog_content(self, content_id):
		return self.get_blog_content_compatible(content_id)

	def get_blog_content_compatible(self, content_id=None, page_id=None):
		"""优先按标准 ObjectId 读取，历史 pointer 失配时按唯一 page_id 兼容读取。"""
		content = None
		try:
			if content_id and ObjectId.is_valid(str(content_id)):
				content = self.blog_content.find_one({'_id': ObjectId(str(content_id))})
			if content is None and page_id is not None:
				content = self.blog_content.find_one({'page_id': page_id})
			if content:
				content['_id'] = str(content['_id'])
			return content
		except Exception as e:
			logger.error(f"MongoDB读取正文错误: {e}", exc_info=True)
			return None

	def _canonical_body_json(self, prepared_body: list[object]) -> str:
		"""生成跨进程稳定的正文 JSON，用于不可变版本身份和完整性校验。

		参数：``prepared_body`` 必须是已通过 Mongo 预处理的 StreamField 列表。
		返回：键排序、无空白且 UTF-8 语义固定的 JSON 文本。
		异常：不可序列化的数据由调用方转换为正文版本不可用错误，不能退化为非确定性哈希。
		"""
		return json.dumps(
			prepared_body,
			sort_keys=True,
			ensure_ascii=False,
			separators=(",", ":"),
			default=json_util.default,
		)

	def _canonical_body_sha256(self, prepared_body: list[object]) -> str:
		"""计算规范化正文的 SHA-256；相同逻辑正文在同一聚合中可幂等复用。"""
		return hashlib.sha256(self._canonical_body_json(prepared_body).encode("utf-8")).hexdigest()

	def save_content_body_version(
		self,
		aggregate_type: str,
		aggregate_id: int | str,
		body_data: list[object],
		*,
		body_schema_version: int = 1,
	) -> dict[str, object]:
		"""以插入一次语义保存 BlogPage 的不可变正文版本。

		参数：聚合类型、聚合主键和 StreamField 原始列表共同确定正文归属；模式版本用于拒绝未知正文格式。
		返回：包含 ``body_version_id``、``body_sha256`` 和 ``body_schema_version`` 的版本身份。
		异常：Mongo 写入或既存文档不满足不可变契约时抛出 :class:`MongoBodyVersionUnavailableError`
		或 :class:`MongoBodyVersionBodyError`；调用方不得改写既有文档或回退为其他版本。
		副作用：仅可能插入一条新文档。唯一键竞争时重新读取既存文档，不使用 ``$set`` 更新。
		"""
		if not isinstance(aggregate_type, str) or not aggregate_type.strip() or aggregate_id is None:
			raise MongoBodyVersionPointerError("不可变正文版本缺少聚合身份")
		if not isinstance(body_schema_version, int) or body_schema_version < 1:
			raise MongoBodyVersionPointerError("不可变正文模式版本无效")

		prepared_body = self._prepare_for_mongo(body_data)
		if not isinstance(prepared_body, list):
			raise MongoBodyVersionBodyError("不可变正文必须是列表")
		try:
			body_sha256 = self._canonical_body_sha256(prepared_body)
		except Exception as exc:
			raise MongoBodyVersionBodyError("不可变正文无法规范化") from exc

		collection = getattr(self, "content_body_versions", None)
		if collection is None:
			raise MongoBodyVersionUnavailableError("不可变正文集合未初始化")
		identity = {
			"aggregate_type": aggregate_type.strip(),
			"aggregate_id": str(aggregate_id),
			"body_sha256": body_sha256,
			"body_schema_version": body_schema_version,
		}
		try:
			existing = collection.find_one(identity)
			if existing is not None:
				return self._body_version_identity(existing, identity, body_schema_version)

			document = {
				**identity,
				"body_version_id": uuid.uuid4().hex,
				"body_schema_version": body_schema_version,
				"body": prepared_body,
				"created_at": timezone.now().isoformat(),
			}
			try:
				collection.insert_one(document)
			except pymongo.errors.DuplicateKeyError:
				# 并发保存相同正文时由唯一键裁决，胜者以外的请求只复用既存版本。
				existing = collection.find_one(identity)
				if existing is None:
					raise
				return self._body_version_identity(existing, identity, body_schema_version)
			return self._body_version_identity(document, identity, body_schema_version)
		except MongoRevisionReadError:
			raise
		except Exception as exc:
			logger.error("MongoDB 保存不可变正文版本失败", exc_info=True)
			raise MongoBodyVersionUnavailableError("MongoDB 不可变正文版本写入失败") from exc

	def _body_version_identity(
		self,
		document: dict[str, object],
		expected_identity: dict[str, object],
		expected_schema_version: int,
	) -> dict[str, object]:
		"""校验既存或新建版本的不可变身份，并返回可写入 Revision 的最小元数据。"""
		if (
			document.get("aggregate_type") != expected_identity["aggregate_type"]
			or str(document.get("aggregate_id")) != expected_identity["aggregate_id"]
			or document.get("body_sha256") != expected_identity["body_sha256"]
			or document.get("schema_version", document.get("body_schema_version")) != expected_schema_version
			or not isinstance(document.get("body_version_id"), str)
			or not document["body_version_id"]
		):
			raise MongoBodyVersionBodyError("不可变正文版本身份不一致")
		return {
			"body_version_id": document["body_version_id"],
			"body_sha256": document["body_sha256"],
			"body_schema_version": document.get("schema_version", document.get("body_schema_version")),
		}

	def get_content_body_version(
		self,
		aggregate_type: str,
		aggregate_id: int | str,
		body_version_id: str,
		body_sha256: str,
		body_schema_version: int,
	) -> dict[str, object]:
		"""按完整聚合身份读取并校验不可变正文版本。

		Revision 同时携带版本 ID、哈希和模式版本；任意一个与 Mongo 文档不符都视为损坏，
		避免错误地显示同页其他版本或当前正式正文。
		"""
		if (
			not isinstance(aggregate_type, str)
			or not aggregate_type.strip()
			or aggregate_id is None
			or not isinstance(body_version_id, str)
			or not body_version_id
			or not isinstance(body_sha256, str)
			or len(body_sha256) != 64
			or not isinstance(body_schema_version, int)
			or body_schema_version < 1
		):
			raise MongoBodyVersionPointerError("不可变正文版本指针无效")
		collection = getattr(self, "content_body_versions", None)
		if collection is None:
			raise MongoBodyVersionUnavailableError("不可变正文集合未初始化")
		identity = {
			"aggregate_type": aggregate_type.strip(),
			"aggregate_id": str(aggregate_id),
			"body_sha256": body_sha256,
			"body_schema_version": body_schema_version,
		}
		try:
			document = collection.find_one({**identity, "body_version_id": body_version_id})
		except Exception as exc:
			logger.error("MongoDB 读取不可变正文版本失败", exc_info=True)
			raise MongoBodyVersionUnavailableError("MongoDB 不可变正文版本读取失败") from exc
		if document is None:
			raise MongoBodyVersionNotFoundError("不可变正文版本不存在")
		self._body_version_identity(document, identity, body_schema_version)
		body_data = document.get("body")
		if not isinstance(body_data, list):
			raise MongoBodyVersionBodyError("不可变正文格式无效")
		try:
			if self._canonical_body_sha256(body_data) != body_sha256:
				raise MongoBodyVersionBodyError("不可变正文哈希校验失败")
		except MongoBodyVersionBodyError:
			raise
		except Exception as exc:
			raise MongoBodyVersionBodyError("不可变正文无法规范化") from exc
		return document

	def delete_blog_content(self, content_id, *, raise_on_error: bool = False):
		if not content_id:
			return False
		try:
			mongo_id = ObjectId(content_id)
			result = self.blog_content.delete_one({'_id': mongo_id})
			return result.deleted_count > 0
		except Exception as e:
			logger.error(f"MongoDB删除内容错误: {e}", exc_info=True)
			if raise_on_error:
				raise
			return False
	
	def delete_page_revisions(self, page_id):
		if not page_id or self.blog_revisions is None:
			return 0
		try:
			result = self.blog_revisions.delete_many({'page_id': page_id})
			return result.deleted_count
		except Exception as e:
			logger.error(f"MongoDB批量删除历史快照错误: {e}", exc_info=True)
			return 0
