import { message as serverMessage, tr } from './i18n.js';
/* Раздел «Озвучка» (#/speech) — раскладка как у «Разговоров» (спека 2026-10-20, раздел 6.4).
 *
 * Слева — озвучки по дням, справа — выбранная: текст, плеер, «Скачать» (mp3), «Удалить»; идущая —
 * состояние словами и «Отменить». Внизу — поле текста (до TEXT_MAX знаков; field.js: «очистить», микрофон)
 * и строка под ним: «модель» (отмеченные модели раздела), «голос», счётчик и «Озвучить» справа; у шлюза srvllm — свои голоса по образцу (их
 * заводят в настройках раздела «Озвучка»), у внешнего сервиса — имя голоса с подсказками.
 * Озвучка — POST /api/speeches, задание на сервере (очередь пользователя: «в очереди · N»); идущие
 * опрашиваются раз в POLL_MS, готовые части идущей можно слушать до склейки (#sp-live).
 * Список и выбор — как у «Расшифровки» (createPicker, feed.js): страницы по PAGE с «Показать ещё»,
 * выбор по номеру. Раздел без модели (ctx.hint): озвучки слушаются, скачиваются и удаляются, вместо
 * поля текста — подсказка. #/speech?id=<id> («Открыть» в «В работе», jobs.js) выбирает озвучку;
 * принятая озвучка зовёт jobs.kick(). Текст сервера — только textContent. */
import { api } from './api.js';
import { icon } from './icons.js';
import { NAMES } from './sections-meta.js';
import { busyBlock, createPicker, keep, pickerList } from './feed.js';
import { fieldBox } from './field.js';
import { sectionDraft } from './drafts.js';
import { kick } from './jobs.js';
import { modelLabel } from './settings.js';
import { el, openDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const TEXT_MAX = 2000;
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const SERVICE_VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'];

let screen = null;

function clock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function build(view, ctx) {
  let links = [];
  let voices = [];
  const records = createPicker({ url: '/api/speeches', size: PAGE, pollMs: POLL_MS,
    running: (i) => RUNNING.has(i.state), paint: paintList,
    show() {
      paintList();
      paintView();
    },
    fail(err) { error.textContent = words(err); } });
  const { select, refresh } = records;

  const list = el('nav', { class: 'tr-list sp-list', 'aria-label': tr("Озвучки") });
  const aside = el('aside', { class: 'split-aside', id: 'sp-aside' }, list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'icon-btn split-toggle', 'aria-label': tr("Озвучки"),
    'aria-controls': 'sp-aside', 'aria-expanded': 'false' }, icon('list'));
  const head = el('header', { class: 'head' }, toggle, el('h1', {}, NAMES.speech));
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view sp-view' });

  const text = el('textarea', { id: 'speech-text', rows: '1', maxlength: String(TEXT_MAX), 'aria-label': tr("Текст для озвучки"),
    placeholder: tr("Текст, который нужно озвучить") });
  const count = el('span', { id: 'speech-count', class: 'muted small' }, `0 / ${TEXT_MAX}`);
  const modelPick = el('select', { class: 'chip', id: 'chip-model', 'aria-label': tr("Модель") });
  const voiceBox = el('span', { class: 'sp-voice' });
  const go = el('button', { type: 'submit', id: 'speech-go', class: 'btn btn-accent composer-go' }, tr("Озвучить"));
  const hint = el('p', { class: 'muted small sp-voice-hint', hidden: true });
  const textBox = fieldBox(text, { fit: true });
  const form = el('form', { class: 'composer', id: 'speech-composer' }, textBox.box, textBox.error, hint,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, modelPick, voiceBox, count), go));
  const main = el('section', { class: 'split-main' }, head, error, body, ctx.hint ? ctx.hint('hint-bar') : form);
  const root = el('div', { class: 'split speech' }, aside, backdrop, main);
  view.append(root);
  const draft = sectionDraft('speech', text);
  count.textContent = `${text.value.length} / ${TEXT_MAX}`;

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
      const pick = el('select', { class: 'chip', id: 'chip-voice', 'aria-label': tr("Голос") },
        ...voices.map((v) => el('option', { value: v.id }, v.name)));
      voiceBox.replaceChildren(pick);
      if (!voices.length) {
        hint.replaceChildren(tr("Голосов пока нет — добавьте голос в настройках раздела «Озвучка»: "),
          el('a', { href: '#/settings/speech' }, tr("открыть настройки")));
        hint.hidden = false;
      }
    } else {
      const options = el('datalist', { id: 'service-voices' }, ...SERVICE_VOICES.map((v) => el('option', { value: v })));
      voiceBox.replaceChildren(el('input', { class: 'chip', id: 'chip-voice', list: 'service-voices', value: 'alloy',
        maxlength: '64', 'aria-label': tr("Голос сервиса"), autocomplete: 'off' }), options);
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
  const paintRows = pickerList(list, { cls: 'sp-item', moreId: 'sp-more', empty: tr("Здесь появятся ваши озвучки"),
    name: (item) => item.text.slice(0, 80),
    state: (item) => (item.state === 'done' ? clock(item.duration)
      : RUNNING.has(item.state) ? serverMessage(item.detail) : item.state === 'cancelled' ? tr("отменено") : tr("ошибка")),
    pick(id) {
      panel(false);
      select(id);
    } });

  function paintList() {
    paintRows(records);
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'sp-delete-confirm' }, tr("Удалить"));
    const dialog = openDialog(root, { id: 'sp-delete-dialog', title: tr("Удалить озвучку?"), actions: [yes],
      body: [el('p', {}, tr("Удалятся текст и звук.")), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/speeches/${item.id}`);
        dialog.close();
        records.forget(item.id);
        paintView();
        await records.load();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /* Слушать по мере готовности: готовые части идущей озвучки (GET …/parts/<n>) — по очереди в одном плеере,
   * он переживает перерисовку при опросе; следующая часть — по `ended`, а пока её нет — после опроса.
   * Итоговый mp3 от этого не меняется; кончилась — обычный плеер готовой озвучки. */
  let live = null;            // { id, audio, note, box, next, ready, waiting }
  let busy = null;            // блок «идёт» выбранной озвучки (feed.busyBlock): опрос меняет в нём только текст

  function playPart() {
    if (!live || live.next >= live.ready) {
      if (live) live.waiting = true;
      return;
    }
    live.waiting = false;
    live.audio.src = `/api/speeches/${encodeURIComponent(live.id)}/parts/${live.next}`;
    live.next += 1;
    live.audio.play().catch(() => {});      // без жеста автозапуск может быть запрещён: «Play» — у человека
  }

  function liveAudio(item) {
    if (!live || live.id !== item.id) {
      if (live) live.audio.pause();
      const audio = el('audio', { class: 'sp-audio sp-live', id: 'sp-live', controls: true, preload: 'auto' });
      const note = el('p', { class: 'muted small' });
      live = { id: item.id, audio, note, box: el('div', { class: 'sp-live-box' }, note, audio), next: 0, ready: 0,
        waiting: false };
      audio.addEventListener('ended', playPart);
      live.ready = item.parts;
      audio.src = `/api/speeches/${encodeURIComponent(item.id)}/parts/0`;
      live.next = 1;
    }
    live.ready = item.parts;
    if (live.waiting) playPart();
    live.note.textContent = tr("Готово частей: {0} — можно слушать, пока озвучка идёт", item.parts);
    return live.box;
  }

  function paintView() {
    const item = records.current;
    if (live && (!item || item.id !== live.id || !RUNNING.has(item.state))) {
      live.audio.pause();
      live = null;
    }
    if (!item) {
      busy = null;
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, ctx.hint ? tr("Выберите озвучку в списке.")
        : tr("Напишите или продиктуйте текст внизу и нажмите «Озвучить».")));
      return;
    }
    const nodes = [el('p', { class: 'sp-text' }, item.text)];
    const acts = el('div', { class: 'tr-acts' });
    busy = RUNNING.has(item.state) ? busyBlock(busy, item, { id: 'sp-cancel', url: '/api/speeches',
      fail: (err) => { error.textContent = words(err); }, after: refresh }) : null;
    if (busy) {
      nodes.push(busy.box);
      if (item.parts > 0) nodes.push(liveAudio(item));
    } else if (item.state === 'done') {
      nodes.push(el('audio', { class: 'sp-audio', controls: true, preload: 'metadata',
        src: `/api/media/${encodeURIComponent(item.media_id)}` }));
      const save = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-download' }, tr("Скачать mp3"));
      save.addEventListener('click', () => {
        el('a', { href: `/api/media/${encodeURIComponent(item.media_id)}?download=1`, download: '' }).click();
      });
      acts.append(save);
    } else {
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, serverMessage(item.detail)));
    }
    if (!RUNNING.has(item.state)) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'sp-delete' }, tr("Удалить"));
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    keep(body, nodes);
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
      error.textContent = tr("Напишите текст");
      return;
    }
    if (!voice) {
      error.textContent = tr("Выберите голос");
      return;
    }
    error.textContent = '';
    go.disabled = true;
    const raw = text.value;
    try {
      const { connection_id: connectionId, model } = chosen();
      const created = await api('POST', '/api/speeches', { text: value, voice,
        ...(model ? { connection_id: connectionId, model } : {}) });
      draft.sent(raw);                 // ушли из раздела до ответа — озвученный текст черновиком не вернётся
      kick();
      text.value = '';
      count.textContent = `0 / ${TEXT_MAX}`;
      await records.load();
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
  /** Список и ?id «В работе»: выбрать озвучку, ?id из адреса убрать — при сборке и при переходе внутри
   * раздела (update; список заново — запись могла начаться на другом устройстве). */
  function follow(wanted) {
    records.load();
    if (!wanted) return;
    history.replaceState(history.state, '', '#/speech');
    select(wanted);
  }
  follow(ctx.query.get('id'));

  return {
    follow,
    destroy() {
      draft.save();                    // озвученный текст поле уже очистило
      records.stop();
      if (live) live.audio.pause();
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

/** Переход внутри открытого раздела (#/speech?id=…): только ?id — набранный текст и голос целы. */
export function update(ctx) {
  const wanted = ctx.query.get('id');
  if (screen && wanted) screen.follow(wanted);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
