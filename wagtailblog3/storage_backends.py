# wagtailblog3/storage_backends.py
from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage


class MinioStorageBase(S3Boto3Storage):
    """基础MinIO存储类"""
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    access_key = settings.AWS_ACCESS_KEY_ID
    secret_key = settings.AWS_SECRET_ACCESS_KEY
    endpoint_url = settings.AWS_S3_ENDPOINT_URL
    file_overwrite = False
    default_acl = 'public-read'
    querystring_auth = False


class MinioMediaStorage(MinioStorageBase):
    """通用媒体文件存储类"""
    location = 'media'

class MinioAudioStorage(MinioStorageBase):
    """音频文件存储类"""
    location = 'audio'

class MinioVideoStorage(MinioStorageBase):
    """视频文件存储类"""
    location = 'video'

class MinioDocumentStorage(MinioStorageBase):
    """Wagtail文档存储类"""
    location = 'documents'

class MinioImageStorage(MinioStorageBase):
    """Wagtail图片存储类"""
    location = 'images'


class MinioOriginalImageStorage(MinioStorageBase):
    """原始图片存储类"""
    location = 'original_images'