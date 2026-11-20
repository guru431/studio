/* Один владелец управления на страницу: чат и массовый перенос при обновлении используют тот же id.
 * Перезагрузка из refresh.js передаёт id в history.state (`studioWindow`): окно после неё остаётся владельцем своих
 * сессий и ничего не отбирает у других окон. Метка снимается сразу — копия вкладки её не унаследует.
 * Перезагрузка браузером (F5, выгрузка вкладки) передаёт id одноразовой записью sessionStorage: она пишется на
 * pagehide и снимается при загрузке или возврате из bfcache — копия живой вкладки её не застанет. */
const KEY = 'studioWindow';
const valid = (value) => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value) ? value : null;
const kept = history.state?.[KEY];
let handed = null;
try {
  handed = sessionStorage.getItem(KEY);
  sessionStorage.removeItem(KEY);
} catch { /* хранилище недоступно — новое окно */ }
export const WINDOW = valid(kept) || valid(handed) || crypto.randomUUID().replaceAll('-', '');
if (kept !== undefined) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}
addEventListener('pagehide', () => {
  try { sessionStorage.setItem(KEY, WINDOW); } catch { /* без передачи — после загрузки окно новое */ }
});
addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  try { sessionStorage.removeItem(KEY); } catch { /* нечего снимать */ }
});
