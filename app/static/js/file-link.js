import { locale, message as serverMessage, tr } from './i18n.js';
/* Анонимная страница личной ссылки на файл (`/f#<token>`, my_files.py). Токен — только во фрагменте адреса: на сервер
 * он уходит телом POST /f/check (имя, размер, срок) и POST /f/open (cookie `studio_file` на 600 с только для
 * /f/file); скачивание — переход на /f/file, ответ всегда вложением. Все отказы сервера — одинаковое «ссылка
 * недоступна» (404), частые неудачи — пауза (429). */

const info = document.getElementById('file-info');
const name = document.getElementById('file-name');
const facts = document.getElementById('file-facts');
const error = document.getElementById('file-error');
const download = document.getElementById('file-download');
const status = document.getElementById('file-status');
let token = '';

async function request(path) {
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }), signal: AbortSignal.timeout(30000) });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { /* общая ошибка ниже */ }
    throw new Error(serverMessage(detail?.message) || tr("Ошибка сервера ({0})", response.status));
  }
  return response.status === 204 ? null : response.json();
}

/* как settings.js sizeText: страница анонимная и модуль настроек не грузит */
function sizeText(bytes) {
  const units = [tr("Б"), tr("КиБ"), tr("МиБ"), tr("ГиБ"), tr("ТиБ")];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit ? value.toLocaleString(locale(), { maximumFractionDigits: 1 }) : value} ${units[unit]}`;
}

function fail(text) {
  info.hidden = true;
  download.hidden = true;
  status.textContent = '';
  error.textContent = text;
}

async function check() {
  token = location.hash.slice(1);
  error.textContent = '';
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    fail(tr("Ссылка недействительна"));
    return;
  }
  try {
    const found = await request('/f/check');
    name.textContent = found.name;
    const until = new Date(found.expires * 1000).toLocaleString(locale(),
      { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
    facts.textContent = `${sizeText(found.size)} · ${tr("действует до {0}", until)}`;
    document.title = `${found.name} — ${tr("Студия")}`;
    info.hidden = false;
    download.hidden = false;
  } catch (err) {
    fail(err.message);
  }
}

download.addEventListener('click', async () => {
  download.disabled = true;
  status.textContent = '';
  try {
    await request('/f/open');
    const link = document.createElement('a');
    link.href = '/f/file';
    link.download = name.textContent;
    link.rel = 'noopener';
    document.body.append(link);
    link.click();
    link.remove();
    status.textContent = tr("Скачивание началось");
  } catch (err) {
    fail(err.message);
  } finally {
    download.disabled = false;
  }
});

window.addEventListener('hashchange', check);
check();
