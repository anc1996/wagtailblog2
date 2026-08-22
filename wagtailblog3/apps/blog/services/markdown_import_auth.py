"""Markdown 导入专用密钥认证。"""

from __future__ import annotations

import hashlib
from typing import Any

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from blog.models import MarkdownImportToken


class MarkdownImportTokenAuthentication(BaseAuthentication):
	"""Markdown 导入接口专用的 Bearer 密钥认证器。

	认证器只接受 ``mdimp_`` 前缀的原始密钥，并在数据库中比较 SHA-256 摘要，避免
	把明文密钥持久化。认证成功后更新最后使用时间；不满足格式、有效期、用户状态或
	作用域条件时返回 ``None``，交由 DRF 继续尝试其他认证器或拒绝请求。
	"""

	def authenticate(self, request: Any) -> tuple[Any, MarkdownImportToken] | None:
		"""解析请求头并返回已授权用户与令牌对象。

		参数：
			request：包含 ``Authorization`` 请求头的 DRF 请求对象。

		返回：``(user, token)`` 表示认证成功；头部不存在、密钥格式不符、令牌无效、
			用户停用或缺少 ``markdown_import`` 作用域时返回 ``None``。
		"""
		header = get_authorization_header(request).split()
		if len(header) != 2 or header[0].lower() != b'bearer':
			return None
		try:
			raw = header[1].decode('utf-8')
		except UnicodeDecodeError:
			return None
		if not raw.startswith('mdimp_'):
			return None
		token = MarkdownImportToken.objects.select_related('user').filter(
			token_hash=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
		).first()
		if token is None or not token.is_valid() or not token.user.is_active:
			return None
		if 'markdown_import' not in (token.scopes or []):
			return None
		MarkdownImportToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
		return token.user, token

	def authenticate_header(self, request: Any) -> str:
		"""返回客户端应使用的认证方案名称。"""
		return 'Bearer'
