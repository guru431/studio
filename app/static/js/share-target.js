import { tr } from './i18n.js';
/* Принятое системным листом «Поделиться» установленной студии (share_target манифеста). POST /receive до сервера не
 * идёт: sw.js кладёт файлы, текст и ссылку в кэш устройства RECEIVED и открывает `/`. Оболочка после первого раздела
 * (app.js enter — и после входа: кэш переживает /login) спрашивает, куда отправить, по типу принятого: запись —
 * «Расшифровка», фото — «Сканы», «Фото» или «Чат», PDF — «Сканы», «Перевод» или «Чат», другой документ — «Перевод» или
 * «Чат», текст и ссылка — «Чат» или «Перевод». Выбор кладёт принятое в поле раздела, как выбор файла или набор текста:
 * дальше — обычный путь раздела. Принятое недоверенное: POST /receive работнику может прислать и чужой сайт (форма с
 * `form.submit()`), а надёжного признака отправителя у работника нет (`Sec-Fetch-Site` ему не виден, referrer пуст и у
 * системного листа). Поэтому окно говорит, откуда это, а «Расшифровка» и «Сканы», где выбор файла сразу начинает
 * загрузку, спрашивают ещё раз своей кнопкой (UPLOAD). Принятое уходит из кэша, как только окно закрыто (выбором или «Не
 * отправлять»), и не живёт дольше RECEIVED_MS. Показанное хоть раз помечается `offered`: выход, аварийный выход, конец
 * сессии и страница входа (login.js) его стирают — следующей учётке устройства оно не достаётся; принятое без сессии
 * (ещё не показанное) ждёт входа. На сервер — только запросом выбранного раздела. */
import { button, el, openDialog } from './ui.js';
import { sectionName } from './sections-meta.js';

const RECEIVED = 'studio-received';             // то же имя — в sw.js
const RECEIVED_MS = 3600 * 1000;
/** Поле файлов раздела (у «Перевода» — одно поле на один документ). */
const FILES = { transcribe: '#transcribe-file', ocr: '#ocr-file', photo: '#photo-file', chat: '#attach',
  translate: '#tl-file-input' };
/** Поле текста раздела. */
const TEXT = { chat: '#chat-input', translate: '#tl-input' };
/** Разделы, где поле файла берёт один файл. */
const SINGLE = new Set(['photo', 'translate']);
/** Разделы, где выбор файла сразу загружает его: кнопка подтверждения — как кнопка загрузки раздела. */
const UPLOAD = { transcribe: () => tr("Загрузить запись"), ocr: () => tr("Загрузить скан") };
const UNTRUSTED = () => tr("Получено из другого приложения или сайта. Отправляйте, только если делились этим сами.");

/** Разделы для одного файла по типу. */
function targetsOf(file) {
  const type = file.type || '';
  if (/^(audio|video)\//.test(type)) return ['transcribe'];
  if (/^image\//.test(type)) return ['ocr', 'photo', 'chat'];
  if (type === 'application/pdf' || /\.pdf$/i.test(file.name)) return ['ocr', 'translate', 'chat'];
  return ['translate', 'chat'];
}

/** Разделы для всего принятого: общие для всех файлов; без файлов — по тексту. */
export function targets(files, text) {
  if (!files.length) return text ? ['chat', 'translate'] : [];
  return files.map(targetsOf).reduce((all, one) => all.filter((id) => one.includes(id)))
    .filter((id) => files.length === 1 || !SINGLE.has(id));
}

/** Текст листа: заголовок, текст и ссылка без повторов (ссылку браузер часто кладёт и в текст). */
export function sharedText({ title = '', text = '', url = '' }) {
  const parts = [title.trim(), text.trim()];
  if (url.trim() && !text.includes(url.trim())) parts.push(url.trim());
  return [...new Set(parts.filter(Boolean))].join('\n');
}

/** Принятое из кэша: { files: [File], text } или null (нет, устарело, кэш закрыт). Устаревшее — стирается. */
async function take() {
  if (!('caches' in window)) return null;
  try {
    if (!(await caches.has(RECEIVED))) return null;
    const cache = await caches.open(RECEIVED);
    const hit = await cache.match('/received/meta');
    const meta = hit ? await hit.json() : null;
    if (!meta || !(Date.now() - meta.at < RECEIVED_MS)) {
      await caches.delete(RECEIVED);
      return null;
    }
    const files = [];
    for (const [i, info] of (meta.files || []).entries()) {
      const body = await cache.match(`/received/${i}`);
      if (body) files.push(new File([await body.blob()], info.name || 'file', { type: info.type || '' }));
    }
    const text = sharedText(meta);
    if (files.length || text) {
      if (!meta.offered) await cache.put('/received/meta', new Response(JSON.stringify({ ...meta, offered: true })));
      return { files, text };
    }
    await caches.delete(RECEIVED);
    return null;
  } catch {
    return null;
  }
}

const sectionOf = () => (/^#\/([a-z]+)/.exec(location.hash) || [])[1];

/** Поле раздела, когда раздел открыт и показан (app.js снимает inert с #view); ушли в другой раздел — null. */
function fieldOf(section, selector) {
  const view = document.getElementById('view');
  return new Promise((resolve) => {
    const check = () => {
      if (sectionOf() !== section) return done(null);
      const field = !view.inert && view.querySelector(selector);
      if (field) done(field);
    };
    const observer = new MutationObserver(check);
    function done(value) {
      observer.disconnect();
      window.removeEventListener('hashchange', check);
      resolve(value);
    }
    observer.observe(view, { childList: true, subtree: true, attributes: true, attributeFilter: ['inert'] });
    window.addEventListener('hashchange', check);
    check();
  });
}

/** Явная кнопка загрузки принятого в разделе, где выбор файла сразу начинает загрузку: true — нажата. */
function confirmUpload(section, files) {
  return new Promise((resolve) => {
    let yes = false;
    const go = button(UPLOAD[section](), { class: 'btn btn-accent', id: 'received-upload' });
    const dialog = openDialog(document.body, { id: 'received-confirm', title: sectionName(section), actions: [go],
      cancelText: tr("Не отправлять"), body: [el('p', { class: 'received-what' }, files.map((f) => f.name).join(', ')),
        el('p', { class: 'muted small' }, UNTRUSTED())] });
    go.addEventListener('click', () => {
      yes = true;
      dialog.close();
    });
    dialog.addEventListener('close', () => resolve(yes));
  });
}

/** Стереть принятое; onlyOffered — только уже показанное какой-то сессии (принятое без сессии ждёт входа). */
async function forgetReceived(onlyOffered) {
  if (!('caches' in window)) return;
  try {
    const hit = onlyOffered && await caches.match('/received/meta', { cacheName: RECEIVED });
    if (!onlyOffered || (hit && (await hit.json()).offered)) await caches.delete(RECEIVED);
  } catch {
    // кэш закрыт — стирать нечего
  }
}

// явный выход — всё, до POST /logout; аварийный выход и конец сессии (api.js toLogin) — только показанное: конец
// сессии в другой вкладке не должен съесть то, что лист только что принёс без сессии (login.js — то же правило)
document.addEventListener('submit', (event) => {
  if (event.target.matches('form[action="/logout"]')) forgetReceived(false);
});
document.addEventListener('studio-signed-out', () => forgetReceived(true));

/** В поле раздела: файлы — как выбор файла (change), текст — как набор (input), к уже набранному. */
async function deliver(section, { files, text }) {
  if (sectionOf() !== section || location.hash.includes('/', 2)) {
    const moved = new Promise((resolve) => window.addEventListener('hashchange', resolve, { once: true }));
    location.hash = `#/${section}`;
    await moved;
  }
  if (files.length) {
    const input = await fieldOf(section, FILES[section]);
    if (!input || (UPLOAD[section] && !(await confirmUpload(section, files) && input.isConnected))) return;
    const list = new DataTransfer();
    for (const file of files) list.items.add(file);
    input.files = list.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  if (text && TEXT[section] && (!files.length || section === 'chat')) {
    const area = await fieldOf(section, TEXT[section]);
    if (!area) return;
    area.value = area.value ? `${area.value}\n${text}` : text;
    area.dispatchEvent(new Event('input', { bubbles: true }));
    area.focus();
  }
}

/** Оболочка зовёт после первого раздела: есть принятое — окно выбора раздела. */
export async function offerReceived() {
  const got = await take();
  if (!got) return;
  const ids = targets(got.files, got.text);
  const what = got.files.length ? got.files.map((f) => f.name).join(', ') : got.text;
  const choices = ids.map((id) => button(sectionName(id), { class: 'btn btn-accent', 'data-section': id }));
  const body = [el('p', { class: 'received-what' }, what), el('p', { class: 'muted small' }, UNTRUSTED())];
  if (!ids.length) body.push(el('p', { class: 'muted' }, tr("Эти файлы вместе не принимает ни один раздел. Поделитесь ими по отдельности.")));
  let chosen = null;
  const dialog = openDialog(document.body, { id: 'received', title: tr("Куда отправить?"), body, actions: choices,
    cancelText: tr("Не отправлять") });
  for (const choice of choices) {
    choice.addEventListener('click', () => {
      chosen = choice.dataset.section;
      dialog.close();
    });
  }
  dialog.addEventListener('close', () => {
    caches.delete(RECEIVED).catch(() => {});
    if (chosen) deliver(chosen, got);
  });
}
