import { message as serverMessage, tr } from './i18n.js';
/* Раздел «Озвучка» (#/speech) — раскладка как у «Расшифровки» (спека 2026-10-20, раздел 6.4).
 *
 * Слева — чаты по дням и времени начала (идущий, ошибка, отмена — словами), справа — их записи: текст, голос, плеер,
 * «Изменить», «Скачать», «Удалить»; идущая — состояние словами и «Отменить»; «Удалить чат» — весь чат разом.
 * Внизу — поле текста (до LONG_MAX знаков: длиннее записи — частями одного чата, «n / 200 000 · будет N частей»;
 * правка записи — до предела записи PART_MAX; сверх — отказ словами; field.js: «очистить», микрофон)
 * и строка под ним: «модель» (отмеченные модели раздела), «голос», «язык» (Qwen3-TTS и F5-TTS шлюза: #chip-language),
 * счётчик и «Озвучить» справа; у шлюза srvllm — свои голоса по образцу (их заводят в настройках раздела «Озвучка»),
 * у внешнего сервиса — имя голоса с подсказками; у инструмента MCP с перечнем голосов (`enum` поля voice) — его
 * голоса группой «имя сервера» рядом с личными. У чата из нескольких записей — «Слушать подряд» (#sp-play-all,
 * speech-read.js chatPlayer), «Скачать одним mp3» (#sp-download-all, GET /api/speeches/chats/<id>/audio) и, пока
 * в нём что-то ждёт или идёт, «Отменить» весь чат (#sp-cancel-chat, POST /api/speeches/chats/<id>/cancel).
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
import { schemaLimits, sectionMcp } from './mcp-sections.js';
import { modelLabel } from './settings.js';
import { picker } from './picker.js';
import { chatPlayer } from './speech-read.js';
import { el, openDialog, plural, words } from './ui.js';
import { timeOnly } from './time.js';

const POLL_MS = 3000;
const PAGE = 60;
const LONG_MAX = 200000;                            // speeches.LONG_MAX
const PART_MAX = { srvllm: 2000, other: 4000 };     // speeches.TEXT_MAX, speeches.EXTERNAL_MAX
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const SERVICE_VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'];

let screen = null;

/** 200000 → «200 000». */
function grouped(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

/** Сколько записей даст текст: как speeches.split_text — абзацы, предложения, слова, соседние — вместе до limit. */
function partCount(text, limit) {
  let count = 0;
  let last = 0;
  const add = (size, sep) => {
    if (count && last + sep + size <= limit) last += sep + size;
    else {
      count += 1;
      last = size;
    }
  };
  for (const raw of text.split(/\n\s*\n/)) {
    const paragraph = raw.trim();
    if (paragraph.length <= limit) {
      if (paragraph) add(paragraph.length, 2);
      continue;
    }
    for (const rawSentence of paragraph.split(/(?<=[.!?…])\s+|\n+/)) {
      const sentence = rawSentence.trim();
      if (sentence.length <= limit) {
        if (sentence) add(sentence.length, 1);
        continue;
      }
      for (let word of sentence.split(/\s+/)) {
        while (word.length > limit) {
          count += 1;
          last = limit;
          word = word.slice(limit);
        }
        if (word) add(word.length, 1);
      }
    }
  }
  return count;
}

function build(view, ctx) {
  let links = [];
  let voices = [];
  let editing = null;
  let sending = false;
  let sampleId = '', serviceVoice;
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
  const dropChat = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'sp-delete-chat', hidden: true },
    tr("Удалить чат"));
  const playAll = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-play-all', hidden: true }, tr("Слушать подряд"));
  const downloadAll = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-download-all', hidden: true },
    tr("Скачать одним mp3"));
  const cancelChat = el('button', { type: 'button', class: 'btn btn-sm', id: 'sp-cancel-chat', hidden: true },
    tr("Отменить"));
  const chainBox = el('div', { class: 'read-box', id: 'sp-chain' });
  let chain = null;              // «Слушать подряд» показанного чата: { box, stop }
  const head = el('header', { class: 'head' }, el('h1', {}, NAMES.speech));
  const toolbar = el('div', { class: 'split-toolbar sp-toolbar' }, toggle, fresh, playAll, downloadAll, cancelChat, dropChat);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view sp-view' });

  // без maxlength: вставка длинного текста не режется молча — сверх TEXT_MAX счётчик красный, «Озвучить» закрыта словами
  const text = el('textarea', { id: 'speech-text', rows: '1', 'aria-label': tr("Текст для озвучки"),
    placeholder: tr("Текст, который нужно озвучить") });
  const count = el('span', { id: 'speech-count', class: 'muted small' }, `0 / ${grouped(LONG_MAX)}`);
  const over = el('p', { class: 'error small', id: 'speech-over', 'aria-live': 'polite', hidden: true });
  const modelPick = el('select', { class: 'chip', id: 'chip-model', 'aria-label': tr("Модель") });
  const mcpModelPick = el('select', { class: 'chip', id: 'chip-mcp-model', 'aria-label': tr("Модель") });
  // выборы — «как у Агентов» (picker.js): select остаётся под кнопкой и хранит значение; голос сервиса — поле в рамке чипа
  const modelView = picker(view, tr("Модель"), modelPick);
  const mcpModelView = picker(view, tr("Модель"), mcpModelPick);
  mcpModelView.wrap.hidden = true;
  let voiceView = null;          // выбор голоса-образца: пересоздаётся вместе с select #chip-voice
  // язык озвучки шлюза (Qwen3-TTS, F5-TTS); названия языков — на самих языках
  const langPick = el('select', { class: 'chip', id: 'chip-language', 'aria-label': tr("Язык озвучки") },
    el('option', { value: 'ru' }, 'Русский'), el('option', { value: 'en' }, 'English'));
  const langView = picker(view, tr("Язык озвучки"), langPick);
  const voiceBox = el('span', { class: 'sp-voice' });
  const go = el('button', { type: 'submit', id: 'speech-go', class: 'btn btn-accent composer-go' }, tr("Озвучить"));
  const hint = el('p', { class: 'muted small sp-voice-hint', hidden: true });
  const editNote = el('p', { class: 'muted small', id: 'speech-edit-note', hidden: true }, tr("Изменение записи"));
  const cancelEdit = el('button', { type: 'button', class: 'btn btn-sm', id: 'speech-edit-cancel', hidden: true },
    tr("Отменить изменение"));
  const mcp = sectionMcp('speech', { native: !ctx.hint, fields: ['text', 'voice', 'model', 'engine', 'reference_text'],
    onChange: paintMcp, onBusy: paintBusy });
  const textBox = fieldBox(text, { fit: true });
  const form = el('form', { class: 'composer', id: 'speech-composer', hidden: !!ctx.hint }, editNote, textBox.box, textBox.error, over, hint,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, mcp.control, modelView.wrap, mcpModelView.wrap, voiceBox, langView.wrap, mcp.fields, count, cancelEdit), go));
  const nativeHint = ctx.hint?.('hint-bar');
  const main = el('section', { class: 'split-main' }, head, toolbar, error, chainBox, body, mcp.output, nativeHint || form);
  const root = el('div', { class: 'split speech' }, aside, backdrop, main);
  view.append(root);
  const draft = sectionDraft('speech', text);
  paintCount();

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
    const active = !!mcp.tool;
    const previous = voiceBox.querySelector('#chip-voice');
    if (previous?.tagName === 'SELECT') sampleId = previous.value;
    if (previous?.tagName === 'INPUT') serviceVoice = previous.value;
    hint.hidden = true;
    const clone = active && mcp.canClone() && !!voices.length;
    // голоса сервера MCP — перечень (`enum`) поля voice инструмента: группой «имя сервера» рядом с личными
    const schema = active && mcp.has('voice') ? mcp.argument('voice') : null;
    const shared = Array.isArray(schema?.enum) ? schema.enum.filter((v) => typeof v === 'string' && v) : [];
    voiceBox.hidden = active && !mcp.has('voice') && !clone;
    if (active && shared.length) {
      const mine = clone ? voices.map((v) => el('option', { value: v.id }, v.name)) : [];
      const pick = el('select', { class: 'chip', id: 'chip-voice', 'aria-label': tr("Голос") },
        ...(mine.length ? [el('optgroup', { label: tr("Мои голоса") }, ...mine)] : []),
        el('optgroup', { label: mcp.server || 'MCP' }, ...shared.map((v) => el('option', { value: v, 'data-mcp': '1' }, v))));
      const known = [...pick.options].find((o) => o.value === sampleId);
      pick.value = known ? sampleId : shared.includes(schema.default) ? schema.default : shared[0];
      const sampleHint = () => {
        hint.textContent = tr("Выбранный образец голоса будет отправлен на MCP-сервер.");
        hint.hidden = !!pick.selectedOptions[0]?.dataset.mcp;
      };
      pick.addEventListener('change', sampleHint);
      voiceView?.unmount();
      voiceView = picker(view, tr("Голос"), pick);
      voiceBox.replaceChildren(voiceView.wrap);
      sampleHint();
    } else if (active ? clone && (mcpModelPick.options.length || kind === 'srvllm' || !mcp.has('voice')) : kind === 'srvllm') {
      const pick = el('select', { class: 'chip', id: 'chip-voice', 'aria-label': tr("Голос") },
        ...voices.map((v) => el('option', { value: v.id }, v.name)));
      if (voices.some(v => v.id === sampleId)) pick.value = sampleId;
      voiceView?.unmount();
      voiceView = picker(view, tr("Голос"), pick);
      voiceBox.replaceChildren(voiceView.wrap);
      if (active) {
        hint.textContent = tr("Выбранный образец голоса будет отправлен на MCP-сервер.");
        hint.hidden = false;
      } else if (!voices.length) {
        hint.replaceChildren(tr("Голосов пока нет — добавьте голос в настройках раздела «Озвучка»: "),
          el('a', { href: '#/settings/speech' }, tr("открыть настройки")));
        hint.hidden = false;
      }
    } else {
      const options = el('datalist', { id: 'service-voices' }, ...SERVICE_VOICES.map((v) => el('option', { value: v })));
      voiceView?.unmount();
      voiceView = null;
      voiceBox.replaceChildren(el('label', { class: 'ctl-chip sp-voice-field' }, el('input', { id: 'chip-voice', list: 'service-voices',
        value: serviceVoice || '', placeholder: tr("Голос сервиса"),
        maxlength: '64', 'aria-label': tr("Голос сервиса"), autocomplete: 'off' })), options);
    }
    langView.wrap.hidden = active || kind !== 'srvllm';
    syncPicks();
    paintCount();
  }

  /** Подписи выборов после программной смены значения, опций или видимости select. */
  function syncPicks() {
    modelView.sync();
    mcpModelView.sync();
    voiceView?.sync();
    langView.sync();
  }

  /** Предел поля: новая озвучка — LONG_MAX частями; правка записи и инструмент MCP — одна запись. */
  function partMax() {
    return PART_MAX[chosen().kind === 'srvllm' || mcp.tool ? 'srvllm' : 'other'];
  }

  function limit() {
    return editing || mcp.tool ? partMax() : LONG_MAX;
  }

  function paintMcp() {
    const active = !!mcp.tool;
    if (active && editing) endEdit();
    form.hidden = !!ctx.hint && !active;
    if (nativeHint) {
      nativeHint.hidden = active;
      if (active && !form.parentNode) nativeHint.after(form);
    }
    const schema = mcp.argument('model');
    const choices = schemaLimits(schema)?.enum?.filter(id => typeof id === 'string') || [];
    const previous = mcpModelPick.value;
    mcpModelPick.replaceChildren(...choices.map(id => el('option', { value: id }, modelLabel(id))));
    if (choices.includes(previous)) mcpModelPick.value = previous;
    else if (choices.includes(schema?.default)) mcpModelPick.value = schema.default;
    mcpModelView.wrap.hidden = choices.length < 2 || (mcp.canClone() && !voices.length);
    modelView.wrap.hidden = modelPick.options.length < 2 || active;
    paintVoice();
  }

  function paintBusy() {
    fresh.disabled = cancelEdit.disabled = sending || mcp.busy;
    go.disabled = fresh.disabled || text.value.length > limit();
  }

  function paintCount() {
    const max = limit();
    const long = text.value.length > max;
    const value = text.value.trim();
    const parts = !long && max === LONG_MAX && value.length > partMax() ? partCount(value, partMax()) : 1;
    count.textContent = `${grouped(text.value.length)} / ${grouped(max)}`
      + (parts > 1 ? ` · ${tr("будет {0} {1}", parts, plural(parts, [tr("часть"), tr("части"), tr("частей")]))}` : '');
    count.classList.toggle('muted', !long);
    count.classList.toggle('error', long);
    over.textContent = tr("Текст длиннее {0} знаков — сократите его или разделите на несколько записей чата.", grouped(max));
    over.hidden = !long;
    paintBusy();
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
      paintMcp();
    } catch (err) {
      error.textContent = words(err);
    }
  }

  /* список и выбранная */
  const paintRows = pickerList(list, { cls: 'sp-item', moreId: 'sp-more', empty: tr("Здесь появятся ваши озвучки"),
    name: (item) => item.text.slice(0, 80),
    // время начала и, пока чат не «готово», — его состояние словами (как у «Расшифровки»)
    state: (item) => [timeOnly(item.created), RUNNING.has(item.state) ? serverMessage(item.detail)
      : item.state === 'error' ? tr("ошибка") : item.state === 'cancelled' ? tr("отменено") : ''].filter(Boolean).join(' · '),
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
      body: [el('p', {}, tr("Текст и звук уйдут в корзину на 14 дней — оттуда их можно вернуть.")), fail] });
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

  /* Весь чат — одним подтверждением; идущие записи сервер не удаляет (409 словами в окне). */
  function confirmDeleteChat(chat) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'sp-delete-chat-confirm' }, tr("Удалить"));
    const dialog = openDialog(root, { id: 'sp-delete-chat-dialog', title: tr("Удалить чат озвучки?"), actions: [yes],
      body: [el('p', {}, tr("Все записи чата ({0}) уйдут в корзину на 14 дней — оттуда их можно вернуть.", chat.items.length)), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      const navigation = following;
      try {
        await api('DELETE', `/api/speeches/chats/${encodeURIComponent(chat.id)}`);
        dialog.close();
        if (editing && chat.items.some((i) => i.id === editing.id)) endEdit();
        const selected = records.current?.id === chat.id && following === navigation;
        if (selected) records.forget(chat.id);
        await records.load();
        if (selected && following === navigation) {
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
      langPick.value = editing.language;
      syncPicks();
      setText(editing.draft);
    }
    editing = null;
    editNote.hidden = true;
    cancelEdit.hidden = true;
    go.textContent = tr("Озвучить");
    paintCount();
  }

  async function startEdit(item) {
    await choicesReady;
    if (!alive || ctx.hint || records.current?.id !== item.chat_id || fresh.disabled || !voiceBox.querySelector('#chip-voice')) return;
    mcp.useNative();
    if (!editing) editing = { draft: text.value, model: modelPick.value,
      voice: voiceBox.querySelector('#chip-voice').value, language: langPick.value };
    editing.id = item.id;
    const option = `${item.connection_id}:${item.model}`;
    if ([...modelPick.options].some((o) => o.value === option)) modelPick.value = option;
    paintVoice();
    const pick = voiceBox.querySelector('#chip-voice');
    pick.value = chosen().kind === 'srvllm' && !voices.some((v) => v.id === item.voice) ? '' : item.voice;
    langPick.value = item.language || 'ru';
    syncPicks();
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
    } else {
      // отказ повторной озвучки оставляет прежний звук: слова ошибки и плеер вместе
      if (item.state !== 'done') nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, serverMessage(item.detail)));
      if (item.media_id) {
        nodes.push(el('audio', { class: 'sp-audio', controls: true, preload: 'metadata',
          src: `/api/media/${encodeURIComponent(item.media_id)}` }));
        const save = el('button', { type: 'button', class: 'btn btn-sm', id: index ? `sp-download-${item.id}` : 'sp-download' },
          tr("Скачать mp3"));
        save.addEventListener('click', () => {
          el('a', { href: `/api/media/${encodeURIComponent(item.media_id)}?download=1`, download: '' }).click();
        });
        acts.append(save);
      }
    }
    if (!RUNNING.has(item.state)) {
      if (item.state === 'error' || item.state === 'cancelled') {
        const repeat = el('button', { type: 'button', class: 'btn btn-sm sp-repeat' }, tr("Повторить"));
        repeat.addEventListener('click', async () => {
          repeat.disabled = true;
          error.textContent = '';
          try {
            await api('PATCH', `/api/speeches/${item.id}`, {
              text: item.text, voice: item.voice, connection_id: item.connection_id, model: item.model, language: item.language,
            });
            kick();
            await refresh();
          } catch (err) {
            error.textContent = words(err);
          } finally {
            repeat.disabled = false;
          }
        });
        acts.append(repeat);
      }
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
      stopChain();
    }
    dropChat.hidden = !chat;
    const items = chat?.items || [];
    playAll.hidden = items.length < 2;
    downloadAll.hidden = items.length < 2 || !items.some((i) => i.media_id && !RUNNING.has(i.state));
    cancelChat.hidden = items.length < 2 || !items.some((i) => RUNNING.has(i.state));
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

  /* озвучить; «Новый чат» набранное не стирает (правка записи — возвращает свой черновик) и черновик не трогает:
   * поле остаётся на экране и под защитой от автоперезагрузки (draft.save — только при уходе из раздела) */
  fresh.addEventListener('click', () => {
    following += 1;
    endEdit();
    records.deselect();
    panel(false);
    error.textContent = '';
    paintList();
    paintView();
    text.focus();
  });
  dropChat.addEventListener('click', () => { if (records.current) confirmDeleteChat(records.current); });
  function stopChain() {
    chain?.stop();
    chain = null;
    chainBox.replaceChildren();
  }
  // записи чата по порядку в одном плеере; ещё идущие играют, когда будут готовы
  playAll.addEventListener('click', () => {
    if (!records.current) return;
    stopChain();
    chain = chatPlayer(records.current.id, { download: false, cancel: false });
    chainBox.replaceChildren(chain.box);
  });
  // весь чат: ждущие части снимаются, идущая останавливается (длинный текст — до 100 записей)
  cancelChat.addEventListener('click', async () => {
    if (!records.current) return;
    cancelChat.disabled = true;
    try {
      await api('POST', `/api/speeches/chats/${encodeURIComponent(records.current.id)}/cancel`);
      kick();
      await refresh();
    } catch (err) {
      error.textContent = words(err);
    } finally {
      cancelChat.disabled = false;
    }
  });
  downloadAll.addEventListener('click', () => {
    if (records.current) el('a', { href: `/api/speeches/chats/${encodeURIComponent(records.current.id)}/audio`, download: '' }).click();
  });
  text.addEventListener('input', paintCount);
  modelPick.addEventListener('change', paintVoice);
  cancelEdit.addEventListener('click', () => endEdit());
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (go.disabled) return;
    const value = text.value.trim();
    const voiceField = voiceBox.querySelector('#chip-voice');
    const voice = voiceField ? voiceField.value.trim() : '';
    if (!value) {
      error.textContent = tr("Напишите текст");
      return;
    }
    if (!voice && !mcp.tool) {
      error.textContent = tr("Выберите голос");
      return;
    }
    error.textContent = '';
    sending = true;
    paintBusy();
    const raw = text.value;
    const edited = editing?.id;
    const cid = records.current?.id;
    const navigation = following;
    try {
      const { connection_id: connectionId, model } = chosen();
      if (mcp.tool) {
        // голос из перечня сервера уходит полем voice, личный — образцом (voiceId)
        const shared = !!voiceField?.selectedOptions?.[0]?.dataset.mcp;
        const voiceId = voiceField?.tagName === 'SELECT' && !shared && mcp.canClone() ? voice : undefined;
        const mcpModel = voiceId || !mcp.canClone() ? mcpModelPick.value || undefined : undefined;
        const ok = await mcp.run({ text: value, model: mcpModel,
          voice: !voiceId && !voiceBox.hidden && voice ? voice : undefined }, { voiceId });
        if (ok) {
          draft.sent(raw);
          if (alive && text.value === raw) setText('');
        }
        return;
      }
      const created = await api(edited ? 'PATCH' : 'POST', edited ? `/api/speeches/${edited}` : '/api/speeches', { text: value, voice,
        ...(cid ? { chat_id: cid } : {}),
        ...(model ? { connection_id: connectionId, model } : {}),
        ...(langView.wrap.hidden ? {} : { language: langPick.value }) });
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
      sending = false;
      paintBusy();
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
      mcp.destroy();
      following += 1;
      if (editing) setText(editing.draft);
      draft.save();
      records.stop();
      stopChain();
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
