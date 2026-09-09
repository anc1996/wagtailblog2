# Redis 缓存全域针对性识别与统一入口网关设计说明书

## 1. 方案概述与背景事实

### 1.1 生产环境真实拓扑与问题根因

生产服务器（`192.168.20.2`）上的单实例 Redis 运行在默认端口 `6379`。通过对 0~15 物理库实地扫描与进程排查，确认该实例**由两个完全独立的业务系统以及测试环境历史进程共享**：
1. **当前博客系统**：`/home/source/Django/wagtail/wagtailblog3`（环境 `wagtailblog`）；
2. **电商系统**：`/home/source/Django/shop`（环境 `shop313`，包含商城 Web 与商城独立 Celery 进程）；
3. **WSL2 / 本地测试栈**：曾经直连同一 Redis 实例。

### 1.2 生产环境 Redis 0~15 物理库真实占用事实表

| 物理库 | 键总数 | 活跃键类型与样本 | 归属系统 | 致命隐患与冲突分析 |
| :--- | :--- | :--- | :--- | :--- |
| **DB 0** | 0 | (空闲) | 商城默认 cache (未启用) | 无冲突 |
| **DB 1** | 22 | `prod:1:wblog:listing:v2:...`<br>`:1:blog-detail:v1:body:...`<br>`:1:blog-detail:v1:generation:1237` (ttl=-1) | 博客生产缓存<br>+ 商城 Session (同库配置) | ⚠️ **无前缀孤儿键堆积**：博客此前未配前缀，留存大量 `ttl=-1` 的旧详情页缓存，与新生成的 `prod:` 键断联；商城将 Session 配置在 DB 1，若无前缀直接碰撞。 |
| **DB 2** | 6 | `_kombu.binding.maintenance`<br>`_kombu.binding.shop`<br>`_kombu.binding.markdown-test-maintenance` | 博客 Broker<br>+ 商城 Broker<br>+ 商城验证码<br>+ 博客评论限流 | 🚨 **严重四重踩踏**：Kombu 默认在根空间写队列与绑定，博客与商城的任务队列、测试环境残留、以及商城短信验证码全部挤在 DB 2，极易误读、误删或队列串台。 |
| **DB 3** | 193 | `celery-task-meta-<uuid>` | 博客 Celery 结果<br>+ 商城浏览历史 | ⚠️ **重叠冲突**：博客定时任务（每30秒）高频产生任务元数据，与商城的读者浏览历史共处一库。 |
| **DB 4** | 5 | `pv:total:549` (ttl=-1)<br>`test_ping` | 博客旧版计数器<br>+ 商城购物车 | ⚠️ **脏数据隐患**：遗留无 TTL 的历史孤儿键，且商城配置为购物车库。 |
| **DB 5** | 5 | `test:1:wblog:sidebar:v2:...` | 测试环境主缓存 | 隔离良好（本次刚完成切换） |
| **DB 10**| 12 | `celery-task-meta-<uuid>` | 商城 Celery 结果 | 正常运行 |
| **DB 12**| 6 | `_kombu.binding.--pool=solo` | 历史测试脚本残留 | 待清理 |
| **其他** | 0 | DB 6, 7, 8, 9, 11, 13, 14, 15 空闲 | - | 可用作物理隔离重组 |

### 1.3 核心设计目标

1. **唯一身份识别**：所有写入 Redis 的数据，必须携带全局唯一的应用标识与环境标识，做到“见键知意、见键知主”；
2. **统一入口网关**：业务代码（View / Service / Task）禁止手工拼装键前缀，必须通过统一的框架级网关集中拦截与注入；
3. **多系统共存零踩踏**：即便博客与商城未来继续共享同一实例（甚至在极端情况下共享同一物理库），两者的缓存、限流与 Celery 队列在逻辑上互为黑盒、完全隔离；
4. **针对性运维与治理**：运维人员可执行“针对博客列表页的批量失效”、“针对特定模块的容量统计”，绝不伤及商城 Session、验证码与队列；
5. **根除内存拒绝服务风险**：消除所有失联孤儿键的永久占用，配合合理淘汰策略，彻底杜绝 `noeviction` 1GB 内存暴击。

---

## 2. 统一数据识别协议 (Key Protocol)

### 2.1 六段式标准命名契约

进入 Redis 的所有键值必须严格符合以下规范：

$$\mathbf{\{app\}:\{env\}:\{module\}:\{entity\}:\{identifier\}:\{version\}}$$

- **第 1 段：`{app}`（应用识别码，固定 5 字符内）**
  - 当前博客系统统一命名为：`wblog`（WagtailBlog）；
  - 电商系统为：`shop`。
- **第 2 段：`{env}`（部署环境识别码）**
  - `prod`：生产运行环境；
  - `test`：WSL2 / 自动化测试流水线；
  - `dev`：开发者本地开发环境。
- **第 3 段：`{module}`（功能域/业务模块）**
  - `listing`：博客分类与列表页查询模型；
  - `detail`：博客详情页正文与上下文缓存；
  - `sidebar`：归档与作者侧边栏聚合；
  - `search`：上游搜索业务缓存；
  - `rate`：评论或访问频率限制；
  - `broker`：Celery 消息队列与交换机；
  - `result`：Celery 异步任务执行结果；
  - `obs`：可观测性日志与概览。
- **第 4 段：`{entity}`（实体类型）**
  - `page`、`author`、`category`、`user`、`lock`、`gen`、`meta`。
- **第 5 段：`{identifier}`（实体唯一标识符）**
  - 主键 ID（如 `24`）、Token 摘要、哈希戳（如 `q_b12977ca`）。
- **第 6 段：`{version}`（可选，数据协议或代次版本）**
  - `v2`、`gen_1`、`b_v1`。

### 2.2 全业务键值映射全景表

| 业务场景 | 统一键格式 (Unified Key Pattern) | 典型 TTL | 驱逐与失效策略 |
| :--- | :--- | :--- | :--- |
| **列表页 DTO 缓存** | `wblog:prod:listing:page:{site}:{locale}:{idx}:{q_hash}:v2:{gen}` | 3600s (1h) | 代次推进自然失效 + LRU |
| **列表页 Single-Flight 锁** | `wblog:prod:listing:lock:{site}:{locale}:{idx}:{q_hash}` | 5s | Lua 脚本原子释放 / 超时自动解开 |
| **列表/侧栏代次计数器** | `wblog:prod:listing:gen:{scope_id}` | 永久 (-1) | `INCR` 自增推进，受保护核心资产 |
| **详情页正文缓存** | `wblog:prod:detail:page:{site}:{locale}:{page_id}:{body_ver}:v1` | 86400s (24h) | 发文/修改代次推进自然失效 |
| **详情页代次计数器** | `wblog:prod:detail:gen:{page_id}` | 永久 (-1) | 发布推进生成新 UUID |
| **侧边栏归档聚合** | `wblog:prod:sidebar:archive:{site}:{locale}:{scope_id}:v2:{gen}` | 86400s (24h) | 归档代次推进自然失效 |
| **侧边栏作者候选** | `wblog:prod:sidebar:author:{site}:{locale}:{author_id}:{bucket}` | 3600s (1h) | 小时桶自然轮换 |
| **搜索上游结果缓存** | `wblog:prod:search:query:{digest}:v5` | 300s (5m) | 版本隔离 + 定时失效 |
| **评论频率限制** | `wblog:prod:rate:comment:{user_id}` | 60s (1m) | 滑动窗口自然过期 |
| **Celery 队列与绑定** | `wblog:prod:broker:_kombu.binding.{queue}` | 永久 (-1) | 框架消息代理管理 |
| **Celery 任务执行结果** | `wblog:prod:result:celery-task-meta-{task_id}` | 3600s (1h) | Celery 自动过期清理 |

---

## 3. 四大全局入口网关架构

为避免业务开发散落拼接字符串，在系统架构层建立 **4 个自动化入口网关**：

```
                          【业务层统一调用】
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
[网关 1: Django Cache]     [网关 2: Celery Broker]     [网关 3: Celery Result]
KEY_FUNCTION 统一网关       Kombu global_keyprefix      Result global_keyprefix
自动注入: wblog:{env}:...   自动注入: wblog:{env}:...   自动注入: wblog:{env}:...
```

### 3.1 网关 1：Django Cache 统一 Key 装饰器 (KEY_FUNCTION)

通过 `django-redis` 原生支持的 `KEY_FUNCTION`，接管所有通过 `cache.get/set/delete` 发起的请求：

```python
# wagtailblog3/settings/database.py

def unified_key_maker(key: str, key_prefix: str, version: int) -> str:
    """
    Django Cache 全局键识别网关：
    将业务传入的相对 key 自动规范化为: wblog:{env}:{clean_prefix}:{clean_key}
    """
    app_id = "wblog"
    env_id = os.environ.get("WAGTAILBLOG_ENV", "test").strip().lower()
    prefix = key_prefix.strip(":") if key_prefix else "cache"
    user_key = key.strip(":")
    return f"{app_id}:{env_id}:{prefix}:{user_key}"

def unified_reverse_key(key: str) -> str:
    """反向解析提取用户原始 key，供 keys / delete_pattern 使用"""
    parts = key.split(":", 3)
    return parts[3] if len(parts) >= 4 else key
```

**配置注入点**：
在 `CACHES['default']` 与 `CACHES['comment_rate_limit_cache']` 中指定：
```python
'OPTIONS': {
    'CLIENT_CLASS': 'django_redis.client.DefaultClient',
    'KEY_FUNCTION': 'wagtailblog3.settings.database.unified_key_maker',
    'REVERSE_KEY_FUNCTION': 'wagtailblog3.settings.database.unified_reverse_key',
    'PASSWORD': REDIS_PASSWORD,
}
```

### 3.2 网关 2：Celery 消息代理全局前缀网关 (Kombu Gateway)

在 `get_celery_config` 中激活 Kombu 原生提供的 `global_keyprefix` 机制：

```python
# wagtailblog3/settings/database.py -> get_celery_config()
env_id = os.environ.get("WAGTAILBLOG_ENV", "test").strip().lower()

celery_settings = {
    # 注入 Kombu 消息队列全局命名空间
    'broker_transport_options': {
        'global_keyprefix': f'wblog:{env_id}:broker:',
    },
    ...
}
```

**技术成效**：
- Kombu 会自动将 `_kombu.binding.maintenance` 转写为 `wblog:prod:broker:_kombu.binding.maintenance`；
- 将队列 List 转写为 `wblog:prod:broker:maintenance`；
- **彻底杜绝与商城的 `_kombu.binding.shop` 发生任何交叉**。

### 3.3 网关 3：Celery 结果后端全局前缀网关 (Result Gateway)

```python
# wagtailblog3/settings/database.py -> get_celery_config()
celery_settings['result_backend_transport_options'] = {
    'global_keyprefix': f'wblog:{env_id}:result:',
}
```

**技术成效**：
- 任务执行结果元数据键自动命名为：`wblog:prod:result:celery-task-meta-<task_id>`；
- 与其他系统的结果键物理隔离，所有权 100% 明确。

### 3.4 网关 4：原生操作门面工厂 (RedisKeyBuilder)

针对列表页 Single-Flight 分布式锁、代次计数器等直接调用 Redis Client 的场景，提供门面工厂类：

```python
# wagtailblog3/apps/blog/services/redis_protocol.py

class RedisKeyProtocol:
    """业务层键构造契约工厂，禁止在业务中硬编码拼接字符串"""

    @staticmethod
    def listing_cache_key(site_id: int, locale_id: int, page_id: int, query_hash: str, gen: int) -> str:
        return f"listing:page:{site_id}:{locale_id}:{page_id}:{query_hash}:v2:{gen}"

    @staticmethod
    def listing_lock_key(site_id: int, locale_id: int, page_id: int, query_hash: str) -> str:
        return f"listing:lock:{site_id}:{locale_id}:{page_id}:{query_hash}"

    @staticmethod
    def listing_generation_key(scope: str) -> str:
        return f"listing:gen:{scope}"
```

---

## 4. 物理库重组与双重隔离架构 (Physical + Logical Separation)

即使逻辑前缀已经做到 100% 隔离，为了符合生产高可用规程，对 Redis 16 个数据库进行物理角色划分：

```
[Redis 6379 实例 16 个物理库全景规划]
┌───────┬───────────────────────────────────┬─────────────────────────────────┐
│ 库号  │ 规划角色                          │ 隔离策略与说明                  │
├───────┼───────────────────────────────────┼─────────────────────────────────┤
│ DB 0  │ [保留]                           │ 预留未分配                      │
│ DB 1  │ [博客生产] 核心业务缓存 (default) │ 强制注入前缀 wblog:prod:cache:  │
│ DB 2  │ [商城专用] 商城业务缓存/队列      │ 留给 /home/source/Django/shop   │
│ DB 3  │ [商城专用] 商城浏览历史           │ 留给商城专用                    │
│ DB 4  │ [商城专用] 商城购物车             │ 留给商城专用                    │
│ DB 5  │ [测试环境] 测试核心缓存           │ 强制注入前缀 wblog:test:cache:  │
│ DB 6  │ [测试环境] 测试评论限流缓存       │ 强制注入前缀 wblog:test:rate:   │
│ DB 7  │ [测试环境] 测试 Celery Broker     │ 强制注入前缀 wblog:test:broker: │
│ DB 8  │ [测试环境] 测试 Celery Result     │ 强制注入前缀 wblog:test:result: │
│ DB 9  │ [保留]                           │ 预留空闲                        │
│ DB 10 │ [商城专用] 商城 Celery 结果       │ 留给商城现有 Celery 使用        │
│ DB 11 │ [博客生产] 评论/访问限流缓存      │ 强制注入前缀 wblog:prod:rate:   │
│ DB 12 │ [保留] (原测试残留待清理)         │ 注销后留作备用                  │
│ DB 13 │ [保留]                           │ 预留空闲                        │
│ DB 14 │ [博客生产] Celery Broker (队列)   │ 强制注入前缀 wblog:prod:broker: │
│ DB 15 │ [博客生产] Celery Result (结果)   │ 强制注入前缀 wblog:prod:result: │
└───────┴───────────────────────────────────┴─────────────────────────────────┘
```

---

## 5. 针对性运维识别与审计能力

### 5.1 秒级所有权审计脚本 (Key Ownership Inspector)

通过统一前缀，运维可直接执行精准扫描统计：
```bash
# 检查当前库中属于博客生产环境的全部键
redis-cli -n 1 --scan --pattern "wblog:prod:*" | wc -l

# 检查是否存在非法无主键（孤儿键）
redis-cli -n 1 --scan --pattern ":1:*" | head -n 20
```

### 5.2 手术刀级批量针对性失效契约

当需要清理博客列表缓存时，绝不调用全局 `cache.clear()`：
```python
# 仅失效博客生产列表缓存，商城 Session、验证码、队列 100% 毫发无损
cache.delete_pattern("listing:page:*")
```
底层的 `KEY_FUNCTION` 会自动将 pattern 变换为 `wblog:prod:cache:listing:page:*`，匹配范围严格收敛在自身模块内。

### 5.3 历史孤儿键渐进式无害化回收方案

对 DB 1 中残留的旧 `:1:blog-detail:v1:generation:*` 与 DB 4 中的 `pv:total:*`：
1. 编写只读识别脚本，确认其数据均已由新前缀键（`wblog:prod:`）替代接管；
2. 为旧键批量设置安全倒计时 `EXPIRE key 86400`（24 小时）；
3. 24 小时后 Redis 自动回收物理内存，实现零停机、零风险的垃圾回收。
