import { api } from './api.js';
import { tr } from './i18n.js';
import { button, el, words } from './ui.js';

const records = new Map();
document.addEventListener('studio-locked', () => {
  for (const [section, record] of records) if (!record.job && !record.starting) records.delete(section);
});
const imageNames = {
  image_generate: tr("Создать фото"), generate_image: tr("Создать фото"),
  image_edit: tr("Изменить фото"), edit_image: tr("Изменить фото"),
  image_describe: tr("Описать фото"), describe_image: tr("Описать фото"),
};

function fileData(file, limit) {
  if (!(file instanceof File)) return { media_id: file.mediaId };
  if (!file.size || file.size > limit) {
    throw new Error(tr("Файл MCP должен быть непустым и не больше {0} МиБ", limit / (1024 * 1024)));
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
  if (output?.text) host.append(el('pre', {}, output.text));
  for (const result of output?.files || []) {
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
    host.append(el('div', {}, preview, el('a', { class: 'btn btn-sm', href: url, download: result.name || '' }, tr("Скачать"))));
  }
}

/** The section owns its controls. MCP receives only the arguments from those controls that the tool declares. */
export function sectionMcp(section, { native, fields, fileRequired, onChange, onBusy, onText }) {
  const base = `/api/mcp-tools/${section}`;
  const controller = new AbortController();
  const urls = [];
  const options = new Map();
  let limit, timer, rendered, polling = false;
  const control = el('select', { id: 'section-mcp-tool', class: 'chip mcp-tool', hidden: true, 'aria-label': tr("Инструмент MCP") });
  const status = el('p', { class: 'status', 'aria-live': 'polite' });
  const result = el('div', { class: 'mcp-result', id: 'section-mcp-result' });
  const retry = button(tr("Обновить"), { class: 'btn btn-sm', hidden: true }, () => {
    record().retry = false;
    poll();
  });
  const dismiss = button(tr("Закрыть"), { class: 'btn btn-sm', hidden: true }, clear);
  // сервер не ответил на список инструментов или каталог не загрузился — сказать, а не молча убрать выбор
  const notice = el('p', { class: 'muted', id: 'section-mcp-notice', hidden: true });
  const output = el('div', { class: 'mcp-run', hidden: true }, notice, status, result, retry, dismiss);
  const record = () => records.get(section);
  const picked = () => options.get(control.value);
  const busy = () => !!(record()?.starting || record()?.job);
  const has = key => Object.hasOwn(picked()?.tool.schema.properties || {}, key);
  const hasFile = key => Object.hasOwn(picked()?.tool.files || {}, key);

  function paint() {
    if (controller.signal.aborted) return;
    const current = record();
    const active = busy();
    control.disabled = active;
    retry.hidden = !current?.retry;
    dismiss.hidden = active || !(current?.output || current?.error);
    output.hidden = notice.hidden && !active && !(current?.error || current?.output);
    status.classList.toggle('error', !!current?.error);
    status.textContent = current?.error || (active ? tr("В работе") : current?.output ? tr("Готово") : '');
    if (rendered !== current?.output) {
      rendered = current?.output;
      if (onText) onText(rendered?.text || '');
      showResult(result, onText && rendered ? { files: rendered.files } : rendered, urls);
    }
    onBusy?.();
    clearTimeout(timer);
    if (current?.job && !polling && !current.retry) timer = setTimeout(poll, 3500);
  }

  function clear() {
    if (busy()) return;
    records.delete(section);
    paint();
  }

  async function poll() {
    const current = record();
    if (!current?.job || polling || controller.signal.aborted) return;
    polling = true;
    try {
      const reply = await api('POST', `${base}/status`, { ticket: current.job }, { signal: controller.signal });
      if (controller.signal.aborted) return;
      current.error = '';
      if (!reply.active) {
        current.job = null;
        current.error = reply.error || (reply.status ? tr("Задание MCP завершилось со статусом «{0}»", reply.status) : '');
        if (!current.error) current.output = reply;
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      current.error = words(err);
      if (err.status === 404) current.job = null;
      else current.retry = true;
    } finally {
      polling = false;
      paint();
    }
  }

  async function run(values, { files = {}, voiceId } = {}) {
    const choice = picked();
    if (!choice || busy()) return false;
    const { server, tool } = choice;
    const current = { starting: true, key: control.value };
    records.set(section, current);
    paint();
    try {
      const args = Object.fromEntries(Object.entries(values).filter(([key, value]) => has(key) && value !== undefined));
      const sources = Object.entries(files).filter(([key, value]) => hasFile(key) && value);
      const supplied = new Set([...Object.keys(args), ...sources.map(([key]) => key)]);
      if (voiceId) ['reference_artifact_id', 'reference_text'].forEach(key => supplied.add(key));
      const missing = tool.schema.required?.find(key => !supplied.has(key));
      if (missing) throw new Error(tr("Для MCP заполните поле «{0}»", tool.schema.properties[missing].title || missing));
      const inputs = Object.fromEntries(await Promise.all(sources.map(async ([key, value]) => [key, await fileData(value, limit)])));
      const reply = await api('POST', `${base}/servers/${server.id}/run`, { tool: tool.name, arguments: args, files: inputs,
        ...(voiceId ? { voice_id: voiceId } : {}) });
      if (reply.job) current.job = reply.job;
      else current.output = reply;
      return true;
    } catch (err) {
      current.error = words(err);
      return false;
    } finally {
      current.starting = false;
      document.dispatchEvent(new Event(`studio-mcp-${section}`));
    }
  }

  function warn(text) {
    notice.replaceChildren(tr("MCP недоступен: {0}", text), ' ',
      el('a', { href: '#/settings/chat' }, tr("Настроить подключения MCP")));
    notice.hidden = false;
    paint();
  }

  async function load() {
    let catalog;
    try { catalog = await api('GET', base, undefined, { signal: controller.signal }); }
    catch (err) {
      if (!controller.signal.aborted) warn(words(err));
      return;
    }
    if (controller.signal.aborted) return;
    const down = catalog.items.filter(server => server.unavailable).map(server => `«${server.name}»`);
    if (down.length) warn(down.join(', '));
    limit = catalog.max_upload_bytes;
    const allowed = new Set(fields);
    for (const server of catalog.items) for (const tool of server.tools || []) {
      if (fileRequired && !Object.hasOwn(tool.files, fileRequired)) continue;
      if (!(tool.schema.required || []).every(key => allowed.has(key) || Object.hasOwn(tool.files, key))) continue;
      options.set(`${server.id}:${tool.name}`, { server, tool });
    }
    if (!options.size) return;
    const label = tool => tool.title || (Object.hasOwn(imageNames, tool.name) ? imageNames[tool.name] :
      tool.name === 'video_estimate' ? tr("Оценить время генерации") : '');
    control.append(el('option', { value: '', disabled: !native }, tr("Студия")), ...[...options].map(([key, { server, tool }]) =>
      el('option', { value: key }, ['MCP', server.name, label(tool)].filter(Boolean).join(' · '))));
    const last = record()?.key;
    control.value = options.has(last) ? last : native ? '' : options.keys().next().value;
    control.hidden = false;
    onChange?.();
    paint();
  }

  control.addEventListener('change', () => onChange?.());
  document.addEventListener(`studio-mcp-${section}`, paint, { signal: controller.signal });
  document.addEventListener('studio-locked', paint, { signal: controller.signal });
  queueMicrotask(() => { paint(); load(); });
  return { control, output, has, hasFile, run, clear,
    argument(key) { return picked()?.tool.schema.properties?.[key]; },
    canClone() {
      const engines = picked()?.tool.schema.properties?.engine?.enum;
      return hasFile('reference_artifact_id') && has('reference_text') && Array.isArray(engines) && engines.includes('clone');
    },
    useNative() {
      if (!native) return;
      control.value = '';
      onChange?.();
    },
    get tool() { return picked()?.tool.name; }, get busy() { return busy(); },
    destroy() {
      controller.abort();
      clearTimeout(timer);
      for (const url of urls.splice(0)) URL.revokeObjectURL(url);
    } };
}
