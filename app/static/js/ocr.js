import { message as serverMessage, tr } from './i18n.js';
/* Раздел «Сканы» (#/ocr, распознавание) — раскладка Б спеки 2026-10-27-studio-ocr-design (раздел 6.4).
 *
 * Слева — «+ Загрузить скан» и документы по дням (имя, «N стр.», состояние словами), справа — выбранный:
 * «N страниц · дата», «Копировать», «Скачать ▾» (txt, md, docx), «В чат ▾», «Удалить»; идущий — состояние
 * и «Отменить». Ниже — листалка «‹ страница N из M ›» и две колонки: картинка страницы (нажатие — окно
 * просмотра #ocr-viewer) и её текст. На телефоне (≤ 760 px) картинка над текстом, список выезжает сбоку,
 * как у «Расшифровки». Готовые страницы идущего документа уже листаются.
 *
 * Загрузка — один PDF или несколько фото одним выбором (один документ, страницы в порядке выбора):
 * XMLHttpRequest с FormData (части `file`, ход в процентах), 423 запертой студии (XHR идёт мимо api.js)
 * ждёт отпирания и шлёт те же файлы заново. Список — createPicker (feed.js): опрос идущих раз в POLL_MS,
 * «Показать ещё», выбор по номеру. Раздел без модели (ctx.hint): документы читаются, скачиваются и
 * удаляются, вместо загрузки — подсказка.
 *
 * Текст страницы — Markdown модели через renderMarkdown(…, { html: true }) (marked + DOMPurify: таблицы
 * HunyuanOCR — HTML-тегами, скрипты — нет); прочий текст сервера — только textContent. «Копировать» — весь
 * документ, как выгрузка .md. «В чат ▾» → #/chat?ocr=<id>&ask=discuss|summary|translate.
 * #/ocr?id=<id> («Открыть» в «В работе», «Поиск») выбирает документ после загрузки списка, если его не
 * выбрали сами; ?id из адреса уходит. Принятый документ зовёт jobs.kick(). */
import { api, hooks, me, toLogin } from './api.js';
import { icon } from './icons.js';
import { actionMenu } from './menu.js';
import { NAMES } from './sections-meta.js';
import { dateTime } from './time.js';
import { busyBlock, createPicker, keep, pickerList } from './feed.js';
import { kick } from './jobs.js';
import { renderMarkdown } from './markdown.js';
import { el, openDialog, plural, words } from './ui.js';

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

function media(id) {
  return `/api/media/${encodeURIComponent(id)}`;
}

/** Текст страницы без распознанного — как в выгрузке (exports._page_body). */
function pageBody(p) {
  if (p.state === 'done' || p.state === 'layer') return p.text || '';
  if (p.state === 'blank') return tr("[пустая страница]");
  return serverMessage(p.detail) ? tr("[не распознана: {0}]", serverMessage(p.detail)) : tr("[не распознана]");
}

/** Весь документ — как выгрузка .md (exports.pages_md). */
export function asMarkdown(pages) {
  return `${pages.map((p) => tr("## Страница {0}\n\n{1}", p.n, pageBody(p))).join('\n\n')}\n`;
}

function build(view, ctx) {
  let picked = false;          // документ выбрали сами (список, загрузка): ?id из адреса его не перебивает
  let shownId = null;          // документ, чья страница сейчас в листалке
  let at = 0;                  // номер показанной страницы (с 0)
  let busy = null;             // блок «идёт» выбранного документа (feed.busyBlock): опрос меняет в нём только текст
  const docs = createPicker({ url: '/api/ocr', size: PAGE, pollMs: POLL_MS,
    running: (i) => RUNNING.has(i.state), paint: paintList,
    show() {
      error.textContent = '';
      paintList();
      paintView();
    },
    fail(err) { error.textContent = words(err); } });
  const { select, refresh } = docs;

  const input = el('input', { type: 'file', class: 'sr', accept: ACCEPT, multiple: true });
  const add = el('label', { class: 'btn btn-accent tr-add', id: 'ocr-add' }, icon('plus'), tr("Загрузить скан"), input);
  const progress = el('p', { id: 'ocr-progress', class: 'muted small', 'aria-live': 'polite' });
  const list = el('nav', { class: 'tr-list', 'aria-label': tr("Документы") });
  const aside = el('aside', { class: 'split-aside', id: 'ocr-aside' }, ...(ctx.hint ? [] : [add, progress]), list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'btn btn-sm split-toggle', 'aria-label': tr("Документы"),
    'aria-controls': 'ocr-aside', 'aria-expanded': 'false' }, icon('list'), tr("Документы"));
  const upload = el('button', { type: 'button', class: 'btn btn-accent btn-sm', id: 'ocr-upload' }, icon('plus'), tr("Загрузить скан"));
  upload.addEventListener('click', () => input.click());
  const toolbar = el('div', { class: 'split-toolbar' }, toggle, ...(ctx.hint ? [] : [upload]));
  const title = el('h1', {}, NAMES.ocr);
  const head = el('header', { class: 'head' }, title);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'ocr-view' });
  const main = el('section', { class: 'split-main' }, head, toolbar, ctx.hint ? ctx.hint('hint-bar') : null, error, body);
  const root = el('div', { class: 'split ocr' }, aside, backdrop, main);
  view.append(root);

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список */
  const paintRows = pickerList(list, { cls: 'ocr-item', moreId: 'ocr-more', empty: tr("Здесь появятся ваши документы"),
    name: (item) => item.name,
    state: (item) => [tr("{0} стр.", item.pages), serverMessage(item.detail)].filter(Boolean).join(' · '),
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
    el('a', { href: `/api/ocr/${docs.current.id}/export?format=${format}`, download: '' }).click();
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'ocr-delete-confirm' }, tr("Удалить"));
    const dialog = openDialog(root, { id: 'ocr-delete-dialog', title: tr("Удалить документ?"), actions: [yes],
      body: [el('p', {}, tr("Удалятся текст, картинки страниц и исходный файл.")), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/ocr/${item.id}`);
        dialog.close();
        docs.forget(item.id);
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

  function textNode(page, item) {
    if (page.state === 'done' || page.state === 'layer') {
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

  function pageView(item) {
    const pages = item.pages;
    if (!pages.length) return [];
    if (shownId !== item.id) {
      shownId = item.id;
      at = 0;
    }
    at = Math.min(at, pages.length - 1);
    const page = pages[at];
    const nodes = [];
    if (pages.length > 1) {
      const prev = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-prev', 'aria-label': tr("Предыдущая страница"),
        disabled: at === 0 }, '‹');
      const next = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-next', 'aria-label': tr("Следующая страница"),
        disabled: at === pages.length - 1 }, '›');
      prev.addEventListener('click', () => turn(-1));
      next.addEventListener('click', () => turn(1));
      nodes.push(el('div', { class: 'ocr-pager' }, prev,
        el('span', { id: 'ocr-page', 'aria-live': 'polite' }, tr("страница {0} из {1}", at + 1, pages.length)), next));
    }
    nodes.push(el('div', { class: 'ocr-two', 'data-n': String(page.n) }, scanNode(page), textNode(page, item)));
    return nodes;
  }

  function turn(step) {
    at += step;
    paintView();
    const focus = body.querySelector(step < 0 ? '#ocr-prev' : '#ocr-next');
    if (focus && !focus.disabled) focus.focus();
  }

  function paintView() {
    const item = docs.current;
    title.textContent = item ? item.name : NAMES.ocr;
    if (!item) {
      shownId = null;
      busy = null;
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
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, serverMessage(item.detail)));
    }
    const n = item.pages.length;
    nodes.push(el('p', { class: 'ocr-meta muted small' },
      `${n} ${plural(n, [tr("страница"), tr("страницы"), tr("страниц")])} · ${when(item.created)}`));
    const acts = el('div', { class: 'tr-acts' });
    if (item.state === 'done') {
      const copy = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-copy' }, tr("Копировать"));
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(asMarkdown(item.pages));
          copy.textContent = tr("Скопировано");
        } catch {
          error.textContent = tr("Буфер обмена недоступен — выделите текст вручную");
        }
      });
      acts.append(copy,
        menu('ocr-download', 'ocr-formats', tr("Скачать ▾"), FORMATS.map(([f, label]) => [f, label, 'data-format', download])),
        menu('ocr-chat', 'ocr-chat-menu', tr("В чат ▾"), ASKS.map(([ask, label]) => [ask, label, 'data-ask',
          () => ctx.navigate(`chat?ocr=${encodeURIComponent(item.id)}&ask=${ask}`)])));
    }
    if (item.state !== 'done' && !RUNNING.has(item.state)
        && item.pages.some((p) => p.state === 'waiting' || p.state === 'failed')) {
      acts.append(retryButton(item, 'ocr-retry', tr("Распознать заново")));     // ошибка или отмена документа
    }
    if (!RUNNING.has(item.state)) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'ocr-delete' }, tr("Удалить"));
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    if (acts.children.length) nodes.push(acts);
    nodes.push(...pageView(item));
    keep(body, nodes);
  }

  /* загрузка */
  function send(files) {
    if (files.length > FILES_MAX) {
      error.textContent = tr("Больше {0} файлов за раз", FILES_MAX);
      return;
    }
    if (files.reduce((sum, f) => sum + f.size, 0) > UPLOAD_MAX) {
      error.textContent = tr("Файлы вместе больше 20 МиБ");
      return;
    }
    error.textContent = '';
    input.disabled = true;
    upload.disabled = true;
    const form = new FormData();
    for (const file of files) form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/ocr');
    xhr.setRequestHeader('x-studio-csrf', me.csrf);
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) progress.textContent = tr("Загружаю · {0}%", Math.round(event.loaded * 100 / event.total));
    });
    xhr.addEventListener('loadend', async () => {
      input.disabled = false;
      upload.disabled = false;
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
        send(files);
        return;
      }
      error.textContent = words((detail && detail.message) || (xhr.status ? tr("Ошибка сервера ({0})", xhr.status) : tr("Нет связи с сервером")));
    });
    xhr.send(form);
  }

  input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    if (files.length) send(files);
  });
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  /** ?id адреса («Открыть» в «В работе», «Поиск») — при сборке и при переходе внутри раздела (update). */
  function follow(wanted) {
    if (wanted) {
      history.replaceState(history.state, '', '#/ocr');
      picked = false;
    }
    // пока шёл список, документ могли выбрать сами (загрузка) — его не перебивать
    docs.load().then(() => { if (wanted && !picked) select(wanted, true); });
  }
  paintView();
  follow(ctx.query && ctx.query.get('id'));

  return {
    follow,
    destroy() {
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
