/* Пресеты раздела «Фото» (спека 2026-10-21-studio-photo-presets-compare-series, раздел 5): чип «★»
 * с меню — пресеты учётки (нажатие — применить, «×» — удалить с подтверждением) и «Сохранить как
 * пресет…» (окно: имя, галочки «промпт» и «seed»; имя уже есть — пресет заменится, окно это
 * говорит). Пресеты — GET/POST/DELETE /api/images/presets, одни на всех устройствах учётки.
 *
 * presetChip({ host, snapshot, apply, say, close }): host() — корень раздела (окна); snapshot() —
 * параметры поля сейчас { model, connection_id, size, seed, strength, prompt }; apply(params) —
 * поставить пресет; say(text, bad) — строка состояния раздела; close() — закрыть меню. Ответ —
 * { wrap, chip, menu, load() }; открывает меню раздел (его общий список меню). Текст — textContent. */
import { api } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';

export function presetChip({ host, snapshot, apply, say, close }) {
  let items = [];
  const chip = el('button', { type: 'button', class: 'chip', id: 'chip-preset', 'aria-haspopup': 'true',
    'aria-expanded': 'false', 'aria-controls': 'chip-preset-menu', 'aria-label': 'Пресеты', title: 'Пресеты' }, '★');
  const list = el('div', { class: 'preset-list' });
  const save = el('button', { type: 'button', class: 'chip-option', id: 'preset-save', role: 'menuitem' },
    'Сохранить как пресет…');
  const menu = el('div', { class: 'chip-menu preset-menu', id: 'chip-preset-menu', role: 'menu', hidden: true }, list, save);

  function paint() {
    list.replaceChildren(...(items.length ? items.map((p) => {
      const use = el('button', { type: 'button', class: 'chip-option preset-apply', role: 'menuitem', 'data-id': p.id }, p.name);
      use.addEventListener('click', () => {
        close();
        apply(p.params);
      });
      const drop = el('button', { type: 'button', class: 'icon-btn preset-delete', 'aria-label': `Удалить пресет «${p.name}»`,
        title: 'Удалить' }, '×');
      drop.addEventListener('click', () => {
        close();
        confirmDelete(p);
      });
      return el('div', { class: 'preset-row' }, use, drop);
    }) : [el('p', { class: 'muted small preset-empty' }, 'Пресетов пока нет')]));
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
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: 'preset-delete-confirm' });
    const dialog = openDialog(host(), { id: 'preset-delete-dialog', title: `Удалить пресет «${p.name}»?`,
      body: [el('p', {}, 'Пресет удалится на всех устройствах учётки. История не изменится.'), fail], actions: [confirm] });
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
    const withSeed = el('input', { type: 'checkbox', id: 'preset-with-seed', checked: now.seed !== null, disabled: now.seed === null });
    const note = el('p', { id: 'preset-replace-note', class: 'muted small', 'aria-live': 'polite' });
    const fail = el('p', { class: 'error', role: 'alert' });
    const ok = button('Сохранить', { class: 'btn btn-accent', id: 'preset-save-confirm' });
    const dialog = openDialog(host(), { id: 'preset-dialog', title: 'Сохранить как пресет', actions: [ok], body: [
      field('Имя', name, 'name'),
      el('label', { class: 'check' }, withPrompt, 'Промпт'),
      el('label', { class: 'check' }, withSeed, now.seed === null ? 'Seed (не задан)' : `Seed ${now.seed}`),
      el('p', { class: 'muted small' }, 'Ещё сохранятся модель, формат и сила изменения.'), note, fail] });
    const taken = () => items.find((p) => p.name === name.value.trim());
    const paintName = () => {
      const old = taken();
      note.textContent = old ? `Пресет «${old.name}» уже есть — сохранение его заменит.` : '';
      ok.textContent = old ? 'Заменить' : 'Сохранить';
    };
    name.addEventListener('input', paintName);
    ok.addEventListener('click', async () => {
      const text = name.value.trim();
      if (!text) {
        fail.textContent = 'Назовите пресет';
        return;
      }
      const params = { ...now, prompt: withPrompt.checked ? now.prompt : null, seed: withSeed.checked ? now.seed : null };
      ok.disabled = true;
      try {
        await api('POST', '/api/images/presets', { name: text, params });
        dialog.close();
        say(`Пресет «${text}» сохранён`, false);
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
