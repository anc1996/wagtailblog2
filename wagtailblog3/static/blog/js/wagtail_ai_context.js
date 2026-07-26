/*
 * Reliable Wagtail AI context for BlogPage StreamField content.
 *
 * The official wagtail-ai provider reads w-preview. That preview can still be
 * stale while a StreamField editor is being hydrated or edited. BlogPage's
 * current, unsaved value is instead held by Wagtail in #body's
 * data-w-block-arguments-value. This provider turns that source into clean
 * text / HTML before the AI request is sent.
 */
(() => {
  'use strict';

  const BODY_SELECTOR = '#body[data-w-block-arguments-value]';
  const MAX_CONTEXT_CHARS = 24000;

  const normaliseText = (value) => String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const escapeHtml = (value) => String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');

  const htmlToText = (value) => {
    const node = document.createElement('div');
    node.innerHTML = String(value ?? '');
    return normaliseText(node.textContent);
  };

  const markdownToText = (value) => normaliseText(String(value ?? '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(^|\n)\s{0,3}#{1,6}\s*/g, '$1')
    .replace(/[>*_`~]/g, ' '));

  const richTextToText = (value) => {
    if (typeof value !== 'string') return normaliseText(value);
    try {
      const data = JSON.parse(value);
      if (Array.isArray(data.blocks)) {
        return normaliseText(data.blocks.map((block) => block.text || '').join('\n'));
      }
    } catch (_) {
      // Older content may already be Wagtail rich-text HTML.
    }
    return htmlToText(value);
  };

  const textFromBlock = (block) => {
    if (!block || typeof block !== 'object') return '';
    const value = block.value;
    switch (block.type) {
      case 'rich_text':
        return richTextToText(value);
      case 'markdown_block':
        return markdownToText(value);
      case 'code_block': {
        const code = typeof value === 'object' ? value.code : value;
        const language = typeof value === 'object' ? value.language : '';
        const summary = normaliseText(code).slice(0, 1200);
        return summary ? `[代码${language ? `：${language}` : ''}]\n${summary}` : '';
      }
      case 'mermaid_chart': {
        const code = typeof value === 'object' ? value.code : value;
        return normaliseText(code) ? `[流程图/图表]\n${normaliseText(code).slice(0, 800)}` : '';
      }
      case 'table_block': {
        const data = value && typeof value === 'object' ? value.data : null;
        if (!Array.isArray(data)) return '';
        return normaliseText(data.slice(0, 20).map((row) =>
          Array.isArray(row) ? row.map(htmlToText).filter(Boolean).join(' | ') : ''
        ).filter(Boolean).join('\n'));
      }
      case 'embed_block':
        return normaliseText(value && typeof value === 'object' ? value.title : '');
      case 'raw_html':
        return htmlToText(value);
      // IDs of chooser blocks contain no useful semantic context by themselves.
      case 'image_block':
      case 'video_block':
      case 'audio_block':
      case 'document_block':
      default:
        return '';
    }
  };

  const currentStreamBlocks = () => {
    const element = document.querySelector(BODY_SELECTOR);
    if (!element) return [];
    try {
      const args = JSON.parse(element.dataset.wBlockArgumentsValue || '[]');
      // StreamBlock passes [blocks, initialError]. Its first item is the live,
      // ordered list of {type, value, id} objects.
      return Array.isArray(args) && Array.isArray(args[0]) ? args[0] : [];
    } catch (error) {
      console.warn('Unable to read current StreamField value for Wagtail AI.', error);
      return [];
    }
  };

  const currentContentText = () => {
    const text = normaliseText(currentStreamBlocks().map(textFromBlock).filter(Boolean).join('\n\n'));
    return text.slice(0, MAX_CONTEXT_CHARS);
  };

  const currentContentHtml = () => currentStreamBlocks()
    .map((block) => {
      const text = textFromBlock(block);
      return text ? `<section data-streamfield-block="${escapeHtml(block.type || '')}"><p>${escapeHtml(text).replace(/\n/g, '<br>')}</p></section>` : '';
    })
    .filter(Boolean)
    .join('\n')
    .slice(0, MAX_CONTEXT_CHARS * 2);

  const install = () => {
    const provider = window.wagtailAI?.ContextProvider;
    if (!provider || !window.wagtail?.app) return false;
    provider.register('content_text', currentContentText);
    provider.register('content_html', currentContentHtml);

    window.wagtailAI.BlogPageContext = { currentStreamBlocks, currentContentText, currentContentHtml };
    return true;
  };

  if (!install()) {
    document.addEventListener('DOMContentLoaded', install, { once: true });
  }
})();
