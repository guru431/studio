/* Режимы обоих CLI: сервер и мост проверяют значения независимо от страницы. */
export const ACCESS = {
  claude: { default: 'Обычные права', auto: 'Автоматически', acceptEdits: 'Правка файлов',
    plan: 'Только план', bypassPermissions: 'Полный доступ', dontAsk: 'Без запросов' },
  codex: { 'read-only': 'Только чтение', 'workspace-write': 'Проект', 'danger-full-access': 'Полный доступ' },
};

export function accessLabel(engine, value) {
  return ACCESS[engine]?.[value] || (value ? String(value) : 'Не получено');
}

export function effortLabel(value) {
  return value === 'auto' ? 'auto' : value || 'Не получено';
}
