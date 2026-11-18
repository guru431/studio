import { tr } from './i18n.js';
import { api } from './api.js';
import { el, openDialog, words } from './ui.js';
import { changeView } from './code-diff.js';

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
    const mirrorPicker = label(tr("Зеркала проекта"), choices);
    mirrorPicker.hidden = true;
    dialog = openDialog(host, { id: 'code-project-mirrors', title: tr("Зеркало · {0}", project.name),
      body: [mirrorPicker, targetForm, pinSlot, status, state], cancelText: tr("Закрыть") });
    let devices = [];
    let items = [];
    let busy = false;
    let timer;
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
        const pin = el('input', { class: 'input', type: 'password', inputmode: 'numeric',
          autocomplete: 'off', minlength: '4', maxlength: '8', pattern: '[0-9]{4,8}', required: true });
        const form = el('form', { class: 'project-mirror-form' },
          label(tr("PIN компьютера назначения"), pin), el('button', { class: 'btn', type: 'submit' }, tr("Открыть")));
        pinSlot.replaceChildren(form);
        tell('');
        form.addEventListener('submit', (event) => {
          event.preventDefault();
          work(async () => {
            const value = pin.value;
            pin.value = '';
            await request('POST', path, { pin: value });
            pinSlot.replaceChildren();
            await next();
          });
        });
        return;
      }
      await next();
    }

    function difference(item, data) {
      const detail = el('details', {}, el('summary', {}, tr("Посмотреть разницу ({0})", data.changes.length)));
      const rows = el('ul', { class: 'project-mirror-diff' });
      const preview = el('div', { class: 'project-mirror-preview' });
      const prev = button(tr("Назад")), next = button(tr("Далее"));
      const range = el('span', { class: 'muted', 'aria-live': 'polite' });
      const labels = { added: tr("Добавить"), changed: tr("Обновить"), removed: tr("Удалить"), skipped: tr("Пропущен") };
      let page = 0;
      function paint() {
        const first = page * 100;
        const chunk = data.changes.slice(first, first + 100);
        preview.replaceChildren();
        rows.replaceChildren(...chunk.map(([path, kind, conflict, text]) => {
          const file = text ? el('button', { type: 'button', class: 'btn-link' }, el('code', {}, path)) : el('code', {}, path);
          if (text) file.addEventListener('click', () => work(async () => {
            preview.replaceChildren(el('p', { class: 'muted' }, tr("Загрузка…")));
            try {
              const delta = await request('POST', `${url}/${item.id}/diff`, { path });
              const diff = changeView([{ path, ...delta }]);
              preview.replaceChildren(...(diff ? [diff] : []));
            } catch (err) {
              if (!signal.aborted) preview.replaceChildren(el('p', { class: 'error' }, words(err)));
            }
          }));
          return el('li', {}, el('span', { class: conflict ? 'error' : 'muted' },
            conflict ? tr("Локальное изменение") : labels[kind]), file);
        }));
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
      const updated = item.synced ? tr("Последняя синхронизация · {0}", new Date(item.synced * 1000).toLocaleString()) : '';
      const title = el('p', {}, `${item.computer} · ${item.name}`, el('br'), el('span', { class: 'muted' }, item.folder));
      const summary = !data.exists ? tr("Копия ещё не создана") : data.changes.length === data.skipped
        ? tr("Доступные для копирования файлы совпадают") : tr("Различий в файлах и папках: {0}", data.changes.length - data.skipped);
      const sync = button(data.exists ? tr("Синхронизировать изменения") : tr("Скопировать весь проект"));
      sync.disabled = data.exists && !data.changes.some(([, kind]) => kind !== 'skipped');
      sync.addEventListener('click', () => work(async () => {
        const cautions = [
          data.conflicts && tr("Локальных изменений в зеркале: {0}. Заменить их состоянием проекта?", data.conflicts),
          data.deletions && tr("Из зеркала будет удалено файлов и папок: {0}. Синхронизировать?", data.deletions),
        ].filter(Boolean);
        if (cautions.length && !window.confirm(cautions.join('\n'))) return;
        await request('POST', `${url}/${item.id}/sync`, { plan: data.plan, overwrite: data.conflicts > 0 });
        await watch(item);
      }));
      state.replaceChildren(title, el('p', {}, summary), ...(updated ? [el('p', { class: 'muted' }, updated)] : []),
        ...(data.conflicts ? [el('p', { class: 'error' }, tr("Локальных изменений в зеркале: {0}", data.conflicts))] : []),
        ...(data.skipped ? [el('p', { class: 'muted' }, tr("Пропущено ссылок, специальных объектов и связанных папок: {0}", data.skipped))] : []),
        ...(data.changes.length ? [difference(item, data)] : []),
        el('div', { class: 'actions' }, check, remove, sync));
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
      if (item?.job?.state === 'running') await watch(item);
      else if (item) await inspect(item);
      else { state.replaceChildren(); tell(''); }
    }

    async function load(prefer) {
      tell(tr("Загружаю настройки зеркал…"));
      const [saved, agents] = await Promise.all([request('GET', url), request('GET', '/api/agents')]);
      items = saved.items;
      devices = agents.items.filter((agent) => agent.id !== Number(aid) && !agent.shared);
      selectField(choices, tr("Новое зеркало"),
        items.map((item) => option(item.id, `${item.computer} · ${item.name} · ${item.folder}`)));
      mirrorPicker.hidden = !items.length;
      choices.value = String(prefer ?? items[0]?.id ?? '');
      selectField(targets, tr("Выберите компьютер"), devices.map((agent) =>
        option(agent.id, agent.online ? agent.name : tr("{0} · не на связи", agent.name))));
      selectField(roots, tr("Выберите папку проектов"), []);
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
        const saved = await request('POST', url, { destination: Number(targets.value), root: roots.value, name: name.value.trim() });
        await load(saved.id);
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
