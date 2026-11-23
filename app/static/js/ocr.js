import { language, message as serverMessage, tr } from './i18n.js';
/* Раздел «Сканы» (#/ocr, распознавание) — раскладка Б спеки 2026-10-27-studio-ocr-design (раздел 6.4).
 *
 * Слева — «+ Загрузить скан» и документы по дням (имя, «N стр.», состояние словами), справа — выбранный:
 * «N страниц · дата», «Копировать», «Скачать ▾» (txt, md, docx), «В чат ▾», «Переименовать» (PATCH `name`,
 * расширение сохраняет сервер), «Удалить»; идущий — состояние
 * и «Отменить». Ниже — листалка «‹ страница N из M ›» и две колонки: картинка страницы (нажатие — окно
 * просмотра #ocr-viewer) и её текст. На телефоне (≤ 760 px) картинка над текстом, список выезжает сбоку,
 * как у «Расшифровки». Готовые страницы идущего документа уже листаются; опрос вид не пересобирает — картинка,
 * текст страницы и кнопки остаются теми же узлами, пока их данные те же (feed.keep).
 *
 * Загрузка — один PDF или несколько фото одним выбором (один документ, страницы в порядке выбора):
 * XMLHttpRequest с FormData (части `file`, ход в процентах), 423 запертой студии (XHR идёт мимо api.js)
 * ждёт отпирания и шлёт те же файлы заново. Список — createPicker (feed.js): опрос идущих раз в POLL_MS,
 * «Показать ещё», выбор по номеру. Раздел без модели (ctx.hint): документы читаются, скачиваются и
 * удаляются, вместо загрузки — подсказка.
 *
 * Текст страницы — Markdown модели через renderMarkdown(…, { html: true }) (marked + DOMPurify: таблицы
 * HunyuanOCR — HTML-тегами, скрипты — нет; формулы `$…$` рисует KaTeX); прочий текст сервера — только textContent.
 * «Копировать» — весь документ, как выгрузка .md, и он же HTML с формулами MathML (`markdownHtml`); «Прочитать» #ocr-read — его же озвучка (speech-read.js, проигрыватель — #ocr-read-box,
 * другой документ его убирает). «В чат ▾» → #/chat?ocr=<id>&ask=discuss|summary|translate.
 * «Перевести» у готового скана — выбор языка и новая запись «Перевода» без повторного распознавания.
 * PDF длиннее предела (422 `pages` с числом страниц) — окно диапазона #ocr-range-dialog и тот же файл заново с
 * x-studio-first-page/x-studio-last-page; страницы документа носят номера исходника (листалка — «страница 51 · 2 из 3»).
 * «Править» у страницы с текстом кончившегося документа (05:I6): Markdown страницы в поле, PATCH …/pages/<n>;
 * «правлено» и «Вернуть распознанное» (DELETE …/pages/<n>/edit). Пока идёт правка, листалка стоит.
 * #/ocr?id=<id> («Открыть» в «В работе», «Поиск») выбирает документ после загрузки списка, если его не
 * выбрали сами; ?id из адреса уходит. Принятый документ зовёт jobs.kick(). */
import { api, hooks, me, toLogin } from './api.js';
import { holdRefresh } from './refresh.js';
import { icon } from './icons.js';
import { actionMenu } from './menu.js';
import { NAMES } from './sections-meta.js';
import { dateTime } from './time.js';
import { busyBlock, createPicker, keep, pickerList, recordSelection } from './feed.js';
import { kick } from './jobs.js';
import { shareDialog } from './chat-share.js';
import { sectionMcp } from './mcp-sections.js';
import { markdownHtml, renderMarkdown } from './markdown.js';
import { clearRead, readAloud } from './speech-read.js';
import { modelLabel } from './settings.js';
import { picker } from './picker.js';
import { DOCUMENT_PHOTO_SIDE, resizePhoto } from './resize-photo.js';
import { el, field, openDialog, plural, renameDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const UPLOAD_MAX = 20 * 1024 * 1024;     // documents.DOC_MAX — на всю загрузку
const FILES_MAX = 50;                    // ocr_render.PAGES_MAX
const ACCEPT = 'application/pdf,.pdf,image/png,image/jpeg,image/webp';
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const FORMATS = [['txt', tr("Текст .txt")], ['md', 'Markdown .md'], ['docx', 'Word .docx']];
const ASKS = [['discuss', tr("Обсудить в чате")], ['summary', tr("Кратко пересказать")], ['translate', tr("Перевести")]];

let screen = null;

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Текст узла — только если сменился: опрос не сбрасывает выделение в нём. */
function put(node, value) {
  const shown = value ?? '';
  if (node.textContent !== shown) node.textContent = shown;
}

function media(id) {
  return `/api/media/${encodeURIComponent(id)}`;
}

/** Текст страницы без распознанного — как в выгрузке (exports._page_body). */
function pageBody(p) {
  if (p.state === 'done' || p.state === 'layer') return p.text || '';
  if (p.state === 'blank') return tr("[пустая страница]");
  return serverMessage(p.detail) ? tr("[не распознана: {0}]", serverMessage(p.detail)) : tr("[не распознана]");
}

/** Кончившийся документ (и ошибка, и отмена) со страницей с текстом — «Копировать», «Скачать», «В чат»
 * (ocr._readable). */
function readable(item) {
  return !RUNNING.has(item.state) && item.pages.some((p) => p.state === 'done' || p.state === 'layer');
}

/** Весь документ — как выгрузка .md (exports.pages_md). */
export function asMarkdown(pages) {
  return `${pages.map((p) => tr("## Страница {0}\n\n{1}", p.n, pageBody(p))).join('\n\n')}\n`;
}

function build(view, ctx) {
  let uploading = false;
  let picked = false;          // документ выбрали сами (список, загрузка): ?id из адреса его не перебивает
  let shownId = null;          // документ, чья страница сейчас в листалке
  let readFor = null;          // документ, чью озвучку показывает readBox
  const readBox = el('div', { class: 'read-box', id: 'ocr-read-box' });
  let at = 0;                  // место показанной страницы в документе (с 0)
  let editing = null;          // { id, n, draft } — правка текста страницы n документа id
  let busy = null;             // блок «идёт» выбранного документа (feed.busyBlock): опрос меняет в нём только текст
  // вид выбранного переживает опрос (05:F14): узлы — по ключу своих данных, иначе меняются только тексты
  let acts = null;             // { key, box } — кнопки документа
  let two = null;              // { key, box, scan, text } — показанная страница: картинка и текст
  let engines = [];            // движки раздела через сервер: { connection_id, model, label } (GET /api/sections)
  const selection = recordSelection('ocr', me.id);
  const docs = createPicker({ url: '/api/ocr', size: PAGE, pollMs: POLL_MS,
    running: (i) => RUNNING.has(i.state), paint: paintList,
    show() {
      if (docs.current.id !== shownId) selection.save(docs.current.id);
      error.textContent = '';
      paintList();
      paintView();
    },
    fail(err) { error.textContent = words(err); } });
  const { select, refresh } = docs;

  const input = el('input', { type: 'file', class: 'sr', accept: ACCEPT, multiple: true });
  const add = el('label', { class: 'btn btn-accent tr-add', id: 'ocr-add', hidden: !!ctx.hint }, icon('plus'), tr("Загрузить скан"), input);
  const progress = el('p', { id: 'ocr-progress', class: 'muted small', 'aria-live': 'polite' });
  const list = el('nav', { class: 'tr-list', 'aria-label': tr("Документы") });
  const aside = el('aside', { class: 'split-aside', id: 'ocr-aside' }, ...(ctx.hint ? [] : [add, progress]), list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'btn btn-sm split-toggle', 'aria-label': tr("Документы"),
    'aria-controls': 'ocr-aside', 'aria-expanded': 'false' }, icon('list'), tr("Документы"));
  const upload = el('button', { type: 'button', class: 'ctl-primary', id: 'ocr-upload', hidden: !!ctx.hint }, icon('plus'), tr("Загрузить скан"));
  upload.addEventListener('click', () => input.click());
  const mcp = sectionMcp('ocr', { native: !ctx.hint, fields: [], fileRequired: 'artifact_id', onChange: paintMcp, onBusy: paintBusy });
  const toolbar = el('div', { class: 'split-toolbar' }, toggle, mcp.control, ctx.hint ? null : upload);
  const title = el('h1', {}, NAMES.ocr);
  // движок загрузки — когда отмеченных пар раздела больше одной; иначе — умолчание раздела на сервере.
  // Выбор «как у Агентов» (picker.js) — в ряду над видом, после выбора инструмента MCP
  const engine = el('select', { id: 'ocr-model' });
  const head = el('header', { class: 'head' }, title);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'ocr-view' });
  const nativeHint = ctx.hint?.('hint-bar');
  const main = el('section', { class: 'split-main' }, head, toolbar, nativeHint, error, mcp.output, body);
  const root = el('div', { class: 'split ocr' }, aside, backdrop, main);
  view.append(root);
  const enginePick = picker(root, tr("Модель распознавания"), engine);
  enginePick.wrap.hidden = true;
  mcp.control.after(enginePick.wrap);

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список */
  const paintRows = pickerList(list, { cls: 'ocr-item', moreId: 'ocr-more', empty: tr("Здесь появятся ваши документы"),
    name: (item) => item.name,
    state: (item) => [tr("{0} стр.", item.pages), serverMessage(item.detail),
      item.failed_pages ? tr("не распознано: {0}", item.failed_pages) : ''].filter(Boolean).join(' · '),
    pick(id) {
      panel(false);
      picked = true;
      select(id);
    } });

  function paintList() {
    paintRows(docs);
  }

  /* выбранный */
  function download(format) {
    el('a', { href: `/api/ocr/${docs.current.id}/export?format=${format}&lang=${language()}`, download: '' }).click();
  }

  function rename(item) {
    renameDialog(root, { id: 'ocr-rename-dialog', name: item.name, async save(name) {
      const got = await api('PATCH', `/api/ocr/${item.id}`, { name });     // ответ ленты: pages — числом
      for (const x of [...docs.items, docs.current]) if (x && x.id === got.id) x.name = got.name;
      paintList();
      paintView();
    } });
  }

  async function translateDialog(item) {
    const target = el('select', { class: 'input', id: 'ocr-translate-target', disabled: true });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const go = el('button', { type: 'button', class: 'btn btn-accent', id: 'ocr-translate-confirm', disabled: true }, tr("Перевести"));
    let saving = false;
    const dialog = openDialog(root, { id: 'ocr-translate-dialog', title: tr("Перевести"), actions: [go],
      guard: () => !saving, body: [field(tr("Язык перевода"), target, 'target_lang'), fail] });
    try {
      const langs = await api('GET', '/api/translate/languages');
      if (!dialog.open || !root.isConnected) return;
      const names = language() === 'en' ? new Intl.DisplayNames(['en'], { type: 'language' }) : null;
      target.replaceChildren(...langs.items.map(({ code, name }) => el('option', { value: code }, names?.of(code) || name)));
      target.value = langs.default_target;
      target.disabled = go.disabled = false;
      target.focus();
      go.addEventListener('click', async () => {
        saving = target.disabled = go.disabled = true;
        fail.textContent = '';
        try {
          const result = await api('POST', `/api/translate/scans/${encodeURIComponent(item.id)}`,
            { source_lang: langs.auto, target_lang: target.value });
          kick();
          dialog.close();
          if (root.isConnected) ctx.navigate(`translate?id=${encodeURIComponent(result.id)}`);
        } catch (err) {
          fail.textContent = words(err);
        } finally {
          saving = target.disabled = go.disabled = false;
        }
      });
    } catch (err) {
      fail.textContent = words(err);
    }
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'ocr-delete-confirm' }, tr("Удалить"));
    const dialog = openDialog(root, { id: 'ocr-delete-dialog', title: tr("Удалить документ?"), actions: [yes],
      body: [el('p', {}, tr("Документ уйдёт в корзину на 14 дней вместе с картинками страниц и исходным файлом — оттуда его можно вернуть.")), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/ocr/${item.id}`);
        dialog.close();
        docs.forget(item.id);
        selection.save(null);
        paintView();
        await docs.load();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /** «Скачать ▾» и «В чат ▾» — общий компонент меню (menu.js actionMenu). */
  function menu(id, cls, label, entries) {
    return actionMenu(label, { id, cls, entries: entries.map(([key, text, attr, fn]) => [text, { [attr]: key }, () => fn(key)]) });
  }

  function openViewer(page) {
    const img = el('img', { src: media(page.media_id), alt: tr("Страница {0}", page.n) });
    const stage = el('div', { class: 'viewer-stage' }, img);
    const close = el('button', { type: 'button', class: 'icon-btn viewer-close', 'aria-label': tr("Закрыть") }, icon('x'));
    const dialog = el('dialog', { id: 'ocr-viewer', class: 'viewer', 'aria-label': tr("Страница {0}", page.n) },
      el('div', { class: 'viewer-top' }, close), stage);
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => dialog.remove());
    // 1:1 — точка под пальцем или курсором остаётся на месте; клик мимо картинки — закрыть
    img.addEventListener('click', (event) => {
      const imageRect = img.getBoundingClientRect();
      const fx = (event.clientX - imageRect.left) / imageRect.width;
      const fy = (event.clientY - imageRect.top) / imageRect.height;
      if (!dialog.classList.toggle('full')) return;
      const rect = stage.getBoundingClientRect();
      // clientX/Y — экранные координаты, scrollLeft/Top — CSS px до масштаба интерфейса.
      const x = (event.clientX - rect.left) * stage.offsetWidth / rect.width - stage.clientLeft;
      const y = (event.clientY - rect.top) * stage.offsetHeight / rect.height - stage.clientTop;
      stage.scrollLeft = fx * img.offsetWidth - x;
      stage.scrollTop = fy * img.offsetHeight - y;
    });
    stage.addEventListener('click', (event) => { if (event.target === stage) dialog.close(); });
    root.append(dialog);
    dialog.showModal();
  }

  function scanNode(page) {
    if (page.media_id) {
      const open = el('button', { type: 'button', class: 'ocr-scan-open', 'aria-label': tr("Страница {0} во весь экран", page.n) },
        el('img', { class: 'ocr-scan', src: media(page.media_id), alt: tr("Страница {0}", page.n), decoding: 'async' }));
      open.addEventListener('click', () => openViewer(page));
      return open;
    }
    return el('p', { class: 'ocr-noscan muted small' },
      page.state === 'layer' ? tr("страница взята из текстового слоя") : tr("картинка страницы ещё не готова"));
  }

  async function loadEngines() {
    try {
      const { sections } = await api('GET', '/api/sections');
      const section = sections.find((s) => s.id === 'ocr');
      engines = !section ? [] : section.links.filter((l) => l.connection.proxied).flatMap(({ connection: c, models }) =>
        models.map((m) => ({ connection_id: c.id, model: m, label: `${c.name} · ${modelLabel(m)}` })));
      engine.replaceChildren(...engines.map((e, i) => el('option', { value: String(i) }, e.label)));
      const chosen = section && section.default;
      engine.selectedIndex = Math.max(0, engines.findIndex((e) => chosen && e.connection_id === chosen.connection_id
        && e.model === chosen.model));
      paintEngine();
    } catch (err) {
      error.textContent = words(err);
    }
    paintView();
  }

  /** Чем распознан документ: «подключение · модель» из движков раздела, иначе — имя модели. */
  function engineName(item) {
    const found = engines.find((e) => e.connection_id === item.connection_id && e.model === item.model);
    return found ? found.label : modelLabel(item.model);
  }

  /** «Распознать заново» с выбором движка (их больше одного): тот же — нераспознанные страницы на месте, другой —
   * новый документ со всеми страницами (ocr.again), он и открывается. */
  function retryDialog(item) {
    if (root.querySelector('#ocr-retry-dialog')) return;
    const pick = el('select', { class: 'input', id: 'ocr-retry-model' },
      ...engines.map((e, i) => el('option', { value: String(i) }, e.label)));
    pick.selectedIndex = Math.max(0, engines.findIndex((e) => e.connection_id === item.connection_id && e.model === item.model));
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const yes = el('button', { type: 'button', class: 'btn btn-accent', id: 'ocr-retry-confirm' }, tr("Распознать"));
    const dialog = openDialog(root, { id: 'ocr-retry-dialog', title: tr("Распознать заново"), actions: [yes],
      body: [field(tr("Модель"), pick, 'model'),
        el('p', { class: 'muted small' }, tr("Той же моделью — только нераспознанные страницы. Другой — новый документ со всеми страницами, прежний остаётся.")),
        fail] });
    yes.addEventListener('click', async () => {
      const { connection_id, model } = engines[pick.selectedIndex];
      yes.disabled = pick.disabled = true;
      fail.textContent = '';
      let answer;
      try {
        answer = await api('POST', `/api/ocr/${item.id}/retry`, { connection_id, model });
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = pick.disabled = false;
        return;
      }
      kick();
      if (dialog.open) dialog.close();
      picked = true;
      await docs.load();
      await select(answer.id);
    });
  }

  /* «Повторить страницу» (page) и «Распознать заново» (без page): документ снова в очереди — POST …/retry */
  function retryButton(item, id, label, page) {
    const go = el('button', { type: 'button', class: 'btn btn-sm', id }, label);
    go.addEventListener('click', async () => {
      go.disabled = true;
      error.textContent = '';
      try {
        await api('POST', `/api/ocr/${item.id}/retry`, page ? { page } : {});
      } catch (err) {
        go.disabled = false;
        error.textContent = words(err);
        return;
      }
      kick();
      // документ мог успеть кончиться тем же «готово»: refresh смены не увидел бы — перечитать его самого
      await docs.load();
      await select(item.id);
    });
    return go;
  }

  /** «Сохранить» правки (PATCH) и «Вернуть распознанное» (DELETE): ответ — документ со страницами. */
  async function pageEdit(item, n, method, body, button) {
    button.disabled = true;
    error.textContent = '';
    try {
      const got = await api(method, `/api/ocr/${item.id}/pages/${n}${method === 'DELETE' ? '/edit' : ''}`, body);
      if (editing && editing.id === item.id && editing.n === n) editing = null;
      if (docs.current && docs.current.id === got.id) docs.current = got;
      paintView();
    } catch (err) {
      button.disabled = false;
      error.textContent = words(err);
    }
  }

  function confirmRestore(item, page) {
    const yes = el('button', { type: 'button', class: 'btn btn-accent', id: 'ocr-restore-confirm' }, tr("Вернуть"));
    const dialog = openDialog(root, { id: 'ocr-restore-dialog', title: tr("Вернуть распознанный текст?"), actions: [yes],
      body: [el('p', {}, tr("Правка страницы {0} пропадёт.", page.n))] });
    yes.addEventListener('click', async () => {
      await pageEdit(item, page.n, 'DELETE', undefined, yes);
      if (dialog.open) dialog.close();
    });
  }

  function editNode(page, item) {
    const area = el('textarea', { class: 'tr-edit-text ocr-edit-text', id: 'ocr-edit-text',
      'aria-label': tr("Текст страницы {0}", page.n) });
    area.value = editing.draft;
    area.addEventListener('input', () => { editing.draft = area.value; });
    const save = el('button', { type: 'button', class: 'btn btn-sm btn-accent', id: 'ocr-edit-save' }, tr("Сохранить"));
    const cancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-edit-cancel' }, tr("Отмена"));
    save.addEventListener('click', () => pageEdit(item, page.n, 'PATCH', { text: area.value }, save));
    cancel.addEventListener('click', () => {
      editing = null;
      paintView();
    });
    return el('div', { class: 'ocr-edit' }, area, el('div', { class: 'ocr-page-acts' }, save, cancel));
  }

  /** «Править», «правлено» и «Вернуть распознанное» над текстом страницы кончившегося документа. */
  function toolsNode(item, page) {
    const edit = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-edit' }, tr("Править"));
    edit.addEventListener('click', () => {
      // узел кнопок переживает сохранение правки — текст берётся из документа на момент нажатия
      const now = docs.current.pages.find((p) => p.n === page.n);
      editing = { id: item.id, n: page.n, draft: (now || page).text || '' };
      paintView();
      body.querySelector('#ocr-edit-text')?.focus();
    });
    const box = el('div', { class: 'ocr-page-acts' }, edit);
    if (page.edited) {
      const restore = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-restore' }, tr("Вернуть распознанное"));
      restore.addEventListener('click', () => confirmRestore(docs.current, page));
      box.append(el('span', { class: 'tr-mark', id: 'ocr-edited' }, tr("правлено")), restore);
    }
    return box;
  }

  function textNode(page, item) {
    if (page.state === 'done' || page.state === 'layer') {
      if (editing && editing.id === item.id && editing.n === page.n) return editNode(page, item);
      const box = el('div', { class: 'ocr-text md' });
      box.append(renderMarkdown(page.text || '', { html: true }));
      return box;
    }
    if (page.state === 'blank') return el('p', { class: 'ocr-text muted' }, tr("пустая страница"));
    if (page.state === 'failed') {
      const box = el('div', { class: 'ocr-text' },
        el('p', { class: 'error' }, tr("не распознана: {0}", serverMessage(page.detail) || tr("причина неизвестна"))));
      // документ кончился — эту страницу можно отправить на распознавание ещё раз, остальные остаются
      if (!RUNNING.has(item.state)) box.append(retryButton(item, 'ocr-retry-page', tr("Повторить страницу"), page.n));
      return box;
    }
    return el('p', { class: 'ocr-text muted' }, RUNNING.has(item.state) ? tr("ждёт распознавания") : tr("не распознана"));
  }

  const prev = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-prev', 'aria-label': tr("Предыдущая страница") }, '‹');
  const next = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-next', 'aria-label': tr("Следующая страница") }, '›');
  const pageNo = el('span', { id: 'ocr-page', 'aria-live': 'polite' });
  const pager = el('div', { class: 'ocr-pager' }, prev, pageNo, next);
  prev.addEventListener('click', () => turn(-1));
  next.addEventListener('click', () => turn(1));
  const note = el('p', {});                                    // слова кончившегося не «готово» документа
  const meta = el('p', { class: 'ocr-meta muted small' });
  // «не распознано страниц: 3 из 50» и переход к следующей такой странице листалки
  const failedNo = el('span', {});
  const toFailed = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-next-failed' }, tr("К нераспознанной"));
  const failed = el('p', { class: 'ocr-failed small' }, failedNo, toFailed);
  toFailed.addEventListener('click', () => {
    const pages = docs.current.pages;
    const later = [...pages.slice(at + 1), ...pages.slice(0, at + 1)].find((p) => p.state === 'failed');
    if (!later) return;
    at = pages.indexOf(later);
    paintView();
  });

  /** Картинка и текст страницы: та же страница с теми же данными — те же узлы (выделение, декодированная
   * картинка и фокус переживают опрос); картинка меняется только со своим файлом, кнопки правки над текстом —
   * отдельно от него (появились у кончившегося документа — текст тот же узел). */
  function pageNode(item, page) {
    // идёт ли документ, видно только у нераспознанной страницы («ждёт распознавания», «Повторить страницу»)
    const running = ['waiting', 'failed'].includes(page.state) && RUNNING.has(item.state);
    const key = `${item.id}:${page.n}`;
    const scanKey = JSON.stringify([page.media_id, page.state === 'layer']);
    const mine = !!editing && editing.id === item.id && editing.n === page.n;
    const textKey = JSON.stringify([page.state, page.text, page.detail, running, mine]);
    const tools = !mine && ['done', 'layer'].includes(page.state) && !RUNNING.has(item.state);
    const toolsKey = JSON.stringify([tools, page.edited]);
    if (!two || two.key !== key) {
      two = { key, box: el('div', { class: 'ocr-two', 'data-n': String(page.n) }), cell: el('div', { class: 'ocr-page' }) };
    }
    if (two.scanKey !== scanKey) Object.assign(two, { scanKey, scan: scanNode(page) });
    if (two.textKey !== textKey) Object.assign(two, { textKey, text: textNode(page, item) });
    if (two.toolsKey !== toolsKey) Object.assign(two, { toolsKey, tools: tools ? toolsNode(item, page) : null });
    keep(two.cell, two.tools ? [two.tools, two.text] : [two.text]);
    keep(two.box, [two.scan, two.cell]);
    return two.box;
  }

  function pageView(item) {
    const pages = item.pages;
    if (!pages.length) return [];
    if (shownId !== item.id) {
      shownId = item.id;
      at = 0;
    }
    at = Math.min(at, pages.length - 1);
    const nodes = [];
    if (pages.length > 1) {
      prev.disabled = at === 0 || !!editing;
      next.disabled = at === pages.length - 1 || !!editing;
      // номер страницы исходника; PDF диапазоном — ещё и место в документе
      const { n } = pages[at];
      put(pageNo, n === at + 1 ? tr("страница {0} из {1}", n, pages.length)
        : tr("страница {0} · {1} из {2}", n, at + 1, pages.length));
      nodes.push(pager);
    }
    nodes.push(pageNode(item, pages[at]));
    return nodes;
  }

  function turn(step) {
    at += step;
    paintView();
    const focus = step < 0 ? prev : next;
    if (focus.isConnected && !focus.disabled) focus.focus();
  }

  /** Кнопки документа — заново только при смене того, что в них видно; нажатие берёт документ на момент
   * нажатия (docs.current: опрос его заменяет). */
  function actions(item) {
    const running = RUNNING.has(item.state);
    const choose = engines.length > 1;
    const again = !running && (choose || item.pages.some((p) => p.state === 'waiting' || p.state === 'failed'));
    const key = JSON.stringify([item.id, readable(item), item.state === 'done', again, choose, running]);
    if (acts && acts.key === key) return acts.box;
    const box = el('div', { class: 'tr-acts' });
    if (readable(item)) {
      const copy = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-copy' }, tr("Копировать"));
      copy.addEventListener('click', async () => {
        // Markdown — текстом, разметка с формулами MathML — HTML (обещание — в ClipboardItem сразу, для Safari)
        const text = asMarkdown(docs.current.pages);
        try {
          if (typeof ClipboardItem === 'function' && navigator.clipboard.write) {
            await navigator.clipboard.write([new ClipboardItem({ 'text/plain': new Blob([text], { type: 'text/plain' }),
              'text/html': markdownHtml(text).then((html) => new Blob([html], { type: 'text/html' })) })]);
          } else await navigator.clipboard.writeText(text);
          copy.textContent = tr("Скопировано");
        } catch {
          error.textContent = tr("Буфер обмена недоступен — выделите текст вручную");
        }
      });
      const read = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-read' }, tr("Прочитать"));
      read.addEventListener('click', () => readAloud(asMarkdown(docs.current.pages), { kind: 'ocr', id: docs.current.id }, readBox));
      box.append(copy, read,
        menu('ocr-download', 'ocr-formats', tr("Скачать ▾"), FORMATS.map(([f, label]) => [f, label, 'data-format', download])),
        menu('ocr-chat', 'ocr-chat-menu', tr("В чат ▾"), ASKS.map(([ask, label]) => [ask, label, 'data-ask',
          () => ctx.navigate(`chat?ocr=${encodeURIComponent(item.id)}&ask=${ask}`)])));
      // ссылка «Поделиться» на скан (chat_shares.ITEMS): текст страниц, исходный файл — по выбору
      const share = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-share' }, tr("Поделиться"));
      share.addEventListener('click', () => shareDialog(root, `/api/ocr/${encodeURIComponent(docs.current.id)}/share`,
        docs.current.name, api, { kind: 'scans', source: true }));
      box.append(share);
      if (item.state === 'done') {
        const translate = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-translate' }, tr("Перевести"));
        translate.addEventListener('click', () => translateDialog(docs.current));
        box.append(translate);
      }
    }
    if (again && choose) {
      const go = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-retry' }, tr("Распознать заново"));
      go.addEventListener('click', () => retryDialog(docs.current));
      box.append(go);
    } else if (again) {
      box.append(retryButton(item, 'ocr-retry', tr("Распознать заново")));          // все нераспознанные разом
    }
    const ren = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-rename' }, tr("Переименовать"));
    ren.addEventListener('click', () => rename(docs.current));
    box.append(ren);
    if (!running) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'ocr-delete' }, tr("Удалить"));
      del.addEventListener('click', () => confirmDelete(docs.current));
      box.append(del);
    }
    acts = { key, box };
    return box;
  }

  function paintView() {
    const item = docs.current;
    if (editing && editing.id !== item?.id) editing = null;      // другой документ — черновик правки уходит
    put(title, item ? item.name : NAMES.ocr);
    if (readFor !== item?.id) {
      clearRead(readBox);
      readFor = item?.id;
    }
    if (!item) {
      shownId = null;
      busy = acts = two = null;
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, ctx.hint ? tr("Выберите документ в списке.")
        : tr("Загрузите PDF-скан или фото страниц — студия распознает текст.")));
      return;
    }
    const nodes = [];
    busy = RUNNING.has(item.state) ? busyBlock(busy, item, { id: 'ocr-cancel', url: '/api/ocr',
      fail: (err) => { error.textContent = words(err); }, after: refresh }) : null;
    if (busy) {
      nodes.push(busy.box);
    } else if (item.state !== 'done') {
      note.className = item.state === 'error' ? 'error' : 'muted';
      put(note, serverMessage(item.detail));
      nodes.push(note);
    }
    const n = item.pages.length;
    const span = n && item.pages[0].n !== 1 ? ` · ${tr("стр. {0}–{1}", item.pages[0].n, item.pages[n - 1].n)}` : '';
    put(meta, `${n} ${plural(n, [tr("страница"), tr("страницы"), tr("страниц")])}${span} · ${when(item.created)} · ${engineName(item)}`);
    nodes.push(meta);
    const missed = item.pages.filter((p) => p.state === 'failed').length;
    if (missed) {
      put(failedNo, tr("не распознано страниц: {0} из {1}", missed, n));
      toFailed.hidden = n < 2;
      toFailed.disabled = !!editing;
      nodes.push(failed);
    }
    nodes.push(actions(item), readBox, ...pageView(item));
    keep(body, nodes);
  }

  /** Выбор модели — только когда есть из чего выбрать и скан идёт своим распознаванием: при инструменте MCP он
   * ни на что не влияет. */
  function paintEngine() {
    enginePick.wrap.hidden = engines.length < 2 || !!mcp.tool;
    enginePick.sync();
    // ряд с выбором виден и на компьютере: «Загрузить скан» тогда в нём, а не над списком
    add.hidden = !mcp.control.hidden || !enginePick.wrap.hidden || !!ctx.hint;
  }

  /* загрузка */
  function paintMcp() {
    const active = !!mcp.tool;
    paintEngine();
    upload.hidden = !!ctx.hint && !active;
    if (nativeHint && active) {
      nativeHint.hidden = true;
      if (!upload.parentNode) {
        aside.prepend(add, progress);
        toolbar.append(upload);
      }
    }
    input.multiple = !active;
    input.accept = active ? 'image/png,image/jpeg,image/webp' : ACCEPT;
  }

  function paintBusy() {
    input.disabled = upload.disabled = uploading || mcp.busy;
  }

  /** PDF длиннее предела: «страницы 1–50 из 187» с правкой диапазона, затем тот же файл заново с ним. */
  function rangeDialog(files, total, max) {
    const number = (id, value) => el('input', { type: 'number', class: 'input', id, min: '1', max: String(total),
      step: '1', inputmode: 'numeric', value: String(value) });
    const first = number('ocr-range-first', 1);
    const last = number('ocr-range-last', Math.min(total, max));
    const summary = el('p', { class: 'muted small', id: 'ocr-range-summary', 'aria-live': 'polite' });
    const go = el('button', { type: 'button', class: 'btn btn-accent', id: 'ocr-range-confirm' }, tr("Распознать"));
    const dialog = openDialog(root, { id: 'ocr-range-dialog', title: tr("Какие страницы распознать?"), actions: [go],
      body: [el('p', {}, tr("Страниц в документе: {0}, за раз распознаётся до {1}.", total, max)),
        el('div', { class: 'ocr-range' }, field(tr("С страницы"), first, 'first'), field(tr("по страницу"), last, 'last')),
        summary] });
    const range = () => [Number(first.value), Number(last.value)];
    const paint = () => {
      const [a, b] = range();
      const ok = Number.isInteger(a) && Number.isInteger(b) && a >= 1 && a <= b && b <= total && b - a + 1 <= max;
      go.disabled = !ok;
      summary.textContent = ok ? tr("страницы {0}–{1} из {2}", a, b, total)
        : tr("Номера — от 1 до {0}, не больше {1} страниц за раз", total, max);
    };
    first.addEventListener('input', paint);
    last.addEventListener('input', paint);
    paint();
    go.addEventListener('click', () => {
      const [a, b] = range();
      dialog.close();
      send(files, { first: a, last: b });
    });
  }

  async function send(chosen, range = null) {
    if (chosen.length > FILES_MAX) {
      error.textContent = tr("Больше {0} файлов за раз", FILES_MAX);
      return;
    }
    error.textContent = '';
    uploading = true;
    paintBusy();
    const files = [];
    for (const file of chosen) {                 // по одному: 50 декодированных снимков разом телефону не по силам
      if (file.type !== 'application/pdf') progress.textContent = tr("Уменьшаю фото · {0} из {1}", files.length + 1, chosen.length);
      files.push(await resizePhoto(file, DOCUMENT_PHOTO_SIDE, true));
    }
    progress.textContent = '';
    if (files.reduce((sum, f) => sum + f.size, 0) > UPLOAD_MAX) {
      uploading = false;
      paintBusy();
      error.textContent = files.length === 1 ? tr("Файл больше 20 МиБ") : tr("Файлы вместе больше 20 МиБ");
      return;
    }
    const form = new FormData();
    for (const file of files) form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/ocr');
    holdRefresh(xhr);                // автоматическая перезагрузка загрузку не оборвёт (refresh.js)
    xhr.setRequestHeader('x-studio-csrf', me.csrf);
    const pick = engines.length > 1 ? engines[engine.selectedIndex] : null;
    if (pick) {
      xhr.setRequestHeader('x-studio-connection', String(pick.connection_id));
      xhr.setRequestHeader('x-studio-model', pick.model);
    }
    if (range) {
      xhr.setRequestHeader('x-studio-first-page', String(range.first));
      xhr.setRequestHeader('x-studio-last-page', String(range.last));
    }
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) progress.textContent = tr("Загружаю · {0}%", Math.round(event.loaded * 100 / event.total));
    });
    xhr.addEventListener('loadend', async () => {
      uploading = false;
      paintBusy();
      progress.textContent = '';
      let answer = null;
      try {
        answer = JSON.parse(xhr.responseText);
      } catch {
        // не JSON — ниже словами по коду
      }
      if (xhr.status === 202 && answer) {
        kick();
        picked = true;
        await docs.load();
        await select(answer.id);
        return;
      }
      if (xhr.status === 401) toLogin();
      const detail = answer && answer.detail;
      if (xhr.status === 423 && detail && detail.code === 'screen_locked' && hooks.locked) {
        await hooks.locked();          // студия заперта (lock.js): те же файлы — после отпирания
        send(files, range);
        return;
      }
      if (xhr.status === 422 && detail && detail.code === 'pages' && detail.pages && !range) {
        rangeDialog(files, detail.pages, detail.max);      // PDF длиннее предела — выбрать страницы
        return;
      }
      error.textContent = words((detail && detail.message) || (xhr.status ? tr("Ошибка сервера ({0})", xhr.status) : tr("Нет связи с сервером")));
    });
    xhr.send(form);
  }

  input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    if (!files.length || input.disabled) return;
    error.textContent = '';
    if (!mcp.tool) send(files);
    else if (files.length === 1) mcp.run({}, { files: { artifact_id: files[0] } });
    else error.textContent = tr("Для MCP выберите изображение одной страницы");
  });
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  /** ?id адреса («Открыть» в «В работе», «Поиск») — при сборке и при переходе внутри раздела (update). */
  function follow(wanted, linked = true) {
    if (wanted && linked) {
      history.replaceState(history.state, '', '#/ocr');
      picked = false;
    }
    // пока шёл список, документ могли выбрать сами (загрузка) — его не перебивать
    docs.load().then(() => { if (wanted && !picked) select(wanted, true); });
  }
  paintView();
  loadEngines();
  const linked = ctx.query && ctx.query.get('id');
  follow(linked || selection.recall(), !!linked);

  return {
    follow,
    destroy() {
      mcp.destroy();
      clearRead(readBox);
      docs.stop();
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

/** Переход внутри открытого раздела (#/ocr?id=…): только ?id. */
export function update(ctx) {
  const wanted = ctx.query && ctx.query.get('id');
  if (screen && wanted) screen.follow(wanted);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
