/* Один владелец управления на страницу: чат и массовый перенос при обновлении используют тот же id.
 * Перезагрузка из refresh.js передаёт id в history.state (`studioWindow`): окно после неё остаётся владельцем своих
 * сессий и ничего не отбирает у других окон. Метка снимается сразу — копия вкладки её не унаследует. */
const KEY = 'studioWindow';
const kept = history.state?.[KEY];
export const WINDOW = typeof kept === 'string' && /^[0-9a-f]{32}$/.test(kept) ? kept : crypto.randomUUID().replaceAll('-', '');
if (kept !== undefined) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}
