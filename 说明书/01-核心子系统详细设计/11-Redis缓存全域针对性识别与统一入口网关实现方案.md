# Redis 缓存全域针对性识别与统一入口网关实现方案

## 1. 方案背景与生产隔离架构

### 1.1 拓扑背景与根因
生产服务器（`192.168.20.2:6379`）单实例 Redis 由**博客系统**与**宿主机独立电商系统（Shop）**共享运行。优化前因缺乏统一命名规范与物理隔离，存在键名碰撞、孤儿键无 TTL、Celery 队列相互串台等严重风险。

### 1.2 最终物理分库隔离拓扑（16 库全景）
生产环境博客系统采用 **`[12, 13, 14, 15]` 连续四库**，便于记忆且与外部系统物理硬隔离：

| 库号 (DB) | 分配归属 | 角色用途 | 统一键前缀 | 生产配置项 (.env.production) |
| :--- | :--- | :--- | :--- | :--- |
| **DB 12** | **博客生产** | 评论/访问频次限制缓存 | `wblog:prod:rate:...` | `REDIS_COMMENT_CACHE_DB='12'` |
| **DB 13** | **博客生产** | 核心业务缓存 (default cache) | `wblog:prod:...` | `REDIS_CACHE_DB='13'` |
| **DB 14** | **博客生产** | Celery Broker 消息队列 | `wblog:prod:broker:...` | `CELERY_BROKER_DB='14'` |
| **DB 15** | **博客生产** | Celery Result 任务执行结果 | `wblog:prod:result:...` | `CELERY_RESULT_DB='15'` |
| **DB 2~4** | **商城独占** | 电商系统 Broker、验证码与购物车 | — | 博客系统**严禁使用** |
| **DB 10** | **商城独占** | 电商系统 Celery Result 后端 | — | 博客系统**严禁使用** |
| **DB 5~8** | **博客测试** | 测试机缓存/限流/Broker/Result | `wblog:test:...` | 仅供 WSL2 测试栈使用 |
| **DB 0, 1, 9** | **系统保留** | 原 DB 1 博客缓存已平滑腾空，备用 | — | 闲置备用 |

---

## 2. 六段式统一数据识别协议

进入 Redis 的所有键值强制遵循六段式命名规范：
$$\mathbf{\{app\}:\{env\}:\{module\}:\{entity\}:\{identifier\}:\{version\}}$$

- `{app}`：固定为应用标识 `wblog`（与商城的 `shop` 绝对区隔）；
- `{env}`：部署环境（`prod`、`test`、`dev`）；
- `{module}`：功能域（`listing`、`detail`、`sidebar`、`rate`、`broker`、`result`、`cache`）；
- `{entity}`：实体类型（`page`、`author`、`category`、`archive`、`lock`、`gen`）；
- `{identifier}`：实体唯一标识符（页面ID、Query哈希、时间戳）；
- `{version}`：数据版本或代次标识（`v2`、`gen_1`）。

### 强类型协议工厂（`wagtailblog3/apps/blog/services/redis_protocol.py`）
禁止业务层裸写字符串拼接，收拢由 `RedisKeyProtocol` 工厂输出：
- `listing_cache_key()` -> `listing:v2:s_{site}:loc_{locale}:idx_{page}:gen_{gen}:q_{hash}`
- `listing_lock_key()` -> `listing:lock:s_{site}:loc_{locale}:idx_{page}:q_{hash}`
- `sidebar_archive_key()` -> `sidebar:v2:archive:aggregate:{site}:{locale}:{gen}:{tree_hash}`
- `sidebar_author_pick_key()` -> `sidebar:v2:author:pick:{site}:{locale}:{gen}:{time_bucket}`
- `rate_limit_key()` -> `rate:{action}:{client_id}`

---

## 3. 核心落盘代码与网关实现

### 3.1 Django Cache 统一网关（`wagtailblog3/settings/database.py`）
通过 `KEY_FUNCTION` 与 `REVERSE_KEY_FUNCTION` 拦截全局缓存流量：
```python
def unified_key_maker(key: str, key_prefix: str, version: int) -> str:
    # 1. 幂等直通：已有 wblog:{env}:* 直接返回
    if key.startswith(f"wblog:{env_name}:"):
        return key
    # 2. 补齐环境段：已有 wblog:* 补齐为 wblog:{env}:...
    if key.startswith("wblog:"):
        return f"wblog:{env_name}:{key[6:]}"
    # 3. 剥离冗余环境前缀（解决历史 .env 中 REDIS_KEY_PREFIX=prod 导致的 prod:prod 重复）
    # 4. 业务/通用键封装：生成 wblog:{env}:{module}:{remainder}
    # 5. delete_pattern 模式支持与版本后缀安全处理
    ...

def unified_reverse_key(key: str) -> str:
    # 双向可逆反解，确保 cache.get_many() / delete_pattern 映射一致
    ...
```

### 3.2 Celery 传输层全局前缀（`wagtailblog3/settings/database.py`）
在 `get_celery_config()` 注入 Kombu 与结果后端选项：
```python
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'global_keyprefix': f'wblog:{env_name}:broker:',
}
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    'global_keyprefix': f'wblog:{env_name}:result:',
}
```
Kombu 自动加载为 `PrefixedStrictRedis`，所有交换机、绑定与任务结果自动携带独立前缀，彻底根除跨系统串台。

### 3.3 分布式锁底层物理键直传（`wagtailblog3/apps/blog/services/listing.py`）
原生 Redis 客户端执行 Lua 脚本释放锁时，通过底层映射直传真实物理键名：
```python
lock_real_key = cache_backend.make_key(lock_key)
client.eval(UNLOCK_SCRIPT, 1, lock_real_key, lock_token)
```

---

## 4. 生产上线实测验证与性能表现

### 4.1 服务运行态验证
- **目标服务器**：`192.168.20.2`（ziliao），生产 4 大应用服务全 active：
  - `wagtailblog3.service`：uWSGI 4 核心 prefork+threaded，HTTP 200；
  - `wagtailblog3-celery-maintenance.service`：`maintenance@ziliao: OK pong`；
  - `wagtailblog3-celery-beat.service`：调度正常；
  - `wagtailblog3-filebeat.service`：日志正常采集。

### 4.2 实机键空间抽检（DB 13 运行实况）
DB 13 已完全接管生产缓存，生成的键全部合规且均带合理 TTL：
- `wblog:prod:sidebar:v2:archive:aggregate:2:1:1:1`
- `wblog:prod:sidebar:v2:author:pick:2:1:1:2026091008`
- `wblog:prod:listing:v2:s_1:loc_1:idx_24:gen_1:q_b12977ca2298f2e8`
- `wblog:prod:cache:template.cache.main_nav_menu...`

### 4.3 实机响应性能（`tools/benchmark_prod.py` 采样）
- **目标分类慢页面（`公文材料/政务公开题材/`，122 篇长文）**：
  - 冷启动：**205.94 ms**
  - 热缓存平均：**79.30 ms**（最低跑出 **70.41 ms**，较最初 10+ 秒提速超 100 倍）
- **AJAX 分页接口（Page 2）**：热响应平均 **47.17 ms**（最低 **39.35 ms**）
- **全站首页（`/zh-hans/`）**：热响应平均 **93.61 ms**（最低 **83.99 ms**）

---

## 5. 日常运维规范与指令速查

1. **严禁全库阻塞扫描**：
   生产环境严禁使用 `KEYS *`，必须使用安全迭代器：
   ```bash
   # 抽检 DB 13 生产缓存键
   redis-cli -n 13 --scan --pattern "wblog:prod:*" | head -n 20
   ```
2. **手术刀级精准失效**：
   发布或更新文章时，仅清理对应分类代次与侧边栏聚合，**严禁执行全局 `cache.clear()`**：
   ```python
   # 仅清理特定列表缓存，不伤及其他模块与电商数据
   cache.delete_pattern("listing:*")
   ```
3. **启停维护关联**：
   详见 `systemctl.md`，涉及 Redis 端口或配置变更时，按 Web -> Worker -> Beat 顺序优雅启停。