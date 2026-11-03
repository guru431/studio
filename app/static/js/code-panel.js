/* Панель раздела «Claude Code» (спека 2026-10-29-studio-code-workspace-design, 6.1–6.3): компьютеры учётки строками,
 * под ними — проекты выбранного компьютера, поле PIN или «не на связи». Блок компьютеров не прокручивается, низ — да.
 *
 * panelView(host, { selected, opened, chose, list, openJournal }) → { select, pin, here, refresh, agents, unmount }:
 * - компьютеры — GET /api/agents (без моста): точка «на связи» (слова — для чтения с экрана), имя, версия моста,
 *   обновление, сведения и рабочий стол; версия Claude Code — подсказкой; agents() — последний список
 *   (обещание, пока показан раздел; не прочитался — null);
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
 *   chose() — нажат проект или его ссылка (телефон прячет панель); openJournal(path) — повторный переход
 *   к журналу по уже открытому адресу, через обычную защиту несохранённых правок «Файлов».
 * PIN живёт только в поле и переменной обработчика, после ответа поле очищается. Текст сервера — только textContent. */
import { api, me } from './api.js';
import { dateTime } from './time.js';
import { act, count, get, subscribe } from './code-workspace.js';
import { attemptsLeft } from './lock.js';
import { el, words } from './ui.js';
import { gitLevel } from './code-git.js';
import { codeActivity, refreshCodeActivity } from './code-activity.js';

const OFFLINE = 'Компьютер не на связи — включите его и запустите мост';
const PIN_LOCKED = 'PIN заблокирован после 5 ошибок — разблокируйте его в окне моста на самом компьютере';
const snapshots = new Map(); // Только метаданные, отдельно по учётке; переживают переход между разделами.
const SUMMARY_MS = 20000;
document.addEventListener('studio-code-clear', () => {
  for (const scope of snapshots.values()) { scope.summaries.clear(); scope.checked.clear(); }
});
const UPDATE_WORDS = { preparing: 'Скачивание и установка…', waiting: 'Ждём завершения заданий…',
  restarting: 'Перезапуск агента…', pin: 'Введите PIN для обновления агента',
  failed: 'Не удалось обновить агент. Повторите попытку.' };
const versionParts = (value) => /^\d+\.\d+\.\d+$/.test(value || '') ? value.split('.').map(Number) : null;
function newer(latest, current) {
  const a = versionParts(latest), b = versionParts(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) { if (a[i] !== b[i]) return a[i] > b[i]; }
  return false;
}

function remoteBadge(remote, name) {
  return {
    current: ['✓', 'green', `${name} синхронизирован с текущей веткой`],
    behind: [`↓${remote.behind}`, 'red', `${name} отстаёт на ${remote.behind} комм. от текущей ветки`],
    ahead: [`↑${remote.ahead}`, 'yellow', `${name} опережает текущую ветку на ${remote.ahead} комм.`],
    diverged: ['↕', 'red', `Истории разошлись: ${name} отстаёт на ${remote.behind} и опережает на ${remote.ahead} комм.`],
    missing: ['∅', 'red', `Текущая ветка ещё не опубликована на ${name}`],
  }[remote.state] || ['?', 'unknown', `Не удалось проверить ${name} или сравнить историю коммитов`];
}

export function panelView(host, { selected: first = null, opened, chose, list, openJournal, initialAgents = null }) {
  const agentsBox = el('ul', { id: 'code-agents', class: 'code-agents' });
  const body = el('div', { class: 'code-panel-body' });
  const status = el('p', { id: 'code-panel-status', class: 'error', 'aria-live': 'polite' });
  const sort = el('select', { id: 'code-project-sort', class: 'input', 'aria-label': 'Сортировка проектов' },
    ...[['name', 'По имени'], ['modified', 'По дате изменения'], ['findings', 'По количеству проблем'],
      ['ideas', 'По количеству идей'], ['unpushed', 'Сначала незапушенные'],
      ['public', 'Сначала публичные'], ['private', 'Сначала приватные']]
      .map(([value, label]) => el('option', { value }, label)));
  const sortKey = `studio-code-project-sort:${me.id}`;
  try {
    const saved = localStorage.getItem(sortKey);
    if ([...sort.options].some((option) => option.value === saved)) sort.value = saved;
  } catch { /* Закрытое хранилище не мешает пользоваться панелью. */ }
  const sortText = el('span', { class: 'code-project-sort-text', 'aria-hidden': 'true' }, sort.selectedOptions[0].textContent);
  const sortBox = el('label', { class: 'input code-project-sort' }, sortText, sort);
  const refresh = el('button', { id: 'code-project-refresh', class: 'btn', type: 'button',
    title: 'Обновить сведения', 'aria-label': 'Обновить сведения' }, el('span', { 'aria-hidden': 'true' }, '↻'));
  const updated = el('p', { id: 'code-project-updated', class: 'muted', 'aria-live': 'polite' }, 'Ещё не обновлено');
  const footer = el('div', { class: 'code-panel-footer', hidden: true }, sortBox, refresh, updated);
  const root = el('aside', { id: 'code-panel', class: 'code-panel', 'aria-label': 'Компьютеры и проекты' },
    el('div', { class: 'code-panel-agents' }, agentsBox), body, status, footer);
  host.append(root);

  let infoIntent = null; // Окна открываются только отдельными кнопками сведений и рабочего стола.
  let infoKind = 'info';
  const viewers = new Map();
  async function showViewer(agent, kind) {
    try {
      if (!viewers.has(kind)) {
        const module = await (kind === 'desktop' ? import('./code-desktop.js') : import('./code-computer.js'));
        if (!alive || infoIntent !== agent.id || infoKind !== kind) return;
        const create = kind === 'desktop' ? module.desktopView : module.computerView;
        viewers.set(kind, create(root, { locked(aid) { infoKind = kind; infoIntent = aid; pin(aid); } }));
      }
      if (!alive || infoIntent !== agent.id || infoKind !== kind) return;
      infoIntent = null;
      viewers.get(kind).show(agent);
    } catch (err) { if (alive) status.textContent = words(err); }
  }
  function showComputer() {
    if (infoIntent !== selected || mode !== 'projects') return;
    showViewer(items.find((a) => a.id === selected), infoKind);
  }
  async function computerClicked(aid, kind = 'info') {
    infoKind = kind;
    infoIntent = aid;
    await select(aid);
    if (!alive || infoIntent !== aid) return;
    const agent = items.find((a) => a.id === aid);
    if (agent && !agent.online) showViewer(agent, kind);
    else showComputer();
  }

  let alive = true;
  let gen = 0;             // номер показа компьютера: ответ прежнего показ не трогает
  let items = [];          // последний GET /api/agents
  const updates = new Map(); // Только память панели: версия и стадия запрошенного обновления.
  let updateTimer = null;
  let updatePolling = false;
  const updating = (item) => item && ['preparing', 'waiting', 'restarting'].includes(item.state);
  function scheduleUpdates() {
    clearTimeout(updateTimer);
    if (!alive || document.visibilityState === 'hidden') return;
    updateTimer = setTimeout(pollUpdates, [...updates.values()].some(updating) ? 2000 : 30000);
  }
  async function pollUpdates() {
    if (updatePolling || !alive) return;
    updatePolling = true;
    try {
      const got = await readAgents();
      if (!got || !alive) return;
      for (const [aid, update] of updates) {
        if (!updating(update)) continue;
        const agent = got.find((a) => a.id === aid);
        if (agent?.online && agent.agent_version && !newer(update.version, agent.agent_version)) {
          update.state = 'complete';
          continue;
        }
        if (!agent?.online) {
          update.offlineSince ||= Date.now();
          update.state = Date.now() - update.offlineSince > 120000 ? 'failed' : 'restarting';
          if (update.state === 'failed') update.message = 'Агент не подключился после обновления — проверьте компьютер';
          continue;
        }
        delete update.offlineSince;
        try {
          const data = await api('GET', `/api/code/agents/${aid}/update`);
          if (data.state === 'idle') update.state = 'failed';
          else Object.assign(update, data);
        } catch (err) {
          if (['closed', 'offline'].includes(err.code)) update.state = 'restarting';
          else { update.state = 'failed'; update.message = words(err); }
        }
      }
      if (alive) paintAgents();
    } finally { updatePolling = false; scheduleUpdates(); }
  }
  async function updateAgent(agent) {
    if (updating(updates.get(agent.id))) return;
    const update = { state: 'preparing', version: agent.latest_version };
    updates.set(agent.id, update);
    paintAgents();
    try {
      // PIN остаётся обычным входом на компьютер; обновление его не обходит.
      await api('POST', `/api/code/agents/${agent.id}/open`, { pin: null });
      if (!alive) return;
      Object.assign(update, await api('POST', `/api/code/agents/${agent.id}/update`));
      if (update.state === 'idle') update.state = 'complete';
    } catch (err) {
      update.state = 'failed';
      update.message = words(err);
      if (err.code === 'pin_needed' || err.code === 'closed') {
        update.state = 'pin';
        update.message = '';
        pin(agent.id);
      }
    }
    if (alive) { paintAgents(); scheduleUpdates(); }
  }
  function resumeUpdate(aid) {
    if (updates.get(aid)?.state === 'pin') updateAgent(items.find((a) => a.id === aid));
  }
  let selected = null;     // id показанного компьютера
  const activity = new Map();
  function refreshActivity() {
    const aid = selected;
    if (mode !== 'projects' || !items.find((a) => a.id === aid)?.project_activity) return;
    activity.set(aid, new Map(codeActivity(aid).map((item) => [item.chat, item])));
    paintCounts();
    refreshCodeActivity();
  }
  function onSnapshot({ detail }) {
    activity.set(detail.aid, new Map(detail.items.map((item) => [item.chat, item])));
    paintCounts();
  }
  function onActivity(event) {
    const item = event.detail;
    if (!activity.has(item.aid)) activity.set(item.aid, new Map());
    if (item.running) activity.get(item.aid).set(item.chat, { ...item, project: item.pid });
    else activity.get(item.aid).delete(item.chat);
    paintCounts();
  }
  function onVisibility() {
    if (document.visibilityState === 'visible') pollUpdates();
    else clearTimeout(updateTimer);
  }
  document.addEventListener('studio-code-snapshot', onSnapshot);
  document.addEventListener('studio-code-activity', onActivity);
  document.addEventListener('visibilitychange', onVisibility);
  let mode = 'idle';       // idle | loading | projects | pin | offline | error
  let shown = null;        // { aid, pid } — проект справа
  let pending = 0;         // select/pin ждут списка: выбор при входе их не перебивает
  let opening = null;      // идущий POST …/open: { aid, done } — done: открылся ли (ready ждёт его)
  let projectItems = [];
  let refreshing = null;
  if (!snapshots.has(me.id)) snapshots.set(me.id, { summaries: new Map(), checked: new Map() });
  const { summaries, checked } = snapshots.get(me.id);
  while (snapshots.size > 4) snapshots.delete(snapshots.keys().next().value);
  const requests = new Set();
  let summaryFrame = null;
  const observer = new IntersectionObserver(() => visible(), { root: body });

  function visible() {
    if (summaryFrame || !alive || mode !== 'projects' || refresh.disabled) return;
    summaryFrame = requestAnimationFrame(() => {
      summaryFrame = null;
      if (refresh.disabled || !alive || mode !== 'projects') return;
      const agent = items.find((a) => a.id === selected);
      if (agent) refreshSummaries(gen, agent);
    });
  }
  body.addEventListener('scroll', visible, { passive: true });

  function dateText(stamp, compact = false) {
    if (!Number.isFinite(stamp)) return compact ? '—' : 'нет данных';
    return dateTime(stamp, compact
      ? { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' } : undefined);
  }

  function paintUpdated() {
    const supported = items.find((a) => a.id === selected)?.project_summary;
    updated.textContent = !supported ? 'Для сведений обновите мост на компьютере'
      : checked.has(selected) ? `Обновлено: ${dateText(checked.get(selected))}` : 'Ещё не обновлено';
  }

  function refreshBusy(busy) {
    refresh.disabled = busy;
    refresh.setAttribute('aria-busy', String(busy));
    footer.setAttribute('aria-busy', String(busy));
    const label = busy ? 'Обновляю сведения…' : 'Обновить сведения';
    refresh.title = label;
    refresh.setAttribute('aria-label', label);
    if (busy) updated.textContent = label;
    else paintUpdated();
  }

  function paintSummary(li, data) {
    const summary = li.querySelector('.code-project-summary');
    const metrics = li.querySelector('.code-project-metrics');
    const href = li.querySelector('.code-project-name').getAttribute('href');
    const value = (key) => Number.isInteger(data?.[key]) ? String(data[key]) : '—';
    metrics.replaceChildren('(',
      el('a', { class: 'code-project-findings', href: `${href}?tab=files&journal=findings`, title: `Открыть FINDINGS.md · Проблемы: ${value('findings')}`,
        'aria-label': `Проблемы: ${value('findings')}` }, `П: ${value('findings')}`), ' · ',
      el('a', { class: 'code-project-ideas', href: `${href}?tab=files&journal=ideas`, title: `Открыть IDEAS.md · Идеи: ${value('ideas')}`,
        'aria-label': `Идеи: ${value('ideas')}` }, `И: ${value('ideas')}`), ')');
    const git = data?.git === 'not_git' ? 'Без Git'
      : `Git: ${value('changed')} изм. ↑${value('unpushed')}`;
    const gh = data?.github;
    const github = gh ? remoteBadge(gh, 'GitHub') : null;
    const githubAt = data && 'github_checked_at' in data ? data.github_checked_at : data?.checked_at;
    const githubTitle = github ? data.github_pending ? 'Проверяю GitHub…'
      : `${github[2]}. Проверено: ${dateText(githubAt)}${data.stale ? '. Не удалось обновить' : ''}` : '';
    const remotes = (data?.remotes || []).filter((remote) => remote.name !== 'origin' && remote.name !== 'upstream'
      && !(remote.name === 'github' && gh))
      .map((remote) => {
        const [mark, level, description] = remoteBadge(remote, remote.name);
        const title = `${description}. По локальным данным Git, без fetch; состояние сервера может отличаться.`
          + `${data.stale ? ' Не удалось обновить.' : ''}`;
        return el('a', { class: 'code-project-remote', href: `${href}?tab=git`,
          'data-git-level': data.stale ? 'unknown' : level, title, 'aria-label': title }, `${remote.name} ${mark}`);
      });
    summary.replaceChildren(el('a', { class: 'code-project-git', href: `${href}?tab=git`,
      'data-git-level': data?.git === 'ok' && !data?.stale ? gitLevel(data.changed, data.unpushed) : 'unknown',
      title: `Изменено файлов: ${value('changed')}. Не запушено коммитов: ${value('unpushed')}. По локальным данным Git, без fetch.` }, git),
    ...(github ? [el('a', { class: 'code-project-github', href: `${href}?tab=git`,
      'data-git-level': data.stale ? 'unknown' : github[1], title: githubTitle, 'aria-label': githubTitle }, `GitHub ${data.github_pending ? '…' : github[0]}`)] : []),
    ...remotes,
    el('span', { 'aria-hidden': 'true' }, ' · '),
    el('span', { class: 'code-project-modified', title: `Изменён: ${dateText(data?.modified)}` }, dateText(data?.modified, true)),
    ...(data?.stale ? [el('span', { class: 'error', title: 'Не удалось обновить', 'aria-label': 'Не удалось обновить' }, ' ⚠')] : []));
    for (const link of metrics.querySelectorAll('a')) link.addEventListener('click', (event) => {
      if (link.hash === location.hash && openJournal && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        openJournal(link.classList.contains('code-project-findings') ? 'FINDINGS.md' : 'IDEAS.md');
      }
      chose();
    });
    for (const link of summary.querySelectorAll('a')) link.addEventListener('click', chose);
    li.classList.toggle('has-unpushed', data?.unpushed > 0);
    if (data?.checked_at) summary.title = `Проверено: ${dateText(data.checked_at)}`;
  }

  function sortProjects() {
    const listBox = body.querySelector('#code-projects');
    if (!listBox) return;
    const details = summaries.get(selected);
    const names = new Map(projectItems.map((p) => [p.pid, p.name]));
    const byVisibility = sort.value === 'public' || sort.value === 'private';
    const visibilityRank = (row) => {
      const data = details?.get(row.dataset.pid);
      if (data?.git !== 'ok' || !('github' in data) || data.github?.state === 'unknown'
        || (data.github === null && data.github_pending)) return 2;
      return (data.github !== null) === (sort.value === 'public') ? 0 : 1;
    };
    const rows = [...listBox.children];
    rows.sort((a, b) => {
      if (byVisibility) {
        const diff = visibilityRank(a) - visibilityRank(b);
        if (diff) return diff;
      } else if (sort.value !== 'name') {
        const av = details?.get(a.dataset.pid)?.[sort.value];
        const bv = details?.get(b.dataset.pid)?.[sort.value];
        const diff = (Number.isFinite(bv) ? bv : -1) - (Number.isFinite(av) ? av : -1);
        if (diff) return diff;
      }
      return names.get(a.dataset.pid).localeCompare(names.get(b.dataset.pid), 'ru', { numeric: true });
    });
    listBox.append(...rows);
  }

  async function refreshSummaries(mine, agent, force = false) {
    if (refreshing) return;
    paintUpdated();
    if (!agent.project_summary || mode !== 'projects' || selected !== agent.id) return;
    const results = summaries.get(agent.id) || new Map();
    summaries.set(agent.id, results);
    const bounds = body.getBoundingClientRect();
    const panelBounds = root.getBoundingClientRect();
    const onScreen = panelBounds.right > 0 && panelBounds.left < innerWidth;
    const queue = projectItems.filter((project) => {
      if (!force && !onScreen) return false;
      const cached = results.get(project.pid);
      if (!force && cached && performance.now() - cached.readAt < SUMMARY_MS) return false;
      if (force || sort.value !== 'name') return true;
      const row = body.querySelector(`li[data-pid="${project.pid}"]`)?.getBoundingClientRect();
      return row && row.bottom >= bounds.top && row.top <= bounds.bottom;
    }).sort((a, b) => Number(b.pid === shown?.pid) - Number(a.pid === shown?.pid));
    if (!queue.length) return;
    const own = new AbortController();
    requests.add(own);
    refreshing = own;
    refreshBusy(true);
    const remoteJobs = [];
    let failures = 0;
    let blocked = false;
    function publish(project, data) {
      if (!alive || mine !== gen || own.signal.aborted) return;
      results.set(project.pid, { ...data, readAt: performance.now() });
      const row = body.querySelector(`li[data-pid="${project.pid}"]`);
      if (row) paintSummary(row, data);
      if (sort.value === 'public' || sort.value === 'private') sortProjects();
    }
    async function worker() {
      while (alive && mine === gen && queue.length && !blocked) {
        const project = queue.shift();
        try {
          const url = `/api/code/agents/${agent.id}/projects/${project.pid}/summary`;
          const data = await api('GET', url + (force ? '?refresh=true' : ''), undefined, { signal: own.signal });
          publish(project, data);
          if (data.github_pending) remoteJobs.push(api('GET', `${url}?remote=true`, undefined, { signal: own.signal })
            .then((remote) => publish(project, remote)).catch(() => {}));
        } catch (err) {
          if (own.signal.aborted || !alive || mine !== gen) return;
          failures += 1;
          publish(project, { ...results.get(project.pid), stale: true });
          if (['closed', 'offline'].includes(err.code)) {
            blocked = true;
            if (alive && mine === gen) {
              if (err.code === 'closed') pin(agent.id);
              else { footer.hidden = true; offline(); }
            }
          }
        }
      }
    }
    try {
      await Promise.all(Array.from({ length: Math.min(4, queue.length) }, () => worker()));
      if (!alive || mine !== gen) return;
      if (!failures) checked.set(agent.id, Date.now() / 1000);
      else status.textContent = `Не удалось обновить сведения: ${failures}. Повторите обновление.`;
      for (const li of body.querySelectorAll('#code-projects li[data-pid]')) paintSummary(li, results.get(li.dataset.pid));
      sortProjects();
      paintUpdated();
    } finally {
      Promise.allSettled(remoteJobs).then(() => requests.delete(own));
      if (refreshing === own) {
        refreshing = null;
        if (alive && mine === gen) refreshBusy(false);
        // Прокрутка во время предыдущей пачки могла открыть ещё строки.
        if (alive && mine === gen) visible();
      }
    }
  }

  sort.addEventListener('change', () => {
    sortText.textContent = sort.selectedOptions[0].textContent;
    sortProjects();
    visible();
    try { localStorage.setItem(sortKey, sort.value); } catch { /* Выбор действует в открытой панели. */ }
  });
  refresh.addEventListener('click', async () => {
    if (refresh.disabled || mode !== 'projects') return;
    const mine = ++gen;
    const agent = items.find((a) => a.id === selected);
    refreshBusy(true);
    status.textContent = '';
    try { await projects(mine, agent, null, true, true); }
    finally { if (alive && mine === gen && !refreshing) refreshBusy(false); }
  });

  /** Список компьютеров; не прочитался — null (прежний список остаётся, «компьютеров нет» не сказать). */
  async function readAgents() {
    try {
      const initial = initialAgents;
      initialAgents = null;
      items = (await (initial || api('GET', '/api/agents'))).items;
      if ([...summaries.keys()].some((aid) => !items.some((agent) => agent.id === aid))) {
        document.dispatchEvent(new Event('studio-code-clear'));
      }
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
    scheduleUpdates();
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
      agent.has_pin ? el('span', { class: 'sr' }, 'защищён PIN') : null,
      el('span', { class: 'code-agent-version', title: agent.agent_version
        ? `Studio Agent ${agent.agent_version}` : 'Этот агент ещё не сообщает свою версию' }, agent.agent_version || '—'));
      row.addEventListener('click', () => { infoIntent = null; select(agent.id); });
      const info = el('button', { type: 'button', class: 'btn code-agent-info',
        title: 'Сведения о компьютере', 'aria-label': `Сведения о компьютере · ${agent.name}`, 'aria-haspopup': 'dialog' },
        el('span', { 'aria-hidden': 'true' }, 'ⓘ'));
      info.addEventListener('click', () => computerClicked(agent.id));
      const update = updates.get(agent.id);
      const available = newer(agent.latest_version, agent.agent_version);
      const busy = updating(update) || update?.state === 'pin';
      const automatic = agent.online && agent.agent_update && available;
      const button = automatic || busy
        ? el('button', { type: 'button', class: 'btn code-agent-update', title: `Обновить агент до ${agent.latest_version}`,
          'aria-label': `Обновить агент · ${agent.name}`, disabled: busy })
        : el('a', { class: 'btn code-agent-update', href: '/api/agent/download', download: 'studio-agent-setup.exe',
          title: `Скачать установщик Studio Agent${agent.latest_version ? ' ' + agent.latest_version : ''} для Windows`,
          'aria-label': `Скачать установщик агента · ${agent.name}` });
      button.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/></svg>';
      if (available) button.classList.add('has-update');
      if (automatic || busy) button.addEventListener('click', () => updateAgent(agent));
      const note = update && update.state !== 'complete'
        ? el('span', { class: `code-agent-update-status${update.state === 'failed' ? ' error' : ''}`,
        role: 'status' }, update.message || UPDATE_WORDS[update.state] || '') : null;
      const remote = el('button', { type: 'button', class: 'btn code-agent-desktop',
        title: 'Подключиться к рабочему столу', 'aria-label': `Подключиться к рабочему столу · ${agent.name}`,
        'aria-haspopup': 'dialog' });
      remote.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2m9 4v3m-4 0h8M2 9h11m-4-4 4 4-4 4"/></svg>';
      remote.addEventListener('click', () => computerClicked(agent.id, 'desktop'));
      return el('li', { 'data-id': agent.id, class: 'code-agent-row' }, row, button, info, remote, note);
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
    for (const link of body.querySelectorAll('#code-projects .code-project')) {
      const li = link.closest('li');
      for (const target of [link, link.querySelector('.code-project-name')]) {
        if (shown && shown.aid === selected && li.dataset.pid === shown.pid) target.setAttribute('aria-current', 'page');
        else target.removeAttribute('aria-current');
      }
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
    if (infoIntent !== aid) infoIntent = null;
    selected = aid;
    paintSelected();
    status.textContent = '';
    footer.hidden = true;
    refreshing = null;
    refreshBusy(false);
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
    summaries.delete(selected);
    checked.delete(selected);
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
    resumeUpdate(agent.id);
    projects(mine, agent, found);
  }

  async function projects(mine, agent, found = null, keep = false, force = false) {
    try {
      if (found === null) found = await list(agent.id, true, force);
    } catch (err) {
      if (err.code === 'closed' && alive && mine === gen) {
        await openIt(mine, agent);
        return;
      }
      if (alive && mine === gen) {
        if (keep && err.code !== 'offline') status.textContent = words(err);
        else { footer.hidden = true; refused(err); }
      }
      return;
    }
    if (!alive || mine !== gen) return;
    mode = 'projects';
    projectItems = found;
    const rows = found.map((p) => {
      const link = el('a', { class: 'code-project-name', title: p.name, href: `#/code/${agent.id}/${p.pid}` }, p.name);
      const row = el('div', { class: 'code-project' },
        el('span', { class: 'code-project-heading' },
          el('span', { class: 'code-project-title' },
            link,
            el('span', { class: 'code-project-metrics' })),
          el('span', { class: 'code-project-state running', hidden: true }),
          el('span', { class: 'code-project-state asking', hidden: true }),
          el('span', { class: 'code-project-count' })),
        el('span', { class: 'code-project-summary' }));
      link.addEventListener('click', (event) => {
        if (shown && shown.aid === agent.id && shown.pid === p.pid && !event.ctrlKey && !event.metaKey
          && !event.shiftKey && !event.altKey) event.preventDefault();
        chose();
      });
      row.addEventListener('click', (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.target.closest('a, button, input, select, textarea')
          || window.getSelection()?.toString()) return;
        link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window,
          ctrlKey: event.ctrlKey, metaKey: event.metaKey, shiftKey: event.shiftKey, altKey: event.altKey }));
      });
      const li = el('li', { 'data-pid': p.pid }, row);
      paintSummary(li, summaries.get(agent.id)?.get(p.pid));
      return li;
    });
    body.replaceChildren(el('ul', { id: 'code-projects', class: 'code-projects' }, ...rows));
    observer.disconnect();
    for (const row of rows) observer.observe(row);
    paintCounts();
    paintHere();
    footer.hidden = false;
    showComputer();
    sortProjects();
    refreshActivity();
    await refreshSummaries(mine, agent, force);
  }

  function showPin(mine, agent) {
    summaries.delete(agent.id);
    checked.delete(agent.id);
    mode = 'pin';
    footer.hidden = true;
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
        resumeUpdate(agent.id);
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
    visible,
    async updateSummary(aid, pid) {
      const agent = items.find((a) => a.id === Number(aid));
      if (!agent?.project_summary || selected !== Number(aid)) return;
      const mine = gen;
      try {
        const data = await api('GET', `/api/code/agents/${aid}/projects/${pid}/summary?refresh=true`);
        if (!alive || mine !== gen) return;
        summaries.get(selected)?.set(pid, { ...data, readAt: performance.now() });
        const row = body.querySelector(`li[data-pid="${pid}"]`);
        if (row) paintSummary(row, data);
        sortProjects();
      } catch { /* Прежние сведения остаются до ручного обновления. */ }
    },
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
      observer.disconnect();
      cancelAnimationFrame(summaryFrame);
      for (const controller of requests) controller.abort();
      clearTimeout(updateTimer);
      document.removeEventListener('studio-code-snapshot', onSnapshot);
      document.removeEventListener('studio-code-activity', onActivity);
      document.removeEventListener('visibilitychange', onVisibility);
      gen += 1;
      unsubscribe();
      for (const viewer of viewers.values()) viewer.unmount();
      root.remove();
    },
  };
}
