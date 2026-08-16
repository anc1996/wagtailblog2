/* 根据当前编辑器内存中的正文生成预览，不执行保存或发布。 */
(() => {
  'use strict';

  const initialise = () => {
    const panel = document.querySelector('#blog-ai-metadata');
    const script = document.querySelector('script[data-blog-ai-metadata]');
    if (!panel || !script) return;

  const endpoint = script.dataset.url;
  const templateEndpoint = script.dataset.templateUrl;
  const status = panel.querySelector('[data-status]');
  const preview = panel.querySelector('[data-preview]');
  const generateButton = panel.querySelector('[data-action="generate"]');
  const templateSelect = panel.querySelector('[data-template-select]');
  const setStatus = (message, isError = false) => {
    status.textContent = message;
    status.classList.toggle('error-message', isError);
  };
  const csrfToken = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value || window.wagtailConfig?.CSRF_TOKEN || '';

  const loadTemplates = async () => {
    try {
      const response = await fetch(templateEndpoint, { credentials: 'same-origin' });
      const payload = await response.json();
      if (!response.ok || !Array.isArray(payload.templates)) throw new Error('无法读取可用提示词列表。');
      templateSelect.replaceChildren();
      if (!payload.templates.length) {
        templateSelect.add(new Option('暂无可用提示词，请管理员先创建并启用模板', ''));
        templateSelect.disabled = true;
        generateButton.disabled = true;
        setStatus('暂无可用提示词，请管理员先创建并启用模板。', true);
        return;
      }
      templateSelect.add(new Option('请选择提示词模板', ''));
      payload.templates.forEach((template) => {
        const label = `${template.name}（v${template.version}）`;
        templateSelect.add(new Option(label, String(template.id)));
      });
      templateSelect.disabled = false;
      generateButton.disabled = true;
      templateSelect.addEventListener('change', () => {
        generateButton.disabled = !templateSelect.value;
        if (templateSelect.value) setStatus('已选择提示词模板，可以生成建议。');
      });
    } catch (error) {
      templateSelect.replaceChildren(new Option('提示词列表加载失败', ''));
      templateSelect.disabled = true;
      generateButton.disabled = true;
      setStatus(error.message || '无法读取提示词列表。', true);
    }
  };

  const applyValue = (selector, value) => {
    const field = document.querySelector(selector);
    if (!field) throw new Error('找不到要写入的编辑字段。');
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const applyIntro = (value) => {
    const field = document.querySelector('#id_intro');
    const editor = field?.draftailEditor;
    const draftJS = window.DraftJS;
    if (!field || !editor || !draftJS?.ContentState || !draftJS?.EditorState) {
      throw new Error('简介编辑器尚未准备完成，请刷新页面后重试。');
    }
    // Draftail 是受控编辑器；必须经 onChange 更新内部状态，不能只改隐藏字段。
    const content = draftJS.ContentState.createFromText(value);
    editor.onChange(draftJS.EditorState.createWithContent(content));
  };

  const applyTags = (tags) => {
    const field = document.querySelector('input[name="tags"]');
    if (!field) throw new Error('找不到标签编辑字段。');
    const tagIt = window.jQuery && window.jQuery(field).data('ui-tagit');
    if (tagIt) {
      window.jQuery(field).tagit('removeAll');
      tags.forEach((tag) => window.jQuery(field).tagit('createTag', tag));
      return;
    }
    // Wagtail 标签组件尚未初始化时，保留值并派发事件，供组件在挂载后读取。
    applyValue('input[name="tags"]', tags.join(','));
  };

  const applyAll = (suggestion) => {
    applyValue('#id_title', suggestion.title);
    applyIntro(suggestion.intro);
    applyTags(suggestion.tags);
  };

  const commandButton = (label, callback) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button button-secondary button-small';
    button.textContent = label;
    button.addEventListener('click', () => {
      try {
        callback();
        setStatus('建议已写入表单，仍需使用页面保存或发布操作确认。');
      } catch (error) {
        setStatus(error.message || '无法写入表单字段。', true);
      }
    });
    return button;
  };

  const renderPreview = (suggestion) => {
    preview.replaceChildren();
    const list = document.createElement('dl');
    [['标题', suggestion.title], ['简介', suggestion.intro], ['标签', suggestion.tags.join('、')]].forEach(([term, description]) => {
      const dt = document.createElement('dt');
      const dd = document.createElement('dd');
      dt.textContent = term;
      dd.textContent = description;
      list.append(dt, dd);
    });
    const actions = document.createElement('p');
    actions.append(
      commandButton('应用标题', () => applyValue('#id_title', suggestion.title)),
      document.createTextNode(' '),
      commandButton('应用简介', () => applyIntro(suggestion.intro)),
      document.createTextNode(' '),
      commandButton('应用标签', () => applyTags(suggestion.tags)),
      document.createTextNode(' '),
      commandButton('全部应用', () => applyAll(suggestion)),
    );
    preview.append(list, actions);
    preview.hidden = false;
  };

    generateButton.addEventListener('click', async () => {
    if (!templateSelect.value) {
      setStatus('请先选择一个启用的提示词模板。', true);
      return;
    }
    const body = window.blogEditorContext?.currentStreamBlocks?.();
    if (!Array.isArray(body) || !body.length) {
      setStatus('未读取到当前正文。请先添加包含文本的正文块后重试。', true);
      return;
    }
    generateButton.disabled = true;
    panel.setAttribute('aria-busy', 'true');
    setStatus('正在生成建议，请稍候。');
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({
          body,
          language: document.documentElement.lang || 'zh-hans',
          template_id: Number(templateSelect.value),
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.suggestion) throw new Error(payload.error?.message || '生成服务暂时不可用，请稍后重试。');
      renderPreview(payload.suggestion);
      setStatus('已生成建议。请检查内容后选择要应用的字段。');
    } catch (error) {
      setStatus(error.message || '生成请求失败，请检查测试环境配置后重试。', true);
    } finally {
      generateButton.disabled = !templateSelect.value;
      panel.removeAttribute('aria-busy');
    }
    });
    loadTemplates();
  };

  // Wagtail 在编辑器脚本后异步挂载面板，必须等待面板实际存在后再绑定事件。
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialise, { once: true });
  } else {
    initialise();
  }
})();
