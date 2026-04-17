#!/user/bin/env python3
# -*- coding: utf-8 -*-
# wagtailblog3/apps/blog/markdown_extensions.py

from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from wagtail.models import Page
from django.core.exceptions import ObjectDoesNotExist


class WagtailLinkRewriter(Treeprocessor):
	"""
	将 href="page:123" 替换为真实的 Wagtail 页面 URL。
	"""
	
	def run(self, root):
		for element in root.iter('a'):
			href = element.get('href')
			if href and href.startswith('page:'):
				try:
					page_id = int(href.split(':')[1])
					# specific=False 提高性能，只获取 URL
					page = Page.objects.get(id=page_id)
					real_url = page.get_url()
					
					if real_url:
						element.set('href', real_url)
					else:
						element.set('href', '#')
						element.set('title', '该页面当前不可访问')
				
				except (ValueError, IndexError, ObjectDoesNotExist):
					# 如果页面ID无效或页面被删除
					element.set('href', '#broken-link')
					element.set('title', '链接无效')
					# 可选：标记为损坏的链接样式
					element.set('class', 'broken-link')


class WagtailAppExtension(Extension):
	def extendMarkdown(self, md):
		md.treeprocessors.register(WagtailLinkRewriter(md), 'wagtail_link_rewriter', 15)