import { tr } from './i18n.js';
import { api } from './api.js';
import { dateTime } from './time.js';
import { computerPin, el, openDialog, words } from './ui.js';
import { changeView } from './code-diff.js';

function* parents(path) {
  while (path.includes('/')) {
    path = path.slice(0, path.lastIndexOf('/'));
    yield path;
  }
}

export function projectMirrors(host, trigger, aid, project) {
  const url = `/api/code/agents/${aid}/projects/${project.pid}/mirrors`;
  let dialog;
  const open = () => {
    if (dialog?.open) return;
    const controller = new AbortController();
    const { signal } = controller;
    const request = (method, path, data) => api(method, path, data, { signal });
    const button = (text) => el('button', { type: 'button', class: 'btn' }, text);
    const status = el('p', { class: 'status', role: 'status', 'aria-live': 'polite' });
    const choices = el('select', { class: 'input', 'aria-label': tr("Зеркала проекта") });
    const targets = el('select', { class: 'input', 'aria-label': tr("Компьютер назначения"), required: true });
    const roots = el('select', { class: 'input', 'aria-label': tr("Папка проектов на компьютере"), required: true });
    const name = el('input', { class: 'input', type: 'text', 'aria-label': tr("Имя папки копии"),
      maxlength: '120', autocomplete: 'off', required: true, value: project.name });
    const save = el('button', { class: 'btn', type: 'submit', disabled: true }, tr("Добавить зеркало"));
    const label = (text, field) => el('label', {}, el('span', {}, text), field);
    const targetForm = el('form', { class: 'project-mirror-form' },
      label(tr("Компьютер назначения"), targets), label(tr("Папка проектов на компьютере"), roots),
      label(tr("Имя папки копии"), name), save);
    const pinSlot = el('div');
    const state = el('div', { class: 'project-mirror-state' });
    const foundStatus = el('p', { class: 'muted', role: 'status' });
    const foundList = el('ul', { class: 'project-mirror-candidates' });
    const found = el('section', { id: 'code-mirror-discovery', hidden: true },
      el('h3', {}, tr("Найденные копии")), foundStatus, foundList);
    const mirrorPicker = label(tr("Зеркала проекта"), choices);
    mirrorPicker.hidden = true;
    dialog = openDialog(host, { id: 'code-project-mirrors', title: tr("Зеркало · {0}", project.name),
      body: [mirrorPicker, pinSlot, found, targetForm, status, state], cancelText: tr("Закрыть") });
    let devices = [];
    let items = [];
    let busy = false;
    let timer;
    let search = 0;
    const discovered = new Map();
    dialog.addEventListener('close', () => { controller.abort(); clearTimeout(timer); }, { once: true });

    function tell(text, error = false) {
      status.textContent = text;
      status.classList.toggle('error', error);
    }

    function option(value, text) { return el('option', { value: String(value) }, text); }

    function selectField(field, placeholder, rows) {
      field.replaceChildren(option('', placeholder), ...rows);
    }

    function interactive() {
      choices.disabled = busy;
      targetForm.inert = busy;
      pinSlot.inert = busy;
      state.inert = busy;
      foundList.inert = busy;
      save.disabled = !roots.value;
    }

    async function work(run) {
      if (busy || signal.aborted) return;
      busy = true;
      interactive();
      try { await run(); }
      catch (err) { if (!signal.aborted) tell(words(err), true); }
      finally {
        busy = false;
        if (!signal.aborted) interactive();
      }
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

    /* Привязка папки копии. Папка — зеркало другого проекта (прежнее имя этого, удалённый): после подтверждения
     * та привязка снимается и папка назначается этому проекту (`replace`); отказ подтверждения — `null`. */
    async function attachFolder(data) {
      try { return await request('POST', url, data); }
      catch (err) {
        if (err.code !== 'exists' || !err.detail?.elsewhere) throw err;
        if (!window.confirm(tr("Эта папка уже назначена зеркалом другого проекта — например, этого же проекта под прежним именем. Перепривязать её к этому проекту? Прежняя привязка будет снята, файлы останутся на компьютере."))) return null;
        return request('POST', url, { ...data, replace: true });
      }
    }

    function paintCopies() {
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
        const copies = device.items.map((candidate) => {
          const attach = button(candidate.elsewhere ? tr("Перепривязать") : tr("Привязать"));
          attach.addEventListener('click', () => work(async () => {
            const item = await attachFolder({
              destination: device.id, root: candidate.root, name: candidate.name, existing: true,
            });
            if (item) await load(item.id);
          }));
          return el('li', {}, el('div', {},
            el('strong', {}, `${device.computer} · ${candidate.name}`),
            el('div', { class: 'muted' }, candidate.folder),
            el('small', { class: 'muted' }, candidate.match === 'repository'
              ? tr("Тот же Git-репозиторий") : tr("Совпадает имя папки")),
            ...(candidate.elsewhere ? [el('br'), el('small', { class: 'muted' }, tr("Уже зеркало другого проекта"))] : [])),
          attach);
        });
        return note ? [...copies, el('li', { class: 'muted' }, `${device.computer} · ${note}`)] : copies;
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
        paintCopies();
      } catch (err) {
        if (!signal.aborted && mine === search) foundStatus.textContent = words(err);
      }
    }

    function difference(item, data, keep, omits, changed) {
      const detail = el('details', {}, el('summary', {}, tr("Посмотреть разницу ({0})", data.changes.length)));
      const rows = el('ul', { class: 'project-mirror-diff' });
      const preview = el('div', { class: 'project-mirror-preview' });
      const prev = button(tr("Назад")), next = button(tr("Далее"));
      const range = el('span', { class: 'muted', 'aria-live': 'polite' });
      const labels = { added: tr("Добавить"), changed: tr("Обновить"), removed: tr("Удалить"), skipped: tr("Пропущен") };
      let page = 0, marks = [];
      function paintMarks() {
        for (const { key, kind, conflict, mark } of marks) {
          const omitted = kind !== 'skipped' && omits(key);
          mark.textContent = omitted ? tr("Останется в копии") : conflict ? tr("Локальное изменение") : labels[kind];
          mark.className = conflict && !omitted ? 'error' : 'muted';
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
          const mark = el('span');
          marks.push({ key, kind, conflict, mark });
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
          return el('li', {}, mark, content);
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
      const check = button(tr("Обновить сравнение")), remove = button(tr("Отвязать зеркало"));
      check.addEventListener('click', () => work(() => inspect(item)));
      remove.addEventListener('click', () => work(async () => {
        await request('DELETE', `${url}/${item.id}`);
        await load();
        tell(tr("Зеркало отвязано. Файлы остались на компьютере."));
      }));
      return { check, remove };
    }

    function paint(item, data) {
      const { check, remove } = controls(item);
      const updated = item.synced ? tr("Последняя синхронизация · {0}", dateTime(item.synced)) : '';
      const title = el('p', {}, `${item.computer} · ${item.name}`, el('br'), el('span', { class: 'muted' }, item.folder));
      const summary = el('p', { 'aria-live': 'polite' });
      const warning = el('p', { class: 'error' });
      const sync = button(data.exists ? tr("Синхронизировать изменения") : tr("Скопировать весь проект"));
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
        ...(data.skipped ? [el('p', { class: 'muted' }, tr("Пропущено исключений, ссылок, специальных объектов, недопустимых имён и связанных папок: {0}", data.skipped))] : []),
        ...(data.changes.length ? [difference(item, data, keep, omits, changed)] : []),
        exclusions(item),
        el('div', { class: 'actions' }, check, remove, sync));
    }

    function exclusions(item) {
      const saved = item.exclude || [];
      const box = el('textarea', { id: 'code-mirror-exclude', class: 'input', rows: '3', spellcheck: 'false',
        autocomplete: 'off', 'aria-label': tr("Исключения зеркала") }, saved.join('\n'));
      const store = button(tr("Сохранить исключения"));
      store.addEventListener('click', () => work(async () => {
        const exclude = box.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        const fresh = await request('PUT', `${url}/${item.id}/exclude`, { exclude });
        items = items.map((row) => (row.id === fresh.id ? fresh : row));
        await inspect(fresh);
      }));
      return el('details', { class: 'project-mirror-exclude', open: saved.length > 0 },
        el('summary', {}, tr("Постоянные исключения ({0})", saved.length)),
        el('p', { class: 'muted' }, tr("Относительные пути папок и файлов проекта, по одному на строку, без шаблонов. Они не копируются, а в копии не удаляются. Каталог .git переносится целиком.")),
        box, store);
    }

    async function inspect(item) {
      pinSlot.replaceChildren();
      const { check, remove } = controls(item);
      state.replaceChildren(el('div', { class: 'actions' }, check, remove));
      tell(tr("Сравниваю файлы на двух компьютерах…"));
      await unlock(item.destination, async () => {
        const data = await request('POST', `${url}/${item.id}/compare`);
        paint(item, data);
        tell('');
      });
    }

    async function watch(item) {
      const fresh = await request('GET', `${url}/${item.id}`);
      if (fresh.job?.state === 'running') {
        state.replaceChildren();
        const job = fresh.job;
        tell(job.total
          ? tr("Передача файлов · {0}%", Math.floor(100 * job.bytes / job.total))
          : tr("Подготавливаю синхронизацию…"));
        timer = setTimeout(() => work(() => watch(fresh)), 1500);
      } else {
        await inspect(fresh);
        if (fresh.job?.state === 'failed') tell(words(fresh.job.error), true);
      }
    }

    async function selected() {
      clearTimeout(timer);
      pinSlot.replaceChildren();
      const item = items.find((row) => String(row.id) === choices.value);
      targetForm.hidden = !!item;
      if (item?.reenter) {
        state.replaceChildren(el('p', { class: 'error' },
          tr("Настройка зеркала недоступна после смены ключа Студии — отвяжите зеркало и привяжите папку заново.")),
        el('div', { class: 'actions' }, controls(item).remove));
        tell('');
      } else if (item?.job?.state === 'running') await watch(item);
      else if (item) await inspect(item);
      else { state.replaceChildren(); tell(''); }
    }

    async function load(prefer) {
      tell(tr("Загружаю настройки зеркал…"));
      const [saved, agents] = await Promise.all([request('GET', url), request('GET', '/api/agents')]);
      items = saved.items;
      devices = agents.items.filter((agent) => agent.id !== Number(aid) && !agent.shared);
      selectField(choices, tr("Новое зеркало"),
        items.map((item) => option(item.id, item.reenter ? `${item.computer} · ${tr("настройка недоступна")}`
          : `${item.computer} · ${item.name} · ${item.folder}`)));
      mirrorPicker.hidden = !items.length;
      choices.value = String(prefer ?? items[0]?.id ?? '');
      selectField(targets, tr("Выберите компьютер"), devices.map((agent) =>
        option(agent.id, agent.online ? agent.name : tr("{0} · не на связи", agent.name))));
      selectField(roots, tr("Выберите папку проектов"), []);
      findCopies();
      await selected();
      if (!items.length && !devices.length) tell(tr("Подключите ещё один компьютер к этой учётной записи."));
    }

    choices.addEventListener('change', () => work(selected));
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
        const saved = await attachFolder({ destination: Number(targets.value), root: roots.value, name: name.value.trim() });
        if (saved) await load(saved.id);
        else tell('');
      });
    });
    if (project.capable) work(load);
    else {
      targetForm.hidden = true;
      tell(tr("Обновите мост на компьютере исходного проекта."), true);
    }
  };
  trigger.addEventListener('click', open);
  return { unmount() { dialog?.close(); trigger.removeEventListener('click', open); } };
}
