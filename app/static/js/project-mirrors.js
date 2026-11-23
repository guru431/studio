import { tr } from './i18n.js';
import { api } from './api.js';
import { dateTime } from './time.js';
import { computerPin, el, openDialog, words } from './ui.js';
import { changeView } from './code-diff.js';

/* Отказы сервера (`project_mirrors`) окно узнаёт по признакам, а не по словам (их переводит `ApiError`):
 * `gone` — копии с этим id больше нет (отвязана или её строка слита с другой строкой той же папки), `local` —
 * передача остановлена изменениями в копии. */
const gone = (err) => err?.status === 404 && err.detail?.gone === true;

function* parents(path) {
  while (path.includes('/')) {
    path = path.slice(0, path.lastIndexOf('/'));
    yield path;
  }
}

/** `компьютер · папка\имя` — разделителем самой папки; адрес неизвестен или нечитаем — только компьютер. */
export function place(item) {
  if (!item.folder || !item.name) return item.computer;
  const separator = item.folder.includes('\\') ? '\\' : '/';
  return `${item.computer} · ${item.folder.replace(/[\\/]+$/, '')}${separator}${item.name}`;
}

function progress(job) {
  return job.total ? tr("Передача файлов · {0}%", Math.floor(100 * job.bytes / job.total)) : tr("Подготавливаю синхронизацию…");
}

/** Пометка копии в списке по её состоянию; ход синхронизации после хода агента (`job.auto`) — своими словами. */
export function mark(item) {
  if (item.reenter) return tr("настройка недоступна");
  if (item.job?.state === 'running') return progress(item.job);
  if (item.job?.state === 'differs') return tr("после хода не отправлено: есть различия — откройте сравнение");
  if (item.job?.auto && item.job.state === 'failed') return tr("после хода не отправлено: {0}", words(item.job.error));
  if (item.unknown) return item.online ? tr("адрес копии уточнится при сравнении")
    : tr("адрес копии уточнится, когда компьютер будет на связи");
  if (!item.online) return tr("не на связи");
  return item.synced ? tr("синхронизирована с этим компьютером · {0}", dateTime(item.synced)) : tr("ещё не синхронизировалась");
}

export function projectMirrors(host, trigger, aid, project) {
  const url = `/api/code/agents/${aid}/projects/${project.pid}/mirrors`;
  const count = trigger.querySelector('.code-mirror-count');
  let dialog;
  let mounted = true;
  let counting = 0;

  /** Цифра на кнопке — число других копий группы, по списку без обращений к мостам. Копию, которой ещё не дописан
   * `pid` (перенос прежних зеркал), находит `locate` при открытии окна — до того цифры у неё нет. */
  async function refresh() {
    if (!mounted) return;
    const mine = ++counting;
    let copies = 0;
    if (project.capable) {
      try {
        const listing = await api('GET', url);
        copies = listing.items.length;
      } catch {
        copies = 0;
      }
    }
    if (!mounted || mine !== counting) return;
    count.textContent = copies ? String(copies) : '';
    count.hidden = !copies;
    if (copies) trigger.setAttribute('aria-label', tr("Зеркало · копий: {0}", copies));
    else trigger.removeAttribute('aria-label');
  }

  const open = () => {
    if (dialog?.open) return;
    const controller = new AbortController();
    const { signal } = controller;
    const request = (method, path, data) => api(method, path, data, { signal });
    const button = (text, attrs = {}) => el('button', { type: 'button', class: 'btn', ...attrs }, text);
    const label = (text, field) => el('label', {}, el('span', {}, text), field);
    const status = el('p', { class: 'status', role: 'status', 'aria-live': 'polite' });
    const copies = el('ul', { id: 'code-mirror-copies', class: 'project-mirror-copies' });
    const copiesBox = el('section', { hidden: true }, el('h3', {}, tr("Копии на других компьютерах")), copies);
    const sendAll = button(tr("Отправить во все копии"), { id: 'code-mirror-send-all' });
    const add = button(tr("+ Добавить зеркало"), { id: 'code-mirror-add', 'aria-expanded': 'false',
      'aria-controls': 'code-mirror-add-panel' });
    const groupActions = el('div', { class: 'actions project-mirror-group', hidden: true }, sendAll, add);
    const auto = el('input', { type: 'checkbox', id: 'code-mirror-auto' });
    const autoBox = el('div', { class: 'project-mirror-auto', hidden: true },
      el('label', { class: 'check' }, auto, tr("Отправлять изменения в копии после каждого хода")),
      el('small', { class: 'muted' }, tr("Только без локальных изменений копии и удалений — иначе пометка у копии и уведомление.")));
    const targets = el('select', { class: 'input', 'aria-label': tr("Компьютер назначения"), required: true });
    const roots = el('select', { class: 'input', 'aria-label': tr("Папка проектов на компьютере"), required: true });
    const name = el('input', { class: 'input', type: 'text', 'aria-label': tr("Имя папки копии"),
      maxlength: '120', autocomplete: 'off', required: true, value: project.name });
    const save = el('button', { class: 'btn', type: 'submit', disabled: true }, tr("Добавить зеркало"));
    const targetForm = el('form', { class: 'project-mirror-form' },
      label(tr("Компьютер назначения"), targets), label(tr("Папка проектов на компьютере"), roots),
      label(tr("Имя папки копии"), name), save);
    const pinSlot = el('div');
    const state = el('div', { class: 'project-mirror-state' });
    const foundStatus = el('p', { class: 'muted', role: 'status' });
    const foundList = el('ul', { class: 'project-mirror-candidates' });
    const found = el('section', { id: 'code-mirror-discovery', hidden: true },
      el('h3', {}, tr("Найденные копии")), foundStatus, foundList);
    const addPanel = el('section', { id: 'code-mirror-add-panel', hidden: true }, found, targetForm);
    const exclusionsBox = el('div', { hidden: true });
    dialog = openDialog(host, { id: 'code-project-mirrors', title: tr("Зеркало · {0}", project.name),
      body: [copiesBox, groupActions, autoBox, addPanel, pinSlot, exclusionsBox, status, state], cancelText: tr("Закрыть") });
    let devices = [];
    let listing = { copy: null, items: [], exclude: [] };
    let chosen = null;
    let busy = false;
    let timer;
    let search = 0;
    const discovered = new Map();
    const notes = new Map();  // id копии → пометка «Отправить во все копии»
    dialog.addEventListener('close', () => { controller.abort(); clearTimeout(timer); refresh(); }, { once: true });

    function tell(text, error = false) {
      status.textContent = text;
      status.classList.toggle('error', error);
    }

    function option(value, text) { return el('option', { value: String(value) }, text); }

    function selectField(field, placeholder, rows) {
      field.replaceChildren(option('', placeholder), ...rows);
    }

    function interactive() {
      for (const node of [copies, groupActions, autoBox, targetForm, pinSlot, state, foundList, exclusionsBox]) node.inert = busy;
      save.disabled = !roots.value;
    }

    async function work(run) {
      if (busy || signal.aborted) return;
      busy = true;
      interactive();
      try { await run(); }
      catch (err) {
        if (signal.aborted) return;
        if (!gone(err)) { tell(words(err), true); return; }
        // id копии сменился (сервер слил строки одной папки) или её отвязали в другой вкладке: свежий список.
        try {
          chosen = null;
          await load();
          tell(tr("Список копий обновлён."));
        } catch (again) {
          if (!signal.aborted) tell(words(again), true);
        }
      } finally {
        busy = false;
        if (!signal.aborted) interactive();
      }
    }

    function pause(ms) {
      return new Promise((resolve) => {
        const wait = setTimeout(resolve, ms);
        signal.addEventListener('abort', () => { clearTimeout(wait); resolve(); }, { once: true });
      });
    }

    async function unlock(target, next) {
      const path = `/api/code/agents/${target}/open`;
      try { await request('POST', path, { pin: null }); }
      catch (err) {
        if (err.code !== 'pin_needed') throw err;
        const pin = computerPin({ maxlength: '8' });
        const go = button(tr("Открыть"));
        pinSlot.replaceChildren(el('div', { class: 'project-mirror-form', id: 'code-mirror-pin' },
          label(tr("PIN компьютера назначения"), pin), go));
        tell('');
        const send = () => {
          if (!pin.value) return;
          work(async () => {
            const value = pin.value;
            pin.value = '';
            await request('POST', path, { pin: value });
            pinSlot.replaceChildren();
            await next();
          });
        };
        go.addEventListener('click', send);
        pin.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' || event.isComposing) return;
          event.preventDefault();
          send();
        });
        return;
      }
      await next();
    }

    /* Привязка папки копии. Папка в другой группе копий — после подтверждения проект присоединяется к ней (`join`);
     * в той группе прежняя копия этого компьютера (проект переименован) — после подтверждения она заменяется
     * (`replace`). Отказ подтверждения — `null`. */
    async function attachFolder(data) {
      for (;;) {
        try { return await request('POST', url, data); }
        catch (err) {
          if (err.code === 'grouped' && !data.join) {
            const computers = (err.detail?.computers || []).join(', ');
            if (!window.confirm(tr("Папка уже в группе копий с компьютерами: {0}. Присоединить этот проект к группе?", computers))) return null;
            data = { ...data, join: true };
          } else if (err.code === 'exists' && err.detail?.former && !data.replace) {
            if (!window.confirm(tr("В группе уже есть прежняя копия этого компьютера — например, проект под прежним именем. Заменить её этим проектом? Файлы прежней папки останутся на компьютере."))) return null;
            data = { ...data, replace: true };
          } else if (err.code === 'conflict' && err.detail?.computers?.length) {
            // Слияние групп отклонено: назвать компьютеры, где копии есть в обеих группах.
            throw new Error(tr("{0} · компьютеры: {1}", words(err), err.detail.computers.join(', ')));
          } else throw err;
        }
      }
    }

    async function attached(saved) {
      if (!saved) { tell(''); return; }
      setAdd(false);
      await load(saved.id);
    }

    function paintCandidates() {
      const groups = [...discovered.values()];
      const rows = groups.flatMap((device) => {
        if (device.state === 'locked') {
          const check = button(tr("Проверить копии"));
          check.addEventListener('click', () => work(async () => {
            pinSlot.replaceChildren();
            await unlock(device.id, () => findCopies(device.id));
          }));
          return [el('li', {}, el('span', {}, device.computer, el('br'),
            el('small', { class: 'muted' }, tr("Для поиска нужен PIN компьютера"))), check)];
        }
        const labels = { offline: tr("Компьютер не на связи"), unsupported: tr("Обновите мост для поиска копий"),
          error: tr("Не удалось проверить папки компьютера"), partial: tr("Проверена часть папок компьютера") };
        const note = labels[device.state === 'ready' && !device.complete ? 'partial' : device.state];
        const candidates = device.items.map((candidate) => {
          const grouped = candidate.grouped;
          const attach = button(!grouped ? tr("Привязать") : listing.copy === null ? tr("Присоединить") : tr("Объединить"));
          attach.addEventListener('click', () => work(async () => {
            await attached(await attachFolder({
              destination: device.id, root: candidate.root, name: candidate.name, existing: true, join: !!grouped,
            }));
          }));
          return el('li', {}, el('div', {},
            el('strong', {}, `${device.computer} · ${candidate.name}`),
            el('div', { class: 'muted' }, candidate.folder),
            el('small', { class: 'muted' }, candidate.match === 'repository'
              ? tr("Тот же Git-репозиторий") : tr("Совпадает имя папки")),
            ...(grouped ? [el('br'), el('small', { class: 'muted' }, tr("В группе копий с {0}", grouped.join(', ')))] : [])),
          attach);
        });
        return note ? [...candidates, el('li', { class: 'muted' }, `${device.computer} · ${note}`)] : candidates;
      });
      foundList.replaceChildren(...rows);
      foundStatus.textContent = rows.length ? '' : tr("Непривязанные копии не найдены.");
    }

    async function findCopies(target = null) {
      const mine = ++search;
      found.hidden = !devices.length;
      if (found.hidden) return;
      if (target === null) {
        discovered.clear();
        foundList.replaceChildren();
      }
      foundStatus.textContent = tr("Ищу копии в папках проектов…");
      try {
        const result = await request('POST', `${url}/discover${target === null ? '' : `?destination=${target}`}`);
        if (signal.aborted || mine !== search) return;
        if (!result.available) {
          foundStatus.textContent = tr("Обновите мост исходного компьютера для поиска копий.");
          return;
        }
        for (const device of result.devices) discovered.set(device.id, device);
        paintCandidates();
      } catch (err) {
        if (!signal.aborted && mine === search) foundStatus.textContent = words(err);
      }
    }

    /** Панель добавления; поиск копий по компьютерам — только при её открытии. */
    function setAdd(show) {
      const opening = show && addPanel.hidden;
      addPanel.hidden = !show;
      add.setAttribute('aria-expanded', String(show));
      if (opening) findCopies();
    }

    function paintCopies() {
      copies.replaceChildren(...listing.items.map((item) => {
        const note = notes.get(item.id);
        const compare = button(tr("Сравнить"));
        compare.addEventListener('click', () => work(() => selected(item.id)));
        return el('li', { 'data-copy': String(item.id), 'aria-current': item.id === chosen ? 'true' : null },
          el('div', {}, el('strong', {}, place(item)), el('br'), el('small', { class: 'muted' }, mark(item)),
            ...(note ? [el('br'), el('small', { class: note.error ? 'error' : 'muted' }, note.text)] : [])),
          compare);
      }));
    }

    function replace(fresh) {
      listing.items = listing.items.map((row) => (row.id === fresh.id ? fresh : row));
      paintCopies();
    }

    function paintExclusions() {
      const saved = listing.exclude || [];
      const box = el('textarea', { id: 'code-mirror-exclude', class: 'input', rows: '3', spellcheck: 'false',
        autocomplete: 'off', 'aria-label': tr("Исключения зеркала") }, saved.join('\n'));
      const store = button(tr("Сохранить исключения"));
      store.addEventListener('click', () => work(async () => {
        const exclude = box.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        listing = await request('PUT', `${url}/exclude`, { exclude });
        paintCopies();
        paintExclusions();
        const item = listing.items.find((row) => row.id === chosen);
        if (item) await inspect(item);
        else tell('');
      }));
      exclusionsBox.replaceChildren(el('details', { class: 'project-mirror-exclude', open: saved.length > 0 },
        el('summary', {}, tr("Постоянные исключения ({0})", saved.length)),
        el('p', { class: 'muted' }, tr("Относительные пути папок и файлов проекта, по одному на строку, без шаблонов. Они не копируются, а в копии не удаляются. Каталог .git переносится целиком.")),
        ...(listing.exclude_reenter ? [el('p', { class: 'error' }, tr("Исключения недоступны после смены ключа Студии — сохраните их заново."))] : []),
        box, store));
      exclusionsBox.hidden = listing.copy === null;
    }

    function difference(item, data, keep, omits, changed) {
      const detail = el('details', {}, el('summary', {}, tr("Посмотреть разницу ({0})", data.changes.length)));
      const rows = el('ul', { class: 'project-mirror-diff' });
      const preview = el('div', { class: 'project-mirror-preview' });
      const prev = button(tr("Назад")), next = button(tr("Далее"));
      const range = el('span', { class: 'muted', 'aria-live': 'polite' });
      const labels = { added: tr("Добавить"), changed: tr("Обновить"), removed: tr("Удалить"), skipped: tr("Пропущен") };
      const reasons = { sqlite: tr("Пропущен · открытая база SQLite"), 'sqlite-journal': tr("Пропущен · служебный файл SQLite") };
      let page = 0, marks = [];
      function paintMarks() {
        for (const { key, kind, conflict, reason, mark: node } of marks) {
          const omitted = kind !== 'skipped' && omits(key);
          node.textContent = omitted ? tr("Останется в копии") : conflict ? tr("Локальное изменение")
            : (kind === 'skipped' && reasons[reason]) || labels[kind];
          node.className = conflict && !omitted ? 'error' : 'muted';
        }
      }
      function paint() {
        const first = page * 100;
        const chunk = data.changes.slice(first, first + 100);
        preview.replaceChildren();
        marks = [];
        rows.replaceChildren(...chunk.map(([path, kind, conflict, text, keepable, , key = path], offset) => {
          const file = text ? el('button', { type: 'button', class: 'btn-link' }, el('code', {}, path)) : el('code', {}, path);
          if (text) file.addEventListener('click', () => work(async () => {
            preview.replaceChildren(el('p', { class: 'muted' }, tr("Загрузка…")));
            try {
              const delta = await request('POST', `${url}/${item.id}/diff`, { path });
              const diff = changeView([{ path, ...delta, whole: true }]);
              preview.replaceChildren(...(diff ? [diff] : []));
            } catch (err) {
              if (!signal.aborted) preview.replaceChildren(el('p', { class: 'error' }, words(err)));
            }
          }));
          const node = el('span');
          marks.push({ key, kind, conflict, reason: data.reasons?.[path], mark: node });
          const content = el('div', {}, file);
          if (keepable) {
            const index = first + offset;
            const checkbox = el('input', { type: 'checkbox', checked: keep.has(index),
              'aria-label': tr("Оставить в копии «{0}»", path) });
            checkbox.addEventListener('change', () => {
              if (checkbox.checked) keep.set(index, key);
              else keep.delete(index);
              changed();
              paintMarks();
            });
            content.append(el('label', { class: 'project-mirror-keep' }, checkbox, tr("Оставить в копии")));
          }
          return el('li', {}, node, content);
        }));
        paintMarks();
        range.textContent = `${first + 1}–${first + chunk.length} / ${data.changes.length}`;
        prev.disabled = first === 0;
        next.disabled = first + chunk.length >= data.changes.length;
      }
      prev.addEventListener('click', () => { page -= 1; paint(); });
      next.addEventListener('click', () => { page += 1; paint(); });
      detail.append(rows);
      if (data.changes.length > 100) detail.append(el('div', { class: 'actions' }, prev, range, next));
      detail.append(preview);
      paint();
      return detail;
    }

    function controls(item) {
      const check = button(tr("Обновить сравнение")), remove = button(tr("Отвязать копию"));
      check.addEventListener('click', () => work(() => inspect(item)));
      remove.addEventListener('click', () => work(async () => {
        await request('DELETE', `${url}/${item.id}`);
        chosen = null;
        notes.delete(item.id);
        await load();
        tell(tr("Копия отвязана. Файлы остались на компьютере."));
      }));
      return { check, remove };
    }

    function paint(item, data) {
      const { check, remove } = controls(item);
      const updated = item.synced ? tr("Последняя синхронизация · {0}", dateTime(item.synced)) : '';
      const title = el('p', {}, el('strong', {}, place(item)));
      const summary = el('p', { 'aria-live': 'polite' });
      const warning = el('p', { class: 'error' });
      const sync = button(data.exists ? tr("Отправить изменения") : tr("Скопировать весь проект"));
      const keep = new Map();
      let files = new Set(), folders = new Set();
      let remaining, conflicts, deletions;
      function omits(path) {
        if (files.has(path) || folders.has(path)) return true;
        for (const parent of parents(path)) if (files.has(parent)) return true;
        return false;
      }
      function changed() {
        files = new Set(keep.values());
        folders = new Set();
        for (const path of files) for (const parent of parents(path)) folders.add(parent);
        remaining = data.changes.length - data.skipped;
        conflicts = data.conflicts;
        deletions = data.deletions;
        if (keep.size) {
          for (const [path, kind, conflict, , , deletion, key = path] of data.changes) {
            if (kind === 'skipped' || !omits(key)) continue;
            remaining -= 1;
            conflicts -= Number(conflict);
            deletions -= Number(deletion);
          }
        }
        summary.textContent = keep.size ? tr("Будет изменено файлов и папок: {0}", remaining)
          : !data.exists ? tr("Копия ещё не создана")
            : remaining ? tr("Различий в файлах и папках: {0}", remaining)
              : tr("Доступные для копирования файлы совпадают");
        warning.hidden = !conflicts;
        warning.textContent = conflicts ? tr("Локальных изменений в зеркале: {0}", conflicts) : '';
        sync.disabled = data.exists && !remaining;
      }
      changed();
      sync.addEventListener('click', () => work(async () => {
        const cautions = [
          keep.size && tr("Будет изменено файлов и папок: {0}", remaining),
          conflicts && tr("Локальных изменений в зеркале: {0}. Заменить их состоянием проекта?", conflicts),
          deletions && tr("Из зеркала будет удалено файлов и папок: {0}. Синхронизировать?", deletions),
        ].filter(Boolean);
        if (cautions.length && !window.confirm(cautions.join('\n'))) return;
        await request('POST', `${url}/${item.id}/sync`, { plan: data.plan, overwrite: conflicts > 0, keep: [...keep.keys()] });
        await watch(item);
      }));
      state.replaceChildren(title, summary, ...(updated ? [el('p', { class: 'muted' }, updated)] : []), warning,
        ...(data.skipped ? [el('p', { class: 'muted' }, tr("Пропущено исключений, ссылок, специальных объектов, баз SQLite, недопустимых имён и связанных папок: {0}", data.skipped))] : []),
        ...(data.changes.length ? [difference(item, data, keep, omits, changed)] : []),
        el('div', { class: 'actions' }, check, remove, sync));
    }

    async function inspect(item) {
      pinSlot.replaceChildren();
      chosen = item.id;
      paintCopies();
      const { check, remove } = controls(item);
      state.replaceChildren(el('p', {}, el('strong', {}, place(item))), el('div', { class: 'actions' }, check, remove));
      tell(tr("Сравниваю файлы на двух компьютерах…"));
      await unlock(item.destination, async () => {
        const data = await request('POST', `${url}/${item.id}/compare`);
        paint(item, data);
        tell('');
      });
    }

    async function watch(item) {
      const fresh = await request('GET', `${url}/${item.id}`);
      replace(fresh);
      if (fresh.job?.state === 'running') {
        state.replaceChildren();
        tell(progress(fresh.job));
        timer = setTimeout(() => work(() => watch(fresh)), 1500);
      } else {
        await inspect(fresh);
        if (fresh.job?.state === 'failed') tell(words(fresh.job.error), true);
      }
    }

    /** Вид выбранной копии: сравнение, ход передачи или объяснение нечитаемой настройки. */
    async function selected(id) {
      clearTimeout(timer);
      pinSlot.replaceChildren();
      const item = listing.items.find((row) => row.id === id);
      if (!item) {
        chosen = null;
        state.replaceChildren();
        tell('');
        return;
      }
      chosen = item.id;
      paintCopies();
      if (item.reenter) {
        state.replaceChildren(el('p', { class: 'error' },
          tr("Настройка зеркала недоступна после смены ключа Студии — отвяжите зеркало и привяжите папку заново.")),
        el('div', { class: 'actions' }, controls(item).remove));
        tell('');
      } else if (item.job?.state === 'running') await watch(item);
      else await inspect(item);
    }

    /** Ход передачи в копию до её конца; пометка строки — по ходу. */
    async function follow(item) {
      for (;;) {
        const fresh = await request('GET', `${url}/${item.id}`);
        replace(fresh);
        if (fresh.job?.state !== 'running') return fresh.job;
        await pause(1500);
        if (signal.aborted) throw new DOMException('', 'AbortError');
      }
    }

    /* «Отправить во все копии»: сначала сравнение с каждой подходящей копией, затем одно подтверждение, затем
     * передачи по очереди. Передача в одну копию не делает ложным план другой: сервер помнит всё, что копия держала
     * на своих синхронизациях. Копии со своими изменениями, не на связи и с PIN — пропуск с пометкой. */
    async function sendToAll() {
      clearTimeout(timer);
      notes.clear();
      pinSlot.replaceChildren();
      const note = (item, text, error = false) => { notes.set(item.id, { text, error }); paintCopies(); };
      const ready = [];
      let stale = false;
      tell(tr("Сравниваю проект с копиями…"));
      for (const item of listing.items) {
        // Копия с неизвестным адресом на связи входит: сравнение само допишет адрес (прежний мост — отказ с пометкой).
        const skip = item.reenter ? tr("настройка недоступна — не вошла в отправку")
          : !item.online ? tr("не на связи — не вошла в отправку")
            : item.job?.state === 'running' ? tr("идёт передача — не вошла в отправку") : null;
        if (skip) {
          note(item, skip);
          continue;
        }
        try {
          await request('POST', `/api/code/agents/${item.destination}/open`, { pin: null });
        } catch (err) {
          if (signal.aborted) throw err;
          note(item, err.code === 'pin_needed' ? tr("нужен PIN компьютера") : words(err), true);
          continue;
        }
        let data;
        try {
          data = await request('POST', `${url}/${item.id}/compare`);
        } catch (err) {
          if (signal.aborted) throw err;
          if (gone(err)) stale = true;
          else note(item, words(err), true);
          continue;
        }
        const changes = data.changes.length - data.skipped;
        if (data.conflicts > 0) note(item, tr("есть изменения, которых нет здесь — откройте сравнение"), true);
        else if (data.exists && !changes) note(item, tr("совпадает"));
        else ready.push({ item, data, changes });
      }
      const lines = ready.map(({ item, data, changes }) =>
        tr("{0} — изменений: {1}, удалений: {2}", item.computer, changes, data.deletions));
      if (ready.length && window.confirm(tr("Отправить изменения в копии?\n{0}", lines.join('\n')))) {
        for (const { item, data } of ready) {
          tell(tr("Отправляю изменения в копии…"));
          try {
            await request('POST', `${url}/${item.id}/sync`, { plan: data.plan, overwrite: false, keep: [] });
            const job = await follow(item);
            if (job?.state !== 'failed') note(item, tr("отправлено"));
            else if (job.error?.local === true) note(item, tr("есть изменения, которых нет здесь — откройте сравнение"), true);
            else note(item, words(job.error), true);
          } catch (err) {
            if (signal.aborted) throw err;
            if (gone(err)) stale = true;
            else note(item, words(err), true);
          }
        }
      }
      if (stale) chosen = null;
      await load();
    }

    async function load(prefer = null, first = false) {
      tell(tr("Загружаю настройки зеркал…"));
      const [saved, agents] = await Promise.all([
        first ? request('POST', `${url}/locate`) : request('GET', url), request('GET', '/api/agents')]);
      listing = saved;
      devices = agents.items.filter((agent) => agent.id !== Number(aid) && !agent.shared);
      for (const id of notes.keys()) if (!listing.items.some((item) => item.id === id)) notes.delete(id);
      const grouped = listing.items.length > 0;
      copiesBox.hidden = !grouped;
      groupActions.hidden = !grouped;
      autoBox.hidden = !grouped || listing.copy === null;
      auto.checked = !!listing.auto;
      paintCopies();
      paintExclusions();
      selectField(targets, tr("Выберите компьютер"), devices.map((agent) =>
        option(agent.id, agent.online ? agent.name : tr("{0} · не на связи", agent.name))));
      selectField(roots, tr("Выберите папку проектов"), []);
      if (!grouped) setAdd(true);
      await selected(prefer ?? chosen);
      if (!grouped && !devices.length) tell(tr("Подключите ещё один компьютер к этой учётной записи."));
    }

    sendAll.addEventListener('click', () => work(sendToAll));
    auto.addEventListener('change', () => work(async () => {
      const wanted = auto.checked;
      try {
        await request('PUT', `/api/code/mirrors/${listing.copy}/auto`, { auto: wanted });
        listing.auto = wanted;
        tell(wanted ? tr("Изменения будут уходить в копии после каждого хода.") : tr("Отправка после хода выключена."));
      } catch (err) {
        auto.checked = !wanted;
        throw err;
      }
    }));
    add.addEventListener('click', () => setAdd(addPanel.hidden));
    targets.addEventListener('change', () => work(async () => {
      selectField(roots, tr("Выберите папку проектов"), []);
      pinSlot.replaceChildren();
      const target = devices.find((agent) => String(agent.id) === targets.value);
      tell('');
      if (!target) return;
      if (!target.online) { tell(tr("Компьютер не на связи"), true); return; }
      if (!target.project_mirror) { tell(tr("Обновите мост на компьютере назначения."), true); return; }
      await unlock(target.id, async () => {
        const { items: folders } = await request('GET', `/api/code/agents/${target.id}/project-roots`);
        selectField(roots, tr("Выберите папку проектов"), folders.map((folder) => option(folder.id, folder.path)));
        if (folders.length === 1) roots.value = folders[0].id;
        if (!folders.length) tell(tr("На компьютере не настроены папки проектов."), true);
      });
    }));
    roots.addEventListener('change', interactive);
    targetForm.addEventListener('submit', (event) => {
      event.preventDefault();
      work(async () => {
        tell(tr("Сохраняю настройки зеркала…"));
        await attached(await attachFolder({ destination: Number(targets.value), root: roots.value, name: name.value.trim() }));
      });
    });
    if (project.capable) work(() => load(null, true));
    else tell(tr("Обновите мост на компьютере исходного проекта."), true);
  };
  trigger.addEventListener('click', open);
  return {
    unmount() {
      mounted = false;
      dialog?.close();
      trigger.removeEventListener('click', open);
    },
    refresh,
  };
}
