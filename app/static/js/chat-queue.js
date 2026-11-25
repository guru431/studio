/* Очередь «После ответа» «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 22): до QUEUE_MAX сообщений
 * на диалог в памяти страницы, вид — queueView (code-queue.js) с ответами на его запросы здесь же. Первое сообщение
 * уходит само, когда хозяин зовёт `idle(id, true)` (ответ кончился, нет правки и ждущей карточки) и его диалог на
 * экране; id сообщения — `request_id` хода, так что повтор после обрыва связи не задваивает ход. Исходы `send(item)`:
 * принято — хозяин зовёт `done(id)`; `busy` — ход на другом устройстве, сообщение ждёт следующего `idle`; `unknown`
 * (ушло ли — неизвестно) — сообщение остаётся первым с отметкой `uncertain`, очередь на паузе со словами:
 * «Продолжить очередь» повторит его тем же `request_id` (принятое сервер не задвоит, а вернёт `replayed`), поздняя
 * квитанция «принято» уберёт его (`done`), «не принято» — хозяин зовёт `unsure(id)`; прочее — пауза до «Продолжить
 * очередь». Закрытие страницы (`pagehide`) дописывает неотправленное к черновику диалога через пустую строку; у
 * черновика с сообщением `uncertain` — квитанция `receipt` (его id): тот же текст уйдёт тем же `request_id`. */
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';
import { tr } from './i18n.js';
import { queueView } from './code-queue.js';

export const QUEUE_MAX = 5;
const queues = new Map();        // id диалога → { items: [{ id, text, version, sending, uncertain }], paused }
let current = null;              // вид на экране: { paint() }

function entryOf(id) {
  if (!queues.has(id)) queues.set(id, { items: [], paused: false });
  return queues.get(id);
}

function forget(id) {
  const entry = queues.get(id);
  if (entry && !entry.items.length) queues.delete(id);
}

window.addEventListener('pagehide', () => {
  const drafts = draftStore(CHAT_DRAFTS);
  for (const [id, entry] of queues) {
    // уходящее (`sending`) не возвращаем: сервер мог его принять — в черновике оно ушло бы второй раз
    const kept = entry.items.filter((item) => !item.sending);
    if (!kept.length) continue;
    const typed = drafts.saved()[id]?.text || '';
    // ушло ли — неизвестно: с квитанцией тот же текст уйдёт тем же `request_id` (chat-drafts.js `receipt`)
    drafts.save(id, [typed, ...kept.map((item) => item.text)].filter(Boolean).join('\n\n'),
      kept.find((item) => item.uncertain)?.id);
  }
  for (const entry of queues.values()) entry.items.length = 0;   // идущий `send` узнает, что очереди больше нет
  queues.clear();
  current?.paint();
});

export function chatQueue(host, { send, failed }) {
  let shown = null;
  const free = new Map();          // id диалога → последний `free` хозяина
  const view = queueView(host, { endpoint: (tail) => tail, request, failed, labels: { region: tr("Очередь сообщений"),
    waiting: tr("После ответа"), field: tr("Сообщение из очереди"), edit: tr("Изменить ожидающее сообщение"),
    remove: tr("Удалить ожидающее сообщение") } });
  const self = { paint };
  current = self;

  function paint() {
    const entry = queues.get(shown);
    view.update(entry ? entry.items.map(({ id, text, version }) => ({ id, text, version })) : [], !!entry?.paused);
  }

  function gone() {
    return Object.assign(new Error(tr("Сообщение уже отправлено")), { code: 'gone' });
  }

  /* запросы вида: пауза на время правки и «Продолжить очередь»; правка и удаление — по `request_id` */
  async function request(method, path, body) {
    const entry = entryOf(shown);
    if (path === '/queue/pause') {
      entry.paused = !!body.paused;
      if (!entry.paused) setTimeout(() => idle(shown, free.get(shown)), 0);
    } else {
      const item = entry.items.find((row) => row.id === body.request_id);
      if (!item || item.sending) throw gone();
      if (body.text === null || !body.text.trim()) entry.items.splice(entry.items.indexOf(item), 1);
      else {
        item.text = body.text;
        item.version += 1;
      }
    }
    const out = { items: entry.items.map(({ id, text, version }) => ({ id, text, version })) };
    forget(shown);
    return out;
  }

  async function run(id, entry, item) {
    item.sending = true;
    paint();
    let outcome = 'failed';
    try {
      outcome = await send(item);
    } catch {
      // отказ — словами у хозяина
    }
    if (!entry.items.includes(item)) return;        // принято (done) или страница уже закрывается
    if (outcome === 'unknown') { unsure(item.id); return; }
    item.sending = false;
    if (outcome !== 'busy') entry.paused = true;
    if (current === self) paint();
  }

  /* ушло ли сообщение `itemId` — неизвестно: оно снова в руках человека, очередь на паузе со словами */
  function unsure(itemId) {
    for (const [id, entry] of queues) {
      const item = entry.items.find((row) => row.id === itemId);
      if (!item) continue;
      Object.assign(item, { sending: false, uncertain: true });
      entry.paused = true;
      if (id === shown && current === self) {
        paint();
        failed(new Error(tr("Не удалось узнать, ушло ли сообщение — «Продолжить очередь» повторит его без задвоения")));
      }
    }
  }

  function idle(id, isFree) {
    free.set(id, isFree);
    if (!isFree || id !== shown || current !== self) return;
    const entry = queues.get(id);
    const item = entry?.items[0];
    if (!item || entry.paused || item.sending) return;
    run(id, entry, item);
  }

  return {
    /** Текст в очередь диалога `id`; false — очередь полна. */
    add(id, text) {
      const entry = entryOf(id);
      if (entry.items.length >= QUEUE_MAX) return false;
      entry.items.push({ id: crypto.randomUUID(), text, version: 1, sending: false });
      if (id === shown) paint();
      return true;
    },
    /** На экране диалог `id` (null — новый): его очередь в виде. */
    show(id) {
      shown = id;
      paint();
    },
    idle,
    /** Квитанция: ход сообщения `itemId` сервер не принимал (или исход неизвестен) — пауза со словами. */
    unsure,
    /** Ход сообщения `itemId` принят сервером. */
    done(itemId) {
      for (const [id, entry] of queues) {
        const at = entry.items.findIndex((item) => item.id === itemId);
        if (at < 0) continue;
        entry.items.splice(at, 1);
        forget(id);
        if (id === shown && current === self) paint();
      }
    },
    unmount() {
      if (current === self) current = null;
      view.unmount();
    },
  };
}
