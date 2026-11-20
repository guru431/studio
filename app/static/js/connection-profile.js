import { tr } from './i18n.js';
/* Профиль API-подключения (profiles.py, спека 2026-11-21-studio-source-foundation-design §2): системный текст,
 * параметры запроса и подсказки. Окно `#conn-profile-dialog`; `url` — `/api/connections/<id>/profile` (своё) или
 * `/api/admin/shared/connections/<id>/profile` (общее, только администратор). Пустое поле параметра — не слать;
 * `top_k` у провайдера `openai` выключен и не отправляется. Сохраняется профиль целиком (PUT). */
import { api } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';
import { icon } from './icons.js';

const TEXT_MAX = 8000;
const SUGGESTIONS_MAX = 12;

/** Пределы — как `profiles.Params`; `above` — строго больше `min`. */
const PARAMS = [
  { key: 'temperature', label: () => tr("Температура"), min: 0, max: 2, step: '0.1' },
  { key: 'top_p', label: () => tr("Доля вероятностей"), min: 0, max: 1, step: '0.05', above: true },
  { key: 'top_k', label: () => tr("Число кандидатов"), min: 1, max: 1000, step: '1', int: true },
  { key: 'presence_penalty', label: () => tr("Штраф за повтор тем"), min: -2, max: 2, step: '0.1' },
  { key: 'max_tokens', label: () => tr("Предел ответа, токенов"), min: 1, max: 1000000, step: '1', int: true },
];

function range(p) {
  if (p.above) return tr("Больше {0} и не больше {1}", p.min, p.max);
  return p.int ? tr("Целое от {0} до {1}", p.min, p.max) : tr("От {0} до {1}", p.min, p.max);
}

/** Окно профиля подключения `conn`; `onSaved` — после удачного сохранения. */
export function profileDialog(host, url, conn, { shared = false, onSaved = null } = {}) {
  const text = el('textarea', { class: 'input', id: 'profile-text', rows: '6', maxlength: String(TEXT_MAX),
    placeholder: tr("Например: отвечай кратко, по-русски") });
  const count = el('p', { class: 'muted small profile-count', id: 'profile-text-count', 'aria-live': 'polite' });
  const showCount = () => { count.textContent = tr("{0} из {1} знаков", text.value.length, TEXT_MAX); };
  text.addEventListener('input', showCount);
  showCount();

  const inputs = {};
  const params = el('div', { class: 'profile-params' });
  for (const p of PARAMS) {
    const input = el('input', { class: 'input', type: 'number', id: `profile-${p.key.replace('_', '-')}`,
      inputmode: p.int ? 'numeric' : 'decimal', min: String(p.min), max: String(p.max), step: p.step,
      placeholder: tr("не задано"), autocomplete: 'off' });
    inputs[p.key] = input;
    const label = el('span', {}, p.label(), ' ', el('code', {}, p.key));
    const box = field(label, input, p.key);
    box.append(el('p', { class: 'muted small profile-hint', 'data-hint': p.key }, range(p)));
    params.append(box);
  }

  const list = el('ol', { class: 'profile-suggestions', id: 'profile-suggestions' });
  const add = button(tr("+ Подсказка"), { class: 'btn btn-sm', id: 'profile-suggestion-add' }, () => {
    addRow({ title: '', text: '' }).querySelector('input').focus();
  });

  function arrange() {
    const rows = [...list.children];
    rows.forEach((row, i) => {
      row.querySelector('.profile-up').disabled = i === 0;
      row.querySelector('.profile-down').disabled = i === rows.length - 1;
    });
    add.disabled = rows.length >= SUGGESTIONS_MAX;
  }

  function addRow(item) {
    const title = el('input', { class: 'input profile-suggestion-title', type: 'text', maxlength: '80',
      autocomplete: 'off', 'aria-label': tr("Заголовок подсказки"), placeholder: tr("Заголовок"), value: item.title });
    const body = el('textarea', { class: 'input profile-suggestion-text', rows: '2', maxlength: '2000',
      'aria-label': tr("Текст подсказки"), placeholder: tr("Текст, который уйдёт в диалог") });
    body.value = item.text;
    const row = el('li', { class: 'profile-suggestion' });
    const up = button(icon('upward'), { class: 'icon-btn profile-up', 'aria-label': tr("Выше"), title: tr("Выше") }, () => {
      if (row.previousElementSibling) list.insertBefore(row, row.previousElementSibling);
      arrange();
    });
    const down = button(icon('downward'), { class: 'icon-btn profile-down', 'aria-label': tr("Ниже"), title: tr("Ниже") }, () => {
      if (row.nextElementSibling) list.insertBefore(row.nextElementSibling, row);
      arrange();
    });
    const remove = button(icon('x'), { class: 'icon-btn profile-remove', 'aria-label': tr("Удалить подсказку"),
      title: tr("Удалить подсказку") }, () => { row.remove(); arrange(); });
    row.append(el('div', { class: 'profile-suggestion-fields' }, title, body),
      el('div', { class: 'profile-suggestion-tools' }, up, down, remove));
    list.append(row);
    arrange();
    return row;
  }

  const fail = el('p', { class: 'error', id: 'profile-status', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'profile-save', disabled: true });
  const intro = el('p', { class: 'muted small' }, shared
    ? tr("Текст добавляется к системному сообщению диалогов с этим подключением, параметры уходят в каждый запрос. Пользователи видят только подсказки.")
    : tr("Текст добавляется к системному сообщению диалогов с этим подключением, параметры уходят в каждый запрос."));
  const dialog = openDialog(host, { id: 'conn-profile-dialog', title: tr("Профиль: {0}", conn.name), actions: [save], body: [
    intro, field(tr("Текст профиля"), text, 'system_text'), count,
    el('h3', { class: 'profile-head' }, tr("Параметры запроса")),
    el('p', { class: 'muted small' }, tr("Пустое поле — параметр не отправляется.")), params,
    el('h3', { class: 'profile-head' }, tr("Подсказки")),
    el('p', { class: 'muted small' }, tr("Готовые запросы для нового диалога с этим подключением; их видят все, кому оно доступно.")),
    list, add, fail] });

  /** Тело PUT или null, если поле с ошибкой (слова — под полем). */
  function collect() {
    let ok = true;
    const out = {};
    for (const p of PARAMS) {
      const input = inputs[p.key];
      const error = dialog.querySelector(`.error[data-for="${p.key}"]`);
      error.textContent = '';
      if (input.disabled) continue;
      if (input.validity.badInput) {
        error.textContent = tr("Введите число");
        ok = false;
        continue;
      }
      if (input.value.trim() === '') continue;
      const value = Number(input.value);
      const fits = (p.above ? value > p.min : value >= p.min) && value <= p.max && (!p.int || Number.isInteger(value));
      if (!fits) {
        error.textContent = range(p);
        ok = false;
        continue;
      }
      out[p.key] = value;
    }
    const suggestions = [];
    for (const row of list.children) {
      const title = row.querySelector('input').value.trim();
      const body = row.querySelector('textarea').value.trim();
      if (!title && !body) continue;
      if (!title || !body) {
        fail.textContent = tr("Заполните заголовок и текст каждой подсказки");
        return null;
      }
      suggestions.push({ title, text: body });
    }
    return ok ? { system_text: text.value, params: out, suggestions } : null;
  }

  save.addEventListener('click', async () => {
    if (save.disabled) return;
    fail.textContent = '';
    const body = collect();
    if (!body) return;
    save.disabled = true;
    try {
      await api('PUT', url, body);
      dialog.close();
      if (onSaved) await onSaved();
    } catch (err) {
      fail.textContent = words(err);
      save.disabled = false;
    }
  });

  api('GET', url).then((profile) => {
    if (!dialog.open) return;
    text.value = profile.system_text;
    showCount();
    for (const p of PARAMS) inputs[p.key].value = p.key in profile.params ? String(profile.params[p.key]) : '';
    const topK = dialog.querySelector('[data-hint="top_k"]');
    if (profile.provider === 'openai') {
      inputs.top_k.disabled = true;
      topK.textContent = tr("OpenAI не принимает top_k — поле не отправляется");
    } else {
      topK.textContent = `${range(PARAMS[2])} · ${tr("только vLLM и совместимые")}`;
    }
    for (const item of profile.suggestions) addRow(item);
    arrange();
    save.disabled = false;
  }, (err) => { fail.textContent = words(err); });
  arrange();
  return dialog;
}
