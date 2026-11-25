/* Навыки «Агентов»: меню «/» поля задания, чип навыка, кнопки пустого чата, «Положить набор в проект» и
 * «Проверить другим помощником».
 *
 * Меню «/» (slash.js) — навыки проекта (мост 0.48.0, `agent.skills`: GET …/projects/<pid>/skills; отказ списка —
 * библиотека без них, `closed` — `failed`), навыки библиотеки (GET /api/skills?scope=agents) и команды помощника
 * (`commands(rows)` из настроек сессии). Навык проекта с ключом навыка библиотеки (`origin`) показывается один раз —
 * проектный. Выбор команды — «/имя » перед остатком поля; выбор навыка — чип «Навык: имя» (#code-skill-chip); выбор
 * живёт на модуль по чату (`key`), как черновик поля, и переживает смену вкладки и вида.
 * `lead()` — текст выбранного навыка (GET /api/skills/<id> или POST …/skills/text), он уходит перед уточнением.
 * Тексты навыков — только память страницы: `label(text)` узнаёт сообщение, начатое текстом навыка, отправленного
 * отсюда (20 последних), — лента показывает «Навык: имя» и уточнение.
 *
 * «Положить набор в проект» (только владелец, мост с `skills`, `file_operations`, `file_upload`): навыки библиотеки
 * со `scope` не `chat` и латинским ключом `origin` — файлами `<папка>/<ключ>/SKILL.md` (`skillFile`) через запись
 * файлов: папки — `create_dir` (409 exists — дальше), файл — PUT …/files/upload без замены (409 exists — не тронут).
 * «Проверить другим помощником: {0}» — новый чат другого помощника того же проекта с навыком «Приёмка» (`origin`
 * acceptance) и списком изменённых файлов последнего ответа в поле; отправляет человек. */
import { api, me } from './api.js';
import { locale, tr } from './i18n.js';
import { menuItem } from './menu.js';
import { ENGINE_NAMES, engineOrder } from './code-options.js';
import { skillChip, slashMenu } from './slash.js';
import { button, el, openDialog, words } from './ui.js';

const FOLDERS = ['.agents/skills', '.claude/skills'];   // claude_code.SKILL_FOLDERS
const FILE_MAX = 64 * 1024;                              // claude_code.SKILL_TEXT_MAX
const DESCRIPTION_MAX = 200;
const KEY = /^[a-z][a-z0-9-]{0,63}$/;
const CHANGED_MAX = 200;                                 // строк «- путь» в поле проверки
const LABELS_MAX = 20;
const sentTexts = [];                                    // { text, name } отправленных отсюда навыков, новые — в конце
/** Выбранный навык чата (`${aid}/${chat}` → строка меню) — на модуль, как черновики поля: смена вкладки, «Файлы» и
 * «Git» снимают вид, а чип возвращается вместе с текстом поля. Блокировка экрана стирает его, как черновики. */
const chips = new Map();
document.addEventListener('studio-locked', () => chips.clear());

const lang = () => (locale().toLowerCase().startsWith('en') ? 'en' : 'ru');
const library = async () => (await api('GET', `/api/skills?scope=agents&lang=${lang()}`)).items || [];
const libraryText = async (id) => (await api('GET', `/api/skills/${encodeURIComponent(id)}?lang=${lang()}`)).text;

/** Строки меню «/»: навыки проекта, библиотеки (без тех, чей `origin` — имя навыка проекта), команды помощника. */
export function mergeItems(skills, project, commands) {
  const local = new Set((project || []).map((row) => row.name));
  return [
    ...(project || []).map((row) => ({ kind: 'project', id: `${row.folder}/${row.name}`, name: row.name,
      description: row.description || '', folder: row.folder })),
    ...(skills || []).filter((row) => !(row.origin && local.has(row.origin)))
      .map((row) => ({ kind: 'skill', id: row.id, name: row.name, description: row.description || '', origin: row.origin })),
    ...(commands || []).filter((row) => typeof row?.name === 'string' && row.name)
      .map((row) => ({ kind: 'command', id: row.name, name: row.name, description: row.description || '' })),
  ];
}

/** SKILL.md навыка библиотеки: латинский ключ, «имя — описание» JSON-строкой, помощник сам его не запускает. */
export function skillFile(key, name, description, text) {
  const about = (description ? `${name} — ${description}` : name).slice(0, DESCRIPTION_MAX);
  return `---\nname: ${key}\ndescription: ${JSON.stringify(about)}\ndisable-model-invocation: true\n---\n\n${text}\n`;
}

export function agentSkills(host, { aid, pid, key, agent, engine, text, chipHost, failed, readonly = false }) {
  const base = `/api/code/agents/${aid}/projects/${pid}`;
  const chip = skillChip(chipHost, { changed: () => chips.delete(key) });   // крестик
  chip.node.id = 'code-skill-chip';
  const choose = (item) => { chip.set(item); chips.set(key, item); };
  if (chips.has(key)) chip.set(chips.get(key));
  let commandRows = [];

  async function items() {
    const [skills, project] = await Promise.all([library(), agent?.skills
      ? api('GET', `${base}/skills`).then((data) => data.items || [], (err) => {
        if (err.code === 'closed') failed(err);
        return [];
      }) : []]);
    return mergeItems(skills, project, commandRows);
  }

  function pick(item) {
    if (item.kind !== 'command') {
      choose(item);
      text.value = text.value.replace(/^\/\S*[ \t]*/, '');
    } else {
      const prefix = `/${item.name} `;
      text.value = prefix + text.value.replace(/^\/\S+[ \t]*/, '');
      text.setSelectionRange(prefix.length, prefix.length);
    }
    text.dispatchEvent(new Event('input', { bubbles: true }));
    text.focus();
  }

  const menu = slashMenu(text, { items, onPick: pick });

  function remember(value, name) {
    if (sentTexts.some((row) => row.text === value && row.name === name)) return;
    sentTexts.push({ text: value, name });
    if (sentTexts.length > LABELS_MAX) sentTexts.shift();
  }

  async function verify(target, verifyWith, changed) {
    try {
      const skill = (await library()).find((row) => row.origin === 'acceptance');
      if (!skill) {
        failed(new Error(tr("Навыка «Приёмка» нет в вашей библиотеке — добавьте умолчания в настройках")));
        return;
      }
      const files = (changed() || []).map((row) => row?.path).filter((path) => typeof path === 'string');
      const value = [tr("Изменённые файлы последнего ответа:"), ...files.slice(0, CHANGED_MAX).map((path) => `- ${path}`)]
        .join('\n');
      await verifyWith(target, value, { kind: 'skill', id: skill.id, name: skill.name,
        description: skill.description || '', origin: skill.origin });
    } catch (err) {
      failed(err);
    }
  }

  function install() {
    const choices = FOLDERS.map((folder, index) => el('label', { class: 'check' },
      el('input', { type: 'radio', name: 'code-skills-folder', value: folder, ...(index ? {} : { checked: true }) }),
      folder));
    const list = el('ul', { id: 'code-skills-install-list', class: 'code-skills-install-list' });
    const note = el('p', { id: 'code-skills-install-note', class: 'small muted', role: 'status' });
    const go = button(tr("Положить"), { class: 'btn btn-accent', id: 'code-skills-install-go' });
    const dialog = openDialog(host, { id: 'code-skills-install', title: tr("Положить набор в проект"),
      body: [el('p', { class: 'small' }, tr("Навыки библиотеки для «Агентов» лягут файлами <папка>/<ключ>/SKILL.md. Существующие файлы не перезаписываются.")),
        el('fieldset', { class: 'code-skills-folders' }, el('legend', {}, tr("Папка навыков")), ...choices), list, note],
      actions: [go] });
    const STATES = { written: () => tr("записан"), exists: () => tr("уже есть — не тронут"),
      too_large: () => tr("больше 64 КиБ — пропущен") };
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      go.disabled = true;
      for (const input of dialog.querySelectorAll('input[name="code-skills-folder"]')) input.disabled = true;
      const folder = dialog.querySelector('input[name="code-skills-folder"]:checked')?.value || FOLDERS[0];
      list.replaceChildren();
      note.textContent = tr("Записываю…");
      try {
        const set = (await library()).filter((row) => row.scope !== 'chat' && KEY.test(row.origin || ''));
        const made = new Set();
        for (const row of set) {
          const item = el('li', { 'data-key': row.origin }, el('span', {}, `${row.origin} · ${row.name}`), ' — ');
          list.append(item);
          const content = skillFile(row.origin, row.name, row.description || '', await libraryText(row.id));
          const bytes = new TextEncoder().encode(content);
          let result = 'written';
          if (bytes.length > FILE_MAX) result = 'too_large';
          else {
            const parts = [...folder.split('/'), row.origin];
            for (let n = 1; n <= parts.length; n += 1) {
              const dir = parts.slice(0, n).join('/');
              if (made.has(dir)) continue;
              try {
                await api('POST', `${base}/files/operate`, { operation: 'create_dir', path: dir });
              } catch (err) {
                if (err.code !== 'exists') throw err;
              }
              made.add(dir);
            }
            const path = `${folder}/${row.origin}/SKILL.md`;
            try {
              await api('PUT', `${base}/files/upload`, new Blob([bytes]), { headers: {
                'content-type': 'application/octet-stream', 'x-studio-name': encodeURIComponent(path) } });
            } catch (err) {
              if (err.code !== 'exists') throw err;
              result = 'exists';
            }
          }
          item.dataset.state = result;
          item.append(STATES[result]());
        }
        note.textContent = set.length ? tr("Готово") : tr("В библиотеке нет навыков для набора — добавьте умолчания в настройках");
      } catch (err) {
        if (err.code === 'closed') { dialog.close(); failed(err); return; }
        note.textContent = words(err);
        go.disabled = false;
        for (const input of dialog.querySelectorAll('input[name="code-skills-folder"]')) input.disabled = false;
      }
    });
  }

  return {
    commands(rows) {
      if (Array.isArray(rows)) commandRows = rows;
    },
    /** Текст выбранного навыка; навыка нет — null. Отказ — исключением (поле и чип не трогаются). */
    async lead() {
      const item = chip.value();
      if (!item) return null;
      const value = item.kind === 'project'
        ? (await api('POST', `${base}/skills/text`, { name: item.name, folder: item.folder })).text
        : await libraryText(item.id);
      if (typeof value !== 'string' || !value.trim()) throw new Error(tr("Навык пуст"));
      remember(value, item.name);
      return { text: value, item };
    },
    preset(item) { if (item) choose(item); },
    clear() { chip.clear(); chips.delete(key); },
    close() { menu.close(); },
    label(value) {
      if (typeof value !== 'string') return null;
      for (let n = sentTexts.length - 1; n >= 0; n -= 1) {
        const row = sentTexts[n];
        if (value === row.text) return { name: row.name, rest: '' };
        if (value.startsWith(`${row.text}\n\n`)) return { name: row.name, rest: value.slice(row.text.length + 2) };
      }
      return null;
    },
    async quick() {
      return (await library()).filter((row) => row.quick).map((row) => ({ id: row.id, name: row.name }));
    },
    libraryText,
    /** Пункты меню «Ветки и рабочие копии»: `changed()` — изменённые файлы последнего ответа, `verify(engine, text,
     * skill)` — новый чат другого помощника с текстом и навыком в поле. */
    menuItems({ changed, verify: verifyWith }) {
      if (readonly) return [];
      const rows = [];
      if (!agent?.shared && agent?.skills && agent?.file_operations && agent?.file_upload) {
        rows.push(menuItem(tr("Положить набор в проект"), { attrs: { id: 'code-skills-install-item' } }));
        rows.at(-1).addEventListener('click', install);
      }
      for (const target of verifyWith ? engineOrder(me?.code_defaults) : []) {
        if (target === engine || (target !== 'claude' && !agent?.[target])) continue;
        const item = menuItem(tr("Проверить другим помощником: {0}", ENGINE_NAMES[target]),
          { attrs: { 'data-verify': target } });
        item.addEventListener('click', () => verify(target, verifyWith, changed));
        rows.push(item);
      }
      return rows;
    },
    unmount() { menu.unmount(); },
  };
}
