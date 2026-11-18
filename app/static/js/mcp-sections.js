import { api } from './api.js';
import { tr } from './i18n.js';
import { button, el, field, openDialog, words } from './ui.js';

const sections = new Set(['photo', 'video', 'speech', 'transcribe', 'ocr', 'translate']);
const labels = {
  image_generate: tr("Создать фото"), image_edit: tr("Изменить фото"), image_describe: tr("Описать фото"),
  video_create: tr("Создать видео"), video_estimate: tr("Оценить время генерации"),
  speech_synthesize: tr("Озвучить текст"), audio_transcribe: tr("Расшифровать запись"),
  document_read: tr("Распознать страницу"), text_translate: tr("Перевести текст"),
  prompt: tr("Промпт"), text: tr("Текст"), model: tr("Модель"), size: tr("Размер"),
  seconds: tr("Длительность (с)"), voice: tr("Голос"), language: tr("Язык"), quality: tr("Качество"),
  artifact_id: tr("Исходный файл"), reference_artifact_id: tr("Образец голоса"), reference_text: tr("Текст образца"),
};
const records = new Map();
document.addEventListener('studio-locked', () => {
  document.querySelector('#section-mcp-dialog')?.close();
  for (const [section, record] of records) {
    if (!record.job && !record.starting) records.delete(section);
  }
});

const input = (tag, attrs, ...children) => el(tag, { class: 'input', ...attrs }, ...children);

function fieldControl(name, schema, required, file) {
  const base = { name, required, title: typeof schema.description === 'string' ? schema.description : null };
  const numeric = value => Number.isFinite(value) ? value : null;
  const defaultText = typeof schema.default === 'string' ? schema.default : '';
  let node, read;
  if (file) {
    node = input('input', { ...base, type: 'file', accept: file === 'image' ? 'image/png,image/jpeg,image/webp' : 'audio/*,video/*' });
    read = () => node.files[0];
  } else if (Array.isArray(schema.enum) || schema.type === 'boolean') {
    const options = Array.isArray(schema.enum) ? schema.enum.map(value => [value,
      typeof value === 'string' ? value : JSON.stringify(value)]) :
      [[true, tr("Да")], [false, tr("Нет")]];
    node = input('select', base, el('option', { value: '' }, '—'),
      ...options.map(([, label], index) => el('option', { value: index }, label)));
    read = () => node.value === '' ? undefined : options[Number(node.value)][0];
    const index = options.findIndex(([value]) => value === schema.default);
    if (index >= 0) node.value = String(index);
  } else if (['integer', 'number'].includes(schema.type)) {
    node = input('input', { ...base, type: 'number', step: schema.type === 'integer' ? 1 : 'any',
      min: numeric(schema.minimum), max: numeric(schema.maximum), value: numeric(schema.default) });
    read = () => node.value === '' ? undefined : node.valueAsNumber;
  } else if (schema.type === 'string' && !schema.format && !['prompt', 'text', 'reference_text'].includes(name)) {
    node = input('input', { ...base, type: 'text', minlength: numeric(schema.minLength), maxlength: numeric(schema.maxLength),
      value: defaultText, autocomplete: 'off' });
    read = () => node.value || undefined;
  } else {
    node = input('textarea', { ...base, rows: 3, spellcheck: 'false', maxlength: numeric(schema.maxLength) });
    if (schema.default !== undefined) node.value = schema.type === 'string' ? defaultText : JSON.stringify(schema.default);
    read = () => {
      if (!node.value) return undefined;
      if (schema.type === 'string') return node.value;
      try { return JSON.parse(node.value); }
      catch { throw new Error(tr("Проверьте формат JSON в поле «{0}»", label)); }
    };
  }
  const label = (typeof schema.title === 'string' && schema.title) || (Object.hasOwn(labels, name) ? labels[name] : name);
  return { node, file, read, field: field(label, node) };
}

function fileData(file, uploadMax) {
  if (!file.size || file.size > uploadMax) {
    throw new Error(tr("Файл MCP должен быть непустым и не больше {0} МиБ", uploadMax / (1024 * 1024)));
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ data: reader.result.split(',', 2)[1], mime_type: file.type, name: file.name });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function showResult(host, output, urls) {
  for (const url of urls.splice(0)) URL.revokeObjectURL(url);
  host.replaceChildren();
  if (!output) return;
  if (output.text) host.append(el('pre', {}, output.text));
  for (const result of output.files || []) {
    let url = result.url;
    if (result.base64) {
      try {
        const bytes = Uint8Array.from(atob(result.base64), char => char.charCodeAt(0));
        url = URL.createObjectURL(new Blob([bytes], { type: result.mime }));
        urls.push(url);
      } catch { continue; }
    }
    if (!url) continue;
    const type = result.mime.split('/')[0];
    const preview = type === 'image' ? el('img', { src: url, alt: result.name || tr("Результат MCP") }) :
      ['audio', 'video'].includes(type) ? el(type, { src: url, controls: true, preload: 'metadata', playsinline: true }) : null;
    host.append(el('div', {}, preview, el('a', { class: 'btn', href: url, download: result.name || '' }, tr("Скачать"))));
  }
}

function openTools(view, section, { items: servers, max_upload_bytes: uploadMax }, refresh) {
  const base = `/api/mcp-tools/${section}`;
  const options = servers.flatMap(server => (server.tools || []).map(tool => ({ server, tool })));
  const choose = input('select', { id: 'section-mcp-tool' }, ...options.map(({ server, tool }, index) =>
    el('option', { value: index }, `${tool.title || labels[tool.name] || tool.name} · ${server.name}`)));
  const parameters = el('div'), description = el('p', { class: 'muted' });
  const form = el('form', { id: 'section-mcp-form' }, field(tr("Инструмент MCP"), choose), description, parameters);
  const status = el('p', { class: 'status', 'aria-live': 'polite' });
  const result = el('div', { class: 'mcp-result', id: 'section-mcp-result' });
  const run = button(tr("Запустить"), { type: 'submit', class: 'btn btn-accent', form: form.id, id: 'section-mcp-run' });
  const retry = button(tr("Обновить"), { hidden: true }, () => { retry.hidden = true; poll(); });
  const unavailable = servers.filter(server => server.unavailable).map(server => server.name);
  const reload = button(tr("Проверить соединение"), { hidden: !unavailable.length }, () => { dialog.close(); refresh(true); });
  const signal = new AbortController();
  const urls = [];
  let controls = [], timer, painted, polling = false;
  let record = records.get(section) || {};
  const dialog = openDialog(view, { id: 'section-mcp-dialog', title: tr("Инструменты MCP"),
    body: [unavailable.length ? el('p', { class: 'error' }, tr("MCP недоступен: {0}", unavailable.join(', '))) : null,
      options.length ? form : null, status, result], actions: [reload, retry, ...(options.length ? [run] : [])],
    cancelText: tr("Закрыть") });

  function paint() {
    if (!dialog.open) return;
    record = records.get(section) || record;
    const busy = !!(record.job || record.starting);
    choose.disabled = run.disabled = busy;
    for (const control of controls) control.node.disabled = busy;
    status.classList.toggle('error', !!record.error);
    status.textContent = record.error || (busy ? tr("В работе") : record.output ? tr("Готово") : '');
    if (painted !== record.output) {
      painted = record.output;
      showResult(result, record.output, urls);
    }
  }

  function pollLater() {
    clearTimeout(timer);
    if (dialog.open && record.job && !polling && retry.hidden) timer = setTimeout(poll, 3500);
  }

  async function poll() {
    if (!record.job || polling || !dialog.open) return;
    polling = true;
    const current = record;
    try {
      const reply = await api('POST', `${base}/status`, { ticket: current.job }, { signal: signal.signal });
      if (!dialog.open) return;
      current.error = '';
      if (!reply.active) {
        current.job = null;
        current.error = reply.error || (reply.status ? tr("Задание MCP завершилось со статусом «{0}»", reply.status) : '');
        if (!current.error) current.output = reply;
      }
    } catch (err) {
      if (!dialog.open) return;
      current.error = words(err);
      if (err.status === 404) current.job = null;
      else retry.hidden = false;
    } finally {
      polling = false;
      paint();
      pollLater();
    }
  }

  function fields() {
    const picked = options[Number(choose.value)];
    if (!picked) return;
    const { tool } = picked;
    description.textContent = tool.description;
    description.hidden = !tool.description;
    controls = Object.entries(tool.schema.properties || {}).filter(([name, schema]) =>
      schema && typeof schema === 'object' && name !== 'wait_seconds').map(([name, schema]) =>
      fieldControl(name, schema, tool.schema.required?.includes(name), Object.hasOwn(tool.files, name) && tool.files[name]));
    parameters.replaceChildren(...controls.map(control => control.field));
  }

  choose.addEventListener('change', fields);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (record.job || record.starting || !options.length) return;
    const { server, tool } = options[Number(choose.value)];
    const current = record = { starting: true };
    records.set(section, current);
    paint();
    try {
      const values = controls.map(control => [control, control.read()]).filter(([, value]) => value !== undefined);
      const args = Object.fromEntries(values.filter(([control]) => !control.file).map(([control, value]) => [control.node.name, value]));
      const files = Object.fromEntries(await Promise.all(values.filter(([control]) => control.file).map(async ([control, value]) =>
        [control.node.name, await fileData(value, uploadMax)])));
      const reply = await api('POST', `${base}/servers/${server.id}/run`, { tool: tool.name, arguments: args, files });
      if (reply.job) current.job = reply.job;
      else if (records.get(section) === current) current.output = reply;
      parameters.querySelectorAll('input[type=file]').forEach(node => { node.value = ''; });
    } catch (err) {
      current.error = words(err);
    } finally {
      current.starting = false;
      document.dispatchEvent(new Event('studio-mcp-result'));
    }
  });

  const update = () => { paint(); pollLater(); };
  document.addEventListener('studio-mcp-result', update, { signal: signal.signal });
  function dispose() {
    signal.abort();
    clearTimeout(timer);
    for (const url of urls.splice(0)) URL.revokeObjectURL(url);
    if (dialog.open) dialog.close();
  }
  dialog.addEventListener('close', dispose, { once: true });
  fields();
  update();
  return dispose;
}

export function mountMcpTools(view, section) {
  if (!sections.has(section)) return;
  const head = view.querySelector('header.head');
  if (!head) return;
  const controller = new AbortController();
  let catalog, dispose;
  const launch = button('MCP', { id: 'section-mcp', 'aria-haspopup': 'dialog', title: tr("Инструменты MCP") },
    () => { if (!view.querySelector('#section-mcp-dialog[open]')) dispose = openTools(view, section, catalog, load); });
  const bar = el('div', { class: 'mcp-tools-bar', hidden: true }, launch);
  head.after(bar);
  async function load(reopen = false) {
    try {
      const reply = await api('GET', `/api/mcp-tools/${section}`, undefined, { signal: controller.signal });
      if (controller.signal.aborted) return;
      catalog = reply;
      bar.hidden = !catalog.items.length && !records.has(section);
      if (reopen && !bar.hidden) launch.click();
    } catch {
      // Discovery may fail independently of the section and its normal connection.
    }
  }
  load();
  return () => { controller.abort(); dispose?.(); };
}
