/* Панель раздела «Claude Code» (спека 2026-10-29-studio-code-workspace-design, 6.1–6.3): компьютеры учётки строками,
 * под ними — проекты выбранного компьютера, поле PIN или «не на связи». Блок компьютеров не прокручивается, низ — да.
 *
 * panelView(host, { selected, opened, chose, list }) → { select, pin, here, refresh, agents, unmount }:
 * - компьютеры — GET /api/agents (без моста): точка «на связи» (слова — для чтения с экрана), имя, «PIN»; версия Claude
 *   Code — подсказкой; agents() — последний список (обещание, пока показан раздел; не прочитался — null);
 * - показ компьютера (вход в раздел — selected, иначе первый; нажатие на имя): не на связи — p#code-offline, запросов
 *   нет; на связи — POST …/open {pin: null} (как строка компьютера прежде): 204 — его проекты (list(aid) из code.js —
 *   GET …/projects), 409 pin_needed — поле PIN (#code-pin: не <form> и не password — менеджеры паролей не предложат
 *   сохранить PIN; Enter и «Открыть»);
 * - select(aid) — нажатие на имя: показ и act('panel'); уже выбранный и показанный (проекты, поле PIN, ждёт ответа) —
 *   ничего, поле PIN — в фокус. Адрес и правую часть не трогает;
 * - pin(aid) — отказ closed справа и ?pin=: компьютер с PIN — сразу поле, без PIN — POST …/open; идущий POST …/open
 *   этого компьютера не повторяется — его итог и так придёт в opened или в поле PIN;
 * - opened(aid) — компьютер открыт (204 или верный PIN): code.js вернёт правую часть туда, где застал отказ;
 * - here(aid, pid) — открытый справа проект (aria-current); refresh() — числа вкладок проектов (code-workspace.js);
 *   chose() — нажат проект (телефон прячет панель).
 * PIN живёт только в поле и переменной обработчика, после ответа поле очищается. Текст сервера — только textContent. */
import { api } from './api.js';
import { act, count, get } from './code-workspace.js';
import { attemptsLeft } from './lock.js';
import { el, words } from './ui.js';

const OFFLINE = 'Компьютер не на связи — включите его и запустите мост';
const PIN_LOCKED = 'PIN заблокирован после 5 ошибок — разблокируйте его в окне моста на самом компьютере';

export function panelView(host, { selected: first = null, opened, chose, list }) {
  const agentsBox = el('ul', { id: 'code-agents', class: 'code-agents' });
  const body = el('div', { class: 'code-panel-body' });
  const status = el('p', { id: 'code-panel-status', class: 'error', 'aria-live': 'polite' });
  const root = el('aside', { id: 'code-panel', class: 'code-panel', 'aria-label': 'Компьютеры и проекты' },
    el('div', { class: 'code-panel-agents' }, agentsBox), body, status);
  host.append(root);

  let alive = true;
  let gen = 0;             // номер показа компьютера: ответ прежнего показ не трогает
  let items = [];          // последний GET /api/agents
  let selected = null;     // id показанного компьютера
  let mode = 'idle';       // idle | loading | projects | pin | offline | error
  let shown = null;        // { aid, pid } — проект справа
  let pending = 0;         // select/pin ждут списка: выбор при входе их не перебивает

  /** Список компьютеров; не прочитался — null (прежний список остаётся, «компьютеров нет» не сказать). */
  async function readAgents() {
    try {
      items = (await api('GET', '/api/agents')).items;
    } catch (err) {
      if (alive) status.textContent = words(err);
      return null;
    }
    if (alive) paintAgents();
    return items;
  }

  let loaded = readAgents();
  loaded.then((got) => {
    if (!alive || pending || !got || !got.length) return;
    show(got.some((a) => a.id === first) ? first : got[0].id, 'open');
  });

  function paintAgents() {
    agentsBox.replaceChildren(...items.map((agent) => {
      const row = el('button', { type: 'button', class: 'code-agent', 'aria-current': agent.id === selected ? 'true' : null,
        title: agent.online && agent.cc_version ? `Claude Code ${agent.cc_version}` : null },
      el('span', { class: `code-agent-state${agent.online ? ' on' : ''}` },
        el('span', { class: 'sr' }, agent.online ? 'на связи' : 'не на связи')),
      el('span', { class: 'code-agent-name' }, agent.name),
      agent.has_pin ? el('span', { class: 'tag code-agent-pin' }, 'PIN') : null);
      row.addEventListener('click', () => select(agent.id));
      return el('li', { 'data-id': agent.id }, row);
    }));
  }

  function paintSelected() {
    for (const row of agentsBox.querySelectorAll('li[data-id]')) {
      const button = row.querySelector('.code-agent');
      if (Number(row.dataset.id) === selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  }

  function paintHere() {
    for (const link of body.querySelectorAll('#code-projects a.code-project')) {
      const li = link.closest('li');
      if (shown && shown.aid === selected && li.dataset.pid === shown.pid) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  }

  function paintCounts() {
    for (const li of body.querySelectorAll('#code-projects li[data-pid]')) {
      const n = count(selected, li.dataset.pid);
      li.querySelector('.code-project-count').textContent = n ? String(n) : '';
    }
  }

  /** Компьютер в панели: kind — open (вход, нажатие) или pin (отказ closed справа). */
  function show(aid, kind) {
    const mine = ++gen;
    selected = aid;
    paintSelected();
    status.textContent = '';
    const agent = items.find((a) => a.id === aid);
    if (!agent) {
      mode = 'idle';
      body.replaceChildren();
      return;
    }
    if (!agent.online) {
      offline();
      return;
    }
    mode = 'loading';
    body.replaceChildren();               // прежний список или поле PIN до ответа не остаётся
    if (kind === 'pin' && agent.has_pin) showPin(mine, agent);
    else openIt(mine, agent);
  }

  function offline() {
    mode = 'offline';
    body.replaceChildren(el('p', { id: 'code-offline', class: 'muted code-offline' }, OFFLINE));
  }

  function refused(err) {
    mode = 'error';
    if (err && err.code === 'offline') offline();
    else status.textContent = words(err);
  }

  async function openIt(mine, agent) {
    try {
      await api('POST', `/api/code/agents/${agent.id}/open`, { pin: null });
    } catch (err) {
      if (!alive || mine !== gen) return;
      if (err.code === 'pin_needed') showPin(mine, agent);
      else if (err.code === 'pin_locked') {
        mode = 'error';
        status.textContent = PIN_LOCKED;
      } else refused(err);
      return;
    }
    if (!alive || mine !== gen) return;
    opened(agent.id);
    projects(mine, agent);
  }

  async function projects(mine, agent) {
    let found;
    try {
      found = await list(agent.id, true);
    } catch (err) {
      if (alive && mine === gen) refused(err);
      return;
    }
    if (!alive || mine !== gen) return;
    mode = 'projects';
    const rows = found.map((p) => {
      const link = el('a', { class: 'code-project', href: `#/code/${agent.id}/${p.pid}` },
        el('span', { class: 'code-project-name' }, p.name), el('span', { class: 'code-project-count' }));
      link.addEventListener('click', () => chose());
      return el('li', { 'data-pid': p.pid }, link);
    });
    body.replaceChildren(el('ul', { id: 'code-projects', class: 'code-projects' }, ...rows));
    paintCounts();
    paintHere();
  }

  function showPin(mine, agent) {
    mode = 'pin';
    // не <form> и не password: исчезнув после удачного запроса, они дали бы менеджеру паролей сохранить PIN
    // компьютера в синхронизируемое хранилище браузера; точки вместо цифр — -webkit-text-security (app.css)
    const input = el('input', { id: 'code-pin-input', class: 'input', type: 'text', inputmode: 'numeric',
      autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', maxlength: '64',
      'aria-label': 'PIN компьютера' });
    const go = el('button', { id: 'code-pin-open', class: 'btn btn-accent', type: 'button' }, 'Открыть');
    const error = el('p', { id: 'code-pin-error', class: 'error', 'aria-live': 'polite' });
    const box = el('div', { id: 'code-pin', class: 'code-pin', role: 'group', 'aria-label': 'PIN компьютера', 'data-id': agent.id },
      el('h2', {}, 'PIN компьютера'), el('p', { class: 'muted code-pin-name' }, agent.name),
      el('div', { class: 'code-pin-row' }, input, go), error);
    go.addEventListener('click', send);
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.isComposing) return;
      event.preventDefault();
      send();
    });
    async function send() {
      if (go.disabled || !input.value) return;
      const pin = input.value;
      go.disabled = true;
      error.textContent = '';
      try {
        await api('POST', `/api/code/agents/${agent.id}/open`, { pin });
        if (!alive || mine !== gen) return;
        mode = 'loading';
        body.replaceChildren();
        opened(agent.id);
        projects(mine, agent);
        return;
      } catch (err) {
        if (!alive || mine !== gen) return;
        const left = err.detail && err.detail.left;
        if (err.code === 'pin') error.textContent = Number.isInteger(left) ? `Неверный PIN — ${attemptsLeft(left)}` : 'Неверный PIN';
        else if (err.code === 'pin_locked') error.textContent = PIN_LOCKED;
        else if (err.code === 'offline') error.textContent = OFFLINE;
        else error.textContent = words(err);
      } finally {
        input.value = '';
        go.disabled = false;
      }
      input.focus();
    }
    body.replaceChildren(box);
    input.focus();
  }

  /** Выбор и pin ждут списка компьютеров; выбранный при входе их не перебивает. */
  async function settled() {
    pending += 1;
    try {
      await loaded;
    } finally {
      pending -= 1;
    }
    return alive;
  }

  function focusPin() {
    const input = body.querySelector('#code-pin-input');
    if (input) input.focus();
  }

  async function select(aid) {
    if (!(await settled())) return;
    aid = Number(aid);
    if (aid === selected && (mode === 'projects' || mode === 'pin' || mode === 'loading')) {
      focusPin();
      return;
    }
    if (get().panel !== aid) act('panel', { aid });
    const agent = items.find((a) => a.id === aid);
    if (!agent || !agent.online) {        // не на связи по прежнему списку — вдруг уже на связи
      loaded = readAgents();
      await loaded;
      if (!alive) return;
    }
    show(aid, 'open');
  }

  async function pin(aid) {
    if (!(await settled())) return;
    aid = Number(aid);
    if (aid === selected && (mode === 'pin' || mode === 'loading')) {     // идущий open сам придёт в opened или в поле
      focusPin();
      return;
    }
    const agent = items.find((a) => a.id === aid);
    if (!agent || !agent.online) {
      loaded = readAgents();
      await loaded;
      if (!alive) return;
    }
    show(aid, 'pin');
  }

  return {
    select,
    pin,
    here(aid, pid) {
      shown = aid === null ? null : { aid: Number(aid), pid };
      paintHere();
    },
    refresh: paintCounts,
    agents: () => loaded,
    unmount() {
      alive = false;
      gen += 1;
      root.remove();
    },
  };
}
