import { locale, tr } from './i18n.js';
/* Раздел «Настройки» (#/settings, #/settings/<раздел>) — вариант А спеки 2026-10-20 (раздел 6.2).
 *
 * Слева две группы. «Разделы» — в порядке рельса (Чат, Фото, Видео, Озвучка, Расшифровка, Сканы, Перевод, Агенты;
 * «Агенты» — умолчания чатов и компьютеры) и «Очистка данных · Cleaner». «Учётка»: «Оформление» (тема и акцент
 * учётки, шрифт, масштаб, плотность и нижняя панель этого устройства, часовой пояс — appearance.js),
 * «Защита» (пароль, блокировка и PIN, ключи устройства; сессии, доступы и ссылки, аварийный выход и журнал безопасности —
 * account-security.js), «Уведомления» (push, ntfy), «Данные» (хранилище, выгрузка, удаление истории),
 * «Ввод и интернет» (голосовой ввод, ключ Exa). Прежние адреса (`other` — «Прочее», `dictation`) ведут
 * на новые (OLD_IDS). Справа — выбранный раздел: его
 * подключения карточками (имя, адрес, хвост ключа, «Обновить модели», меню «⋯»: изменить, убрать из
 * раздела, удалить подключение целиком), галочки моделей и одна модель по умолчанию (радио);
 * «+ Адрес для раздела» — уже заведённое подключение или новый адрес. Галочка и радио сохраняются
 * сразу; отказ — словами в строке статуса раздела.
 *
 * Модели: у шлюза srvllm вне чата — его встроенный перечень (`catalog`), прочее — после «Обновить
 * модели» (`GET …/found`, через сервер). Список прямого подключения берёт браузер (modelsOf: токен и
 * адрес — из одного ответа /token, токен живёт только в памяти запроса; сервис без CORS — словами).
 * Отмеченная модель, которой нет в найденном списке, остаётся отмеченной с пометкой «не найдена».
 * Подключение, убранное из списка администратора (`dropped`), спрашивается у сервера: он скажет это словами.
 *
 * «Проверить» и паспорт под шапкой карточки, «Пробный ответ» у отмеченных моделей чата не-srvllm
 * подключений — passport.js; найденные проверкой модели встают в найденные. Только в своих настройках.
 *
 * В «Речи» своих настроек — ещё «Голоса» (образцы для озвучки srvllm): запись 5–15 с файлом или
 * с микрофона, текст из ASR с возможностью правки и имя (POST /api/voices), список с «Удалить».
 *
 * settingsView — и для панели администратора (базы API — /api/admin/users/<id>/…, разделы
 * переключаются на месте, из «Учётки» — только «Голосовой ввод»; личных настроек, голосов и прямого списка моделей нет). На телефоне без раздела
 * в адресе — список разделов, с разделом — его экран со ссылкой «‹ Настройки». */
import { api, loadMe, me } from './api.js';
import { emailBlock } from './email-settings.js';
import { accessBlock, emergencyBlock, securityEventsBlock, sessionsBlock } from './account-security.js';
import { accountLookBlock, deviceLookBlock } from './appearance.js';
import { openMobileNavigationEditor } from './mobile-navigation.js';
import { DEFAULT_TIMEZONE, dateOnly, dateTime } from './time.js';
import { ACCESS, ENGINE_NAMES, accessLabel, effortLabel, engineOrder } from './code-options.js';
import { quickActionsBlock } from './code-quick-actions.js';
import { ntfyBlock } from './ntfy-settings.js';
import { connectionForm, editConnection, SECTION_NAMES, sectionsText } from './connection-form.js';
import { notifyEnabled, notifySupported, setNotify } from './jobs.js';
import { addPasskey, FRESH_SEC, isTouch, passkeyOptions } from './lock.js';
import { recordingName, startRecording } from './mic.js';
import { check, passportBlock, probe, PROBE_WARNING } from './passport.js';
import { agentDownload, button, el, field, openDialog, plural, showError, words } from './ui.js';
import { icon } from './icons.js';
import { NAMES, SECTIONS as RAIL_SECTIONS, sectionPairs } from './sections-meta.js';

export const CORS = tr("Сервис не разрешает запросы из браузера (CORS) — подключите его через сервер");
const LABELS = { lustify: 'Lustify', flux: 'FLUX', wan: 'Wan', whisperx: 'WhisperX', 'qwen3-tts': 'Qwen3-TTS',
  gigaam: 'GigaAM' };
const SEARCH_FROM = 8;

/** Подпись модели: короткие имена шлюза — словами, прочие — как есть. */
export function modelLabel(id) {
  return LABELS[id] || id;
}

/* ---- модели прямого подключения ------------------------------------------------ */

async function directModels(conn) {
  let token;
  let url;
  try {
    ({ token, models_url: url } = await api('GET', `/api/connections/${conn.id}/token`));
  } catch (err) {
    if (err.status === 403 || err.status === 404) throw new Error(tr("Токен этого подключения браузеру не выдаётся"));
    throw err;
  }
  let response;
  try {
    response = await fetch(url, { headers: { Authorization: `Bearer ${token}` },
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
  } catch (err) {
    if (err instanceof TypeError) throw new Error(CORS);
    throw err;
  }
  if (response.status === 401 || response.status === 403) throw new Error(tr("Сервис не принял токен ({0})", response.status));
  if (!response.ok) throw new Error(tr("Сервис не отдал список моделей ({0})", response.status));
  let data;
  try {
    data = (await response.json()).data;
  } catch {
    throw new Error(tr("Сервис ответил не списком моделей"));
  }
  return (Array.isArray(data) ? data : []).map((m) => m && m.id).filter((id) => typeof id === 'string');
}

/** Имена моделей своего подключения: через сервер — GET /api/connections/<id>/models, прямое — браузер. */
export async function modelsOf(conn) {
  if (conn.proxied) return (await api('GET', `/api/connections/${conn.id}/models`)).items.map((m) => m.id);
  return directModels(conn);
}

/* ---- «Учётка»: оформление, защита, уведомления, данные ------------------------------ */

function say(node, text, kind) {
  node.textContent = text;
  node.classList.toggle('error', kind === 'error');
  node.classList.toggle('ok', kind === 'ok');
}

function status(id) {
  return el('p', { class: 'status', id, 'aria-live': 'polite' });
}

function enginePriorityBlock(defaults) {
  const order = engineOrder(defaults);
  const rows = el('ol', { id: 'code-engine-order', class: 'code-engine-order' });
  const note = status('code-engine-order-status');
  function paint() {
    rows.replaceChildren(...order.map((engine, index) => {
      const move = (step) => {
        [order[index], order[index + step]] = [order[index + step], order[index]];
        note.textContent = '';
        paint();
      };
      const up = button('↑', { class: 'btn btn-sm code-engine-up', disabled: index === 0,
        'aria-label': tr("Поднять {0}", ENGINE_NAMES[engine]) }, () => move(-1));
      const down = button('↓', { class: 'btn btn-sm code-engine-down', disabled: index === order.length - 1,
        'aria-label': tr("Опустить {0}", ENGINE_NAMES[engine]) }, () => move(1));
      return el('li', { 'data-engine': engine }, el('span', { class: 'code-engine-name' }, `${index + 1}. ${ENGINE_NAMES[engine]}`),
        el('div', { class: 'code-engine-order-controls' }, up, down));
    }));
  }
  const save = button(tr("Сохранить приоритет"), { id: 'code-engine-order-save', class: 'btn' }, async () => {
    save.disabled = true;
    for (const control of rows.querySelectorAll('button')) control.disabled = true;
    try {
      const code_defaults = Object.fromEntries(order.map((engine, index) => [engine, { priority: index + 1 }]));
      const answer = await api('PATCH', '/api/me', { code_defaults });
      me.code_defaults = answer.code_defaults;
      say(note, tr("Приоритет агентов сохранён"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
      paint();
    }
  });
  paint();
  return el('section', { class: 'block code-engine-priority' }, el('h3', {}, tr("Приоритет агентов")),
    el('p', { class: 'muted small' }, tr("Порядок кнопок нового чата. Для автоматического создания чата выбирается первый доступный агент. Сохраняется для всех ваших устройств.")), rows, save, note);
}

function codeDefaultsBlock(agents = () => api('GET', '/api/agents')) {
  const note = status('code-defaults-status');
  const rows = el('div', { id: 'code-defaults-rows' });
  const block = el('section', { class: 'block code-defaults-block' }, el('h2', {}, tr("Claude Code, Codex и OpenCode")),
    el('p', { class: 'muted small' }, tr("Модель, усилие и права для новых чатов на всех ваших устройствах. Начатые чаты сохраняют свой выбор. Показаны текущие настройки подключённого компьютера; в проекте они уточняются при открытии чата.")),
    rows, note);

  async function load() {
    try {
      const [{ items }, profile] = await Promise.all([agents(), loadMe()]);
      const forms = ['codex', 'claude', 'opencode'].map((engine) => {
        const catalogs = items.filter((a) => a.online).map((a) => engine === 'claude' ? a : a[engine]).filter(Boolean);
        const models = new Map();
        const efforts = new Set();
        const native = catalogs.find((c) => c.defaults)?.defaults || {};
        for (const catalog of catalogs) {
          for (const model of catalog.models || []) models.set(model.id, model.label || model.id);
          for (const effort of catalog.efforts || []) efforts.add(effort);
        }
        const saved = profile.code_defaults?.[engine] || {};
        if (saved.model && !models.has(saved.model)) models.set(saved.model, tr("{0} (сейчас недоступна)", saved.model));
        if (saved.effort) efforts.add(saved.effort);
        const prefix = `code-defaults-${engine}`;
        const model = el('select', { class: 'input', id: `${prefix}-model` },
          el('option', { value: '' }, native.model || (catalogs.length ? tr("Не получено") : tr("Компьютер не на связи"))),
          ...[...models].map(([id, label]) => el('option', { value: id }, label)));
        const effort = el('select', { class: 'input', id: `${prefix}-effort` },
          el('option', { value: '' }, effortLabel(native.effort)),
          ...[...efforts].map((e) => el('option', { value: e }, effortLabel(e))));
        model.value = saved.model || '';
        effort.value = saved.effort || '';
        const access = el('select', { class: 'input', id: `${prefix}-access` },
          el('option', { value: '' }, accessLabel(engine, native.access)),
          ...Object.entries(ACCESS[engine]).map(([value, label]) => el('option', { value }, label)));
        access.value = saved.access || '';
        return { engine, model, effort, access, node: el('div', { class: 'block' },
          el('h3', {}, ENGINE_NAMES[engine]), el('div', { class: 'grid3' },
            field(tr("Модель"), model, `${prefix}-model`), field(tr("Усилие"), effort, `${prefix}-effort`),
            field(tr("Права доступа"), access, `${prefix}-access`))) };
      });
      const save = button(tr("Сохранить"), { class: 'btn', id: 'code-defaults-save' }, async () => {
        save.disabled = true;
        try {
          const code_defaults = Object.fromEntries(forms.map(({ engine, model, effort, access }) => [engine,
            { model: model.value || null, effort: effort.value || null, access: access.value || null }]));
          const answer = await api('PATCH', '/api/me', { code_defaults });
          me.code_defaults = answer.code_defaults;
          say(note, tr("Умолчания сохранены — применятся к новым чатам"), 'ok');
        } catch (err) {
          say(note, words(err), 'error');
        } finally {
          save.disabled = false;
        }
      });
      rows.replaceChildren(enginePriorityBlock(profile.code_defaults), ...forms.map((f) => f.node),
        ...(!items.some((a) => a.online) ? [el('p', { class: 'muted small' }, tr("Подключите компьютер, чтобы выбрать его модели и усилия."))] : []),
        el('div', { class: 'row' }, save));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }
  load();
  return block;
}

/** «Нижняя панель» телефона — тот же редактор, что в листе «Ещё» (mobile-navigation.js); на широком экране скрыт. */
function mobileNavBlock() {
  const open = button(tr("Настроить нижнюю панель"), { class: 'btn', id: 'appearance-mobile-nav-open',
    'aria-haspopup': 'dialog' }, () => openMobileNavigationEditor(open));
  return el('section', { class: 'block appearance-mobile-nav' }, el('h2', {}, tr("Нижняя панель")),
    el('p', { class: 'muted small scope-note' }, tr("Это устройство — этот браузер и учётка.")),
    el('p', { class: 'muted small' }, tr("На телефоне: все разделы в один ряд или 3–4 основных и «Ещё». Та же настройка — в листе «Ещё».")), open);
}

function timezoneBlock(refreshTimes) {
  const zones = [...new Set([DEFAULT_TIMEZONE, 'UTC', me.timezone,
    ...(Intl.supportedValuesOf?.('timeZone') || [])].filter(Boolean))].sort();
  const select = el('select', { class: 'input', id: 'timezone' }, ...zones.map((zone) =>
    el('option', { value: zone }, zone === DEFAULT_TIMEZONE ? tr("Москва (Europe/Moscow)") : zone)));
  select.value = me.timezone || DEFAULT_TIMEZONE;
  const note = status('timezone-status');
  const save = button(tr("Сохранить"), { class: 'btn', id: 'timezone-save' }, async () => {
    save.disabled = select.disabled = true;
    try {
      const profile = await api('PATCH', '/api/me', { timezone: select.value });
      me.timezone = profile.timezone;
      refreshTimes();
      document.dispatchEvent(new Event('studio-me'));
      say(note, tr("Часовой пояс сохранён"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = select.disabled = false;
    }
  });
  return el('section', { class: 'block' }, el('h2', {}, tr("Часовой пояс")),
    el('p', { class: 'muted small scope-note' }, tr("Вся учётка — на всех ваших устройствах.")),
    el('p', { class: 'muted small' }, tr("Для дат, времени и поиска на всех ваших устройствах, а также календарной даты в контексте чата — через сервер, напрямую и через Cleaner. По умолчанию — Москва.")),
    field(tr("Часовой пояс"), select, select.id), el('div', { class: 'row' }, save, note));
}

function passwordBlock() {
  const input = (id, autocomplete) => el('input', { class: 'input', id, type: 'password', autocomplete, maxlength: '1024' });
  const current = input('pw-current', 'current-password');
  const fresh = input('pw-new', 'new-password');
  const repeat = input('pw-repeat', 'new-password');
  const note = status('password-status');
  const save = button(tr("Сменить пароль"), { class: 'btn', id: 'password-save' });
  save.addEventListener('click', async () => {
    if (fresh.value !== repeat.value) {
      say(note, tr("Пароли не совпадают"), 'error');
      return;
    }
    save.disabled = true;
    try {
      await api('POST', '/api/me/password', { current: current.value, new: fresh.value });
      current.value = fresh.value = repeat.value = '';
      say(note, tr("Пароль сменён, другие сессии закрыты"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  return el('section', { class: 'block' }, el('h2', {}, tr("Пароль")), el('div', { class: 'grid3' },
    field(tr("Текущий"), current, 'current'), field(tr("Новый"), fresh, 'new'), field(tr("Повтор нового"), repeat, 'repeat')),
  el('div', { class: 'row' }, save, note));
}

function day(ts) {
  return dateOnly(ts, { day: 'numeric', month: 'long' });
}

const LOCK_SECTIONS = Object.fromEntries(sectionPairs());

/** Пользователь задаёт время в удобных единицах; сервер всегда получает целое число секунд. */
function timeoutField(id, seconds, max) {
  const unit = seconds > 0 && seconds % 3600 === 0 ? 3600 : seconds > 0 && seconds % 60 === 0 ? 60 : 1;
  const amount = el('input', { class: 'input', id, type: 'number', min: '0', step: '1', value: seconds / unit });
  const units = el('select', { class: 'input', id: `${id}-unit`, 'aria-label': tr("Единицы времени") },
    ...[[1, tr("секунд")], [60, tr("минут")], [3600, tr("часов")]].map(([value, text]) => el('option', { value }, text)));
  units.value = String(unit);
  const read = () => Number(amount.value) * Number(units.value);
  return { node: el('div', { class: 'lock-timeout' }, amount, units), read,
    valid: (min = 0) => amount.value !== '' && Number.isInteger(read()) && read() >= min && read() <= max };
}

function computerPinBlock() {
  const timer = timeoutField('computer-pin-timeout', me.computer_pin_timeout ?? 10800, 604800);
  const note = status('computer-pin-status');
  const save = button(tr("Сохранить таймер компьютера"), { class: 'btn', id: 'computer-pin-save' }, async () => {
    if (!timer.valid(60)) {
      say(note, tr("Укажите время от 1 минуты до 7 дней"), 'error');
      return;
    }
    save.disabled = true;
    try {
      const answer = await api('PATCH', '/api/me', { computer_pin_timeout: timer.read() });
      me.computer_pin_timeout = answer.computer_pin_timeout;
      say(note, tr("Таймер PIN компьютера сохранён"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  return el('section', { class: 'block' }, el('h2', {}, tr("PIN компьютера")),
    el('p', { class: 'muted small' }, tr("Через это время без обращений компьютер снова запросит свой PIN. Настройка действует для всех ваших компьютеров и отдельно от PIN приложения.")),
    field(tr("Запрашивать повторно через"), timer.node, 'computer-pin-period'), el('div', { class: 'row' }, save, note));
}

/* «Блокировка»: всё приложение или выбранные разделы и таймер; изменения включённой защиты и выключение —
 * с паролем учётки. PIN учётки, ключи учётки с «Удалить» и — на
 * телефоне и планшете — «Добавить Face ID / отпечаток»: пароль и «Далее» готовят варианты, и нажатие
 * «Добавить» сразу зовёт navigator.credentials.create (Safari без нажатия Face ID не покажет). Варианты
 * старше FRESH_SEC — снова к паролю и «Далее»: вызов на сервере истекает через 120 с. */
function lockBlock() {
  const note = status('screen-lock-status');
  const enabled = el('input', { type: 'checkbox', id: 'lock-enabled', checked: !!me.screen_lock });
  const offPassword = el('input', { class: 'input', id: 'lock-off-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const off = button(tr("Выключить"), { class: 'btn', id: 'lock-off' });
  const offBox = el('div', { hidden: true }, field(tr("Пароль учётки — чтобы выключить блокировку"), offPassword, 'lock-off'),
    el('div', { class: 'row' }, off));
  const list = el('ul', { class: 'passkeys', id: 'passkeys' });
  const chosen = me.screen_lock_sections || ['*'];
  const all = el('input', { type: 'radio', name: 'lock-scope', id: 'lock-scope-all', checked: chosen.includes('*') });
  const some = el('input', { type: 'radio', name: 'lock-scope', id: 'lock-scope-sections', checked: !chosen.includes('*') });
  const checks = Object.entries(LOCK_SECTIONS).map(([id, title]) => {
    const input = el('input', { type: 'checkbox', id: `lock-section-${id}`, value: id, checked: chosen.includes(id) });
    return { id, input, node: el('label', { class: 'check' }, input, title) };
  });
  const selection = el('div', { class: 'lock-sections', id: 'lock-sections', hidden: all.checked }, ...checks.map((c) => c.node));
  const timer = timeoutField('lock-timeout', me.screen_lock_timeout ?? 60, 86400);
  const password = el('input', { class: 'input', id: 'lock-policy-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const passwordBox = el('div', { hidden: !me.screen_lock }, field(tr("Пароль учётки — чтобы изменить защиту"), password, 'lock-policy'));
  const save = button(tr("Сохранить защиту приложения"), { class: 'btn', id: 'lock-save' });
  const selected = () => all.checked ? ['*'] : checks.filter((c) => c.input.checked).map((c) => c.id);
  const valid = () => {
    if (!selected().length) {
      say(note, tr("Выберите хотя бы один раздел или всё приложение"), 'error');
      return false;
    }
    if (!timer.valid()) {
      say(note, tr("Укажите время от 0 секунд до 24 часов"), 'error');
      return false;
    }
    return true;
  };
  for (const radio of [all, some]) radio.addEventListener('change', () => { selection.hidden = all.checked; });

  async function setLock(body) {
    const answer = await api('POST', '/api/me/screen-lock', body);
    me.screen_lock = answer.screen_lock;
    me.screen_lock_sections = answer.screen_lock_sections || body.sections || me.screen_lock_sections;
    me.screen_lock_timeout = answer.screen_lock_timeout ?? body.timeout ?? me.screen_lock_timeout;
    enabled.checked = answer.screen_lock;
    passwordBox.hidden = !answer.screen_lock;
    document.dispatchEvent(new Event('studio-me'));
  }

  enabled.addEventListener('change', async () => {
    say(note, '');
    if (!enabled.checked) {           // выключение — с паролем: переключатель включён, пока не «Выключить»
      enabled.checked = true;
      offBox.hidden = false;
      offPassword.focus();
      return;
    }
    if (!valid()) {
      enabled.checked = false;
      return;
    }
    try {
      await setLock({ enabled: true, sections: selected(), timeout: timer.read() });
      say(note, tr("Защита приложения включена"), 'ok');
    } catch (err) {
      enabled.checked = false;
      say(note, words(err), 'error');
    }
  });
  save.addEventListener('click', async () => {
    if (!valid()) return;
    save.disabled = true;
    try {
      await setLock({ enabled: enabled.checked, sections: selected(), timeout: timer.read(), password: password.value });
      password.value = '';
      say(note, tr("Настройки защиты приложения сохранены"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  off.addEventListener('click', async () => {
    off.disabled = true;
    try {
      await setLock({ enabled: false, password: offPassword.value });
      offPassword.value = '';
      offBox.hidden = true;
      say(note, tr("Блокировка выключена"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      off.disabled = false;
    }
  });

  // PIN учётки (спека 2026-10-28-studio-lock-pin): задать и сменить — с паролем учётки, убрать — без;
  // повтор не совпал — запроса нет. На любом устройстве. Поля PIN — не password (пароль учётки рядом и два поля
  // password менеджер паролей принял бы за смену пароля и предложил бы сохранить PIN): text с точками
  // (-webkit-text-security, app.css), one-time-code — без автозаполнения.
  const pinState = el('span', { id: 'pin-state' });
  const pinSet = button('', { class: 'btn', id: 'pin-set' });
  const pinRemove = button(tr("Убрать PIN"), { class: 'btn', id: 'pin-remove' });
  const pinPassword = el('input', { class: 'input', id: 'pin-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const pinInput = (id) => el('input', { class: 'input', id, type: 'text', inputmode: 'numeric',
    autocomplete: 'one-time-code', maxlength: '8' });
  const pinNew = pinInput('pin-new');
  const pinRepeat = pinInput('pin-repeat');
  const pinSave = button(tr("Сохранить"), { class: 'btn', id: 'pin-save' });
  const pinForm = el('div', { id: 'pin-form', class: 'pin-form', hidden: true }, el('div', { class: 'grid3' },
    field(tr("Пароль учётки"), pinPassword, 'pin-password'), field('PIN', pinNew, 'pin-new'),
    field(tr("Повтор PIN"), pinRepeat, 'pin-repeat')),
  el('p', { class: 'muted small' }, tr("От 4 до 8 цифр. Не совпадающий с кодом телефона")), el('div', { class: 'row' }, pinSave));

  function paintPin() {
    pinState.textContent = me.pin ? tr("PIN задан") : tr("PIN: не задан");
    pinSet.textContent = me.pin ? tr("Сменить PIN") : tr("Задать PIN");
    pinRemove.hidden = !me.pin;
  }

  pinSet.addEventListener('click', () => {
    say(note, '');
    pinForm.hidden = false;
    pinPassword.focus();
  });
  pinSave.addEventListener('click', async () => {
    if (pinNew.value !== pinRepeat.value) {
      say(note, tr("PIN и повтор не совпадают"), 'error');
      return;
    }
    pinSave.disabled = true;
    try {
      await api('PUT', '/api/me/pin', { password: pinPassword.value, pin: pinNew.value });
      me.pin = true;
      pinPassword.value = pinNew.value = pinRepeat.value = '';
      pinForm.hidden = true;
      paintPin();
      say(note, tr("PIN задан"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      pinSave.disabled = false;
    }
  });
  pinRemove.addEventListener('click', async () => {
    pinRemove.disabled = true;
    try {
      await api('DELETE', '/api/me/pin');
      me.pin = false;
      pinPassword.value = pinNew.value = pinRepeat.value = '';
      pinForm.hidden = true;
      paintPin();
      say(note, tr("PIN убран"), 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      pinRemove.disabled = false;
    }
  });
  paintPin();

  async function reload() {
    try {
      const { items } = await api('GET', '/api/passkeys');
      list.replaceChildren(...(items.length ? items.map((k) => {
        const del = button(tr("Удалить"), { class: 'btn btn-sm btn-danger passkey-delete' }, async () => {
          del.disabled = true;
          try {
            await api('DELETE', `/api/passkeys/${k.id}`);
          } catch (err) {
            say(note, words(err), 'error');
          }
          reload();
        });
        return el('li', { class: 'passkey', 'data-id': k.id }, el('b', {}, k.name), el('span', { class: 'muted small' },
          tr(" добавлен {0} · {1}", day(k.created), k.used ? tr("открывал {0}", day(k.used)) : tr("ещё не открывал"))), del);
      }) : [el('li', { class: 'muted small' }, tr("Ключей нет: запертую студию откроет пароль учётки"))]));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  const adding = [];
  if (isTouch()) {
    const password = el('input', { class: 'input', id: 'passkey-password', type: 'password',
      autocomplete: 'current-password', maxlength: '1024' });
    const next = button(tr("Далее"), { class: 'btn', id: 'passkey-next' });
    const ask = el('div', {}, field(tr("Пароль учётки — чтобы добавить ключ этого устройства"), password, 'passkey'),
      el('div', { class: 'row' }, next));
    const create = button(tr("Добавить Face ID / отпечаток этого устройства"), { class: 'btn btn-accent', id: 'passkey-add',
      hidden: true });
    let options = null;
    let optionsAt = 0;
    let stale = null;
    const restart = () => {
      clearTimeout(stale);
      options = null;
      create.hidden = true;
      ask.hidden = false;
    };
    // вызов на сервере живёт 120 с: позже устройство создало бы ключ, который сервер не примет (сирота в «Паролях»)
    const expire = () => {
      restart();
      say(note, tr("Прошло больше полутора минут — введите пароль ещё раз"));
    };
    next.addEventListener('click', async () => {
      next.disabled = true;
      say(note, '');
      try {
        options = await passkeyOptions(password.value);
        optionsAt = Date.now();
        clearTimeout(stale);
        stale = setTimeout(expire, FRESH_SEC * 1000);
        password.value = '';
        ask.hidden = true;
        create.hidden = false;
      } catch (err) {
        say(note, words(err), 'error');
      } finally {
        next.disabled = false;
      }
    });
    create.addEventListener('click', async () => {
      if (Date.now() - optionsAt >= FRESH_SEC * 1000) {    // таймер мог не успеть (телефон спал)
        expire();
        return;
      }
      const pending = addPasskey(options);           // create — до первого await (Safari)
      create.disabled = true;
      try {
        await pending;
        restart();
        say(note, tr("Ключ добавлен"), 'ok');
      } catch (err) {
        if (err && err.name === 'NotAllowedError') {
          say(note, tr("Устройство не подтвердило — нажмите ещё раз"), 'error');      // вызов на сервере ещё жив
        } else {
          restart();
          say(note, err && err.name === 'InvalidStateError' ? tr("Ключ этого устройства уже добавлен") : words(err), 'error');
        }
      } finally {
        create.disabled = false;
      }
      reload();
    });
    adding.push(ask, el('div', { class: 'row' }, create));
  }

  reload();
  return el('section', { class: 'block lock-block' }, el('h2', {}, tr("PIN приложения")),
    el('label', { class: 'check' }, enabled, tr("Запрашивать PIN, пароль или ключ устройства")),
    el('div', { class: 'checks' }, el('label', { class: 'check' }, all, tr("Всё приложение")),
      el('label', { class: 'check' }, some, tr("Выбранные разделы"))), selection,
    el('p', { class: 'muted small' }, tr("Защита действует на телефоне и компьютере. Одно отпирание открывает все выбранные разделы.")),
    field(tr("Запрашивать после бездействия"), timer.node, 'lock-period'),
    el('p', { class: 'muted small' }, tr("0 — при каждом возвращении в приложение. Максимум — 24 часа.")),
    passwordBox, el('div', { class: 'row' }, save), offBox,
    el('div', { class: 'row pin-row' }, pinState, pinSet, pinRemove), pinForm, list, ...adding, note);
}

/* «Хранилище»: свои файлы по видам (из базы: размер оригиналов, без миниатюр), итог и очередь удаления —
 * «ждут удаления: N (с …)» или «очередь пуста». Квот и автоудаления нет. Панель берёт отсюда же подписи. */
const KIND_NAMES = { image: tr("Картинки"), video: tr("Ролики"), audio: tr("Звук"), document: tr("Документы и расшифровки"), other: tr("Прочее") };
const UNITS = [tr("Б"), tr("КиБ"), tr("МиБ"), tr("ГиБ"), tr("ТиБ")];

export function sizeText(bytes) {
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i ? n.toLocaleString(locale(), { maximumFractionDigits: 1 }) : n} ${UNITS[i]}`;
}

export function filesText(n) {
  const tens = n % 100;
  const ones = n % 10;
  const word = tens >= 11 && tens <= 14 ? tr("файлов") : ones === 1 ? tr("файл") : ones >= 2 && ones <= 4 ? tr("файла") : tr("файлов");
  return `${n} ${word}`;
}

export function momentText(ts) {
  return dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

/** Очередь удаления словами: пуста — kind 'ok'. */
export function queueText(pending) {
  return pending.files ? [tr("Ждут удаления на хранилище: {0} (с {1}) — ", filesText(pending.files), momentText(pending.since))
    + tr("удалятся, когда оно станет доступно"), ''] : [tr("Очередь удаления пуста: удалённое стёрто и с хранилища"), 'ok'];
}

/* «Выгрузить мою историю»: окно с галочками разделов (все отмечены) → «Подготовить» (POST /api/me/export,
 * токен на час) → ссылка «Скачать: N файлов, X» на ZIP по токену. Ссылка — настоящая <a download>: её
 * нажатие — жест человека, и на iPhone загрузка уйдёт в «Файлы». Отказ — словами в окне. */
const EXPORT_SECTIONS = [...sectionPairs(['chat', 'photo', 'video', 'transcribe', 'speech']), ['voices', tr("Голоса озвучки")],
  ...sectionPairs(['ocr', 'translate']), ['code', tr("{0}: чаты", NAMES.code)], ['agents', tr("{0}: подключения", NAMES.code)],
  ['security', tr("Журнал безопасности")]];

function exportDialog(host) {
  const boxes = EXPORT_SECTIONS.map(([key, name]) => el('label', { class: 'check' },
    el('input', { type: 'checkbox', name: 'export-section', value: key, checked: true }), name));
  const fail = el('p', { class: 'error', id: 'export-error', 'aria-live': 'polite' });
  const ready = el('p', { id: 'export-ready', hidden: true });
  const prepare = button(tr("Подготовить"), { class: 'btn btn-accent', id: 'export-prepare' });
  openDialog(host, { id: 'export-dialog', title: tr("Выгрузить мою историю"), cancelText: tr("Закрыть"), actions: [prepare],
    body: [el('p', { class: 'muted small' }, tr("Один ZIP: чаты, промпты, цепочки, расшифровки и файлы, с описанием и списком сумм sha256. Ключей подключений и пароля в нём нет. Ссылка живёт час.")), ...boxes, fail, ready] });
  prepare.addEventListener('click', async () => {
    const sections = boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
    fail.textContent = '';
    ready.hidden = true;
    if (!sections.length) {
      fail.textContent = tr("Отметьте хотя бы один раздел");
      return;
    }
    prepare.disabled = true;
    try {
      const { id, files, bytes } = await api('POST', '/api/me/export', { sections });
      const count = `${files.toLocaleString(locale())} ${plural(files, [tr("файл"), tr("файла"), tr("файлов")])}`;
      ready.replaceChildren(el('a', { class: 'btn btn-accent', id: 'export-download', download: '',
        href: `/api/me/export/${encodeURIComponent(id)}` }, tr("Скачать: {0}, {1}", count, sizeText(bytes))));
      ready.hidden = false;
    } catch (err) {
      fail.textContent = words(err);
    } finally {
      prepare.disabled = false;
    }
  });
}

function storageBlock(host) {
  const list = el('dl', { class: 'storage', id: 'storage' });
  const queue = status('storage-queue');

  async function reload() {
    try {
      const { kinds, total, pending, cleaner_pending, cleaner_stalled } = await api('GET', '/api/me/storage');
      const line = (key, name, n) => [el('dt', {}, name),
        el('dd', { 'data-kind': key }, `${filesText(n.files)} · ${sizeText(n.bytes)}`)];
      list.replaceChildren(...kinds.filter((k) => k.files).flatMap((k) => line(k.kind, KIND_NAMES[k.kind] || k.kind, k)),
        ...line('total', tr("Всего"), total));
      say(queue, ...queueText(pending));
      if (cleaner_pending) queue.textContent += tr(" Контекстов Cleaner в очереди удаления: {0}.", cleaner_pending);
      for (const s of cleaner_stalled || []) queue.textContent += tr(" Отложено после неудачи ({0}): {1}.", s.base_url, s.count);
    } catch (err) {
      say(queue, words(err), 'error');
    }
  }

  reload();
  return { reload, el: el('section', { class: 'block storage-block' }, el('h2', {}, tr("Хранилище")), list, queue,
    el('p', { class: 'muted small' }, tr("Счёт — по записям истории, без миниатюр. Тексты чатов и промпты — в базе: удалённое из неё остаётся в копиях базы, пока его не вытеснят 7 новых копий (обычно неделю).")),
    el('div', { class: 'row' }, button(tr("Выгрузить мою историю"), { class: 'btn', id: 'export-open' },
      () => exportDialog(host)))) };
}

/* «Интернет» чата: ключ Exa учётки (PUT/DELETE /api/me/exa-key). Страница знает только признак `me.exa_key` —
 * ключ после сохранения из поля стирается и обратно не приходит. Ссылка — в новую вкладку без Referer. */
const EXA_KEYS = 'https://dashboard.exa.ai/api-keys';

function exaBlock() {
  const key = el('input', { class: 'input', id: 'exa-key', type: 'password', autocomplete: 'off', maxlength: '200',
    spellcheck: 'false' });
  const note = status('exa-status');
  const save = button(tr("Сохранить"), { class: 'btn', id: 'exa-save' });
  const remove = button(tr("Убрать"), { class: 'btn', id: 'exa-remove' });
  const show = () => {
    say(note, me.exa_key ? tr("Ключ задан") : tr("Ключа нет"), me.exa_key ? 'ok' : '');
    remove.disabled = !me.exa_key;
  };
  async function change(method, body) {
    save.disabled = remove.disabled = true;
    try {
      await api(method, '/api/me/exa-key', body);
      me.exa_key = method === 'PUT';
      key.value = '';
      show();
    } catch (err) {
      say(note, words(err), 'error');
      remove.disabled = !me.exa_key;
    } finally {
      save.disabled = false;
    }
  }
  save.addEventListener('click', () => change('PUT', { key: key.value }));
  remove.addEventListener('click', () => change('DELETE'));
  show();
  return el('section', { class: 'block exa-block' }, el('h2', {}, tr("Интернет")),
    el('p', { class: 'muted small' }, tr("Кнопка «Интернет» в чате — поиск и чтение страниц через Exa по вашему ключу и субтитры YouTube — включается только с ключом; убранный ключ выключает её во всех диалогах. Ключ — на "),
    el('a', { href: EXA_KEYS, target: '_blank', rel: 'noopener noreferrer' }, 'dashboard.exa.ai'),
    tr(": бесплатно $10 в месяц, без привязанной карты перерасхода нет. Поисковые запросы и адреса страниц видит Exa.")),
    field(tr("Ключ Exa"), key, 'exa-key'), el('div', { class: 'row' }, save, remove, note));
}

/* Push раздела «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.8): подписка этого устройства —
 * строка сервера у сессии (GET/POST/DELETE /api/me/push), флаг устройства localStorage `studio-push` — только
 * '1'; ключи и адрес подписки знают браузер и сервер, в localStorage их нет. Разрешение спрашивается первым
 * в обработчике нажатия (Safari без нажатия его не покажет), подписка — после него. */
const PUSH_KEY = 'studio-push';
const DENIED = tr("Браузер запретил уведомления — разрешите их в настройках сайта");
const NO_WORKER = tr("Фоновая служба студии в этом браузере не запустилась (частный режим?) — push здесь недоступен");
const PUSH_READY_SEC = 10;       // служебный работник не зарегистрирован — ready не разрешится никогда

/** Регистрация служебного работника — не дольше PUSH_READY_SEC, иначе отказ словами NO_WORKER. */
function ready() {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(NO_WORKER)), PUSH_READY_SEC * 1000);
    navigator.serviceWorker.ready.then((registration) => {
      clearTimeout(timer);
      resolve(registration);
    }, reject);
  });
}

function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function pushFlag(on) {
  try {
    if (on === undefined) return localStorage.getItem(PUSH_KEY) === '1';
    if (on) localStorage.setItem(PUSH_KEY, '1');
    else localStorage.removeItem(PUSH_KEY);
  } catch {
    // хранилище недоступно: флажок покажет состояние сервера при следующем открытии
  }
  return false;
}

function permission() {
  if (Notification.permission === 'granted') return Promise.resolve('granted');
  return new Promise((resolve) => {              // старый Safari отвечает в обратный вызов, а не обещанием
    const pending = Notification.requestPermission(resolve);
    if (pending) pending.then(resolve, () => resolve('denied'));
  });
}

/** Публичный ключ VAPID (base64url) — байтами для applicationServerKey. */
function keyBytes(text) {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function subscribePush() {
  const registration = await ready();
  const { key } = await api('GET', '/api/me/push');
  const options = { userVisibleOnly: true, applicationServerKey: keyBytes(key) };
  let sub;
  try {
    sub = await registration.pushManager.subscribe(options);
  } catch (err) {
    // подписка прежним ключом VAPID (сменился STUDIO_DATA_KEY): снять её и подписаться заново
    const old = err && err.name === 'InvalidStateError' ? await registration.pushManager.getSubscription() : null;
    if (!old) throw err;
    await old.unsubscribe();
    sub = await registration.pushManager.subscribe(options);
  }
  await api('POST', '/api/me/push', sub.toJSON());
}

async function unsubscribePush() {
  await api('DELETE', '/api/me/push');
  // сервер уже не шлёт: подписку браузера — как выйдет, флажок «вкл» её сбой не вернёт
  const registration = await ready().catch(() => null);
  const sub = registration ? await registration.pushManager.getSubscription().catch(() => null) : null;
  if (sub) await sub.unsubscribe().catch(() => false);
}

function pushBox() {
  const note = status('push-status');
  const box = el('input', { type: 'checkbox', id: 'push-enabled', disabled: true });
  box.addEventListener('change', async () => {
    say(note, '');
    const on = box.checked;
    box.disabled = true;
    try {
      if (on) {
        if (await permission() !== 'granted') {
          box.checked = false;
          say(note, DENIED, 'error');
          return;
        }
        await subscribePush();
        pushFlag(true);
        say(note, tr("Push включён на этом устройстве"), 'ok');
      } else {
        await unsubscribePush();
        pushFlag(false);
        say(note, tr("Push выключен на этом устройстве"));
      }
    } catch (err) {
      box.checked = !on;
      say(note, err && err.name === 'NotAllowedError' ? DENIED : words(err), 'error');
    } finally {
      box.disabled = false;
    }
  });
  if (pushSupported()) {
    api('GET', '/api/me/push').then(async (answer) => {
      if (!answer || typeof answer.subscribed !== 'boolean') {
        throw new Error(tr("Сервер вернул некорректные данные подписки push — обновите страницу"));
      }
      let on = pushFlag() && answer.subscribed;
      if (on) {
        // браузер мог снять подписку сам, а переподписка служебного работника (pushsubscriptionchange, sw.js) — не
        // дойти до сервера: тогда она снята и там — флажок по подписке браузера, флаг устройства прочь
        const sub = await ready().then((r) => r.pushManager.getSubscription(), () => null);
        if (!sub) {
          on = false;
          pushFlag(false);
        }
      }
      box.checked = on;
      box.disabled = false;
    }).catch((err) => say(note, words(err), 'error'));
  }
  return [el('label', { class: 'check push-check' }, box, tr("Push на этом устройстве")),
    el('p', { class: 'muted small', id: 'push-hint' }, tr("Приходят и при закрытой студии и погасшем экране: запрос разрешения, вопрос и конец хода Claude Code, Codex или OpenCode, готовность и ошибки фоновых заданий. Идут через службу браузера (Apple, Google или Mozilla) — содержимое зашифровано, но факт и время им видны. На iPhone — только у студии, добавленной на экран «Домой».")), note];
}

/* Локальные уведомления (jobs.js) — запасной вариант без подписки; Web Push — для всех фоновых заданий и «Агентов». */
function notifyBlock() {
  const note = status('notify-status');
  const supported = notifySupported();
  const box = el('input', { type: 'checkbox', id: 'notify-enabled', checked: notifyEnabled(), disabled: !supported });
  box.addEventListener('change', async () => {
    say(note, '');
    if (!box.checked) {
      await setNotify(false);
      say(note, tr("Уведомления выключены на этом устройстве"));
      return;
    }
    const on = await setNotify(true);
    box.checked = on;
    if (on) say(note, tr("Уведомления включены на этом устройстве"), 'ok');
    else say(note, tr("Браузер запретил уведомления — разрешите их в настройках сайта"), 'error');
  });
  if (!supported) {
    say(note, tr("Этот браузер не показывает уведомления сайта: на iPhone — только у студии, добавленной на экран «Домой»"));
  }
  return el('section', { class: 'block notify-block' }, el('h2', {}, tr("Уведомления")),
    el('label', { class: 'check' }, box, tr("Сообщать о готовности, когда студия открыта в фоне")), note,
    el('p', { class: 'muted small' }, tr("Только на этом устройстве и пока студия открыта — свёрнутая вкладка или окно. При включённом push готовность сообщает сервер, без повторного локального уведомления. Промптов, текстов и имён файлов в уведомлении нет.")),
    ...pushBox());
}

/* «Компьютеры (Claude Code)»: установщик находится наверху корня настроек (ui.js agentDownload).
 * Мосты учётки (GET /api/agents) — имя, «на связи», значок «PIN»; «Отозвать» — окно и
 * DELETE /api/agents/<id> (мост на связи отключается). История подключений (GET /api/agents/events) — только
 * чтение, новые сверху страницами сервера, «Показать ещё» — по `next`. */
const AGENT_EVENTS = { enroll: tr("привязан"), revoke: tr("отозван"), online: tr("на связи"), offline: tr("связь потеряна"),
  open: tr("открыт"), pin_ok: tr("открыт PIN"), pin_fail: tr("неверный PIN"), session_start: tr("новый чат"),
  member_add: tr("доступ к проекту выдан"), member_remove: tr("доступ к проекту отозван") };

function agentsBlock(host, agents = () => api('GET', '/api/agents')) {
  const list = el('ul', { class: 'agents', id: 'agents' });
  const events = el('ul', { class: 'agent-events', id: 'agent-events' });
  const note = status('agents-status');
  let next = null;
  const more = button(tr("Показать ещё"), { class: 'btn btn-sm', id: 'agent-events-more', hidden: true },
    () => loadEvents(next));

  function revoke(agent) {
    const yes = button(tr("Отозвать"), { class: 'btn btn-danger', id: 'agent-revoke-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'agent-revoke-dialog', title: tr("Отозвать компьютер?"), actions: [yes], body: [
      el('p', {}, tr("Мост на нём отключится и не войдёт снова без нового входа. История подключений останется.")),
      el('p', { class: 'muted' }, `«${agent.name}»`), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/agents/${agent.id}`);
        dialog.close();
        reload();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  async function loadAgents() {
    try {
      const { items } = await agents();
      list.replaceChildren(...(items.length ? items.map((a) => el('li', { class: 'agent', 'data-id': String(a.id) },
        el('b', { class: 'agent-name' }, a.name),
        el('span', { class: `tag agent-state${a.online ? ' server' : ''}` }, a.online ? tr("на связи") : tr("не на связи")),
        a.has_pin ? el('span', { class: 'tag agent-pin' }, 'PIN') : null,
        button(tr("Отозвать"), { class: 'btn btn-sm btn-danger agent-revoke' }, () => revoke(a))))
        : [el('li', { class: 'muted small' }, tr("Компьютеров пока нет."))]));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  async function loadEvents(before) {
    more.disabled = true;
    try {
      const page = await api('GET', before ? `/api/agents/events?before=${before}` : '/api/agents/events');
      const rows = page.items.map((e) => el('li', {}, [momentText(e.created), e.agent_name, AGENT_EVENTS[e.kind] || e.kind,
        e.device, e.ip].filter(Boolean).join(' · ')));
      if (before) events.append(...rows);
      else events.replaceChildren(...rows);
      next = page.next;
      more.hidden = next === null;
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      more.disabled = false;
    }
  }

  function reload() {
    say(note, '');
    loadAgents();
    loadEvents(null);
  }

  reload();
  return el('section', { class: 'block agents-block' }, el('h2', {}, tr("Компьютеры (Claude Code, Codex и OpenCode)")),
    list, note,
    el('h3', {}, tr("История подключений")), events, el('div', { class: 'row' }, more));
}

/** Что уносит «Удалить всю историю» (history._delete_all): записи разделов с историей — имена из реестра разделов —
 * с их файлами, образцы голосов, ссылки «Поделиться» и рабочее место «Агентов». */
function wipeWhat() {
  const sections = RAIL_SECTIONS.filter((s) => s.id !== 'code').map((s) => `«${s.name}»`).join(', ');
  return tr("Удалятся безвозвратно: вся история разделов {0} вместе с файлами, образцы голосов, все ссылки ", sections)
    + tr("«Поделиться», а в разделе «{0}» — вкладки, отметки чатов и непрочитанные завершения. Сами чаты Claude Code, ", NAMES.code)
    + tr("Codex и OpenCode хранятся на компьютерах и останутся.");
}

function wipeBlock(host, onWiped) {
  const note = status('wipe-status');
  const start = button(tr("Удалить всю историю"), { class: 'btn btn-danger', id: 'history-wipe' }, () => {
    say(note, '');
    const text = el('p', {}, wipeWhat());
    const kept = el('p', { class: 'muted small' }, tr("Подключения, пресеты и ключи останутся, как и настройки, компьютеры, выданные доступы к проектам и журнал безопасности."));
    const fail = el('p', { class: 'error' });
    const next = button(tr("Удалить"), { class: 'btn btn-danger', id: 'wipe-next' });
    const confirm = button(tr("Да, удалить навсегда"), { class: 'btn btn-danger solid', id: 'wipe-confirm', hidden: true });
    const dialog = openDialog(host, { id: 'wipe-dialog', title: tr("Удалить всю историю?"), body: [text, kept, fail],
      actions: [next, confirm] });
    next.addEventListener('click', () => {
      text.textContent = tr("Точно? Отменить удаление нельзя.");
      next.hidden = true;
      confirm.hidden = false;
      confirm.focus();
    });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        const { pending_files: left, cleaner_pending } = await api('DELETE', '/api/history');
        dialog.close();
        // хранилище было недоступно: файлы ждут в очереди удаления — сказать как есть
        say(note, left ? tr("История удалена, но файлов на хранилище ещё {0}: удалятся, когда оно станет доступно", left)
          : tr("История удалена"), 'ok');
        if (cleaner_pending) note.textContent += tr(" Контекстов Cleaner в очереди удаления: {0}.", cleaner_pending);
        onWiped();
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
      }
    });
  });
  return el('section', { class: 'block' }, el('h2', {}, tr("История")),
    el('p', { class: 'muted small' }, tr("История всех разделов с файлами, образцы голосов и ссылки «Поделиться» этого профиля. Подключения, пресеты и ключи не удаляются.")),
    el('div', { class: 'row' }, start, note));
}

/* ---- голоса озвучки ------------------------------------------------------------------ */

function clock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Проба голоса своей короткой фразой: POST /api/voices/<id>/probe — звук в ответе, в историю не идёт,
 * карта занята — словами сразу. Blob плеера освобождается при закрытии окна. */
function probeVoice(host, voice) {
  const phrase = el('input', { id: 'voice-probe-text', maxlength: '200', value: 'Привет! Так звучит этот голос.',
    autocomplete: 'off' });
  const fail = el('p', { class: 'error', 'aria-live': 'polite' });
  const player = el('audio', { id: 'voice-probe-audio', controls: true, hidden: true });
  const go = button(tr("Послушать"), { class: 'btn btn-accent', id: 'voice-probe-go' });
  const dialog = openDialog(host, { id: 'voice-probe-dialog', title: tr("Проба: {0}", voice.name), cancelText: tr("Закрыть"),
    body: [field(tr("Фраза (до 200 знаков)"), phrase, 'text'), player, fail], actions: [go] });
  let url = null;
  dialog.addEventListener('close', () => {
    player.pause();
    if (url) URL.revokeObjectURL(url);
  });
  go.addEventListener('click', async () => {
    go.disabled = true;
    fail.textContent = '';
    try {
      const response = await api('POST', `/api/voices/${encodeURIComponent(voice.id)}/probe`, { text: phrase.value });
      const blob = await response.blob();
      if (!dialog.open) return;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(blob);
      player.src = url;
      player.hidden = false;
      player.play().catch(() => {});
    } catch (err) {
      fail.textContent = words(err);
    } finally {
      go.disabled = false;
    }
  });
}

function voicesBlock(host) {
  const list = el('ul', { class: 'voices' });
  const note = status('voice-status');

  async function reload() {
    try {
      const { items } = await api('GET', '/api/voices');
      list.replaceChildren(...(items.length ? items.map((v) => {
        const del = button(tr("Удалить"), { class: 'btn btn-sm btn-danger voice-delete' }, async () => {
          del.disabled = true;
          try {
            await api('DELETE', `/api/voices/${v.id}`);
          } catch (err) {
            say(note, words(err), 'error');
          }
          reload();
        });
        const tryIt = button(tr("Проба"), { class: 'btn btn-sm voice-probe' }, () => probeVoice(host, v));
        return el('li', { class: 'voice', 'data-id': v.id }, el('b', {}, v.name),
          el('span', { class: 'muted small' }, tr(" образец {0}", clock(v.duration))), tryIt, del);
      }) : [el('li', { class: 'muted small' }, tr("Голосов пока нет: озвучка шлюза srvllm говорит голосом по образцу"))]));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  function add() {
    const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off' });
    const text = el('textarea', { class: 'input', name: 'text', rows: '3', maxlength: '500',
      placeholder: tr("Определится автоматически после загрузки или записи") });
    const file = el('input', { type: 'file', name: 'audio', accept: 'audio/*,.m4a,.mp3,.wav,.ogg,.webm' });
    const rec = button(tr("Записать с микрофона"), { class: 'btn btn-sm', id: 'voice-record' });
    const recNote = el('p', { class: 'muted small', 'aria-live': 'polite' });
    const recognizeNote = el('p', { class: 'muted small', 'aria-live': 'polite', id: 'voice-asr-status' });
    const retry = button(tr("Распознать ещё раз"), { class: 'btn btn-sm', id: 'voice-transcribe', hidden: true });
    const fail = el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' });
    const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'voice-save' });
    let recorded = null;
    let recording = null;
    let recognizing = null;
    const dialog = openDialog(host, { id: 'voice-dialog', title: tr("Новый голос"), actions: [save], body: [
      field(tr("Имя голоса"), name, 'name'), field(tr("Текст образца (можно поправить)"), text, 'text'),
      el('p', { class: 'muted small' }, tr("Запись 5–15 секунд чистой речи: файлом или с микрофона.")),
      el('div', { class: 'row' }, file, rec), recNote, recognizeNote, retry, fail] });
    dialog.addEventListener('close', () => { recognizing?.abort(); });

    async function transcribe(sample) {
      recognizing?.abort();
      const controller = new AbortController();
      recognizing = controller;
      text.value = '';
      fail.textContent = '';
      recognizeNote.textContent = tr("Распознаю текст образца…");
      text.disabled = save.disabled = rec.disabled = true;
      text.setAttribute('aria-busy', 'true');
      retry.hidden = true;
      const body = new FormData();
      body.append('audio', sample, sample.name || recordingName(sample, 'sample'));
      try {
        const result = await api('POST', '/api/voices/transcribe', body, { signal: controller.signal });
        if (recognizing !== controller || !dialog.open || !dialog.isConnected) return;
        text.value = result.text;
        recognizeNote.textContent = tr("Текст распознан. Проверьте его перед сохранением.");
      } catch (err) {
        if (controller.signal.aborted || recognizing !== controller || !dialog.open || !dialog.isConnected) return;
        recognizeNote.textContent = tr("Распознавание не удалось. Повторите или введите текст образца вручную.");
        fail.textContent = words(err);
      } finally {
        if (recognizing === controller) {
          recognizing = null;
          text.disabled = save.disabled = rec.disabled = false;
          text.setAttribute('aria-busy', 'false');
          retry.hidden = false;
        }
      }
    }
    file.addEventListener('change', () => {
      const sample = file.files[0];
      if (!sample) return;
      recorded = null;
      recNote.textContent = '';
      transcribe(sample);
    });
    retry.addEventListener('click', () => {
      const sample = file.files[0] || recorded;
      if (sample) transcribe(sample);
    });
    rec.addEventListener('click', async () => {
      if (recording) {
        recording.stop();
        return;
      }
      file.disabled = save.disabled = rec.disabled = true;
      retry.hidden = true;
      try {
        recording = await startRecording(15, (sec) => { recNote.textContent = tr("Запись · {0} — нажмите ещё раз, чтобы остановить", clock(sec)); }, rec);
      } catch (err) {
        recNote.textContent = words(err);
        file.disabled = save.disabled = rec.disabled = false;
        return;
      }
      rec.disabled = false;
      if (!recording) {                              // окно закрыли, пока браузер спрашивал разрешение
        file.disabled = save.disabled = false;
        return;
      }
      rec.textContent = tr("Остановить");
      const sample = await recording.done;
      recording = null;
      file.disabled = save.disabled = false;
      if (!sample) {                                 // отменена (окно закрыли, ушли из раздела)
        rec.textContent = recorded ? tr("Записать заново") : tr("Записать с микрофона");
        recNote.textContent = '';
        return;
      }
      recorded = sample;
      rec.textContent = tr("Записать заново");
      recNote.textContent = tr("Запись готова");
      file.value = '';
      transcribe(sample);
    });
    save.addEventListener('click', async () => {
      const sample = file.files[0] || recorded;
      if (!sample) {
        fail.textContent = tr("Нужна запись образца: файл или с микрофона");
        return;
      }
      save.disabled = file.disabled = rec.disabled = retry.disabled = text.disabled = true;
      const body = new FormData();
      body.append('name', name.value.trim());
      body.append('text', text.value.trim());
      body.append('audio', sample, sample.name || recordingName(sample, 'sample'));
      try {
        await api('POST', '/api/voices', body);
        dialog.close();
        reload();
      } catch (err) {
        showError(dialog, err, { name: 'name', text: 'text' }, 'form');
      } finally {
        save.disabled = file.disabled = rec.disabled = retry.disabled = text.disabled = false;
      }
    });
    name.focus();
  }

  reload();
  return el('section', { class: 'block voices-block' }, el('div', { class: 'block-head' }, el('h2', {}, tr("Голоса")),
    button(tr("+ Голос"), { class: 'btn', id: 'voice-add' }, add)), list, note);
}

/* ---- разделы ---------------------------------------------------------------------- */

/** Разделы — в порядке рельса; «Учётка» — личные группы. Прежние адреса — на новые места. */
const RAIL_ORDER = RAIL_SECTIONS.map((s) => s.id);                 // порядок рельса — из реестра разделов
const ACCOUNT_IDS = ['appearance', 'security', 'notifications', 'data', 'input'];
const OLD_IDS = { other: 'security', dictation: 'input' };
const PERSONAL = new Set(['code', 'appearance', 'security', 'notifications', 'data']);   // ответ разделов их не перерисовывает

export function settingsView(host, { sections: base, connections: connBase, section = null, hash = false,
  other = false, direct = false, voices = false, onChange = null }) {
  const names = { ...SECTION_NAMES, cleaner: tr("Очистка данных · Cleaner"), code: NAMES.code, appearance: tr("Оформление"),
    security: tr("Защита"), notifications: tr("Уведомления"), data: tr("Данные"), input: other ? tr("Ввод и интернет") : tr("Голосовой ввод") };
  // панель администратора (other = false): разделы без «Агентов» и Cleaner, из «Учётки» — только голосовой ввод
  const sectionIds = [...RAIL_ORDER.filter((id) => other || id !== 'code'), ...(other ? ['cleaner'] : [])];
  const accountIds = other ? ACCOUNT_IDS : ['input'];
  const ids = [...sectionIds, ...accountIds];
  const nav = el('nav', { class: 'settings-nav', 'aria-label': tr("Разделы настроек") });
  const panel = el('div', { class: 'settings-panel' });
  const download = other ? agentDownload() : null;
  const root = el('div', { class: 'settings' }, ...(download ? [download] : []), nav, panel);
  const note = el('p', { class: 'status section-status', 'aria-live': 'polite' });
  const found = new Map();        // `<раздел>:<подключение>` → найденные модели после «Обновить модели»
  const checkErrors = new Map();  // подключение → отказ последней проверки (в блоке паспорта)
  const checking = new Set();     // подключения, которые проверяются сейчас
  const opened = new Set();       // `<раздел>:<подключение>` — паспорт раскрыт
  let data = [];
  let dictationHost = null;
  const start = moved(section);
  let current = ids.includes(start) ? start : null;

  /** Прежний адрес («Прочее», голосовой ввод) — на новое место; в адресной строке — заменой, без новой записи. */
  function moved(id) {
    if (!(id in OLD_IDS)) return id;
    if (hash) history.replaceState(history.state, '', `#/settings/${OLD_IDS[id]}`);
    return OLD_IDS[id];
  }

  for (const [title, list] of [[tr("Разделы"), sectionIds], [tr("Учётка"), accountIds]]) {
    nav.append(el('h2', { class: 'settings-group' }, title));
    for (const id of list) {
      const link = el('a', { href: `#/settings/${id}`, 'data-section': id }, names[id]);
      if (!hash) {
        link.addEventListener('click', (event) => {
          event.preventDefault();
          show(id);
        });
      }
      nav.append(link);
    }
  }

  if (other && me?.studio_version) nav.append(el('p', { class: 'muted small', id: 'studio-version' }, `Studio ${me.studio_version}`));

  async function changed() {
    await reload();
    if (onChange) await onChange();
  }

  function sectionOf(id) {
    return data.find((s) => s.id === id);
  }

  async function saveModels(s, conn, names, box) {
    const checked = names.filter((m) => box.querySelector(`input.model-check[value="${CSS.escape(m)}"]`).checked);
    try {
      await api('PUT', `${base}/${s.id}/links/${conn.id}`, { models: checked });
      say(note, '');
    } catch (err) {
      say(note, words(err), 'error');
    }
    await changed();
  }

  async function saveDefault(s, conn, model) {
    try {
      await api('PUT', `${base}/${s.id}/default`, { connection_id: conn.id, model });
      say(note, '');
    } catch (err) {
      say(note, words(err), 'error');
    }
    await changed();
  }

  async function refresh(s, conn) {
    say(note, tr("Загружаю список моделей…"));
    try {
      // прямое подключение: список берёт браузер; через сервер, убранное из списка (и в панели) — GET …/found
      const items = direct && !conn.proxied && !conn.dropped ? await modelsOf(conn)
        : (await api('GET', `${base}/${s.id}/links/${conn.id}/found`)).items;
      found.set(`${s.id}:${conn.id}`, items);
      say(note, items.length ? tr("Найдено моделей: {0}", items.length) : tr("Сервис не назвал ни одной модели"));
    } catch (err) {
      say(note, words(err), 'error');
    }
    render();
  }

  async function runCheck(s, conn) {
    checking.add(conn.id);
    checkErrors.delete(conn.id);
    render();
    try {
      const answer = await check(conn, !conn.proxied && !conn.dropped);
      for (const [section, items] of Object.entries(answer.found)) found.set(`${section}:${conn.id}`, items);
      opened.add(`${s.id}:${conn.id}`);
    } catch (err) {
      checkErrors.set(conn.id, words(err));
    }
    checking.delete(conn.id);
    await reload();                  // паспорт — в подключении
  }

  function tryModel(s, conn, model) {
    const send = button(tr("Отправить"), { class: 'btn btn-accent', id: 'probe-send' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'probe-dialog', title: tr("Пробный ответ"), actions: [send], body: [
      el('p', {}, tr("Модель «{0}» подключения «{1}».", modelLabel(model), conn.name)), el('p', {}, PROBE_WARNING), fail] });
    send.addEventListener('click', async () => {
      send.disabled = true;
      try {
        await probe(conn, model, !conn.proxied && !conn.dropped);
        dialog.close();
        opened.add(`${s.id}:${conn.id}`);
        await reload();
      } catch (err) {
        fail.textContent = words(err);
        send.disabled = false;
      }
    });
  }

  function remove(conn) {
    const yes = button(tr("Удалить"), { class: 'btn btn-danger', id: 'conn-delete-confirm' });
    const fail = el('p', { class: 'error' });
    const used = sectionsText(conn.sections);
    const dialog = openDialog(host, { id: 'conn-delete-dialog', title: tr("Удалить подключение?"), actions: [yes], body: [
      el('p', {}, `«${conn.name}» — ${conn.base_url}`),
      el('p', { class: 'muted' }, used ? tr("Подключение участвует в разделах: {0}. Оно уйдёт из всех.", used) : tr("Подключение не участвует ни в одном разделе.")),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `${connBase}/${conn.id}`);
        dialog.close();
        await changed();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function linkCard(s, link) {
    const conn = link.connection;
    const known = found.get(`${s.id}:${conn.id}`);
    const offer = known || link.catalog || [];
    const names = [...link.models, ...offer.filter((m) => !link.models.includes(m))];
    const box = el('ul', { class: 'models' });
    for (const m of names) {
      const checked = link.models.includes(m);
      const check = el('input', { type: 'checkbox', class: 'model-check', value: m, checked });
      const radio = el('input', { type: 'radio', class: 'model-default', name: `default-${s.id}`, value: m,
        'data-conn': String(conn.id), 'aria-label': tr("По умолчанию: {0}", modelLabel(m)), disabled: !checked,
        checked: !!s.default && s.default.connection_id === conn.id && s.default.model === m });
      check.addEventListener('change', () => saveModels(s, conn, names, box));
      radio.addEventListener('change', () => saveDefault(s, conn, m));
      // Cleaner отправляет пробу через предпросмотр чата; srvllm занял бы карту.
      const tryable = direct && checked && s.id === 'chat' && conn.kind !== 'srvllm' && !conn.cleaner;
      box.append(el('li', { class: 'model', 'data-model': m },
        el('label', { class: 'check model-name' }, check, modelLabel(m)),
        known && checked && !known.includes(m) ? el('span', { class: 'tag model-missing' }, tr("не найдена")) : null,
        tryable ? button(tr("Пробный ответ"), { class: 'btn btn-sm model-probe' }, () => tryModel(s, conn, m)) : null,
        el('label', { class: 'model-default-label', title: tr("По умолчанию") }, radio, el('span', { class: 'small muted' }, tr("по умолчанию")))));
    }
    let search = null;
    if (names.length > SEARCH_FROM) {
      search = el('input', { class: 'input model-search', type: 'search', placeholder: tr("Поиск модели"), 'aria-label': tr("Поиск модели") });
      search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        for (const li of box.children) li.hidden = !!q && !li.dataset.model.toLowerCase().includes(q);
      });
    }
    const actions = el('div', { class: 'link-actions', hidden: true },
      button(tr("Изменить"), { class: 'btn btn-sm link-edit' }, () => editConnection(host, connBase, conn, changed)),
      button(tr("Убрать из раздела"), { class: 'btn btn-sm link-remove' }, async () => {
        try {
          await api('DELETE', `${base}/${s.id}/links/${conn.id}`);
          say(note, '');
        } catch (err) {
          say(note, words(err), 'error');
        }
        await changed();
      }),
      button(tr("Удалить подключение"), { class: 'btn btn-sm btn-danger link-delete' }, () => remove(conn)));
    const menu = button(icon('more'), { class: 'btn btn-sm link-menu', 'aria-label': tr("Ещё"), title: tr("Ещё"),
      'aria-expanded': 'false' }, () => {
      actions.hidden = !actions.hidden;
      menu.setAttribute('aria-expanded', String(!actions.hidden));
    });
    const key = `${s.id}:${conn.id}`;
    const busy = checking.has(conn.id);
    const passport = direct && (conn.passport || busy || checkErrors.has(conn.id))
      ? passportBlock({ conn, section: s.id, models: link.catalog || link.models, label: modelLabel,
        error: checkErrors.get(conn.id), busy, open: opened.has(key),
        onToggle: (open) => (open ? opened.add(key) : opened.delete(key)) }) : null;
    const checkButton = direct ? button(tr("Проверить"), { class: 'btn btn-sm link-check', disabled: busy },
      () => runCheck(s, conn)) : null;
    return el('li', { class: 'link', 'data-conn': String(conn.id) },
      el('div', { class: 'link-head' },
        el('div', { class: 'link-main' },
          el('b', { class: 'link-name' }, conn.name),
          el('span', { class: 'link-url' }, conn.base_url),
          el('span', { class: 'link-hint' },
            conn.cleaner ? el('span', { class: 'tag' }, tr("Cleaner · очистка данных")) : null,
            el('span', { class: `tag${conn.proxied ? ' server' : ''}` }, conn.proxied ? tr("через сервер") : tr("из браузера")),
            tr(" ключ {0}", conn.token_hint))),
        el('div', { class: 'link-buttons' }, checkButton,
          button(tr("Обновить модели"), { class: 'btn btn-sm link-refresh' }, () => refresh(s, conn)), menu)),
      passport, actions, search,
      names.length ? box : el('p', { class: 'muted small' }, tr("Нажмите «Обновить модели» и отметьте нужные")));
  }

  async function addLink(s, cleaner = false) {
    let conns = [];
    try {
      conns = (await api('GET', connBase)).items.filter((c) => !c.sections.includes(s.id) && (!cleaner || c.cleaner));
    } catch (err) {
      say(note, words(err), 'error');
      return;
    }
    const select = el('select', { class: 'input', name: 'existing' },
      ...conns.map((c) => el('option', { value: String(c.id) }, `${c.name} — ${c.base_url}`)),
      el('option', { value: 'new' }, tr("Новый адрес…")));
    select.value = conns.length ? String(conns[0].id) : 'new';
    const form = connectionForm(null, { cleaner });
    form.el.hidden = select.value !== 'new';
    select.addEventListener('change', () => { form.el.hidden = select.value !== 'new'; });
    const box = el('div', {}, field(tr("Подключение"), select, 'existing'), form.el);
    const save = button(tr("Добавить"), { class: 'btn btn-accent', id: 'link-save' });
    const dialog = openDialog(host, { id: 'link-dialog', title: tr("Адрес для раздела «{0}»", names[s.id]), body: [box], actions: [save] });
    save.addEventListener('click', async () => {
      if (save.disabled) return;
      save.disabled = true;
      try {
        if (select.value === 'new') await api('POST', connBase, { ...form.value(), sections: [s.id] });
        else await api('POST', `${base}/${s.id}/links`, { connection_id: Number(select.value) });
        dialog.close();
        await changed();
      } catch (err) {
        if (select.value === 'new') form.showError(err);
        else showError(box, err, {}, 'existing');
      } finally {
        save.disabled = false;
      }
    });
  }

  function sectionBlock(s, cleaner = false) {
    if (cleaner) s = { ...s, links: s.links.filter((l) => l.connection.cleaner) };
    const list = el('ul', { class: 'links' }, ...(s.links.length ? s.links.map((l) => linkCard(s, l))
      : [el('li', { class: 'muted' }, tr("В разделе пока нет подключений"))]));
    return el('section', { class: 'block section-panel', 'data-section': s.id },
      el('h2', {}, names[s.id]), list,
      el('div', { class: 'row' }, button(cleaner ? tr("+ Подключить Cleaner") : tr("+ Адрес для раздела"),
        { class: 'btn', id: cleaner ? `cleaner-add-${s.id}` : 'link-add' }, () => addLink(s, cleaner))),
      cleaner ? el('a', { class: 'small', href: `#/settings/${s.id}` }, tr("Все модели раздела")) : note);
  }

  function renderDictation() {
    const s = sectionOf('dictation');
    dictationHost.replaceChildren(s ? sectionBlock(s) : note);
  }

  function render() {
    const shown = current || ids[0];
    root.dataset.view = current ? 'panel' : 'nav';
    if (download) download.hidden = !!current;
    for (const a of nav.children) {
      if (a.dataset.section === shown) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    if (shown === 'input' && dictationHost) {
      renderDictation();
      return;
    }
    dictationHost = null;
    const back = hash ? el('a', { class: 'settings-back', href: '#/settings' }, tr("‹ Настройки"))
      : button(tr("‹ Разделы"), { class: 'btn-link settings-back' }, () => show(null));
    const group = (id, ...blocks) => panel.replaceChildren(back,
      el('div', { class: 'section-panel', 'data-section': id }, ...blocks));
    if (shown === 'cleaner') {
      panel.replaceChildren(back, el('div', { class: 'section-panel', 'data-section': 'cleaner' },
        el('section', { class: 'block' }, el('h2', {}, tr("Очистка данных · Cleaner")),
          el('p', {}, tr("Личные подключения и модели по умолчанию для чата и перевода.")),
          el('details', { class: 'small' }, el('summary', {}, tr("Как подключить и использовать")),
          el('p', { class: 'muted' }, tr("В Cleaner откройте «Ключи» и создайте ключ с доступом к облачному подключению и его моделям. У каждого пользователя должна быть своя учётная запись Cleaner. Адрес сервиса разрешает администратор Studio.")),
          el('p', { class: 'muted' }, tr("«Проверить» проверяет связь и доступ к моделям. Перед отправкой из чата вы увидите очищенный текст; в переводе очистка выполняется автоматически. Настройки по умолчанию действуют для новых диалогов и переводов. Адрес и ключ можно изменить через меню «⋯» подключения.")))),
        ...['chat', 'translate'].map((id) => sectionOf(id)).filter(Boolean).map((s) => sectionBlock(s, true)), note));
      return;
    }
    if (shown === 'code') {
      let pendingAgents = null;
      const agents = () => pendingAgents ||= api('GET', '/api/agents').finally(() => { pendingAgents = null; });
      panel.replaceChildren(back, el('div', { class: 'section-panel', 'data-section': 'code' },
        codeDefaultsBlock(agents), quickActionsBlock(), computerPinBlock(), agentsBlock(host, agents)));
      return;
    }
    if (shown === 'input') {
      dictationHost = el('div', { class: 'dictation-settings' });
      renderDictation();
      group('input', dictationHost, ...(other ? [exaBlock()] : []));
      return;
    }
    if (shown === 'appearance') {
      group('appearance', accountLookBlock(), deviceLookBlock(), mobileNavBlock(), timezoneBlock(() => {}));
      return;
    }
    if (shown === 'security') {
      group('security', emailBlock(), passwordBlock(), lockBlock(), sessionsBlock(host), accessBlock(host), emergencyBlock(host),
        securityEventsBlock());
      return;
    }
    if (shown === 'notifications') {
      group('notifications', notifyBlock(), ntfyBlock());
      return;
    }
    if (shown === 'data') {
      const storage = storageBlock(host);
      group('data', storage.el, wipeBlock(host, storage.reload));
      return;
    }
    const s = sectionOf(shown);
    if (!s) {
      panel.replaceChildren(back, note);
      return;
    }
    panel.replaceChildren(back, sectionBlock(s), ...(voices && s.id === 'speech' ? [voicesBlock(host)] : []));
  }

  function show(id) {
    id = moved(id);
    const next = ids.includes(id) ? id : null;
    if (next !== current) say(note, '');
    current = next;
    render();
  }

  async function reload() {
    try {
      data = (await api('GET', base)).sections;
    } catch (err) {
      say(note, words(err), 'error');
    }
    // Ответ разделов обновляет только голосовой ввод: личные блоки и «Агенты» сохраняют поля и свои ответы.
    if (dictationHost) renderDictation();
    else if (!PERSONAL.has(current || ids[0])) render();
  }

  render();
  return { el: root, show, reload };
}

/* ---- маршрут ---------------------------------------------------------------------- */

let screen = null;

export function mount(view, ctx) {
  screen = settingsView(view, { sections: '/api/sections', connections: '/api/connections', section: ctx.id,
    hash: true, other: true, direct: true, voices: true, onChange: () => loadMe().catch(() => null) });
  view.append(el('header', { class: 'head' }, el('h1', {}, tr("Настройки"))), el('div', { class: 'page wide' }, screen.el));
  screen.reload();
}

export function update(ctx) {
  if (screen) screen.show(ctx.id);
}

export function unmount() {
  screen = null;
}
