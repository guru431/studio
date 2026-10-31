/* Раздел «Настройки» (#/settings, #/settings/<раздел>) — вариант А спеки 2026-10-20 (раздел 6.2).
 *
 * Слева — разделы, «Агенты» (умолчания чатов и компьютеры) и «Прочее» (голосовой ввод, пароль,
 * блокировка, уведомления и push, ключ Exa, хранилище и история), справа — выбранный раздел: его
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
 * В «Озвучке» своих настроек — ещё «Голоса» (образцы для озвучки srvllm): запись 5–15 с файлом или
 * с микрофона, её текст и имя (POST /api/voices), список с «Удалить».
 *
 * settingsView — и для панели администратора (базы API — /api/admin/users/<id>/…, разделы
 * переключаются на месте, в «Прочем» — только голосовой ввод; личных настроек, голосов и прямого списка моделей нет). На телефоне без раздела
 * в адресе — список разделов, с разделом — его экран со ссылкой «‹ Настройки». */
import { api, loadMe, me } from './api.js';
import { ACCESS, ENGINE_NAMES, accessLabel, effortLabel } from './code-options.js';
import { connectionForm, editConnection, SECTION_NAMES, sectionsText } from './connection-form.js';
import { notifyEnabled, notifySupported, setNotify } from './jobs.js';
import { addPasskey, FRESH_SEC, isTouch, passkeyOptions } from './lock.js';
import { recordingName, startRecording } from './mic.js';
import { check, passportBlock, probe, PROBE_WARNING } from './passport.js';
import { agentDownload, button, el, field, openDialog, plural, showError, words } from './ui.js';

export const CORS = 'Сервис не разрешает запросы из браузера (CORS) — подключите его через сервер';
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
    if (err.status === 403 || err.status === 404) throw new Error('Токен этого подключения браузеру не выдаётся');
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
  if (response.status === 401 || response.status === 403) throw new Error(`Сервис не принял токен (${response.status})`);
  if (!response.ok) throw new Error(`Сервис не отдал список моделей (${response.status})`);
  let data;
  try {
    data = (await response.json()).data;
  } catch {
    throw new Error('Сервис ответил не списком моделей');
  }
  return (Array.isArray(data) ? data : []).map((m) => m && m.id).filter((id) => typeof id === 'string');
}

/** Имена моделей своего подключения: через сервер — GET /api/connections/<id>/models, прямое — браузер. */
export async function modelsOf(conn) {
  if (conn.proxied) return (await api('GET', `/api/connections/${conn.id}/models`)).items.map((m) => m.id);
  return directModels(conn);
}

/* ---- «Прочее»: пароль и история -------------------------------------------------- */

function say(node, text, kind) {
  node.textContent = text;
  node.classList.toggle('error', kind === 'error');
  node.classList.toggle('ok', kind === 'ok');
}

function status(id) {
  return el('p', { class: 'status', id, 'aria-live': 'polite' });
}

function codeDefaultsBlock() {
  const note = status('code-defaults-status');
  const rows = el('div', { id: 'code-defaults-rows' });
  const block = el('section', { class: 'block code-defaults-block' }, el('h2', {}, 'Claude Code, Codex и OpenCode'),
    el('p', { class: 'muted small' }, 'Модель, усилие и права для новых чатов на всех ваших устройствах. '
      + 'Начатые чаты сохраняют свой выбор. Показаны текущие настройки подключённого компьютера; в проекте они уточняются при открытии чата.'),
    rows, note);

  async function load() {
    try {
      const [{ items }, profile] = await Promise.all([api('GET', '/api/agents'), loadMe()]);
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
        if (saved.model && !models.has(saved.model)) models.set(saved.model, `${saved.model} (сейчас недоступна)`);
        if (saved.effort) efforts.add(saved.effort);
        const prefix = `code-defaults-${engine}`;
        const model = el('select', { class: 'input', id: `${prefix}-model` },
          el('option', { value: '' }, native.model || (catalogs.length ? 'Не получено' : 'Компьютер не на связи')),
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
            field('Модель', model, `${prefix}-model`), field('Усилие', effort, `${prefix}-effort`),
            field('Права доступа', access, `${prefix}-access`))) };
      });
      const save = button('Сохранить', { class: 'btn', id: 'code-defaults-save' }, async () => {
        save.disabled = true;
        try {
          const code_defaults = Object.fromEntries(forms.map(({ engine, model, effort, access }) => [engine,
            { model: model.value || null, effort: effort.value || null, access: access.value || null }]));
          const answer = await api('PATCH', '/api/me', { code_defaults });
          me.code_defaults = answer.code_defaults;
          say(note, 'Умолчания сохранены — применятся к новым чатам', 'ok');
        } catch (err) {
          say(note, words(err), 'error');
        } finally {
          save.disabled = false;
        }
      });
      rows.replaceChildren(...forms.map((f) => f.node),
        ...(!items.some((a) => a.online) ? [el('p', { class: 'muted small' }, 'Подключите компьютер, чтобы выбрать его модели и усилия.')] : []),
        el('div', { class: 'row' }, save));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }
  load();
  return block;
}

function passwordBlock() {
  const input = (id, autocomplete) => el('input', { class: 'input', id, type: 'password', autocomplete, maxlength: '1024' });
  const current = input('pw-current', 'current-password');
  const fresh = input('pw-new', 'new-password');
  const repeat = input('pw-repeat', 'new-password');
  const note = status('password-status');
  const save = button('Сменить пароль', { class: 'btn', id: 'password-save' });
  save.addEventListener('click', async () => {
    if (fresh.value !== repeat.value) {
      say(note, 'Пароли не совпадают', 'error');
      return;
    }
    save.disabled = true;
    try {
      await api('POST', '/api/me/password', { current: current.value, new: fresh.value });
      current.value = fresh.value = repeat.value = '';
      say(note, 'Пароль сменён, другие сессии закрыты', 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  return el('section', { class: 'block' }, el('h2', {}, 'Пароль'), el('div', { class: 'grid3' },
    field('Текущий', current, 'current'), field('Новый', fresh, 'new'), field('Повтор нового', repeat, 'repeat')),
  el('div', { class: 'row' }, save, note));
}

function day(ts) {
  return new Date(ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

/* «Блокировка»: переключатель (выключить — только с паролем учётки), PIN учётки, ключи учётки с «Удалить» и — на
 * телефоне и планшете — «Добавить Face ID / отпечаток»: пароль и «Далее» готовят варианты, и нажатие
 * «Добавить» сразу зовёт navigator.credentials.create (Safari без нажатия Face ID не покажет). Варианты
 * старше FRESH_SEC — снова к паролю и «Далее»: вызов на сервере истекает через 120 с. */
function lockBlock() {
  const note = status('screen-lock-status');
  const enabled = el('input', { type: 'checkbox', id: 'lock-enabled', checked: !!me.screen_lock });
  const offPassword = el('input', { class: 'input', id: 'lock-off-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const off = button('Выключить', { class: 'btn', id: 'lock-off' });
  const offBox = el('div', { hidden: true }, field('Пароль учётки — чтобы выключить блокировку', offPassword, 'lock-off'),
    el('div', { class: 'row' }, off));
  const list = el('ul', { class: 'passkeys', id: 'passkeys' });

  async function setLock(body) {
    const answer = await api('POST', '/api/me/screen-lock', body);
    me.screen_lock = answer.screen_lock;
    enabled.checked = answer.screen_lock;
  }

  enabled.addEventListener('change', async () => {
    say(note, '');
    if (!enabled.checked) {           // выключение — с паролем: переключатель включён, пока не «Выключить»
      enabled.checked = true;
      offBox.hidden = false;
      offPassword.focus();
      return;
    }
    try {
      await setLock({ enabled: true });
    } catch (err) {
      enabled.checked = false;
      say(note, words(err), 'error');
    }
  });
  off.addEventListener('click', async () => {
    off.disabled = true;
    try {
      await setLock({ enabled: false, password: offPassword.value });
      offPassword.value = '';
      offBox.hidden = true;
      say(note, 'Блокировка выключена', 'ok');
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
  const pinRemove = button('Убрать PIN', { class: 'btn', id: 'pin-remove' });
  const pinPassword = el('input', { class: 'input', id: 'pin-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const pinInput = (id) => el('input', { class: 'input', id, type: 'text', inputmode: 'numeric',
    autocomplete: 'one-time-code', maxlength: '8' });
  const pinNew = pinInput('pin-new');
  const pinRepeat = pinInput('pin-repeat');
  const pinSave = button('Сохранить', { class: 'btn', id: 'pin-save' });
  const pinForm = el('div', { id: 'pin-form', class: 'pin-form', hidden: true }, el('div', { class: 'grid3' },
    field('Пароль учётки', pinPassword, 'pin-password'), field('PIN', pinNew, 'pin-new'),
    field('Повтор PIN', pinRepeat, 'pin-repeat')),
  el('p', { class: 'muted small' }, 'От 4 до 8 цифр. Не совпадающий с кодом телефона'), el('div', { class: 'row' }, pinSave));

  function paintPin() {
    pinState.textContent = me.pin ? 'PIN задан' : 'PIN: не задан';
    pinSet.textContent = me.pin ? 'Сменить PIN' : 'Задать PIN';
    pinRemove.hidden = !me.pin;
  }

  pinSet.addEventListener('click', () => {
    say(note, '');
    pinForm.hidden = false;
    pinPassword.focus();
  });
  pinSave.addEventListener('click', async () => {
    if (pinNew.value !== pinRepeat.value) {
      say(note, 'PIN и повтор не совпадают', 'error');
      return;
    }
    pinSave.disabled = true;
    try {
      await api('PUT', '/api/me/pin', { password: pinPassword.value, pin: pinNew.value });
      me.pin = true;
      pinPassword.value = pinNew.value = pinRepeat.value = '';
      pinForm.hidden = true;
      paintPin();
      say(note, 'PIN задан', 'ok');
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
      say(note, 'PIN убран', 'ok');
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
        const del = button('Удалить', { class: 'btn btn-sm btn-danger passkey-delete' }, async () => {
          del.disabled = true;
          try {
            await api('DELETE', `/api/passkeys/${k.id}`);
          } catch (err) {
            say(note, words(err), 'error');
          }
          reload();
        });
        return el('li', { class: 'passkey', 'data-id': k.id }, el('b', {}, k.name), el('span', { class: 'muted small' },
          ` добавлен ${day(k.created)} · ${k.used ? `открывал ${day(k.used)}` : 'ещё не открывал'}`), del);
      }) : [el('li', { class: 'muted small' }, 'Ключей нет: запертую студию откроет пароль учётки')]));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  const adding = [];
  if (isTouch()) {
    const password = el('input', { class: 'input', id: 'passkey-password', type: 'password',
      autocomplete: 'current-password', maxlength: '1024' });
    const next = button('Далее', { class: 'btn', id: 'passkey-next' });
    const ask = el('div', {}, field('Пароль учётки — чтобы добавить ключ этого устройства', password, 'passkey'),
      el('div', { class: 'row' }, next));
    const create = button('Добавить Face ID / отпечаток этого устройства', { class: 'btn btn-accent', id: 'passkey-add',
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
      say(note, 'Прошло больше полутора минут — введите пароль ещё раз');
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
        say(note, 'Ключ добавлен', 'ok');
      } catch (err) {
        if (err && err.name === 'NotAllowedError') {
          say(note, 'Устройство не подтвердило — нажмите ещё раз', 'error');      // вызов на сервере ещё жив
        } else {
          restart();
          say(note, err && err.name === 'InvalidStateError' ? 'Ключ этого устройства уже добавлен' : words(err), 'error');
        }
      } finally {
        create.disabled = false;
      }
      reload();
    });
    adding.push(ask, el('div', { class: 'row' }, create));
  }

  reload();
  return el('section', { class: 'block lock-block' }, el('h2', {}, 'Блокировка'),
    el('label', { class: 'check' }, enabled, 'Запирать на телефоне и планшете через минуту после ухода с экрана'),
    offBox, el('div', { class: 'row pin-row' }, pinState, pinSet, pinRemove), pinForm, list, ...adding, note);
}

/* «Хранилище»: свои файлы по видам (из базы: размер оригиналов, без миниатюр), итог и очередь удаления —
 * «ждут удаления: N (с …)» или «очередь пуста». Квот и автоудаления нет. Панель берёт отсюда же подписи. */
const KIND_NAMES = { image: 'Картинки', video: 'Ролики', audio: 'Звук', document: 'Документы и расшифровки', other: 'Прочее' };
const UNITS = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];

export function sizeText(bytes) {
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i ? n.toLocaleString('ru-RU', { maximumFractionDigits: 1 }) : n} ${UNITS[i]}`;
}

export function filesText(n) {
  const tens = n % 100;
  const ones = n % 10;
  const word = tens >= 11 && tens <= 14 ? 'файлов' : ones === 1 ? 'файл' : ones >= 2 && ones <= 4 ? 'файла' : 'файлов';
  return `${n} ${word}`;
}

export function momentText(ts) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

/** Очередь удаления словами: пуста — kind 'ok'. */
export function queueText(pending) {
  return pending.files ? [`Ждут удаления на хранилище: ${filesText(pending.files)} (с ${momentText(pending.since)}) — `
    + 'удалятся, когда оно станет доступно', ''] : ['Очередь удаления пуста: удалённое стёрто и с хранилища', 'ok'];
}

/* «Выгрузить мою историю»: окно с галочками разделов (все отмечены) → «Подготовить» (POST /api/me/export,
 * токен на час) → ссылка «Скачать: N файлов, X» на ZIP по токену. Ссылка — настоящая <a download>: её
 * нажатие — жест человека, и на iPhone загрузка уйдёт в «Файлы». Отказ — словами в окне. */
const EXPORT_SECTIONS = [['chat', 'Чат'], ['photo', 'Фото'], ['video', 'Видео'], ['transcribe', 'Расшифровка'],
  ['speech', 'Озвучка'], ['voices', 'Голоса озвучки'], ['ocr', 'Распознавание'], ['translate', 'Перевод'],
  ['code', 'Агенты: чаты'], ['agents', 'Агенты: подключения']];

function exportDialog(host) {
  const boxes = EXPORT_SECTIONS.map(([key, name]) => el('label', { class: 'check' },
    el('input', { type: 'checkbox', name: 'export-section', value: key, checked: true }), name));
  const fail = el('p', { class: 'error', id: 'export-error', 'aria-live': 'polite' });
  const ready = el('p', { id: 'export-ready', hidden: true });
  const prepare = button('Подготовить', { class: 'btn btn-accent', id: 'export-prepare' });
  openDialog(host, { id: 'export-dialog', title: 'Выгрузить мою историю', cancelText: 'Закрыть', actions: [prepare],
    body: [el('p', { class: 'muted small' }, 'Один ZIP: чаты, промпты, цепочки, расшифровки и файлы, с описанием '
      + 'и списком сумм sha256. Ключей подключений и пароля в нём нет. Ссылка живёт час.'), ...boxes, fail, ready] });
  prepare.addEventListener('click', async () => {
    const sections = boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
    fail.textContent = '';
    ready.hidden = true;
    if (!sections.length) {
      fail.textContent = 'Отметьте хотя бы один раздел';
      return;
    }
    prepare.disabled = true;
    try {
      const { id, files, bytes } = await api('POST', '/api/me/export', { sections });
      const count = `${files.toLocaleString('ru-RU')} ${plural(files, ['файл', 'файла', 'файлов'])}`;
      ready.replaceChildren(el('a', { class: 'btn btn-accent', id: 'export-download', download: '',
        href: `/api/me/export/${encodeURIComponent(id)}` }, `Скачать: ${count}, ${sizeText(bytes)}`));
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
      const { kinds, total, pending } = await api('GET', '/api/me/storage');
      const line = (key, name, n) => [el('dt', {}, name),
        el('dd', { 'data-kind': key }, `${filesText(n.files)} · ${sizeText(n.bytes)}`)];
      list.replaceChildren(...kinds.filter((k) => k.files).flatMap((k) => line(k.kind, KIND_NAMES[k.kind] || k.kind, k)),
        ...line('total', 'Всего', total));
      say(queue, ...queueText(pending));
    } catch (err) {
      say(queue, words(err), 'error');
    }
  }

  reload();
  return { reload, el: el('section', { class: 'block storage-block' }, el('h2', {}, 'Хранилище'), list, queue,
    el('p', { class: 'muted small' }, 'Счёт — по записям истории, без миниатюр. Тексты чатов и промпты — в базе: '
      + 'удалённое из неё остаётся в копиях базы, пока его не вытеснят 7 новых копий (обычно неделю).'),
    el('div', { class: 'row' }, button('Выгрузить мою историю', { class: 'btn', id: 'export-open' },
      () => exportDialog(host)))) };
}

/* «Интернет» чата: ключ Exa учётки (PUT/DELETE /api/me/exa-key). Страница знает только признак `me.exa_key` —
 * ключ после сохранения из поля стирается и обратно не приходит. Ссылка — в новую вкладку без Referer. */
const EXA_KEYS = 'https://dashboard.exa.ai/api-keys';

function exaBlock() {
  const key = el('input', { class: 'input', id: 'exa-key', type: 'password', autocomplete: 'off', maxlength: '200',
    spellcheck: 'false' });
  const note = status('exa-status');
  const save = button('Сохранить', { class: 'btn', id: 'exa-save' });
  const remove = button('Убрать', { class: 'btn', id: 'exa-remove' });
  const show = () => {
    say(note, me.exa_key ? 'Ключ задан' : 'Ключа нет', me.exa_key ? 'ok' : '');
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
  return el('section', { class: 'block exa-block' }, el('h2', {}, 'Интернет'),
    el('p', { class: 'muted small' }, 'Кнопка «Интернет» в чате — поиск и чтение страниц через Exa по вашему ключу и '
      + 'субтитры YouTube — включается только с ключом; убранный ключ выключает её во всех диалогах. Ключ — на ',
    el('a', { href: EXA_KEYS, target: '_blank', rel: 'noopener noreferrer' }, 'dashboard.exa.ai'),
    ': бесплатно $10 в месяц, без привязанной карты перерасхода нет. Поисковые запросы и адреса страниц видит Exa.'),
    field('Ключ Exa', key, 'exa-key'), el('div', { class: 'row' }, save, remove, note));
}

/* Push раздела «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.8): подписка этого устройства —
 * строка сервера у сессии (GET/POST/DELETE /api/me/push), флаг устройства localStorage `studio-push` — только
 * '1'; ключи и адрес подписки знают браузер и сервер, в localStorage их нет. Разрешение спрашивается первым
 * в обработчике нажатия (Safari без нажатия его не покажет), подписка — после него. */
const PUSH_KEY = 'studio-push';
const DENIED = 'Браузер запретил уведомления — разрешите их в настройках сайта';
const NO_WORKER = 'Фоновая служба студии в этом браузере не запустилась (частный режим?) — push здесь недоступен';
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
        say(note, 'Push включён на этом устройстве', 'ok');
      } else {
        await unsubscribePush();
        pushFlag(false);
        say(note, 'Push выключен на этом устройстве');
      }
    } catch (err) {
      box.checked = !on;
      say(note, err && err.name === 'NotAllowedError' ? DENIED : words(err), 'error');
    } finally {
      box.disabled = false;
    }
  });
  if (pushSupported()) {
    api('GET', '/api/me/push').then(async ({ subscribed }) => {
      let on = pushFlag() && subscribed;
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
    }, (err) => say(note, words(err), 'error'));
  }
  return [el('label', { class: 'check push-check' }, box, 'Push на этом устройстве'),
    el('p', { class: 'muted small', id: 'push-hint' }, 'Приходят и при закрытой студии и погасшем экране: запрос '
      + 'разрешения, вопрос и конец хода Claude Code, Codex или OpenCode, готовность и ошибки фоновых заданий. '
      + 'Идут через серверы Apple или Google — содержимое зашифровано, но факт и время '
      + 'им видны. На iPhone — только у студии, добавленной на экран «Домой».'), note];
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
      say(note, 'Уведомления выключены на этом устройстве');
      return;
    }
    const on = await setNotify(true);
    box.checked = on;
    if (on) say(note, 'Уведомления включены на этом устройстве', 'ok');
    else say(note, 'Браузер запретил уведомления — разрешите их в настройках сайта', 'error');
  });
  if (!supported) {
    say(note, 'Этот браузер не показывает уведомления сайта: на iPhone — только у студии, добавленной на экран «Домой»');
  }
  return el('section', { class: 'block notify-block' }, el('h2', {}, 'Уведомления'),
    el('label', { class: 'check' }, box, 'Сообщать о готовности, когда студия открыта в фоне'), note,
    el('p', { class: 'muted small' }, 'Только на этом устройстве и пока студия открыта — свёрнутая вкладка или окно. '
      + 'При включённом push готовность сообщает сервер, без повторного локального уведомления. '
      + 'Промптов, текстов и имён файлов в уведомлении нет.'),
    ...pushBox());
}

/* «Компьютеры (Claude Code)»: под заголовком всегда «Скачать мост» (ui.js agentDownload — второй компьютер ставят
 * тем же архивом); мосты учётки (GET /api/agents) — имя, «на связи», значок «PIN»; «Отозвать» — окно и
 * DELETE /api/agents/<id> (мост на связи отключается). История подключений (GET /api/agents/events) — только
 * чтение, новые сверху страницами сервера, «Показать ещё» — по `next`. */
const AGENT_EVENTS = { enroll: 'привязан', revoke: 'отозван', online: 'на связи', offline: 'связь потеряна',
  open: 'открыт', pin_ok: 'открыт PIN', pin_fail: 'неверный PIN', session_start: 'новый чат' };

function agentsBlock(host) {
  const list = el('ul', { class: 'agents', id: 'agents' });
  const events = el('ul', { class: 'agent-events', id: 'agent-events' });
  const note = status('agents-status');
  let next = null;
  const more = button('Показать ещё', { class: 'btn btn-sm', id: 'agent-events-more', hidden: true },
    () => loadEvents(next));

  function revoke(agent) {
    const yes = button('Отозвать', { class: 'btn btn-danger', id: 'agent-revoke-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'agent-revoke-dialog', title: 'Отозвать компьютер?', actions: [yes], body: [
      el('p', {}, 'Мост на нём отключится и не войдёт снова без нового входа. История подключений останется.'),
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
      const { items } = await api('GET', '/api/agents');
      list.replaceChildren(...(items.length ? items.map((a) => el('li', { class: 'agent', 'data-id': String(a.id) },
        el('b', { class: 'agent-name' }, a.name),
        el('span', { class: `tag agent-state${a.online ? ' server' : ''}` }, a.online ? 'на связи' : 'не на связи'),
        a.has_pin ? el('span', { class: 'tag agent-pin' }, 'PIN') : null,
        button('Отозвать', { class: 'btn btn-sm btn-danger agent-revoke' }, () => revoke(a))))
        : [el('li', { class: 'muted small' }, 'Компьютеров пока нет.')]));
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
  return el('section', { class: 'block agents-block' }, el('h2', {}, 'Компьютеры (Claude Code, Codex и OpenCode)'), agentDownload(),
    list, note,
    el('h3', {}, 'История подключений'), events, el('div', { class: 'row' }, more));
}

function wipeBlock(host, onWiped) {
  const note = status('wipe-status');
  const start = button('Удалить всю историю', { class: 'btn btn-danger', id: 'history-wipe' }, () => {
    say(note, '');
    const text = el('p', {}, 'Все чаты, фото и видео удалятся безвозвратно. Подключения и настройки останутся.'
      + ' Чаты Claude Code, Codex и OpenCode хранятся на компьютерах — студия забудет только свои отметки, сами сессии останутся.');
    const fail = el('p', { class: 'error' });
    const next = button('Удалить', { class: 'btn btn-danger', id: 'wipe-next' });
    const confirm = button('Да, удалить навсегда', { class: 'btn btn-danger solid', id: 'wipe-confirm', hidden: true });
    const dialog = openDialog(host, { id: 'wipe-dialog', title: 'Удалить всю историю?', body: [text, fail],
      actions: [next, confirm] });
    next.addEventListener('click', () => {
      text.textContent = 'Точно? Отменить удаление нельзя.';
      next.hidden = true;
      confirm.hidden = false;
      confirm.focus();
    });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        const { pending_files: left } = await api('DELETE', '/api/history');
        dialog.close();
        // хранилище было недоступно: файлы ждут в очереди удаления — сказать как есть
        say(note, left ? `История удалена, но файлов на хранилище ещё ${left}: удалятся, когда оно станет доступно`
          : 'История удалена', 'ok');
        onWiped();
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
      }
    });
  });
  return el('section', { class: 'block' }, el('h2', {}, 'История'),
    el('p', { class: 'muted small' }, 'Чаты, фото и видео этого профиля. Подключения не удаляются.'),
    el('div', { class: 'row' }, start, note));
}

/* ---- голоса озвучки ------------------------------------------------------------------ */

function clock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function voicesBlock(host) {
  const list = el('ul', { class: 'voices' });
  const note = status('voice-status');

  async function reload() {
    try {
      const { items } = await api('GET', '/api/voices');
      list.replaceChildren(...(items.length ? items.map((v) => {
        const del = button('Удалить', { class: 'btn btn-sm btn-danger voice-delete' }, async () => {
          del.disabled = true;
          try {
            await api('DELETE', `/api/voices/${v.id}`);
          } catch (err) {
            say(note, words(err), 'error');
          }
          reload();
        });
        return el('li', { class: 'voice', 'data-id': v.id }, el('b', {}, v.name),
          el('span', { class: 'muted small' }, ` образец ${clock(v.duration)}`), del);
      }) : [el('li', { class: 'muted small' }, 'Голосов пока нет: озвучка шлюза srvllm говорит голосом по образцу')]));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  function add() {
    const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off' });
    const text = el('textarea', { class: 'input', name: 'text', rows: '3', maxlength: '500',
      placeholder: 'Что именно сказано в записи — дословно' });
    const file = el('input', { type: 'file', name: 'audio', accept: 'audio/*,.m4a,.mp3,.wav,.ogg,.webm' });
    const rec = button('Записать с микрофона', { class: 'btn btn-sm', id: 'voice-record' });
    const recNote = el('p', { class: 'muted small', 'aria-live': 'polite' });
    const fail = el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' });
    const save = button('Сохранить', { class: 'btn btn-accent', id: 'voice-save' });
    let recorded = null;
    let recording = null;
    const dialog = openDialog(host, { id: 'voice-dialog', title: 'Новый голос', actions: [save], body: [
      field('Имя голоса', name, 'name'), field('Текст образца', text, 'text'),
      el('p', { class: 'muted small' }, 'Запись 5–15 секунд чистой речи: файлом или с микрофона.'),
      el('div', { class: 'row' }, file, rec), recNote, fail] });
    rec.addEventListener('click', async () => {
      if (recording) {
        recording.stop();
        return;
      }
      try {
        recording = await startRecording(15, (sec) => { recNote.textContent = `Запись · ${clock(sec)} — нажмите ещё раз, чтобы остановить`; }, rec);
      } catch (err) {
        recNote.textContent = words(err);
        return;
      }
      if (!recording) return;                        // окно закрыли, пока браузер спрашивал разрешение
      rec.textContent = 'Остановить';
      const sample = await recording.done;
      recording = null;
      if (!sample) {                                 // отменена (окно закрыли, ушли из раздела)
        rec.textContent = recorded ? 'Записать заново' : 'Записать с микрофона';
        recNote.textContent = '';
        return;
      }
      recorded = sample;
      rec.textContent = 'Записать заново';
      recNote.textContent = 'Запись готова';
      file.value = '';
    });
    save.addEventListener('click', async () => {
      const sample = file.files[0] || recorded;
      if (!sample) {
        fail.textContent = 'Нужна запись образца: файл или с микрофона';
        return;
      }
      save.disabled = true;
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
        save.disabled = false;
      }
    });
    name.focus();
  }

  reload();
  return el('section', { class: 'block voices-block' }, el('div', { class: 'block-head' }, el('h2', {}, 'Голоса'),
    button('+ Голос', { class: 'btn', id: 'voice-add' }, add)), list, note);
}

/* ---- разделы ---------------------------------------------------------------------- */

export function settingsView(host, { sections: base, connections: connBase, section = null, hash = false,
  other = false, direct = false, voices = false, onChange = null }) {
  const names = { ...SECTION_NAMES, code: 'Агенты', other: 'Прочее' };
  const ids = [...Object.keys(SECTION_NAMES).filter((id) => id !== 'dictation'), ...(other ? ['code'] : []), 'other'];
  const nav = el('nav', { class: 'settings-nav', 'aria-label': 'Разделы настроек' });
  const panel = el('div', { class: 'settings-panel' });
  const root = el('div', { class: 'settings' }, nav, panel);
  const note = el('p', { class: 'status section-status', 'aria-live': 'polite' });
  const found = new Map();        // `<раздел>:<подключение>` → найденные модели после «Обновить модели»
  const checkErrors = new Map();  // подключение → отказ последней проверки (в блоке паспорта)
  const checking = new Set();     // подключения, которые проверяются сейчас
  const opened = new Set();       // `<раздел>:<подключение>` — паспорт раскрыт
  let data = [];
  let dictationHost = null;
  if (section === 'dictation') section = 'other';  // прежняя ссылка открывает перенесённые настройки
  let current = ids.includes(section) ? section : null;

  for (const id of ids) {
    const link = el('a', { href: `#/settings/${id}`, 'data-section': id }, names[id]);
    if (!hash) {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        show(id);
      });
    }
    nav.append(link);
  }

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
    say(note, 'Загружаю список моделей…');
    try {
      // прямое подключение: список берёт браузер; через сервер, убранное из списка (и в панели) — GET …/found
      const items = direct && !conn.proxied && !conn.dropped ? await modelsOf(conn)
        : (await api('GET', `${base}/${s.id}/links/${conn.id}/found`)).items;
      found.set(`${s.id}:${conn.id}`, items);
      say(note, items.length ? `Найдено моделей: ${items.length}` : 'Сервис не назвал ни одной модели');
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
    const send = button('Отправить', { class: 'btn btn-accent', id: 'probe-send' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'probe-dialog', title: 'Пробный ответ', actions: [send], body: [
      el('p', {}, `Модель «${modelLabel(model)}» подключения «${conn.name}».`), el('p', {}, PROBE_WARNING), fail] });
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
    const yes = button('Удалить', { class: 'btn btn-danger', id: 'conn-delete-confirm' });
    const fail = el('p', { class: 'error' });
    const used = sectionsText(conn.sections);
    const dialog = openDialog(host, { id: 'conn-delete-dialog', title: 'Удалить подключение?', actions: [yes], body: [
      el('p', {}, `«${conn.name}» — ${conn.base_url}`),
      el('p', { class: 'muted' }, used ? `Подключение участвует в разделах: ${used}. Оно уйдёт из всех.` : 'Подключение не участвует ни в одном разделе.'),
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
        'data-conn': String(conn.id), 'aria-label': `По умолчанию: ${modelLabel(m)}`, disabled: !checked,
        checked: !!s.default && s.default.connection_id === conn.id && s.default.model === m });
      check.addEventListener('change', () => saveModels(s, conn, names, box));
      radio.addEventListener('change', () => saveDefault(s, conn, m));
      // пробный ответ тратит запрос: только по кнопке, только у отмеченной модели чата и не у srvllm (занял бы карту)
      const tryable = direct && checked && s.id === 'chat' && conn.kind !== 'srvllm';
      box.append(el('li', { class: 'model', 'data-model': m },
        el('label', { class: 'check model-name' }, check, modelLabel(m)),
        known && checked && !known.includes(m) ? el('span', { class: 'tag model-missing' }, 'не найдена') : null,
        tryable ? button('Пробный ответ', { class: 'btn btn-sm model-probe' }, () => tryModel(s, conn, m)) : null,
        el('label', { class: 'model-default-label', title: 'По умолчанию' }, radio, el('span', { class: 'small muted' }, 'по умолчанию'))));
    }
    let search = null;
    if (names.length > SEARCH_FROM) {
      search = el('input', { class: 'input model-search', type: 'search', placeholder: 'Поиск модели', 'aria-label': 'Поиск модели' });
      search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        for (const li of box.children) li.hidden = !!q && !li.dataset.model.toLowerCase().includes(q);
      });
    }
    const actions = el('div', { class: 'link-actions', hidden: true },
      button('Изменить', { class: 'btn btn-sm link-edit' }, () => editConnection(host, connBase, conn, changed)),
      button('Убрать из раздела', { class: 'btn btn-sm link-remove' }, async () => {
        try {
          await api('DELETE', `${base}/${s.id}/links/${conn.id}`);
          say(note, '');
        } catch (err) {
          say(note, words(err), 'error');
        }
        await changed();
      }),
      button('Удалить подключение', { class: 'btn btn-sm btn-danger link-delete' }, () => remove(conn)));
    const menu = button('⋯', { class: 'btn btn-sm link-menu', 'aria-label': 'Ещё', 'aria-expanded': 'false' }, () => {
      actions.hidden = !actions.hidden;
      menu.setAttribute('aria-expanded', String(!actions.hidden));
    });
    const key = `${s.id}:${conn.id}`;
    const busy = checking.has(conn.id);
    const passport = direct && (conn.passport || busy || checkErrors.has(conn.id))
      ? passportBlock({ conn, section: s.id, models: link.catalog || link.models, label: modelLabel,
        error: checkErrors.get(conn.id), busy, open: opened.has(key),
        onToggle: (open) => (open ? opened.add(key) : opened.delete(key)) }) : null;
    const checkButton = direct ? button('Проверить', { class: 'btn btn-sm link-check', disabled: busy },
      () => runCheck(s, conn)) : null;
    return el('li', { class: 'link', 'data-conn': String(conn.id) },
      el('div', { class: 'link-head' },
        el('div', { class: 'link-main' },
          el('b', { class: 'link-name' }, conn.name),
          el('span', { class: 'link-url' }, conn.base_url),
          el('span', { class: 'link-hint' },
            el('span', { class: `tag${conn.proxied ? ' server' : ''}` }, conn.proxied ? 'через сервер' : 'из браузера'),
            ` ключ ${conn.token_hint}`)),
        el('div', { class: 'link-buttons' }, checkButton,
          button('Обновить модели', { class: 'btn btn-sm link-refresh' }, () => refresh(s, conn)), menu)),
      passport, actions, search,
      names.length ? box : el('p', { class: 'muted small' }, 'Нажмите «Обновить модели» и отметьте нужные'));
  }

  async function addLink(s) {
    let conns = [];
    try {
      conns = (await api('GET', connBase)).items.filter((c) => !c.sections.includes(s.id));
    } catch (err) {
      say(note, words(err), 'error');
      return;
    }
    const select = el('select', { class: 'input', name: 'existing' },
      ...conns.map((c) => el('option', { value: String(c.id) }, `${c.name} — ${c.base_url}`)),
      el('option', { value: 'new' }, 'Новый адрес…'));
    select.value = conns.length ? String(conns[0].id) : 'new';
    const form = connectionForm(null);
    form.el.hidden = select.value !== 'new';
    select.addEventListener('change', () => { form.el.hidden = select.value !== 'new'; });
    const box = el('div', {}, field('Подключение', select, 'existing'), form.el);
    const save = button('Добавить', { class: 'btn btn-accent', id: 'link-save' });
    const dialog = openDialog(host, { id: 'link-dialog', title: `Адрес для раздела «${names[s.id]}»`, body: [box], actions: [save] });
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

  function sectionBlock(s) {
    const list = el('ul', { class: 'links' }, ...(s.links.length ? s.links.map((l) => linkCard(s, l))
      : [el('li', { class: 'muted' }, 'В разделе пока нет подключений')]));
    return el('section', { class: 'block section-panel', 'data-section': s.id },
      el('h2', {}, names[s.id]), list,
      el('div', { class: 'row' }, button('+ Адрес для раздела', { class: 'btn', id: 'link-add' }, () => addLink(s))),
      note);
  }

  function renderDictation() {
    const s = sectionOf('dictation');
    dictationHost.replaceChildren(s ? sectionBlock(s) : note);
  }

  function render() {
    const shown = current || ids[0];
    root.dataset.view = current ? 'panel' : 'nav';
    for (const a of nav.children) {
      if (a.dataset.section === shown) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    if (shown === 'other' && dictationHost) {
      renderDictation();
      return;
    }
    dictationHost = null;
    const back = hash ? el('a', { class: 'settings-back', href: '#/settings' }, '‹ Настройки')
      : button('‹ Разделы', { class: 'btn-link settings-back' }, () => show(null));
    if (shown === 'code') {
      panel.replaceChildren(back, el('div', { class: 'section-panel', 'data-section': 'code' },
        codeDefaultsBlock(), agentsBlock(host)));
      return;
    }
    if (shown === 'other') {
      dictationHost = el('div', { class: 'dictation-settings' });
      renderDictation();
      const storage = other ? storageBlock(host) : null;
      panel.replaceChildren(back, el('div', { class: 'section-panel', 'data-section': 'other' }, dictationHost,
        ...(other ? [passwordBlock(), lockBlock(), notifyBlock(), exaBlock(), storage.el, wipeBlock(host, storage.reload)] : [])));
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
    if (id === 'dictation') id = 'other';
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
    else if ((current || ids[0]) !== 'code') render();
  }

  render();
  return { el: root, show, reload };
}

/* ---- маршрут ---------------------------------------------------------------------- */

let screen = null;

export function mount(view, ctx) {
  screen = settingsView(view, { sections: '/api/sections', connections: '/api/connections', section: ctx.id,
    hash: true, other: true, direct: true, voices: true, onChange: () => loadMe().catch(() => null) });
  view.append(el('header', { class: 'head' }, el('h1', {}, 'Настройки')), el('div', { class: 'page wide' }, screen.el));
  screen.reload();
}

export function update(ctx) {
  if (screen) screen.show(ctx.id);
}

export function unmount() {
  screen = null;
}
