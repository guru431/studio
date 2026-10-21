/* Раздел «Настройки» (#/settings, #/settings/<раздел>) — вариант А спеки 2026-10-20 (раздел 6.2).
 *
 * Слева — шесть разделов и «Прочее» (пароль, блокировка на телефоне, уведомления, ключ Exa «Интернета» чата, хранилище и выгрузка истории, удаление истории), справа — выбранный раздел: его
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
 * переключаются на месте, «Прочего», голосов и прямого списка моделей нет). На телефоне без раздела
 * в адресе — список разделов, с разделом — его экран со ссылкой «‹ Настройки». */
import { api, loadMe, me } from './api.js';
import { connectionForm, editConnection, SECTION_NAMES, sectionsText } from './connection-form.js';
import { notifyEnabled, notifySupported, setNotify } from './jobs.js';
import { addPasskey, FRESH_SEC, isTouch, passkeyOptions } from './lock.js';
import { recordingName, startRecording } from './mic.js';
import { check, passportBlock, probe, PROBE_WARNING } from './passport.js';
import { button, el, field, openDialog, plural, showError, words } from './ui.js';

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

/* «Блокировка»: переключатель (выключить — только с паролем учётки), ключи учётки с «Удалить» и — на
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
    offBox, list, ...adding, note);
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
  ['speech', 'Озвучка'], ['voices', 'Голоса озвучки']];

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

/* «Уведомления» (jobs.js): флаг этого устройства; включение спрашивает разрешение браузера. */
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
      + 'Закрытая студия и запертый телефон уведомлений не получают. Промптов, текстов и имён файлов в уведомлении нет.'));
}

function wipeBlock(host, onWiped) {
  const note = status('wipe-status');
  const start = button('Удалить всю историю', { class: 'btn btn-danger', id: 'history-wipe' }, () => {
    say(note, '');
    const text = el('p', {}, 'Все чаты, фото и видео удалятся безвозвратно. Подключения и настройки останутся.');
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
  const ids = [...Object.keys(SECTION_NAMES), ...(other ? ['other'] : [])];
  const nav = el('nav', { class: 'settings-nav', 'aria-label': 'Разделы настроек' });
  const panel = el('div', { class: 'settings-panel' });
  const root = el('div', { class: 'settings' }, nav, panel);
  const note = el('p', { class: 'status section-status', 'aria-live': 'polite' });
  const found = new Map();        // `<раздел>:<подключение>` → найденные модели после «Обновить модели»
  const checkErrors = new Map();  // подключение → отказ последней проверки (в блоке паспорта)
  const checking = new Set();     // подключения, которые проверяются сейчас
  const opened = new Set();       // `<раздел>:<подключение>` — паспорт раскрыт
  let data = [];
  let current = ids.includes(section) ? section : null;

  for (const id of ids) {
    const link = el('a', { href: `#/settings/${id}`, 'data-section': id }, id === 'other' ? 'Прочее' : SECTION_NAMES[id]);
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
    const dialog = openDialog(host, { id: 'link-dialog', title: `Адрес для раздела «${s.name}»`, body: [box], actions: [save] });
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

  function render() {
    const shown = current || ids[0];
    root.dataset.view = current ? 'panel' : 'nav';
    for (const a of nav.children) {
      if (a.dataset.section === shown) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    const back = hash ? el('a', { class: 'settings-back', href: '#/settings' }, '‹ Настройки')
      : button('‹ Разделы', { class: 'btn-link settings-back' }, () => show(null));
    if (shown === 'other') {
      const storage = storageBlock(host);
      panel.replaceChildren(back, el('div', { class: 'section-panel', 'data-section': 'other' }, passwordBlock(),
        lockBlock(), notifyBlock(), exaBlock(), storage.el, wipeBlock(host, storage.reload)));
      return;
    }
    const s = sectionOf(shown);
    if (!s) {
      panel.replaceChildren(back, note);
      return;
    }
    const list = el('ul', { class: 'links' }, ...(s.links.length ? s.links.map((l) => linkCard(s, l))
      : [el('li', { class: 'muted' }, 'В разделе пока нет подключений')]));
    panel.replaceChildren(back, el('section', { class: 'block section-panel', 'data-section': s.id },
      el('h2', {}, s.name), list,
      el('div', { class: 'row' }, button('+ Адрес для раздела', { class: 'btn', id: 'link-add' }, () => addLink(s))),
      note), ...(voices && s.id === 'speech' ? [voicesBlock(host)] : []));
  }

  function show(id) {
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
    render();
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
