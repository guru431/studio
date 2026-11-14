import { tr } from './i18n.js';
/* Пресеты раздела «Фото» (спека 2026-10-21-studio-photo-presets-compare-series, раздел 5): чип «★»
 * с меню — пресеты учётки (нажатие — применить, «×» — удалить с подтверждением) и «Сохранить как
 * пресет…» (окно: имя, галочка «промпт»; имя уже есть — пресет заменится, окно это говорит). Seed
 * пресет не хранит: поля seed нет (спека 2026-10-27-studio-ui-tidy, 4.5). Пресеты — GET/POST/DELETE
 * /api/images/presets, одни на всех устройствах учётки.
 *
 * presetChip({ host, snapshot, apply, say }): host() — корень раздела (окна); snapshot() —
 * параметры поля сейчас { model, connection_id, size, strength, prompt }; apply(params) —
 * поставить пресет; say(text, bad) — строка состояния раздела. Ответ — { wrap, chip, menu, load() }.
 * Меню — общий компонент menu.js: в строке пресета два пункта — применить и «Удалить пресет «…»» (значок ×),
 * стрелки проходят их по порядку. Текст — textContent. */
import { api } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';

export function presetChip({ host, snapshot, apply, say }) {
  let items = [];
  const chip = el('button', { type: 'button', class: 'chip', id: 'chip-preset', 'aria-label': tr("Пресеты"), title: tr("Пресеты") },
    icon('star'));
  const list = el('div', { class: 'preset-list' });
  const save = menuItem(tr("Сохранить как пресет…"), { attrs: { id: 'preset-save' } });
  const menu = el('div', { class: 'chip-menu preset-menu', id: 'chip-preset-menu', role: 'menu', 'aria-label': tr("Пресеты") },
    list, save);
  const popup = popupMenu(chip, menu, { side: 'above' });
  const close = () => popup.close();

  function paint() {
    list.replaceChildren(...(items.length ? items.map((p) => {
      const use = menuItem(p.name, { attrs: { class: 'preset-apply', 'data-id': p.id } });
      use.addEventListener('click', () => {
        close();
        apply(p.params);
      });
      const drop = el('button', { type: 'button', class: 'icon-btn preset-delete', role: 'menuitem', tabindex: '-1',
        'aria-label': tr("Удалить пресет «{0}»", p.name), title: tr("Удалить") }, icon('x'));
      drop.addEventListener('click', () => {
        close();
        confirmDelete(p);
      });
      return el('div', { class: 'preset-row' }, use, drop);
    }) : [el('p', { class: 'muted small preset-empty' }, tr("Пресетов пока нет"))]));
  }

  async function load() {
    try {
      items = (await api('GET', '/api/images/presets')).items;
    } catch (err) {
      say(words(err), true);
    }
    paint();
  }

  function confirmDelete(p) {
    const fail = el('p', { class: 'error' });
    const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'preset-delete-confirm' });
    const dialog = openDialog(host(), { id: 'preset-delete-dialog', title: tr("Удалить пресет «{0}»?", p.name),
      body: [el('p', {}, tr("Пресет удалится на всех устройствах учётки. История не изменится.")), fail], actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('DELETE', `/api/images/presets/${encodeURIComponent(p.id)}`);
      } catch (err) {
        if (err.code !== 'gone') {
          fail.textContent = words(err);
          confirm.disabled = false;
          return;
        }
      }
      dialog.close();
      load();
    });
  }

  function openSave() {
    close();
    const now = snapshot();
    const name = el('input', { id: 'preset-name', class: 'input', maxlength: '60', autocomplete: 'off' });
    const withPrompt = el('input', { type: 'checkbox', id: 'preset-with-prompt', checked: !!now.prompt, disabled: !now.prompt });
    const note = el('p', { id: 'preset-replace-note', class: 'muted small', 'aria-live': 'polite' });
    const fail = el('p', { class: 'error', role: 'alert' });
    const ok = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'preset-save-confirm' });
    const dialog = openDialog(host(), { id: 'preset-dialog', title: tr("Сохранить как пресет"), actions: [ok], body: [
      field(tr("Имя"), name, 'name'),
      el('label', { class: 'check' }, withPrompt, tr("Промпт")),
      el('p', { class: 'muted small' }, tr("Ещё сохранятся модель, формат и сила изменения.")), note, fail] });
    const taken = () => items.find((p) => p.name === name.value.trim());
    const paintName = () => {
      const old = taken();
      note.textContent = old ? tr("Пресет «{0}» уже есть — сохранение его заменит.", old.name) : '';
      ok.textContent = old ? tr("Заменить") : tr("Сохранить");
    };
    name.addEventListener('input', paintName);
    ok.addEventListener('click', async () => {
      const text = name.value.trim();
      if (!text) {
        fail.textContent = tr("Назовите пресет");
        return;
      }
      const params = { ...now, prompt: withPrompt.checked ? now.prompt : null };
      ok.disabled = true;
      try {
        await api('POST', '/api/images/presets', { name: text, params });
        dialog.close();
        say(tr("Пресет «{0}» сохранён", text), false);
        load();
      } catch (err) {
        fail.textContent = words(err);
        ok.disabled = false;
      }
    });
    name.focus();
  }

  save.addEventListener('click', openSave);
  paint();
  return { wrap: el('div', { class: 'chip-wrap' }, chip, menu), chip, menu, load };
}
