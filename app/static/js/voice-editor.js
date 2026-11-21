/* A voice owns up to ten independent references. The selected one is used for synthesis.
 * Recognition (`POST /api/voices/transcribe`) also returns `upload`, the prepared WAV token: saving reuses it while it
 * is fresh, so a file is uploaded once; only a sample without a fresh token goes to `prepare-sample`. Tokens that were
 * not saved are discarded when the dialog closes (the server keeps referenced ones). Closing with unsaved changes
 * («Отмена», Escape, Android back) asks first (`#voice-unsaved`); so does any address change (the iOS back swipe,
 * a section tab, «Открыть» in «В работе»): settings.js `hold()` asks `holdVoiceEditor()`. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { cancelRecording, recordingName, startRecording } from './mic.js';
import { button, el, field, openDialog, words } from './ui.js';

const MAX_SAMPLES = 10;
const MAX_SECONDS = 60;
const TEXT_MAX = 2000;
const DRAFT_MS = 10 * 60 * 1000;   // token of a recognized sample: voices.DRAFT_SEC (900) with a margin

let shown = null;   // the open editor: { dialog, home, entry, dirty, ask } — address and history mark it was opened at

/** Router (app.js, through settings.js `hold`) before any address change, like transcribe.js `hold`: unsaved editor —
 * the address goes back and «Изменения не сохранены» asks; «Не сохранять» goes on to where the user was heading.
 * A history move (an entry with a mark) leaves that entry alone: the editor's address is pushed anew, leaving goes
 * back to it. true — the change is held. */
export function holdVoiceEditor() {
  if (!shown || !shown.dialog.isConnected || !shown.dirty() || location.hash === shown.home) return false;
  const target = location.hash;
  if (history.state && history.state.entry) {
    history.pushState({ entry: shown.entry }, '', shown.home);
    shown.ask(() => history.back());
  } else {
    history.replaceState({ entry: shown.entry }, '', shown.home);
    shown.ask(() => location.replace(target));
  }
  return true;
}

function clock(sec) {
  return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
}

export async function editVoice(host, voice, onSaved) {
  if (host.querySelector('#voice-dialog')) return;
  const original = voice ? await api('GET', `/api/voices/${voice.id}`) : null;
  if (!host.isConnected || host.querySelector('#voice-dialog')) return;
  const voiceId = original?.id || crypto.randomUUID().replaceAll('-', '');
  let entries = [];
  let selected = null;
  let recording = null;
  let starting = false;
  let saving = false;
  let initial = null;          // snapshot() right after opening: anything else is an unsaved change
  const issued = [];           // every upload token recognition returned: discarded on close unless saved
  const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off', value: original?.name || '' });
  const list = el('div', { class: 'voice-samples' });
  const file = el('input', { type: 'file', id: 'voice-files', accept: 'audio/*,.m4a,.mp3,.wav,.ogg,.webm', multiple: true });
  const rec = button(tr("Записать с микрофона"), { class: 'btn btn-sm', id: 'voice-record' });
  const recNote = el('p', { class: 'muted small', 'aria-live': 'polite' });
  const fail = el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'voice-save' });
  const dialog = openDialog(host, { id: 'voice-dialog', title: voice ? tr("Изменить голос") : tr("Новый голос"),
    guard: () => !dirty() || askClose(), actions: [save], body: [field(tr("Имя голоса"), name, 'name'),
      el('p', { class: 'muted small' }, tr("Запись 5–60 секунд чистой речи. Можно выбрать несколько файлов, до 10 образцов на голос.")),
      field(tr("Добавить образцы"), file, 'audio'), rec, recNote,
      el('p', { class: 'muted small' }, tr("Для озвучки используется выбранный образец.")), list, fail] });

  function busy() {
    const pending = entries.some(e => e.pending);
    save.disabled = saving || starting || !!recording || pending || entries.some(rangeChanged);
    file.disabled = saving || starting || !!recording || pending;
    rec.disabled = saving || starting || pending;
    name.disabled = saving;
    for (const entry of entries) {
      entry.text.disabled = saving || entry.pending;
      entry.radio.disabled = saving;
      entry.remove.disabled = saving || !!recording || starting;
      entry.replace.disabled = saving || !!recording || starting;
      entry.retry.disabled = saving || entry.pending || !!recording || starting;
      entry.start.disabled = entry.end.disabled = entry.rangeStart.disabled = entry.rangeEnd.disabled = saving || entry.pending;
      entry.applyRange.disabled = saving || entry.pending || !rangeChanged(entry);
    }
  }

  function choose(entry) {
    selected = entry;
    for (const e of entries) e.radio.checked = e === entry;
  }

  /** Name, samples (saved id, new recording, text) and the selected one — what saving would send. */
  function snapshot() {
    return JSON.stringify([name.value, entries.map(e => [e.id || null, !!e.file, e.text.value, e.start.value, e.end.value]),
      entries.indexOf(selected)]);
  }

  function dirty() {
    return initial !== null && snapshot() !== initial;
  }

  /** «Изменения не сохранены»: «Не сохранять» closes the editor (then `leave`, if given), «Остаться» keeps it.
   * Always false (stay for now). */
  function askClose(leave = null) {
    if (host.querySelector('#voice-unsaved')) return false;
    const drop = button(tr("Не сохранять"), { class: 'btn btn-danger', id: 'voice-unsaved-drop' });
    const ask = openDialog(host, { id: 'voice-unsaved', title: tr("Изменения не сохранены"), actions: [drop],
      cancelText: tr("Остаться"), body: [el('p', {}, tr("Образцы, тексты и имя голоса не сохранятся."))] });
    drop.addEventListener('click', () => {
      ask.close();
      dialog.close();
      shown = null;          // the dialog's close event comes as a later task: the router must not hold again
      if (leave) leave();
    });
    return false;
  }

  function stopPreview(entry) {
    entry.player.pause();
    entry.player.removeAttribute('src');
    entry.player.load();
    if (entry.url) URL.revokeObjectURL(entry.url);
    entry.url = null;
  }

  function preview(entry) {
    stopPreview(entry);
    if (entry.file) entry.url = URL.createObjectURL(entry.file);
    const src = entry.url || (entry.mediaId ? `/api/media/${encodeURIComponent(entry.mediaId)}` : '');
    entry.player.hidden = !src;
    entry.range.hidden = !entry.file;
    if (src) entry.player.src = src;
  }

  function rangeChanged(entry) {
    return !!entry.file && (entry.start.value !== entry.trim.start || entry.end.value !== entry.trim.end);
  }

  function formOf(entry) {
    const body = new FormData();
    body.append('audio', entry.file, entry.file.name || recordingName(entry.file, 'sample'));
    for (const [key, value] of Object.entries(entry.trim)) if (value !== '') body.append(key, value);
    return body;
  }

  function makeEntry(sample = {}) {
    const entry = { id: sample.id, mediaId: sample.media_id, file: null, pending: false, trim: { start: '', end: '' } };
    entry.player = el('audio', { class: 'voice-sample-audio', controls: true, preload: 'metadata', hidden: true,
      'aria-label': tr("Прослушать образец") });
    entry.text = el('textarea', { class: 'input', name: 'text', rows: '3', maxlength: String(TEXT_MAX),
      placeholder: tr("Определится автоматически после загрузки или записи") }, sample.text || '');
    entry.note = el('p', { class: 'muted small', 'aria-live': 'polite', ...(!entries.length ? { id: 'voice-asr-status' } : {}) });
    entry.error = el('p', { class: 'error', 'aria-live': 'polite' });
    entry.start = el('input', { class: 'input voice-start', type: 'number', min: '0', step: '0.1', placeholder: '0' });
    entry.end = el('input', { class: 'input voice-end', type: 'number', min: '0', step: '0.1' });
    const setTime = (input) => {
      input.value = entry.player.currentTime.toFixed(1);
      busy();
    };
    entry.rangeStart = button(tr("Начало здесь"), { class: 'btn btn-sm' }, () => setTime(entry.start));
    entry.rangeEnd = button(tr("Конец здесь"), { class: 'btn btn-sm' }, () => setTime(entry.end));
    entry.applyRange = button(tr("Распознать фрагмент"), { class: 'btn btn-sm voice-apply-range' }, () => {
      entry.trim = { start: entry.start.value, end: entry.end.value };
      transcribe(entry);
    });
    entry.start.addEventListener('input', busy);
    entry.end.addEventListener('input', busy);
    entry.range = el('details', { class: 'voice-range', hidden: true }, el('summary', {}, tr("Выбрать фрагмент")),
      el('div', { class: 'row' }, field(tr("Начало, с"), entry.start), entry.rangeStart),
      el('div', { class: 'row' }, field(tr("Конец, с"), entry.end), entry.rangeEnd), entry.applyRange);
    entry.radio = el('input', { type: 'radio', name: 'voice-reference' });
    entry.radio.addEventListener('change', () => choose(entry));
    entry.retry = button(tr("Распознать ещё раз"), { class: 'btn btn-sm', hidden: true,
      ...(!entries.length ? { id: 'voice-transcribe' } : {}) }, () => transcribe(entry));
    entry.replace = el('input', { type: 'file', accept: file.accept });
    entry.replace.addEventListener('change', () => {
      if (!entry.replace.files[0]) return;
      entry.file = entry.replace.files[0];
      entry.start.value = entry.end.value = '';
      entry.trim = { start: '', end: '' };
      entry.replace.value = '';
      preview(entry);
      transcribe(entry);
    });
    entry.remove = button(tr("Удалить образец"), { class: 'btn btn-sm btn-danger' }, () => {
      if (entries.length === 1) {
        fail.textContent = tr("Нужен хотя бы один образец голоса");
        return;
      }
      entry.controller?.abort();
      entry.discarded = true;
      stopPreview(entry);
      entries = entries.filter(e => e !== entry);
      entry.node.remove();
      if (selected === entry) choose(entries[0]);
      busy();
    });
    entry.node = el('section', { class: 'voice-sample' },
      el('label', { class: 'row' }, entry.radio, tr("Для озвучки"),
        sample.duration ? ` · ${clock(sample.duration)}` : ''),
      entry.player, entry.range,
      field(tr("Текст образца (можно поправить)"), entry.text, 'text'),
      field(tr("Заменить запись"), entry.replace, 'replacement'), entry.note, entry.error,
      el('div', { class: 'row' }, entry.retry, entry.remove));
    entries.push(entry);
    list.append(entry.node);
    preview(entry);
    if (sample.active || !selected) choose(entry);
    return entry;
  }

  async function transcribe(entry) {
    if (!entry.file || entry.discarded || !dialog.open || !dialog.isConnected) return;
    entry.controller?.abort();
    const controller = new AbortController();
    entry.controller = controller;
    entry.pending = true;
    entry.upload = null;               // the token belonged to the previous recording or recognition
    entry.text.value = '';
    entry.error.textContent = '';
    entry.note.textContent = tr("Распознаю текст образца…");
    entry.text.setAttribute('aria-busy', 'true');
    entry.retry.hidden = true;
    busy();
    try {
      const result = await api('POST', '/api/voices/transcribe', formOf(entry), { signal: controller.signal });
      if (result.upload) {
        if (dialog.isConnected) issued.push(result.upload);
        else discard([result.upload]);   // the editor closed while recognizing: nobody will save it
      }
      if (entry.controller !== controller || entry.discarded || !dialog.open || !dialog.isConnected) return;
      if (result.upload) {
        entry.upload = result.upload;
        entry.uploadAt = Date.now();
      }
      entry.text.value = result.text;
      entry.note.textContent = tr("Текст распознан. Проверьте его перед сохранением.");
    } catch (err) {
      if (controller.signal.aborted || entry.controller !== controller || !dialog.open || !dialog.isConnected) return;
      entry.note.textContent = tr("Распознавание не удалось. Повторите или введите текст образца вручную.");
      entry.error.textContent = words(err);
    } finally {
      if (entry.controller === controller) {
        entry.controller = null;
        entry.pending = false;
        entry.text.setAttribute('aria-busy', 'false');
        entry.retry.hidden = false;
        busy();
      }
    }
  }

  async function addFiles(files) {
    const kept = entries.filter(e => e.id || e.file);
    if (kept.length + files.length > MAX_SAMPLES) {
      fail.textContent = tr("У голоса не больше 10 образцов");
      return;
    }
    for (const entry of entries.filter(e => !kept.includes(e))) {
      entry.controller?.abort();
      entry.discarded = true;
      stopPreview(entry);
      entry.node.remove();
    }
    entries = kept;
    if (!entries.includes(selected)) selected = null;
    fail.textContent = '';
    const added = files.map(blob => {
      const entry = makeEntry();
      entry.file = blob;
      preview(entry);
      entry.pending = true;
      return entry;
    });
    busy();
    // Recognize the batch in order; every transcript stays with its own recording.
    for (const entry of added) await transcribe(entry);
  }

  file.addEventListener('change', () => {
    const files = [...file.files];
    file.value = '';
    if (files.length) addFiles(files);
  });
  rec.addEventListener('click', async () => {
    if (recording) { recording.stop(); return; }
    starting = true;
    busy();
    try {
      recording = await startRecording(MAX_SECONDS, sec => {
        recNote.textContent = tr("Запись · {0} — нажмите ещё раз, чтобы остановить", clock(sec));
      }, rec);
    } catch (err) {
      recNote.textContent = words(err);
    } finally {
      starting = false;
      busy();
    }
    if (!recording) return;
    rec.textContent = tr("Остановить");
    const blob = await recording.done;
    recording = null;
    rec.textContent = tr("Записать с микрофона");
    busy();
    if (!blob || !dialog.open || !dialog.isConnected) return;
    recNote.textContent = tr("Запись готова");
    file.value = '';
    await addFiles([blob]);
  });

  save.addEventListener('click', async () => {
    if (save.disabled) return;
    fail.textContent = '';
    if (!name.value.trim()) {
      fail.textContent = tr("Имя голоса не должно быть пустым");
      name.focus();
      return;
    }
    if (!entries.length || entries.some(e => !e.id && !e.file)) {
      fail.textContent = tr("Нужна запись образца: файл или с микрофона");
      return;
    }
    if (entries.some(e => !e.text.value.trim())) {
      fail.textContent = tr("Введите текст каждого образца");
      return;
    }
    saving = true;
    busy();
    const uploads = [];
    try {
      const samples = [];
      for (const entry of entries) {
        const sample = { id: entry.id, text: entry.text.value.trim() };
        if (entry.file && entry.upload && Date.now() - entry.uploadAt < DRAFT_MS) {
          sample.upload = entry.upload;        // prepared by recognition: no second upload
        } else if (entry.file) {
          const { upload } = await api('POST', '/api/voices/prepare-sample', formOf(entry));
          uploads.push(upload);
          sample.upload = upload;
        }
        if (!dialog.open || !dialog.isConnected) return;
        samples.push(sample);
      }
      await api('PUT', `/api/voices/${voiceId}/batch`, { name: name.value.trim(), revision: original?.revision,
        selected: entries.indexOf(selected), samples });
      uploads.length = 0;
      if (dialog.open) dialog.close();
      onSaved();
    } catch (err) {
      fail.textContent = words(err);
      // «prepared sample unavailable» (an expired token among others): the next save uploads the files again
      if (err.code === 'samples') for (const entry of entries) entry.upload = null;
    } finally {
      saving = false;
      busy();
      // A lost response may mean that the voice was already saved. The server keeps referenced uploads.
      if (uploads.length) discard(uploads);
    }
  });
  const mine = { dialog, home: location.hash, entry: history.state?.entry ?? null, dirty, ask: askClose };
  shown = mine;
  dialog.addEventListener('close', () => {
    if (shown === mine) shown = null;
    for (const entry of entries) {
      entry.controller?.abort();
      stopPreview(entry);
    }
    if (recording || starting) cancelRecording();
    discard(issued);                   // the saved ones are referenced and stay
  });
  for (const sample of original?.samples || [{}]) makeEntry(sample);
  initial = snapshot();
  busy();
  name.focus();
}

/** Prepared WAV tokens nobody will save: the server retires the unreferenced ones (DraftsIn takes up to ten). */
function discard(tokens) {
  for (let i = 0; i < tokens.length; i += MAX_SAMPLES) {
    api('POST', '/api/voices/discard-samples', { uploads: tokens.slice(i, i + MAX_SAMPLES) }).catch(() => {});
  }
}
