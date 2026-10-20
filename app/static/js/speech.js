/* Раздел «Озвучка» (#/speech) — раскладка как у «Расшифровки» (спека 2026-10-20, раздел 6.4).
 *
 * Слева — озвучки по дням, справа — выбранная: текст, плеер, «Скачать» (mp3), «Удалить»; идущая —
 * состояние словами и «Отменить». Внизу — поле текста (до TEXT_MAX знаков, счётчик) с микрофоном,
 * «модель» (отмеченные модели раздела) и «голос»: у шлюза srvllm — свои голоса по образцу (их
 * заводят в настройках раздела «Озвучка»), у внешнего сервиса — имя голоса с подсказками.
 * Озвучка — POST /api/speeches, задание на сервере; идущие опрашиваются раз в POLL_MS.
 * Текст сервера — только textContent. */
import { api } from './api.js';
import { attachMic } from './mic.js';
import { modelLabel } from './settings.js';
import { el, openDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const TEXT_MAX = 2000;
const RUNNING = new Set(['working', 'cancelling']);
const SERVICE_VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'];
const ICONS = { list: '<path d="M4 6h16M4 12h16M4 18h16"/>' };

let screen = null;

function icon(name) {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константа выше
  return box.firstChild;
}

function clock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function day(ts) {
  return new Date(ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

function build(view) {
  let items = [];
  let current = null;
  let links = [];
  let voices = [];
  let alive = true;
  let poll = null;

  const list = el('nav', { class: 'tr-list sp-list', 'aria-label': 'Озвучки' });
  const aside = el('aside', { class: 'split-aside', id: 'sp-aside' }, list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'icon-btn split-toggle', 'aria-label': 'Озвучки',
    'aria-controls': 'sp-aside', 'aria-expanded': 'false' }, icon('list'));
  const head = el('header', { class: 'head' }, toggle, el('h1', {}, 'Озвучка'));
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view sp-view' });

  const text = el('textarea', { id: 'speech-text', rows: '3', maxlength: String(TEXT_MAX), 'aria-label': 'Текст для озвучки',
    placeholder: 'Текст, который нужно озвучить' });
  const count = el('span', { id: 'speech-count', class: 'muted small' }, `0 / ${TEXT_MAX}`);
  const modelPick = el('select', { class: 'chip', id: 'chip-model', 'aria-label': 'Модель' });
  const voiceBox = el('span', { class: 'sp-voice' });
  const go = el('button', { type: 'submit', id: 'speech-go', class: 'btn btn-accent' }, 'Озвучить');
  const hint = el('p', { class: 'muted small sp-voice-hint', hidden: true });
  const micNote = el('div', { class: 'mic-note' });
  const form = el('form', { class: 'composer', id: 'speech-composer' }, text, micNote, hint,
    el('div', { class: 'row' }, modelPick, voiceBox, count, el('span', { class: 'grow' }), attachMic(text, micNote), go));
  const main = el('section', { class: 'split-main' }, head, error, body, form);
  const root = el('div', { class: 'split speech' }, aside, backdrop, main);
  view.append(root);

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* модель и голос */
  function chosen() {
    const [connId, ...rest] = (modelPick.value || '').split(':');
    const link = links.find((l) => String(l.connection.id) === connId);
    return { connection_id: Number(connId), model: rest.join(':'), kind: link ? link.connection.kind : 'srvllm' };
  }

  function paintVoice() {
    const { kind } = chosen();
    hint.hidden = true;
    if (kind === 'srvllm') {
      const pick = el('select', { class: 'chip', id: 'chip-voice', 'aria-label': 'Голос' },
        ...voices.map((v) => el('option', { value: v.id }, v.name)));
      voiceBox.replaceChildren(pick);
      if (!voices.length) {
        hint.replaceChildren('Голосов пока нет — добавьте голос в настройках раздела «Озвучка»: ',
          el('a', { href: '#/settings/speech' }, 'открыть настройки'));
        hint.hidden = false;
      }
    } else {
      const options = el('datalist', { id: 'service-voices' }, ...SERVICE_VOICES.map((v) => el('option', { value: v })));
      voiceBox.replaceChildren(el('input', { class: 'chip', id: 'chip-voice', list: 'service-voices', value: 'alloy',
        maxlength: '64', 'aria-label': 'Голос сервиса', autocomplete: 'off' }), options);
    }
  }

  async function loadChoices() {
    try {
      const [{ sections }, voiceList] = await Promise.all([api('GET', '/api/sections'), api('GET', '/api/voices')]);
      const section = sections.find((s) => s.id === 'speech');
      links = section.links;
      voices = voiceList.items;
      const options = links.flatMap(({ connection: c, models }) =>
        models.map((m) => el('option', { value: `${c.id}:${m}` }, `${modelLabel(m)} · ${c.name}`)));
      modelPick.replaceChildren(...options);
      if (section.default) modelPick.value = `${section.default.connection_id}:${section.default.model}`;
      modelPick.hidden = options.length < 2;
      paintVoice();
    } catch (err) {
      error.textContent = words(err);
    }
  }

  /* список и выбранная */
  function paintList() {
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small' }, 'Здесь появятся ваши озвучки'));
      return;
    }
    const nodes = [];
    let last = '';
    for (const item of items) {
      const label = day(item.created);
      if (label !== last) {
        nodes.push(el('p', { class: 'tr-group' }, label));
        last = label;
      }
      const state = item.state === 'done' ? clock(item.duration)
        : RUNNING.has(item.state) ? item.detail : item.state === 'cancelled' ? 'отменено' : 'ошибка';
      const link = el('button', { type: 'button', class: 'tr-item sp-item', 'data-id': item.id, 'data-state': item.state,
        'aria-current': current && current.id === item.id ? 'true' : null },
      el('span', { class: 'tr-name' }, item.text.slice(0, 80)), el('span', { class: 'tr-state' }, state));
      link.addEventListener('click', () => {
        panel(false);
        select(item.id);
      });
      nodes.push(link);
    }
    list.replaceChildren(...nodes);
  }

  async function loadList() {
    try {
      items = (await api('GET', `/api/speeches?limit=${PAGE}`)).items;
    } catch (err) {
      error.textContent = words(err);
    }
    paintList();
    clearTimeout(poll);
    if (alive && items.some((i) => RUNNING.has(i.state))) poll = setTimeout(refresh, POLL_MS);
  }

  async function refresh() {
    await loadList();
    if (current) {
      const fresh = items.find((i) => i.id === current.id);
      if (fresh && (fresh.state !== current.state || fresh.detail !== current.detail)) await select(current.id);
    }
  }

  async function select(id) {
    try {
      current = await api('GET', `/api/speeches/${id}`);
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    if (!alive) return;
    paintList();
    paintView();
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'sp-delete-confirm' }, 'Удалить');
    const dialog = openDialog(root, { id: 'sp-delete-dialog', title: 'Удалить озвучку?', actions: [yes],
      body: [el('p', {}, 'Удалятся текст и звук.'), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/speeches/${item.id}`);
        dialog.close();
        current = null;
        paintView();
        await loadList();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function paintView() {
    const item = current;
    if (!item) {
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, 'Напишите или продиктуйте текст внизу и нажмите «Озвучить».'));
      return;
    }
    const nodes = [el('p', { class: 'sp-text' }, item.text)];
    const acts = el('div', { class: 'tr-acts' });
    if (RUNNING.has(item.state)) {
      const cancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-cancel' }, 'Отменить');
      cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        try {
          await api('POST', `/api/speeches/${item.id}/cancel`);
        } catch (err) {
          error.textContent = words(err);
        }
        refresh();
      });
      nodes.push(el('div', { class: 'tr-busy' }, el('span', { class: 'ring', 'aria-hidden': 'true' }), el('p', {}, item.detail), cancel));
    } else if (item.state === 'done') {
      nodes.push(el('audio', { class: 'sp-audio', controls: true, preload: 'metadata',
        src: `/api/media/${encodeURIComponent(item.media_id)}` }));
      const save = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-download' }, 'Скачать mp3');
      save.addEventListener('click', () => {
        el('a', { href: `/api/media/${encodeURIComponent(item.media_id)}?download=1`, download: '' }).click();
      });
      acts.append(save);
    } else {
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, item.detail));
    }
    if (!RUNNING.has(item.state)) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'sp-delete' }, 'Удалить');
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    body.replaceChildren(...nodes);
  }

  /* озвучить */
  text.addEventListener('input', () => { count.textContent = `${text.value.length} / ${TEXT_MAX}`; });
  modelPick.addEventListener('change', paintVoice);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = text.value.trim();
    const voiceField = voiceBox.querySelector('#chip-voice');
    const voice = voiceField ? voiceField.value.trim() : '';
    if (!value) {
      error.textContent = 'Напишите текст';
      return;
    }
    if (!voice) {
      error.textContent = 'Выберите голос';
      return;
    }
    error.textContent = '';
    go.disabled = true;
    try {
      const { connection_id: connectionId, model } = chosen();
      const created = await api('POST', '/api/speeches', { text: value, voice,
        ...(model ? { connection_id: connectionId, model } : {}) });
      text.value = '';
      count.textContent = `0 / ${TEXT_MAX}`;
      await loadList();
      await select(created.id);
    } catch (err) {
      error.textContent = words(err);
    } finally {
      go.disabled = false;
    }
  });
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  paintView();
  loadChoices();
  loadList();

  return {
    destroy() {
      alive = false;
      clearTimeout(poll);
    },
  };
}

export function mount(view) {
  screen = build(view);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
