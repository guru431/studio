import { tr } from './i18n.js';
/* Режимы CLI: сервер и мост проверяют значения независимо от страницы. */
export const ENGINE_NAMES = { claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode' };

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
};

export function accessLabel(engine, value) {
  return ACCESS[engine]?.[value] || (value ? String(value) : tr("Не получено"));
}

export function effortLabel(value) {
  return value === 'auto' ? 'auto' : value || tr("Не получено");
}
