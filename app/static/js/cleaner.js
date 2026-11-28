import { api } from './api.js';
import { message, tr } from './i18n.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

const BASE = '/api/cleaner/manual/chats';
const MAX_TEXT = 500000;
const WORKING = new Set(['queued', 'running']);
let stop;

export function unmount() {
  stop?.();
  stop = null;
}

function source(item, selectFragment) {
  const text = el('pre', { class: 'manual-cleaner-source' });
  const letters = Array.from(item.text);
  let start = 0;
  for (const mark of item.replacements) {
    text.append(letters.slice(start, mark.start).join(''));
    const fragment = letters.slice(mark.start, mark.end).join('');
    text.append(button(fragment, { class: 'manual-cleaner-mark', title: mark.replacement },
      () => selectFragment(item.id, fragment)));
    start = mark.end;
  }
  text.append(letters.slice(start).join(''));
  return text;
}

function editor(id, label, value, rows = 5) {
  return el('textarea', { class: 'input', id, 'aria-label': label, rows, maxlength: MAX_TEXT, spellcheck: false }, value);
}

export function mount(host, { id, navigate }) {
  const signal = new AbortController();
  let timer;
  stop = () => { signal.abort(); clearTimeout(timer); };
  const request = (method, path, body) => api(method, path, body, { signal: signal.signal });
  const path = id === null ? null : `${BASE}/${encodeURIComponent(id)}`;
  const notice = el('p', { class: 'manual-cleaner-notice', role: 'status', hidden: true });
  const content = el('div', { class: 'manual-cleaner-content' });
  const list = el('nav', { class: 'manual-cleaner-list', 'aria-label': tr("Диалоги очистки") });
  const create = button(tr("Новый диалог"), { class: 'btn btn-accent', disabled: true, id: 'cleaner-manual-new' },
    () => run(async () => navigate(`cleaner/${(await request('POST', BASE)).id}`), false));
  const settings = el('a', { href: '#/settings/cleaner', class: 'btn' }, tr("Настройки Cleaner"));
  const wrap = el('div', { class: 'manual-cleaner' },
    el('header', { class: 'head' }, el('h1', {}, tr("Очистка")), el('div', { class: 'row' }, create, settings)),
    notice, el('div', { class: 'manual-cleaner-layout' }, list, content));
  host.append(wrap);
  let busy = false;
  let draft = '';
  let answer = '';
  let conversation = null;

  function tell(text, error = false) {
    notice.textContent = text;
    notice.hidden = !text;
    notice.classList.toggle('error', !!error);
  }

  async function run(action, reload = true) {
    if (busy || signal.signal.aborted) return;
    busy = true;
    wrap.setAttribute('aria-busy', 'true');
    tell('');
    try {
      await action();
      if (reload) await refresh();
    } catch (err) {
      if (signal.signal.aborted) return;
      if (path && reload && err.status === 409) await refresh().catch(() => {});
      tell(words(err), true);
    } finally {
      busy = false;
      wrap.removeAttribute('aria-busy');
    }
  }

  function setList(data) {
    create.disabled = !data.configured;
    list.hidden = !data.items.length;
    list.replaceChildren(...data.items.map((item) => el('a', { href: `#/cleaner/${item.id}`,
      ...(item.id === id ? { 'aria-current': 'page' } : {}) }, dateTime(item.created, { dateStyle: 'medium', timeStyle: 'medium' }))));
    if (!path) {
      content.replaceChildren(el('p', { class: 'muted' }, data.configured
        ? tr("Начните новый диалог или откройте предыдущий.")
        : tr("Подключите личный ключ Cleaner, чтобы очищать запросы и восстанавливать ответы.")));
    }
  }

  async function refresh() {
    if (signal.signal.aborted) return;
    clearTimeout(timer);
    conversation = await request('GET', path);
    if (signal.signal.aborted) return;
    draw();
    if (WORKING.has(conversation.items.at(-1)?.status)) timer = setTimeout(poll, 900);
  }

  function poll() {
    if (busy) timer = setTimeout(poll, 900);
    else run(refresh, false);
  }

  function submit(label, id, value, saveDraft, action, caption) {
    const area = editor(id, label, value);
    const send = el('button', { class: 'btn btn-accent', type: 'submit', disabled: !value.trim() }, caption);
    area.addEventListener('input', () => { saveDraft(area.value); send.disabled = !area.value.trim(); });
    const form = el('form', { class: 'manual-cleaner-form' }, field(label, area), send);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!area.value.trim()) return;
      run(async () => {
        await action(area.value);
        saveDraft('');
      });
    });
    return form;
  }

  function editReplacements(items) {
    const details = el('details', { class: 'manual-cleaner-rules' }, el('summary', {}, tr("Исправить замены")));
    const selection = el('select', { class: 'input', 'aria-label': tr("Текст запроса") },
      ...items.map((item, index) => el('option', { value: item.id }, tr("Текст {0}", index + 1))));
    selection.value = items.at(-1).id;
    const value = el('input', { class: 'input', maxlength: 2000, autocomplete: 'off',
      'aria-label': tr("Фрагмент текста") });
    const save = (action) => run(async () => {
      if (!value.value.trim()) { value.focus(); throw new Error(tr("Введите фрагмент текста")); }
      await request('POST', `${path}/rules`, { action, value: value.value, item_id: selection.value });
    });
    details.append(el('div', { class: 'manual-cleaner-form' }, selection, field(tr("Фрагмент текста"), value),
      el('div', { class: 'row' }, button(tr("Скрыть фрагмент"), { class: 'btn' }, () => save('mask')),
        button(tr("Снять замену"), { class: 'btn' }, () => save('unmask')))));
    return { details, select(itemId, fragment) {
      details.open = true;
      selection.value = itemId;
      value.value = fragment;
      value.focus();
    } };
  }

  function copyRequest(preview) {
    run(async () => {
      const verified = request('POST', `${path}/copy`, { digest: preview.digest });
      let clipboard;
      if (globalThis.ClipboardItem && navigator.clipboard?.write) {
        try {
          const text = verified.then((result) => new Blob([result.text], { type: 'text/plain' }));
          text.catch(() => {});
          clipboard = navigator.clipboard.write([new ClipboardItem({ 'text/plain': text })])
            .then(() => true, () => false);
        } catch {
          // Some browsers only support writeText.
        }
      }
      const result = await verified;
      const copied = clipboard ? await clipboard : await writeText(result.text);
      await refresh();
      if (copied) tell(tr("Очищенный запрос скопирован"));
      else {
        const full = content.querySelector('#cleaner-manual-full');
        if (full) {
          full.closest('details').open = true;
          full.select();
          tell(tr("Текст выделен. Скопируйте его вручную."));
        }
      }
    }, false);
  }

  function output(preview, transfer) {
    const box = el('section', { class: 'block manual-cleaner-panel' }, el('h2', {}, tr("Очищенный запрос")));
    if (preview.digest) {
      const area = editor('cleaner-manual-result', tr("Очищенный запрос"), preview.body, 6);
      const full = editor('cleaner-manual-full', tr("Полный текст для копирования"), preview.text, 9);
      area.readOnly = true;
      full.readOnly = true;
      box.append(button(tr("Скопировать очищенный запрос"), { class: 'btn btn-accent', id: 'cleaner-manual-copy' },
        () => copyRequest(preview)), area, el('details', { class: 'manual-cleaner-rules' },
        el('summary', {}, tr("Полный текст для копирования")), full));
    } else if (preview.stale) {
      box.append(el('p', {}, tr("Правила изменились. Очистите тексты заново.")),
        button(tr("Очистить заново"), { class: 'btn' }, () => run(() => request('POST', `${path}/refresh`))));
    } else if (preview.blocked || preview.leaks) {
      box.append(el('p', { class: 'error' }, tr("Копирование заблокировано. Найдено оригиналов: {0}. Запрещённых фрагментов: {1}.",
        preview.leaks, preview.blocked)));
    } else if (preview.error) {
      box.append(el('p', { class: 'error' }, message(preview.error)));
    } else {
      box.append(el('p', { class: 'muted' }, transfer
        ? tr("Скопированный запрос больше недоступен для повторного копирования.")
        : tr("Здесь появится текст для внешней модели.")));
    }
    return box;
  }

  function draw() {
    const { items, preview, transfer } = conversation;
    const last = items.at(-1);
    const active = WORKING.has(last?.status);
    const readyInputs = items.filter((item) => item.role === 'user' && item.status === 'done');
    const rules = readyInputs.length ? editReplacements(readyInputs) : null;
    const original = el('section', { class: 'block manual-cleaner-panel' }, el('h2', {}, tr("Исходный текст")));
    for (const item of items) {
      if (item.role === 'assistant') {
        const label = tr("Восстановленный ответ");
        const restored = editor(`cleaner-answer-${item.id}`, label, item.text, 4);
        restored.readOnly = true;
        original.append(el('div', { class: 'manual-cleaner-answer' }, el('h3', {}, label), restored,
          item.unknown ? el('p', { class: 'muted small' }, tr("Неизвестных меток: {0}", item.unknown)) : null,
          button(tr("Копировать ответ"), { class: 'btn btn-sm' }, async () => {
            if (await writeText(item.text)) tell(tr("Ответ скопирован"));
            else { restored.select(); tell(tr("Текст выделен. Скопируйте его вручную.")); }
          })));
      } else {
        original.append(source(item, (itemId, fragment) => rules?.select(itemId, fragment)));
      }
    }
    if (rules) original.append(rules.details);
    if (active) {
      original.append(el('p', { class: 'muted', role: 'status' }, tr("Cleaner обрабатывает текст…")));
    } else if (last?.status === 'failed') {
      original.append(el('p', { class: 'error' }, message(last.error) || tr("Очистка не выполнена")),
        button(tr("Повторить очистку"), { class: 'btn' }, () => run(() => request('POST', `${path}/refresh`))));
    } else if (!transfer) {
      const form = submit(tr("Текст запроса"), 'cleaner-manual-prompt', draft, (value) => { draft = value; },
        (text) => request('POST', `${path}/texts`, { text, previous: last?.id ?? null }),
        last?.role === 'user' ? tr("Добавить текст") : tr("Очистить запрос"));
      original.append(last?.role === 'user'
        ? el('details', { class: 'manual-cleaner-rules', open: !!draft }, el('summary', {}, tr("Добавить к запросу")), form)
        : form);
    }

    const result = output(preview, transfer);
    if (preview.digest && items.some((item) => item.role === 'assistant')) {
      result.append(el('p', { class: 'muted small' }, tr("Текст включает предыдущие вопросы и ответы.")));
    }
    if (transfer) {
      result.append(el('h3', {}, tr("Ответ внешней модели")),
        submit(tr("Вставьте ответ с метками"), 'cleaner-manual-reply', answer, (value) => { answer = value; },
          (text) => request('POST', `${path}/restore`, { text, transfer }), tr("Восстановить")),
        button(tr("Отменить перенос"), { class: 'btn btn-sm' },
          () => run(() => request('DELETE', `${path}/transfer/${transfer}`))));
    }
    const remove = button(tr("Удалить диалог"), { class: 'btn btn-sm' }, () => {
      const confirm = button(tr("Удалить"), { class: 'btn btn-danger' }, () => run(async () => {
        await request('DELETE', path);
        dialog.close();
        navigate('cleaner');
      }, false));
      const dialog = openDialog(host, { id: 'cleaner-delete-dialog', title: tr("Удалить диалог очистки?"), actions: [confirm] });
    });
    content.replaceChildren(el('div', { class: 'row manual-cleaner-actions' },
      button(tr("Обновить"), { class: 'btn btn-sm' }, () => run(async () => {})), remove),
      el('div', { class: 'manual-cleaner-columns' }, original, result));
  }

  async function start() {
    await run(async () => {
      setList(await request('GET', BASE));
      if (path) await refresh();
    }, false);
  }
  start();
}

async function writeText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
