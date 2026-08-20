# Markdown 本地导入博客方案

## 背景与现状证据

- 用户希望将本机目录中的 Markdown 文章（例如 `F:\openclaw\workspace\kaogong\第八章.md`）上传为博客文章，并把同目录相对引用的图片一并迁入网站。
- 已抽样核对 `第八章.md`：图片使用 `第八章.assets/image-...png`、`第八章.assets/...jpg` 形式的相对路径；该目录与 Markdown 文件同级。
- `blog.BlogPage` 已有 `markdown_block`，且 `VditorMarkdownBlock` 在渲染时才通过项目的 `MarkdownRenderer` 转 HTML，MongoDB 中保留每个 Markdown 片段的原始字符串。因此导入会按媒体和特殊代码块位置拆分 StreamField，不把整篇文章转换为富文本或 HTML。
- `MarkdownRenderer` 已使用 `Markdown==3.8` 和 `nh3` 白名单清洗。依赖中还存在 `markdown-it-py==3.0.0`，可用于客户端识别图片令牌，但不能以正则替代 Markdown 语法解析。
- 项目已有 `audio_block`、`video_block` 和 `embed_block`；`settings/third_party.py` 已注册 Bilibili、YouTube 以及部分音乐平台的 Wagtail embed finder。这些能力只对 StreamField 媒体块生效，不会自动把 Markdown 中的普通 URL 变成播放器。
- 当前 Markdown 安全白名单没有把 `audio`、`video`、`source` 或任意 `iframe` 作为普通 Markdown HTML 放行；直接上传后写入这些 HTML 会被清洗或引入不受控的外链风险。
- 远程图片可以作为导入源，但不能让网站服务端无条件抓取用户提交的 URL。推荐客户端下载后按本地图片处理，再通过受认证 API 上传；服务器仍必须执行最终图片格式、内容和大小校验。
- Wagtail 后台已有受管理员权限保护的图片上传入口和 `BlogImage` 模型；现有图片入口不适合作为公开客户端 API，因为它依赖后台会话和 Vditor 的单图交互。
- 已有 `add_blog_page` 管理命令可以写入 Markdown，但它面向服务器本机运行，且默认发布页面；它不能满足外部客户端、媒体导入、失败报告和“只创建草稿”的要求。
- 已有 BlogPage 元数据建议服务，可基于 Markdown 块生成标题、简介和标签建议，但不自动保存或发布。正文属于受保护数据，调用外部 AI 前必须获得明确确认。

## 产品结论

这个需求合理，但应收敛为“本地 Markdown 导入器”，而不是一开始开发桌面 GUI、同步系统或开放匿名上传接口。

推荐第一版由两个受控组件组成：

1. 本地命令行客户端：读取指定 `.md` 文件及受限目录内的图片，显示预检报告并调用网站接口。
2. 网站端受认证导入 API：验证身份、创建 Wagtail 草稿、接收图片、重写图片 URL、返回可继续在后台审阅的页面链接。

命令行客户端适合当前“从本机路径导入单篇或少量文件”的工作流：安装和维护成本最低，能够可靠访问本地文件；以后如确有非技术编辑者需求，再在同一导入协议上增加小型桌面界面。客户端绝不直接访问 MySQL、MongoDB、MinIO 或生产服务器文件系统。

**已确认的结构决策**：导入后的 `BlogPage.body` 是有序的 StreamField 块序列，例如 `[markdown_block, embed_block, markdown_block, ...]`，不再是一个完整 Markdown 文档。图片、视频、音频、已注册流媒体和 Mermaid 图表都转换为各自的现有模块；普通正文和未转换的 Markdown 语法保留在相邻的 `markdown_block` 片段中。

**已确认的流媒体决策**：Bilibili、YouTube、网易云音乐、QQ 音乐、酷狗和咪咕音乐链接进入现有 `embed_block`，而不是只显示为普通超链接；导入器在媒体块前后保留 Markdown 片段。

## 目标

1. 用户选择一个 Markdown 文件和一个目标博客索引页。
2. 客户端识别并校验受支持的本地图片；网站将图片存为具有权限和校验的 Wagtail 图片。
3. 文章正文仍是原始 Markdown，仅把已成功上传的本地图片引用替换为网站媒体 URL。
4. 网站创建 `BlogPage` 草稿，归档到用户选择的 `BlogIndexPage` 下，不自动发布。
5. 标题、简介和标签可由用户填写或由现有 AI 元数据能力生成“可审阅建议”；任何建议均不绕过人工确认和 Wagtail 保存/发布流程。
6. 客户端给出可操作的成功、部分失败和拒绝原因，不静默遗漏图片或发布文章。

## 非目标

- 不批量扫描、监听或自动同步整个目录；首期每次只导入一篇文章。
- 不自动发布、不覆盖既有页面、不批量更新文章，也不删除媒体。
- 不改变既有块的 key、StreamField schema、MongoDB 正文、草稿快照或 revision pointer；导入只组合现有 `markdown_block`、`image_block`、`video_block`、`audio_block`、`embed_block` 和 `mermaid_chart`。
- 不支持把 PDF、Office 文件、Obsidian 嵌入语法、Wiki 链接或任意 Markdown 扩展作为首期承诺；客户端必须明确报告不支持项。首期支持独立 HTML `<img src>`，其本地文件和显式允许的 HTTPS 远程图片按图片块处理；标准 Markdown 远程图片同样必须下载并转换为博客网站受管图片，不保留为默认外链。音视频文件可以在首期上传为受管媒体，但“上传成功”与“文章内出现播放器”是两个独立能力。
- 不把网站 API 公开给匿名用户，不在客户端保存账号密码或 API 密钥。
- 不新增 systemd unit、端口或 Nginx 规则；失败 MinIO 对象的精确补偿重试复用既有 `maintenance` Worker 和 Beat，属于现有 Celery 任务/调度变更，必须更新 `systemctl.md` 并通过服务验收。

## 用户流程与验收口径

### 首期流程

1. 用户在客户端指定 Markdown 文件，例如 `第八章.md`，并选择或传入网站地址。
2. 客户端从网站读取当前用户可写入的博客索引页列表，用户选择一个 `BlogIndexPage`。
3. 客户端执行本地预检：文件编码、文章长度、首个一级标题、图片数量与大小、相对路径、缺失文件、重复引用、不支持语法和预计上传体积。
4. 用户在预览中确认标题、发布日期、目标索引页、是否请求元数据建议，以及将创建“草稿”。
5. 客户端上传文章和图片。服务端只有在全部必需图片校验、保存和 Markdown 重写成功后，才创建 BlogPage 草稿和 revision。
6. 客户端显示文章 ID、后台编辑链接、文章预览链接、已上传图片数和失败项。用户进入 Wagtail 后台审阅正文、元数据和图片，再使用既有发布功能发布。

### 成功标准

- `第八章.md` 中每个受支持且存在的相对图片都能在导入后的草稿中显示为网站受管图片。
- 导入后的 `BlogPage.body` 按源文件顺序生成块序列：文本运行是 `markdown_block`，图片是 `image_block`，本地视频是 `video_block`，本地音频是 `audio_block`，已注册远程平台链接是 `embed_block`，Mermaid fenced code 是 `mermaid_chart`。每个 Markdown 片段的文本与原文件对应片段保持一致。
- 草稿位于所选索引页下，不能出现在错误栏目。
- 图片路径缺失、超限、越过授权根目录、内容伪装或用户无权写入索引页时，导入失败且不创建 BlogPage。
- AI 元数据失败不影响草稿和图片；用户可在 Wagtail 编辑页手工填写或重试建议。草稿绝不因 AI 成功而自动发布。

## 需求细化与决策

### 文章元数据

客户端应先读取可选 YAML front matter；没有 front matter 时采用以下候选值，并在预览中允许修改：

| 字段 | 优先级 | 规则 |
| --- | --- | --- |
| 标题 | `title` > 第一条 H1 > 文件名 | 必填，服务端校验长度和同父级 slug 冲突。 |
| 发布日期 | `date` > 客户端当天日期 | 创建草稿时写入，不代表发布时刻。 |
| slug | `slug` > 由标题生成 | 用户可编辑；服务端在目标父页范围内保证唯一。 |
| 标签 | `tags` > AI 建议 > 空 | 使用既有 taggit 规则，用户最终确认。 |
| 简介 | `intro` > AI 建议 > 空 | BlogPage 当前字段必填；没有 AI 或手工值时，客户端必须要求填写，不能用整篇正文代替。 |
| 目标索引页 | 显式选择 | 仅显示当前用户有权限新增 BlogPage 的索引页。 |

推荐支持的 front matter 最小样例：

```yaml
---
title: 第八章 判断推理
date: 2026-08-16
tags: [判断推理, 图形推理]
intro: 供审阅的人工简介
---
```

front matter 是客户端元数据，不进入 `markdown_block`。其字段使用白名单，未知字段显示提示但不传给服务端。

### 图片与路径规则

- 首期支持 Markdown 行内图片 `![替代文字](相对路径 "可选标题")` 与引用式图片定义；保留原有 alt 和 title。
- 客户端以 `--source-root` 为安全根目录，默认是 Markdown 所在目录。每个引用先规范化、解析真实路径，再确认其仍在根目录内；指向根目录外、网络共享、符号链接/联接逃逸的文件一律拒绝。
- 只接受项目现有 Wagtail/Pillow 实际验证成功的图片类型；客户端先按扩展名和大小预检，服务器仍作为最终内容校验方。首期默认上限应复用 `WAGTAILIMAGES_MAX_UPLOAD_SIZE`，并在接口返回有效限制；不在客户端硬编码第二套限制。
- 同一导入批次内，同一个规范化本地路径只上传一次，多个引用位置复用同一个新建 Wagtail 图片对象；不同路径即使内容相同，首期也不自动合并。
- 远程 `https://` 图片默认在预检中标记为“外部依赖”。只有用户显式确认 `--allow-external-images` 后，客户端才下载到临时文件、校验并作为 multipart 媒体上传；服务端不按 URL 主动抓取。
- 缺失图片、不可读文件、客户端远程下载失败、拒绝类型和无法解析的图片语法，默认均按单个媒体失败处理：该媒体进入 `failed_missing`，原位置插入独立缺失标记，其他媒体继续导入。客户端可提供“严格预检”选项，但它只能在任何文件上传或草稿写入前由用户取消本次导入，不能改变服务端“单媒体失败不回滚其他成功媒体”的提交协议。

### 本地视频、音频与相对链接

客户端必须先把 Markdown 中的链接按 URL 类型分类，而不是只按扩展名猜测：

| 来源/写法 | 首期处理 | 文章中的结果 |
| --- | --- | --- |
| `[演示视频](assets/demo.mp4)`、`[音频](assets/intro.mp3)` | 解析相对路径并上传；服务端按实际 MIME 和内容校验 | 分别生成 `video_block`、`audio_block` |
| `![视频](assets/demo.mp4)` | 根据实际媒体类型报告“图片语法引用了视频”；不作为图片上传 | 该引用按单媒体失败处理并生成独立缺失标记；其他媒体继续导入 |
| 仅含一个安全本地 `src` 的 HTML `<video>`/`<audio>` | 解析 `src`、按 source root 约束定位并上传；多 source、远程 source 或无法确定主资源时标记失败 | 校验成功分别生成 `video_block`/`audio_block`；失败则在原位置生成独立缺失标记 |
| `![说明](https://example.com/image.png)` | 客户端按安全规则下载到临时文件，再按本地图片上传 | 正文中的 URL 改写为博客网站图片 URL |
| 独占段落的 `https://www.youtube.com/watch?v=...`、`https://youtu.be/...` | 只接受 HTTPS、精确域名白名单和现有 OEmbed finder 能识别的 URL | 转换为项目现有 `embed_block` |
| 独占段落的 `https://www.bilibili.com/video/BV...` | 校验精确域名、路径和 BV 号；不下载视频、不抓取页面 | 转换为项目现有 Bilibili `embed_block` |
| 独占段落的网易云歌曲/外链播放器 URL | 只接受现有 `NetEaseMusicFinder` 支持的歌曲 ID、outchain 或播放器 URL | 转换为项目现有音频 `embed_block` |
| 独占段落的 QQ 音乐、酷狗、咪咕歌曲 URL | 只接受各现有 finder 支持的单曲 URL 与精确平台 ID | 转换为项目现有音频 `embed_block` |
| `#章节锚点`、`https://...`、`mailto:` | 不上传、不改写；按安全协议白名单保留 | 普通链接 |
| `其他章节.md`、`../资料/文件.pdf` 等本地非媒体文件 | 解析并报告未导入目标；首期默认阻止提交 | 不生成指向服务器文件系统的链接 |

首期推荐支持上传但不承诺浏览器原生播放的媒体类型：视频 `mp4`、`webm`、`ogv`；音频 `mp3`、`ogg`、`wav`。具体 codec、时长、分辨率、单文件大小和总上传大小由服务器返回的限制决定，不能仅依据扩展名放行。Wagtail `VideoBlock`/`AudioBlock` 的选择器和模板可以作为后续播放器实现的复用基础。

相对链接必须以 Markdown 文件所在目录或用户选择的 `--source-root` 解析，解析结果不得越过根目录。客户端不得把 Windows 绝对路径、`file://`、`javascript:`、`data:`（除明确允许的内联图片）或服务器本地路径发送给 API。

### 远程图片下载与网站格式校验

- 只识别标准 Markdown 图片和独立 HTML `<img src>` 的远程 `https://` URL；默认不下载普通超链接或 `http://` 图片。用户可在客户端预览中看到每个远程图片的来源 URL、最终文件名、大小和校验结果。
- 下载由本地客户端完成，服务端不根据 URL 主动访问互联网。客户端必须限制连接超时、响应字节数、重定向次数，并在每次重定向后重新检查 HTTPS、主机名和禁止的本地/私有地址；不允许 `file://`、回环地址、内网地址、云元数据地址或带用户密码的 URL。
- 客户端负责体验层预检：检查扩展名/大小、远程 HTTPS 下载安全、重定向/响应上限、真实图片解码和预计上传限制。下载连接、HTTP 状态、超时、响应超限、损坏或无法解码等错误记录为 `client_download_failed`，失败文件不进入 multipart。
- “博客网站支持的格式”由服务端实际 Wagtail 图片表单和运行时限制决定。API 的 `GET /destinations/` 或 `GET /limits/` 返回允许的 MIME、扩展名、单图大小、总上传大小和像素限制；客户端只能用它做预检，服务端校验是最终依据。
- 服务端负责安全与合规边界：收到 multipart 后重新检查文件签名、解码结果、实际 MIME、格式、像素尺寸、单文件/总请求大小和 Wagtail 图片表单。格式伪装、损坏内容、不支持格式、尺寸或体积超限记录为 `server_validation_failed`，只拒绝该媒体，不创建其对象或媒体块，其他合格媒体继续处理。客户端预检通过不代表服务端必然接受；客户端校验服务于及时反馈，服务端校验服务于安全与站点规则，两者不得互相替代。
- 首期默认只承诺服务器返回列表中的常见图片类型，例如 JPEG、PNG、GIF、WebP；不承诺 TIFF、SVG、HEIC/HEIF、AVIF 等格式，除非测试环境的 Wagtail/Willow 明确返回并验证它们。SVG 即使扩展名允许，也必须单独评估脚本和外链风险。
- 上传成功后，正文中的远程图片引用改写为博客站点受管图片 URL，并记录脱敏的来源域名和内容摘要用于本次导入审计；不把远程站点 URL 作为前台长期依赖。
- 同一导入批次内，同一个规范化远程 URL 只下载和上传一次，多个引用位置复用同一个新建 Wagtail 图片对象。规范化只处理 scheme/hostname 大小写、默认端口和 fragment；path/query 保留。
- 首期不跨 Markdown 文件或导入批次自动复用媒体：不同 URL 即使 SHA-256 相同、不同本地路径即使内容相同，也不合并。跨批次内容寻址复用需另行设计 collection 权限、引用计数、所有权和删除生命周期。
- 远程图片下载或上传失败时按单个媒体处理：该媒体标记为 `failed_missing`，原位置插入缺失标记，其他成功媒体继续导入。只有页面、revision 或 MongoDB 最终组装失败时，才进入页面级批次补偿。临时文件在客户端清理，服务端只对本次导入创建且确认未被引用的媒体执行精确补偿。

### StreamField 拆分与块映射

拆分是首期确定方案，不再保留“整篇文章一个 Markdown 块”的备选路径。解析器按 Markdown AST 的块级和行内令牌遍历源文件，维护当前 Markdown 片段；遇到可转换节点时先提交片段，再插入对应块，随后继续收集下一个片段。空片段不创建空块。

| 源内容 | 导入后的块 | 块值规则 |
| --- | --- | --- |
| 普通段落、标题、列表、链接、普通代码和表格 | `markdown_block` | 保留该片段的 Markdown 字符串，使用项目现有渲染器展示。表格的详细边界见下文。 |
| Markdown 图片（本地或远程下载后） | `image_block` | 创建/上传 Wagtail `BlogImage`，保留 alt/title；不把图片 URL 留在 Markdown 片段中。 |
| 本地视频链接 | `video_block` | 创建 Wagtail Media 视频对象，标题取链接文字或文件名；使用现有 `VideoBlock`。 |
| 本地音频链接 | `audio_block` | 创建 Wagtail Media 音频对象，标题取链接文字或文件名；使用现有 `AudioBlock`。 |
| Bilibili、YouTube、网易云、QQ、酷狗、咪咕独占段落链接 | `embed_block` | 使用现有 `CustomEmbedBlock` 和对应 finder；标题取链接文字或平台默认标题。 |
| fenced code 的 info string 为 `mermaid`（大小写归一） | `mermaid_chart` | `code` 保存围栏内部代码；`renderer` 使用当前默认值。该围栏及其代码不得在任何 `markdown_block` 中重复保存。 |
| 简单 Markdown 表格 | 首期仍为 `markdown_block` | 导入阶段保持原始 Markdown 和可回溯性；以后只能由编辑者显式转换为 `table_block`。 |
| 复杂 HTML 表格、`rowspan`/`colspan`、表格内 LaTeX 或嵌套 HTML | `markdown_block` | 原样保留并由现有 Markdown 渲染器安全清洗；不自动转换为 `table_block`，避免丢失结构、公式或混合内容。 |
| 任意原始 HTML | 不生成 `raw_html` | 导入器不得把 Markdown 文件中的 HTML 自动提升为原始 HTML 块。 |

例如，导入结果可以是：

```text
BlogPage.body = [
    ("markdown_block", "# 第八章\\n\\n正文前半段..."),
    ("image_block", <BlogImage>),
    ("markdown_block", "\\n图片说明...\\n\\n"),
    ("video_block", <WagtailMedia>),
    ("markdown_block", "\\n视频后的正文...\\n\\n"),
    ("embed_block", {"title": "课程视频", "embed_url": <EmbedValue>}),
    ("mermaid_chart", {"code": "graph TD; A-->B;", "renderer": "..."}),
    ("markdown_block", "\\n结尾正文...")
]
```

图片、视频和音频的来源 URL、文件名和校验摘要只用于导入清单与审计，不作为 Markdown 正文块的替代内容。普通链接中的媒体 URL只有在能够准确映射到上述块时才拆分；无法识别或不满足独占段落规则的链接留在 Markdown 片段中或按预检策略拒绝。

### 缺失媒体标记

- 每个失败的图片、视频或音频都在原媒体位置生成一个独立的 `markdown_block`，不并入前后正文块，也不创建空的 `image_block`、`video_block` 或 `audio_block`。因此实际序列可以是 `markdown_block, image_block, markdown_block(缺失标记), markdown_block, ...`；连续 Markdown 块在这里是有意保留的审阅边界。
- 标准内容为纯文本 `[导入缺失：{媒体类型} 原始引用：{安全化引用} 原因：{错误码}]`。不使用 `>`，避免渲染为文章引用；不使用 HTML，避免绕过 Markdown 清洗或被误识别成现有原始 HTML 能力。
- `安全化引用` 只保留文件名、相对路径末段或脱敏域名，不得出现本机绝对路径、查询参数、完整远程 URL、Token、凭据或异常堆栈。`错误码` 只从有限集合中选择，例如 `local_file_missing`、`client_download_failed`、`server_validation_failed`、`storage_write_failed`；详细诊断只进入受限导入结果，且同样脱敏。
- 缺失标记与前后 Markdown 片段分离，便于编辑者在 Wagtail 后台定位、替换或删除。含缺失媒体的 batch 状态为 `committed_partial`，可以创建草稿并进入人工审阅，但导入 API 永不自动发布；发布前是否允许保留缺失标记由现有编辑权限和人工审核决定。

### 表格块与原始 HTML 的产品定位

现有 `table_block` 定位为“站内可继续编辑的结构化资料块”，而不是 Markdown 导入的默认表格容器。它适合课程表、参数表、对比表和统计表等规则二维数据，并已具备表头、首列表头、caption、`rowspan`/`colspan` 元数据、单元格 CSS 类和响应式前台模板。

首期导入时，普通 Markdown 表格和复杂 HTML 表格都保留在 `markdown_block` 中。这样可以完整保留原始 Markdown、HTML 合并单元格、表格内公式和混合内容。后续可以增加显式的“转换为可编辑表格”操作，但只能转换无嵌套 HTML、无合并单元格、无复杂公式的规则 Markdown 表格；转换前必须展示预览并由编辑者确认，解析或转换失败不得覆盖原 Markdown。

`raw_html` 仅作为受信任高权限用户的人工逃生口，用于现有块无法表达且已经审核的特殊 HTML。Markdown 导入 API 不生成该块，也不把 `<table>`、`<iframe>`、脚本、事件属性或其他 HTML 自动写入 `raw_html`。导入内容继续经过现有 Markdown 渲染器和 `nh3` 安全清洗；`raw_html` 权限、审计和前台 CSP 需在实现阶段按真实配置核验。

### 本地音视频与远程嵌入

本地音视频首期直接生成 `video_block`/`audio_block`，不再退化为下载链接。已注册的 Bilibili、YouTube、网易云音乐、QQ 音乐、酷狗和咪咕音乐直接生成 `embed_block`，不经过 Markdown HTML 白名单。未经验证不能直接放宽 `nh3` 的 HTML 白名单或信任任意远程 iframe。

### 已注册流媒体到流媒体块的规则

导入器仅在以下任一形式独占一个 Markdown 段落时转换：

```markdown
[课程演示视频](https://www.bilibili.com/video/BV1xx411c7mD/)

https://www.youtube.com/watch?v=VIDEO_ID

[课程音频](https://music.163.com/song?id=SONG_ID)
```

- 链接文字作为 `embed_block.title`；裸 URL 使用“Bilibili 视频”“YouTube 视频”“网易云音乐”“QQ 音乐”“酷狗音乐”或“咪咕音乐”作为标题。导入器不爬取平台页面补标题。
- 服务端使用现有 `CustomEmbedBlock` 和 `WAGTAILEMBEDS_FINDERS` 建立流媒体值：Bilibili 使用项目 `BilibiliFinder`，YouTube 使用 Wagtail OEmbed finder；网易云、QQ、酷狗、咪咕分别使用已注册的音乐 finder。
- API 在交给 finder 前必须以 URL 解析器验证 `https`、精确 hostname、允许端口、允许路径与平台 ID。现有 finder 的正则只用于识别，不应作为 API 的安全边界。
- 解析失败、平台不可用或 URL 不被 finder 接受时，导入必须返回明确的媒体错误；不应静默降级为普通链接。客户端可让用户移除该链接或显式改为普通外链后重新预检。
- 夹在句子、列表项、表格或代码块中的流媒体链接保持普通链接，不改变 Markdown 结构。需要嵌入时，用户把链接单独放成一段即可。
- 远程流媒体不上传、下载、转码或代理视频。第三方播放器的隐私、地区限制、内容下架和 CSP/网络失败通过现有流媒体模块的加载状态处理，仍须浏览器验收。
- Mermaid 仅识别标准 fenced code（```` ```mermaid ```` 或带大小写差异的同类 info string）；围栏内代码原样且仅进入 `mermaid_chart.code`，不得回写或复制到相邻/其他 `markdown_block`。普通 ` ```text `、` ```javascript ` 等代码围栏继续留在 `markdown_block`，Mermaid 语法校验失败时导入预检报告错误，不静默生成空图表。

### Markdown 模块的使用边界

用户提出“尽量用 Markdown 模块”是正确方向，但 Python `Markdown` 的职责是把 Markdown 渲染为 HTML，不提供可安全回写原始 Markdown 源码的位置模型。首期采用以下分工：

- 使用项目现有 `markdown` 配置验证每个导入后的 Markdown 片段能够按网站规则渲染，并保留各片段的原始 Markdown 字符串；整篇源文件不再作为单一正文块保存。
- 使用已安装的 `markdown-it-py` 在客户端识别 Markdown 图片和引用定义，精确获得需要替换的 URL 位置；不使用正则批量改写正文。
- 服务端再次通过同一渲染配置和 `nh3` 清洗进行验证。客户端预览只是辅助，网站服务端才是最终安全边界。

这既复用当前的 Markdown 模块，又避免先转换为 HTML 再反向还原 Markdown 时损坏内容、表格、代码块或公式。

### 简介、标签与 AI

“根据文章内容生成简介和标签”应定义为可选的元数据建议，不是创建文章的前置条件。

- 客户端预览页应有明确开关“请求 AI 元数据建议”，默认关闭，文字说明会将当前文章文本发送到已配置的外部 AI 服务。
- 客户端在开关开启后，必须从服务端读取并显示当前“已启用”的博客 AI 提示词模板；不得把某个固定模板硬编码到导入程序。不同 Markdown 文件可各自选择一个模板，所选模板 ID 仅属于该文件对应的元数据建议请求，不改变目标索引页或其他文件的选择。
- 模板选择可为空；为空时不发起 AI 请求。客户端只在用户显式执行“生成建议”后发送该文件的受限文本与所选模板 ID，不在扫描、预检或上传媒体时自动外发正文。
- 服务端复用现有严格 JSON 契约：标题、纯文本简介、3 至 5 个标签；导入流程仅回填简介和标签建议，标题仍以该文件当前填写值为准。服务端在每次请求时重新验证模板存在、已启用且提示词完整，拒绝客户端缓存的停用或失效模板。
- 外部请求必须保持 `store=false`，不得记录正文、图片 URL、MongoDB ID、草稿指针、密钥或完整模型响应。
- 生产环境是否允许真实文章发送给外部模型，必须作为独立生产授权项。未启用、模板失效或生成失败时，客户端保留当前手工内容并要求手工填写必填简介，可空标签创建草稿。

### 认证与权限

不建议让客户端携带 Django session cookie、管理账号密码或长期静态 Token。推荐提供“个人访问令牌”机制：令牌只显示一次、可撤销、只授予 Markdown 导入范围，并绑定 Wagtail/Django 用户。

网站端必须逐请求校验：令牌有效性、用户状态、导入权限、目标 `BlogIndexPage` 的 add 权限、Wagtail 图片 collection 的 add 权限、请求大小、速率限制和 CSRF/认证策略。接口返回的索引页列表必须是权限过滤后的结果，客户端传入的 parent ID 也必须再次验证。

令牌只保存在操作系统凭据库或由用户每次输入；不得写入 Markdown、Git、配置示例、日志或命令历史。实现时需要选择 Windows Credential Manager 的具体接入方式；在此之前可以只支持交互式粘贴且关闭命令回显。

## 推荐 API 草案

API 仅服务于受认证客户端，版本前缀为 `/api/v1/markdown-import/`。路径和字段为设计草案，需在实现前与现有 DRF/JWT 配置核对。

| 接口 | 作用 | 写入行为 |
| --- | --- | --- |
| `GET /destinations/` | 返回当前用户可导入的 BlogIndexPage、图片 collection 和有效限制 | 无 |
| `POST /preflight/` | 校验元数据、目标父页、文件清单摘要和 Markdown 结构，返回错误/限制 | 无 |
| `POST /imports/` | 单个 multipart 请求包含 Markdown、图片和元数据，创建草稿 | 创建图片、BlogPage、revision、MongoDB 正文 |
| `GET /imports/{id}/` | 返回本次导入的最小状态、页面 ID 和后台 URL | 无 |

首期使用单次 multipart 请求，但不把它误称为覆盖 MinIO 的数据库事务。请求中的 `media[]` 必须包含客户端建立的逻辑 ID、`kind`（`image`/`video`/`audio`）、原始 URL、来源类型（`local`/`remote`）、文件和处理模式。远程图片必须已经由客户端下载为 multipart 文件，服务端不接受只给 URL 就替网站抓取。`media_references` 映射逻辑 ID 到原始 URL。服务端完成媒体创建后，才对 Markdown 中已解析的位置进行替换。

### 幂等键与重复提交

- 幂等属于 P1 的数据安全能力。客户端在用户确认一次新的导入时生成 UUIDv4 `idempotency_key`；同一次导入因连接中断、超时或进程内自动重试时必须复用该键。用户重新选择文件，或修改目标页、元数据、Markdown、媒体清单后，视为新导入并生成新键。
- 客户端同时发送请求指纹所需清单；服务端不得直接信任客户端指纹，而应重新计算规范化请求的 SHA-256。指纹输入固定包含：目标 parent ID、影响页面结果的元数据、原始 Markdown 文件 SHA-256，以及按源文位置排序的媒体 `kind`、上传文件内容 SHA-256、来源类型和安全化来源标识。JSON 使用固定字段顺序和 UTF-8 编码；本机绝对路径、凭据和完整敏感 URL 不进入指纹或审计记录。
- `MarkdownImportBatch` 对 `(user_id, idempotency_key)` 建唯一约束，并保存服务端请求指纹。相同用户、相同键且指纹相同：若已完成，返回原 batch、page ID、后台 URL 和原状态，不再创建草稿或媒体；若仍处理中，返回同一 batch 的 `processing` 状态，客户端通过查询接口继续获取结果。
- 相同用户、相同键但指纹不同返回 HTTP `409 idempotency_conflict`，不得覆盖旧 batch 或继续写入。不同用户的相同 UUID 不共享结果，也不能用于探测他人的 batch。
- 并发到达的同键请求依靠数据库唯一约束和短事务竞争创建权；唯一约束失败的一方重新读取既有 batch，并按“同指纹复用、异指纹冲突”处理。不能只做“先查再建”，否则并发超时重试仍可能产生重复草稿。

### MinIO 媒体补偿清理协议

Django `transaction.atomic()` 只覆盖 MySQL，不能回滚 MinIO/S3 object。导入实现必须采用“数据库事务 + 持久化对象清单 + 补偿”的流程。单个图片、音频或视频失败时，只清理该媒体 artifact 自身可能产生的半上传 object 和未引用模型行；同批其他成功媒体继续保留。只有页面、revision 或 MongoDB 最终组装失败、整个 batch 无法形成可用草稿时，才清理本批全部未被引用的成功媒体。

新增两个仅用于导入状态的 MySQL 记录：

| 记录 | 必要字段 | 用途 |
| --- | --- | --- |
| `MarkdownImportBatch` | UUID、请求用户、幂等键、状态、创建/完成时间、目标 parent ID、page ID（成功后）、缺失媒体数 | 标识一次导入，状态为 `uploading`、`assembling`、`committed`、`committed_partial`、`compensating`、`cleaned` 或 `cleanup_retry`。 |
| `MarkdownImportArtifact` | batch UUID、顺序号、媒体种类、storage alias、精确 object name、关联图片/Media ID、SHA-256、状态、删除错误码 | 记录本批次每个媒体，包括 `stored`、`failed_missing`、`claimed`；作为删除白名单，不记录正文、凭据或完整来源 URL。 |

具体顺序如下：

1. 先验证请求结构、Markdown 拆分结果、目标页面权限、数量和总体限制；每个媒体单独校验类型、大小和内容。某一个媒体校验失败只将该 artifact 标记为 `failed_missing`，不阻止其他媒体继续上传。
2. 以独立提交创建 `MarkdownImportBatch`。每个媒体在写入前先写入 `MarkdownImportArtifact` 的 `reserved` 记录，并为它分配不可冲突的 batch UUID 对象名；实际写入后立即持久化对应的 `storage alias`、`FieldFile.name` 和新建图片/Media ID。实现必须使用实际 `FieldFile.storage` 的精确名称，不得依据 `images/`、`media/` 等字符串猜测 MinIO key。
3. 图片使用 Wagtail 图片表单/模型，音频和视频使用 Wagtail Media 模型；每成功一个对象就更新其 artifact 为 `stored`。成功媒体在页面创建前均不得被其他页面、revision 或导入批次复用。
4. 某个媒体上传或校验失败时，不创建该媒体块，而是在对应位置插入“缺失媒体标记”章节规定的独立 `markdown_block`，例如 `[导入缺失：图片 原始引用：diagram.png 原因：storage_write_failed]`。客户端和后台结果中同时返回脱敏的缺失媒体清单；其他成功媒体继续保留其 `image_block`、`video_block` 或 `audio_block`。
5. 所有媒体处理完成（允许存在 `failed_missing`）后，组装拆分后的 StreamField 并创建 BlogPage 草稿和 revision。没有缺失项时 batch 为 `committed`；有缺失项时为 `committed_partial`，接口返回 `partial_success`，页面仍可进入 Wagtail 审阅和发布流程。
6. 单个媒体上传中途失败时，只清理该 artifact 可能已经写入的 MinIO object 和未引用的 Wagtail 图片/Media 行：确认 artifact 未被页面/revision 引用后调用其 `FieldFile.storage.delete(artifact.object_name)`；不得删除同一 batch 中其他 `stored` 或 `claimed` 媒体。不得只调用模型 `.delete()` 并假定它会删除 MinIO 文件。
7. 如果页面、revision 或 MongoDB 最终组装失败，导致整个 batch 没有可用 BlogPage，才进入 `compensating` 并清理本批所有未被引用的成功媒体；这与“单个媒体失败继续导入”的路径严格区分。当前 `BlogPage.save()` 会先写 MongoDB，故页面级失败补偿还必须记录并精确清理本批新建的 `mongo_content_id` 和 revision 草稿，绝不触碰既有页面的 MongoDB 正文、草稿或 revision pointer。
8. 任一单媒体清理或页面级清理失败，保留 artifact 及 object name 并将 batch 标记为 `cleanup_retry`。同步请求先对失败 artifact 做一次立即重试；仍失败则投递到现有 `maintenance` Worker，并由 Beat 以有限退避领取精确 artifact 重试。只有对应对象删除成功或确认不存在后，才把该 artifact 标记为 `cleaned`；接口必须区分 `partial_success`、`cleanup_complete` 与 `cleanup_pending`，后者不能声称空间已经释放。

该协议覆盖“10 张图片中第 7 张失败”的场景：第 7 张被标记为缺失并在文章对应位置显示缺失标记；第 1 至 6 张以及第 8 至 10 张成功上传的对象继续保留并生成 `image_block`。如果第 7 张在失败前已经产生了部分 MinIO object，只删除第 7 张 artifact 对应的精确 object；音频、视频使用同一规则。不会删除已经存在的同名对象、其他导入批次对象、已被页面引用的媒体或任何生产历史媒体。

## 服务端实现边界

1. 新建独立 `markdown_import` 应用或 `blog` 下独立导入模块，避免把客户端协议混入现有 Vditor 单图端点。
2. 以 Wagtail `add_child()`、`save_revision()` 和现有 BlogPage MongoDB 保存路径创建草稿；调用时显式禁止 publish。
3. 通过 Wagtail 图片表单/权限策略创建图片，复用文件内容校验，不直接写 media 存储路径。
4. 按源文件顺序组装现有 StreamField 块：Markdown 片段使用 `markdown_block`，图片/视频/音频使用对应 chooser 块，已注册流媒体使用 `embed_block`，Mermaid 围栏使用 `mermaid_chart`。图片使用网站可访问的原图或受控 rendition URL；实现前须确认当前模板、媒体域名和 CSP 均允许。
5. 新增导入审计仅记录请求人、时间、源文件名（不含绝对路径）、数量、大小、页面 ID、结果和脱敏错误码；不记录正文、token、图片二进制、完整本地路径或模型内容。
6. 对创建成功但客户端超时的情况，通过客户端生成的幂等键返回同一结果，避免重复创建草稿和图片。

## 分期计划

### P0：方案确认与样本验收

- 选定客户端形态、认证方式、图片 collection、单篇大小/图片数量上限和 AI 外发授权状态。
- 使用匿名或用户提供的非敏感样本检查 `第八章.md` 的图片、代码块、表格、公式、引用图片和 HTML 图片比例。
- 仅输出预检报告和 UI/CLI 原型，不写入测试数据。

### P1：安全的单篇草稿导入

- 实现网站权限、目标索引页枚举、预检和单次 multipart 导入 API。
- 实现 Windows 本地 CLI：选文件、下载并校验远程图片、预检、预览、显式确认、图片/媒体上传、页面结果链接。
- 支持标准图片、本地音视频、已注册远程流媒体和 Mermaid fenced code；按规则创建拆分后的 StreamField 草稿和 revision；不接入 AI。
- 实现 UUIDv4 幂等键、服务端请求指纹、`(user_id, idempotency_key)` 唯一约束和并发重放/冲突处理，网络超时重试不得创建重复草稿或媒体。
- 实现 `MarkdownImportBatch`/`MarkdownImportArtifact`、逐媒体精确 MinIO 对象删除、同步补偿和既有 maintenance/Beat 的失败重试；单个媒体失败时保留其他成功媒体，并在对应位置显示缺失标记。
- 在 WSL2 测试环境以合成文章与图片完成端到端测试，并人工检查 Wagtail 后台和前台草稿预览。

### P2：元数据建议与可靠性

- 接入已有元数据建议服务，保留用户明确开关、人工审阅和生产外发门禁。
- 扩展导入审计、失败恢复提示和跨会话状态展示；P1 已包含防止重复草稿所需的基础幂等能力。
- 对引用式图片、外部图片显式确认和可观测性补充测试。

### P2 媒体扩展（待决策）

- 在 P1 已确定的拆分结构上补充更多 provider、断点上传、媒体复用和批量导入；不得恢复“整篇一个 Markdown 块”的替代方案。
- Bilibili、YouTube、网易云、QQ、酷狗和咪咕按“已注册流媒体到流媒体块的规则”在 P1 验收；其他 provider 仍需 finder、域名白名单、隐私策略、失败占位和前台浏览器验收均通过，才允许增加。

### P3：仅在真实需求出现后评估

- 多文件队列、断点续传、桌面 GUI、版本更新/差异同步、媒体复用和批量导入。
- 每项都需重新评估并发、重复媒体、页面覆盖、审计、回滚和数据保护，不预先加入首期实现。

## 预计修改与不修改文件

预计修改或新增：

- `wagtailblog3/apps/blog/`：导入服务、受权限保护的 API、Wagtail hook/URL、导入审计、补偿任务及测试。
- `wagtailblog3/apps/blog/models.py` 与迁移：新增 `MarkdownImportBatch`/`MarkdownImportArtifact`；导入器组合既有 StreamField 块（Markdown 片段、图片、视频、音频、`embed_block`、`mermaid_chart`），不修改块定义或 MongoDB 存储契约。
- `wagtailblog3/settings/` 与 `.env.example`：仅增加无凭据的导入限制/认证开关说明；真实密钥不进入 Git。
- 独立的本地客户端包或 `tools/` 下 CLI、测试和使用说明。
- 本方案、`说明书/06-API接口文档.md`、`systemctl.md`：记录复用 maintenance Worker/Beat 的补偿重试、状态和健康检查；不新增 unit。

明确不修改：

- 既有 BlogPage、MongoDB 正文、revision pointer、`markdown_block` 存储 key 和生产内容；补偿仅处理可由失败 batch 精确证明的新建媒体和 MongoDB 文档。
- 生产环境文件、systemd unit、Celery 队列、Beat、Filebeat、Nginx、Elasticsearch 和搜索索引。
- 现有 Vditor 图片上传 URL 的会话权限边界。

## 数据与服务影响

- P1 会在测试数据库/MongoDB/MinIO 中创建 Wagtail 图片、音视频、BlogPage 草稿、revision 与正文，也会在失败路径精确删除本批新建对象，属于真实测试数据写入和删除；实施前需要用户针对测试环境明确授权。
- 生产首次启用会创建草稿、图片和音视频，绝不自动发布。生产媒体、MongoDB 正文和页面树均属受保护数据，实施与每次生产导入需按权限和审计边界执行。
- 不新增常驻服务，但补偿失败会复用现有 `wagtailblog3-celery-maintenance.service` 和 Beat。实现时必须更新 `systemctl.md`，核对 task 注册、路由、有限退避、任务幂等性和失败状态，不得新增 `email`/`default` 队列 Worker。

### 测试数据隔离与清理

- 真实集成测试只允许在确认 `WAGTAILBLOG_ENV=test`、测试数据库/MongoDB/Redis/MinIO 端点和测试 collection 后运行；任一目标无法证明属于测试环境时立即拒绝执行。生产环境不运行自动清理测试。
- 每次测试生成唯一 `test_run_id`，并关联精确的 `MarkdownImportBatch`、page ID、revision ID、`mongo_content_id`、Wagtail 图片/Media ID、storage alias 和 object name。测试数据可使用明确标题前缀或专用 collection 便于人工识别，但删除依据只能是这些持久化精确 ID，不能使用标题、路径前缀、时间范围或存储桶全量扫描。
- 测试完成后按引用关系逆序清理：先确认草稿未发布且只属于该 test run，再删除测试 revision/页面和本批新建 MongoDB 测试正文；随后只删除 artifact 明确记录、由该测试创建且已确认未被其他页面/revision 引用的 Wagtail 图片/Media 行及其精确 MinIO object。不得删除既有媒体、共享媒体或无法证明归属的对象。
- 测试断言完成后立即执行清理，并再次查询精确 ID/object name 验证不存在。清理失败时保留 batch/artifact 证据并进入 `cleanup_retry`，由测试环境 maintenance 任务精确重试；测试报告必须列出未清理数量和 batch ID，不能把清理失败隐藏为测试成功。
- 为保留失败复现证据而暂缓清理时，必须显式记录保留原因、test run、对象清单和最晚复核时间；证据确认无用后仍按同一精确清理流程处理，不进行前缀删除。

## 测试与验收

- 单元测试：front matter 白名单、标题/简介校验、Markdown 图片识别和重写、路径逃逸、重复引用、缺失/超限/伪装图片、远程图片重定向与私有地址、外部 URL、幂等键与错误脱敏。
- 媒体测试：MP4/MP3 MIME 与 codec 不匹配、重复媒体引用、相对视频/音频路径、未导入的本地 Markdown/PDF 链接、YouTube/Bilibili/音乐平台域名伪装、`javascript:`/`file:`/任意 iframe、播放器失败占位和移动端无障碍控件。
- StreamField 结构测试：给定交错的正文、图片、视频、音频、远程流媒体和 Mermaid 围栏，断言块类型和顺序准确、Markdown 片段边界无空块、普通代码围栏未被误转、Mermaid 代码完整且仅进入 `mermaid_chart.code`，所有 `markdown_block` 均不包含该围栏或代码。
- 补偿测试：模拟 10 张图片的第 7 张 MinIO 写入失败，断言第 7 张被标记为 `failed_missing`、对应位置有缺失标记，第 1 至 6 张及第 8 至 10 张仍保留并生成 `image_block`；若第 7 张产生半上传 object，只删除第 7 张的精确 object。分别覆盖音频、视频和混合媒体。另模拟页面/MongoDB 最终失败，断言只有页面级失败才清理本批未引用的成功媒体。
- Worker/Beat 测试：补偿失败的 artifact 进入 `cleanup_retry`，maintenance 任务以幂等方式删除精确 object name；重复投递、对象已不存在和模型行已删除时视为可收敛，不得改动其他 batch。
- API 测试：未认证、无导入权限、无目标父页权限、无 collection 权限均拒绝；只提交远程 URL 而没有文件时拒绝；服务端按实际图片格式拒绝伪装或不支持文件；单媒体失败仍创建带缺失标记的草稿并返回 `partial_success`；页面级失败才回滚本批未引用媒体；重复请求不产生重复页面。
- 幂等测试：同用户同键同指纹的串行和并发请求只创建一个 batch/page 与一组媒体；处理中重放返回同一 batch；已完成重放返回原结果；同键不同指纹返回 `409 idempotency_conflict`；不同用户不能读取或复用彼此结果。
- 测试清理验收：清理前验证测试环境和精确 test run；清理后逐一验证测试 page/revision/MongoDB 文档、Wagtail 媒体行和 MinIO object 均不存在；模拟删除失败时必须保留 artifact 并进入 `cleanup_retry`，且不删除其他 batch 或共享媒体。
- WSL2：`python manage.py check`、相关测试、`makemigrations --check --dry-run`、`migrate --plan`（若新增模型）。迁移不在共享测试库或生产执行，除非另获授权。
- 浏览器验收：在 Wagtail 后台检查拆分后的 `markdown_block`、`image_block`、`video_block`、`audio_block`、`embed_block`、`mermaid_chart` 顺序和渲染，目标索引页、元数据、保存与发布前状态；桌面和移动视口检查编辑器、控制台、网络、键盘路径和溢出。产物放入 `output/playwright/markdown-import/`。
- 客户端验收：使用合成 Markdown、本地图片和受控测试站点的远程图片，以 dry-run 和真实测试导入分别验证；不使用生产正文或不明第三方 URL 做自动化样本。

## 回滚点与残余风险

- 代码回滚到导入功能前 commit；不删除由已成功导入生成的页面、图片、音视频、MongoDB 正文或 revision 作为普通回滚操作。
- 对尚未发布的错误草稿，需由有权限人员在明确目标下在 Wagtail 后台处理；是否删除关联媒体必须逐项确认，避免误伤共享图片。失败 batch 的补偿是例外，但只删除 artifact 清单证明为本次新建且未被引用的对象。
- 生产 API 关闭或撤销令牌可停止后续导入，不影响已创建草稿。`cleanup_retry` 必须保留到精确对象确认删除，不能以删除审计记录代替实际清理。
- 残余风险包括：MinIO 或网络在单媒体清理和后台重试均不可用时会暂时保留该失败媒体的 object、客户端网络超时导致用户重试、复杂 Markdown 扩展无法完全保持语义、外部图片可用性，以及 AI 生成内容的准确性。P1 用幂等键、逐媒体状态、缺失标记、持久化 artifact 清单、精确删除、有限退避重试和不自动发布降低风险。

## 需要用户确认的事项

1. 首期是否接受“Windows 命令行客户端 + 网站受认证 API”，而不做桌面 GUI？
2. 默认 `--source-root` 是否固定为 `F:\openclaw\workspace\kaogong`，还是每次由用户选择？
3. 初期图片应写入哪个 Wagtail collection，单篇最大图片数、单图/总上传体积各是多少？
4. 首期是否只接受 UTF-8 Markdown 和标准图片语法，并将 HTML 图片、Obsidian 语法列为不支持；远程图片统一由客户端下载后上传？
5. 认证是否采用可撤销的个人访问令牌？令牌的创建、轮换和撤销入口需由谁管理？
6. AI 元数据是否在 P1 之后再做？生产中是否允许将文章正文发送到已配置的外部 AI 服务？
7. 测试环境中是否授权创建少量合成 BlogPage 草稿和 Wagtail 图片用于端到端验收？
8. 已确认本地 MP4/MP3 首期分别进入现有 `video_block`/`audio_block`；仍需在实现前核对现有前台模板是否提供可用播放器，以及测试环境允许的 codec、时长和大小上限。
9. 已确认 Bilibili、YouTube、网易云、QQ、酷狗和咪咕链接进入现有 `embed_block`。还需确认是否接受第三方播放器的隐私、可用性和地区限制，以及是否允许后续增加其他流媒体 provider。
10. 本地非媒体相对链接（其他 Markdown、PDF 等）是首期阻止提交，还是允许保留为待修复链接？
11. 是否接受远程图片按博客网站接口返回的格式白名单执行，默认不承诺 SVG、TIFF、HEIC/HEIF、AVIF 等格式？
12. 已确认单个失败媒体只清理它自己的半上传 MinIO object，其他成功媒体必须保留并继续生成模块；是否同意在该单媒体清理失败时复用既有 maintenance Worker/Beat 精确重试，并在 `systemctl.md` 登记这一任务？

## 模型/推理强度建议

- 事实收集、样本结构统计、文档维护：`gpt-5.6-luna`，低/中推理。理由是范围明确、无需处理真实内容。
- Django/Wagtail API、客户端、图片处理和针对性测试：`gpt-5.6-terra`，中推理。理由是跨文件但局部的功能实现。
- 认证设计、幂等/事务补偿、生产数据写入、外部 AI 正文外发与生产发布复核：`gpt-5.6-sol`，高推理。升级条件是需要确定 token 权限、对象存储补偿、迁移、生产配置或发布/回滚。
- 验证门禁：模型选择不能替代权限测试、Markdown 安全测试、WSL2 测试、浏览器验收、备份、生产授权或回滚审查。

## 实施记录

- 2026-08-16，状态：方案完成，尚未获得实现授权。已核对 BlogPage 的 `markdown_block`/MongoDB 保存边界、Markdown 渲染器、后台图片上传入口、元数据建议能力、`requirements.txt`、`systemctl.md` 和示例 Markdown 的相对图片引用形式。未执行测试、迁移、服务重启、数据写入、Git 提交或生产操作。
- 2026-08-16，状态：方案补充完成，尚未获得实现授权。已核对 `audio_block`、`video_block`、`embed_block`、Bilibili/YouTube/网易云/QQ/酷狗/咪咕 finder 与 Markdown HTML 白名单边界；确认这些平台的独占段落链接映射到既有流媒体块，明确本地音视频上传、普通链接、播放器和远程嵌入的分期差异。未执行测试、迁移、服务重启、数据写入、Git 提交或生产操作。
- 2026-08-17，状态：方案补充完成，尚未获得实现授权。新增决策：标准 Markdown 远程图片由客户端下载后作为 multipart 上传，正文改写为站内图片 URL；服务端按 Wagtail 实际格式/大小限制作最终校验，拒绝伪装、超限和不支持格式，不由服务器按 URL 抓取互联网。
- 2026-08-17，状态：方案补充完成，尚未获得实现授权。新增核心结构：导入后的 BlogPage 使用有序拆分的 StreamField 块序列；普通文本为 `markdown_block`，图片/视频/音频/流媒体/Mermaid 分别为 `image_block`/`video_block`/`audio_block`/`embed_block`/`mermaid_chart`，不再保存完整单一 Markdown 文档。
- 2026-08-17，状态：方案澄清完成，尚未获得实现授权。Mermaid 围栏及代码只保存到 `mermaid_chart.code`，不得回填或复制到任何 `markdown_block`；已把这一约束加入块映射和结构测试。
- 2026-08-17，状态：方案补充完成，尚未获得实现授权。已确认 MinIO 不受 MySQL 事务回滚保护；新增导入 batch/artifact 持久化清单、逐媒体失败清理、部分成功草稿、缺失媒体标记、页面级失败的批次补偿，以及 MongoDB 新建内容精确补偿设计。未执行测试、迁移、服务重启、数据写入、对象删除、Git 提交或生产操作。
- 2026-08-17，状态：方案冲突收敛完成，尚未获得实现授权。统一为“单媒体失败不阻断整体”：本地/远程图片、音频和视频失败均在原位置生成独立纯文本 `markdown_block` 缺失标记，只清理失败 artifact 自身；远程图片由客户端在 `--allow-external-images` 明确确认后下载，客户端预检与服务端最终校验职责分离。补充 P1 UUIDv4 幂等键、服务端请求指纹、并发唯一约束，以及测试环境按 test run 和精确 artifact 清理草稿、MongoDB、Wagtail 媒体与 MinIO object 的协议。仅修改方案文档；未修改代码或 `systemctl.md`，未执行测试、迁移、服务重启、数据写入、对象删除、Git 提交、推送或生产操作。
- 2026-08-17，状态：已获得实现授权，T1 纯解析器完成。新增 `markdown_import_types.py`、`markdown_import_parser.py` 和 8 项解析测试；已验证普通 Markdown/代码围栏保真、Mermaid 仅进入 `mermaid_chart.code`、独占图片与引用式图片、本地单一 `src` 音视频、Bilibili 等流媒体候选按源顺序切块，表格、公式、行内链接和复杂 HTML 媒体保持 Markdown。WSL2 定向测试 8/8 通过，未连接或写入 MySQL、MongoDB、MinIO，未执行迁移、服务操作、Git 提交、推送或部署；`systemctl.md` 无需更新。
- 2026-08-17，状态：T2 客户端安全层完成。新增本地媒体路径边界和远程图片下载安全模块及 10 项测试；本地路径按真实 `source_root` 拒绝目录/符号链接逃逸和绝对/UNC/非文件 scheme，远程图片必须显式授权且仅允许 HTTPS，每跳拒绝非公网 DNS 答案并把连接固定到已校验 IP，TLS 仍校验原域名，响应超限、无效图片或其他失败均删除本次临时半文件。WSL2 定向测试 10/10、编译检查和差异检查通过；测试使用注入的 DNS/HTTP 响应，未访问互联网，未写数据库、MongoDB 或 MinIO，未执行迁移、服务操作、Git 提交、推送或部署；`systemctl.md` 无需更新。
- 2026-08-18，状态：T3-T4 完成。新增 `MarkdownImportBatch`/`MarkdownImportArtifact`、UUIDv4 幂等和请求指纹、Wagtail 图片/媒体最终表单校验、音视频深度探测失败关闭、逐媒体对象补偿与 cleanup retry 证据；未改变既有 StreamField key、Mongo 正文契约或生产数据。T1-T4 联合测试 59/59，T4 独立测试 31/31，`manage.py check`、迁移检查和差异检查通过；迁移未应用，未写 MySQL/MongoDB/MinIO，未执行服务、Git 或生产操作。
- 2026-08-18，状态：T5-T7 完成。按原顺序组装 `markdown_block`、媒体、embed 和 Mermaid 块，失败媒体保留独立缺失标记；创建未发布 BlogPage/revision 并支持页面级依赖阻断补偿；新增认证 limits/destinations/preview/import API、collection/权限/幂等校验和 Windows CLI。CLI 支持 YAML front matter 白名单、显式远程图片下载、超时重试、脱敏输出及临时目录清理；API 文档同步更新。T5-T7 定向测试 15/15 通过，未创建真实草稿或媒体。
- 2026-08-18，状态：T8 完成。cleanup 任务只接受 artifact UUID，Beat 每 60 秒按 `cleanup_retry`、下一次退避时间和最大尝试次数投递到现有 `maintenance` 队列；更新 `settings/database.py`、`systemctl.md`，不新增 Worker/unit。任务测试 5/5、导入相关联合测试 78/78 通过，迁移仍未应用，未重启服务或执行对象删除。
- 2026-08-18，状态：T9 部分完成、T10 完成（未发布）。只读核对测试环境配置，完成导入相关 78 项联合测试、Django check、迁移计划和 diff 检查；未获得真实测试写入授权，未创建/清理 `test_run_id` 草稿、Mongo revision、Wagtail 媒体或 MinIO object，也未进行 Playwright 后台验收。方案、任务包、API 文档和 `systemctl.md` 已记录实际文件、测试、服务影响、回滚点与残余风险；工作树保持未提交，不推送、不部署。模型实际使用按任务包执行：T3-T5/T8 的数据保护和一致性复核采用 sol 高推理角色，T6/T7/T9 常规实现/只读验证采用 terra 中推理，文档与重复检查采用 luna；未调用外部模型服务或发送源码、正文、凭据、生产日志。
- 2026-08-18，状态：T9 只读回归复核完成，真实写入与浏览器验收仍待单独授权。WSL2 `wagtailblog-test` 环境重新运行导入相关测试 76/76 通过，覆盖解析、路径/SSRF、批次幂等、媒体补偿、StreamField 组装、认证 API、Windows CLI 和 cleanup retry；启动输出确认测试端点为测试 MySQL `wagtailsoftblog_test`、MongoDB `wagtailblog_test`、Redis `192.168.20.2:6379`、MinIO `wagtail-test-bucket` 和测试 Elasticsearch。`python manage.py check` 通过；`makemigrations --check --dry-run` 返回 `No changes detected`；`migrate --plan` 仅列出未应用的 `blog.0025_markdown_import_batch_artifact`，未执行迁移；Python 编译检查和 `git diff --check` 通过。未创建或删除真实 BlogPage 草稿、revision、MongoDB 正文、Wagtail 媒体或 MinIO object，未启动 Playwright，未重启服务，未提交、推送或部署。已有 MySQL `wagtailcore.WorkflowState models.W036` 警告与本次迁移无关。T9 剩余验收仍包括：获授权后用唯一 `test_run_id` 做成功/部分成功/幂等重放/页面失败/cleanup retry 写入测试，按精确 ID/object name 逆序清理并核验残留为零，以及桌面/移动后台和前台浏览器验收。模型实际使用：本轮只读回归按 `gpt-5.6-terra + 中推理` 角色执行，文档更新按 `gpt-5.6-luna` 角色执行，未调用外部模型服务。
- 2026-08-18，状态：T9 集成写入、精确清理与后台浏览器验收完成，前台公开渲染保留为已知限制。测试环境唯一 `test_run_id=73a60b64-16d8-4058-a199-08e0f26acbbc` 创建成功、部分成功、幂等重放、页面失败和 cleanup retry 场景；批次/页面/媒体/MinIO/Mongo 精确清单已写入任务包，清理后逐项核验为零。Windows 主机 Playwright 后台桌面/移动视口确认导入块顺序和移动端无横向溢出；站内网络请求成功，唯一控制台错误为外部 Gravatar 被网络重置。测试页保持未发布，公开 URL 404，编辑预览因 Mongo 正文未回填 SQL 表单而不可用，因此未把前台渲染标记为通过，也未为测试强制发布页面。测试服务器已停止；最终导入相关回归 77/77、Django check、迁移检查、无待执行迁移计划和 `git diff --check` 通过。未触碰生产、未提交、推送或部署。

### 版本化方案：大批量导入（超过 1000 个媒体）

#### 背景与现状证据

当前客户端把每个媒体作为一个 multipart 文件字段，在一次 HTTP 请求内提交。Django 默认 `DATA_UPLOAD_MAX_NUMBER_FILES=100`；测试环境临时提高到 256 后可覆盖 `第七章.md` 的 162 个媒体，但这只是容量缓冲，不解决长请求、内存、反向代理、uWSGI 超时、网络中断、重复上传和单次请求重试成本。超过 1000 个媒体时，不应继续把文件数上限简单调大。

#### 目标

- 支持至少 1000 个、目标 10000 个媒体引用的单次导入任务。
- 最终仍可组装成一个未发布 BlogPage，保持原 Markdown 顺序、表格内图片位置和 StreamField 拆分契约。
- 上传可暂停、续传、重试；单个媒体失败不影响其他媒体，页面组装失败可精确补偿。
- 幂等重放不重复创建页面、Wagtail 媒体或 MinIO 对象。
- 所有上限由服务端返回：媒体数量、总字节数、单文件大小、并发数、分片大小、会话 TTL 和任务超时。

#### 非目标

- 不把大批量导入改成自动发布。
- 不跨不同导入会话按内容哈希合并媒体。
- 不允许客户端直接写任意 MinIO 路径；对象名必须由服务端按导入会话和 artifact 生成。
- 不通过提高 multipart 上限来承载无限文件。

#### 推荐企业架构

1. `POST /imports/sessions/` 只提交 JSON manifest，不上传文件。manifest 包含 Markdown 内容摘要、块顺序、每个 artifact 的稳定来源、大小、SHA-256、媒体类型、引用位置、目标父页和幂等键。服务端校验权限、数量、总字节数和正文指纹，创建 `import_session`。
2. 服务端按 artifact 返回短期上传凭证或受控上传地址。优先使用 MinIO/S3 multipart upload；客户端按 8-16 MiB 分片、4-8 路并发上传，单个分片可重试，完成后提交分片摘要。凭证只绑定指定 session/artifact/object，不允许改变 bucket 或 object name。
3. `POST /imports/sessions/{id}/artifacts/{id}/complete/` 完成单个 artifact。服务端用 storage/head 校验对象存在、大小、SHA-256 和允许的媒体格式，再进入 Wagtail 图片/音频/视频表单校验。校验结果写入 artifact 状态；失败只标记该媒体并生成缺失标记。
4. Celery `maintenance` 队列异步处理媒体验证和页面组装。状态建议为 `created -> uploading -> uploaded -> validating -> ready/failed_missing -> assembling -> success/partial_success/failed/cleanup_retry`。每个任务必须可重入，按 session、artifact 和 idempotency key 加锁。
5. 所有 artifact ready 或失败关闭后，组装任务按 manifest 中的 position 和 occurrence_id 生成 StreamField。表格内图片继续留在对应 `markdown_block`；表格外图片继续生成独立 `image_block`。只在最终组装阶段创建一个未发布 BlogPage/revision，并写入 Mongo 草稿指针。
6. 页面失败时只按数据库中的精确 artifact object name、Wagtail media ID 和 Mongo pointer 补偿删除；上传中断的临时对象按 session TTL 清理。清理任务不使用全桶删除，重试必须保留审计状态。

#### 过渡方案

在完整分片上传前，可先实现“逻辑分批但单页组装”：客户端每 50-100 个 artifact 提交一个上传批次，服务端只保存 artifact 和对象，不创建页面；最后调用 `finalize` 组装一个 BlogPage。这样可以绕开单次 multipart 文件数限制，但仍保留未来切换到 MinIO multipart 的 API 形状。单纯把 `DATA_UPLOAD_MAX_NUMBER_FILES` 调到 2048/10000 不作为交付方案。

#### 限制与门禁

建议初始服务端限制为：单会话 10000 个 artifact、总大小 20 GiB、单文件沿用 Wagtail 实际限制、单分片 8 MiB、客户端并发 4、会话 TTL 24 小时、单会话组装超时 30 分钟；具体值必须根据 MinIO、Nginx、uWSGI、Celery worker 和磁盘/网络实测调整。达到任一限制时在预检阶段拒绝，不进入上传。

#### UI/客户端流程

连接 -> 创建导入会话 -> 扫描和预检 -> 显示总媒体/总大小/预计分片数 -> 上传进度（可暂停） -> 展示成功/失败媒体 -> 仅重试失败 artifact -> 服务端组装 -> 显示 batch/page/revision 和后台地址。客户端关闭后可凭 session token 恢复，不保存 JWT 到磁盘。

#### 数据与服务影响

需要新增导入会话、分片上传记录和 artifact 状态字段，可能新增迁移；MinIO 需要短期 staging object 生命周期；Celery maintenance Worker/Beat 需要处理验证、组装和过期会话清理。必须同步更新 `systemctl.md`、限额 API、监控指标和回滚说明。生产实施前必须单独确认数据库迁移、对象生命周期规则、队列容量和备份。

#### 验收与回滚

1001、5000 和限制上限的专项压力测试不再作为当前交付门槛；当前以真实常用上限约 160 个媒体的会话导入、单媒体失败、幂等重放、组装和精确清理为验收基线。未来实际出现超过当前样本数量级的需求时，再恢复大容量压测并据 MinIO、Nginx/uWSGI、Worker 与网络实测调整限额。回滚时停止新会话入口，保留已完成 artifact 的审计和清理任务；不删除既有 BlogPage、Mongo 正文或共享 Wagtail 媒体。

#### 模型/推理强度建议

会话/分片协议、幂等锁、对象生命周期和补偿属于跨服务一致性设计，使用 `gpt-5.6-sol + 高推理` 做规格复审；Django API、Celery 任务、Windows 客户端和测试按 `gpt-5.6-terra + 中推理` 实现；文档、限额核对和重复检查按 `gpt-5.6-luna + 中推理`。涉及生产迁移、MinIO 生命周期、队列扩容或回滚失败时必须升级 sol 门禁。当前仅完成方案，未开始代码实现。

#### T19 首阶段实施记录（2026-08-19）

- 已实现会话创建、逐 artifact 受认证上传、SHA-256/大小/媒体表单校验、异步最终组装、会话过期精确 cleanup、限额返回和 Windows GUI 数量进度；新增迁移 `0026_markdown_import_session`，复用既有 `maintenance` Worker 与 Beat，不新增 unit。
- 该首阶段不向客户端开放 MinIO/S3 预签名 multipart，也尚未提供跨进程断点恢复或 4 路并发。1001/5000 真实写入压测已按用户当前使用上限取消，不作为交付门槛；不得将未做实测的存储直写或超大容量声明为已上线能力。
- 已完成 WSL2 静态/单元回归与迁移一致性检查；未应用迁移、未写测试或生产数据、未重启服务、未提交、推送或部署。生产实施仍需单独确认迁移备份、MinIO 生命周期、Worker/Beat 服务状态和容量。
- 2026-08-18，状态：T12 后续缺陷修复与真实样本复测完成。API 异常响应统一调用 DRF `finalize_response`；客户端远程图片下载失败按单媒体生成无文件 artifact，服务端返回 `client_download_failed` 缺失标记，4xx 错误不再压缩为 `request_failed`。测试用户导入 `测试页面.md` 时，本地 HTML 图片、MP4、MP3 与 HTTPS 图片均进入对应块；页面 585/批次 `cda7840f-7ee9-4ac0-ab74-af5d661fa3f2` 状态 `success`、缺失 0，随后按精确 ID/object name 清理并验证零残留。未触碰页面 583 或生产。
- 2026-08-18，状态：Windows CLI 路径与 URL 编码路径修正。`tools/markdown_import/client.py` 现将 `wagtailblog3/apps` 加入模块搜索路径；本地媒体路径先 URL 解码再做 source-root 边界校验，支持 `第八章.md` 中编码后的中文 `.assets` 目录，同时拒绝解码后路径逃逸。Windows CLI 相关测试 16/16 通过，实测 `第八章.md` 预检识别 59 个块和 29 个图片且无错误。`测试页面.md` 实际为方案说明/示例文本，仍只有 1 个 Markdown 块、0 个媒体块。未创建网站数据或执行生产操作。

### 方案增量：表格内图片导入边界（2026-08-18，已由 T14-T18 实施）

- **明确规则**：HTML 复杂表格和 Markdown 普通表格内部的图片，必须留在原有 `markdown_block` 中，不能拆成独立 `image_block`；表格外的独占图片继续按现有规则生成独立 `image_block`。该规则优先于“所有图片统一转为图片块”的其他解释。
- **样本依据**：`workspace/kaogong/测试页面2.md` 含 1 个 HTML 复杂表格、1 个 Markdown 普通表格和 5 个图片引用（本地 3 个、HTTPS 远程 2 个）；T14 解析器已将这些引用识别为表格内图片，并保留在 `markdown_block` 语义范围内。
- **识别范围**：使用 Markdown token 解析识别 Markdown 表格中的 `![](...)`/`![说明](...)`，使用受限 HTML 解析器识别表格内 `<img src="...">`；跳过 fenced code、inline code、普通超链接、脚本和样式节点。不把任意 HTML 属性或普通文本中的 URL 当成图片。
- **上传与重写**：客户端对本地路径执行 `source_root` 边界校验；远程图片仅在用户显式确认后由客户端按 HTTPS 安全策略下载。所有图片复用 Wagtail 图片表单和现有 collection/格式/大小校验。上传成功后，服务端把表格内原引用改写为 Vditor/Wagtail 可识别的 `<embed embedtype="image" id="..." format="fullwidth_web" src="..." alt="..." />`，保留表格行列结构和图片说明；`id` 是正文的权威引用，`src` 只作为编辑器预览提示，前台仍按当前 rendition 重新展开。
- **样式边界**：不保留 `style="zoom:..."`、任意 CSS 或不受控 HTML 样式；统一使用 `fullwidth_web` rendition、响应式最大宽度和现有表格横向滚动容器。若未来需要精确尺寸，另行设计受限的宽高字段，不把原始 CSS 直接写回正文。
- **失败与补偿**：表格内单个图片失败时，在原单元格位置插入纯文本缺失标记，保持行列数量不变；其他图片继续上传并保留。仅页面/revision/Mongo 最终组装失败时，才按 artifact 精确清理本批未引用成功图片。远程下载失败、服务端格式/尺寸拒绝和 MinIO 写入失败均使用同一单媒体失败协议。
- **去重**：同一导入批次内，同一规范化路径或 URL 只创建一个图片 artifact，多个单元格引用同一图片 ID；不跨文件或批次复用，不因不同 URL 的内容哈希相同而合并。

### 方案增量：表格图片重写契约（2026-08-18，开发前最终版）

为避免“图片进入图片块”与“图片留在表格 Markdown”两种解释再次冲突，首期按下面的优先级执行：

1. **表格上下文优先**：HTML `<table>` 后代节点中的 `<img>`，以及 Markdown 表格单元格中的 `![](...)`/`![说明](...)` 和 HTML `<img>`，全部记为 `inline_image`。无论图片是本地相对路径还是用户确认后的 HTTPS 远程图片，都不得生成独立 `image_block`。
2. **表格外规则不变**：表格外、独占一个块级位置的 Markdown 图片或独立 HTML `<img>`，继续按现有解析器生成独立 `image_block`。普通段落中与文字混排的图片暂不改变既有解析边界，不因本任务强行拆块。
3. **结构保护**：重写只替换图片引用节点，不重排 `<tr>`、`<td>`、`<th>` 或 Markdown 表格行；`rowspan`、`colspan`、单元格文字、公式、链接和表格顺序必须保持不变。解析器无法安全定位节点时，该引用按单媒体失败处理，不得猜测性地改写整张表。

#### 统一的内联图片生命周期

客户端预检为每个引用生成 `inline_image` manifest 项，至少包含脱敏的 `source_ref`、`source_kind`（local/remote）、`table_locator`（表格序号、行/单元格序号或 HTML 节点路径）、`occurrence_id` 和规范化来源键。远程图片只有在用户显式确认 `--allow-external-images` 后才由客户端下载；服务端永远不根据 URL 主动抓取。

服务端先完成每个媒体的最终格式、内容、大小、collection 和 storage 校验，再创建该图片 artifact。只有上传成功并得到 Wagtail 图片 ID 后，才在对应 `markdown_block` 的原始片段上执行基于解析节点的重写。重写结果统一为项目现有 Vditor/Wagtail 图片嵌入：

```html
<embed embedtype="image" id="图片ID" format="fullwidth_web" src="图片预览地址" alt="图片说明" />
```

`id` 是正文引用的权威值，`src` 只用于编辑器预览提示；前台仍通过现有 `MarkdownRenderer` 按图片 ID 和 `fullwidth_web` rendition 展开。不得把客户端提交的图片 ID 当作可信输入，也不得直接拼接 storage URL。

#### 样式与属性边界

- 删除图片节点上的 `style` 属性，尤其是 `zoom:30%` 等编辑器私有样式；不把任意 CSS、事件属性或未知属性写回正文。
- 保留安全的 `alt`，必要时保留纯文本 `title`；不新增宽高字段。图片显示由 `fullwidth_web` rendition、现有响应式最大宽度和表格横向滚动容器负责。
- HTML 表格的结构属性（包括 `rowspan`、`colspan`）不因图片重写被删除。经过现有 Markdown/HTML 安全清洗后仍必须保持可渲染结构。

#### 失败、缺失与重复引用

- 单个 `inline_image` 下载、解码、服务端校验、storage 写入或重写失败，只影响该引用：在原单元格位置写入纯文本 `[导入缺失：图片 原始引用：{安全化引用} 原因：{错误码}]`，不使用 `>`，不生成空的 `image_block`，其余图片和表格结构继续保留。
- 同一批次内，规范化后的本地路径或远程 URL 只上传一次；同一图片在多个单元格重复引用时复用同一 Wagtail 图片 ID。规范化规则为：本地路径 URL 解码、统一分隔符并在 `source_root` 内解析；远程 URL 仅统一 scheme/host 大小写、移除 fragment 和默认端口，保留 query 原样，因为 CDN query 可能改变图片内容。
- 不同来源即使内容 SHA-256 相同，首期也不跨来源合并；不跨文件、批次或用户复用图片。页面最终创建失败时，只清理本批次已成功上传但没有被任何正文引用的 artifact；单媒体失败只清理该 artifact 自身。

#### `测试页面2.md` 的验收基线

预检必须报告 5 个 `inline_image`（HTML 复杂表格 2 个、Markdown 普通表格 3 个，其中本地 3 个、HTTPS 远程 2 个）。成功导入后 `BlogPage.body` 中对应内容仍是一个或相邻的 `markdown_block`，不得出现由这 5 张图产生的 `image_block`；Vditor 后台和前台均能按图片 ID 显示，`rowspan`/`colspan`、表格公式和链接保持不变。故意让其中一张失败时，只有该单元格出现缺失标记，其余 4 张仍显示；幂等重放不得重复创建页面或图片。

本节保留开发前的规则与边界；T14-T18 已按本契约完成实现和测试，真实测试数据已按 `test_run_id` 精确清理。
- **接口与实现边界**：manifest 增加 `inline_image` artifact/引用位置，服务端在组装 `markdown_block` 前执行基于语法节点的安全重写；现有独占图片、音视频、流媒体、Mermaid 的 StreamField 结构不变。服务端不根据客户端路径或 URL 主动抓取互联网，也不信任客户端直接提交的 Wagtail 图片 ID。
- **验收标准**：`测试页面2.md` 预检显示表格内图片 5 个（本地 3、远程 2），导入后仍为表格 Markdown 块；表格外独占图片仍为 `image_block`；后台 Vditor 和前台均显示图片；`rowspan`/`colspan` 不变；远程图片进入网站图片库；单媒体失败显示单元格缺失标记；幂等重放不重复创建图片；页面失败后按精确 ID/object name 清理为零。
- **模型/推理强度建议**：样本解析和文档核对使用 `gpt-5.6-luna + 中推理`；解析器、语法重写、API/客户端实现使用 `gpt-5.6-terra + 中推理`；涉及 HTML 注入、Wagtail image embed、MinIO 补偿、幂等和跨页面引用复核时升级 `gpt-5.6-sol + 高推理`。升级门禁为跨批次媒体复用、不可逆数据迁移或真实生产图片写入。

- 2026-08-20，状态：导入客户端 AI 简介、标签与提示词模板选择已实现。新增导入专用模板列表和建议接口，复用现有 `BlogMetadataPromptTemplate` 与严格 JSON 契约；客户端按文件保存 AI 授权、模板、简介和标签，结果不覆盖标题。客户端先从 Markdown 解析计划抽取纯文本并移除代码、媒体块、URL 和本地路径，服务端再次拒绝含 URL/路径的篡改请求。测试未调用真实外部 AI，未创建页面、批次或媒体，未修改 MongoDB/MinIO 或生产服务。
