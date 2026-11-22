/* Настройки DeepSeek Harness в чате: из какого профиля мост взял настройки и что из профиля не применено
 * (сводка наложения моста — hello.deepseek.settings, затем settings.overlay ответа state и события хода).
 * deepseekView(host, settings) — панель перед host (форма ввода); update(settings, { fixed }) — новая сводка
 * и признак «права закреплены при создании чата» (settings.access_fixed). */
import { tr } from './i18n.js';
import { el } from './ui.js';

const REASONS = {
  unknown: tr("неизвестный вид настройки"),
  acp_own: tr("задано в профиле acp или общем патче DSH"),
  env_key: tr("ключ из переменной окружения недоступен мосту"),
  unreadable: tr("профиль не читается"),
};

function source(profile) {
  if (profile === 'web') return tr("Настройки взяты из профиля Web");
  if (profile === 'desktop') return tr("Настройки взяты из профиля Desktop");
  return tr("Профиль DeepSeek Harness не найден — действуют общие настройки");
}

export function deepseekView(host, settings) {
  const profile = el('p', { class: 'muted small code-deepseek-profile' });
  const fixed = el('p', { class: 'muted small code-deepseek-fixed', hidden: true }, tr("Права заданы при создании чата"));
  const skipped = el('ul', { class: 'code-deepseek-skipped' });
  const skippedBox = el('div', { class: 'code-deepseek-not-applied', hidden: true },
    el('p', { class: 'small' }, tr("Не применено:")), skipped);
  const panel = el('details', { id: 'code-deepseek-options' },
    el('summary', {}, tr("Настройки DeepSeek Harness")), profile, fixed, skippedBox);
  host.before(panel);
  let shown = false;

  function update(summary, { fixed: locked } = {}) {
    if (typeof locked === 'boolean') fixed.hidden = !locked;
    if (!summary || typeof summary !== 'object') {
      if (!shown) profile.textContent = source(null);
      return;
    }
    shown = true;
    profile.textContent = source(summary.profile);
    const rows = (Array.isArray(summary.skipped) ? summary.skipped : [])
      .filter((row) => row && typeof row.name === 'string');
    skipped.replaceChildren(...rows.map((row) =>
      el('li', {}, `${row.name} — ${REASONS[row.reason] || String(row.reason ?? '')}`)));
    skippedBox.hidden = !rows.length;
  }

  update(settings);
  return { update };
}
