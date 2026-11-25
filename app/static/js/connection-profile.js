import { tr } from './i18n.js';
/* Профиль API-подключения (profiles.py, спека 2026-11-21-studio-source-foundation-design §2): системный текст,
 * параметры запроса и подсказки. Окно `#conn-profile-dialog`; `url` — `/api/connections/<id>/profile` (своё) или
 * `/api/admin/shared/connections/<id>/profile` (общее, только администратор). Пустое поле параметра — не слать;
 * `top_k` у провайдера `openai` выключен и не отправляется. Сохраняется профиль целиком (PUT). «Параметры модели»
 * (п. 35 спеки 2026-11-25) — те же поля на одну модель (`model_params`), сильнее параметров подключения; пустое поле
 * блока — как у подключения, блок без значений не уходит. */
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
  // в запрос не уходит: предел картинок на весь запрос (`profiles.max_images`), без него — 4
  { key: 'max_images', label: () => tr("Картинок в запросе"), min: 1, max: 32, step: '1', int: true },
  // в запрос не уходит: предел инструментов MCP хода (`profiles.max_tools`), без него — 32 у локальной модели
  // (`mcp_client.LOCAL_MAX_TOOLS`), иначе 128 (API в духе OpenAI больше не принимают)
  { key: 'max_tools', label: () => tr("Инструментов в запросе"), min: 1, max: 256, step: '1', int: true },
  // в запрос не уходит: окно контекста модели (`chat.model_window`), без него — из списка моделей шлюза или таблицы
  { key: 'context_window', label: () => tr("Окно контекста, токенов"), min: 8192, max: 4000000, step: '1', int: true },
];
const MODEL_MAX = 200;           // знаков в имени модели — `sections.MODEL_MAX`
const MODELS_MAX = 200;          // моделей с параметрами — `sections.MODELS_MAX`

function range(p) {
  if (p.above) return tr("Больше {0} и не больше {1}", p.min, p.max);
  return p.int ? tr("Целое от {0} до {1}", p.min, p.max) : tr("От {0} до {1}", p.min, p.max);
}

/** Поле параметра `p`; `id` — у полей подключения, в блоке модели поле узнаётся по `data-param`. */
function paramInput(p, id, placeholder) {
  return el('input', { class: 'input', type: 'number', ...(id ? { id } : {}), 'data-param': p.key,
    inputmode: p.int ? 'numeric' : 'decimal', min: String(p.min), max: String(p.max), step: p.step,
    placeholder, autocomplete: 'off' });
}

/** Значения полей `inputs` (ключ → поле), слова ошибки — в `errorOf(ключ)`. → объект или null, если есть ошибка. */
function readParams(inputs, errorOf) {
  let ok = true;
  const out = {};
  for (const p of PARAMS) {
    const input = inputs[p.key];
    const error = errorOf(p.key);
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
  return ok ? out : null;
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
    const input = paramInput(p, `profile-${p.key.replace('_', '-')}`, tr("не задано"));
    inputs[p.key] = input;
    const label = el('span', {}, p.label(), ' ', el('code', {}, p.key));
    const box = field(label, input, p.key);
    box.append(el('p', { class: 'muted small profile-hint', 'data-hint': p.key }, range(p)));
    params.append(box);
  }

  let openai = false;               // провайдер `openai` не принимает top_k: поле выключено и в блоках моделей
  let blocks = 0;                   // счётчик блоков моделей — метка `data-for` их ошибок «<n>:<ключ>»
  const models = el('div', { class: 'profile-models', id: 'profile-models' });
  const modelName = el('input', { class: 'input', type: 'text', id: 'profile-model-name', maxlength: String(MODEL_MAX),
    autocomplete: 'off', 'aria-label': tr("Имя модели"), placeholder: tr("Имя модели") });
  const known = ((conn.passport && conn.passport.models) || []).map((m) => m.id);
  const options = known.length
    ? el('datalist', { id: 'profile-model-list' }, ...known.map((id) => el('option', { value: id }))) : null;
  if (options) modelName.setAttribute('list', 'profile-model-list');
  // до загрузки профиля выключена: блоки сохранённых моделей и выключенный top_k приходят с ним
  const modelAdd = button(tr("Добавить"), { class: 'btn btn-sm', id: 'profile-model-add', disabled: true }, () => {
    const name = modelName.value.trim();
    if (!name) { modelName.focus(); return; }
    const block = [...models.children].find((b) => b.dataset.model === name) || addModel(name, {});
    modelName.value = '';
    block.scrollIntoView({ block: 'nearest' });
    block.querySelector('input:not(:disabled)').focus();
  });
  modelName.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); modelAdd.click(); }
  });

  /** Блок параметров модели `name` со значениями `values`. */
  function addModel(name, values) {
    const n = ++blocks;
    const block = el('div', { class: 'profile-model', 'data-model': name });
    const fields = el('div', { class: 'profile-params' });
    for (const p of PARAMS) {
      const input = paramInput(p, null, tr("как у подключения"));
      if (p.key in values) input.value = String(values[p.key]);
      if (p.key === 'top_k' && openai) input.disabled = true;
      fields.append(field(el('span', {}, p.label(), ' ', el('code', {}, p.key)), input, `${n}:${p.key}`));
    }
    const remove = button(icon('x'), { class: 'icon-btn profile-model-remove', 'aria-label': tr("Убрать модель {0}", name),
      title: tr("Убрать модель") }, () => { block.remove(); modelAdd.disabled = false; });
    block.append(el('div', { class: 'profile-model-head' }, el('strong', { class: 'profile-model-title' }, name), remove),
      fields);
    models.append(block);
    modelAdd.disabled = models.children.length >= MODELS_MAX;
    return block;
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
    el('h3', { class: 'profile-head' }, tr("Параметры модели")),
    el('p', { class: 'muted small' }, tr("Для своей модели сильнее параметров подключения; пустое поле — как у подключения.")),
    el('div', { class: 'profile-model-new' }, modelName, modelAdd), options, models,
    el('h3', { class: 'profile-head' }, tr("Подсказки")),
    el('p', { class: 'muted small' }, tr("Готовые запросы для нового диалога с этим подключением; их видят все, кому оно доступно.")),
    list, add, fail] });

  /** Тело PUT или null, если поле с ошибкой (слова — под полем). */
  function collect() {
    const out = readParams(inputs, (key) => dialog.querySelector(`.error[data-for="${key}"]`));
    let ok = out !== null;
    const perModel = {};
    for (const block of models.children) {
      const fields = Object.fromEntries([...block.querySelectorAll('input[data-param]')].map((i) => [i.dataset.param, i]));
      const values = readParams(fields, (key) => block.querySelector(`.error[data-for$=":${key}"]`));
      if (values === null) ok = false;
      else if (Object.keys(values).length) perModel[block.dataset.model] = values;
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
    return ok ? { system_text: text.value, params: out, model_params: perModel, suggestions } : null;
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
    openai = profile.provider === 'openai';
    if (openai) {
      inputs.top_k.disabled = true;
      topK.textContent = tr("OpenAI не принимает top_k — поле не отправляется");
    } else {
      topK.textContent = `${range(PARAMS[2])} · ${tr("только vLLM и совместимые")}`;
    }
    for (const [name, values] of Object.entries(profile.model_params || {})) addModel(name, values);
    modelAdd.disabled = models.children.length >= MODELS_MAX;
    for (const item of profile.suggestions) addRow(item);
    arrange();
    save.disabled = false;
  }, (err) => { fail.textContent = words(err); });
  arrange();
  return dialog;
}
