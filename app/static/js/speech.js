import { message as serverMessage, tr } from './i18n.js';
/* Раздел «Озвучка» (#/speech) — раскладка как у «Разговоров» (спека 2026-10-20, раздел 6.4).
 *
 * Слева — чаты по дням и времени начала, справа — их записи: текст, голос, плеер, «Изменить», «Скачать», «Удалить»; идущая —
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
import { timeOnly } from './time.js';

const POLL_MS = 3000;
const PAGE = 60;
const TEXT_MAX = 2000;
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const SERVICE_VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'];

let screen = null;

function build(view, ctx) {
  let links = [];
  let voices = [];
  let editing = null;
  let shownChat = null;
  let alive = true;
  const cards = new Map();
  const records = createPicker({ url: '/api/speeches/chats', size: PAGE, pollMs: POLL_MS, pollSelected: true,
    running: (i) => RUNNING.has(i.state), paint: paintList,
    changed: (a, b) => a.detail !== b.detail || JSON.stringify(a.revision) !== JSON.stringify(b.revision),
    show() {
      paintList();
      paintView();
    },
    fail(err) { error.textContent = words(err); } });
  const { refresh } = records;
  let following = 0;
  function select(id) {
    following += 1;
    return records.select(id);
  }

  const list = el('nav', { class: 'tr-list sp-list', 'aria-label': tr("Озвучки") });
  const aside = el('aside', { class: 'split-aside', id: 'sp-aside' }, list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'icon-btn split-toggle', 'aria-label': tr("Озвучки"),
    'aria-controls': 'sp-aside', 'aria-expanded': 'false' }, icon('list'));
  const fresh = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-new' }, icon('plus'), tr("Новый чат"));
  const head = el('header', { class: 'head' }, el('h1', {}, NAMES.speech));
  const toolbar = el('div', { class: 'split-toolbar sp-toolbar' }, toggle, fresh);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view sp-view' });

  const text = el('textarea', { id: 'speech-text', rows: '1', maxlength: String(TEXT_MAX), 'aria-label': tr("Текст для озвучки"),
    placeholder: tr("Текст, который нужно озвучить") });
  const count = el('span', { id: 'speech-count', class: 'muted small' }, `0 / ${TEXT_MAX}`);
  const modelPick = el('select', { class: 'chip', id: 'chip-model', 'aria-label': tr("Модель") });
  const voiceBox = el('span', { class: 'sp-voice' });
  const go = el('button', { type: 'submit', id: 'speech-go', class: 'btn btn-accent composer-go' }, tr("Озвучить"));
  const hint = el('p', { class: 'muted small sp-voice-hint', hidden: true });
  const editNote = el('p', { class: 'muted small', id: 'speech-edit-note', hidden: true }, tr("Изменение записи"));
  const cancelEdit = el('button', { type: 'button', class: 'btn btn-sm', id: 'speech-edit-cancel', hidden: true },
    tr("Отменить изменение"));
  const textBox = fieldBox(text, { fit: true });
  const form = el('form', { class: 'composer', id: 'speech-composer' }, editNote, textBox.box, textBox.error, hint,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, modelPick, voiceBox, count, cancelEdit), go));
  const main = el('section', { class: 'split-main' }, head, toolbar, error, body, ctx.hint ? ctx.hint('hint-bar') : form);
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
    state: (item) => timeOnly(item.created),
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
      const navigation = following;
      try {
        await api('DELETE', `/api/speeches/${item.id}`);
        dialog.close();
        if (editing?.id === item.id) endEdit();
        const cid = records.current?.id;
        const remaining = records.current?.items.some((i) => i.id !== item.id);
        const selected = cid === item.chat_id && following === navigation;
        if (selected) records.forget(cid);
        await records.load();
        if (selected && following === navigation && remaining) await select(cid);
        else if (selected && following === navigation) {
          paintList();
          paintView();
        }
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

  function setText(value) {
    text.value = value;
    text.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function endEdit(restore = true) {
    if (editing && restore) {
      modelPick.value = editing.model;
      paintVoice();
      voiceBox.querySelector('#chip-voice').value = editing.voice;
      setText(editing.draft);
    }
    editing = null;
    editNote.hidden = true;
    cancelEdit.hidden = true;
    go.textContent = tr("Озвучить");
  }

  async function startEdit(item) {
    await choicesReady;
    if (!alive || records.current?.id !== item.chat_id || go.disabled || !voiceBox.querySelector('#chip-voice')) return;
    if (!editing) editing = { draft: text.value, model: modelPick.value,
      voice: voiceBox.querySelector('#chip-voice').value };
    editing.id = item.id;
    const option = `${item.connection_id}:${item.model}`;
    if ([...modelPick.options].some((o) => o.value === option)) modelPick.value = option;
    paintVoice();
    const pick = voiceBox.querySelector('#chip-voice');
    pick.value = chosen().kind === 'srvllm' && !voices.some((v) => v.id === item.voice) ? '' : item.voice;
    setText(item.text);
    editNote.hidden = false;
    cancelEdit.hidden = false;
    go.textContent = tr("Озвучить заново");
    text.focus();
  }

  function paintItem(item, index) {
    let card = cards.get(item.id);
    if (!card) {
      card = { box: el('article', { class: 'sp-record', 'data-id': item.id }), busy: null, stamp: '' };
      cards.set(item.id, card);
    }
    const stamp = JSON.stringify([item.text, item.voice_name, item.state, item.media_id, item.model]);
    if (stamp === card.stamp) {
      if (card.busy) busyBlock(card.busy, item, {});
      if (item.parts > 0) {
        const box = liveAudio(item);
        if (!box.isConnected) card.box.insertBefore(box, card.box.lastChild);
      }
      return card.box;
    }
    card.stamp = stamp;
    card.box.querySelectorAll('audio').forEach((a) => a.pause());
    const nodes = [el('p', { class: 'sp-text' }, item.text),
      el('p', { class: 'muted small sp-voice-name' }, tr("Голос: {0}", item.voice_name || item.voice))];
    const acts = el('div', { class: 'tr-acts' });
    card.busy = RUNNING.has(item.state) ? busyBlock(card.busy, item, { id: index ? `sp-cancel-${item.id}` : 'sp-cancel',
      url: '/api/speeches', fail: (err) => { error.textContent = words(err); }, after: refresh }) : null;
    if (card.busy) {
      nodes.push(card.busy.box);
      if (item.parts > 0) nodes.push(liveAudio(item));
    } else if (item.state === 'done') {
      nodes.push(el('audio', { class: 'sp-audio', controls: true, preload: 'metadata',
        src: `/api/media/${encodeURIComponent(item.media_id)}` }));
      const save = el('button', { type: 'button', class: 'btn btn-sm', id: index ? `sp-download-${item.id}` : 'sp-download' },
        tr("Скачать mp3"));
      save.addEventListener('click', () => {
        el('a', { href: `/api/media/${encodeURIComponent(item.media_id)}?download=1`, download: '' }).click();
      });
      acts.append(save);
    } else {
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, serverMessage(item.detail)));
    }
    if (!RUNNING.has(item.state)) {
      if (!ctx.hint) {
        const edit = el('button', { type: 'button', class: 'btn btn-sm sp-edit' }, tr("Изменить"));
        edit.addEventListener('click', () => startEdit(item));
        acts.append(edit);
      }
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: index ? `sp-delete-${item.id}` : 'sp-delete' },
        tr("Удалить"));
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    keep(card.box, nodes);
    return card.box;
  }

  function paintView() {
    const chat = records.current;
    if (shownChat !== chat?.id) {
      endEdit();
      shownChat = chat?.id;
    }
    const items = chat?.items || [];
    if (live && !items.some((i) => i.id === live.id && RUNNING.has(i.state))) {
      live.audio.pause();
      live = null;
    }
    const ids = new Set(items.map((i) => i.id));
    for (const [id, card] of cards) if (!ids.has(id)) {
      card.box.querySelectorAll('audio').forEach((a) => a.pause());
      cards.delete(id);
    }
    if (!chat) {
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, ctx.hint ? tr("Выберите озвучку в списке.")
        : tr("Напишите или продиктуйте текст внизу и нажмите «Озвучить».")));
      return;
    }
    keep(body, items.map(paintItem));
  }

  /* озвучить */
  fresh.addEventListener('click', () => {
    following += 1;
    endEdit(false);
    records.deselect();
    panel(false);
    error.textContent = '';
    text.value = '';
    text.dispatchEvent(new Event('input', { bubbles: true }));
    draft.save();
    paintList();
    paintView();
    text.focus();
  });
  text.addEventListener('input', () => { count.textContent = `${text.value.length} / ${TEXT_MAX}`; });
  modelPick.addEventListener('change', paintVoice);
  cancelEdit.addEventListener('click', () => endEdit());
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
    fresh.disabled = true;
    cancelEdit.disabled = true;
    const raw = text.value;
    const edited = editing?.id;
    const cid = records.current?.id;
    const navigation = following;
    try {
      const { connection_id: connectionId, model } = chosen();
      const created = await api(edited ? 'PATCH' : 'POST', edited ? `/api/speeches/${edited}` : '/api/speeches', { text: value, voice,
        ...(cid ? { chat_id: cid } : {}),
        ...(model ? { connection_id: connectionId, model } : {}) });
      draft.sent(raw);                 // ушли из раздела до ответа — озвученный текст черновиком не вернётся
      kick();
      if (alive && following === navigation && records.current?.id === cid && text.value === raw && editing?.id === edited) {
        if (edited) endEdit();
        else setText('');
      }
      await records.load();
      if (alive && following === navigation && records.current?.id === cid) {
        await select(created.chat_id);
        cards.get(created.id)?.box.scrollIntoView({ block: 'nearest' });
      }
    } catch (err) {
      error.textContent = words(err);
    } finally {
      go.disabled = false;
      fresh.disabled = false;
      cancelEdit.disabled = false;
    }
  });
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  paintView();
  const choicesReady = loadChoices();
  /** Список и ?id «В работе»: выбрать озвучку, ?id из адреса убрать — при сборке и при переходе внутри
   * раздела (update; список заново — запись могла начаться на другом устройстве). */
  async function follow(wanted) {
    const mine = ++following;
    records.load();
    if (!wanted) return;
    history.replaceState(history.state, '', '#/speech');
    try {
      const item = await api('GET', `/api/speeches/${encodeURIComponent(wanted)}`);
      if (alive && mine === following) select(item.chat_id);
    } catch (err) {
      if (alive && mine === following) error.textContent = words(err);
    }
  }
  follow(ctx.query.get('id'));

  return {
    follow,
    destroy() {
      alive = false;
      following += 1;
      if (editing) setText(editing.draft);
      draft.save();
      records.stop();
      if (live) live.audio.pause();
      for (const card of cards.values()) card.box.querySelectorAll('audio').forEach((a) => a.pause());
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
