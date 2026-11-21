import { message as serverMessage, tr } from './i18n.js';
/* «Прочитать» (п. 12, 28 спеки 2026-11-21-studio-source-capabilities-design): текст ответа чата, перевода или скана —
 * новым чатом «Озвучки» (POST /api/speeches/read) источником по умолчанию раздела. Проигрыватель на месте (chatPlayer):
 * записи чата — части длинного текста — играют по порядку по мере готовности (опрос GET /api/speeches/chats/<id>
 * раз в POLL_MS), «Скачать» — готовые части одним mp3 (GET /api/speeches/chats/<id>/audio), «Отменить» — весь чат
 * (POST /api/speeches/chats/<id>/cancel; повторное «Прочитать» в том же месте отменяет прежний чат). Умолчание раздела —
 * инструмент MCP (409 `read_mcp`): его запускает sectionMcp('speech').run, как раздел «Озвучка», результат — его
 * вывод. Проигрыватель и опрос живут, пока узел-хозяин на странице (ушёл — sweep останавливает). */
import { api } from './api.js';
import { kick } from './jobs.js';
import { sectionMcp } from './mcp-sections.js';
import { el, words } from './ui.js';

const POLL_MS = 3000;
const TERMINAL = new Set(['done', 'error', 'cancelled']);
const MCP_FIELDS = ['text', 'voice', 'model', 'engine', 'reference_text'];

const active = new Set();        // { host, stop, seen } — проигрыватели «Прочитать» на странице
const players = new WeakMap();   // host → запись active
let sweeper = null;

function sweep() {
  for (const entry of active) {
    if (entry.host.isConnected) entry.seen = true;
    else if (entry.seen) {
      active.delete(entry);
      entry.stop();
    }
  }
  if (!active.size) {
    clearInterval(sweeper);
    sweeper = null;
  }
}

function track(host, stop) {
  const entry = { host, stop, seen: host.isConnected };
  active.add(entry);
  players.set(host, entry);
  sweeper ??= setInterval(sweep, POLL_MS);
}

/** Остановить «Прочитать» в узле и очистить его. */
export function clearRead(host) {
  const entry = players.get(host);
  if (entry) {
    active.delete(entry);
    players.delete(host);
    entry.stop();
  }
  host.replaceChildren();
}

/** Записи чата озвучки `chatId` по порядку в одном плеере: готовая — сразу, следующая — по `ended`, а пока её нет —
 * после опроса. Кончившаяся без звука (ошибка, отмена) пропускается словами. `download` — кнопка «Скачать»,
 * `cancel` — «Отменить» весь чат, пока в нём что-то ждёт или идёт (POST /api/speeches/chats/<id>/cancel). */
export function chatPlayer(chatId, { download = true, cancel = true } = {}) {
  const audio = el('audio', { class: 'sp-audio read-audio', controls: true, preload: 'auto' });
  const note = el('p', { class: 'muted small', 'aria-live': 'polite' });
  const save = el('a', { class: 'btn btn-sm read-download', download: '', hidden: true,
    href: `/api/speeches/chats/${encodeURIComponent(chatId)}/audio` }, tr("Скачать"));
  const stopButton = el('button', { type: 'button', class: 'btn btn-sm read-cancel', hidden: true }, tr("Отменить"));
  const box = el('div', { class: 'read-player' }, note, audio, ...(download ? [save] : []), ...(cancel ? [stopButton] : []));
  let items = [];
  let next = 0;
  let waiting = true;            // плеер ждёт следующую готовую запись
  let timer = null;
  let stopped = false;
  const ready = (item) => TERMINAL.has(item.state) && !!item.media_id;

  function play() {
    while (next < items.length && TERMINAL.has(items[next].state) && !items[next].media_id) next += 1;
    const item = items[next];
    waiting = !item || !ready(item);
    if (waiting) return;
    audio.src = `/api/media/${encodeURIComponent(item.media_id)}`;
    next += 1;
    audio.play().catch(() => {});      // без жеста автозапуск может быть запрещён: «Play» — у человека
  }

  function paint() {
    const done = items.filter(ready).length;
    const failed = items.find((i) => i.state === 'error' && !i.media_id);
    const running = items.find((i) => !TERMINAL.has(i.state));
    note.className = failed ? 'error small' : 'muted small';
    note.textContent = failed ? serverMessage(failed.detail)
      : items.length > 1 ? tr("Готово частей: {0} из {1}", done, items.length)
        : running ? serverMessage(running.detail) : '';
    save.hidden = !done;
    stopButton.hidden = !running;
  }

  /** Весь чат — отменить на сервере (ждущие части снимаются, идущая останавливается); итог — опросом. */
  async function cancelChat() {
    stopButton.disabled = true;
    try {
      await api('POST', `/api/speeches/chats/${encodeURIComponent(chatId)}/cancel`);
      kick();
    } catch (err) {
      note.className = 'error small';
      note.textContent = words(err);
    } finally {
      stopButton.disabled = false;
    }
    if (!stopped) {
      clearTimeout(timer);
      timer = setTimeout(poll, 0);
    }
  }
  stopButton.addEventListener('click', cancelChat);

  async function poll() {
    timer = null;
    if (stopped) return;
    try {
      const chat = await api('GET', `/api/speeches/chats/${encodeURIComponent(chatId)}`);
      if (stopped) return;
      items = chat.items;
      paint();
      if (waiting) play();
      if (items.some((i) => !TERMINAL.has(i.state))) timer = setTimeout(poll, POLL_MS);
    } catch (err) {
      if (stopped) return;
      note.className = 'error small';
      note.textContent = words(err);
      if (err.status !== 404) timer = setTimeout(poll, POLL_MS);
    }
  }

  audio.addEventListener('ended', play);
  timer = setTimeout(poll, 0);
  return {
    box,
    cancel: cancelChat,
    /** В чате что-то ждёт или идёт (по последнему опросу; до первого — да). */
    get running() { return !items.length || items.some((i) => !TERMINAL.has(i.state)); },
    stop() {
      stopped = true;
      clearTimeout(timer);
      audio.pause();
    },
  };
}

/** Инструмент MCP по умолчанию раздела «Озвучка» — тем же путём, что раздел; голос — по умолчанию схемы. */
function readMcp(text, host) {
  const status = el('p', { class: 'muted small', 'aria-live': 'polite' }, tr("Ищу инструмент озвучки MCP…"));
  let started = false;
  const mcp = sectionMcp('speech', { native: false, fields: MCP_FIELDS, async onChange() {
    if (started || !mcp.tool) return;
    started = true;
    status.hidden = true;
    const prop = mcp.argument('voice');
    const choices = Array.isArray(prop?.enum) ? prop.enum : [];
    await mcp.run({ text, voice: choices.includes(prop.default) ? prop.default : choices[0] });
  } });
  host.replaceChildren(status, mcp.output);
  track(host, () => mcp.destroy());
}

/** «Прочитать» `text` (Markdown — сервер снимет разметку) из `origin` {kind: 'chat'|'translate'|'ocr', id}: в `host` —
 * состояние, затем проигрыватель. Повторное нажатие в том же узле заменяет прежний проигрыватель и отменяет его
 * чат, если тот ещё не озвучен (иначе его части ждали бы в очереди впереди нового). */
export async function readAloud(text, origin, host) {
  const previous = players.get(host)?.player;
  if (previous?.running) previous.cancel();
  clearRead(host);
  const status = el('p', { class: 'muted small', 'aria-live': 'polite' }, tr("Готовлю озвучку…"));
  host.replaceChildren(status);
  let cancelled = false;
  track(host, () => { cancelled = true; });
  try {
    const { chat_id: chatId } = await api('POST', '/api/speeches/read', { text, origin });
    if (cancelled) return;
    kick();
    const player = chatPlayer(chatId);
    Object.assign(players.get(host), { stop: player.stop, player });
    host.replaceChildren(player.box);
  } catch (err) {
    if (cancelled) return;
    if (err.code === 'read_mcp') {
      clearRead(host);
      readMcp(text, host);
      return;
    }
    status.className = 'error small';
    status.textContent = words(err);
  }
}
