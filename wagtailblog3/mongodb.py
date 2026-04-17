# wagtailblog3/mongodb.py

import json
import uuid
from django.core.serializers.json import DjangoJSONEncoder
from wagtail.blocks.stream_block import StreamValue
import logging
import traceback

# 设置日志记录器
logger = logging.getLogger(__name__)


class MongoDBStreamFieldAdapter:
	"""处理StreamField数据与MongoDB存储格式的转换"""
	
	@staticmethod
	def to_mongodb(stream_value):
		"""将StreamField值转换为适合MongoDB存储的格式"""
		if stream_value is None:
			logger.warning("StreamValue为None，返回空列表")
			return []
		
		# 如果是StreamValue对象
		if isinstance(stream_value, StreamValue):
			try:
				logger.debug(f"转换StreamValue到MongoDB格式，类型: {type(stream_value)}")
				# 确保我们使用raw_data
				if hasattr(stream_value, 'raw_data'):
					raw_data = stream_value.raw_data
					
					# 如果raw_data已经是列表，直接返回
					if isinstance(raw_data, list):
						logger.debug(f"raw_data是列表，包含{len(raw_data)}项")
						return list(raw_data)
				
				# 手动构建块列表
				blocks = []
				for block in stream_value:
					logger.debug(f"处理块类型: {block.block_type}")
					block_data = {
						'type': block.block_type,
						'id': block.id,
						'value': MongoDBStreamFieldAdapter._process_block_value(block)
					}
					blocks.append(block_data)
				
				logger.debug(f"成功转换{len(blocks)}个块")
				return blocks
			except Exception as e:
				logger.error(f"转换StreamField数据出错: {e}")
				logger.error(traceback.format_exc())
				# 安全返回空列表
				return []
		
		# 如果是列表，假设已经是正确的格式
		elif isinstance(stream_value, list):
			# 确保列表中的每个项都有必要的字段
			result = []
			for item in stream_value:
				if isinstance(item, dict):
					# 确保有类型字段
					if 'type' not in item:
						continue
					
					# 确保有ID
					if 'id' not in item or not item['id']:
						item['id'] = str(uuid.uuid4())
					
					# 确保有value
					if 'value' not in item:
						item['value'] = ""
					
					result.append(item)
			
			logger.debug(f"处理输入列表，包含{len(result)}项")
			return result
		
		# 其他情况，尝试转换为JSON字符串再解析
		logger.debug(f"尝试将其他类型转换为MongoDB格式: {type(stream_value)}")
		try:
			result = json.loads(json.dumps(stream_value, cls=DjangoJSONEncoder))
			return result
		except Exception as e:
			logger.error(f"JSON转换失败: {e}")
			return []
	
	@staticmethod
	def _process_block_value(block):
		"""处理特定类型块的值"""
		value = block.value
		block_type = block.block_type
		
		try:
			# 针对不同类型的块进行处理
			if block_type == 'markdown_block':
				# Markdown块特殊处理
				
				# 如果是字符串，直接返回
				if isinstance(value, str):
					return value
				
				# 如果有特殊格式，保留结构
				if isinstance(value, dict):
					return value
				
				# 其他情况，转换为字符串
				return str(value)
			
			elif block_type in ['document_block', 'image_block', 'audio_block', 'video_block']:
				# 引用类型块，保存ID
				logger.debug(f"处理引用类型块: {block_type}")
				if value and hasattr(value, 'id'):
					return value.id
				return None
			
			elif block_type == 'code_block':
				logger.debug(f"处理代码块")
				if isinstance(value, dict):
					return value
				# 如果格式不正确，记录日志并返回一个安全的默认值
				logger.warning(f"代码块的值不是预期的字典格式: {type(value)}")
				return {'language': 'plaintext', 'code': str(value)}
			
			elif block_type in ['rich_text', 'raw_html']:
				# 文本类型块，直接返回值
				logger.debug(f"处理文本类型块: {block_type}")
				return str(value)
			
			elif block_type == 'embed_block':
				# 嵌入块，可能是URL或配置对象
				if hasattr(value, 'url'):
					return {'url': value.url, 'title': getattr(value, 'title', ''), 'html': getattr(value, 'html', '')}
				return value
			
			elif block_type == 'table_block':
				# 表格块，确保数据格式正确
				logger.debug(f"处理表格块")
				if isinstance(value, dict) and 'data' in value:
					return value
				return {'data': value if isinstance(value, list) else []}
			
			# 其他类型的通用处理
			else:
				logger.debug(f"处理通用块类型: {block_type}")
				if hasattr(value, 'id'):
					return value.id
				elif isinstance(value, dict):
					return value
				elif hasattr(value, '__iter__') and not isinstance(value, str):
					return list(value)
				else:
					return str(value)
		except Exception as e:
			logger.error(f"处理块值时出错: {e}, 块类型: {block_type}")
			# 安全返回None
			return None
	
	@staticmethod
	def from_mongodb(data, stream_block):
		"""将MongoDB中的数据转换回StreamValue对象"""
		logger.debug(f"开始从MongoDB数据创建StreamValue")
		
		if not data:
			logger.warning("MongoDB数据为空，返回空StreamValue")
			return StreamValue(stream_block, [], is_lazy=True)
		
		if not isinstance(data, list):
			logger.warning(f"MongoDB数据不是列表格式: {type(data)}")
			# 尝试转换成列表
			try:
				if isinstance(data, str):
					data = json.loads(data)
				else:
					data = []
			except:
				logger.error("无法将数据转换为列表")
				data = []
		
		try:
			# 确保数据格式正确
			processed_data = []
			
			for i, item in enumerate(data):
				if not isinstance(item, dict):
					logger.warning(f"跳过非字典项 #{i}: {type(item)}")
					continue
				
				# 确保必要的字段存在
				if 'type' not in item:
					logger.warning(f"项 #{i} 缺少type字段")
					continue
				
				block_data = item.copy()  # 复制，避免修改原始数据
				
				# 确保value字段存在
				if 'value' not in block_data:
					logger.warning(f"项 #{i} 缺少value字段，添加空值")
					block_data['value'] = ""
				
				# 确保有ID
				if 'id' not in block_data or not block_data['id']:
					block_data['id'] = str(uuid.uuid4())
					logger.debug(f"为项 #{i} 生成ID: {block_data['id']}")
				
				# 特殊处理Markdown块
				if block_data['type'] == 'markdown_block':
					logger.debug(f"特殊处理Markdown块 #{i}")
					if isinstance(block_data['value'], dict):
						# 如果是字典格式，尝试提取内容
						if 'raw' in block_data['value']:
							block_data['value'] = block_data['value']['raw']
						elif 'content' in block_data['value']:
							block_data['value'] = block_data['value']['content']
						elif 'value' in block_data['value']:
							block_data['value'] = block_data['value']['value']
						else:
							# 转为字符串
							block_data['value'] = str(block_data['value'])
					
					# 确保是字符串
					if not isinstance(block_data['value'], str):
						block_data['value'] = str(block_data['value'])
				
				# 特殊处理其他块类型
				if block_data['type'] in ['document_block', 'image_block', 'audio_block', 'video_block']:
					# 引用类型块需要整数ID
					if isinstance(block_data['value'], str) and block_data['value'].isdigit():
						block_data['value'] = int(block_data['value'])
				
				processed_data.append(block_data)
			
			# 创建StreamValue - 关键是先试用is_lazy=True加载，这样不会立即验证
			logger.debug(f"使用{len(processed_data)}个块创建StreamValue")
			stream_value = StreamValue(stream_block, processed_data, is_lazy=True)
			
			return stream_value
		
		except Exception as e:
			logger.error(f"从MongoDB创建StreamValue失败: {e}")
			logger.error(traceback.format_exc())
			# 返回空的StreamValue
			return StreamValue(stream_block, [], is_lazy=True)