import { tr } from './i18n.js';
/* Режимы CLI: сервер и мост проверяют значения независимо от страницы. */
export const ENGINE_NAMES = { claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode', deepseek: 'DeepSeek Harness' };
/** Уточнение во время хода ждёт конца ответа: «Уточнить сейчас» нет. */
export const QUEUE_ONLY = new Set(['opencode', 'deepseek']);
/** Лимиты (у DeepSeek Harness — баланс) зависят от провайдера выбранной модели: панель спрашивает с ?model=. */
export const USAGE_BY_MODEL = new Set(['opencode', 'deepseek']);

export function engineOrder(choices = {}) {
  const engines = Object.keys(ENGINE_NAMES);
  return [...engines].sort((a, b) => (choices[a]?.priority ?? engines.indexOf(a) + 1)
    - (choices[b]?.priority ?? engines.indexOf(b) + 1));
}

export const ACCESS = {
  claude: { default: tr("Обычные права"), auto: tr("Автоматически"), acceptEdits: tr("Правка файлов"),
    plan: tr("Только план"), bypassPermissions: tr("Полный доступ"), dontAsk: tr("Без запросов") },
  codex: { 'read-only': tr("Только чтение"), 'workspace-write': tr("Проект"), 'danger-full-access': tr("Полный доступ") },
  opencode: { default: tr("Правила OpenCode"), auto: tr("Автоматически") },
  deepseek: { default: tr("По настройкам DeepSeek Harness"), 'read-only': tr("Только чтение"),
    'workspace-write': tr("Правка в папке проекта"), 'danger-full-access': tr("Полный доступ") },
};

export function accessLabel(engine, value) {
  return ACCESS[engine]?.[value] || (value ? String(value) : tr("Не получено"));
}

export function effortLabel(value) {
  return value === 'auto' ? 'auto' : value || tr("Не получено");
}
