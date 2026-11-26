import { tr } from './i18n.js';
/* Строка состояния «Чата» над полем (спека 2026-11-25-studio-skills-status-local-models, п. 12, 16, 36): общий с
 * «Агентами» каркас (status-bar.js). Слева — стадия хода с временем стадии и «последнее событие N с назад» после
 * SILENCE_MS тишины; справа — «Файлы диалога (N)», «Статистика», счётчик вызовов инструментов идущего ответа и
 * кольцо контекста. Сведения о карте (`wait`, очередь `card.py`) — здесь, под ответом — только «жду карту · м:сс». */
import { el, withCard } from './ui.js';
import { icon } from './icons.js';
import { popupPanel } from './menu.js';
import { statusActions } from './status-bar.js';

export const SILENCE_MS = 10000;
const QUIET = ['tool', 'think', 'write', 'load', 'wait'];   // у них тишина — признак, у карты и скана — нет
const UNTIMED = ['remote', 'pending'];

function clockText(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function toolWords(tool) {
  // часть ответа — её названием; MCP — подписью сервера; «Интернет» и встроенные (`todo_write`, `memory_save`…) — словом вида
  if (tool.kind === 'part') return tool.label ? tr("часть: {0}", tool.label) : tr("часть");
  const what = { search: tr("поиск"), page: tr("страница"), youtube: 'YouTube', ask: tr("вопрос"), todo: tr("план"),
    memory: tr("память"), knowledge: tr("знания") }[tool.kind] || tool.label;
  return what ? tr("инструмент {0}", what) : tr("инструмент");
}

/** Шаги ответа `m`: записанные (`m.tools`), а пока в них нет ничего, кроме автоматического блока «Знаний» (`auto`), —
 * живые `m.calls` хода; нет ни тех, ни других — []. */
export function answerSteps(m) {
  const tools = Array.isArray(m?.tools) ? m.tools : [];
  return tools.some((s) => !s?.auto) || !m?.calls?.length ? tools : m.calls;
}

/** Ответ идёт: `streaming`, кроме ответа прямого подключения, брошенного другой вкладкой (под ним «прервано»). */
function ongoing(job, m) {
  return m?.status === 'streaming' && (!!job || m.via !== 'browser' || !!m.live);
}

/** Стадия ответа: первая подошедшая из списка. `job` — ответ, который читает эта вкладка (или null), `message` —
 * идущий или последний ответ диалога. `track` — `{stageKey, stageAt}` ответа без `job` (ведёт `paint`). → `{ key,
 * words, title, state, since, eventAt }` или null — активного ответа нет. */
export function stageOf(job, message, { local = false, now = Date.now(), track = null } = {}) {
  const m = message || {};
  const streaming = ongoing(job, m);
  let key = null;
  let words = '';
  let state = 'thinking';
  let title = '';
  let since = null;
  if (job ? job.wait != null : streaming && Number.isFinite(m.wait)) {
    key = 'card';
    const card = (job ? job.card : m.card) || null;
    words = [tr("жду карту"), card?.ahead ? String(card.ahead) : '',
      card?.minutes ? tr("≈{0} мин", card.minutes) : ''].filter(Boolean).join(' · ');
    title = withCard(tr("жду карту"), card);
    since = job ? job.wait : now - m.wait * 1000;
  } else if (job ? job.scan : streaming && m.ocr) {
    key = 'scan';
    const scan = job ? job.scan : m.ocr;
    words = Number.isFinite(scan.page) && Number.isFinite(scan.pages) && scan.pages > 0
      ? tr("распознаю скан · стр. {0} из {1}", Math.min(scan.page + 1, scan.pages), scan.pages) : tr("распознаю скан");
  } else if (streaming && m.recovery) {
    key = 'recovery';
    words = m.recovery === 'summary' ? tr("сжимаю историю") : tr("продолжаю ответ");
  } else if (job?.tool) {
    key = 'tool';
    words = toolWords(job.tool);
    state = 'tool';
  } else if (job?.thinking) {
    key = 'think';
    words = tr("думает");
  } else if (job?.first) {
    key = 'write';
    words = tr("пишет");
    state = 'responding';
  } else if (job) {
    key = local ? 'load' : 'wait';
    words = local ? tr("загружаю модель") : tr("жду ответа");
  } else if (streaming) {
    key = 'remote';
    words = tr("ответ идёт");
    state = 'responding';
  } else if (m.pending) {
    key = 'pending';
    words = tr("ждёт вашего ответа");
    state = 'waiting';
  } else return null;
  const own = job || track;
  if (since === null) since = own?.stageKey === key ? own.stageAt : (own?.stageKey == null && job?.started) || now;
  return { key, words, title: title || words, state, since, eventAt: job?.eventAt ?? job?.started ?? now };
}

/** Строка стадии: слова, ` · м:сс` стадии и ` · последнее событие N с назад` после SILENCE_MS тишины. */
export function stageText(stage, now = Date.now()) {
  if (!stage) return tr("нет активного ответа");
  let text = stage.words;
  if (!UNTIMED.includes(stage.key)) text += ` · ${clockText(now - stage.since)}`;
  if (QUIET.includes(stage.key) && now - stage.eventAt >= SILENCE_MS) {
    text += ` · ${tr("последнее событие {0} с назад", Math.floor((now - stage.eventAt) / 1000))}`;
  }
  return text;
}

/** Строка над полем. `job()` — ответ, который читает вкладка; `last()` — последний ответ открытого диалога;
 * `local()` — модель диалога локальная; `stepNode(step)` — строка шага; `stats()`, `files()` — открыть окна. */
export function chatStatus(host, { ring, job, last, local, stepNode, stats, files, parts = null }) {
  const text = el('span', {}, tr("нет активного ответа"));
  // слова меняются каждую секунду: начало и конец ответа зачитывает #chat-status, а не эта строка
  const activity = el('div', { id: 'chat-activity', class: 'code-activity', role: 'status', 'aria-live': 'off',
    'data-stage': 'idle', 'data-state': 'idle' }, el('span', { class: 'code-activity-dot', 'aria-hidden': 'true' }), text);
  const filesCount = el('span', { class: 'code-changed-count', 'aria-hidden': 'true' }, '0');
  const filesButton = el('button', { type: 'button', id: 'chat-files-open', class: 'icon-btn', 'aria-haspopup': 'dialog',
    hidden: true }, icon('folder'), filesCount);
  const statsButton = el('button', { type: 'button', id: 'chat-stats-open', class: 'icon-btn', 'aria-haspopup': 'dialog',
    'aria-label': tr("Статистика"), title: tr("Статистика") }, icon('stats'));
  const toolsCount = el('span', { class: 'code-active-count', 'aria-hidden': 'true' }, '0');
  const toolsButton = el('button', { type: 'button', id: 'chat-tools', class: 'icon-btn', hidden: true }, icon('code'), toolsCount);
  const toolsList = el('ul', { class: 'web-list' });
  const toolsPanel = el('div', { id: 'chat-tools-menu', class: 'code-command-menu chat-tools-menu' },
    el('p', { class: 'small muted' }, tr("Шаги текущего ответа")), toolsList);
  host.append(toolsPanel);
  const toolsPopup = popupPanel(toolsButton, toolsPanel, { label: tr("Шаги текущего ответа"), side: 'above', align: 'end' });
  // `parts` — кнопка «Части» (chat-parts.js): в ряду перед «Файлами диалога», до разбора ряда statusActions
  const actions = el('div', { class: 'code-chat-actions' }, ...(parts ? [parts.node] : []), filesButton, statsButton);
  const fixed = el('div', { class: 'code-status-fixed' }, toolsButton, ring);
  const bar = el('div', { class: 'code-status-bar chat-status-bar' });
  const controls = statusActions(host, bar, activity, actions, fixed, { id: 'chat-status' });
  const remote = { id: null, stageKey: null, stageAt: 0 };   // стадия ответа, который читает не эта вкладка
  let steps = [];
  let stepsKey = '';

  function paintSteps() {
    const key = JSON.stringify(steps);
    if (key === stepsKey) return;
    stepsKey = key;
    toolsList.replaceChildren(...steps.map(stepNode));
  }

  filesButton.addEventListener('click', () => files());
  statsButton.addEventListener('click', () => stats());
  toolsPanel.addEventListener('beforetoggle', (event) => { if (event.newState === 'open') paintSteps(); });

  return {
    bar,
    paint() {
      const now = Date.now();
      const current = job();
      const m = current ? current.message : last();
      if (!current && remote.id !== (m?.id ?? null)) Object.assign(remote, { id: m?.id ?? null, stageKey: null, stageAt: now });
      const track = current || remote;
      // новый вызов инструмента (каждое событие `tool` — новый объект) — время стадии с нуля
      if (current?.tool && current.tool !== track.stageTool) {
        if (track.stageKey === 'tool') track.stageAt = now;
        track.stageTool = current.tool;
      }
      const stage = stageOf(current, m, { local: local(), now, track });
      if (stage && track.stageKey !== stage.key) Object.assign(track, { stageKey: stage.key, stageAt: stage.since });
      const words = stageText(stage, now);
      if (text.textContent !== words) text.textContent = words;
      activity.title = stage ? stage.title : words;
      activity.dataset.stage = stage?.key || 'idle';
      activity.dataset.state = stage?.state || 'idle';
      parts?.paint(m);
      const going = ongoing(current, m) ? m : null;
      steps = going ? answerSteps(going) : [];
      steps = steps.filter((s) => !s?.auto);      // шаг автоматического блока «Знаний» — не вызов инструмента
      toolsCount.textContent = String(steps.length);
      const label = tr("Вызовы инструментов: {0}", steps.length);
      if (toolsButton.getAttribute('aria-label') !== label) {
        toolsButton.setAttribute('aria-label', label);
        toolsButton.title = label;
      }
      toolsButton.hidden = !steps.length;
      if (!steps.length) toolsPopup.close();
      else if (toolsPopup.isOpen()) paintSteps();
    },
    /** Счёт «Файлов диалога» из ответа GET диалога. */
    dialog(chat) {
      const n = chat?.file_count ?? 0;
      filesCount.textContent = String(n);
      const label = tr("Файлы диалога: {0}", n);
      if (filesButton.getAttribute('aria-label') !== label) {
        filesButton.setAttribute('aria-label', label);
        filesButton.title = label;
      }
      filesButton.hidden = !n;
    },
    unmount() {
      toolsPopup.close();
      toolsPanel.remove();
      controls.unmount();
    },
  };
}
