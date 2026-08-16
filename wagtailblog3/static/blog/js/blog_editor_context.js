/* 读取 BlogPage StreamField 当前编辑态，供元数据建议功能使用。 */
(() => {
  'use strict';

  const LEGACY_BODY_SELECTOR = '#body[data-w-block-arguments-value]';
  const STREAMFIELD_ROOT_SELECTOR = '#body-root';

  const legacyStreamBlocks = () => {
    const element = document.querySelector(LEGACY_BODY_SELECTOR);
    if (!element) return null;
    try {
      const args = JSON.parse(element.dataset.wBlockArgumentsValue || '[]');
      // 当前编辑态保存在有序块列表中，读取时不修改表单或页面内容。
      return Array.isArray(args) && Array.isArray(args[0]) ? args[0] : [];
    } catch (error) {
      console.warn('无法读取当前 StreamField 编辑态。', error);
      return null;
    }
  };

  const streamfieldFormBlocks = () => {
    const root = document.querySelector(STREAMFIELD_ROOT_SELECTOR);
    const count = Number.parseInt(root?.querySelector('[name="body-count"]')?.value || '0', 10);
    if (!root || !Number.isInteger(count) || count < 1) return [];

    const blocks = [];
    for (let index = 0; index < count; index += 1) {
      const prefix = `body-${index}`;
      const type = root.querySelector(`[name="${prefix}-type"]`)?.value;
      const deleted = root.querySelector(`[name="${prefix}-deleted"]`)?.value;
      if (!type || deleted) continue;

      const id = root.querySelector(`[name="${prefix}-id"]`)?.value;
      const directValue = root.querySelector(`[name="${prefix}-value"]`);
      let value = directValue ? directValue.value : null;
      if (!directValue) {
        const fields = Array.from(root.querySelectorAll(`[name^="${prefix}-value-"]`));
        if (!fields.length) continue;
        value = Object.fromEntries(fields.map((field) => [
          field.name.slice(`${prefix}-value-`.length),
          field.value,
        ]));
      }
      blocks.push({ id, type, value });
    }
    return blocks;
  };

  const currentStreamBlocks = () => legacyStreamBlocks() || streamfieldFormBlocks();

  window.blogEditorContext = Object.freeze({ currentStreamBlocks });
})();
