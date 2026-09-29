"""
Wagtail Page 正文外置通用协议 - Codec 包导出。
"""

from external_content.codecs.base import BaseContentCodec
from external_content.codecs.streamfield import StreamFieldV1Codec

__all__ = ["BaseContentCodec", "StreamFieldV1Codec"]