/* Правка картинки ленты: инструкция, сила для Lustify, кисть по маске (перенос окна правки
 * прежней страницы app/index.html).
 *
 * openEditor(item, choice, onCreated): item — готовая запись ленты (media_id), choice — модель,
 * выбранная в разделе ({ connection_id, model, kind } или null — умолчание раздела); onCreated({ id,
 * seed, ids }) — задание принято (ids — все строки серии). Серия (series.js) — и здесь: правку
 * подбирают серией по силе; поле `series` уходит строкой JSON; с кистью выбора серии нет. Отправка — POST /api/images формой: connection_id, model, size=original,
 * prompt, source_media_id, strength (только Lustify) и mask (PNG: белое — менять, чёрное — взять из
 * исходника), если включено «Изменить только выделенную область» — кисть только у шлюза srvllm.
 * Маска снимается копией до первого await: штрих после нажатия в запрос не попадёт. */
import { api } from './api.js';
import { fieldBox } from './field.js';
import { seriesControl } from './series.js';
import { modelLabel } from './settings.js';
import { button, el, openDialog, words } from './ui.js';

const STRENGTH = '0.25';
const PROMPT_MAX = 24000;

export function openEditor(item, choice, onCreated) {
  const host = document.getElementById('view');
  const photo = el('img', { id: 'edit-photo', alt: 'Фото для изменения' });
  photo.src = `/api/media/${encodeURIComponent(item.media_id)}`;
  photo.decode().then(() => show(host, item, choice, photo, onCreated), () => {
    const status = document.getElementById('photo-status');
    if (status) {
      status.textContent = 'Не удалось открыть фото для изменения';
      status.className = 'status error';
    }
  });
}

function show(host, item, choice, photo, onCreated) {
  const gateway = !choice || choice.kind === 'srvllm';
  const lustify = gateway && (!choice || choice.model === 'lustify');
  const mask = el('canvas', { id: 'edit-mask', hidden: true });
  const selection = document.createElement('canvas');           // белое — выделено, чёрное — нет
  const prompt = el('textarea', { id: 'edit-prompt', class: 'input', rows: '3', maxlength: String(PROMPT_MAX),
    placeholder: 'Например: замени цвет куртки на синий, остальное оставь как есть' });
  const strength = el('input', { id: 'edit-strength', type: 'number', class: 'input', min: '0.05', max: '1', step: '0.05',
    value: STRENGTH });
  const strengthLabel = el('label', { id: 'edit-strength-label', class: 'field', hidden: !lustify },
    el('span', {}, 'Сила перерисовки'), strength);
  const series = seriesControl({ prefix: 'edit-series', canStrength: () => lustify, canSeed: () => gateway,
    from: () => strength.value });
  const seriesBox = el('div', { id: 'edit-series', class: 'field' }, el('span', {}, 'Серия'), series.panel);
  strength.addEventListener('input', () => series.refresh());
  const local = el('input', { id: 'edit-local', type: 'checkbox' });
  const brush = el('input', { id: 'edit-brush', type: 'range', min: '8', max: '120', value: '40' });
  const clear = button('Очистить выделение', { class: 'btn btn-sm', id: 'edit-clear-mask' });
  const tools = el('div', { id: 'edit-brush-tools', class: 'edit-tools', hidden: true },
    el('label', { class: 'edit-brush' }, 'Размер кисти', brush), clear,
    el('p', { class: 'muted small' }, 'Закрасьте нужную часть фото пальцем или мышью. Пиксели вне выделения будут взяты из исходника без изменений.'));
  const error = el('p', { id: 'edit-error', class: 'error', role: 'alert' });
  const promptBox = fieldBox(prompt);
  const submit = button('Применить изменения', { class: 'btn btn-accent', id: 'edit-submit' });
  const dialog = openDialog(host, { id: 'editor', title: 'Изменить фотографию', actions: [submit], body: [
    el('div', { class: 'edit-layout' }, el('div', { id: 'edit-stage' }, photo, mask), el('div', { class: 'edit-controls' },
    el('div', { class: 'field' }, el('label', { for: 'edit-prompt' }, 'Что изменить'), promptBox.box, promptBox.error),
    el('p', { id: 'edit-model-label', class: 'small' }, `Модель: ${choice ? modelLabel(choice.model) : 'по умолчанию раздела'} (выбрана в разделе)`),
    el('p', { id: 'edit-mode-tip', class: 'muted small' }, !gateway
      ? 'Внешний сервис меняет фото по инструкции. Правка кистью — только через шлюз srvllm.'
      : lustify
        ? 'Lustify перерисовывает фото. Меньшая сила лучше сохраняет исходник. Для сохранения окружения выделите область кистью.'
        : 'FLUX использует исходное фото и инструкцию. Без выделения области соседние детали тоже могут измениться.'),
    strengthLabel, seriesBox,
    el('label', { class: 'check', hidden: !gateway }, local, 'Изменить только выделенную область'),
    tools, error))] });

  selection.width = mask.width = photo.naturalWidth;
  selection.height = mask.height = photo.naturalHeight;
  let drawing = null;
  let selected = false;
  let sending = false;

  function clearSelection() {
    drawing = null;
    selected = false;
    mask.getContext('2d').clearRect(0, 0, mask.width, mask.height);
    const ctx = selection.getContext('2d');
    ctx.fillStyle = 'black';
    ctx.fillRect(0, 0, selection.width, selection.height);
  }

  function draw(event) {
    if (drawing === null) return;
    const box = mask.getBoundingClientRect();
    const point = { x: (event.clientX - box.left) * mask.width / box.width,
      y: (event.clientY - box.top) * mask.height / box.height };
    for (const [canvas, color] of [[mask, 'rgba(245,158,11,.6)'], [selection, 'white']]) {
      const ctx = canvas.getContext('2d');
      ctx.lineWidth = Number(brush.value) * mask.width / box.width;
      ctx.strokeStyle = color;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(drawing.x ?? point.x, drawing.y ?? point.y);
      ctx.lineTo(point.x + 0.01, point.y);
      ctx.stroke();
    }
    drawing = point;
    selected = true;
  }

  clearSelection();
  local.addEventListener('change', () => {
    drawing = null;
    tools.hidden = mask.hidden = !local.checked;
    seriesBox.hidden = local.checked;                // кистью серии нет
  });
  clear.addEventListener('click', clearSelection);
  mask.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    mask.setPointerCapture(event.pointerId);
    drawing = {};
    draw(event);
  });
  mask.addEventListener('pointermove', draw);
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    mask.addEventListener(name, () => { drawing = null; });
  }

  submit.addEventListener('click', async () => {
    if (sending) return;
    error.textContent = '';
    const text = prompt.value.trim();
    if (!text) {
      error.textContent = 'Опишите необходимые изменения';
      return;
    }
    if (local.checked && !selected) {
      error.textContent = 'Выделите кистью область для изменения';
      return;
    }
    const force = Number(strength.value);         // пусто или не число — тоже отказ
    if (lustify && !(force >= 0.05 && force <= 1)) {
      error.textContent = 'Сила перерисовки — число от 0.05 до 1';
      return;
    }
    const body = new FormData();
    if (choice) {
      body.append('model', choice.model);
      body.append('connection_id', String(choice.connection_id));
    }
    body.append('size', 'original');
    body.append('prompt', text);
    body.append('source_media_id', item.media_id);
    if (lustify) body.append('strength', strength.value);
    const many = local.checked ? null : series.value();
    if (many) body.append('series', JSON.stringify(many));
    let snapshot = null;
    if (local.checked) {
      snapshot = document.createElement('canvas');
      snapshot.width = selection.width;
      snapshot.height = selection.height;
      snapshot.getContext('2d').drawImage(selection, 0, 0);
    }
    sending = true;
    submit.disabled = true;
    try {
      if (snapshot) {
        const png = await new Promise((resolve) => snapshot.toBlob(resolve, 'image/png'));
        if (!png) throw new Error('Не удалось подготовить выделение');
        body.append('mask', png, 'mask.png');
      }
      const created = await api('POST', '/api/images', body);
      dialog.close();
      if (onCreated) onCreated(created);
    } catch (err) {
      error.textContent = words(err);
    } finally {
      sending = false;
      submit.disabled = false;
    }
  });
  prompt.focus();
}
