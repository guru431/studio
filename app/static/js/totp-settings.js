import { tr } from './i18n.js';
import { api, me } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';
/* «Настройки → Защита → Приложение-аутентификатор» (totp.py): подключение — пароль, QR (`otpauth://`, рисует
 * vendor/qrcode.js в canvas) и секрет текстом, проверочный код; резервные коды показываются один раз.
 * Отключение — паролем. `me.totp`: on | off | reenter (ключ сервера сменился — подключить заново). */

let qrLibrary = null;

function loadQr() {
  qrLibrary ??= new Promise((resolve, reject) => {
    if (window.qrcode) { resolve(window.qrcode); return; }
    const script = document.createElement('script');
    script.src = new URL('../vendor/qrcode.js', import.meta.url).href;
    script.onload = () => (window.qrcode ? resolve(window.qrcode) : reject(new Error('QR library unavailable')));
    script.onerror = () => { qrLibrary = null; reject(new Error('QR library unavailable')); };
    document.head.append(script);
  });
  return qrLibrary;
}

/** QR адреса в canvas: тёмные модули на белом с полем в 4 модуля — в любой теме, иначе камера не прочтёт. */
async function qrCanvas(text) {
  const qrcode = await loadQr();
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const count = qr.getModuleCount();
  const scale = 4;
  const size = (count + 8) * scale;
  const canvas = el('canvas', { id: 'totp-qr', width: String(size), height: String(size), role: 'img',
    'aria-label': tr("QR-код для приложения-аутентификатора") });
  const context = canvas.getContext('2d');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, size, size);
  context.fillStyle = '#000';
  for (let row = 0; row < count; row++) {
    for (let col = 0; col < count; col++) {
      if (qr.isDark(row, col)) context.fillRect((col + 4) * scale, (row + 4) * scale, scale, scale);
    }
  }
  return canvas;
}

export function totpBlock(host) {
  const box = el('div', { id: 'totp-box' });
  const note = el('p', { id: 'totp-status', role: 'status', 'aria-live': 'polite' });

  function say(text, kind) {
    note.className = kind || '';
    note.textContent = text;
  }

  function passwordInput(id) {
    return el('input', { class: 'input', id, type: 'password', autocomplete: 'current-password', maxlength: '1024', required: true });
  }

  function render() {
    if (me.totp === 'on') {
      const password = passwordInput('totp-off-password');
      const off = button(tr("Отключить"), { class: 'btn btn-danger', type: 'submit', id: 'totp-off' });
      const form = el('form', {}, field(tr("Текущий пароль"), password, password.id), off);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (off.disabled) return;
        off.disabled = true;
        try {
          await api('POST', '/api/me/totp/off', { password: password.value });
          me.totp = 'off';
          me.totp_backup_left = 0;
          render();
          say(tr("Приложение отключено"), 'ok');
        } catch (err) { say(words(err), 'error'); off.disabled = false; }
      });
      box.replaceChildren(el('p', { id: 'totp-on' }, tr("Приложение подключено. Резервных кодов осталось: {0}.", me.totp_backup_left)), form);
      return;
    }
    const password = passwordInput('totp-setup-password');
    const start = button(tr("Подключить приложение"), { class: 'btn', type: 'submit', id: 'totp-setup' });
    const form = el('form', {}, field(tr("Текущий пароль"), password, password.id), start);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (start.disabled) return;
      start.disabled = true;
      say('');
      try {
        const data = await api('POST', '/api/me/totp/setup', { password: password.value });
        password.value = '';
        await setup(data);
      } catch (err) { say(words(err), 'error'); }
      finally { start.disabled = false; }
    });
    box.replaceChildren(...(me.totp === 'reenter'
      ? [el('p', { class: 'error', id: 'totp-reenter' }, tr("Ключ сервера сменился, и приложение больше не действует. Подключите его заново."))] : []), form);
  }

  async function setup(data) {
    const code = el('input', { class: 'input', id: 'totp-code', type: 'text', inputmode: 'numeric',
      autocomplete: 'one-time-code', maxlength: '6', pattern: '[0-9]{6}', required: true });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const confirm = button(tr("Включить"), { class: 'btn btn-accent', id: 'totp-enable', type: 'submit', form: 'totp-enable-form' });
    const form = el('form', { id: 'totp-enable-form' }, field(tr("Код из приложения"), code, code.id), fail);
    let picture;
    try { picture = await qrCanvas(data.uri); }
    catch { picture = el('p', { class: 'muted small' }, tr("QR-код не загрузился — введите ключ вручную.")); }
    const dialog = openDialog(host, { id: 'totp-dialog', title: tr("Приложение-аутентификатор"), actions: [confirm], body: [
      el('p', { tabindex: '-1', autofocus: true }, tr("Отсканируйте QR-код в приложении-аутентификаторе (например, Google Authenticator, Microsoft Authenticator, 2FAS) и введите шестизначный код из него.")),
      picture,
      el('p', { class: 'small' }, tr("Ключ для ручного ввода:"), ' ', el('code', { id: 'totp-secret' }, data.secret.match(/.{1,4}/g).join(' '))),
      form] });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (confirm.disabled) return;
      confirm.disabled = true;
      try {
        const result = await api('POST', '/api/me/totp', { ticket: data.ticket, code: code.value.trim() });
        me.totp = 'on';
        me.totp_backup_left = result.codes.length;
        dialog.close();
        render();
        showCodes(result.codes);
      } catch (err) { fail.textContent = words(err); confirm.disabled = false; }
    });
  }

  function showCodes(codes) {
    const list = el('ul', { class: 'totp-codes', id: 'totp-codes' }, ...codes.map((c) => el('li', {}, el('code', {}, c))));
    const copy = button(tr("Копировать"), { class: 'btn', id: 'totp-codes-copy' }, async () => {
      try { await navigator.clipboard.writeText(codes.join('\n')); copy.textContent = tr("Скопировано"); }
      catch { getSelection().selectAllChildren(list); }
    });
    openDialog(host, { id: 'totp-codes-dialog', title: tr("Резервные коды"), cancelText: tr("Готово"), actions: [copy], body: [
      el('p', { tabindex: '-1', autofocus: true }, tr("Сохраните коды в надёжном месте: каждый подходит для входа один раз, если телефона нет под рукой. Больше они не покажутся.")),
      list] });
    say(tr("Приложение подключено"), 'ok');
  }

  render();
  return el('section', { class: 'block totp-block' }, el('h2', {}, tr("Приложение-аутентификатор")),
    el('p', { class: 'muted small' }, tr("Второй шаг входа и привязки компьютера — код из приложения на телефоне вместо письма: вход не зависит от почты. Если телефон потерян, войдите резервным кодом; без него приложение снимает только оператор сервера.")),
    box, note);
}
