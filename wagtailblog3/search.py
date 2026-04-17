# wagtailblog3/search.py
from wagtail.search.backends.database.fallback import DatabaseSearchBackend
from wagtailblog3.mongo import MongoManager
from wagtail.models import Page
from blog.models import BlogPage
import logging
from django.db.models import Q

logger = logging.getLogger(__name__)

mongo_manager = MongoManager()
class CustomSearchBackend(DatabaseSearchBackend):
	"""自定义搜索后端，结合MySQL和MongoDB搜索，优化中文搜索支持"""
	
	def __init__(self, params):
		super().__init__(params)
		self.mongo = mongo_manager
	
	def search(self, query, model_or_queryset, fields=None, operator=None,
	           order_by_relevance=True, **kwargs):
		"""实现优化的混合搜索，支持中文"""
		logger.debug(f"开始混合搜索: '{query}'")
		
		# 获取搜索模型
		model = self._get_model(model_or_queryset)
		
		# 如果不是页面模型，使用默认搜索
		if not issubclass(model, Page):
			return super().search(query, model_or_queryset, fields=fields,
			                      operator=operator, order_by_relevance=order_by_relevance)
		
		# 博客页面的特殊搜索逻辑
		if model == BlogPage or issubclass(model, BlogPage):
			return self._search_blog_pages(query, model_or_queryset)
		
		# 其他页面的混合搜索
		return self._hybrid_search(query, model_or_queryset, fields, operator, order_by_relevance)
	
	def _get_model(self, model_or_queryset):
		"""获取搜索的模型类"""
		if hasattr(model_or_queryset, 'model'):
			return model_or_queryset.model
		return model_or_queryset
	
	def _search_blog_pages(self, query, model_or_queryset):
		"""专门针对博客页面的搜索"""
		try:
			# 1. 在MySQL中搜索基础信息（标题、简介）
			mysql_results = self._search_mysql_blog(query, model_or_queryset)
			
			# 2. 在MongoDB中搜索内容
			mongo_results = self._search_mongodb_blog(query)
			
			# 3. 合并和去重结果
			combined_results = self._merge_blog_results(mysql_results, mongo_results, query)
			
			# 确保返回的是QuerySet
			if not isinstance(combined_results, (list, tuple)):
				return combined_results
			
			# 如果是列表，转为QuerySet
			if hasattr(model_or_queryset, 'model'):
				# 使用提供的QuerySet的模型
				page_ids = [page.id for page in combined_results if hasattr(page, 'id')]
				return model_or_queryset.filter(id__in=page_ids)
			else:
				# 直接使用BlogPage
				page_ids = [page.id for page in combined_results if hasattr(page, 'id')]
				return BlogPage.objects.filter(id__in=page_ids)
		except Exception as e:
			logger.error(f"搜索博客页面出错: {e}")
			# 发生错误时尝试使用标准搜索
			if hasattr(model_or_queryset, 'filter'):
				return model_or_queryset.filter(title__icontains=query)
			return BlogPage.objects.filter(title__icontains=query)
	
	def _search_mysql_blog(self, query, model_or_queryset):
		"""在MySQL中搜索博客基础信息"""
		if hasattr(model_or_queryset, 'model'):
			queryset = model_or_queryset
		else:
			queryset = model_or_queryset.objects.all()
		
		# 构建Q对象进行搜索
		q_objects = Q()
		
		# 搜索标题
		q_objects |= Q(title__icontains=query)
		
		# 搜索简介
		q_objects |= Q(intro__icontains=query)
		
		# 搜索标签
		q_objects |= Q(tags__name__icontains=query)
		
		# 搜索分类
		q_objects |= Q(categories__name__icontains=query)
		
		# 执行查询
		results = queryset.filter(q_objects).distinct()
		
		# 搜索作者
		q_objects |= Q(authors__name__icontains=query)
		
		# 计算相关性分数
		scored_results = []
		for page in results:
			score = self._calculate_mysql_score(page, query)
			scored_results.append((page, score))
		
		# 按分数排序
		scored_results.sort(key=lambda x: x[1], reverse=True)
		
		return [result[0] for result in scored_results]
	
	def _search_mongodb_blog(self, query):
		"""在MongoDB中搜索博客内容"""
		logger.debug(f"执行MongoDB博客搜索: {query}")
		
		try:
			# 使用MongoManager执行搜索
			mongo_results = self.mongo.search_blog_content(query)
			
			# 添加调试日志
			logger.debug(f"MongoDB搜索结果数: {len(mongo_results)}")
			if len(mongo_results) > 0:
				for i, result in enumerate(mongo_results[:5]):  # 只记录前5个结果
					logger.debug(f"  结果 {i + 1}: 标题={result.get('title')}, ID={result.get('page_id')}")
			
			return mongo_results
		except Exception as e:
			import traceback
			logger.error(f"MongoDB搜索失败: {e}")
			logger.error(traceback.format_exc())
			return []
	
	def _merge_blog_results(self, mysql_results, mongo_results, query):
		"""合并MySQL和MongoDB的搜索结果"""
		# 创建页面ID到分数的映射
		page_scores = {}
		
		# 处理MySQL结果
		for i, page in enumerate(mysql_results):
			score = len(mysql_results) - i  # 基础分数
			page_scores[page.id] = {
				'page': page,
				'mysql_score': score,
				'mongo_score': 0,
				'total_score': score * 0.3  # MySQL权重30%
			}
		
		# 记录MongoDB结果
		logger.debug(f"处理MongoDB搜索结果: 共 {len(mongo_results)} 个结果")
		
		# 处理MongoDB结果
		for mongo_doc in mongo_results:
			try:
				# 检查page_id是否存在
				if 'page_id' not in mongo_doc:
					logger.warning(f"MongoDB结果缺少page_id字段: {mongo_doc.get('_id')}")
					continue
				
				# 获取页面ID，处理可能的格式问题
				page_id_raw = mongo_doc['page_id']
				try:
					if isinstance(page_id_raw, int):
						page_id = page_id_raw
					elif isinstance(page_id_raw, str) and page_id_raw.isdigit():
						page_id = int(page_id_raw)
					else:
						logger.warning(f"无法解析page_id: {page_id_raw}，类型: {type(page_id_raw)}")
						continue
				except (ValueError, TypeError) as e:
					logger.warning(f"转换page_id失败: {e}, 值: {page_id_raw}")
					continue
				
				# 获取相关性分数
				mongo_score = mongo_doc.get('score', 1.0)
				logger.debug(f"MongoDB结果: page_id={page_id}, score={mongo_score}")
				
				if page_id in page_scores:
					# 更新已存在的页面分数
					page_scores[page_id]['mongo_score'] = mongo_score
					page_scores[page_id]['total_score'] += mongo_score * 0.7  # MongoDB权重70%
					logger.debug(f"更新页面分数: page_id={page_id}, total_score={page_scores[page_id]['total_score']}")
				else:
					# 尝试从数据库获取页面
					try:
						page = BlogPage.objects.get(id=page_id)
						page_scores[page_id] = {
							'page': page,
							'mysql_score': 0,
							'mongo_score': mongo_score,
							'total_score': mongo_score * 0.7
						}
						logger.debug(f"添加新页面: page_id={page_id}, title={page.title}")
					except Exception as e:
						logger.warning(f"无法获取页面 ID={page_id}: {e}")
						continue
			except Exception as e:
				logger.error(f"处理MongoDB结果时出错: {e}, doc={mongo_doc.get('_id')}")
				continue
		
		# 记录合并结果
		logger.debug(f"合并后的结果数: {len(page_scores)}")
		
		# 按总分排序并返回页面列表
		sorted_results = sorted(page_scores.values(),
		                        key=lambda x: x['total_score'],
		                        reverse=True)
		
		# 返回页面列表
		result_pages = [item['page'] for item in sorted_results]
		return result_pages
	
	def _calculate_mysql_score(self, page, query):
		"""计算MySQL搜索结果的相关性分数"""
		score = 0
		query_lower = query.lower()
		
		# 标题匹配（权重最高）
		if page.title and query_lower in page.title.lower():
			score += 10
			# 完全匹配加分
			if query_lower == page.title.lower():
				score += 5
		
		# 简介匹配
		if hasattr(page, 'intro') and page.intro and query_lower in page.intro.lower():
			score += 5
		
		# 标签匹配
		if hasattr(page, 'tags'):
			for tag in page.tags.all():
				if query_lower in tag.name.lower():
					score += 3
		
		# 分类匹配
		if hasattr(page, 'categories'):
			for category in page.categories.all():
				if query_lower in category.name.lower():
					score += 2
					
		# 作者匹配
		if hasattr(page, 'authors'):
			for author in page.authors.all():
				if author.name and query_lower in author.name.lower():
					score += 4
					
		return score
	
	def _hybrid_search(self, query, model_or_queryset, fields, operator, order_by_relevance):
		"""其他页面类型的混合搜索"""
		try:
			# 使用标准Wagtail搜索
			standard_results = super().search(
				query, model_or_queryset, fields=fields,
				operator=operator, order_by_relevance=order_by_relevance
			)
			
			# 尝试在MongoDB中搜索补充
			try:
				mongo_results = self.mongo.search_blog_content(query)
				
				# 提取MongoDB找到的页面ID
				mongo_page_ids = set()
				for result in mongo_results:
					if 'page_id' in result:
						try:
							mongo_page_ids.add(int(result['page_id']))
						except (ValueError, TypeError):
							continue
				
				# 如果有MongoDB结果，补充到标准结果中
				if mongo_page_ids:
					standard_ids = {page.id for page in standard_results}
					missing_ids = mongo_page_ids - standard_ids
					
					if missing_ids:
						# 获取缺失的页面
						if hasattr(model_or_queryset, 'model'):
							missing_pages = model_or_queryset.filter(id__in=missing_ids)
						else:
							missing_pages = model_or_queryset.objects.filter(id__in=missing_ids)
						
						# 合并结果
						from itertools import chain
						combined_results = list(chain(standard_results, missing_pages))
						
						# 转回QuerySet
						if hasattr(model_or_queryset, 'model'):
							page_ids = [page.id for page in combined_results]
							return model_or_queryset.filter(id__in=page_ids)
						else:
							page_ids = [page.id for page in combined_results]
							return Page.objects.filter(id__in=page_ids)
			
			except Exception as e:
				logger.error(f"混合搜索MongoDB补充失败: {e}")
			
			return standard_results
		except Exception as e:
			logger.error(f"混合搜索出错: {e}")
			# 错误时尝试简单搜索
			if hasattr(model_or_queryset, 'filter'):
				return model_or_queryset.filter(title__icontains=query)
			return Page.objects.filter(title__icontains=query)