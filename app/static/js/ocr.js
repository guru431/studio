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
 * HunyuanOCR — HTML-тегами, скрипты — нет); прочий текст сервера — только textContent. «Копировать» — весь
 * документ, как выгрузка .md. «В чат ▾» → #/chat?ocr=<id>&ask=discuss|summary|translate.
 * #/ocr?id=<id> («Открыть» в «В работе», «Поиск») выбирает документ после загрузки списка, если его не
 * выбрали сами; ?id из адреса уходит. Принятый документ зовёт jobs.kick(). */
import { api, hooks, me, toLogin } from './api.js';
import { holdRefresh } from './refresh.js';
import { icon } from './icons.js';
import { actionMenu } from './menu.js';
import { NAMES } from './sections-meta.js';
import { dateTime } from './time.js';
import { busyBlock, createPicker, keep, pickerList } from './feed.js';
import { kick } from './jobs.js';
import { sectionMcp } from './mcp-sections.js';
import { renderMarkdown } from './markdown.js';
import { modelLabel } from './settings.js';
import { picker } from './picker.js';
import { el, field, openDialog, plural, renameDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const UPLOAD_MAX = 20 * 1024 * 1024;     // documents.DOC_MAX — на всю загрузку
const FILES_MAX = 50;                    // ocr_render.PAGES_MAX
const PHOTO_SIDE = 2200;                 // ocr_render.MAX_SIDE: длиннее фото уменьшает до отправки shrink
const PHOTO_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const ACCEPT = 'application/pdf,.pdf,image/png,image/jpeg,image/webp';
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const FORMATS = [['txt', tr("Текст .txt")], ['md', 'Markdown .md'], ['docx', 'Word .docx']];
const ASKS = [['discuss', tr("Обсудить в чате")], ['summary', tr("Кратко пересказать")], ['translate', tr("Перевести")]];

let screen = null;

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Фото длиннее PHOTO_SIDE — JPEG с этой стороной до отправки: сервер всё равно ужимает до неё
 * (ocr_render.photo_png), а снимок телефона в 3–8 МБ съел бы предел UPLOAD_MAX за несколько страниц. Рисует <img>:
 * браузер сам поворачивает по EXIF (image-orientation: from-image), в JPEG метаданных нет. Не вышло меньше
 * исходного, не PNG/JPEG/WebP или не декодируется — исходный файл (откажет и объяснит сервер). */
async function shrink(file) {
  if (!PHOTO_TYPES.has(file.type)) return file;
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const side = Math.max(img.naturalWidth, img.naturalHeight);
    if (side <= PHOTO_SIDE) return file;
    const canvas = el('canvas', { width: Math.round(img.naturalWidth * PHOTO_SIDE / side),
      height: Math.round(img.naturalHeight * PHOTO_SIDE / side) });
    const g = canvas.getContext('2d');
    g.fillStyle = '#fff';                        // прозрачное PNG — на белом, а не на чёрном
    g.fillRect(0, 0, canvas.width, canvas.height);
    g.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((done) => { canvas.toBlob(done, 'image/jpeg', 0.9); });
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], `${file.name.replace(/\.[^.]*$/, '') || file.name}.jpg`, { type: 'image/jpeg' });
  } catch {
    return file;
  } finally {
    URL.revokeObjectURL(url);
  }
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
  let at = 0;                  // номер показанной страницы (с 0)
  let busy = null;             // блок «идёт» выбранного документа (feed.busyBlock): опрос меняет в нём только текст
  // вид выбранного переживает опрос (05:F14): узлы — по ключу своих данных, иначе меняются только тексты
  let acts = null;             // { key, box } — кнопки документа
  let two = null;              // { key, box, scan, text } — показанная страница: картинка и текст
  let engines = [];            // движки раздела через сервер: { connection_id, model, label } (GET /api/sections)
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
   * картинка и фокус переживают опрос); картинка меняется только со своим файлом. */
  function pageNode(item, page) {
    // идёт ли документ, видно только у нераспознанной страницы («ждёт распознавания», «Повторить страницу»)
    const running = ['waiting', 'failed'].includes(page.state) && RUNNING.has(item.state);
    const key = `${item.id}:${page.n}`;
    const scanKey = JSON.stringify([page.media_id, page.state === 'layer']);
    const textKey = JSON.stringify([page.state, page.text, page.detail, running]);
    if (!two || two.key !== key) two = { key, box: el('div', { class: 'ocr-two', 'data-n': String(page.n) }) };
    if (two.scanKey !== scanKey) Object.assign(two, { scanKey, scan: scanNode(page) });
    if (two.textKey !== textKey) Object.assign(two, { textKey, text: textNode(page, item) });
    keep(two.box, [two.scan, two.text]);
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
      prev.disabled = at === 0;
      next.disabled = at === pages.length - 1;
      put(pageNo, tr("страница {0} из {1}", at + 1, pages.length));
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
    const key = JSON.stringify([item.id, readable(item), again, choose, running]);
    if (acts && acts.key === key) return acts.box;
    const box = el('div', { class: 'tr-acts' });
    if (readable(item)) {
      const copy = el('button', { type: 'button', class: 'btn btn-sm', id: 'ocr-copy' }, tr("Копировать"));
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(asMarkdown(docs.current.pages));
          copy.textContent = tr("Скопировано");
        } catch {
          error.textContent = tr("Буфер обмена недоступен — выделите текст вручную");
        }
      });
      box.append(copy,
        menu('ocr-download', 'ocr-formats', tr("Скачать ▾"), FORMATS.map(([f, label]) => [f, label, 'data-format', download])),
        menu('ocr-chat', 'ocr-chat-menu', tr("В чат ▾"), ASKS.map(([ask, label]) => [ask, label, 'data-ask',
          () => ctx.navigate(`chat?ocr=${encodeURIComponent(item.id)}&ask=${ask}`)])));
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
    put(title, item ? item.name : NAMES.ocr);
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
    put(meta, `${n} ${plural(n, [tr("страница"), tr("страницы"), tr("страниц")])} · ${when(item.created)} · ${engineName(item)}`);
    nodes.push(meta);
    const missed = item.pages.filter((p) => p.state === 'failed').length;
    if (missed) {
      put(failedNo, tr("не распознано страниц: {0} из {1}", missed, n));
      toFailed.hidden = n < 2;
      nodes.push(failed);
    }
    nodes.push(actions(item), ...pageView(item));
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

  async function send(chosen) {
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
      files.push(await shrink(file));
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
    if (!files.length || input.disabled) return;
    error.textContent = '';
    if (!mcp.tool) send(files);
    else if (files.length === 1) mcp.run({}, { files: { artifact_id: files[0] } });
    else error.textContent = tr("Для MCP выберите изображение одной страницы");
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
  loadEngines();
  follow(ctx.query && ctx.query.get('id'));

  return {
    follow,
    destroy() {
      mcp.destroy();
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
