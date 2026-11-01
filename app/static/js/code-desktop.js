/* Экран и ввод живут только в открытом окне. Разрешение меняется исключительно локально в агенте. */
import { api } from './api.js';
import { el, words } from './ui.js';

const LOCAL = 'Рабочий стол отключён. Человек за компьютером должен включить его в настройках агента.';
const FRAME_MS = 250;
const button = (id, label) => el('button', { id: `code-desktop-${id}`, type: 'button', class: 'btn' }, label);

export function desktopView(host, { locked }) {
  let agent = null;
  let generation = 0;
  let request = null;
  let session = null;
  let enabled = false;
  let disposed = false;
  const title = el('h2', { id: 'code-desktop-title' });
  const status = el('p', { class: 'code-desktop-status muted', role: 'status' });
  const connect = button('connect', 'Подключиться');
  const refresh = button('refresh', 'Проверить доступ');
  const disconnect = button('disconnect', 'Отключиться');
  const close = button('close', 'Закрыть');
  const monitor = el('select', { id: 'code-desktop-monitor', 'aria-label': 'Монитор' });
  const screen = el('canvas', { id: 'code-desktop-screen', width: 1, height: 1, tabindex: '0',
    'aria-label': 'Удалённый экран. Нажмите для управления мышью и клавиатурой.' });
  const stage = el('div', { class: 'code-desktop-stage', hidden: true }, screen);
  const text = el('textarea', { id: 'code-desktop-text', rows: 2, maxlength: 4096,
    autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Текст для ввода на компьютере',
    placeholder: 'Текст, включая русский' });
  const sendText = button('send-text', 'Ввести текст');
  const clipboard = el('textarea', { id: 'code-desktop-clipboard', rows: 2, maxlength: 65536,
    autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Текст буфера обмена',
    placeholder: 'Вставьте текст для передачи или получите буфер компьютера' });
  const clipboardSend = button('clipboard-send', 'Отправить буфер');
  const clipboardGet = button('clipboard-get', 'Получить буфер');
  const keys = el('div', { class: 'code-desktop-keys', 'aria-label': 'Клавиши и сочетания' });
  const modifierButtons = new Map();
  const tools = el('div', { class: 'code-desktop-tools', hidden: true },
    el('p', { class: 'muted small' }, 'Касание — клик, движение с нажатой кнопкой — перетаскивание. '
      + 'Для клавиатуры нажмите на экран. Сочетания можно набрать кнопками ниже.'), keys,
    el('div', { class: 'code-desktop-entry' }, text, sendText),
    el('div', { class: 'code-desktop-clipboard' }, el('label', { for: clipboard.id }, 'Буфер обмена'),
      clipboard, el('div', { class: 'code-desktop-actions' }, clipboardSend, clipboardGet)),
    el('p', { class: 'muted small' }, 'Обмен буфером выполняется только этими кнопками. '
      + 'Полученный текст можно выделить и скопировать.'));
  const dialog = el('dialog', { id: 'code-desktop-dialog', class: 'dialog code-desktop-dialog',
    'aria-labelledby': title.id },
    el('div', { class: 'code-desktop-heading' }, title, close), status,
    el('div', { class: 'code-desktop-actions' }, connect, refresh, disconnect,
      el('label', { class: 'code-desktop-monitor-label' }, 'Монитор ', monitor)),
    el('div', { class: 'code-desktop-body' }, stage, tools));
  host.append(dialog);

  const background = () => document.hidden || document.documentElement.classList.contains('shrouded');
  const current = (value) => value && session === value && value.generation === generation
    && dialog.open && !disposed && !background();
  const path = (target, suffix = '') => `/api/code/agents/${target.id}/desktop${suffix}`;

  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle('error', error);
    status.classList.toggle('muted', !error);
  }

  function buttons() {
    const active = !!session;
    connect.disabled = active || !enabled || !!request || background();
    refresh.disabled = active || !!request || !agent?.online || !agent?.desktop;
    disconnect.hidden = !active;
    monitor.disabled = !active;
    monitor.closest('label').hidden = !active;
    tools.hidden = !active;
    for (const control of tools.querySelectorAll('button, textarea')) control.disabled = !active;
  }

  function clearScreen() {
    screen.width = 1;
    screen.height = 1;
    stage.hidden = true;
    text.value = '';
    clipboard.value = '';
    for (const control of modifierButtons.values()) control.setAttribute('aria-pressed', 'false');
  }

  // После закрытия отправляется только отпускание и stop со старым токеном, без повторного подключения.
  function stop(message = '', error = false) {
    generation += 1;
    request?.abort();
    request = null;
    const old = session;
    session = null;
    if (old) {
      clearTimeout(old.frameTimer);
      clearTimeout(old.inputTimer);
      old.abort.abort();
      old.queue.length = 0;
      const events = releases(old);
      const finish = async () => {
        try {
          if (events.length) await api('POST', path(old.agent, '/input'), { control: old.control, events },
            { signal: AbortSignal.timeout(3000) });
        } catch { /* stop на агенте также отпускает весь ввод */ }
        try {
          await api('POST', path(old.agent, '/stop'), { control: old.control },
            { signal: AbortSignal.timeout(3000) });
        } catch { /* потерю соединения закрывает короткий срок сеанса на агенте */ }
      };
      void finish();
    }
    clearScreen();
    buttons();
    if (message) say(message, error);
  }

  function failed(err, target) {
    stop(words(err), true);
    if (err.code === 'closed') {
      dialog.close();
      locked?.(target.id);
    }
  }

  function releases(value) {
    const events = [...value.keys].map((code) => ({ type: 'key', code, down: false }));
    events.push(...[...value.buttons].map((name) => ({ type: 'button', button: name, down: false })));
    value.keys.clear();
    value.buttons.clear();
    for (const control of modifierButtons.values()) control.setAttribute('aria-pressed', 'false');
    return events;
  }

  function releaseHeld() {
    if (current(session)) enqueue(...releases(session));
  }

  function enqueue(...events) {
    const value = session;
    if (!current(value)) return;
    for (const event of events) {
      if (event.type === 'move' && value.queue.at(-1)?.type === 'move') value.queue[value.queue.length - 1] = event;
      else value.queue.push(event);
    }
    if (value.queue.length > 128) {
      stop('Соединение не успевает передавать ввод. Подключитесь снова.', true);
      return;
    }
    if (!value.inputTimer && !value.sending) value.inputTimer = setTimeout(() => flush(value), 16);
  }

  async function flush(value) {
    value.inputTimer = null;
    if (!current(value) || value.sending || !value.queue.length) return;
    const events = value.queue.splice(0, 32);
    value.sending = true;
    try {
      await api('POST', path(value.agent, '/input'), { control: value.control, events }, { signal: value.abort.signal });
    } catch (err) {
      if (current(value)) failed(err, value.agent);
    } finally {
      value.sending = false;
      if (current(value) && value.queue.length) value.inputTimer = setTimeout(() => flush(value), 0);
    }
  }

  async function frame(value) {
    if (!current(value)) return;
    const selected = Number(monitor.value);
    try {
      const data = await api('POST', path(value.agent, '/frame'),
        { control: value.control, monitor: selected }, { signal: value.abort.signal });
      if (!current(value)) return;
      if (!data || !Number.isInteger(data.width) || !Number.isInteger(data.height)
        || data.width < 1 || data.height < 1 || data.width > 8192 || data.height > 8192
        || typeof data.image !== 'string' || data.image.length > 960000
        || !/^[A-Za-z0-9+/]+={0,2}$/.test(data.image)) throw new Error('Не удалось получить изображение экрана');
      if (selected === Number(monitor.value) && data.monitor === selected) {
        const image = new Image();
        image.src = `data:image/jpeg;base64,${data.image}`;
        await image.decode();
        if (!current(value) || selected !== Number(monitor.value)) return;
        if (image.naturalWidth !== data.width || image.naturalHeight !== data.height)
          throw new Error('Размер изображения экрана не совпадает');
        screen.width = data.width;
        screen.height = data.height;
        screen.getContext('2d').drawImage(image, 0, 0);
        stage.hidden = false;
      }
    } catch (err) {
      if (current(value)) failed(err, value.agent);
      return;
    } finally {
      if (current(value)) value.frameTimer = setTimeout(() => frame(value), FRAME_MS);
    }
  }

  async function load() {
    stop();
    enabled = false;
    const target = agent;
    const mine = generation;
    if (!target.online || !target.desktop) {
      say(!target.online ? 'Компьютер не на связи — включите его и запустите агент.'
        : 'Обновите агент на компьютере для управления рабочим столом.');
      buttons();
      return;
    }
    request = new AbortController();
    const signal = request.signal;
    say('Проверяю доступ к рабочему столу…');
    buttons();
    try {
      const data = await api('GET', path(target), undefined, { signal });
      if (mine !== generation || !dialog.open || signal.aborted) return;
      enabled = data.enabled === true;
      say(!enabled ? LOCAL : data.active ? 'Рабочий стол уже занят другим подключением.'
        : 'Доступ разрешён на компьютере. Нажмите «Подключиться», чтобы открыть экран.');
      if (data.active) enabled = false;
    } catch (err) {
      if (mine === generation && dialog.open && !signal.aborted) failed(err, target);
    } finally {
      if (mine === generation) { request = null; buttons(); }
    }
  }

  async function start() {
    if (!enabled || request || session || background()) return;
    const target = agent;
    const mine = generation;
    const pending = new AbortController();
    request = pending;
    buttons();
    say('Подключаюсь…');
    try {
      // Не обрываем start при закрытии: поздно полученный токен сразу закрывается ниже.
      const data = await api('POST', path(target, '/start'), {}, { signal: AbortSignal.timeout(15000) });
      const valid = data && /^[0-9a-f]{32}$/.test(data.control) && Array.isArray(data.monitors)
        && data.monitors.length > 0 && data.monitors.length <= 32
        && data.monitors.every((item) => Number.isInteger(item.id) && Number.isInteger(item.width)
          && Number.isInteger(item.height) && item.width > 0 && item.height > 0)
        && data.monitors.some((item) => item.id === data.monitor);
      if (!valid) throw new Error('Агент вернул неверные параметры рабочего стола');
      if (mine !== generation || !dialog.open || disposed || background()) {
        await api('POST', path(target, '/stop'), { control: data.control }, { signal: AbortSignal.timeout(3000) });
        return;
      }
      monitor.replaceChildren(...data.monitors.map((item, index) => el('option', { value: item.id },
        `${index + 1}: ${item.width} × ${item.height}${item.primary ? ' · основной' : ''}`)));
      monitor.value = String(data.monitor);
      session = { control: data.control, agent: target, generation: mine, abort: new AbortController(),
        queue: [], keys: new Set(), buttons: new Set(), sending: false, frameTimer: null, inputTimer: null };
      say('Соединение открыто. При закрытии окна или уходе с экрана управление отключится.');
      void frame(session);
    } catch (err) {
      if (mine === generation && dialog.open) failed(err, target);
    } finally {
      if (request === pending) { request = null; buttons(); }
    }
  }

  function position(event) {
    const bounds = screen.getBoundingClientRect();
    const scale = Math.min(bounds.width / screen.width, bounds.height / screen.height);
    const width = screen.width * scale;
    const height = screen.height * scale;
    const x = (event.clientX - bounds.left - (bounds.width - width) / 2) / width;
    const y = (event.clientY - bounds.top - (bounds.height - height) / 2) / height;
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x > 1 || y > 1) return null;
    return { type: 'move', monitor: Number(monitor.value), x, y };
  }

  const mouseButton = (event) => ['left', 'middle', 'right'][event.button];
  screen.addEventListener('pointerdown', (event) => {
    const pos = position(event);
    const name = mouseButton(event);
    if (!current(session) || !pos || !name) return;
    event.preventDefault();
    screen.focus({ preventScroll: true });
    screen.setPointerCapture(event.pointerId);
    session.buttons.add(name);
    enqueue(pos, { type: 'button', button: name, down: true });
  });
  screen.addEventListener('pointermove', (event) => {
    if (!current(session)) return;
    const pos = position(event);
    if (pos) { event.preventDefault(); enqueue(pos); }
  });
  screen.addEventListener('pointerup', (event) => {
    const name = mouseButton(event);
    if (!current(session) || !session.buttons.has(name)) return;
    event.preventDefault();
    const pos = position(event);
    session.buttons.delete(name);
    enqueue(...(pos ? [pos] : []), { type: 'button', button: name, down: false });
    if (screen.hasPointerCapture(event.pointerId)) screen.releasePointerCapture(event.pointerId);
  });
  screen.addEventListener('pointercancel', releaseHeld);
  screen.addEventListener('lostpointercapture', () => {
    if (!current(session) || !session.buttons.size) return;
    const events = [...session.buttons].map((name) => ({ type: 'button', button: name, down: false }));
    session.buttons.clear();
    enqueue(...events);
  });
  screen.addEventListener('contextmenu', (event) => { if (current(session)) event.preventDefault(); });
  screen.addEventListener('wheel', (event) => {
    if (!current(session)) return;
    event.preventDefault();
    const factor = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? screen.clientHeight : 1;
    const bounded = (value) => Math.max(-1200, Math.min(1200, Math.round(value * factor)));
    enqueue({ type: 'wheel', dx: bounded(event.deltaX), dy: bounded(event.deltaY) });
  }, { passive: false });
  screen.addEventListener('blur', releaseHeld);

  function key(code, down) {
    if (!current(session) || !/^[A-Za-z][A-Za-z0-9]{0,31}$/.test(code)) return;
    if (down) session.keys.add(code);
    else session.keys.delete(code);
    modifierButtons.get(code)?.setAttribute('aria-pressed', String(down));
    enqueue({ type: 'key', code, down });
  }
  for (const type of ['keydown', 'keyup']) screen.addEventListener(type, (event) => {
    if (!current(session) || event.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    key(event.code, type === 'keydown');
  });
  dialog.addEventListener('cancel', (event) => { if (document.activeElement === screen && session) event.preventDefault(); });
  for (const [label, code] of [['Ctrl', 'ControlLeft'], ['Alt', 'AltLeft'], ['Win', 'MetaLeft'], ['Shift', 'ShiftLeft']]) {
    const control = el('button', { type: 'button', class: 'btn', 'aria-pressed': 'false' }, label);
    control.addEventListener('click', () => { if (current(session)) key(code, !session.keys.has(code)); });
    modifierButtons.set(code, control);
    keys.append(control);
  }
  for (const [label, code] of [['Tab', 'Tab'], ['Enter', 'Enter'], ['Esc', 'Escape'], ['⌫', 'Backspace'],
    ['Del', 'Delete'], ['←', 'ArrowLeft'], ['↑', 'ArrowUp'], ['↓', 'ArrowDown'], ['→', 'ArrowRight']]) {
    const control = el('button', { type: 'button', class: 'btn', 'aria-label': code }, label);
    control.addEventListener('click', () => { key(code, true); key(code, false); releaseHeld(); });
    keys.append(control);
  }
  const right = button('right-click', 'Правая кнопка');
  right.addEventListener('click', () => enqueue({ type: 'button', button: 'right', down: true },
    { type: 'button', button: 'right', down: false }));
  keys.append(right);
  monitor.addEventListener('change', () => { releaseHeld(); clearScreen(); });
  sendText.addEventListener('click', () => {
    if (!current(session) || !text.value) return;
    releaseHeld();
    enqueue({ type: 'text', text: text.value.slice(0, 4096) });
    text.value = '';
  });

  async function exchange(read) {
    const value = session;
    if (!current(value)) return;
    clipboardGet.disabled = true;
    clipboardSend.disabled = true;
    try {
      const data = await api('POST', path(value.agent, '/clipboard'),
        { control: value.control, text: read ? null : clipboard.value.slice(0, 65536) }, { signal: value.abort.signal });
      if (!current(value)) return;
      if (read) {
        if (!data || typeof data.text !== 'string' || data.text.length > 65536) throw new Error('Не удалось получить буфер');
        clipboard.value = data.text;
        say('Буфер компьютера получен. Выделите и скопируйте текст в поле.');
      } else say('Текст передан в буфер компьютера.');
    } catch (err) {
      if (current(value)) failed(err, value.agent);
    } finally {
      if (current(value)) { clipboardGet.disabled = false; clipboardSend.disabled = false; }
    }
  }
  clipboardGet.addEventListener('click', () => exchange(true));
  clipboardSend.addEventListener('click', () => exchange(false));
  connect.addEventListener('click', start);
  refresh.addEventListener('click', load);
  disconnect.addEventListener('click', () => stop('Соединение закрыто. Для нового сеанса нажмите «Подключиться».'));
  close.addEventListener('click', () => { stop(); dialog.close(); });
  dialog.addEventListener('close', () => { if (!dialog.open) stop(); });
  const hidden = () => {
    if (background() && dialog.open) stop('Управление остановлено при уходе с экрана. Подключитесь снова.');
    else if (dialog.open) buttons();
  };
  const leave = () => { if (dialog.open) stop('Соединение закрыто.'); };
  const observer = new MutationObserver(hidden);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  document.addEventListener('visibilitychange', hidden);
  window.addEventListener('pagehide', leave);
  window.addEventListener('blur', releaseHeld);
  buttons();
  return {
    show(value) {
      stop();
      agent = value;
      title.textContent = `${value.name} · рабочий стол`;
      if (!dialog.open) dialog.showModal();
      void load();
    },
    unmount() {
      disposed = true;
      stop();
      observer.disconnect();
      document.removeEventListener('visibilitychange', hidden);
      window.removeEventListener('pagehide', leave);
      window.removeEventListener('blur', releaseHeld);
      dialog.remove();
    },
  };
}
