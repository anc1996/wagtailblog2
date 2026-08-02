"""创建并初始化 MinIO/S3 兼容对象存储桶的管理命令。"""

import boto3
from botocore.client import Config
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    """从 Django 配置读取凭据，创建存储桶并设置公开读取策略。"""

    help = '创建MinIO存储桶'

    def handle(self, *args, **options):
        # 从 Django 设置读取对象存储连接参数，避免把凭据写死在命令中。
        
        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
        endpoint_url = settings.AWS_S3_ENDPOINT_URL
        access_key = settings.AWS_ACCESS_KEY_ID
        secret_key = settings.AWS_SECRET_ACCESS_KEY

        self.stdout.write(f"正在连接到MinIO服务器: {endpoint_url}")
        self.stdout.write(f"准备创建存储桶: {bucket_name}")

        # 使用兼容 S3 协议的客户端连接 MinIO；所有连接参数均来自配置。
        s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version='s3v4'),
            verify=False
        )

        try:
            # 创建存储桶；已存在或权限不足时交由异常分支提示。
            s3_client.create_bucket(Bucket=bucket_name)
            self.stdout.write(self.style.SUCCESS(f"存储桶 {bucket_name} 创建成功"))

            # 写入公开读取策略，使前台媒体 URL 可以直接访问对象。
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket_name}/*"]
                    }
                ]
            }
            s3_client.put_bucket_policy(Bucket=bucket_name, Policy=str(policy).replace("'", '"'))
            self.stdout.write(self.style.SUCCESS("存储桶权限已设置为公开可读"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"创建存储桶时出错: {e}"))
