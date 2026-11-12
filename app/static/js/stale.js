/* Статика после выкладки. Служба держит снимок только своей версии (`/static/v/<hash>/`): вкладка или PWA,
 * открытые до выкладки, при первом открытии раздела получают 404 на модули прежнего хеша.
 *
 * `lazy(load, what)` — ленивый import() раздела или его части. Отказ сверяет хеш страницы (адрес этого модуля)
 * со `static` из /health: другой — плашка «Студия обновилась» с кнопкой «Обновить» (location.reload — работает
 * и в standalone-PWA без адресной строки) и отказ code 'updated'; тот же или нет связи — прежние слова о связи
 * (code 'module'). Английский TypeError браузера наружу не выходит. `check()` — та же сверка для отказа обычной
 * статики (скрипты Markdown). Навигацию служебный работник берёт из сети: перезагрузка получает новую оболочку. */
import { el } from './ui.js';

const PAGE = (/\/static\/v\/([0-9a-f]+)\//.exec(import.meta.url) || [])[1] || null;
export const UPDATED = 'Студия обновилась — нажмите «Обновить» вверху страницы.';

let stale = false;
let asking = null;

/** Плашка одна на страницу, поверх раздела; остаётся до перезагрузки. */
function bar() {
  if (document.getElementById('studio-updated')) return;
  const go = el('button', { type: 'button', class: 'btn btn-accent', id: 'studio-update' }, 'Обновить');
  go.addEventListener('click', () => location.reload());
  document.body.append(el('div', { id: 'studio-updated', class: 'update-bar', role: 'alert' },
    el('span', {}, 'Студия обновилась'), go));
}

/** true — служба уже другой версии (плашка показана); тот же хеш, нет связи, нет ответа — false. */
export function check() {
  if (stale) return Promise.resolve(true);
  if (!asking) {
    asking = (async () => {
      try {
        const answer = await fetch('/health', { cache: 'no-store', credentials: 'same-origin' });
        const current = answer.ok ? (await answer.json()).static : null;
        stale = !!PAGE && typeof current === 'string' && current !== PAGE;
      } catch {
        return false;
      } finally {
        asking = null;
      }
      if (stale) bar();
      return stale;
    })();
  }
  return asking;
}

/** Модуль из load() или отказ русскими словами: what — «Раздел не загрузился», «Чат не загрузился»… */
export async function lazy(load, what = 'Раздел не загрузился') {
  // Перезагрузка и переход обрывают идущий import() раньше pagehide: сверять версию тогда незачем, а запрос
  // /health уходящего документа WebKit сам называет ошибкой страницы. beforeunload приходит до обрыва; слушатель —
  // только пока идёт импорт, чтобы не лишать страницу bfcache.
  let leaving = false;
  const leave = () => { leaving = true; };
  addEventListener('beforeunload', leave);
  try {
    return await load();
  } catch {
    if (!leaving && await check()) throw Object.assign(new Error(UPDATED), { code: 'updated' });
    throw Object.assign(new Error(`${what}. Проверьте связь и обновите страницу.`), { code: 'module' });
  } finally {
    removeEventListener('beforeunload', leave);
  }
}
