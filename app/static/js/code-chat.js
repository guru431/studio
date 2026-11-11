/* Чат Claude Code (#/code/<aid>/<pid>/<chat>) — спека 2026-10-28-studio-claude-code-design 6.6 и 6.10,
 * протокол канала §4 (history, turn, watch, answer, stop) и §7 (события).
 *
 * chatView(host, { aid, pid, chat, agent, project, origin, fresh, closed, gone, openFile }) → { unmount() }:
 * - закрытие — крестик вкладки; история отвечает 404
 *   not_found (сессии на машине нет, студия её не заводила) — gone() вместо слов (вкладка уходит сама);
 * - лента #code-feed — хвост 12 сообщений (GET …/chats/<chat>), «Показать раньше» (#code-more) —
 *   страницы по 30 сообщений;
 *   предыдущая (before = start); сообщения — .code-msg.user (текст как есть) и .code-msg.assistant
 *   (текст markdown.js и свёрнутые details.code-step в хронологическом порядке);
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
 *   пока страница видна; live без компьютера — поток ожидания (offline {wait: true}), его online открывает live
 *   сразу; ошибка загрузки истории — слова и «Повторить» (#code-reload) в строке статуса, сбой связи, моста
 *   или шлюза — ещё и повтор через RETRY_MS; closed (событие или 409) — closed() из code.js: к панели PIN этого компьютера, после
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
import { icon } from './icons.js';
import { ACCESS, ENGINE_NAMES, accessLabel, effortLabel } from './code-options.js';
import { codePicker } from './code-picker.js';
import { fieldBox } from './field.js';
import { renderMarkdown, renderMarkdownInto, renderNodesInto } from './markdown.js';
import { readSSE } from './sse.js';
import { el, focusDesktop, words } from './ui.js';
import { usageView } from './code-usage.js';
import { contextView } from './code-context.js';
import { changeView } from './code-diff.js';
import { progressView } from './code-progress.js';
import { changesView } from './code-changes.js';
import { withFollowups } from './code-followups.js';
import { fileHints } from './code-autocomplete.js';
import { queueView } from './code-queue.js';
import { subagentView } from './code-subagents.js';
import { projectFilePath } from './code-file-links.js';
import { visualizationsView } from './code-visualizations.js';
import { shareMenu } from './chat-share.js';
import { statusActions } from './code-status-bar.js';
import { historyImages } from './code-chat-images.js';
import { commandsView } from './code-commands.js';

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
/** Черновики чатов (`${aid}/${chat}`) — только память страницы. Смена «есть черновик / нет» — событие
 * `studio-code-draft` { key, draft } на document: по нему вкладка показывает ✎ (code.js, code-tabs.js). */
class Drafts extends Map {
  set(key, value) {
    const had = hasDraft(key);
    super.set(key, value);
    announceDraft(key, had);
    return this;
  }

  delete(key) {
    const had = hasDraft(key);
    const out = super.delete(key);
    announceDraft(key, had);
    return out;
  }
}
function hasDraft(key) {
  return drafts.has(key) || imageDrafts.has(key);
}
function announceDraft(key, had) {
  if (had !== hasDraft(key)) document.dispatchEvent(new CustomEvent('studio-code-draft', { detail: { key, draft: !had } }));
}
const drafts = new Drafts();
const imageDrafts = new Drafts();  // картинки черновика — только память, как текст
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

export function chatView(host, { aid, pid, chat, agent, project, origin, fresh = false, engine = 'claude', closed: toPin, gone,
  openFile, git, gitAction = null, taskDone, forked, verify = null, onReady = null, title = null }) {
  const api = (method, path, body, options = {}) => request(method, path, body,
    { ...options, headers: { ...options.headers, 'x-studio-window': WINDOW } });
  const url = `/api/code/agents/${aid}/projects/${pid}/chats/${chat}`;
  const endpoint = (tail = '') => `${url}${tail}${engine !== 'claude' ? `?engine=${engine}` : ''}`;
  const assistant = ENGINE_NAMES[engine];
  const draftKey = `${aid}/${chat}`;
  const historyKey = `${me.id}/${aid}/${pid}/${engine}/${chat}`;
  const workspace = document.getElementById('view');
  const scroller = el('div', { id: 'code-chat-scroll', class: 'code-chat-scroll', tabindex: '0',
    role: 'region', 'aria-label': 'Лента чата' });
  const visualizations = visualizationsView(host, { aid, pid, supported: agent.visualizations });
  const fileLinks = { resolve: (href) => projectFilePath(href, agent, project), open: openFile,
    visualization: visualizations.open };
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
  let turn = null;               // ответ идущего хода: узлы, текстовые сегменты и модели шагов
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
  const stepPaint = new WeakMap();
  let expandSteps = false;
  const messageViews = new WeakMap();
  const pendingPaints = new Set();
  let historyRevision = 0;
  let opened = false;            // история хоть раз загрузилась (и запрошено состояние сессии)
  let loadError = null;          // слова последней ошибки загрузки в строке статуса
  let renderedHistory = null;    // снимок и узлы хвоста: неизменившаяся история сохраняет раскрытие и выделение
  let attention = null;
  let markingRead = false;
  let interactions = 0;
  const interacted = () => { interactions += 1; };
  for (const event of ['pointerdown', 'wheel', 'keydown']) workspace?.addEventListener(event, interacted, { passive: true });
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
  const pictures = historyImages(scroller, feed, { api, endpoint, closed: () => closed() });
  const previous = el('button', { type: 'button', id: 'code-message-up', class: 'icon-btn',
    'aria-label': 'Предыдущее моё сообщение', title: 'Предыдущее моё сообщение', disabled: true });
  const next = el('button', { type: 'button', id: 'code-message-down', class: 'icon-btn',
    'aria-label': 'Следующее моё сообщение', title: 'Следующее моё сообщение', disabled: true });
  const allSteps = el('button', { type: 'button', id: 'code-toggle-steps', class: 'icon-btn',
    'aria-label': 'Развернуть все команды', title: 'Развернуть все команды', 'aria-pressed': 'false', disabled: true });
  previous.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
  next.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6"/></svg>';
  allSteps.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 9 4-4 4 4m-8 6 4 4 4-4"/></svg>';
  const navigation = el('nav', { class: 'code-message-nav', 'aria-label': 'Навигация по чату', hidden: true }, previous, allSteps, next);
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
  const picks = new Map();
  const context = contextView(host, { chat, engine, project,
    messages: () => feed.querySelectorAll('.code-msg').length });
  const contextBox = context.button;
  const usage = usageView(host, { aid, engine, supported: agent?.subscription_usage });
  const changedFiles = changesView(host, { openFile: (path) => {
    const relative = projectFilePath(path, agent, project);
    if (relative) openFile(relative);
  } });
  function paintChangedFiles() {
    const latest = [...feed.children].findLast((node) => node.classList.contains('assistant'));
    changedFiles.update(messageViews.get(latest)?.progress.changes());
  }
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
  const stop = el('button', { type: 'button', id: 'code-stop', class: 'btn composer-go composer-stop', hidden: true,
    'aria-label': 'Остановить', title: 'Остановить' }, el('span', { class: 'code-go-label' }, 'Остановить'));
  send.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>');
  stop.insertAdjacentHTML('afterbegin', '<svg class="ico code-go-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>');
  const activityText = el('span', {}, 'Нет активной задачи');
  const activity = el('div', { id: 'code-activity', class: 'code-activity', role: 'status',
    'aria-live': 'polite', 'aria-atomic': 'true', 'data-state': 'idle' },
    el('span', { class: 'code-activity-dot', 'aria-hidden': 'true' }), activityText);
  const activeTools = new Map();
  const reloadButton = el('button', { type: 'button', id: 'code-reload', class: 'btn btn-sm code-reload', hidden: true,
    title: 'Загрузить чат заново', 'aria-label': 'Повторить' }, el('span', { class: 'code-reload-label' }, 'Повторить'));
  reloadButton.insertAdjacentHTML('afterbegin', '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>');
  reloadButton.addEventListener('click', () => reload());
  const actions = el('div', { class: 'code-chat-actions' });
  const sharing = shareMenu(actions, { id: 'code-share-chat', content: scroller, request: api, failed: (err) => {
    if (err.code === 'closed') closed();
    else showStatus(words(err));
  } });
  sharing.set({ title: typeof title === 'string' ? title : 'Чат', share: endpoint('/share'), export: endpoint('/export') });
  actions.append(sharing.button);
  actions.append(reloadButton);
  const statusFixed = el('div', { class: 'code-status-fixed' }, context.cacheButton, contextBox, usage.button);
  const statusBar = el('div', { class: 'code-status-bar' });
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
    const view = codePicker(host, label, select);
    picks.set(select, view);
    return view.root;
  };
  const form = el('form', { class: 'composer code-composer', id: 'code-composer' }, attachments, picker, field.box, field.error,
    el('div', { class: 'composer-bar' },
      el('div', { class: 'composer-opts' },
        pick('Модель', model), pick('Усилие', effort), pick('Права', access)),
      stop, send));
  host.dataset.engine = engine;
  scroller.append(
    ...(origin === 'other' ? [el('p', { id: 'code-shared-note', class: 'code-shared-note' }, SHARED)] : []),
    more, feed, quickActions);
  // Плашка ожидающего вопроса над полем (D2): на узком экране карточка .code-ask часто вне ленты на экране.
  // Кнопки нажимают кнопки самой карточки — тот же ответ, те же блокировки (чужое окно, отправка).
  const askText = el('span', { class: 'code-ask-pin-text' });
  const askAllow = el('button', { type: 'button', id: 'code-ask-pin-allow', class: 'btn btn-sm btn-accent' }, 'Разрешить');
  const askDeny = el('button', { type: 'button', id: 'code-ask-pin-deny', class: 'btn btn-sm' }, 'Отклонить');
  const askShow = el('button', { type: 'button', id: 'code-ask-pin-show', class: 'btn btn-sm' }, 'Показать');
  const askPin = el('div', { id: 'code-ask-pin', class: 'code-ask-pin', role: 'region', 'aria-label': 'Ждёт вашего ответа',
    hidden: true }, askText, el('div', { class: 'code-ask-pin-actions' }, askAllow, askDeny, askShow));
  const firstAsk = () => feed.querySelector('.code-ask');
  function paintAskPin() {
    const cards = feed.querySelectorAll('.code-ask');
    const card = cards[0];
    askPin.hidden = !card;
    if (!card) return;
    const yes = card.querySelector('.code-ask-yes');
    const no = card.querySelector('.code-ask-no');
    const tool = card.querySelector('.code-ask-tool')?.textContent;
    askText.textContent = (yes ? `${assistant} просит разрешения${tool ? `: ${tool}` : ''}` : `${assistant} задаёт вопрос`)
      + (cards.length > 1 ? ` · ещё ${cards.length - 1}` : '');
    askAllow.hidden = !yes;
    askDeny.hidden = !no;
    askAllow.disabled = !!yes?.disabled;
    askDeny.disabled = !!no?.disabled;
  }
  askAllow.addEventListener('click', () => firstAsk()?.querySelector('.code-ask-yes')?.click());
  askDeny.addEventListener('click', () => firstAsk()?.querySelector('.code-ask-no')?.click());
  askShow.addEventListener('click', () => {
    const card = firstAsk();
    if (!card) return;
    card.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    card.querySelector('button:not([disabled]), input')?.focus({ preventScroll: true });
  });
  const askObserver = new MutationObserver(paintAskPin);
  askObserver.observe(feed, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
  host.append(el('div', { class: 'code-chat-history' }, scroller, navigation),
    el('div', { class: 'code-compose-area' }, status, sessionNotice, askPin, statusBar, form));
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
  const subtaskControl = subagentView(host, { endpoint, supported: !!agent?.subagents && engine !== 'opencode' });
  const children = subtaskControl.button.disabled ? null : subtaskControl;
  const commands = commandsView(host, { stepNode, repaint: node => stepPaint.get(node)?.() });
  statusFixed.prepend(subtaskControl.button, commands.button, changedFiles.button);
  if (context.cacheButton.hidden) {
    context.cacheButton.hidden = false;
    context.cacheButton.disabled = true;
    context.cacheButton.title = 'Помощник не сообщает сведения о кэше';
    context.cacheButton.setAttribute('aria-label', context.cacheButton.title);
    context.cacheButton.removeAttribute('aria-haspopup');
  }
  const statusControls = statusActions(host, statusBar, activity, actions, statusFixed);
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
    for (const view of picks.values()) view.update();
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
    paintPicks();
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
        'aria-label': `Убрать ${image.name}`, title: 'Убрать', disabled: sending }, icon('x'));
      remove.addEventListener('click', () => {
        images = images.filter((i) => i !== image);
        keepImages();
      });
      return el('div', { class: `code-image${IMAGE_TYPES.has(image.mime) ? '' : ' code-file'}`, title: image.name },
        IMAGE_TYPES.has(image.mime) ? el('img', { src: `data:${image.mime};base64,${image.data}`, alt: image.name })
          : el('span', { class: 'code-attachment-name' }, image.name), remove);
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

  let followTail = true;
  let lastScrollTop = scroller.scrollTop;
  let followFrame = null;

  function stuck() {
    return !scroller || scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - STICK_PX;
  }

  /** Якорь — верх ленты. Сохраняем выбранное сообщение даже у конца прокрутки. */
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
    return scroller.getBoundingClientRect().top;
  }

  function scrollByViewportPixels(delta) {
    const scale = Number(document.documentElement.style.getPropertyValue('--ui-scale')) || 1;
    scroller.scrollTop += delta / scale;
  }

  function paintNavigation() {
    if (!scroller || historyLoading) return;
    const { before, after } = messageNeighbours();
    const steps = [...feed.querySelectorAll('details.code-step')];
    const expanded = steps.length > 0 && steps.every((step) => step.open);
    const stepsLabel = expanded ? 'Свернуть все команды' : 'Развернуть все команды';
    allSteps.disabled = steps.length === 0;
    allSteps.setAttribute('aria-label', stepsLabel);
    allSteps.setAttribute('aria-pressed', String(expanded));
    allSteps.title = stepsLabel;
    allSteps.querySelector('path').setAttribute('d', expanded ? 'm8 5 4 4 4-4m-8 14 4-4 4 4' : 'm8 9 4-4 4 4m-8 6 4 4 4-4');
    navigation.hidden = !feed.querySelector('.code-msg.user') && !steps.length && start <= 0;
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
        followTail = true;
        scroller.scrollTop = scroller.scrollHeight;
        paintNavigation();
      }
      return;
    }
    messageTarget = target;
    followTail = false;
    scrollByViewportPixels(target.getBoundingClientRect().top - messageTop());
    messageScrollTop = scroller.scrollTop;
    paintNavigation();
  }

  /** Правка ленты: была внизу — остаётся внизу. */
  function grow(change) {
    trackScroll();
    const was = !messageTarget && followTail;
    change();
    if (was && scroller) {
      scroller.scrollTop = scroller.scrollHeight;
      lastScrollTop = scroller.scrollTop;
    }
  }

  function trackScroll() {
    const gap = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
    const tolerance = Math.max(160, Math.min(320, scroller.clientHeight / 2));
    if (gap <= tolerance && !messageTarget) followTail = true;
    else if (scroller.scrollTop < lastScrollTop - 2) followTail = false;
    lastScrollTop = scroller.scrollTop;
  }

  // Markdown, раскрытие деталей и изменение высоты поля заканчиваются после события потока.
  // Их перерисовка тоже держит конец, пока владелец не ушёл читать далеко вверх.
  const tailObserver = new ResizeObserver(() => {
    trackScroll();
    if (followFrame !== null || historyLoading || !followTail || messageTarget) return;
    followFrame = requestAnimationFrame(() => {
      followFrame = null;
      trackScroll();
      if (alive && !historyLoading && followTail && !messageTarget) {
        scroller.scrollTop = scroller.scrollHeight;
        lastScrollTop = scroller.scrollTop;
      }
    });
  });
  tailObserver.observe(feed);
  tailObserver.observe(scroller);

  function stepNode(s) {
    const stepInput = () => s.input && typeof s.input === 'object' ? s.input : null;
    let input = stepInput();
    const links = el('span', { class: 'code-step-files' });
    let filesSerial = null;
    const state = el('span', { class: 'code-step-state' });
    const timing = el('span', { class: 'code-step-duration' });
    const head = el('span', { class: 'code-step-head' });
    const details = el('details', { class: `code-step${s.error ? ' error' : ''}`, 'data-id': String(s.id ?? '') },
      el('summary', {}, el('b', { class: 'code-step-tool' }, String(s.tool ?? '')), head, state, timing, links));
    stepData.set(details, s);
    let filled = false, changeSerial = null;
    let result, inputNode, changeNode;
    const progress = el('p', { class: 'code-step-progress muted small', hidden: true });
    function paintInput() {
      inputNode.textContent = typeof input?.command === 'string'
        ? `${input.command}${input.cwd ? `\n${input.cwd}` : ''}` : JSON.stringify(input ?? {}, null, 2);
    }
    function paint() {
      const latest = stepInput();
      if (input !== latest) {
        input = latest;
        if (filled) paintInput();
      }
      const changes = Array.isArray(s.changes) && s.changes.length ? s.changes : Array.isArray(input?.changes) ? input.changes : [];
      const changedPaths = changes.map((change) => change?.path).filter((path) => typeof path === 'string');
      const paths = [...new Set(changedPaths.length ? changedPaths : typeof input?.file_path === 'string' ? [input.file_path] : [])];
      head.textContent = paths.length > 1 ? `Файлов: ${paths.length}` : paths[0] || preview(input);
      const pathsKey = JSON.stringify(paths);
      if (filesSerial !== pathsKey) {
        filesSerial = pathsKey;
        links.replaceChildren();
        if (FILE_TOOLS.has(s.tool)) for (const path of paths) {
          const tail = projectFilePath(path, agent, project);
          if (!tail) {
            if (paths.length > 1) links.append(el('span', {}, path));
            continue;
          }
          const link = el('a', { href: `#/code/${aid}/${pid}`, class: 'code-step-file', title: `Открыть файл: ${path}` },
            paths.length > 1 ? path : 'Открыть файл');
          link.addEventListener('click', (event) => {
            event.preventDefault();               // и заголовок шага не раскрывается
            openFile(tail);
          });
          links.append(link);
        }
      }
      details.classList.toggle('error', !!s.error);
      details.dataset.state = s.error ? 'failed' : s.status || 'completed';
      state.textContent = s.error ? 'Ошибка' : ({ running: 'Выполняется', completed: 'Готово', failed: 'Ошибка',
        stopped: 'Остановлено', unknown: 'Статус не сообщается' }[s.status] || 'Готово');
      timing.textContent = Number.isFinite(s.duration_ms) && s.duration_ms >= 0
        ? `${(s.duration_ms / 1000).toLocaleString('ru', { maximumFractionDigits: 2 })} с` : '';
      if (Number.isInteger(s.exit_code)) timing.textContent += `${timing.textContent ? ' · ' : ''}код ${s.exit_code}`;
      progress.hidden = !s.progress_message && !s.output_truncated;
      progress.textContent = [s.progress_message, s.output_truncated ? 'Показана часть вывода' : ''].filter(Boolean).join(' · ');
      if (filled) {
        const followOutput = result.scrollHeight - result.scrollTop - result.clientHeight < 24;
        result.textContent = typeof s.text === 'string' ? s.text : '';
        if (followOutput) result.scrollTop = result.scrollHeight;
        result.hidden = !result.textContent;
        const serial = JSON.stringify(s.error ? null : s.changes || null);
        if (serial !== changeSerial) {
          changeSerial = serial;
          changeNode?.remove();
          changeNode = !s.error ? changeView(s.changes) : null;
          if (changeNode) result.before(changeNode);
          inputNode.hidden = !!changeNode;
        }
      }
    }
    stepPaint.set(details, paint);
    details.addEventListener('toggle', () => {
      paintNavigation();
      if (!details.open || filled) return;
      filled = true;
      result = el('pre', { class: 'code-step-result', tabindex: '0', 'aria-label': 'Вывод инструмента' });
      inputNode = el('pre', { class: 'code-step-input', tabindex: '0', 'aria-label': 'Ввод инструмента' });
      paintInput();
      details.append(inputNode, result, progress);
      paint();
    });
    details.open = expandSteps;
    paint();
    return details;
  }

  function textSegment(value, block = null, phase = null) {
    const node = el('div', { class: 'md code-timeline-text', ...(phase ? { 'data-phase': phase } : {}) });
    return { node, text: value, block, phase, dirty: false, finalized: true };
  }

  function renderTimeline(body, m) {
    const textValue = typeof m.text === 'string' ? m.text : '';
    const items = Array.isArray(m.steps) ? m.steps : [];
    const byId = new Map(items.map((step) => [step.id, step]));
    let position = 0;
    const seen = new Set();
    const valid = Array.isArray(m.timeline) && m.timeline.length <= 2000 && m.timeline.every((part) => {
      if (part?.kind === 'step' && byId.has(part.id) && !seen.has(part.id)) { seen.add(part.id); return true; }
      if (part?.kind !== 'text' || !Number.isInteger(part.start) || !Number.isInteger(part.end)
        || part.start !== position || part.end < part.start || part.end > textValue.length) return false;
      position = part.end;
      return true;
    }) && position === textValue.length && seen.size === byId.size;
    const parts = withFollowups(valid ? m.timeline : [...items.map((step) => ({ kind: 'step', id: step.id })),
      { kind: 'text', start: 0, end: textValue.length }], m.followups, textValue.length, items.length);
    const segments = [];
    const nodes = new Map();
    const textPaints = [];
    for (const part of parts) {
      if (part.kind !== 'text') continue;
      const segment = textSegment(textValue.slice(part.start, part.end), part.block, part.phase);
      segments.push(segment);
      nodes.set(part, segment);
    }
    const make = (part) => {
      if (part.kind === 'step') return stepNode(byId.get(part.id));
      if (part.kind === 'followup') return followupNode(part.data);
      const segment = nodes.get(part);
      // Начинаем Markdown при добавлении сегмента: очередь списка ещё не прикрепила остальные узлы.
      if (segment.text) textPaints.push(renderMarkdownInto(segment.node, segment.text,
        { valid: () => alive && feed.contains(segment.node), fileLinks }));
      return segment.node;
    };
    // Снимок активного хода строится сразу: следующая дельта должна найти все его шаги.
    let built;
    if (m.live || parts.length <= 8) {
      body.replaceChildren(...parts.map(make));
      built = Promise.resolve();
    } else {
      built = renderNodesInto(body, parts, make, { valid: () => alive && feed.contains(body) });
    }
    const paint = built.then(() => Promise.all(textPaints));
    pendingPaints.add(paint);
    paint.then(() => pendingPaints.delete(paint));
    return { segments, active: parts.at(-1)?.kind === 'text' ? segments.at(-1) : null };
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
        : agent?.history_images && typeof m.id === 'string' && Number.isInteger(i.index) && i.index >= 0
          ? pictures.node(m.id, i) : el('span', { class: 'code-sent-file' }, i.name)), typeof m.text === 'string' ? m.text : '', branch);
    }
    const body = el('div', { class: 'code-steps code-timeline' });
    const progress = progressView({ onChanges: paintChangedFiles,
      openChild: children ? (row) => children.open(row) : null });
    progress.update(m.progress || {}, m.steps || []);
    const view = { body, progress, ...renderTimeline(body, m) };
    const node = el('div', { class: 'code-msg assistant' }, progress.root, body);
    messageViews.set(node, view);
    return node;
  }

  function messages(items) {
    return (Array.isArray(items) ? items : []).filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
      .map(messageNode);
  }

  /** Ответ идущего хода — в конце ленты (первое событие хода его заводит). */
  function current() {
    if (!turn) {
      const node = messageNode({ role: 'assistant', text: '', steps: [], live: true });
      turn = { node, steps: node.querySelector('.code-steps'), view: messageViews.get(node),
        asks: el('div', { class: 'code-asks' }), models: [] };
      node.append(turn.asks);
      grow(() => { feed.append(node); paintChangedFiles(); });
    }
    return turn;
  }

  function paintText(final = false) {
    clearTimeout(paintTimer);
    paintTimer = null;
    if (!turn) return;
    for (const segment of turn.view.segments) {
      if (!segment.dirty && !(final && !segment.finalized)) continue;
      segment.dirty = false;
      segment.finalized = final;
      const target = segment.node;
      if (final) {
        const paint = renderMarkdownInto(target, segment.text, { valid: () => alive && feed.contains(target), update: grow, fileLinks });
        pendingPaints.add(paint);
        paint.then(() => pendingPaints.delete(paint));
      } else if (segment.text.length < 2000) grow(() => target.replaceChildren(renderMarkdown(segment.text, { highlight: false, fileLinks })));
      else grow(() => target.replaceChildren(el('pre', { class: 'code-stream-text' }, segment.text)));
    }
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
    paintPicks();
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

  function followupNode(data) {
    if (data.sent) { sentFollowups.add(data.id); queuedInputs.get(draftKey)?.delete(data.id); }
    if (agent?.queue_edit && data.mode === 'queue' && !data.sent && !sentFollowups.has(data.id)) queue.add(data);
    if (followups.has(data.id)) return followups.get(data.id);
    const node = messageNode({ role: 'user', text: data.text });
    node.dataset.followup = data.id;
    followups.set(data.id, node);
    return node;
  }

  function showFollowup(data) {
    if (typeof data.id !== 'string') return;
    const node = followupNode(data);
    if (node.isConnected) return;
    const t = current();
    paintText(true);
    t.view.active = null;
    grow(() => t.steps.append(node));
    paintNavigation();
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
    sharing.set(null);
    historyCache.clear();
    if (alive) toPin();
  }

  /* ---- карточки ----------------------------------------------------------------- */

  function askNode(a) {
    const card = el('div', { class: 'code-ask', 'data-request': String(a.request_id ?? '') });
    const error = el('p', { class: 'error code-ask-error', 'aria-live': 'polite' });
    const buttons = [];
    // Запрос субагента хода (Codex, OpenCode): ответ уходит ему же; без поля — запрос самого хода
    if (typeof a.subtask === 'string' && a.subtask) {
      card.append(el('p', { class: 'code-ask-subtask muted small' }, `Подзадача · ${a.subtask}`));
    }

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
      for (const step of turn.steps.querySelectorAll('.code-step')) {
        const model = stepData.get(step);
        if (model?.status === 'running') {
          model.status = data.status === 'stopped' ? 'stopped' : 'unknown';
          stepPaint.get(step)?.();
        }
      }
      turn.view.progress.update({}, turn.models);
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
    if (['turn', 'snapshot', 'done'].includes(event)) children?.refresh();
    if (['turn', 'snapshot', 'step', 'step_update', 'result', 'idle', 'done'].includes(event)) {
      queueMicrotask(() => { if (alive) commands.update(running ? turn?.models : []); });
    }
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
        children?.update(data.progress?.agents, true);
        clearTimeout(paintTimer);
        paintTimer = null;
        t.models = Array.isArray(data.steps) ? data.steps : [];
        for (const step of t.models) if (step.status === 'running') activeTools.set(String(step.id), step.tool);
        grow(() => {
          Object.assign(t.view, renderTimeline(t.steps, { ...data, live: true }));
          t.view.progress.update(data.progress || {}, t.models, true);
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
      case 'text': {
        paintActivity(turn?.asks.querySelector('.code-ask') ? 'waiting' : 'responding');
        const t = current();
        const delta = typeof data.text === 'string' ? data.text : '';
        if (!t.view.active || (data.block && t.view.active.block && t.view.active.block !== data.block)) {
          paintText(true);
          const segment = textSegment('', data.block, data.phase);
          t.view.segments.push(segment);
          t.view.active = segment;
          grow(() => t.steps.append(segment.node));
        }
        t.view.active.block ||= data.block;
        t.view.active.text += delta;
        t.view.active.dirty = true;
        t.view.active.finalized = false;
        if (!paintTimer) paintTimer = setTimeout(paintText, PAINT_MS);
        return false;
      }
      case 'step': {
        const t = current();
        paintText(true);
        t.view.active = null;
        const model = { ...data, text: '', error: false, status: data.status || 'running' };
        t.models.push(model);
        if (model.status === 'running') activeTools.set(String(data.id), data.tool);
        continueActivity();
        grow(() => {
          t.steps.append(stepNode(model));
          t.view.progress.update({}, t.models);
        });
        paintNavigation();
        return false;
      }
      case 'progress':
        children?.update(data.agents, true);
        grow(() => current().view.progress.update(data, current().models));
        return false;
      case 'step_update':
      case 'result': {
        const completed = event === 'result';
        if (completed) activeTools.delete(String(data.id));
        for (const step of (turn ? turn.steps : feed).querySelectorAll('details.code-step')) {
          if (step.dataset.id !== String(data.id)) continue;
          const model = stepData.get(step);
          Object.assign(model, completed ? { progress_message: '', output_truncated: false } : {}, data,
            completed ? { status: data.status || (data.error ? 'failed' : 'completed') } : {});
          grow(() => stepPaint.get(step)?.());
        }
        if (turn) grow(() => turn.view.progress.update({}, turn.models));
        continueActivity();
        return false;
      }
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
        if (data.wait === true) {     // live без компьютера: поток ждёт online, без опроса
          setRunning(false);
          paintActivity('offline');
          showStatus(WAITING);
          return false;
        }
        waitForComputer();
        return true;
      case 'online':                  // компьютер вернулся — история и live сразу (поток его ожидания кончился)
        if (status.textContent === WAITING) showStatus('');
        reload(true);
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

  /** Повтор по таймеру — фоновая просьба: срок PIN компьютера не продлевает. */
  function later(ms) {
    clearTimeout(retry);
    retry = alive && document.visibilityState !== 'hidden' ? setTimeout(() => openLive(true), ms) : null;
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

  async function openLive(passive = false) {
    if (!alive || document.visibilityState === 'hidden') return;
    const own = take();
    let response;
    try {
      response = await api('GET', endpoint('/live'), undefined,
        { signal: own.signal, ...(passive ? { headers: { 'x-studio-passive': '1' } } : {}) });
    } catch (err) {
      if (!alive || own.signal.aborted) return;
      if (controller === own) controller = null;
      if (err.code === 'closed') closed();
      else if (err.code === 'offline') waitForComputer();
      else {
        showStatus(words(err));
        if (err.code === 'network' || err.status >= 500) later(RETRY_MS);
        else reloadButton.hidden = false;      // само не повторится — «Повторить»
      }
      return;
    }
    reloadButton.hidden = opened;
    if (status.textContent === WAITING) showStatus('');
    await follow(response, own);
  }

  /** История заново. true — сама открыла live или отправила Git-действие, false — готово без этого, undefined —
   * не загрузилась или её обогнала более новая загрузка. Первая удачная загрузка (opened) — даже если не
   * thenLive: первую обогнали (возвращение на экран) или она упала — запрашивает состояние сессии, без него
   * «Отправить» и выбор модели остаются выключенными. */
  async function loadHistory(thenLive, preserveScroll = false, passive = false) {
    const previousScroll = preserveScroll ? scroller?.scrollTop : 0;
    const followed = !preserveScroll || stuck();
    const revision = ++historyRevision;
    const first = thenLive || !opened;
    const snapshot = thenLive && historyCache.get(historyKey);
    const cached = snapshot?.page;
    if (cached) {
      feed.replaceChildren(...messages(cached.items));
      paintChangedFiles();
      renderedHistory = { serial: snapshot.serial, nodes: [...feed.children] };
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
      page = await api('GET', endpoint(), undefined, passive ? { headers: { 'x-studio-passive': '1' } } : {});
    } catch (err) {
      if (!alive) return;
      if (err.code === 'closed') closed();
      else if (err.status === 404 && err.code === 'not_found') gone();
      else if (revision === historyRevision) loadFailed(err);
      return;
    }
    if (!alive || revision !== historyRevision) return;
    loadRecovered();
    const serial = JSON.stringify([page.start, page.total, page.items]);
    historyCache.delete(historyKey);
    historyCache.set(historyKey, { page, serial, bytes: serial.length * 4 });
    let cacheBytes = [...historyCache.values()].reduce((total, value) => total + value.bytes, 0);
    while (historyCache.size > 12 || cacheBytes > 8 * 1024 * 1024) {
      const key = historyCache.keys().next().value;
      cacheBytes -= historyCache.get(key).bytes;
      historyCache.delete(key);
    }
    if (first && !(Array.isArray(page.items) && page.items.length)) applyDefaults();    // чат без истории — новый
    const offset = renderedHistory ? feed.children.length - renderedHistory.nodes.length : -1;
    const unchanged = renderedHistory?.serial === serial && offset >= 0 && (offset === 0 || start < page.start)
      && renderedHistory.nodes.every((node, index) => feed.children[offset + index] === node);
    if (!unchanged) {
      feed.replaceChildren(...messages(page.items));
      renderedHistory = { serial, nodes: [...feed.children] };
    }
    paintChangedFiles();
    historyEmpty = page.total === 0;
    paintQuickActions();
    followups.clear();
    const pending = pendingInputs.get(draftKey);
    const lastUser = page.items?.findLast((m) => m.role === 'user');
    if (pending && page.total > pending.beforeTotal && lastUser?.text === pending.value) acknowledgeInput();
    turn = null;
    if (!unchanged) start = Number.isInteger(page.start) ? page.start : 0;
    more.hidden = start <= 0;
    if (thenLive) {
      if (settings) await settings;
      if (!settings || page.total === 0) await loadState();
    } else if (first && resolving && !stateRequest) await loadState();
    await Promise.all([...pendingPaints]);
    if (!alive || revision !== historyRevision) return;
    opened = true;
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
    if (first && gitAction) {
      const prompt = gitAction;
      gitAction = null;
      submit(prompt);
      return true;
    }
    if (thenLive) openLive();
    return thenLive;
  }

  /** Ошибка загрузки истории: слова и «Повторить»; связь, сбой моста или шлюза — повтор сам через RETRY_MS. */
  function loadFailed(err) {
    loadError = err.code === 'offline' ? WAITING : words(err);
    showStatus(loadError);
    if (err.code === 'offline') paintActivity('offline');
    reloadButton.hidden = false;
    if (['offline', 'network', 'timeout'].includes(err.code) || err.status >= 500) {
      clearTimeout(retry);
      retry = alive && document.visibilityState !== 'hidden' ? setTimeout(() => reload(true), RETRY_MS) : null;
    }
  }

  function loadRecovered() {
    reloadButton.hidden = true;
    if (loadError && status.textContent === loadError) showStatus('');
    loadError = null;
  }

  /** «Повторить» (или повтор по таймеру — фоновая просьба): история, затем live, если она его не открыла. */
  async function reload(passive = false) {
    clearTimeout(retry);
    retry = null;
    if (!passive) acted = Date.now();
    reloadButton.hidden = true;
    if (await loadHistory(false, opened, passive) === false) openLive(passive);
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
      if (anchor?.isConnected) scrollByViewportPixels(anchor.getBoundingClientRect().top - top);
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
  allSteps.addEventListener('click', () => {
    interacted();
    acted = Date.now();
    const steps = [...feed.querySelectorAll('details.code-step')];
    expandSteps = steps.some((step) => !step.open);
    grow(() => { for (const step of steps) step.open = expandSteps; });
    paintNavigation();
  });
  next.addEventListener('click', () => jumpMessage(1));
  scroller?.addEventListener('scroll', paintNavigation, { passive: true });
  scroller.addEventListener('scroll', trackScroll, { passive: true });
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
      if (!(await loadHistory(false, true))) openLive();
    }
  }
  document.addEventListener('visibilitychange', onVisibility);
  scroller?.addEventListener('scroll', markRead, { passive: true });

  setRunning(false);
  loadHistory(true);

  return {
    focus() { if (alive) focusDesktop(text); },
    unmount() {
      alive = false;
      askObserver.disconnect();
      sharing.unmount();
      pictures.unmount();
      statusControls.unmount();
      for (const view of picks.values()) view.unmount();
      visualizations.unmount();
      unsubscribeGit?.();
      context.unmount();
      usage.unmount();
      changedFiles.unmount();
      hints?.unmount();
      queue.unmount();
      subtaskControl.unmount();
      commands.unmount();
      stateRequest?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
      scroller?.removeEventListener('scroll', paintNavigation);
      scroller.removeEventListener('scroll', trackScroll);
      tailObserver.disconnect();
      if (followFrame !== null) cancelAnimationFrame(followFrame);
      scroller?.removeEventListener('scroll', markRead);
      window.removeEventListener('resize', paintNavigation);
      for (const event of ['pointerdown', 'wheel', 'keydown']) workspace?.removeEventListener(event, interacted);
      feedObserver.disconnect();
      if (!sending) drop();      // «Отправить» оборвёт поток сам, сняв черновик принятого хода
      clearTimeout(retry);
      clearTimeout(paintTimer);
      clearTimeout(claimTimer);
    },
  };
}
