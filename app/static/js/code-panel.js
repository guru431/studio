/* Панель раздела «Claude Code» (спека 2026-10-29-studio-code-workspace-design, 6.1–6.3): компьютеры учётки строками,
 * под ними — проекты выбранного компьютера, поле PIN или «не на связи». Блок компьютеров не прокручивается, низ — да.
 *
 * panelView(host, { selected, opened, chose, list }) → { select, pin, here, refresh, agents, unmount }:
 * - компьютеры — GET /api/agents (без моста): точка «на связи» (слова — для чтения с экрана), имя, «PIN»; версия Claude
 *   Code — подсказкой; agents() — последний список (обещание, пока показан раздел; не прочитался — null);
 * - показ компьютера (вход в раздел — selected, иначе первый; нажатие на имя): не на связи — p#code-offline, запросов
 *   нет; на связи — GET …/projects, только closed ведёт к POST …/open {pin: null}: 204 — его проекты,
 *   409 pin_needed — поле PIN (#code-pin: не <form> и не password — менеджеры паролей не предложат
 *   сохранить PIN; Enter и «Открыть»);
 * - select(aid) — нажатие на имя: показ и act('panel'); уже выбранный и показанный (проекты, поле PIN, ждёт ответа) —
 *   проекты — перечитать, поле PIN — в фокус, идущий запрос — не повторять. Адрес и правую часть не трогает;
 * - pin(aid) — отказ closed справа и ?pin=: компьютер с PIN — сразу поле, без PIN — POST …/open; идущий POST …/open
 *   этого компьютера не повторяется — его итог и так придёт в opened или в поле PIN;
 * - opened(aid) — компьютер открыт (204 или верный PIN): code.js вернёт правую часть туда, где застал отказ;
 * - ready(aid) — правая часть ждёт идущего POST …/open этого компьютера (вход в раздел сразу в проект: иначе её
 *   запросы обгоняли бы открытие и получали closed); false — компьютер ждёт PIN;
 * - here(aid, pid) — открытый справа проект (aria-current); числа вкладок проектов (code-workspace.js, без запросов к
 *   мосту) — на каждую смену рабочего места (subscribe), refresh() — то же вручную;
 *   chose() — нажат проект (телефон прячет панель).
 * PIN живёт только в поле и переменной обработчика, после ответа поле очищается. Текст сервера — только textContent. */
import { api } from './api.js';
import { act, count, get, subscribe } from './code-workspace.js';
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
  let activityTimer = null;
  let activityRequested = false;
  let lastAction = Date.now();
  const activity = new Map();
  function scheduleActivity() {
    clearTimeout(activityTimer);
    if (!alive || document.visibilityState === 'hidden' || mode !== 'projects'
      || Date.now() - lastAction > 1800000) return;
    activityTimer = setTimeout(() => { activityTimer = null; refreshActivity(); }, 20000);
  }
  async function refreshActivity() {
    const aid = selected;
    if (activityRequested || mode !== 'projects' || !items.find((a) => a.id === aid)?.project_activity) return;
    activityRequested = true;
    try {
      const data = await api('GET', `/api/code/agents/${aid}/activity`);
      if (alive && selected === aid && mode === 'projects') {
        activity.set(aid, new Map((data.items || []).map((item) => [item.chat, item])));
        paintCounts();
      }
    } catch {
      if (alive && selected === aid) { activity.delete(aid); paintCounts(); }
    } finally {
      activityRequested = false;
      scheduleActivity();
    }
  }
  function onActivity(event) {
    const item = event.detail;
    if (!activity.has(item.aid)) activity.set(item.aid, new Map());
    if (item.running) activity.get(item.aid).set(item.chat, { ...item, project: item.pid });
    else activity.get(item.aid).delete(item.chat);
    paintCounts();
  }
  function onAction() { lastAction = Date.now(); if (!activityTimer) scheduleActivity(); }
  function onVisibility() {
    if (document.visibilityState === 'visible') { lastAction = Date.now(); refreshActivity(); }
    else clearTimeout(activityTimer);
  }
  document.addEventListener('studio-code-activity', onActivity);
  document.addEventListener('visibilitychange', onVisibility);
  host.addEventListener('pointerdown', onAction);
  host.addEventListener('keydown', onAction);
  let mode = 'idle';       // idle | loading | projects | pin | offline | error
  let shown = null;        // { aid, pid } — проект справа
  let pending = 0;         // select/pin ждут списка: выбор при входе их не перебивает
  let opening = null;      // идущий POST …/open: { aid, done } — done: открылся ли (ready ждёт его)

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

  const unsubscribe = subscribe(() => paintCounts());   // числа вкладок — после каждого действия и ответа сервера
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
      li.querySelector('.code-project-count').title = `Открыто вкладок: ${n}`;
      const tasks = [...(activity.get(selected)?.values() || [])].filter((item) => item.project === li.dataset.pid);
      for (const kind of ['running', 'asking']) {
        const badge = li.querySelector(`.code-project-state.${kind}`);
        const total = tasks.filter((item) => kind === 'asking' ? item.asking : item.running && !item.asking).length;
        badge.hidden = !total;
        badge.textContent = String(total);
        const label = kind === 'asking' ? `Требуют вашего ответа: ${total}` : `В работе: ${total}`;
        badge.title = label;
        badge.setAttribute('aria-label', label);
      }
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
    else openIt(mine, agent, kind === 'open');
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

  async function openIt(mine, agent, resume = false) {
    let finish;
    const own = { aid: agent.id, done: new Promise((ok) => { finish = ok; }) };
    opening = own;
    let ok = true;                // false — только «нужен PIN»: прочие отказы правая часть получит от моста сама
    let found = null;
    try {
      if (resume) {
        try {
          found = await list(agent.id, true);
        } catch (err) {
          if (err.code !== 'closed') throw err;
        }
      }
      if (!alive || mine !== gen) return;
      if (found === null) await api('POST', `/api/code/agents/${agent.id}/open`, { pin: null });
    } catch (err) {
      ok = err.code !== 'pin_needed' && err.code !== 'pin_locked';
      if (!alive || mine !== gen) return;
      if (err.code === 'pin_needed') showPin(mine, agent);
      else if (err.code === 'pin_locked') {
        mode = 'error';
        status.textContent = PIN_LOCKED;
      } else refused(err);
      return;
    } finally {
      if (opening === own) opening = null;
      finish(ok);
    }
    if (!alive || mine !== gen) return;
    opened(agent.id);
    projects(mine, agent, found);
  }

  async function projects(mine, agent, found = null) {
    try {
      if (found === null) found = await list(agent.id, true);
    } catch (err) {
      if (err.code === 'closed' && alive && mine === gen) {
        await openIt(mine, agent);
        return;
      }
      if (alive && mine === gen) refused(err);
      return;
    }
    if (!alive || mine !== gen) return;
    mode = 'projects';
    const rows = found.map((p) => {
      const link = el('a', { class: 'code-project', href: `#/code/${agent.id}/${p.pid}` },
        el('span', { class: 'code-project-name' }, p.name),
        el('span', { class: 'code-project-state running', hidden: true }),
        el('span', { class: 'code-project-state asking', hidden: true }),
        el('span', { class: 'code-project-count' }));
      link.addEventListener('click', (event) => {
        if (shown && shown.aid === agent.id && shown.pid === p.pid && !event.ctrlKey && !event.metaKey
          && !event.shiftKey && !event.altKey) event.preventDefault();
        chose();
      });
      return el('li', { 'data-pid': p.pid }, link);
    });
    body.replaceChildren(el('ul', { id: 'code-projects', class: 'code-projects' }, ...rows));
    paintCounts();
    paintHere();
    refreshActivity();
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
    if (aid === selected && mode === 'projects') {
      mode = 'loading';
      projects(++gen, items.find((a) => a.id === aid));
      return;
    }
    if (aid === selected && (mode === 'pin' || mode === 'loading')) {
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

  /** Можно ли правой части идти к мосту за этим компьютером: панель его открывает — дождаться итога; он ждёт PIN — нет
   * (правая часть сразу ведёт к полю, без запроса и отказа closed); прочее — да, отказ скажет сам мост. */
  async function ready(aid) {
    await loaded;
    aid = Number(aid);
    if (!alive || selected !== aid) return true;
    if (opening && opening.aid === aid) return opening.done;
    return mode !== 'pin';
  }

  return {
    select,
    pin,
    ready,
    here(aid, pid) {
      shown = aid === null ? null : { aid: Number(aid), pid };
      paintHere();
    },
    refresh: paintCounts,
    agents: () => loaded,
    unmount() {
      alive = false;
      clearTimeout(activityTimer);
      document.removeEventListener('studio-code-activity', onActivity);
      document.removeEventListener('visibilitychange', onVisibility);
      host.removeEventListener('pointerdown', onAction);
      host.removeEventListener('keydown', onAction);
      gen += 1;
      unsubscribe();
      root.remove();
    },
  };
}
