/* Настройки и статусы OpenCode; тексты сессии остаются на открытой странице. Команды OpenCode — строки меню «/»
 * поля задания (code-skills.js). */
import { tr } from './i18n.js';
import { timeOnly } from './time.js';
import { el } from './ui.js';

export function opencodeView(form, { enabled, shared, changed }) {
  if (!enabled) return { options: () => ({}), settings() {}, progress() {}, busy() {} };
  const mode = el('select', { id: 'code-agent-mode', 'aria-label': tr('Основной агент') },
    el('option', { value: '' }, tr('По настройкам OpenCode')));
  const directories = el('textarea', { id: 'code-additional-directories', rows: '2', maxlength: '8200',
    'aria-label': tr('Дополнительные каталоги'), placeholder: tr('Полные пути, по одному на строку') });
  const status = el('p', { id: 'code-opencode-status', class: 'muted small', role: 'status', hidden: true });
  const panel = el('details', { id: 'code-opencode-options' }, el('summary', {}, tr('Настройки OpenCode')),
    el('label', {}, tr('Основной агент'), mode),
    ...(!shared ? [el('label', {}, tr('Дополнительные каталоги'), directories)] : []));
  form.before(panel, status);
  let touchedMode = false, touchedDirectories = false;
  mode.addEventListener('change', () => { touchedMode = true; changed(); });
  directories.addEventListener('change', () => { touchedDirectories = true; changed(); });
  return {
    options: () => ({ ...(mode.value ? { agent_mode: mode.value } : {}),
      ...(touchedDirectories && !shared ? { additional_directories: directories.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean) } : {}) }),
    settings(data) {
      const selected = mode.value;
      if (Array.isArray(data.modes)) {
        mode.replaceChildren(el('option', { value: '' }, tr('По настройкам OpenCode')),
          ...data.modes.map(row => el('option', { value: row.id }, row.label || row.id)));
      }
      mode.value = touchedMode ? selected : data.agent_mode || '';
      if (!touchedDirectories && Array.isArray(data.additional_directories)) directories.value = data.additional_directories.join('\n');
    },
    progress(data) {
      const rows = [];
      const retry = data?.retry;
      if (retry) {
        const stamp = Date.parse(retry.next) / 1000;   // ISO-время моста → секунды для time.js (пояс учётки)
        rows.push(tr('Повторная попытка {0}', retry.attempt) + (Number.isFinite(stamp) ? ` · ${timeOnly(stamp)}` : '')
          + (Number.isFinite(retry.status) ? ` · HTTP ${retry.status}` : ''));
      }
      const states = { started: tr('Сжимаю контекст…'), in_progress: tr('Сжимаю контекст…'),
        completed: tr('Контекст сжат'), failed: tr('Не удалось сжать контекст') };
      if (states[data?.compaction]) rows.push(states[data.compaction]);
      status.textContent = rows.join(' · ');
      status.hidden = !rows.length;
    },
    busy(value) { mode.disabled = directories.disabled = value; },
  };
}
