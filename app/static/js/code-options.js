/* Режимы CLI: сервер и мост проверяют значения независимо от страницы. */
export const ENGINE_NAMES = { claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode' };

export function engineOrder(choices = {}) {
  const engines = Object.keys(ENGINE_NAMES);
  return [...engines].sort((a, b) => (choices[a]?.priority ?? engines.indexOf(a) + 1)
    - (choices[b]?.priority ?? engines.indexOf(b) + 1));
}

export const ACCESS = {
  claude: { default: 'Обычные права', auto: 'Автоматически', acceptEdits: 'Правка файлов',
    plan: 'Только план', bypassPermissions: 'Полный доступ', dontAsk: 'Без запросов' },
  codex: { 'read-only': 'Только чтение', 'workspace-write': 'Проект', 'danger-full-access': 'Полный доступ' },
  opencode: { default: 'Правила OpenCode', auto: 'Автоматически' },
};

export function accessLabel(engine, value) {
  return ACCESS[engine]?.[value] || (value ? String(value) : 'Не получено');
}

export function effortLabel(value) {
  return value === 'auto' ? 'auto' : value || 'Не получено';
}
