/* Чат Claude Code (#/code/<aid>/<pid>/<chat>) — спека 2026-10-28-studio-claude-code-design 6.6 и 6.10,
 * протокол канала §4 (history, turn, watch, answer, stop) и §7 (события).
 *
 * chatView(host, { aid, pid, chat, agent, project, origin, fresh, closed, closedChat, gone, openFile }) → { unmount() }:
 * - «Закрыть чат» — PATCH {state: "closed"}, затем closedChat() (code.js убирает вкладку); история отвечает 404
 *   not_found (сессии на машине нет, студия её не заводила) — gone() вместо слов (вкладка уходит сама);
 * - лента #code-feed — страница истории (GET …/chats/<chat>, по 100), «Показать раньше» (#code-more) —
 *   предыдущая (before = start); сообщения — .code-msg.user (текст как есть) и .code-msg.assistant (шаги
 *   details.code-step, затем текст — markdown.js);
 * - сразу за историей — поток live: idle — ничего, snapshot — всё состояние идущего хода вместо прежнего
 *   хвоста; «Отправить» — поток turn; события text, step, result, ask (карточка .code-ask; у вопроса — ещё
 *   «Свой ответ»), asked (карточка уходит), done (stopped — «Ход прерван», error — «Ошибка: …» или «Ошибка»;
 *   done раньше снимка — ход кончился между watch и снимком: история заново);
 * - хода нет и страница видна — live раз в IDLE_POLL_SEC: ход, начатый в другой вкладке, на другом устройстве
 *   или в VS Code, страница увидит; не дольше IDLE_POLL_FOR_SEC после действия владельца в чате — опрос
 *   продлевает открытость компьютера (протокол §3), и забытая вкладка держала бы его открытым вечно;
 * - отказ «Отправить» 409 running (ход уже идёт) и 504 timeout (ход, может быть, начат) — слова и live;
 * - offline (событие или 409, и у «Отправить») — «Компьютер пропал со связи — жду его» и live раз в RETRY_MS,
 *   пока страница видна; closed (событие или 409) — closed() из code.js: к панели PIN этого компьютера, после
 *   PIN — снова сюда; ответ, пришедший после ухода с чата, никуда не уводит;
 * - страница ушла с экрана — свой поток она снимает (push от сервера уходит, только если чат никто не
 *   смотрит), вернулась — снова live: снимок догонит ход, ход на машине от этого не прерывается.
 * Модель и усилие: первый пункт — «по умолчанию» (null — прежние модель и усилие чата, протокол §4). Выбор
 * учётки — из me.code_defaults, подставляется только в новый чат (fresh — заведён
 * «Новым чатом» в этом окне, или история пуста), пока выбор не трогали; существующий начинает с «по умолчанию».
 * Черновик поля — в памяти модуля по чату (drafts): уход к PIN и назад его не теряет; снимается, когда ход
 * принят — идущий запрос «Отправить» ни уход с экрана, ни уход с чата не обрывают, поток снимается после ответа. В хранилище браузера —
 * ни ленты, ни поля. Текст машины — только textContent и markdown.js. */
import { api, me } from './api.js';
import { ACCESS, ENGINE_NAMES, accessLabel, effortLabel } from './code-options.js';
import { fieldBox } from './field.js';
import { renderMarkdown } from './markdown.js';
import { readSSE } from './sse.js';
import { el, words } from './ui.js';

const WAITING = 'Компьютер пропал со связи — жду его';
const SHARED = 'Эту сессию начали в VS Code или терминале. Если она открыта и там, не пишите в двух местах сразу'
  + ' — ответы перемешаются.';
const STOPPED = 'Ход прерван';
const INSTEAD = 'Что сделать вместо этого (необязательно)';
const OWN = 'Свой ответ';
const OWN_MAX = 2000;            // claude_code.ANSWER_MAX
const DEFAULT = 'Загрузка…';
const RETRY_MS = 5000;           // повтор live, пока компьютера нет
const AGAIN_MS = 1000;           // поток кончился без конца хода (предел прокси, сеть) — снова live
const IDLE_POLL_SEC = 20;        // опрос live на простое (тесты подменяют паузу setTimeout)
const IDLE_POLL_FOR_SEC = 1800;  // ... не дольше этого после действия владельца в чате
const PAINT_MS = 80;             // текст хода перерисовывается не чаще
const STICK_PX = 80;             // лента у низа — новое её прокручивает
const PREVIEW_MAX = 80;          // начало ввода шага в его заголовке
const FILE_TOOLS = new Set(['Read', 'Edit', 'Write']);
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const IMAGE_MAX = 5 * 1024 * 1024;
const IMAGES_MAX = 8 * 1024 * 1024;
const IMAGE_COUNT = 4;

/** Черновики поля задания: `<aid>/<chat>` → текст; только память страницы. */
const drafts = new Map();
const imageDrafts = new Map();  // картинки черновика — только память, как текст

/** Начало ввода шага: первая строка-значение (команда, путь, шаблон), иначе JSON. */
function preview(input) {
  let text = '';
  if (input && typeof input === 'object') {
    const first = Object.values(input).find((v) => typeof v === 'string');
    text = first !== undefined ? first : JSON.stringify(input);
  }
  text = text.replace(/\s+/g, ' ').trim();
  return text.length > PREVIEW_MAX ? `${text.slice(0, PREVIEW_MAX)}…` : text;
}

/** Путь файла шага внутри проекта — хвост через «/» ('src/app.py'), иначе null. Начало — projects_root
 * компьютера, «\» и имя проекта, без учёта регистра. */
function tailOf(filePath, agent, project) {
  if (typeof filePath !== 'string' || !agent || typeof agent.projects_root !== 'string' || !project) return null;
  const start = `${agent.projects_root.replace(/\//g, '\\').replace(/\\+$/, '')}\\${project.name}\\`;
  const path = filePath.replace(/\//g, '\\');
  if (path.length <= start.length || path.slice(0, start.length).toLowerCase() !== start.toLowerCase()) return null;
  const parts = path.slice(start.length).split('\\');
  return parts.some((p) => p === '' || p === '.' || p === '..') ? null : parts.join('/');
}

export function chatView(host, { aid, pid, chat, agent, project, origin, fresh = false, engine = 'claude', closed: toPin, closedChat, gone,
  openFile }) {
  const url = `/api/code/agents/${aid}/projects/${pid}/chats/${chat}`;
  const endpoint = (tail = '') => `${url}${tail}${engine !== 'claude' ? `?engine=${engine}` : ''}`;
  const assistant = ENGINE_NAMES[engine];
  const draftKey = `${aid}/${chat}`;
  const scroller = document.getElementById('view');
  let alive = true;
  let touched = false;           // выбор модели или усилия меняли в этом чате
  let controller = null;         // поток (turn или live), который читает страница
  let retry = null;              // таймер следующего live
  let running = false;
  let sending = false;
  let resolving = true;
  let stateRequest = null;
  let adding = false;
  let images = imageDrafts.get(draftKey) || [];
  let turn = null;               // ответ идущего хода: { node, steps, md, asks, text }
  let start = 0;                 // номер первого показанного сообщения истории
  let paintTimer = null;
  let acted = Date.now();        // последнее действие владельца в чате (открыл, вернулся, ввёл, ответил)

  const status = el('p', { id: 'code-status', class: 'error', 'aria-live': 'polite' });
  const more = el('button', { type: 'button', id: 'code-more', class: 'btn btn-sm code-more', hidden: true },
    'Показать раньше');
  const feed = el('div', { id: 'code-feed', class: 'code-feed' });
  const text = el('textarea', { id: 'code-text', rows: '1', 'aria-label': 'Задание',
    placeholder: 'Задание…', maxlength: '100000' });
  const catalog = engine === 'claude' ? agent : agent?.[engine];
  const models = Array.isArray(catalog?.models) ? catalog.models : [];
  const efforts = Array.isArray(catalog?.efforts) ? catalog.efforts : [];
  const model = el('select', { id: 'code-model', class: 'chip' }, el('option', { value: '' }, DEFAULT),
    ...models.map((m) => el('option', { value: String(m.id) }, String(m.label || m.id))));
  const effort = el('select', { id: 'code-effort', class: 'chip' }, el('option', { value: '' }, DEFAULT),
    ...efforts.map((e) => el('option', { value: String(e) }, String(e))));
  const access = el('select', { id: 'code-access', class: 'chip', 'aria-label': 'Права доступа' },
    el('option', { value: '' }, DEFAULT), ...Object.entries(ACCESS[engine]).map(([value, label]) =>
      el('option', { value }, label)));
  const contextMeter = el('meter', { id: 'code-context-meter', min: '0', max: '100', value: '0', hidden: true });
  const contextText = el('span', { id: 'code-context-text' }, 'Контекст —');
  const contextBox = el('span', { id: 'code-context', class: 'code-context', title: 'Статистика контекста ещё не получена' },
    contextMeter, contextText);

  /** Умолчания учётки — в новый чат, пока свой выбор не тронут. */
  function applyDefaults() {
    if (touched) return;
    const saved = me?.code_defaults?.[engine] || {};
    if (models.some((m) => String(m.id) === saved.model)) model.value = saved.model;
    if (efforts.some((e) => String(e) === saved.effort)) effort.value = saved.effort;
    if (ACCESS[engine][saved.access]) access.value = saved.access;
    if ((saved.model && !model.value) || (saved.effort && !effort.value)) {
      status.textContent = 'Часть умолчаний недоступна на этом компьютере — выберите модель и усилие в чате';
    }
  }

  if (fresh) applyDefaults();
  const send = el('button', { type: 'submit', id: 'code-send', class: 'btn btn-accent composer-go',
    'aria-label': 'Отправить', title: 'Отправить' }, el('span', { class: 'code-go-label' }, 'Отправить'));
  const stop = el('button', { type: 'button', id: 'code-stop', class: 'btn composer-go', hidden: true,
    'aria-label': 'Остановить', title: 'Остановить' }, el('span', { class: 'code-go-label' }, 'Остановить'));
  send.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>');
  stop.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>');
  const closeChat = el('button', { type: 'button', id: 'code-close-chat', class: 'btn btn-sm' }, 'Закрыть чат');
  const activityText = el('span', {}, 'Нет активной задачи');
  const activity = el('div', { id: 'code-activity', class: 'code-activity', role: 'status',
    'aria-live': 'polite', 'aria-atomic': 'true', 'data-state': 'idle' },
    el('span', { class: 'code-activity-dot', 'aria-hidden': 'true' }), activityText);
  const activeTools = new Map();
  const field = fieldBox(text);
  const attachments = el('div', { id: 'code-images', class: 'code-images' });
  const picker = el('input', { type: 'file', id: 'code-image-input', accept: [...IMAGE_TYPES].join(','),
    multiple: true, hidden: true, 'aria-label': 'Скриншоты' });
  const attach = el('button', { type: 'button', id: 'code-attach', class: 'icon-btn', 'aria-label': 'Добавить скриншот',
    title: agent?.images ? 'Выберите скриншот, вставьте Ctrl+V или перетащите в поле'
      : 'Обновите мост на компьютере для отправки изображений', disabled: !agent?.images });
  attach.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.5"/><path d="m3 16 5-4 4 4 4-6 5 6"/></svg>';
  field.box.querySelector('.field-tools').insertBefore(attach, field.box.querySelector('.mic'));
  field.box.querySelector('.field-tools').insertBefore(activity, attach);
  const pick = (label, select) => {
    select.setAttribute('aria-label', label);
    return el('label', { class: 'code-pick' }, el('span', { class: 'code-pick-name' }, label), select);
  };
  const form = el('form', { class: 'composer code-composer', id: 'code-composer' }, attachments, picker, field.box, field.error,
    el('div', { class: 'composer-bar' },
      el('div', { class: 'composer-opts' },
        pick('Модель', model), pick('Усилие', effort), pick('Права', access), contextBox),
      stop, send));
  host.append(el('div', { class: 'code-chat-bar' }, el('span', { class: 'tag code-engine' }, assistant), closeChat),
    ...(origin === 'other' ? [el('p', { id: 'code-shared-note', class: 'code-shared-note' }, SHARED)] : []),
    more, feed, status, form);
  if (drafts.has(draftKey)) {
    text.value = drafts.get(draftKey);     // после fieldBox: «очистить» видит текст
    autosize();
  }
  paintImages();

  function paintSettings(data) {
    model.options[0].textContent = typeof data.model === 'string' && data.model ? data.model : 'Не получено';
    model.title = typeof data.model === 'string' ? data.model : '';
    effort.options[0].textContent = effortLabel(data.effort);
    access.options[0].textContent = accessLabel(engine, data.access);
    if (model.value && typeof data.model === 'string' && data.model) model.selectedOptions[0].textContent = data.model;
  }

  function paintContext(data) {
    if (!data || !Number.isFinite(data.used) || !Number.isFinite(data.limit) || data.used < 0 || data.limit <= 0) return;
    const percent = Math.min(100, Math.round(1000 * data.used / data.limit) / 10);
    contextMeter.value = percent;
    contextMeter.hidden = false;
    contextText.textContent = `Контекст ${percent}%`;
    contextBox.title = `${data.used.toLocaleString('ru-RU')} / ${data.limit.toLocaleString('ru-RU')} токенов`;
    contextBox.dataset.used = String(data.used);
    contextBox.dataset.limit = String(data.limit);
  }

  async function loadState() {
    const own = new AbortController();
    stateRequest = own;
    resolving = true;
    model.disabled = effort.disabled = access.disabled = true;
    send.disabled = true;
    try {
      const data = await api('POST', endpoint('/state'), { model: model.value || null, effort: effort.value || null,
        access: access.value || null }, { signal: own.signal });
      if (!alive || own.signal.aborted) return;
      paintSettings(data.settings || {});
      paintContext(data.context);
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      paintSettings({});
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
    } finally {
      resolving = false;
      stateRequest = null;
      model.disabled = effort.disabled = access.disabled = false;
      send.disabled = running || sending || adding;
    }
  }

  function paintImages() {
    attachments.replaceChildren(...images.map((image) => {
      const remove = el('button', { type: 'button', class: 'icon-btn code-image-remove',
        'aria-label': `Убрать ${image.name}`, disabled: sending }, '×');
      remove.addEventListener('click', () => {
        images = images.filter((i) => i !== image);
        keepImages();
      });
      return el('div', { class: 'code-image' },
        el('img', { src: `data:${image.mime};base64,${image.data}`, alt: image.name }), remove);
    }));
  }

  function keepImages() {
    if (images.length) imageDrafts.set(draftKey, images);
    else imageDrafts.delete(draftKey);
    paintImages();
    acted = Date.now();
  }

  async function addImages(files) {
    if (!files.length || sending || adding) return;
    if (!agent?.images) {
      status.textContent = 'Обновите мост на компьютере для отправки изображений';
      return;
    }
    adding = true;
    send.disabled = true;
    attach.disabled = true;
    status.textContent = '';
    try {
      if (images.length + files.length > IMAGE_COUNT) throw new Error('Можно прикрепить до 4 изображений');
      if (files.some((f) => !IMAGE_TYPES.has(f.type))) throw new Error('Нужен PNG, JPEG, WebP или GIF');
      if (files.some((f) => !f.size || f.size > IMAGE_MAX)) throw new Error('Размер изображения — от 1 байта до 5 МиБ');
      if ([...images, ...files].reduce((sum, f) => sum + f.size, 0) > IMAGES_MAX) {
        throw new Error('Общий размер изображений — до 8 МиБ');
      }
      const selected = await Promise.all(files.map((file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({ mime: file.type, data: String(reader.result).split(',')[1],
          name: file.name || 'Скриншот', size: file.size });
        reader.onerror = () => reject(new Error('Не получилось прочитать изображение'));
        reader.readAsDataURL(file);
      })));
      // Даже после ухода к PIN черновик остаётся у своего чата.
      images = [...images, ...selected];
      keepImages();
    } catch (err) {
      if (alive) status.textContent = words(err);
    } finally {
      adding = false;
      send.disabled = running || sending || resolving;
      attach.disabled = sending || !agent?.images;
    }
  }

  attach.addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => {
    addImages([...picker.files]);
    picker.value = '';
  });
  form.addEventListener('paste', (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    addImages(files);
  });
  form.addEventListener('dragover', (event) => {
    if ([...(event.dataTransfer?.types || [])].includes('Files')) event.preventDefault();
  });
  form.addEventListener('drop', (event) => {
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    addImages(files);
  });

  /* ---- лента -------------------------------------------------------------------- */

  function stuck() {
    return !scroller || scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - STICK_PX;
  }

  /** Правка ленты: была внизу — остаётся внизу. */
  function grow(change) {
    const was = stuck();
    change();
    if (was && scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  function stepNode(s) {
    const input = s.input && typeof s.input === 'object' ? s.input : {};
    const tail = FILE_TOOLS.has(s.tool) ? tailOf(input.file_path, agent, project) : null;
    let link = null;
    if (tail) {
      link = el('a', { href: `#/code/${aid}/${pid}`, class: 'code-step-file' }, 'Открыть файл');
      link.addEventListener('click', (event) => {
        event.preventDefault();               // и заголовок шага не раскрывается
        openFile(tail);
      });
    }
    const result = el('pre', { class: 'code-step-result' }, typeof s.text === 'string' ? s.text : '');
    result.hidden = !result.textContent;
    return el('details', { class: `code-step${s.error ? ' error' : ''}`, 'data-id': String(s.id ?? '') },
      el('summary', {}, el('b', { class: 'code-step-tool' }, String(s.tool ?? '')),
        el('span', { class: 'code-step-head' }, preview(input)), link),
      el('pre', { class: 'code-step-input' }, JSON.stringify(input, null, 2)), result);
  }

  function messageNode(m) {
    if (m.role === 'user') return el('div', { class: 'code-msg user' },
      ...(Array.isArray(m.images) ? m.images : []).map((i) => el('img', { class: 'code-sent-image',
        src: `data:${i.mime};base64,${i.data}`, alt: i.name })), typeof m.text === 'string' ? m.text : '');
    const md = el('div', { class: 'md' });
    if (typeof m.text === 'string' && m.text) md.append(renderMarkdown(m.text));
    return el('div', { class: 'code-msg assistant' },
      el('div', { class: 'code-steps' }, ...(Array.isArray(m.steps) ? m.steps : []).map(stepNode)), md);
  }

  function messages(items) {
    return (Array.isArray(items) ? items : []).filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
      .map(messageNode);
  }

  /** Ответ идущего хода — в конце ленты (первое событие хода его заводит). */
  function current() {
    if (!turn) {
      const node = messageNode({ role: 'assistant', text: '', steps: [] });
      turn = { node, steps: node.querySelector('.code-steps'), md: node.querySelector('.md'),
        asks: el('div', { class: 'code-asks' }), text: '' };
      node.append(turn.asks);
      grow(() => feed.append(node));
    }
    return turn;
  }

  function paintText() {
    clearTimeout(paintTimer);
    paintTimer = null;
    if (turn) grow(() => turn.md.replaceChildren(renderMarkdown(turn.text)));
  }

  function setRunning(value) {
    running = value;
    stop.hidden = !value;
    send.disabled = value || sending || adding || resolving;
    host.dataset.running = value ? '1' : '0';
  }

  function paintActivity(state, tool) {
    const labels = { idle: 'Нет активной задачи', thinking: 'Думает…', responding: 'Пишет ответ…',
      waiting: 'Ждёт вашего ответа', done: 'Задача завершена', stopped: 'Ход остановлен',
      error: 'Ошибка выполнения', offline: 'Нет связи с компьютером' };
    const text = state === 'tool' ? `Выполняет: ${String(tool || 'действие')}` : labels[state];
    if (!text) return;
    activity.dataset.state = state;
    activityText.textContent = text;
    activity.title = text;
  }

  function continueActivity() {
    if (turn?.asks.querySelector('.code-ask')) paintActivity('waiting');
    else if (activeTools.size) paintActivity('tool', [...activeTools.values()].at(-1));
    else paintActivity('thinking');
  }

  /** Компьютер закрыт для сессии — к PIN (code.js запомнит этот чат); с чата уже ушли — никуда. */
  function closed() {
    if (alive) toPin();
  }

  /* ---- карточки ----------------------------------------------------------------- */

  function askNode(a) {
    const card = el('div', { class: 'code-ask', 'data-request': String(a.request_id ?? '') });
    const error = el('p', { class: 'error code-ask-error', 'aria-live': 'polite' });
    const buttons = [];

    async function reply(body) {
      acted = Date.now();
      for (const b of buttons) b.disabled = true;
      error.textContent = '';
      try {
        await api('POST', endpoint('/answer'), { request_id: a.request_id, ...body });
      } catch (err) {
        if (err.code === 'gone') card.remove();             // запроса уже нет: ответили в другой вкладке
        else if (err.code === 'closed') closed();
        else {
          for (const b of buttons) b.disabled = false;
          error.textContent = words(err);
        }
      }
    }

    function action(cls, label, body, accent = false) {
      const b = el('button', { type: 'button', class: `btn ${accent ? 'btn-accent ' : ''}${cls}` }, label);
      b.addEventListener('click', () => reply(body()));
      buttons.push(b);
      return b;
    }

    if (a.kind === 'question') {
      const groups = (Array.isArray(a.questions) ? a.questions : []).map((q, i) => {
        const type = q.multi ? 'checkbox' : 'radio';
        const name = `code-ask-${a.request_id}-${i}`;
        const options = (Array.isArray(q.options) ? q.options : []).map((o) => el('label', { class: 'code-ask-option' },
          el('input', { type, name, value: String(o.label ?? '') }), el('span', {}, String(o.label ?? '')),
          o.description ? el('span', { class: 'muted small' }, String(o.description)) : null));
        // «Свой ответ» (в Claude Code — «Other»): написан — уходит вместо отмеченного
        const own = el('input', { type: 'text', class: 'input code-ask-own', maxlength: String(OWN_MAX),
          placeholder: OWN, 'aria-label': OWN, autocomplete: 'off' });
        return { q, own, box: el('fieldset', { class: 'code-ask-question' },
          q.header ? el('legend', {}, String(q.header)) : null, el('p', {}, String(q.question ?? '')), ...options,
          own) };
      });
      const go = el('button', { type: 'button', class: 'btn btn-accent code-ask-answer' }, 'Ответить');
      go.addEventListener('click', () => {
        const answers = {};
        for (const { q, own, box } of groups) {
          const typed = own.value.trim();
          const inputs = [...box.querySelectorAll('input:not(.code-ask-own)')];
          const picked = inputs.filter((i) => i.checked).map((i) => i.value);
          if (!typed && !picked.length) {
            (inputs[0] || own).focus();
            return;
          }
          answers[String(q.question ?? '')] = typed || picked.join(', ');
        }
        reply({ decision: 'allow', answers });
      });
      buttons.push(go);
      card.append(...groups.map((g) => g.box), el('div', { class: 'code-ask-actions' }, go), error);
      return card;
    }

    const instead = el('textarea', { class: 'input code-ask-instead', rows: '2', maxlength: '2000',
      placeholder: INSTEAD, 'aria-label': INSTEAD });
    card.append(el('p', { class: 'code-ask-title' }, `${assistant} просит разрешения`),
      el('b', { class: 'code-ask-tool' }, String(a.tool ?? '')),
      el('pre', { class: 'code-ask-input' }, JSON.stringify(a.input && typeof a.input === 'object' ? a.input : {}, null, 2)),
      el('div', { class: 'code-ask-actions' },
        action('code-ask-yes', 'Да', () => ({ decision: 'allow' }), true),
        a.always ? action('code-ask-always', engine === 'codex' ? 'Да, для этой сессии' : 'Да, больше не спрашивать', () => ({ decision: 'always' })) : null,
        action('code-ask-no', 'Нет', () => ({ decision: 'deny', message: instead.value.trim() || null }))),
      instead, error);
    return card;
  }

  function dropAsk(id) {
    for (const card of feed.querySelectorAll('.code-ask')) {
      if (card.dataset.request === String(id)) card.remove();
    }
  }

  /* ---- события хода ------------------------------------------------------------- */

  function finish(data) {
    paintText();
    if (turn) {
      for (const card of turn.asks.querySelectorAll('.code-ask')) card.remove();
      let end = null;
      if (data.status === 'stopped') end = STOPPED;
      else if (data.status === 'error') end = typeof data.message === 'string' && data.message ? `Ошибка: ${data.message}` : 'Ошибка';
      if (end) {
        const note = el('p', { class: `code-turn-end${data.status === 'error' ? ' error' : ' muted'}` }, end);
        grow(() => turn.node.append(note));
      }
    } else {
      loadHistory(false);        // ход кончился раньше снимка: его ответ — уже в истории
    }
    turn = null;
    activeTools.clear();
    paintActivity(data.status === 'error' ? 'error' : data.status === 'stopped' ? 'stopped' : 'done');
    setRunning(false);
    idlePoll();
  }

  /** Событие потока; true — поток кончился. */
  function handle(event, data) {
    switch (event) {
      case 'turn':
        setRunning(true);
        activeTools.clear();
        paintActivity('thinking');
        current();
        return false;
      case 'idle':
        setRunning(false);
        if (!['done', 'stopped', 'error'].includes(activity.dataset.state)) paintActivity('idle');
        if (turn) {                // ход кончился, пока страница не смотрела: история его уже знает
          turn = null;
          loadHistory(false);
        }
        idlePoll();
        return true;
      case 'snapshot': {
        setRunning(true);
        activeTools.clear();
        const t = current();
        if (data.settings) paintSettings(data.settings);
        paintContext(data.context);
        t.text = typeof data.text === 'string' ? data.text : '';
        paintText();
        grow(() => {
          t.steps.replaceChildren(...(Array.isArray(data.steps) ? data.steps : []).map(stepNode));
          t.asks.replaceChildren(...(Array.isArray(data.asks) ? data.asks : []).map(askNode));
        });
        if (data.activity) paintActivity(data.activity.state, data.activity.tool);
        else continueActivity();
        return false;
      }
      case 'activity':
        if (turn?.asks.querySelector('.code-ask')) paintActivity('waiting');
        else paintActivity(data.state, data.tool);
        return false;
      case 'settings':
        paintSettings(data);
        return false;
      case 'context':
        paintContext(data);
        return false;
      case 'text':
        paintActivity(turn?.asks.querySelector('.code-ask') ? 'waiting' : 'responding');
        current().text += typeof data.text === 'string' ? data.text : '';
        if (!paintTimer) paintTimer = setTimeout(paintText, PAINT_MS);
        return false;
      case 'step': {
        const t = current();
        activeTools.set(String(data.id), data.tool);
        continueActivity();
        grow(() => t.steps.append(stepNode({ id: data.id, tool: data.tool, input: data.input, text: '', error: false })));
        return false;
      }
      case 'result':
        activeTools.delete(String(data.id));
        for (const step of (turn ? turn.steps : feed).querySelectorAll('details.code-step')) {
          if (step.dataset.id !== String(data.id)) continue;
          const result = step.querySelector('.code-step-result');
          result.textContent = typeof data.text === 'string' ? data.text : '';
          result.hidden = !result.textContent;
          step.classList.toggle('error', !!data.error);
        }
        continueActivity();
        return false;
      case 'ask': {
        const t = current();
        dropAsk(data.request_id);
        grow(() => t.asks.append(askNode(data)));
        paintActivity('waiting');
        return false;
      }
      case 'asked':
        dropAsk(data.request_id);
        continueActivity();
        return false;
      case 'done':
        finish(data);
        return true;
      case 'offline':
        waitForComputer();
        return true;
      case 'closed':
        closed();
        return true;
      default:
        return false;
    }
  }

  /* ---- потоки ------------------------------------------------------------------- */

  function drop() {
    if (controller) controller.abort();
    controller = null;
  }

  function take() {
    drop();
    clearTimeout(retry);
    retry = null;
    controller = new AbortController();
    return controller;
  }

  function later(ms) {
    clearTimeout(retry);
    retry = alive && document.visibilityState !== 'hidden' ? setTimeout(openLive, ms) : null;
  }

  /** Хода нет: live снова через IDLE_POLL_SEC — пока владелец недавно что-то делал в чате. */
  function idlePoll() {
    if (Date.now() - acted < IDLE_POLL_FOR_SEC * 1000) later(IDLE_POLL_SEC * 1000);
  }

  function waitForComputer() {
    setRunning(false);
    paintActivity('offline');
    status.textContent = WAITING;
    later(RETRY_MS);
  }

  async function follow(response, own) {
    let ended = false;
    try {
      for await (const { event, data } of readSSE(response)) {
        if (!alive || own.signal.aborted) return;
        ended = handle(event, data && typeof data === 'object' ? data : {});
        if (ended) break;
      }
    } catch {
      // обрыв: свой (страница ушла, новый поток) или сети — ниже
    }
    if (controller === own) controller = null;
    if (!ended && alive && !own.signal.aborted) later(AGAIN_MS);
  }

  async function openLive() {
    if (!alive || document.visibilityState === 'hidden') return;
    const own = take();
    let response;
    try {
      response = await api('GET', endpoint('/live'), undefined, { signal: own.signal });
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      if (controller === own) controller = null;
      if (err.code === 'closed') closed();
      else if (err.code === 'offline') waitForComputer();
      else {
        status.textContent = words(err);
        if (err.code === 'network' || err.status >= 500) later(RETRY_MS);
      }
      return;
    }
    if (status.textContent === WAITING) status.textContent = '';
    await follow(response, own);
  }

  async function loadHistory(thenLive) {
    let page;
    try {
      page = await api('GET', endpoint());
    } catch (err) {
      if (!alive) return;
      if (err.code === 'closed') closed();
      else if (err.status === 404 && err.code === 'not_found') gone();
      else status.textContent = words(err);
      return;
    }
    if (!alive) return;
    if (thenLive && !(Array.isArray(page.items) && page.items.length)) applyDefaults();    // чат без истории — новый
    feed.replaceChildren(...messages(page.items));
    turn = null;
    start = Number.isInteger(page.start) ? page.start : 0;
    more.hidden = start <= 0;
    if (thenLive) await loadState();
    if (!alive) return;
    host.dataset.loaded = '1';
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    if (thenLive) openLive();
  }

  /* ---- действия ----------------------------------------------------------------- */

  function autosize() {
    text.style.height = 'auto';
    if (text.value) text.style.height = `${text.scrollHeight}px`;
  }

  /** Выбор здесь действует в этом чате; умолчания учётки меняются в настройках. */
  function remember() {
    touched = true;
  }

  async function submit() {
    const value = text.value;
    if ((!value.trim() && !images.length) || running || sending || adding || resolving) return;
    const sentImages = images;
    acted = Date.now();
    sending = true;
    attach.disabled = true;
    paintImages();
    send.disabled = true;
    status.textContent = '';
    const mine = messageNode({ role: 'user', text: value, images: sentImages });
    grow(() => feed.append(mine));
    text.value = '';             // черновик (drafts) остаётся до ответа: отказ и уход к PIN его не теряют
    autosize();
    const own = take();          // уход с экрана или с чата этот запрос не обрывает: ход, может, уже принят
    let response;
    try {
      response = await api('POST', endpoint('/turn'), { text: value, model: model.value || null,
        effort: effort.value || null,
        ...(access.value ? { access: access.value } : {}),
        ...(sentImages.length ? { images: sentImages.map(({ mime, data }) => ({ mime, data })) } : {}) }, { signal: own.signal });
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      if (controller === own) controller = null;
      mine.remove();
      if (!text.value) {
        text.value = value;
        autosize();
      }
      if (err.code === 'closed') closed();
      else if (err.code === 'offline') waitForComputer();
      else {
        status.textContent = words(err);
        // ход уже идёт (другая вкладка, VS Code) или, может быть, начат (компьютер не ответил вовремя) — смотреть его
        if (err.code === 'running' || err.code === 'timeout') openLive();
      }
      return;
    } finally {
      sending = false;
      send.disabled = running || adding || resolving;
      attach.disabled = adding || !agent?.images;
      paintImages();
    }
    // ход принят (ответ — поток, первое его событие — turn); набранное после — остаётся
    if (drafts.get(draftKey) === value) drafts.delete(draftKey);
    if (imageDrafts.get(draftKey) === sentImages) imageDrafts.delete(draftKey);
    images = images.filter((i) => !sentImages.includes(i));
    paintImages();
    if (!alive || document.visibilityState === 'hidden') {     // ушли, пока ход принимали: поток не читать
      own.abort();
      if (controller === own) controller = null;
      return;                    // вернулись на экран — live (onVisibility), в чат — его новая страница
    }
    await follow(response, own);
  }

  more.addEventListener('click', async () => {
    more.disabled = true;
    try {
      const page = await api('GET', `${endpoint()}${engine !== 'claude' ? '&' : '?'}before=${start}`);
      if (!alive) return;
      const height = scroller ? scroller.scrollHeight : 0;
      feed.prepend(...messages(page.items));
      if (scroller) scroller.scrollTop += scroller.scrollHeight - height;   // глаза — на прежнем месте
      start = Number.isInteger(page.start) ? page.start : 0;
      more.hidden = start <= 0;
    } catch (err) {
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
    } finally {
      more.disabled = false;
    }
  });

  stop.addEventListener('click', async () => {
    stop.disabled = true;
    try {
      await api('POST', endpoint('/stop'));
    } catch (err) {
      if (err.code === 'closed') closed();
      else if (err.code !== 'idle') status.textContent = words(err);     // idle — ход уже кончился
    } finally {
      stop.disabled = false;
    }
  });

  closeChat.addEventListener('click', async () => {
    closeChat.disabled = true;
    try {
      await api('PATCH', endpoint(), { state: 'closed' });
      if (alive) closedChat();
    } catch (err) {
      closeChat.disabled = false;
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
    }
  });

  model.addEventListener('change', remember);
  effort.addEventListener('change', remember);
  access.addEventListener('change', remember);
  text.addEventListener('input', () => {
    acted = Date.now();
    autosize();
    if (text.value) drafts.set(draftKey, text.value);
    else drafts.delete(draftKey);
  });
  text.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit();
  });

  /* Ушли с экрана — поток долой: пока его нет, сервер шлёт push. Вернулись — live, снимок догонит. */
  function onVisibility() {
    if (document.visibilityState === 'hidden') {
      clearTimeout(retry);
      retry = null;
      if (!sending) drop();      // идущий «Отправить» обрывает сам, узнав, принят ли ход
    } else if (!sending) {
      acted = Date.now();
      openLive();
    }
  }
  document.addEventListener('visibilitychange', onVisibility);

  setRunning(false);
  loadHistory(true);

  return {
    unmount() {
      alive = false;
      stateRequest?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
      if (!sending) drop();      // «Отправить» оборвёт поток сам, сняв черновик принятого хода
      clearTimeout(retry);
      clearTimeout(paintTimer);
    },
  };
}
