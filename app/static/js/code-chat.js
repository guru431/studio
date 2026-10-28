/* Чат Claude Code (#/code/<aid>/<pid>/<chat>) — спека 2026-10-28-studio-claude-code-design 6.6 и 6.10,
 * протокол канала §4 (history, turn, watch, answer, stop) и §7 (события).
 *
 * chatView(host, { aid, pid, chat, agent, project, origin, navigate, openFile }) → { unmount() }:
 * - лента #code-feed — страница истории (GET …/chats/<chat>, по 100), «Показать раньше» (#code-more) —
 *   предыдущая (before = start); сообщения — .code-msg.user (текст как есть) и .code-msg.assistant (шаги
 *   details.code-step, затем текст — markdown.js);
 * - сразу за историей — поток live: idle — ничего, snapshot — всё состояние идущего хода вместо прежнего
 *   хвоста; «Отправить» — поток turn; события text, step, result, ask (карточка .code-ask), asked (карточка
 *   уходит), done (stopped — «Ход прерван», error — «Ошибка: …»);
 * - offline (событие или 409) — «Компьютер пропал со связи — жду его» и live раз в RETRY_MS, пока страница
 *   видна; closed (событие или 409) — к панели PIN этого компьютера (#/code?pin=<aid>), как в code.js;
 * - страница ушла с экрана — свой поток она снимает (push от сервера уходит, только если чат никто не
 *   смотрит), вернулась — снова live: снимок догонит ход, ход на машине от этого не прерывается.
 * В хранилище браузера — только выбор модели и усилия (studio-code-choice:<aid>), ни ленты, ни поля.
 * Текст машины — только textContent и markdown.js. */
import { api } from './api.js';
import { fieldBox } from './field.js';
import { renderMarkdown } from './markdown.js';
import { readSSE } from './sse.js';
import { el, words } from './ui.js';

const WAITING = 'Компьютер пропал со связи — жду его';
const SHARED = 'Эту сессию начали в VS Code или терминале. Если она открыта и там, не пишите в двух местах сразу'
  + ' — ответы перемешаются.';
const STOPPED = 'Ход прерван';
const ASK_TITLE = 'Claude Code просит разрешения';
const INSTEAD = 'Что сделать вместо этого (необязательно)';
const CHOICE = 'studio-code-choice:';
const RETRY_MS = 5000;           // повтор live, пока компьютера нет
const AGAIN_MS = 1000;           // поток кончился без конца хода (предел прокси, сеть) — снова live
const PAINT_MS = 80;             // текст хода перерисовывается не чаще
const STICK_PX = 80;             // лента у низа — новое её прокручивает
const PREVIEW_MAX = 80;          // начало ввода шага в его заголовке
const FILE_TOOLS = new Set(['Read', 'Edit', 'Write']);

function loadChoice(aid) {
  try {
    const value = JSON.parse(localStorage.getItem(CHOICE + aid));
    return value && typeof value === 'object' ? value : {};
  } catch {
    return {};
  }
}

function saveChoice(aid, choice) {
  try {
    localStorage.setItem(CHOICE + aid, JSON.stringify(choice));
  } catch {
    // хранилище закрыто — выбор живёт до ухода со страницы
  }
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

export function chatView(host, { aid, pid, chat, agent, project, origin, navigate, openFile }) {
  const url = `/api/code/agents/${aid}/projects/${pid}/chats/${chat}`;
  const scroller = document.getElementById('view');
  let alive = true;
  let controller = null;         // поток (turn или live), который читает страница
  let retry = null;              // таймер следующего live
  let running = false;
  let sending = false;
  let turn = null;               // ответ идущего хода: { node, steps, md, asks, text }
  let start = 0;                 // номер первого показанного сообщения истории
  let paintTimer = null;

  const status = el('p', { id: 'code-status', class: 'error', 'aria-live': 'polite' });
  const more = el('button', { type: 'button', id: 'code-more', class: 'btn btn-sm code-more', hidden: true },
    'Показать раньше');
  const feed = el('div', { id: 'code-feed', class: 'code-feed' });
  const text = el('textarea', { id: 'code-text', rows: '1', 'aria-label': 'Задание', maxlength: '100000' });
  const models = Array.isArray(agent && agent.models) ? agent.models : [];
  const efforts = Array.isArray(agent && agent.efforts) ? agent.efforts : [];
  const model = el('select', { id: 'code-model', class: 'chip' },
    ...models.map((m) => el('option', { value: String(m.id) }, String(m.label || m.id))));
  const effort = el('select', { id: 'code-effort', class: 'chip' },
    ...efforts.map((e) => el('option', { value: String(e) }, String(e))));
  const saved = loadChoice(aid);
  if (models.some((m) => String(m.id) === saved.model)) model.value = saved.model;
  if (efforts.some((e) => String(e) === saved.effort)) effort.value = saved.effort;
  const send = el('button', { type: 'submit', id: 'code-send', class: 'btn btn-accent composer-go' }, 'Отправить');
  const stop = el('button', { type: 'button', id: 'code-stop', class: 'btn composer-go', hidden: true }, 'Остановить');
  const closeChat = el('button', { type: 'button', id: 'code-close-chat', class: 'btn btn-sm' }, 'Закрыть чат');
  const field = fieldBox(text);
  const form = el('form', { class: 'composer code-composer', id: 'code-composer' }, field.box, field.error,
    el('div', { class: 'composer-bar' },
      el('div', { class: 'composer-opts' },
        el('label', { class: 'code-pick' }, 'Модель', model), el('label', { class: 'code-pick' }, 'Усилие', effort)),
      stop, send));
  host.append(el('div', { class: 'code-chat-bar' }, closeChat),
    origin === 'other' ? el('p', { id: 'code-shared-note', class: 'code-shared-note' }, SHARED) : null,
    more, feed, status, form);

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
    if (m.role === 'user') return el('div', { class: 'code-msg user' }, typeof m.text === 'string' ? m.text : '');
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
    send.disabled = value || sending;
    host.dataset.running = value ? '1' : '0';
  }

  function closed() {
    navigate(`code?pin=${aid}`);
  }

  /* ---- карточки ----------------------------------------------------------------- */

  function askNode(a) {
    const card = el('div', { class: 'code-ask', 'data-request': String(a.request_id ?? '') });
    const error = el('p', { class: 'error code-ask-error', 'aria-live': 'polite' });
    const buttons = [];

    async function reply(body) {
      for (const b of buttons) b.disabled = true;
      error.textContent = '';
      try {
        await api('POST', `${url}/answer`, { request_id: a.request_id, ...body });
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
        return { q, box: el('fieldset', { class: 'code-ask-question' },
          q.header ? el('legend', {}, String(q.header)) : null, el('p', {}, String(q.question ?? '')), ...options) };
      });
      const go = el('button', { type: 'button', class: 'btn btn-accent code-ask-answer' }, 'Ответить');
      go.addEventListener('click', () => {
        const answers = {};
        for (const { q, box } of groups) {
          const inputs = [...box.querySelectorAll('input')];
          const picked = inputs.filter((i) => i.checked).map((i) => i.value);
          if (inputs.length && !picked.length) {
            inputs[0].focus();
            return;
          }
          answers[String(q.question ?? '')] = picked.join(', ');
        }
        reply({ decision: 'allow', answers });
      });
      buttons.push(go);
      card.append(...groups.map((g) => g.box), el('div', { class: 'code-ask-actions' }, go), error);
      return card;
    }

    const instead = el('textarea', { class: 'input code-ask-instead', rows: '2', maxlength: '2000',
      placeholder: INSTEAD, 'aria-label': INSTEAD });
    card.append(el('p', { class: 'code-ask-title' }, ASK_TITLE),
      el('b', { class: 'code-ask-tool' }, String(a.tool ?? '')),
      el('pre', { class: 'code-ask-input' }, JSON.stringify(a.input && typeof a.input === 'object' ? a.input : {}, null, 2)),
      el('div', { class: 'code-ask-actions' },
        action('code-ask-yes', 'Да', () => ({ decision: 'allow' }), true),
        a.always ? action('code-ask-always', 'Да, больше не спрашивать', () => ({ decision: 'always' })) : null,
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
      else if (data.status === 'error') end = `Ошибка: ${typeof data.message === 'string' ? data.message : ''}`;
      if (end) {
        const note = el('p', { class: `code-turn-end${data.status === 'error' ? ' error' : ' muted'}` }, end);
        grow(() => turn.node.append(note));
      }
    }
    turn = null;
    setRunning(false);
  }

  /** Событие потока; true — поток кончился. */
  function handle(event, data) {
    switch (event) {
      case 'turn':
        setRunning(true);
        current();
        return false;
      case 'idle':
        setRunning(false);
        if (turn) {                // ход кончился, пока страница не смотрела: история его уже знает
          turn = null;
          loadHistory(false);
        }
        return true;
      case 'snapshot': {
        setRunning(true);
        const t = current();
        t.text = typeof data.text === 'string' ? data.text : '';
        paintText();
        grow(() => {
          t.steps.replaceChildren(...(Array.isArray(data.steps) ? data.steps : []).map(stepNode));
          t.asks.replaceChildren(...(Array.isArray(data.asks) ? data.asks : []).map(askNode));
        });
        return false;
      }
      case 'text':
        current().text += typeof data.text === 'string' ? data.text : '';
        if (!paintTimer) paintTimer = setTimeout(paintText, PAINT_MS);
        return false;
      case 'step': {
        const t = current();
        grow(() => t.steps.append(stepNode({ id: data.id, tool: data.tool, input: data.input, text: '', error: false })));
        return false;
      }
      case 'result':
        for (const step of (turn ? turn.steps : feed).querySelectorAll('details.code-step')) {
          if (step.dataset.id !== String(data.id)) continue;
          const result = step.querySelector('.code-step-result');
          result.textContent = typeof data.text === 'string' ? data.text : '';
          result.hidden = !result.textContent;
          step.classList.toggle('error', !!data.error);
        }
        return false;
      case 'ask': {
        const t = current();
        dropAsk(data.request_id);
        grow(() => t.asks.append(askNode(data)));
        return false;
      }
      case 'asked':
        dropAsk(data.request_id);
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

  function waitForComputer() {
    setRunning(false);
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
      response = await api('GET', `${url}/live`, undefined, { signal: own.signal });
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
      page = await api('GET', url);
    } catch (err) {
      if (!alive) return;
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
      return;
    }
    if (!alive) return;
    feed.replaceChildren(...messages(page.items));
    turn = null;
    start = Number.isInteger(page.start) ? page.start : 0;
    more.hidden = start <= 0;
    host.dataset.loaded = '1';
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    if (thenLive) openLive();
  }

  /* ---- действия ----------------------------------------------------------------- */

  function autosize() {
    text.style.height = 'auto';
    if (text.value) text.style.height = `${text.scrollHeight}px`;
  }

  function remember() {
    saveChoice(aid, { model: model.value || null, effort: effort.value || null });
  }

  async function submit() {
    const value = text.value;
    if (!value.trim() || running || sending) return;
    sending = true;
    send.disabled = true;
    status.textContent = '';
    const mine = messageNode({ role: 'user', text: value });
    grow(() => feed.append(mine));
    text.value = '';
    autosize();
    remember();
    const own = take();
    let response;
    try {
      response = await api('POST', `${url}/turn`, { text: value, model: model.value || null,
        effort: effort.value || null }, { signal: own.signal });
    } catch (err) {
      if (!alive || own.signal.aborted) return;    // ушли с экрана: ход, может, и начат — live догонит
      if (controller === own) controller = null;
      mine.remove();
      if (!text.value) {
        text.value = value;
        autosize();
      }
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
      return;
    } finally {
      sending = false;
      send.disabled = running;
    }
    await follow(response, own);
  }

  more.addEventListener('click', async () => {
    more.disabled = true;
    try {
      const page = await api('GET', `${url}?before=${start}`);
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
      await api('POST', `${url}/stop`);
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
      await api('PATCH', url, { state: 'closed' });
      if (alive) navigate(`code/${aid}/${pid}`);
    } catch (err) {
      closeChat.disabled = false;
      if (err.code === 'closed') closed();
      else status.textContent = words(err);
    }
  });

  model.addEventListener('change', remember);
  effort.addEventListener('change', remember);
  text.addEventListener('input', autosize);
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
      drop();
    } else if (!sending) {
      openLive();
    }
  }
  document.addEventListener('visibilitychange', onVisibility);

  setRunning(false);
  loadHistory(true);

  return {
    unmount() {
      alive = false;
      document.removeEventListener('visibilitychange', onVisibility);
      drop();
      clearTimeout(retry);
      clearTimeout(paintTimer);
    },
  };
}
