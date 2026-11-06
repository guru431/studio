/* Чат Claude Code (#/code/<aid>/<pid>/<chat>) — спека 2026-10-28-studio-claude-code-design 6.6 и 6.10,
 * протокол канала §4 (history, turn, watch, answer, stop) и §7 (события).
 *
 * chatView(host, { aid, pid, chat, agent, project, origin, fresh, closed, gone, openFile }) → { unmount() }:
 * - закрытие — крестик вкладки; история отвечает 404
 *   not_found (сессии на машине нет, студия её не заводила) — gone() вместо слов (вкладка уходит сама);
 * - лента #code-feed — хвост 12 сообщений (GET …/chats/<chat>), «Показать раньше» (#code-more) —
 *   страницы по 30 сообщений;
 *   предыдущая (before = start); сообщения — .code-msg.user (текст как есть) и .code-msg.assistant (шаги
 *   details.code-step, затем текст — markdown.js);
 * - сразу за историей — поток live: idle — ничего, snapshot — всё состояние идущего хода вместо прежнего
 *   хвоста; «Отправить» — поток turn; события text, step, result, ask (карточка .code-ask; у вопроса — ещё
 *   «Свой ответ»), asked (карточка уходит), done (stopped — «Ход прерван», error — «Ошибка: …» или «Ошибка»;
 *   done раньше снимка — ход кончился между watch и снимком: история заново);
 * - хода нет и страница видна — live раз в IDLE_POLL_SEC: ход, начатый в другой вкладке, на другом устройстве
 *   или в VS Code, страница увидит; не дольше IDLE_POLL_FOR_SEC после действия владельца в чате — опрос
 *   продлевает открытость компьютера (протокол §3), и забытая вкладка держала бы его открытым вечно;
 * - во время хода — steer; чужое окно сначала забирает управление через claim. Codex/Claude получают
 *   уточнение в текущем клиенте, OpenCode — очередь до конца ответа. Отказ turn 409 running и 504 timeout — live;
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
import { api as request, me } from './api.js';
import { ACCESS, ENGINE_NAMES, accessLabel, effortLabel } from './code-options.js';
import { fieldBox } from './field.js';
import { renderMarkdown, renderMarkdownInto, renderNodesInto } from './markdown.js';
import { readSSE } from './sse.js';
import { el, focusDesktop, words } from './ui.js';
import { usageView } from './code-usage.js';
import { contextView } from './code-context.js';
import { changeView } from './code-diff.js';
import { fileHints } from './code-autocomplete.js';
import { queueView } from './code-queue.js';
import { subagentView } from './code-subagents.js';

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
const IMAGE_COUNT = 8;

/** Черновики поля задания: `<aid>/<chat>` → текст; только память страницы. */
const drafts = new Map();
const imageDrafts = new Map();  // картинки черновика — только память, как текст
const historyCache = new Map(); // последние 12 историй, только память этой страницы
document.addEventListener('studio-code-clear', () => historyCache.clear());
const pendingInputs = new Map(); // до подтверждения, что CLI принял исходное задание
const queuedInputs = new Map(); // принятые OpenCode уточнения до передачи в CLI
const WINDOW = crypto.randomUUID().replaceAll('-', '');

function attachmentBody(items) {
  const images = items.filter((i) => IMAGE_TYPES.has(i.mime)).map(({ mime, data }) => ({ mime, data }));
  const attachments = items.filter((i) => !IMAGE_TYPES.has(i.mime)).map(({ name, mime, data }) => ({ name, mime, data }));
  return { ...(images.length ? { images } : {}), ...(attachments.length ? { attachments } : {}) };
}

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

export function chatView(host, { aid, pid, chat, agent, project, origin, fresh = false, engine = 'claude', closed: toPin, gone,
  openFile, git, gitAction = null, taskDone, forked, verify = null, onReady = null, title = null }) {
  const api = (method, path, body, options = {}) => request(method, path, body,
    { ...options, headers: { 'x-studio-window': WINDOW } });
  const url = `/api/code/agents/${aid}/projects/${pid}/chats/${chat}`;
  const endpoint = (tail = '') => `${url}${tail}${engine !== 'claude' ? `?engine=${engine}` : ''}`;
  const assistant = ENGINE_NAMES[engine];
  const draftKey = `${aid}/${chat}`;
  const historyKey = `${me.id}/${aid}/${pid}/${engine}/${chat}`;
  const scroller = document.getElementById('view');
  let alive = true;
  let touched = false;           // выбор модели или усилия меняли в этом чате
  let controller = null;         // поток (turn или live), который читает страница
  let retry = null;              // таймер следующего live
  let running = false;
  let sending = false;
  let resolving = true;
  let historyLoading = true;
  let settingsReady = !agent?.chat_state;
  let stateError = null;
  let stateRequest = null;
  let adding = false;
  let images = imageDrafts.get(draftKey) || [];
  let turn = null;               // ответ идущего хода: { node, steps, md, asks, text }
  let start = 0;                 // номер первого показанного сообщения истории
  let paintTimer = null;
  let acted = Date.now();        // последнее действие владельца в чате (открыл, вернулся, ввёл, ответил)
  let foreign = false;
  let external = false;
  let waitingForRelease = false;
  let claimTimer = null;
  let claiming = false;
  let historyEmpty = false;
  const stepData = new WeakMap();
  const pendingPaints = new Set();
  let historyRevision = 0;
  let attention = null;
  let markingRead = false;
  let interactions = 0;
  const interacted = () => { interactions += 1; };
  for (const event of ['pointerdown', 'wheel', 'keydown']) scroller?.addEventListener(event, interacted, { passive: true });
  const followups = new Map();
  const sentFollowups = new Set();
  const unsentFollowups = new Set();

  const status = el('p', { id: 'code-status', class: 'error', 'aria-live': 'polite' });
  function showStatus(message, error = true) {
    status.classList.toggle('error', error);
    status.classList.toggle('muted', !error);
    status.textContent = message;
  }
  const more = el('button', { type: 'button', id: 'code-more', class: 'btn btn-sm code-more', hidden: true },
    'Показать раньше');
  const feed = el('div', { id: 'code-feed', class: 'code-feed' });
  const previous = el('button', { type: 'button', id: 'code-message-up', class: 'icon-btn',
    'aria-label': 'Предыдущее моё сообщение', title: 'Предыдущее моё сообщение', disabled: true });
  const next = el('button', { type: 'button', id: 'code-message-down', class: 'icon-btn',
    'aria-label': 'Следующее моё сообщение', title: 'Следующее моё сообщение', disabled: true });
  previous.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
  next.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6"/></svg>';
  const navigation = el('nav', { class: 'code-message-nav', 'aria-label': 'Мои сообщения', hidden: true }, previous, next);
  let messageTarget = null;
  let messageScrollTop = 0;
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
  const delivery = el('select', { id: 'code-delivery', class: 'chip code-delivery',
    'aria-label': 'Когда передать сообщение' },
    el('option', { value: 'steer' }, 'Уточнить сейчас'), el('option', { value: 'queue' }, 'После текущей задачи'));
  const context = contextView(host, { chat, engine, project,
    messages: () => feed.querySelectorAll('.code-msg').length });
  const contextBox = context.button;
  const usage = usageView(host, { aid, engine, supported: agent?.subscription_usage });
  let hasGit = false;
  const unsubscribeGit = git?.subscribe(({ data }) => {
    hasGit = !!data;
  });

  /** Умолчания учётки — в новый чат, пока свой выбор не тронут. */
  function applyDefaults() {
    if (touched) return;
    const saved = me?.code_defaults?.[engine] || {};
    if (models.some((m) => String(m.id) === saved.model)) model.value = saved.model;
    if (efforts.some((e) => String(e) === saved.effort)) effort.value = saved.effort;
    if (ACCESS[engine][saved.access]) access.value = saved.access;
    if ((saved.model && !model.value) || (saved.effort && !effort.value)) {
      showStatus('Часть умолчаний недоступна на этом компьютере — выберите модель и усилие в чате');
    }
    paintPicks();
  }

  if (fresh) applyDefaults();
  const send = el('button', { type: 'submit', id: 'code-send', class: 'btn btn-accent composer-go',
    'aria-label': 'Отправить', title: 'Отправить' }, el('span', { class: 'code-go-label' }, 'Отправить'));
  const stop = el('button', { type: 'button', id: 'code-stop', class: 'btn composer-go', hidden: true,
    'aria-label': 'Остановить', title: 'Остановить' }, el('span', { class: 'code-go-label' }, 'Остановить'));
  send.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>');
  stop.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>');
  const activityText = el('span', {}, 'Нет активной задачи');
  const activity = el('div', { id: 'code-activity', class: 'code-activity', role: 'status',
    'aria-live': 'polite', 'aria-atomic': 'true', 'data-state': 'idle' },
    el('span', { class: 'code-activity-dot', 'aria-hidden': 'true' }), activityText);
  const activeTools = new Map();
  const actions = el('div', { class: 'code-chat-actions' });
  const statusBar = el('div', { class: 'code-status-bar' }, activity, actions, contextBox, usage.button);
  const quickActions = el('section', { id: 'code-quick-actions', class: 'code-quick-actions', hidden: true,
    'aria-label': 'Быстрые действия' }, el('h2', {}, 'Быстрые действия'));
  for (const action of me?.code_quick_actions || []) {
    const button = el('button', { type: 'button', class: 'btn code-quick-action', title: action.title }, action.title);
    button.addEventListener('click', () => {
      if (quickActions.hidden || send.disabled) return;
      text.value = action.text;
      text.dispatchEvent(new Event('input', { bubbles: true }));
      form.requestSubmit();
    });
    quickActions.append(button);
  }
  const field = fieldBox(text);
  const attachments = el('div', { id: 'code-images', class: 'code-images' });
  const picker = el('input', { type: 'file', id: 'code-image-input',
    multiple: true, hidden: true, 'aria-label': 'Вложения' });
  const attach = el('button', { type: 'button', id: 'code-attach', class: 'icon-btn', 'aria-label': 'Добавить вложение',
    title: agent?.attachments ? 'Прикрепить файлы любого типа · до 8 файлов, 5 МиБ на файл, 8 МиБ всего'
      : 'Изображения · обновите мост для файлов любого типа', disabled: !agent?.images && !agent?.attachments });
  attach.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 13 6.5-6.5a3 3 0 0 1 4.2 4.2L10 19.4a5 5 0 0 1-7.1-7.1L12 3.2m-6.2 11 8.7-8.7"/></svg>';
  field.box.querySelector('.field-tools').insertBefore(attach, field.box.querySelector('.mic'));
  const sessionText = el('p');
  const waitSession = el('button', { type: 'button', id: 'code-wait-session', class: 'btn btn-sm' }, 'Дождаться завершения');
  const takeSession = el('button', { type: 'button', id: 'code-take-session', class: 'btn btn-sm' }, 'Продолжить здесь');
  const sessionNotice = el('div', { id: 'code-session-notice', class: 'code-session-notice', hidden: true, role: 'status' },
    sessionText, el('div', { class: 'code-session-actions' }, waitSession, takeSession));
  const pick = (label, select) => {
    select.setAttribute('aria-label', label);
    return el('label', { class: 'code-pick chip' }, el('span', { class: 'code-pick-name' }, label),
      el('span', { class: 'code-pick-value', 'aria-hidden': 'true' }, select.selectedOptions[0].textContent), select);
  };
  const form = el('form', { class: 'composer code-composer', id: 'code-composer' }, attachments, picker, field.box, field.error,
    el('div', { class: 'composer-bar' },
      el('div', { class: 'composer-opts' },
        pick('Модель', model), pick('Усилие', effort), pick('Права', access)),
      stop, send));
  host.dataset.engine = engine;
  host.append(
    ...(origin === 'other' ? [el('p', { id: 'code-shared-note', class: 'code-shared-note' }, SHARED)] : []),
    more, feed, quickActions, status, el('div', { class: 'code-compose-area' }, navigation, sessionNotice, statusBar, form));
  const queueHost = el('div', { hidden: true });
  form.before(queueHost);
  const queue = queueView(queueHost, { endpoint, request: api, failed: (err) => {
    if (err.code === 'closed') closed();
    else showStatus(words(err));
  } });
  if (agent?.chat_fork && engine !== 'opencode') {
    const copy = el('button', { type: 'button', class: 'icon-btn code-fork', id: 'code-fork-chat',
      'aria-label': 'Создать ветку чата', title: 'Создать ветку чата · копия переписки, файлы в текущем состоянии' });
    copy.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m12-10c0 6-12 3-12 10"/></svg>';
    copy.addEventListener('click', () => forkAt(null));
    actions.append(copy);
  }
  const children = agent?.subagents && engine !== 'opencode' ? subagentView(host, { endpoint }) : null;
  if (children) actions.append(children.button);
  const deliveryPick = pick('Когда передать сообщение', delivery);
  deliveryPick.hidden = true;
  access.closest('.code-pick').after(deliveryPick);
  delivery.addEventListener('change', paintPicks);
  const hintsHost = el('div', { hidden: true });
  form.insertBefore(hintsHost, field.box);
  const hints = agent?.file_suggestions ? fileHints(text, hintsHost, { aid, pid, failed: (err) => {
    if (err.code === 'closed') closed();
    else showStatus(words(err));
  } }) : null;
  if (drafts.has(draftKey)) {
    text.value = drafts.get(draftKey);     // после fieldBox: «очистить» видит текст
    autosize();
  }
  paintImages();

  function paintPicks() {
    for (const select of [model, effort, access, delivery]) {
      const label = select.closest('.code-pick');
      if (!label) continue;
      const value = select.selectedOptions[0]?.textContent || DEFAULT;
      label.querySelector('.code-pick-value').textContent = value;
      label.title = value;
    }
  }

  function paintSettings(data) {
    context.settings(data);
    model.options[0].textContent = typeof data.model === 'string' && data.model ? data.model : 'Не получено';
    model.title = typeof data.model === 'string' ? data.model : '';
    effort.options[0].textContent = effortLabel(data.effort);
    access.options[0].textContent = accessLabel(engine, data.access);
    if (model.value && typeof data.model === 'string' && data.model) model.selectedOptions[0].textContent = data.model;
    paintPicks();
  }

  function paintContext(data) {
    context.update(data);
  }

  function acceptSettings(data) {
    paintSettings(data);
    settingsReady = true;
    if (stateError && status.textContent === stateError) showStatus('');
    stateError = null;
    updateControls();
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
      acceptSettings(data.settings || {});
      paintContext(data.context);
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      paintSettings({});
      settingsReady = !agent?.chat_state;
      if (err.code === 'closed') closed();
      else { stateError = words(err); showStatus(stateError); }
    } finally {
      resolving = false;
      stateRequest = null;
      model.disabled = effort.disabled = access.disabled = false;
      updateControls();
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
      return el('div', { class: `code-image${IMAGE_TYPES.has(image.mime) ? '' : ' code-file'}`, title: image.name },
        IMAGE_TYPES.has(image.mime) ? el('img', { src: `data:${image.mime};base64,${image.data}`, alt: image.name })
          : el('span', { class: 'code-file-name' }, image.name), remove);
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
    if (!agent?.images && !agent?.attachments) {
      showStatus('Обновите мост на компьютере для отправки изображений');
      return;
    }
    adding = true;
    send.disabled = true;
    attach.disabled = true;
    showStatus('');
    try {
      if (images.length + files.length > IMAGE_COUNT) throw new Error('Можно прикрепить до 8 файлов');
      if (!agent?.attachments && files.some((f) => !IMAGE_TYPES.has(f.type))) {
        throw new Error('Обновите мост на компьютере для отправки файлов любого типа');
      }
      if (files.some((f) => f.size > IMAGE_MAX)) throw new Error('Размер файла — до 5 МиБ');
      if ([...images, ...files].filter((f) => IMAGE_TYPES.has(f.mime || f.type)).length > 4) {
        throw new Error('Можно прикрепить до 4 изображений за один раз');
      }
      if ([...images, ...files].reduce((sum, f) => sum + f.size, 0) > IMAGES_MAX) {
        throw new Error('Общий размер вложений — до 8 МиБ');
      }
      const selected = await Promise.all(files.map((file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({ mime: file.type || 'application/octet-stream', data: String(reader.result).split(',')[1] || '',
          name: file.name || 'Вложение', size: file.size });
        reader.onerror = () => reject(new Error('Не получилось прочитать изображение'));
        reader.readAsDataURL(file);
      })));
      // Даже после ухода к PIN черновик остаётся у своего чата.
      images = [...images, ...selected];
      keepImages();
    } catch (err) {
      if (alive) showStatus(words(err));
    } finally {
      adding = false;
      updateControls();
      attach.disabled = sending || (!agent?.images && !agent?.attachments);
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

  /** Якорь — верх ленты под прилипшей шапкой. Сохраняем выбранное сообщение даже у конца прокрутки. */
  function messageNeighbours() {
    const users = [...feed.querySelectorAll('.code-msg.user')];
    if (messageTarget && (!feed.contains(messageTarget) || Math.abs(scroller.scrollTop - messageScrollTop) > 2)) {
      messageTarget = null;
    }
    const index = users.indexOf(messageTarget);
    if (index >= 0) return { before: users[index - 1], after: users[index + 1] };
    if (stuck()) return { before: users.at(-1), after: null };
    const top = messageTop();
    return { before: users.findLast((node) => node.getBoundingClientRect().top < top - 2),
      after: users.find((node) => node.getBoundingClientRect().top > top + 2) };
  }

  function messageTop() {
    const header = host.closest('.code-main')?.querySelector('.code-top');
    const scale = scroller.getBoundingClientRect().height / scroller.offsetHeight;
    return Math.max(scroller.getBoundingClientRect().top, header?.getBoundingClientRect().bottom || 0) + 12 * scale;
  }

  function paintNavigation() {
    if (!scroller || historyLoading) return;
    const { before, after } = messageNeighbours();
    navigation.hidden = !feed.querySelector('.code-msg.user') && start <= 0;
    previous.disabled = more.disabled || (!before && start <= 0);
    next.disabled = !after && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    const nextLabel = after ? 'Следующее моё сообщение' : 'В конец чата';
    next.setAttribute('aria-label', nextLabel);
    next.title = nextLabel;
  }

  async function jumpMessage(direction) {
    interacted();
    acted = Date.now();
    let target = messageNeighbours()[direction < 0 ? 'before' : 'after'];
    while (!target && direction < 0 && start > 0) {
      const first = feed.querySelector('.code-msg.user');
      if (!await loadEarlier() || !alive) return;
      const users = [...feed.querySelectorAll('.code-msg.user')];
      target = first ? users[users.indexOf(first) - 1] : users.at(-1);
    }
    if (!scroller) return;
    if (!target) {
      if (direction > 0) {
        messageTarget = null;
        scroller.scrollTop = scroller.scrollHeight;
        paintNavigation();
      }
      return;
    }
    messageTarget = target;
    const scale = scroller.getBoundingClientRect().height / scroller.offsetHeight;
    scroller.scrollTop += (target.getBoundingClientRect().top - messageTop()) / scale;
    messageScrollTop = scroller.scrollTop;
    paintNavigation();
  }

  /** Правка ленты: была внизу — остаётся внизу. */
  function grow(change) {
    const was = !messageTarget && stuck();
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
    const details = el('details', { class: `code-step${s.error ? ' error' : ''}`, 'data-id': String(s.id ?? '') },
      el('summary', {}, el('b', { class: 'code-step-tool' }, String(s.tool ?? '')),
        el('span', { class: 'code-step-head' }, preview(input)), link));
    stepData.set(details, s);
    let filled = false;
    details.addEventListener('toggle', () => {
      if (!details.open || filled) return;
      filled = true;
      const result = el('pre', { class: 'code-step-result' }, typeof s.text === 'string' ? s.text : '');
      result.hidden = !result.textContent;
      const changes = !s.error ? changeView(s.changes) : null;
      details.append(el('pre', { class: 'code-step-input', hidden: !!changes }, JSON.stringify(input, null, 2)), changes || '', result);
    });
    return details;
  }

  function messageNode(m) {
    if (m.role === 'user') {
      const branch = agent?.chat_fork && engine !== 'opencode' && m.forkable !== false && typeof m.id === 'string'
        ? el('button', { type: 'button', class: 'btn btn-sm code-fork', disabled: running,
          title: 'Создать ветку до этого сообщения. Текст задания будет скопирован; вложения прикрепите заново.' }, 'Продолжить иначе') : null;
      branch?.addEventListener('click', () => forkAt(m.id));
      return el('div', { class: 'code-msg user' },
      ...(Array.isArray(m.images) ? m.images : []).map((i) => IMAGE_TYPES.has(i.mime)
        ? el('img', { class: 'code-sent-image', src: `data:${i.mime};base64,${i.data}`, alt: i.name })
        : el('span', { class: 'code-sent-file' }, i.name)), typeof m.text === 'string' ? m.text : '', branch);
    }
    const md = el('div', { class: 'md' });
    if (typeof m.text === 'string' && m.text) {
      // История выставляет прокрутку после всей страницы; измерять её после каждого блока дорого.
      const paint = renderMarkdownInto(md, m.text, { valid: () => alive && feed.contains(md) });
      pendingPaints.add(paint);
      paint.then(() => pendingPaints.delete(paint));
    }
    const steps = el('div', { class: 'code-steps' });
    const items = Array.isArray(m.steps) ? m.steps : [];
    if (items.length > 8) {
      const paint = renderNodesInto(steps, items, stepNode, { valid: () => alive && feed.contains(steps) });
      pendingPaints.add(paint);
      paint.then(() => pendingPaints.delete(paint));
    } else steps.append(...items.map(stepNode));
    return el('div', { class: 'code-msg assistant' }, steps, md);
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

  function paintText(final = false) {
    clearTimeout(paintTimer);
    paintTimer = null;
    if (!turn) return;
    const target = turn.md;
    if (final) renderMarkdownInto(target, turn.text, { valid: () => alive && feed.contains(target), update: grow });
    else if (turn.text.length < 2000) grow(() => target.replaceChildren(renderMarkdown(turn.text, { highlight: false })));
    else grow(() => target.replaceChildren(el('pre', { class: 'code-stream-text' }, turn.text)));
  }

  function setRunning(value) {
    running = value;
    stop.hidden = !value;
    host.dataset.running = value ? '1' : '0';
    updateControls();
  }

  function updateControls() {
    send.disabled = sending || adding || resolving || historyLoading || !settingsReady || foreign || external || queue.paused() || (running && !agent?.chat_control);
    stop.disabled = foreign || external;
    deliveryPick.hidden = !running || !agent?.queue_edit || engine === 'opencode';
    delivery.disabled = foreign || sending;
    queue.ownership(foreign || external);
    for (const button of host.querySelectorAll('.code-fork')) button.disabled = running || sending || resolving || historyLoading || !settingsReady;
    paintQuickActions();
    model.disabled = effort.disabled = access.disabled = resolving || historyLoading || running;
    const label = running ? (engine === 'opencode' ? 'Отправить после ответа' : 'Отправить уточнение') : 'Отправить';
    send.title = label;
    send.setAttribute('aria-label', label);
    send.querySelector('.code-go-label').textContent = label;
    text.placeholder = running ? 'Уточнение…' : 'Задание…';
    for (const button of feed.querySelectorAll('.code-ask button')) button.disabled = foreign;
    sessionNotice.hidden = !foreign && !external;
    if (external) {
      sessionText.textContent = waitingForRelease
        ? 'Жду освобождения сессии в Codex Desktop или другом приложении. Текст и вложения сохранены.'
        : 'Сессия занята в Codex Desktop или другом приложении. Дождитесь её освобождения или продолжите копию с той же историей. Копия и исходная сессия смогут работать независимо.';
      takeSession.textContent = 'Продолжить копией';
      waitSession.textContent = 'Дождаться освобождения';
    } else if (foreign) {
      sessionText.textContent = waitingForRelease ? 'Жду завершения задания в другом окне. Результат появится здесь.'
        : 'Задание выполняется в другом окне или браузере. Можно дождаться результата или перенести управление сюда.';
      takeSession.textContent = 'Продолжить здесь';
      waitSession.textContent = 'Дождаться завершения';
    }
    waitSession.disabled = waitingForRelease || claiming;
    takeSession.disabled = claiming;
  }

  function paintQuickActions() {
    quickActions.hidden = !historyEmpty || running || sending || !!feed.querySelector('.code-msg')
      || quickActions.children.length <= 1;
    for (const button of quickActions.querySelectorAll('button')) button.disabled = send.disabled;
  }

  function acknowledgeInput() {
    const pending = pendingInputs.get(draftKey);
    if (!pending) return;
    historyEmpty = false;
    if (pending.quick) { pendingInputs.delete(draftKey); return; }
    if (drafts.get(draftKey) === pending.value) drafts.delete(draftKey);
    if (imageDrafts.get(draftKey) === pending.images) imageDrafts.delete(draftKey);
    pendingInputs.delete(draftKey);
    if (text.value === pending.value) { text.value = ''; autosize(); }
    images = images.filter((i) => !pending.images.includes(i));
    paintImages();
  }

  function restoreInput() {
    const pending = pendingInputs.get(draftKey);
    if (!pending) return;
    pendingInputs.delete(draftKey);
    pending.mine?.remove();
    if (pending.quick) return;
    text.value = text.value && text.value !== pending.value ? `${pending.value}\n\n${text.value}` : pending.value;
    if (text.value) drafts.set(draftKey, text.value);
    images = [...pending.images, ...images.filter((i) => !pending.images.includes(i))];
    keepImages();
    autosize();
  }

  function showFollowup(data) {
    if (data.sent) { sentFollowups.add(data.id); queuedInputs.get(draftKey)?.delete(data.id); }
    if (agent?.queue_edit && data.mode === 'queue' && !data.sent && !sentFollowups.has(data.id)) queue.add(data);
    if (typeof data.id !== 'string' || followups.has(data.id)) return;
    const node = messageNode({ role: 'user', text: data.text });
    node.dataset.followup = data.id;
    followups.set(data.id, node);
    grow(() => feed.append(node));
  }

  async function forkAt(messageId) {
    if (running || sending) return;
    sending = true;
    updateControls();
    try {
      const data = await api('POST', endpoint('/fork'), { message_id: messageId });
      if (!alive) return;
      if (data.draft) drafts.set(`${aid}/${data.chat}`, data.draft);
      forked?.(data);
    } catch (err) {
      if (alive) {
        if (err.code === 'closed') closed();
        else showStatus(words(err));
      }
    } finally {
      sending = false;
      if (alive) updateControls();
    }
  }

  async function claimSession(fork = false) {
    if (!alive || claiming || document.visibilityState === 'hidden') return;
    claiming = true;
    updateControls();
    try {
      const result = await api('POST', endpoint('/claim'), { fork });
      if (!alive) return;
      if (result.available === false) {
        external = true;
        if (waitingForRelease) claimTimer = setTimeout(() => claimSession(), RETRY_MS);
      } else {
        const wasExternal = external;
        foreign = external = waitingForRelease = false;
        clearTimeout(claimTimer);
        if (wasExternal) {
          showStatus(result.forked ? 'Открыта копия с историей сессии. Задание можно отправить.'
            : 'Сессия свободна. Задание можно отправить.', false);
          await loadHistory(true);
        }
      }
    } catch (err) {
      if (!alive) return;
      if (err.code === 'closed') closed();
      else showStatus(words(err));
      waitingForRelease = false;
    } finally {
      claiming = false;
      if (alive) updateControls();
    }
  }

  async function markRead() {
    if (!attention || markingRead || !alive || historyLoading || !stuck()
        || document.visibilityState !== 'visible' || document.documentElement.classList.contains('shrouded')) return;
    const value = attention;
    markingRead = true;
    try {
      await api('POST', endpoint('/seen'), { attention: value });
      document.dispatchEvent(new CustomEvent('studio-code-seen', { detail: { aid: Number(aid), chat, attention: value } }));
      if (attention === value) attention = null;
    } catch {
      // Сбой сети оставляет результат непрочитанным до следующего просмотра.
    } finally {
      markingRead = false;
      if (attention && attention !== value) markRead();
    }
  }

  function paintActivity(state, tool, completed = null) {
    const labels = { idle: 'Нет активной задачи', thinking: 'Думает…', responding: 'Пишет ответ…',
      waiting: 'Ждёт вашего ответа', done: 'Задача завершена', stopped: 'Ход остановлен',
      error: 'Ошибка выполнения', offline: 'Нет связи с компьютером' };
    const text = state === 'tool' ? `Выполняет: ${String(tool || 'действие')}` : labels[state];
    if (!text) return;
    activity.dataset.state = state;
    activityText.textContent = text;
    activity.title = text;
    document.dispatchEvent(new CustomEvent('studio-code-activity', { detail: { aid: Number(aid), pid, chat,
      engine, ...(title ? { title } : {}),
      running: ['thinking', 'responding', 'tool', 'waiting'].includes(state), asking: state === 'waiting',
      ...(completed ? { attention: completed, unread: true } : {}) } }));
  }

  function continueActivity() {
    if (turn?.asks.querySelector('.code-ask')) paintActivity('waiting');
    else if (activeTools.size) paintActivity('tool', [...activeTools.values()].at(-1));
    else paintActivity('thinking');
  }

  /** Компьютер закрыт для сессии — к PIN (code.js запомнит этот чат); с чата уже ушли — никуда. */
  function closed() {
    historyCache.clear();
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
        const offered = Array.isArray(q.options) ? q.options : [];
        const identities = offered.length > 0 && offered.every((o) => typeof o.id === 'string' && o.id);
        const options = offered.map((o) => el('label', { class: 'code-ask-option' },
          el('input', { type, name, value: String(identities ? o.id : o.label ?? '') }), el('span', {}, String(o.label ?? '')),
          o.description ? el('span', { class: 'muted small' }, String(o.description)) : null));
        // «Свой ответ» (в Claude Code — «Other»): написан — уходит вместо отмеченного
        const own = el('input', { type: 'text', class: 'input code-ask-own', maxlength: String(OWN_MAX),
          placeholder: OWN, 'aria-label': OWN, autocomplete: 'off' });
        return { q, own, identities, box: el('fieldset', { class: 'code-ask-question' },
          q.header ? el('legend', {}, String(q.header)) : null, el('p', {}, String(q.question ?? '')), ...options,
          own) };
      });
      const go = el('button', { type: 'button', class: 'btn btn-accent code-ask-answer' }, 'Ответить');
      go.addEventListener('click', () => {
        const answers = Object.create(null);
        for (const { q, own, identities, box } of groups) {
          const typed = own.value.trim();
          const inputs = [...box.querySelectorAll('input:not(.code-ask-own)')];
          const picked = inputs.filter((i) => i.checked).map((i) => i.value);
          if (!typed && !picked.length) {
            (inputs[0] || own).focus();
            return;
          }
          answers[String(q.id || q.question || '')] = typed || (identities ? picked : picked.join(', '));
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
    historyCache.delete(historyKey);
    for (const item of Array.isArray(data.unsent) ? data.unsent : []) {
      unsentFollowups.add(item.id);
      const queued = queuedInputs.get(draftKey)?.get(item.id);
      const value = queued?.value ?? item.text;
      if (value && text.value !== value) text.value = text.value ? `${text.value}\n\n${value}` : value;
      if (queued) images = [...images, ...queued.images.filter((i) => !images.includes(i))];
      queuedInputs.get(draftKey)?.delete(item.id);
    }
    if (data.unsent?.length) {
      if (text.value) drafts.set(draftKey, text.value);
      keepImages();
      autosize();
      showStatus('Очередь не выполнена — уточнения возвращены в поле', false);
    }
    if (data.not_started) restoreInput();
    else acknowledgeInput();
    if (data.code === 'writer_active') external = true;
    foreign = false;
    if (!external) waitingForRelease = false;
    paintText(true);
    if (turn) {
      attention = data.attention || null;
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
    paintActivity(data.status === 'error' ? 'error' : data.status === 'stopped' ? 'stopped' : 'done', null, data.attention);
    Promise.all([...pendingPaints]).then(markRead);
    setRunning(false);
    taskDone?.();
    usage.refresh();
    queue.clear();
    updateControls();
    idlePoll();
  }

  /** Событие потока; true — поток кончился. */
  function handle(event, data) {
    switch (event) {
      case 'turn':
        foreign = false;
        setRunning(true);
        activeTools.clear();
        paintActivity('thinking');
        current();
        return false;
      case 'idle':
        foreign = false;
        queue.clear();
        setRunning(false);
        if (!['done', 'stopped', 'error'].includes(activity.dataset.state)) paintActivity('idle');
        if (turn) {                // ход кончился, пока страница не смотрела: история его уже знает
          turn = null;
          loadHistory(false);
        }
        idlePoll();
        return true;
      case 'snapshot': {
        foreign = !!data.owner && data.owner !== WINDOW;
        if (data.owner === WINDOW && data.accepted) acknowledgeInput();
        setRunning(true);
        activeTools.clear();
        const t = current();
        if (data.settings) acceptSettings(data.settings);
        paintContext(data.context);
        t.text = typeof data.text === 'string' ? data.text : '';
        paintText();
        grow(() => {
          t.steps.replaceChildren(...(Array.isArray(data.steps) ? data.steps : []).map(stepNode));
          t.asks.replaceChildren(...(Array.isArray(data.asks) ? data.asks : []).map(askNode));
        });
        if (data.activity) paintActivity(data.activity.state, data.activity.tool);
        else continueActivity();
        for (const item of Array.isArray(data.followups) ? data.followups : []) showFollowup(item);
        if (agent?.queue_edit) queue.update(data.queue || [], data.queue_paused === true);
        updateControls();
        return false;
      }
      case 'input_accepted':
        acknowledgeInput();
        return false;
      case 'followup':
        showFollowup(data);
        return false;
      case 'followup_sent':
        sentFollowups.add(data.id);
        queuedInputs.get(draftKey)?.delete(data.id);
        queue.remove(data.id);
        return false;
      case 'followup_removed':
        followups.get(data.id)?.remove();
        followups.delete(data.id);
        queuedInputs.get(draftKey)?.delete(data.id);
        queue.remove(data.id);
        return false;
      case 'queue':
        queue.update(data.items, data.paused === true);
        updateControls();
        for (const item of data.items || []) {
          const node = followups.get(item.id);
          if (node) node.textContent = item.text;
          const saved = queuedInputs.get(draftKey)?.get(item.id);
          if (saved) saved.value = item.input_text ?? item.text;
        }
        return false;
      case 'ownership':
        foreign = !!data.owner && data.owner !== WINDOW;
        waitingForRelease = false;
        updateControls();
        return false;
      case 'activity':
        if (turn?.asks.querySelector('.code-ask')) paintActivity('waiting');
        else paintActivity(data.state, data.tool);
        return false;
      case 'settings':
        acceptSettings(data);
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
          Object.assign(stepData.get(step) || {}, { text: data.text, error: data.error, changes: data.changes });
          step.classList.toggle('error', !!data.error);
          const result = step.querySelector('.code-step-result');
          if (!result) continue;
          result.textContent = typeof data.text === 'string' ? data.text : '';
          result.hidden = !result.textContent;
          step.classList.toggle('error', !!data.error);
          step.querySelector('.code-changes')?.remove();
          step.querySelector('.code-step-input').hidden = false;
          if (!data.error) {
            const changes = changeView(data.changes);
            if (changes) {
              result.before(changes);
              step.querySelector('.code-step-input').hidden = true;
            }
          }
        }
        continueActivity();
        return false;
      case 'ask': {
        const t = current();
        dropAsk(data.request_id);
        grow(() => t.asks.append(askNode(data)));
        paintActivity('waiting');
        updateControls();
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
    showStatus(WAITING);
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
        showStatus(words(err));
        if (err.code === 'network' || err.status >= 500) later(RETRY_MS);
      }
      return;
    }
    if (status.textContent === WAITING) showStatus('');
    await follow(response, own);
  }

  async function loadHistory(thenLive, preserveScroll = false) {
    const previousScroll = preserveScroll ? scroller?.scrollTop : 0;
    const followed = !preserveScroll || stuck();
    const revision = ++historyRevision;
    const snapshot = thenLive && historyCache.get(historyKey);
    const cached = snapshot?.page;
    if (cached) {
      feed.replaceChildren(...messages(cached.items));
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
      const action = interactions;
      Promise.all([...pendingPaints]).then(() => {
        if (alive && revision === historyRevision && action === interactions && historyLoading && scroller) {
          scroller.scrollTop = scroller.scrollHeight;
          paintNavigation();
        }
      });
    }
    let page;
    let settings = null;
    try {
      if (thenLive && verify) await verify;
      if (!alive) return;
      if (thenLive && cached?.total > 0) settings = loadState();
      page = await api('GET', endpoint());
    } catch (err) {
      if (!alive) return;
      if (err.code === 'closed') closed();
      else if (err.status === 404 && err.code === 'not_found') gone();
      else showStatus(words(err));
      return;
    }
    if (!alive || revision !== historyRevision) return;
    const serial = JSON.stringify(page);
    historyCache.delete(historyKey);
    historyCache.set(historyKey, { page, serial, bytes: serial.length * 4 });
    let cacheBytes = [...historyCache.values()].reduce((total, value) => total + value.bytes, 0);
    while (historyCache.size > 12 || cacheBytes > 8 * 1024 * 1024) {
      const key = historyCache.keys().next().value;
      cacheBytes -= historyCache.get(key).bytes;
      historyCache.delete(key);
    }
    if (thenLive && !(Array.isArray(page.items) && page.items.length)) applyDefaults();    // чат без истории — новый
    if (!cached || snapshot.serial !== serial) feed.replaceChildren(...messages(page.items));
    historyEmpty = page.total === 0;
    paintQuickActions();
    followups.clear();
    const pending = pendingInputs.get(draftKey);
    const lastUser = page.items?.findLast((m) => m.role === 'user');
    if (pending && page.total > pending.beforeTotal && lastUser?.text === pending.value) acknowledgeInput();
    turn = null;
    start = Number.isInteger(page.start) ? page.start : 0;
    more.hidden = start <= 0;
    if (thenLive) {
      if (settings) await settings;
      if (!settings || page.total === 0) await loadState();
    }
    await Promise.all([...pendingPaints]);
    if (!alive || revision !== historyRevision) return;
    attention = page.attention || null;
    historyLoading = false;
    updateControls();
    host.dataset.loaded = '1';
    onReady?.();
    // Навигация вкладок с клавиатуры сохраняет фокус в ряду вкладок.
    if (thenLive && !interactions && !document.activeElement?.matches('[role="tab"]:focus-visible')) focusDesktop(text);
    if (scroller) scroller.scrollTop = preserveScroll && !followed ? previousScroll : scroller.scrollHeight;
    paintNavigation();
    markRead();
    const initialScroll = scroller?.scrollTop;
    if (thenLive) requestAnimationFrame(() => {
      if (alive && revision === historyRevision && scroller && !messageTarget
          && Math.abs(scroller.scrollTop - initialScroll) <= 2) {
        scroller.scrollTop = scroller.scrollHeight;
        paintNavigation();
      }
    });
    if (thenLive && gitAction) {
      const prompt = gitAction;
      gitAction = null;
      submit(prompt);
    } else if (thenLive) openLive();
  }

  /* ---- действия ----------------------------------------------------------------- */

  function autosize() {
    text.style.height = 'auto';
    if (text.value) text.style.height = `${Math.min(text.scrollHeight, innerHeight * .25)}px`;
  }

  /** Выбор здесь действует в этом чате; умолчания учётки меняются в настройках. */
  function remember() {
    touched = true;
    paintPicks();
    if (!settingsReady && !resolving) loadState();
  }

  async function submit(quick = null) {
    if (resolving || historyLoading || !settingsReady) return;
    if (queue.paused()) { showStatus('Сначала завершите редактирование очереди'); return; }
    const value = quick ?? text.value;
    if (quick && (!hasGit || running)) {
      showStatus(running ? 'Дождитесь завершения задания в чате' : 'Git недоступен — обновите статус проекта');
      return;
    }
    if ((!value.trim() && !images.length) || sending || adding || resolving || foreign || external) return;
    if (running) {
      if (!agent?.chat_control) {
        showStatus('Обновите мост для отправки уточнений во время работы');
        return;
      }
      const sentImages = images;
      sending = true;
      updateControls();
      try {
        const result = await api('POST', endpoint('/steer'), { text: value,
          ...(agent?.queue_edit && delivery.value === 'queue' ? { mode: 'queue' } : {}),
          ...attachmentBody(sentImages) });
        if (unsentFollowups.has(result.id)) return;
        if (result.mode === 'queue' && !sentFollowups.has(result.id)) {
          if (!queuedInputs.has(draftKey)) queuedInputs.set(draftKey, new Map());
          queuedInputs.get(draftKey).set(result.id, { value, images: sentImages });
        }
        if (text.value === value) { text.value = ''; drafts.delete(draftKey); autosize(); }
        images = images.filter((i) => !sentImages.includes(i));
        keepImages();
        if (alive) showStatus(result.mode === 'queue' ? 'Уточнение принято — будет выполнено после текущего ответа'
          : 'Уточнение отправлено в текущее задание', false);
      } catch (err) {
        if (alive) {
          if (err.code === 'closed') closed();
          else {
            if (err.code === 'owned') foreign = true;
            showStatus(words(err));
            if (err.code === 'idle' || err.code === 'conflict') openLive();
          }
        }
      } finally {
        sending = false;
        if (alive) updateControls();
      }
      return;
    }
    const sentImages = quick ? [] : images;
    acted = Date.now();
    sending = true;
    updateControls();
    attach.disabled = true;
    paintImages();
    send.disabled = true;
    showStatus('');
    const mine = messageNode({ role: 'user', text: value, images: sentImages });
    pendingInputs.set(draftKey, { value, images: sentImages, mine, quick: !!quick });
    grow(() => feed.append(mine));
    if (!quick) text.value = ''; // Действие Git сохраняет текст и вложения черновика.
    autosize();
    const own = take();          // уход с экрана или с чата этот запрос не обрывает: ход, может, уже принят
    let response;
    try {
      // Старое сообщение с таким же текстом не подтверждает принятие нового задания.
      const before = await api('GET', endpoint(), undefined, { signal: own.signal });
      const pending = pendingInputs.get(draftKey);
      if (pending) pending.beforeTotal = before.total;
      response = await api('POST', endpoint('/turn'), { text: value, model: model.value || null,
        effort: effort.value || null,
        ...(access.value ? { access: access.value } : {}),
        ...attachmentBody(sentImages) }, { signal: own.signal });
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      if (controller === own) controller = null;
      restoreInput();
      if (err.code === 'closed') closed();
      else if (err.status === 404 && err.code === 'not_found') gone();
      else if (err.code === 'offline') waitForComputer();
      else {
        showStatus(words(err));
        // ход уже идёт (другая вкладка, VS Code) или, может быть, начат (компьютер не ответил вовремя) — смотреть его
        if (err.code === 'running' || err.code === 'timeout') openLive();
      }
      return;
    } finally {
      sending = false;
      updateControls();
      attach.disabled = adding || (!agent?.images && !agent?.attachments);
      paintImages();
    }
    // ход принят (ответ — поток, первое его событие — turn); набранное после — остаётся
    if (!agent?.chat_control) acknowledgeInput();
    images = images.filter((i) => !sentImages.includes(i));
    paintImages();
    if (!alive || document.visibilityState === 'hidden') {     // ушли, пока ход принимали: поток не читать
      own.abort();
      if (controller === own) controller = null;
      return;                    // вернулись на экран — live (onVisibility), в чат — его новая страница
    }
    await follow(response, own);
  }

  async function loadEarlier() {
    if (more.disabled || start <= 0) return false;
    more.disabled = true;
    const revision = historyRevision;
    paintNavigation();
    try {
      const page = await api('GET', `${endpoint()}${engine !== 'claude' ? '&' : '?'}before=${start}`);
      if (!alive || revision !== historyRevision) return false;
      const anchor = feed.firstElementChild;
      const top = anchor?.getBoundingClientRect().top;
      feed.prepend(...messages(page.items));
      await Promise.all([...pendingPaints]);
      if (!alive || revision !== historyRevision) return false;
      const oldStart = start;
      start = Number.isInteger(page.start) ? page.start : 0;
      more.hidden = start <= 0;
      if (scroller && anchor?.isConnected) scroller.scrollTop += anchor.getBoundingClientRect().top - top;
      if (messageTarget && scroller) messageScrollTop = scroller.scrollTop;
      return start < oldStart;
    } catch (err) {
      if (err.code === 'closed') closed();
      else showStatus(words(err));
      return false;
    } finally {
      more.disabled = false;
      paintNavigation();
    }
  }

  more.addEventListener('click', loadEarlier);
  previous.addEventListener('click', () => jumpMessage(-1));
  next.addEventListener('click', () => jumpMessage(1));
  scroller?.addEventListener('scroll', paintNavigation, { passive: true });
  window.addEventListener('resize', paintNavigation);
  const feedObserver = new MutationObserver(() => { paintNavigation(); paintQuickActions(); });
  feedObserver.observe(feed, { childList: true });

  stop.addEventListener('click', async () => {
    stop.disabled = true;
    try {
      await api('POST', endpoint('/stop'));
    } catch (err) {
      if (err.code === 'closed') closed();
      else if (err.code !== 'idle') showStatus(words(err));     // idle — ход уже кончился
    } finally {
      updateControls();
    }
  });

  waitSession.addEventListener('click', () => {
    waitingForRelease = true;
    updateControls();
    if (external) claimSession();
  });
  takeSession.addEventListener('click', () => claimSession(external));

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
  async function onVisibility() {
    if (document.visibilityState === 'hidden') {
      clearTimeout(claimTimer);
      clearTimeout(retry);
      retry = null;
      if (!sending) drop();      // идущий «Отправить» обрывает сам, узнав, принят ли ход
    } else if (!sending) {
      acted = Date.now();
      if (external && waitingForRelease) claimSession();
      await loadHistory(false, true);
      openLive();
    }
  }
  document.addEventListener('visibilitychange', onVisibility);
  scroller?.addEventListener('scroll', markRead, { passive: true });

  setRunning(false);
  loadHistory(true);

  return {
    unmount() {
      alive = false;
      unsubscribeGit?.();
      context.unmount();
      usage.unmount();
      hints?.unmount();
      queue.unmount();
      children?.unmount();
      stateRequest?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
      scroller?.removeEventListener('scroll', paintNavigation);
      scroller?.removeEventListener('scroll', markRead);
      window.removeEventListener('resize', paintNavigation);
      for (const event of ['pointerdown', 'wheel', 'keydown']) scroller?.removeEventListener(event, interacted);
      feedObserver.disconnect();
      if (!sending) drop();      // «Отправить» оборвёт поток сам, сняв черновик принятого хода
      clearTimeout(retry);
      clearTimeout(paintTimer);
      clearTimeout(claimTimer);
    },
  };
}
