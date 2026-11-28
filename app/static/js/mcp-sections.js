import { api } from './api.js';
import { tr } from './i18n.js';
import { button, el, words } from './ui.js';
import { icon } from './icons.js';
import { picker } from './picker.js';
import { dateTime } from './time.js';
import { modelLabel } from './models.js';

const records = new Map();
document.addEventListener('studio-locked', () => {
  for (const [section, record] of records) if (!record.job && !record.starting) records.delete(section);
});
const imageNames = {
  image_generate: tr("Создать фото"), generate_image: tr("Создать фото"),
  image_edit: tr("Изменить фото"), edit_image: tr("Изменить фото"),
  image_describe: tr("Описать фото"), describe_image: tr("Описать фото"),
  upscale_image: tr("Улучшить фото"),
};

const PHOTO_TOOLS = new Set(['image_generate', 'image_edit', 'generate_image', 'edit_image', 'upscale_image']);
const VIDEO_TOOLS = new Set(['video_create', 'generate_video']);
const JOB_WORDS = {
  queued: tr("В очереди"), waiting_gpu: tr("Ожидает GPU"), running: tr("Выполняется"),
  completed: tr("Готово"), cancelled: tr("Отменено"), failed: tr("Ошибка"),
};

const STATE_WORDS = { ok: tr("работает"), busy: tr("занят"), down: tr("недоступен") };

/** Состояние источника MCP (`studio/status`, GET /api/mcp/status) словами: «занят, в очереди 2, сообщение сервера». */
export function mcpStateWords(item) {
  const parts = [STATE_WORDS[item.state] || item.state];
  if (item.queue) parts.push(tr("в очереди {0}", item.queue));
  if (item.message) parts.push(item.message);
  return parts.join(', ');
}

const LIMIT_MAX = 1e6;
const ENUM_MAX = 64;
const ENUM_TEXT = 80;

/** Пределы поля схемы, которым форма может верить: {type, min, max, enum, default} или null — бессмысленная схема
 * (не числа, min > max, больше LIMIT_MAX, огромный enum) оставляет разделу его собственные пределы. Те же правила —
 * у сервера (mcp_sections.field_limits): форма не пропустит то, что откажет run, и наоборот. */
export function schemaLimits(prop) {
  if (!prop || typeof prop !== 'object') return null;
  const number = value => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= LIMIT_MAX;
  const min = prop.minimum ?? null;
  const max = prop.maximum ?? null;
  if ((min !== null && !number(min)) || (max !== null && !number(max)) || (min !== null && max !== null && min > max)) return null;
  const choices = prop.enum ?? null;
  if (choices !== null && (!Array.isArray(choices) || !choices.length || choices.length > ENUM_MAX
    || !choices.every(value => (typeof value === 'string' && value.length <= 2 * ENUM_TEXT && [...value].length <= ENUM_TEXT)
      || number(value)))) return null;
  const value = prop.default;
  const inRange = typeof value === 'number' ? (min === null || value >= min) && (max === null || value <= max) : min === null && max === null;
  const fits = value !== undefined && value !== null && inRange && (!choices || choices.includes(value));
  return { type: typeof prop.type === 'string' ? prop.type : null, min, max, enum: choices, default: fits ? value : null };
}

function fileData(file, limit = 12 * 1024 * 1024) {
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

// подписи известных полей инструментов разделов (у остальных — `title` схемы или имя поля)
const FIELD_NAMES = { model: tr("Модель"), engine: tr("Движок"), protocol: tr("Протокол"),
  speakers: tr("Говорящих"), num_speakers: tr("Число говорящих"), language: tr("Язык"),
  diarize: tr("Разделить по говорящим"), diarization: tr("Разделить по говорящим") };
const UPSCALE_FIELDS = { mode: tr("Режим улучшения"), scale: tr("Масштаб от 1 до 4") };
const UPSCALE_MODES = { restore: tr("Восстановление · SeedVR2"), fast: tr("Быстрое увеличение · RealESRGAN") };
const LANGUAGE_NAMES = { auto: tr("Авто"), ru: tr("Русский"), russian: tr("Русский"), en: 'English', english: 'English' };
const TEXT_MAX = 200;

/** Поля схемы инструмента, которых раздел сам не рисует (`shown`) и не файловые: `boolean` — переключатель
 * `.ctl-chip`, `enum` — выбор picker, число с границами — поле `chip-num`, строка без `format` с `maxLength` до TEXT_MAX —
 * маленькое поле. Обёртка `.mcp-fields` (display: contents): `values()` — значения, отличные от умолчания схемы
 * (пустое и нетронутое не шлётся; число вне границ — ошибка словами), `disable(on)`, `destroy()` — меню выбора. */
export function extraFields(tool, shown, suffix = '') {
  const box = el('span', { class: 'mcp-fields' });
  const readers = [], picks = [], controls = [];
  const skip = new Set(['wait_seconds', ...shown, ...Object.keys(tool?.files || {})]);
  const required = new Set(tool?.schema.required || []);
  for (const [key, prop] of Object.entries(tool?.schema?.properties || {})) {
    if (skip.has(key) || !prop || typeof prop !== 'object') continue;
    const name = (tool.name === 'upscale_image' && Object.hasOwn(UPSCALE_FIELDS, key) && UPSCALE_FIELDS[key])
      || FIELD_NAMES[key] || (typeof prop.title === 'string' && prop.title.trim().slice(0, 40)) || key;
    const limits = schemaLimits(prop);
    const id = `mcp-field-${key.replace(/[^\w-]/g, '_')}${suffix}`;
    const models = key === 'model' && prop.type === 'string' ? tool.models || limits?.enum : null;
    if (Array.isArray(models) && models.length && models.every(value => typeof value === 'string')) {
      const select = el('select', { id, 'data-field': key, 'aria-label': tr("Модель") },
        el('option', { value: '' }, required.has(key) ? tr("Выбрать") : tr("По умолчанию")),
        ...models.map(value => el('option', { value }, modelLabel(value))));
      select.value = models.includes(prop.default) ? prop.default : '';
      const pick = picker(document.body, tr("Модель"), select);
      picks.push(pick);
      box.append(pick.wrap);
      controls.push(select);
      readers.push([key, () => select.value || undefined]);
    } else if (prop.type === 'boolean') {
      const initial = prop.default === true;
      const chip = el('button', { type: 'button', class: 'chip ctl-chip', id, 'data-field': key, 'aria-pressed': String(initial) }, name);
      chip.addEventListener('click', () => chip.setAttribute('aria-pressed', String(chip.getAttribute('aria-pressed') !== 'true')));
      box.append(chip);
      controls.push(chip);
      readers.push([key, () => (chip.getAttribute('aria-pressed') === 'true') === initial ? undefined : !initial]);
    } else if (limits?.enum) {
      const label = value => (tool.name === 'upscale_image' && key === 'mode' && Object.hasOwn(UPSCALE_MODES, value)
        && UPSCALE_MODES[value]) || (/lang/i.test(key) && LANGUAGE_NAMES[String(value).toLowerCase()]) || String(value);
      const select = el('select', { id, 'data-field': key }, el('option', { value: '' }, name),
        ...limits.enum.map(value => el('option', { value: String(value) }, label(value))));
      select.value = limits.default === null ? '' : String(limits.default);
      const pick = picker(document.body, name, select);
      picks.push(pick);
      box.append(pick.wrap);
      controls.push(select);
      readers.push([key, () => {
        const value = limits.enum.find(item => String(item) === select.value);
        return value === limits.default ? undefined : value;
      }]);
    } else if (['number', 'integer'].includes(prop.type) && limits && (limits.min !== null || limits.max !== null)) {
      const { min, max } = limits;
      const range = min !== null && max !== null ? tr("Значение «{0}» — от {1} до {2}", name, min, max)
        : min !== null ? tr("Значение «{0}» — не меньше {1}", name, min) : tr("Значение «{0}» — не больше {1}", name, max);
      const input = el('input', { id, 'data-field': key, inputmode: prop.type === 'integer' ? 'numeric' : 'decimal',
        autocomplete: 'off', 'aria-label': `${name}: ${range}`, value: limits.default === null ? '' : String(limits.default) });
      input.style.width = `${Math.max(2, String(max ?? min).length)}ch`;
      box.append(el('label', { class: 'chip ctl-chip chip-num', title: range }, el('span', {}, name), input));
      controls.push(input);
      readers.push([key, () => {
        const raw = input.value.trim();
        if (!raw) return undefined;
        const value = Number(raw.replace(',', '.'));
        if (!Number.isFinite(value) || (prop.type === 'integer' && !Number.isInteger(value))
          || (min !== null && value < min) || (max !== null && value > max)) throw new Error(range);
        return value === limits.default ? undefined : value;
      }]);
    } else if (prop.type === 'string' && !prop.format && (key === 'speakers' ||
      (Number.isInteger(prop.maxLength) && prop.maxLength > 0 && prop.maxLength <= TEXT_MAX))) {
      const initial = typeof prop.default === 'string' ? prop.default : '';
      const maxLength = Number.isInteger(prop.maxLength) && prop.maxLength > 0 ? prop.maxLength : 16;
      const input = el('input', { id, 'data-field': key, maxlength: String(maxLength), autocomplete: 'off',
        'aria-label': name, value: initial });
      input.style.width = '10ch';
      box.append(el('label', { class: 'chip ctl-chip chip-num mcp-field-text', title: name }, el('span', {}, name), input));
      controls.push(input);
      readers.push([key, () => {
        const value = input.value.trim();
        return !value || value === initial ? undefined : value;
      }]);
    }
  }
  box.values = () => Object.fromEntries(readers.map(([key, read]) => [key, read()]).filter(([, value]) => value !== undefined));
  box.disable = (on) => {
    for (const control of controls) control.disabled = on;
    for (const pick of picks) pick.sync();
  };
  box.destroy = () => { for (const pick of picks) pick.unmount(); };
  return box;
}

/** Реплики расшифровки инструмента (`turns` итога, mcp_sections.turns) — видом раздела `render`; файлы, кроме
 * картинок, звука и видео, — списком скачивания под итогом. */
function showResult(host, output, urls, render) {
  for (const url of urls.splice(0)) URL.revokeObjectURL(url);
  host.replaceChildren();
  if (output?.turns?.length && render) host.append(render(output.turns));
  else if (output?.text) host.append(el('pre', {}, output.text));
  const downloads = el('ul', { class: 'mcp-files' });
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
    if (preview) host.append(el('div', {}, preview, el('a', { class: 'btn btn-sm', href: url, download: result.name || '' }, tr("Скачать"))));
    else downloads.append(el('li', {}, el('a', { class: 'btn btn-sm', href: url, download: result.name || '',
      title: result.name ? tr("Скачать «{0}»", result.name) : tr("Скачать") }, icon('save'), result.name || tr("Скачать"))));
  }
  if (downloads.children.length) host.append(downloads);
}

/** The section owns its controls. MCP receives only the arguments from those controls that the tool declares.
 * `key` — место задания в `records` (по умолчанию — раздел: задание переживает уход из раздела); у «Прочитать» —
 * своё на каждый запуск, уходит с destroy(). `onEmpty` — каталог загрузился, а подходящих инструментов нет. */
export function mcpHeaders(source) {
  return source ? { 'X-Studio-MCP': encodeURIComponent(JSON.stringify(source)) } : undefined;
}

export function sectionMcp(section, { native, fields, fileRequired, onChange, onBusy, onText, onHistory, turns, onEmpty, onPhoto, onVideo,
  key: place = section, internalOnly = false }) {
  const base = `/api/mcp-tools/${section}`;
  const suffix = place === section ? '' : `-${place.replace(/[^\w-]/g, '-')}`;
  const controller = new AbortController();
  const urls = [];
  const options = new Map();
  let limit, timer, rendered, polling = false;
  let states = {};             // состояние серверов со `studio/status` (GET /api/mcp/status)
  // выбор «как у Агентов» (picker.js) поверх select#section-mcp-tool: значение и change — у select, прятать — обёртку;
  // меню — в body (поле ввода раздела прокручивается и ловит клавиши), уходит с destroy()
  const select = el('select', { id: `section-mcp-tool${suffix}` });
  const pick = picker(document.body, tr("Инструмент MCP"), select);
  const control = pick.wrap;
  control.classList.add('mcp-tool');
  control.hidden = true;
  const status = el('p', { class: 'status', 'aria-live': 'polite' });
  const result = el('div', { class: 'mcp-result', id: `section-mcp-result${suffix}` });
  const retry = button(tr("Обновить"), { class: 'btn btn-sm', hidden: true }, () => {
    record().retry = false;
    poll();
  });
  const dismiss = button(tr("Закрыть"), { class: 'btn btn-sm', hidden: true }, clear);
  // сервер не ответил на список инструментов или каталог не загрузился — сказать, а не молча убрать выбор
  const notice = el('p', { class: 'muted', id: `section-mcp-notice${suffix}`, hidden: true });
  const list = el('div', { class: 'mcp-history-list' });
  const past = el('details', { class: 'mcp-history', hidden: true },
    el('summary', {}, tr("История MCP")), list);
  const output = el('div', { class: 'mcp-run', hidden: true }, notice, status, result, retry, dismiss, past);
  let historyLoaded = false;
  const galleryTools = onVideo ? VIDEO_TOOLS : onPhoto ? PHOTO_TOOLS : null;
  const galleryMime = onVideo ? 'video/' : 'image/';
  const hasGalleryFile = output => output?.files?.some(file => file.mime.startsWith(galleryMime));
  const inGallery = item => galleryTools?.has(item.tool) &&
    (item.status !== 'completed' || (onVideo ? item.has_video : item.has_photo) || hasGalleryFile(item.result));

  async function loadHistory(before = '') {
    if (place !== section) return;
    const query = new URLSearchParams();
    if (before) query.set('before', before);
    if (onVideo) query.set('without_clips', 'true');
    const page = await api('GET', base + '/history' + (query.toString() ? '?' + query : ''),
      undefined, { signal: controller.signal });
    if (controller.signal.aborted) return;
    if (!before) list.replaceChildren();
    for (const item of page.items) {
      if (inGallery(item)) continue;
      const detail = el('div', { class: 'mcp-result' });
      const label = [dateTime(item.created), item.server, item.model && modelLabel(item.model), JOB_WORDS[item.status] || item.status]
        .filter(Boolean).join(' · ');
      const entry = el('details', { class: 'mcp-history-entry' }, el('summary', {}, label),
        item.prompt ? el('p', { class: 'mcp-history-prompt' }, item.prompt) : null, detail);
      entry.addEventListener('toggle', async () => {
        if (!entry.open || entry.dataset.loaded) return;
        entry.dataset.loaded = 'true';
        try {
          const viewed = await api('GET', base + '/history/' + item.id, undefined, { signal: controller.signal });
          if (controller.signal.aborted) return;
          showResult(detail, viewed.result, [], turns);
          if (viewed.error) detail.append(el('p', { class: 'status error' }, viewed.error));
          if (viewed.active) {
            detail.append(el('p', { class: 'status' }, JOB_WORDS[viewed.status] || viewed.status));
            delete entry.dataset.loaded;
          } else {
            detail.append(button(tr("Удалить"), { class: 'btn btn-sm' }, async () => {
              try {
                await api('DELETE', base + '/history/' + item.id, undefined, { signal: controller.signal });
                if (record()?.id === item.id) clear();
                await loadHistory();
              } catch (err) { if (!controller.signal.aborted) warn(words(err)); }
            }));
          }
        } catch (err) {
          delete entry.dataset.loaded;
          if (!controller.signal.aborted) detail.textContent = words(err);
        }
      });
      list.append(entry);
    }
    if (page.next) {
      const more = button(tr("Ещё"), { class: 'btn btn-sm' }, async () => {
        more.disabled = true;
        try { await loadHistory(page.next); more.remove(); }
        catch (err) { if (!controller.signal.aborted) { more.disabled = false; warn(words(err)); } }
      });
      list.append(more);
    }
    past.hidden = !list.childElementCount;
    const active = !before && (page.active || page.items.find(item => item.active));
    if (!historyLoaded && active && !record()) {
      records.set(place, { id: active.id, job: active.id, state: active.status, gallery: galleryTools?.has(active.tool),
        inGallery: inGallery(active), key: active.server_id + ':' + active.tool });
      if (options.has(record().key)) {
        select.value = record().key;
        paintFields();
        onChange?.();
      }
    }
    historyLoaded = true;
    paint();
  }
  // поля схемы выбранного инструмента, которых раздел не рисует (extraFields): раздел ставит `fields` в свой ряд
  const extras = el('span', { class: 'mcp-fields' });
  let extra = null;
  const record = () => records.get(place);
  const picked = () => options.get(select.value);
  const busy = () => !!(record()?.starting || record()?.job);
  const has = key => Object.hasOwn(picked()?.tool.schema.properties || {}, key);
  const hasFile = key => Object.hasOwn(picked()?.tool.files || {}, key);

  function galleryChanged(current, item) {
    current.inGallery = inGallery(item);
    if (controller.signal.aborted) return;
    if (current.gallery) (onVideo || onPhoto)(item);
    if (!item.active && item.result?.translation_id) onHistory?.(item);
  }

  function paint() {
    if (controller.signal.aborted) return;
    const current = record();
    const active = busy();
    const content = (current?.gallery && hasGalleryFile(current.output)) || (onHistory && current?.output?.translation_id)
      ? null : current?.output;
    const inVideoFeed = onVideo && current?.inGallery && !current.retry;
    const visibleError = inVideoFeed ? '' : current?.error;
    const visibleActive = !inVideoFeed && active;
    select.disabled = active;
    extra?.disable(active);
    paintState();
    retry.hidden = !current?.retry;
    dismiss.hidden = active || !(content || visibleError);
    output.hidden = notice.hidden && past.hidden && !visibleActive && !(visibleError || content);
    status.classList.toggle('error', !!visibleError);
    status.classList.toggle('mcp-job-active', visibleActive);
    status.textContent = visibleError || (visibleActive ? JOB_WORDS[current?.state] || tr("Отправляется…")
      : content ? tr("Готово") : '');
    if (rendered !== content) {
      rendered = content;
      if (onText) onText(rendered?.text || '');
      showResult(result, onText && rendered ? { files: rendered.files } : rendered, urls, turns);
    }
    onBusy?.();
    clearTimeout(timer);
    if (current?.job && !polling && !current.retry) timer = setTimeout(poll, 3500);
  }

  function clear() {
    if (busy()) return;
    records.delete(place);
    paint();
  }

  async function poll() {
    const current = record();
    if (!current?.job || polling || controller.signal.aborted) return;
    polling = true;
    try {
      const reply = current.id
        ? await api('GET', base + '/history/' + current.id, undefined, { signal: controller.signal })
        : await api('POST', base + '/status', { ticket: current.job }, { signal: controller.signal });
      if (controller.signal.aborted) return;
      current.error = '';
      current.state = reply.status;
      if (!reply.active) {
        current.job = null;
        current.error = reply.error || (!current.id && reply.status ? tr("Задание MCP завершилось со статусом «{0}»", reply.status) : '');
        current.output = current.id ? reply.result : !current.error ? reply : undefined;
        galleryChanged(current, reply);
        if (current.id) loadHistory().catch(err => { if (!controller.signal.aborted) warn(words(err)); });
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

  async function run(values, { files = {}, voiceId, source, photoJobId, origin } = {}) {
    const choice = source ? options.get(source) : picked();
    if (!choice || busy()) return false;
    const { server, tool } = choice;
    const current = { starting: true, key: source || select.value, gallery: galleryTools?.has(tool.name) };
    records.set(place, current);
    paint();
    try {
      const args = Object.fromEntries(Object.entries({ ...(!source && extra?.values()), ...values })
        .filter(([key, value]) => Object.hasOwn(tool.schema.properties || {}, key) && value !== undefined));
      if (Object.hasOwn(tool.schema.properties || {}, 'model') && !Object.hasOwn(args, 'model')
        && typeof tool.schema.properties.model.default === 'string' && tool.schema.required?.includes('model')) {
        args.model = tool.schema.properties.model.default;
      }
      const sources = Object.entries(files).filter(([key, value]) => Object.hasOwn(tool.files, key) && value);
      const supplied = new Set([...Object.keys(args), ...sources.map(([key]) => key)]);
      if (voiceId) ['reference_artifact_id', 'reference_text'].forEach(key => supplied.add(key));
      const missing = tool.schema.required?.find(key => !supplied.has(key));
      if (missing) throw new Error(tr("Для MCP заполните поле «{0}»", tool.schema.properties[missing].title || (missing === 'model' ? tr("Модель") : missing)));
      const inputs = Object.fromEntries(await Promise.all(sources.map(async ([key, value]) => [key, await fileData(value, limit)])));
      const reply = await api('POST', `${base}/servers/${server.id}/run`, { tool: tool.name, arguments: args, files: inputs, origin,
        ...(voiceId ? { voice_id: voiceId } : {}), ...(photoJobId ? { photo_job_id: photoJobId } : {}), ...(place === section ? { record: true } : {}) });
      if (reply.history) {
        const saved = reply.history;
        current.id = saved.id;
        current.job = saved.active ? saved.id : null;
        current.state = saved.status;
        current.output = saved.result;
        current.error = saved.error;
        galleryChanged(current, saved);
        loadHistory().catch(err => { if (!controller.signal.aborted) warn(words(err)); });
      } else if (reply.job) {
        current.job = reply.job;
        current.state = reply.status;
      } else {
        current.output = reply;
      }
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
    try { catalog = await api('GET', internalOnly ? base + '?internal_only=true' : base, undefined, { signal: controller.signal }); }
    catch (err) {
      if (!controller.signal.aborted) warn(words(err));
      return;
    }
    if (controller.signal.aborted) return;
    const down = catalog.items.filter(server => server.unavailable).map(server => `«${server.name}»`);
    if (down.length) warn(down.join(', '));
    limit = catalog.max_upload_bytes;
    const allowed = new Set([...fields, 'model']);
    for (const server of catalog.items) for (const tool of server.tools || []) {
      if (fileRequired && !Object.hasOwn(tool.files, fileRequired)) continue;
      if (!(tool.schema.required || []).every(key => allowed.has(key) || Object.hasOwn(tool.files, key))) continue;
      options.set(`${server.id}:${tool.name}`, { server, tool });
    }
    if (!options.size) {
      onEmpty?.();
      return;
    }
    const label = tool => tool.title || (Object.hasOwn(imageNames, tool.name) ? imageNames[tool.name] :
      tool.name === 'video_estimate' ? tr("Оценить время генерации") : '');
    const names = new Map();
    select.append(el('option', { value: '', disabled: !native }, tr("Студия")), ...[...options].map(([key, { server, tool }]) => {
      const option = el('option', { value: key }, ['MCP', server.name, label(tool)].filter(Boolean).join(' · '));
      names.set(option, server.id);
      return option;
    }));
    const current = record();
    const last = section === 'photo' && !native && !busy() ? null : current?.key;
    const photoCreate = section === 'photo'
      ? [...options].find(([, { tool }]) => tool.name === 'image_generate' || tool.name === 'generate_image')?.[0] : null;
    select.value = options.has(last) ? last : native ? '' : photoCreate || options.keys().next().value;
    control.hidden = false;
    paintFields();
    onChange?.();
    paint();
    // состояние источника: точка у выбора — по выбранному серверу, занятый и недоступный подписаны в списке
    api('GET', '/api/mcp/status', undefined, { signal: controller.signal }).then((found) => {
      states = found;
      for (const [option, sid] of names) if (found[sid] && found[sid].state !== 'ok') {
        option.textContent += ` · ${STATE_WORDS[found[sid].state]}`;
      }
      paintState();
    }, () => {});
  }

  /** Поля выбранного инструмента вне `fields` раздела — заново при смене инструмента; «Студия» — без полей. */
  function paintFields() {
    extra?.destroy();
    extra = picked() ? extraFields(picked().tool, fields, suffix) : null;
    extras.replaceChildren(...(extra ? [extra] : []));
    extra?.disable(busy());
  }

  /** Подпись выбора и точка состояния выбранного сервера на кнопке; подсказка — «Инструмент MCP: …, работает». */
  function paintState() {
    pick.sync();
    const item = states[picked()?.server.id];
    if (item) {
      pick.trigger.dataset.mcpState = item.state;
      pick.trigger.title += ` · ${mcpStateWords(item)}`;
    } else {
      delete pick.trigger.dataset.mcpState;
    }
  }

  select.addEventListener('change', () => { paintState(); paintFields(); onChange?.(); });
  document.addEventListener(`studio-mcp-${section}`, paint, { signal: controller.signal });
  document.addEventListener('studio-locked', paint, { signal: controller.signal });
  queueMicrotask(() => {
    paint();
    load();
    loadHistory().catch(err => { if (!controller.signal.aborted) warn(words(err)); });
  });
  return { control, output, fields: extras, has, hasFile, run, clear,
    selection() {
      const choice = picked();
      if (!choice) return null;
      return { server_id: choice.server.id, tool: choice.tool.name, arguments: extra?.values() || {} };
    },
    sources() {
      return [...options].map(([key, { server, tool }]) => ({ key, tool, label: [...select.options]
        .find(option => option.value === key)?.textContent, server_id: server.id }));
    },
    option(key) { return options.get(key)?.tool; },
    fileInput(file) { return fileData(file, limit); },
    fileSource(names, sid) {
      return [...options].find(([, { server, tool }]) => (!sid || server.id === sid)
        && names.includes(tool.name) && Object.hasOwn(tool.files, 'artifact_id'))?.[0];
    },
    acceptsPhotoSource() {
      const name = picked()?.tool.name;
      return PHOTO_TOOLS.has(name) && name !== 'upscale_image' && has('prompt') && hasFile('artifact_id');
    },
    editSource(sid, { withPrompt = false } = {}) {
      return [...options].find(([, { server, tool }]) => (!sid || server.id === sid)
        && ['image_edit', 'edit_image'].includes(tool.name) && Object.hasOwn(tool.files, 'artifact_id')
        && (!withPrompt || Object.hasOwn(tool.schema.properties || {}, 'prompt')))?.[0];
    },
    sourceName(key) { return options.get(key)?.server.name; },
    selectSource(key, model, values = {}) {
      if (!options.has(key)) return false;
      select.value = key;
      paintFields();
      for (const field of extra?.querySelectorAll('[data-field]') || []) {
        const value = field.dataset.field === 'model' ? model : values[field.dataset.field];
        if (value === undefined || value === null || value === '') continue;
        if (field.tagName === 'BUTTON') {
          field.setAttribute('aria-pressed', String(value === true));
          continue;
        }
        if (field.tagName === 'SELECT' && ![...field.options].some(o => o.value === String(value))) continue;
        field.value = String(value);
        field.dispatchEvent(new Event('change', { bubbles: true }));
      }
      paintState();
      onChange?.();
      return true;
    },
    argument(key) { return picked()?.tool.schema.properties?.[key]; },
    requires(key) { return picked()?.tool.schema.required?.includes(key) ?? false; },
    limits(key) { return schemaLimits(picked()?.tool.schema.properties?.[key]); },
    canClone() {
      const engines = picked()?.tool.schema.properties?.engine?.enum;
      return hasFile('reference_artifact_id') && has('reference_text') && Array.isArray(engines) && engines.includes('clone');
    },
    useNative() {
      if (!native) return;
      select.value = '';
      paintState();
      paintFields();
      onChange?.();
    },
    get tool() { return picked()?.tool.name; }, get busy() { return busy(); }, get error() { return record()?.error; },
    /** Предел записи инструмента, с (`studio/limits`, mcp_sections.supported) или null. */
    get maxSeconds() { return picked()?.tool.limits?.max_seconds ?? null; },
    get serverId() { return picked()?.server.id; },
    get server() { return picked()?.server.name; },          // имя сервера выбранного инструмента (голоса «Озвучки»)
    destroy() {
      controller.abort();
      clearTimeout(timer);
      if (place !== section) records.delete(place);      // своё место запуска с ним и уходит
      pick.unmount();
      extra?.destroy();
      for (const url of urls.splice(0)) URL.revokeObjectURL(url);
    } };
}
