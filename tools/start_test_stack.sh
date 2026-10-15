#!/usr/bin/env bash
# 旧脚本分区与生产重叠；资源启动需独立审核，不再执行隐式服务操作。
printf "%s\n" "ERR_TEST_STACK_RETIRED" >&2
exit 2
