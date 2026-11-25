import { locale, tr } from './i18n.js';
/* Заполнение контекста в строке состояния чата (спека 2026-11-21-studio-source-capabilities-design, п. 33; спека
 * 2026-11-25-studio-skills-status-local-models, п. 13): кольцо 18 px — кнопка окна «Диалог и контекст» (`open`).
 * Оценка — POST /api/chats/<id>/estimate {text, attachment_ids} → {tokens, limit | null} (системное сообщение,
 * история, текст и вложения хода); запрос — через DELAY_MS после ввода или смены вложений. Доля от 0.7 — `.ctx-warn`,
 * от 0.9 — `.ctx-full`; подсказка — «Контекст ~62 % (81 000 из 131 072)». Предел не известен или оценка не удалась —
 * `.ctx-none`, «Контекст: нет данных»; оценивать нечего (новый диалог) — кольца нет. */
import { el } from './ui.js';

const DELAY_MS = 800;
const WARN = 0.7;
const FULL = 0.9;

/** Кольцо: `request()` — Promise оценки {tokens, limit} или null (оценивать нечего); `open()` — окно по нажатию.
 * → { node, schedule, hide } */
export function contextRing(request, { open } = {}) {
  const ring = el('span', { class: 'ctx-ring', 'aria-hidden': 'true' });
  const node = el('button', { type: 'button', class: 'ctx-meter', id: 'ctx-meter', 'data-status': 'ring',
    'aria-haspopup': 'dialog', hidden: true }, ring);
  if (open) node.addEventListener('click', () => open());
  let timer = null;
  let seq = 0;

  function paint(out) {
    if (!out) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    const known = Number.isFinite(out.limit) && out.limit > 0 && Number.isFinite(out.tokens);
    node.classList.toggle('ctx-none', !known);
    if (!known) {
      const words = tr("Контекст: нет данных");
      node.style.setProperty('--context-percent', '0%');
      node.classList.remove('ctx-warn', 'ctx-full');
      node.title = words;
      node.setAttribute('aria-label', words);
      return;
    }
    const share = out.tokens / out.limit;
    const n = (v) => v.toLocaleString(locale());
    const words = tr("Контекст ~{0} % ({1} из {2})", Math.round(share * 100), n(out.tokens), n(out.limit));
    node.style.setProperty('--context-percent', `${Math.min(100, share * 100)}%`);   // как кольцо «Агентов»
    node.classList.toggle('ctx-warn', share >= WARN && share < FULL);
    node.classList.toggle('ctx-full', share >= FULL);
    node.title = words;
    node.setAttribute('aria-label', words);
  }

  async function run() {
    const mine = ++seq;
    let out = null;
    try {
      out = await request();
    } catch {
      out = { tokens: null, limit: null };         // оценка — подсказка: отказ — «нет данных», а не слова ошибки
    }
    if (mine === seq) paint(out);
  }

  return {
    node,
    /** Оценить заново через DELAY_MS (прежний отложенный запрос отменяется); `now` — сразу (открыли диалог). */
    schedule(now = false) {
      clearTimeout(timer);
      if (now) run();
      else timer = setTimeout(run, DELAY_MS);
    },
    hide() {
      clearTimeout(timer);
      seq++;
      node.hidden = true;
    },
  };
}
