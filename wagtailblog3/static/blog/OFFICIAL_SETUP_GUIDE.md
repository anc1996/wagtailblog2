# Highlight.js 官方资源使用指南

## 🎯 推荐方案：CDN优先

这个项目采用 **CDN优先** 的策略，本地文件仅作为备用。

### 📋 当前配置

**模板文件**: `blog_page.html` 已配置为使用CDN资源

**支持功能**:
- ✅ 192种编程语言高亮
- ✅ 官方行号插件 (highlightjs-line-numbers.js)
- ✅ 官方复制按钮插件 (highlightjs-copy)
- ✅ 多种官方主题选择
- ✅ 响应式设计
- ✅ 无障碍访问支持

### 🌐 使用的CDN资源

```html
<!-- 核心库 -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js"></script>

<!-- 行号插件 -->
<script src="https://cdn.jsdelivr.net/npm/highlightjs-line-numbers.js@2.9.0/dist/highlightjs-line-numbers.min.js"></script>

<!-- 复制按钮插件 -->
<script src="https://cdn.jsdelivr.net/npm/highlightjs-copy@1.0.3/dist/highlightjs-copy.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlightjs-copy@1.0.3/dist/highlightjs-copy.min.css">

<!-- 主题样式 -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/vs2015.min.css">
```

### 🎨 可用主题列表

您可以在模板中轻松切换主题，只需替换CSS链接：

**深色主题**:
- `vs2015.min.css` - Visual Studio 2015 深色主题 (推荐)
- `atom-one-dark.min.css` - Atom One Dark 主题  
- `monokai.min.css` - 经典 Monokai 主题

**浅色主题**:
- `github.min.css` - GitHub 风格主题
- `atom-one-light.min.css` - Atom One Light 主题
- `default.min.css` - 默认浅色主题

### 🔧 添加更多语言

需要支持更多语言？只需在模板中添加对应的语言包：

```html
<!-- 添加更多语言支持 -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/languages/kotlin.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/languages/scala.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/languages/ruby.min.js"></script>
```

**完整语言列表**: https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/languages/

### 📱 备用本地文件

本地下载的文件位于 `static/blog/` 目录下，仅在CDN不可用时使用。

**下载统计**:
- 总文件数: 29
- 成功下载: 3
- 跳过文件: 26
- 失败文件: 0

### 🚀 性能优势

使用CDN的好处：
1. **更快加载**: 全球CDN节点分发
2. **减少维护**: 无需管理本地文件更新
3. **自动更新**: 安全补丁和功能更新
4. **减少带宽**: 节省服务器带宽

### 🔄 切换到本地文件

如果需要使用本地文件，请修改模板中的资源链接：

```html
<!-- 从CDN -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js"></script>

<!-- 改为本地 -->
<script src="{% static 'blog/js/highlight/highlight.min.js' %}"></script>
```

### 📞 技术支持

- **Highlight.js 官网**: https://highlightjs.org/
- **行号插件**: https://github.com/wcoder/highlightjs-line-numbers.js
- **复制插件**: https://github.com/arronhunt/highlightjs-copy
- **主题预览**: https://highlightjs.org/static/demo/

---
*最后更新: 2025-06-10 10:56:27*
