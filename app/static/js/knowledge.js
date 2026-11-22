import { tr } from './i18n.js';
/* Знания (knowledge.py, спека 2026-11-21-studio-source-capabilities-design, п. 15).
 *
 * `knowledgeBlock` — «Настройки» → «Чат»: свои базы знаний (создать, документы — файлом, удалить в корзину) и
 * открытые учётке общие (только чтение). `knowledgeAdminBlock` — панель: источник эмбеддингов и переранжирования
 * (только общее подключение через сервер; без него — поиск по словам) и общие базы с доступом по группам (удаление —
 * сразу). `knowledgeChip` — чип «Знания» поля ввода чата (`.ctl-chip`) с панелью флажков баз (menu.js popupPanel):
 * выбор — PUT /api/chats/<id>/knowledge. Документ уходит телом-файлом, имя — заголовком X-Studio-Name. */
import { api } from './api.js';
import { accessPicker } from './mcp-settings.js';
import { popupPanel } from './menu.js';
import { button, el, field, openDialog, words } from './ui.js';

const ACCEPT = ['.pdf', '.docx', '.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.xml', '.yaml', '.yml', '.toml',
  '.ini', '.log', '.py', '.js', '.ts', '.html', '.css', '.sql', '.sh', '.ps1'].join(',');   // documents.SUFFIXES
const STATES = { queued: tr("индексируется"), ready: tr("готов"), error: tr("без векторов — только поиск по словам") };

function say(node, text, kind) {
  node.textContent = text;
  node.classList.toggle('error', kind === 'error');
  node.classList.toggle('ok', kind === 'ok');
}

function upload(url, file) {
  return api('POST', url, file, { headers: { 'content-type': file.type || 'application/octet-stream',
    'x-studio-name': encodeURIComponent(file.name) } });
}

/** Список баз: `prefix` — адрес базы, `editable(kb)` — можно менять, `remove(kb)` — удалить базу, `extra(kb)` —
 * ещё кнопки строки. Документы грузятся по одному; отказ — словами в строке статуса. */
function basesList({ note, prefix, editable, remove, extra = () => [], reload }) {
  function documentRow(kb, doc) {
    const del = editable(kb) ? button(tr("Удалить"), { class: 'btn btn-sm kb-doc-delete',
      'aria-label': tr("Удалить документ {0}", doc.name) }, async () => {
      del.disabled = true;
      try {
        await api('DELETE', `${prefix(kb)}/documents/${encodeURIComponent(doc.id)}`);
        await reload();
      } catch (err) {
        say(note, words(err), 'error');
        del.disabled = false;
      }
    }) : null;
    return el('li', { class: 'kb-doc', 'data-doc-id': doc.id }, el('span', { class: 'kb-doc-name' }, doc.name),
      el('span', { class: 'muted small' }, ` · ${STATES[doc.state] || doc.state}`), del);
  }

  function baseNode(kb) {
    const files = el('input', { type: 'file', class: 'sr kb-files', accept: ACCEPT, multiple: true });
    files.addEventListener('change', async () => {
      const chosen = [...files.files];
      files.value = '';
      for (const file of chosen) {
        say(note, tr("Загружаю: {0}", file.name));
        try {
          await upload(`${prefix(kb)}/documents`, file);
        } catch (err) {
          say(note, `${file.name}: ${words(err)}`, 'error');
          await reload();
          return;
        }
      }
      say(note, tr("Документы добавлены"), 'ok');
      await reload();
    });
    const actions = editable(kb) ? el('div', { class: 'row' },
      el('label', { class: 'btn btn-sm kb-add' }, tr("Добавить документы"), files), ...extra(kb),
      button(tr("Удалить базу"), { class: 'btn btn-sm btn-danger kb-delete' }, () => remove(kb))) : null;
    return el('div', { class: 'block kb-base', 'data-kb-id': kb.id },
      el('b', {}, kb.name), kb.shared ? el('span', { class: 'muted small' }, ` · ${tr("общая")}`) : null,
      kb.documents.length ? el('ul', { class: 'kb-docs' }, ...kb.documents.map((doc) => documentRow(kb, doc)))
        : el('p', { class: 'muted small' }, tr("Документов пока нет")),
      actions);
  }
  return baseNode;
}

function createForm(id, onCreate) {
  const name = el('input', { class: 'input', id, maxlength: '100', placeholder: tr("Название базы") });
  const create = button(tr("Создать базу"), { class: 'btn', id: `${id}-create` }, async () => {
    if (!name.value.trim()) { name.focus(); return; }
    create.disabled = true;
    try {
      await onCreate(name.value.trim());
      name.value = '';
    } finally {
      create.disabled = false;
    }
  });
  return el('div', { class: 'row' }, field(tr("Новая база знаний"), name), create);
}

/** «Настройки» → «Чат»: свои базы и открытые учётке общие. */
export function knowledgeBlock() {
  const list = el('div', { id: 'kb-list' });
  const note = el('p', { class: 'status', id: 'kb-status', 'aria-live': 'polite' });
  const root = el('section', { class: 'block', id: 'knowledge-settings' }, el('h2', {}, tr("Базы знаний")),
    el('p', { class: 'muted' }, tr("Документы для поиска в чате: выберите базы чипом «Знания» в поле ввода. Удалённое лежит в корзине 14 дней.")));
  const baseNode = basesList({ note, prefix: (kb) => `/api/knowledge/${encodeURIComponent(kb.id)}`,
    editable: (kb) => !kb.shared, reload,
    remove: async (kb) => {
      try {
        await api('DELETE', `/api/knowledge/${encodeURIComponent(kb.id)}`);
        say(note, tr("База в корзине: {0}", kb.name), 'ok');
        await reload();
      } catch (err) {
        say(note, words(err), 'error');
      }
    } });
  async function reload() {
    try {
      const { items } = await api('GET', '/api/knowledge');
      list.replaceChildren(...items.map(baseNode));
      if (!items.length) list.append(el('p', { class: 'muted' }, tr("Баз знаний пока нет")));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }
  root.append(createForm('kb-name', async (name) => {
    try {
      await api('POST', '/api/knowledge', { name });
      say(note, '');
      await reload();
    } catch (err) {
      say(note, words(err), 'error');
    }
  }), list, note);
  queueMicrotask(reload);
  return root;
}

/** Панель администратора: источник эмбеддингов и общие базы с группами. */
export function knowledgeAdminBlock(host) {
  const conn = el('select', { class: 'input', id: 'kb-source-connection' });
  const embed = el('input', { class: 'input', id: 'kb-source-embed', maxlength: '200', placeholder: 'bge-m3' });
  const rerank = el('input', { class: 'input', id: 'kb-source-rerank', maxlength: '200', placeholder: 'bge-reranker-v2-m3' });
  const sourceNote = el('p', { class: 'status', id: 'kb-source-status', 'aria-live': 'polite' });
  const sourceOff = el('p', { class: 'status error', id: 'kb-source-off', hidden: true },
    tr("Cleaner обязателен для внешних моделей: этот источник внешний и не используется — индексация и поиск у всех только по словам (BM25)."));
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'kb-source-save' }, async () => {
    save.disabled = true;
    try {
      const id = conn.value ? Number(conn.value) : null;
      const out = await api('PUT', '/api/admin/knowledge', { connection_id: id, embed_model: embed.value,
        rerank_model: rerank.value.trim() || null });
      sourceOff.hidden = !out.source_off;
      say(sourceNote, tr("Сохранено"), 'ok');
    } catch (err) {
      say(sourceNote, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  const list = el('div', { id: 'kb-shared-list' });
  const note = el('p', { class: 'status', id: 'kb-shared-status', 'aria-live': 'polite' });
  const prefix = (kb) => `/api/admin/knowledge/bases/${encodeURIComponent(kb.id)}`;
  const baseNode = basesList({ note, prefix, editable: () => true, reload,
    extra: (kb) => [button(tr("Доступ"), { class: 'btn btn-sm kb-access' }, () => accessDialog(kb))],
    remove: (kb) => {
      const yes = button(tr("Удалить"), { class: 'btn btn-danger', id: 'kb-delete-confirm' });
      const dialog = openDialog(host, { id: 'kb-delete-dialog', title: tr("Удалить общую базу знаний?"),
        body: [el('p', {}, kb.name), el('p', { class: 'muted small' }, tr("Общая база удаляется сразу, без корзины."))],
        actions: [yes] });
      yes.addEventListener('click', async () => {
        yes.disabled = true;
        try {
          await api('DELETE', prefix(kb));
          dialog.close();
          await reload();
        } catch (err) {
          say(note, words(err), 'error');
          dialog.close();
        }
      });
    } });

  function accessDialog(kb) {
    const box = el('div', {}, el('p', { class: 'muted' }, tr("Загружаю…")));
    const fail = el('p', { class: 'error', id: 'kb-access-status', 'aria-live': 'polite' });
    const ok = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'kb-access-save', disabled: true });
    const dialog = openDialog(host, { id: 'kb-access-dialog', title: tr("Доступ: {0}", kb.name), body: [box, fail],
      actions: [ok] });
    let picker = null;
    ok.addEventListener('click', async () => {
      ok.disabled = true;
      try {
        await api('PUT', `${prefix(kb)}/access`, { groups: picker.value() });
        dialog.close();
        await reload();
      } catch (err) {
        fail.textContent = words(err);
        ok.disabled = false;
      }
    });
    api('GET', '/api/admin/groups').then(({ items }) => {
      if (!dialog.open) return;
      picker = accessPicker(items, kb.groups.length ? kb.groups : null);
      box.replaceChildren(picker.el);
      ok.disabled = false;
    }, (err) => { box.replaceChildren(); fail.textContent = words(err); });
  }

  async function reload() {
    try {
      const [{ source, source_off: off, bases }, { items }] = await Promise.all([api('GET', '/api/admin/knowledge'),
        api('GET', '/api/admin/shared/connections')]);
      sourceOff.hidden = !off;
      conn.replaceChildren(el('option', { value: '' }, tr("Нет — только поиск по словам")),
        ...items.filter((c) => c.proxied).map((c) => el('option', { value: String(c.id) }, c.name)));
      conn.value = source ? String(source.connection_id) : '';
      embed.value = source ? source.embed_model : '';
      rerank.value = source?.rerank_model || '';
      list.replaceChildren(...bases.map(baseNode));
      if (!bases.length) list.append(el('p', { class: 'muted' }, tr("Общих баз знаний пока нет")));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }
  queueMicrotask(reload);
  return el('section', { class: 'block', id: 'admin-knowledge' }, el('h2', {}, tr("Знания")),
    el('p', { class: 'muted' }, tr("Эмбеддинги и переранжирование для поиска по большим вложениям и базам знаний. Без источника работает поиск по словам.")),
    field(tr("Общее подключение"), conn), field(tr("Модель эмбеддингов"), embed),
    field(tr("Модель переранжирования (необязательно)"), rerank), el('div', { class: 'row' }, save, sourceNote), sourceOff,
    el('h3', {}, tr("Общие базы знаний")),
    el('p', { class: 'muted small' }, tr("По умолчанию доступны всем; ограничить группами — кнопка «Доступ». Администратор видит все общие базы.")),
    createForm('kb-shared-name', async (name) => {
      try {
        await api('POST', '/api/admin/knowledge/bases', { name });
        say(note, '');
        await reload();
      } catch (err) {
        say(note, words(err), 'error');
      }
    }), list, note);
}

/** Чип «Знания» поля ввода: `current()` — выбранные id, `choose(ids)` — сохранить (PUT или черновик нового
 * диалога). Баз нет — чип скрыт; `refresh()` перечитывает базы, `paint()` — подпись. */
export function knowledgeChip({ current, choose, failed }) {
  let bases = [];
  const label = el('span', { class: 'chip-name' }, tr("Знания"));
  const chip = el('button', { type: 'button', id: 'kb-chip', class: 'ctl-chip kb-chip', hidden: true,
    title: tr("Базы знаний диалога") }, label);
  const panel = el('div', { id: 'kb-panel', class: 'kb-panel' });
  const popup = popupPanel(chip, panel, { label: tr("Базы знаний диалога"), side: 'above' });
  // флажки — до размещения и фокуса панели: beforetoggle идёт внутри showPopover
  panel.addEventListener('beforetoggle', (event) => { if (event.newState === 'open') fill(); });

  function fill() {
    const chosen = new Set(current());
    panel.replaceChildren(...bases.map((kb) => {
      const box = el('input', { type: 'checkbox', value: kb.id, checked: chosen.has(kb.id) });
      box.addEventListener('change', async () => {
        const ids = [...panel.querySelectorAll('input:checked')].map((input) => input.value);
        try {
          await choose(ids);
          paint();
        } catch (err) {
          box.checked = !box.checked;
          failed(err);
        }
      });
      return el('label', { class: 'check kb-option' }, box, kb.name,
        kb.shared ? el('span', { class: 'muted small' }, ` · ${tr("общая")}`) : null);
    }));
  }

  function paint() {
    const known = new Set(bases.map((kb) => kb.id));
    const n = current().filter((id) => known.has(id)).length;
    label.textContent = n ? tr("Знания ({0})", n) : tr("Знания");
    chip.hidden = !bases.length;
  }

  async function refresh() {
    try {
      bases = (await api('GET', '/api/knowledge')).items;
    } catch {
      bases = [];
    }
    paint();
  }
  return { chip, panel, paint, refresh, close: () => popup.close() };
}
