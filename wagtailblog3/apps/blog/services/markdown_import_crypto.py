"""Markdown 导入 Token 的对称认证加密服务。

基于 AES-256-GCM 算法对导入 Token 明文进行认证加密（AEAD），密钥派生自 Django
全局 ``settings.SECRET_KEY``。密文存储用于在 Wagtail 后台列表或详情中支持管理员按需
复制密钥明文，同时不影响 API 鉴权层基于 SHA-256 哈希的秒级校验性能。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from django.conf import settings

logger = logging.getLogger(__name__)

# AES-GCM 标准 Nonce (IV) 长度：12 字节
_GCM_NONCE_LENGTH = 12
# AES-GCM 认证 Tag 长度：16 字节 (128 位)
_GCM_TAG_LENGTH = 16


def _get_derived_key() -> bytes:
    """根据 settings.SECRET_KEY 派生 256 位（32 字节）对称加密密钥。

    使用 SHA-256 哈希将任意长度的 SECRET_KEY 规范化为固定 32 字节的二进制密钥。
    """
    secret_key = getattr(settings, "SECRET_KEY", "")
    if not secret_key:
        raise ValueError("Django settings.SECRET_KEY 不能为空，无法派生加密密钥")
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def encrypt_token(plaintext: str) -> str:
    """使用 AES-256-GCM 对 Token 明文字符串进行认证加密。

    参数：
        plaintext: 待加密的原始 Token 明文（例如 mdimp_xxx）。

    返回：
        Base64 编码的密文字符串，结构为 base64(nonce[12] + tag[16] + ciphertext)。

    异常：
        ValueError: 当明文为空或加密过程异常时抛出。
    """
    if not plaintext:
        raise ValueError("待加密的 Token 明文不能为空")

    key = _get_derived_key()
    nonce = get_random_bytes(_GCM_NONCE_LENGTH)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))

    # 封包结构：Nonce(12B) + Tag(16B) + Ciphertext
    payload = nonce + tag + ciphertext
    return base64.b64encode(payload).decode("ascii")


def decrypt_token(encrypted_payload: str) -> str:
    """解密由 encrypt_token 生成的 Base64 密文字符串并验证完整性。

    参数：
        encrypted_payload: Base64 编码的密文字符串。

    返回：
        解密还原后的原始 Token 明文字符串。

    异常：
        ValueError: 当密文格式损坏、长度不足、认证标签不匹配或篡改时抛出。
    """
    if not encrypted_payload:
        raise ValueError("待解密的 Token 密文不能为空")

    try:
        raw = base64.b64decode(encrypted_payload.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError(f"Token 密文 Base64 解码失败: {exc}") from exc

    min_length = _GCM_NONCE_LENGTH + _GCM_TAG_LENGTH
    if len(raw) <= min_length:
        raise ValueError(f"Token 密文数据长度不足，至少需要 {min_length + 1} 字节")

    nonce = raw[:_GCM_NONCE_LENGTH]
    tag = raw[_GCM_NONCE_LENGTH : _GCM_NONCE_LENGTH + _GCM_TAG_LENGTH]
    ciphertext = raw[_GCM_NONCE_LENGTH + _GCM_TAG_LENGTH :]

    key = _get_derived_key()
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        plaintext_bytes = cipher.decrypt_and_verify(ciphertext, tag)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Token 密文验证或解密失败（可能已被篡改或密钥不一致）: {exc}") from exc

    try:
        return plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Token 明文 UTF-8 解码失败: {exc}") from exc
