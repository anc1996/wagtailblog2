"""Markdown 导入专用密钥认证。"""

from __future__ import annotations

import hashlib

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from blog.models import MarkdownImportToken


class MarkdownImportTokenAuthentication(BaseAuthentication):
	"""仅接受 mdimp_ 前缀密钥，并限制为 Markdown 导入接口使用。"""

	def authenticate(self, request):
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

	def authenticate_header(self, request):
		return 'Bearer'
