"""
Django 管理命令：初始化与核验 external_content_versions MongoDB 集合唯一索引。

用于：
1. 生产与测试环境部署前建立不可变正文集合的唯一索引；
2. CI/CD 自动化门禁中通过 --check 验证索引是否就绪。
"""

import sys
from django.core.management.base import BaseCommand

from external_content.store import check_indexes, ensure_indexes, get_collection


class Command(BaseCommand):
    help = "初始化或校验 MongoDB external_content_versions 不可变正文集合的唯一索引"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="仅检查索引是否存在且具备 unique=True 约束，不执行创建；未就绪时退出码为 1",
        )

    def handle(self, *args, **options):
        coll = get_collection()
        is_check = options.get("check", False)

        if is_check:
            self.stdout.write(f"正在检查集合 '{coll.name}' 的唯一索引完备性...")
            is_ready, errors = check_indexes(coll)
            if not is_ready:
                self.stderr.write(self.style.ERROR(f"[FAIL] 集合 '{coll.name}' 唯一索引未就绪: {errors}"))
                sys.exit(1)
            self.stdout.write(self.style.SUCCESS(f"[OK] 集合 '{coll.name}' 唯一索引完备且约束有效。"))
            return

        self.stdout.write(f"正在为集合 '{coll.name}' 建立唯一索引...")
        ensure_indexes(coll)
        is_ready, errors = check_indexes(coll)
        if not is_ready:
            self.stderr.write(self.style.ERROR(f"[FAIL] 索引建立后校验未通过: {errors}"))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS(f"[SUCCESS] 集合 '{coll.name}' 唯一索引初始化成功并校验通过。"))