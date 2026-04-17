# blog/management/commands/test_all_search.py
from django.core.management.base import BaseCommand
import requests
import json
from django.http import HttpRequest
from django.core.paginator import Paginator
import traceback


class Command(BaseCommand):
	help = '测试所有搜索功能（常规、AJAX和API）'
	
	def add_arguments(self, parser):
		parser.add_argument('query', type=str, help='搜索关键词')
		parser.add_argument('--type', type=str, default='all', choices=['all', 'blog', 'pages'], help='搜索类型')
		parser.add_argument('--host', type=str, default='http://localhost:8000', help='服务器地址')
		parser.add_argument('--local', action='store_true', help='仅测试本地函数，不发送HTTP请求')
	
	def handle(self, *args, **options):
		query = options['query']
		search_type = options['type']
		host = options['host']
		local_only = options.get('local', False)
		
		self.stdout.write(f'测试全部搜索功能: "{query}" (类型: {search_type})')
		self.stdout.write('=' * 80)
		
		if not local_only:
			# 1. 测试普通视图搜索
			self.test_view_search(host, query, search_type)
			
			# 2. 测试AJAX搜索
			self.test_ajax_search(host, query, search_type)
			
			# 3. 测试API搜索
			self.test_api_search(host, query, search_type)
			
			# 4. 测试搜索建议
			self.test_suggestions(host, query)
		
		# 5. 测试直接调用核心函数
		self.test_core_functions(query, search_type)
	
	def test_view_search(self, host, query, search_type):
		"""测试普通视图搜索"""
		self.stdout.write('\n1. 测试普通视图搜索:')
		
		try:
			# 构建URL
			url = f"{host}/search/?query={query}&type={search_type}"
			
			# 发送请求
			self.stdout.write(f'  发送请求到: {url}')
			response = requests.get(url, timeout=10)
			
			# 检查响应
			if response.status_code == 200:
				self.stdout.write(self.style.SUCCESS(f'  请求成功: HTTP {response.status_code}'))
				
				# 检查内容
				if 'text/html' in response.headers.get('Content-Type', ''):
					html_length = len(response.text)
					self.stdout.write(f'  接收到HTML响应: {html_length} 字节')
					
					# 检查页面中是否包含搜索关键词
					if query in response.text:
						self.stdout.write(self.style.SUCCESS(f'  页面包含搜索关键词: "{query}"'))
					else:
						self.stdout.write(self.style.WARNING(f'  页面不包含搜索关键词'))
				else:
					self.stdout.write(self.style.WARNING(f'  响应不是HTML: {response.headers.get("Content-Type")}'))
			else:
				self.stdout.write(self.style.ERROR(f'  请求失败: HTTP {response.status_code}'))
				self.stdout.write(response.text[:500])  # 只显示前500个字符
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'  请求出错: {e}'))
			self.stdout.write(traceback.format_exc())
	
	def test_ajax_search(self, host, query, search_type):
		"""测试AJAX搜索"""
		self.stdout.write('\n2. 测试AJAX搜索:')
		
		try:
			# 构建URL
			url = f"{host}/search/?query={query}&type={search_type}"
			
			# 发送请求
			self.stdout.write(f'  发送AJAX请求到: {url}')
			response = requests.get(
				url,
				headers={'X-Requested-With': 'XMLHttpRequest'},
				timeout=10
			)
			
			# 检查响应
			if response.status_code == 200:
				self.stdout.write(self.style.SUCCESS(f'  请求成功: HTTP {response.status_code}'))
				
				# 解析JSON
				try:
					data = response.json()
					
					# 显示结果摘要
					self.stdout.write(f'  搜索查询: {data.get("query")}')
					self.stdout.write(f'  总结果数: {data.get("total_count", 0)}')
					self.stdout.write(f'  当前页: {data.get("current_page", 1)} / {data.get("total_pages", 1)}')
					
					# 显示部分结果
					results = data.get('results', [])
					self.stdout.write(f'  显示 {len(results)} 个结果:')
					
					for i, result in enumerate(results[:3]):  # 只显示前3个
						self.stdout.write(f'    {i + 1}. {result.get("title")} (ID: {result.get("id")})')
				except json.JSONDecodeError:
					self.stdout.write(self.style.ERROR(f'  无法解析JSON响应'))
					self.stdout.write(response.text[:500])
			else:
				self.stdout.write(self.style.ERROR(f'  请求失败: HTTP {response.status_code}'))
				self.stdout.write(response.text[:500])
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'  请求出错: {e}'))
			self.stdout.write(traceback.format_exc())
	
	def test_api_search(self, host, query, search_type):
		"""测试API搜索"""
		self.stdout.write('\n3. 测试API搜索:')
		
		try:
			# 构建URL
			url = f"{host}/search/api/?q={query}&type={search_type}"
			
			# 发送请求
			self.stdout.write(f'  发送请求到: {url}')
			response = requests.get(url, timeout=10)
			
			# 检查响应
			if response.status_code == 200:
				self.stdout.write(self.style.SUCCESS(f'  请求成功: HTTP {response.status_code}'))
				
				# 解析JSON
				try:
					data = response.json()
					
					# 显示结果摘要
					self.stdout.write(f'  搜索查询: {data.get("query")}')
					self.stdout.write(f'  总结果数: {data.get("total", 0)}')
					self.stdout.write(f'  页码: {data.get("page", 1)}, 每页: {data.get("per_page", 10)}')
					
					# 显示部分结果
					results = data.get('results', [])
					self.stdout.write(f'  显示 {len(results)} 个结果:')
					
					for i, result in enumerate(results[:3]):  # 只显示前3个
						self.stdout.write(f'    {i + 1}. {result.get("title")} (ID: {result.get("id")})')
				except json.JSONDecodeError:
					self.stdout.write(self.style.ERROR(f'  无法解析JSON响应'))
					self.stdout.write(response.text[:500])
			else:
				self.stdout.write(self.style.ERROR(f'  请求失败: HTTP {response.status_code}'))
				self.stdout.write(response.text[:500])
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'  请求出错: {e}'))
			self.stdout.write(traceback.format_exc())
	
	def test_suggestions(self, host, query):
		"""测试搜索建议"""
		self.stdout.write('\n4. 测试搜索建议:')
		
		try:
			# 构建URL
			url = f"{host}/search/suggestions/?q={query}"
			
			# 发送请求
			self.stdout.write(f'  发送请求到: {url}')
			response = requests.get(url, timeout=10)
			
			# 检查响应
			if response.status_code == 200:
				self.stdout.write(self.style.SUCCESS(f'  请求成功: HTTP {response.status_code}'))
				
				# 解析JSON
				try:
					data = response.json()
					
					# 显示建议列表
					suggestions = data.get('suggestions', [])
					self.stdout.write(f'  找到 {len(suggestions)} 个建议:')
					
					for i, suggestion in enumerate(suggestions):
						self.stdout.write(f'    {i + 1}. {suggestion.get("query")} (点击: {suggestion.get("hits", 0)})')
				except json.JSONDecodeError:
					self.stdout.write(self.style.ERROR(f'  无法解析JSON响应'))
					self.stdout.write(response.text[:500])
			else:
				self.stdout.write(self.style.ERROR(f'  请求失败: HTTP {response.status_code}'))
				self.stdout.write(response.text[:500])
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'  请求出错: {e}'))
			self.stdout.write(traceback.format_exc())
	
	def test_core_functions(self, query, search_type):
		"""测试核心搜索函数"""
		self.stdout.write('\n5. 测试核心搜索函数:')
		
		try:
			from search.core import perform_search, get_search_suggestions, format_search_results_for_api
			
			# 测试搜索函数
			self.stdout.write('  测试 perform_search 函数...')
			search_results = perform_search(query, search_type)
			
			if hasattr(search_results, 'count'):
				self.stdout.write(f'  搜索结果数: {search_results.count()}')
				self.stdout.write(f'  结果类型: {type(search_results)}')
			else:
				self.stdout.write(self.style.WARNING(f'  搜索结果不支持 count() 方法，类型: {type(search_results)}'))
				self.stdout.write(
					f'  结果长度: {len(search_results) if hasattr(search_results, "__len__") else "未知"}')
			
			# 显示部分结果
			try:
				for i, result in enumerate(search_results[:3]):  # 只显示前3个
					self.stdout.write(f'    {i + 1}. {result.title if hasattr(result, "title") else "无标题"} ' +
					                  f'(ID: {result.id if hasattr(result, "id") else "无ID"})')
			except Exception as e:
				self.stdout.write(self.style.ERROR(f'  显示搜索结果出错: {e}'))
			
			# 测试建议函数
			self.stdout.write('\n  测试 get_search_suggestions 函数...')
			try:
				suggestions = get_search_suggestions(query)
				
				self.stdout.write(f'  建议数: {len(suggestions)}')
				
				# 显示建议
				for i, suggestion in enumerate(suggestions):
					self.stdout.write(f'    {i + 1}. {suggestion.get("query")} (点击: {suggestion.get("hits", 0)})')
			except Exception as e:
				self.stdout.write(self.style.ERROR(f'  获取搜索建议出错: {e}'))
			
			# 测试格式化函数
			self.stdout.write('\n  测试 format_search_results_for_api 函数...')
			try:
				results_for_api = search_results[:3] if hasattr(search_results, '__getitem__') else []
				formatted_results = format_search_results_for_api(results_for_api)
				
				self.stdout.write(f'  格式化结果数: {len(formatted_results)}')
				
				# 显示格式化结果
				for i, result in enumerate(formatted_results):
					self.stdout.write(f'    {i + 1}. {result.get("title")} (ID: {result.get("id")})')
			except Exception as e:
				self.stdout.write(self.style.ERROR(f'  格式化搜索结果出错: {e}'))
		
		except Exception as e:
			self.stdout.write(self.style.ERROR(f'  测试核心函数出错: {e}'))
			self.stdout.write(traceback.format_exc())