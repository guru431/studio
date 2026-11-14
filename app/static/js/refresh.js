/* Полная перезагрузка сохраняет адрес чата и метку истории. Одноразовый флаг в записи
 * переживает reload без local/sessionStorage и просит раздел заново прочитать кэши моста. */
const KEY = 'studioRefresh';
export const refreshRequested = history.state?.[KEY] === true;
if (refreshRequested) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}

export function reloadStudio() {
  history.replaceState({ ...history.state, [KEY]: true }, '');
  location.reload();
}
