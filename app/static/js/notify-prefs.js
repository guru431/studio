import { tr } from './i18n.js';
/* «О чём уведомлять» этого устройства (GET/PUT /api/me/notify): виды push и ntfy, тихие часы по поясу учётки и
 * напоминание о ждущей карточке «Агентов» и «Чата». Настройка — у сессии: телефон и компьютер настраиваются отдельно,
 * после выхода из учётки — заново. Решает сервер до отправки (push.muted, bridge.remind,
 * chat_pending.remind). */
import { api } from './api.js';
import { el, field, words } from './ui.js';

const KINDS = [
  ['ask', tr("Вопросы и подтверждения агентов и чата")],
  ['done', tr("Конец хода агентов и ответы чата")],
  ['jobs', tr("Готовность заданий разделов")],
  ['service', tr("Служебные: копия базы, отключённая ссылка, неверный код входа")],
];
const REMINDS = [[0, tr("Не напоминать")], [10, tr("Через 10 минут")], [30, tr("Через 30 минут")],
  [60, tr("Через час")]];

export function notifyPrefsBlock() {
  const boxes = KINDS.map(([kind]) => el('input', { type: 'checkbox', id: `notify-kind-${kind}`, value: kind }));
  const quiet = el('input', { type: 'checkbox', id: 'notify-quiet' });
  const from = el('input', { type: 'time', id: 'notify-quiet-from', class: 'input', value: '22:00' });
  const to = el('input', { type: 'time', id: 'notify-quiet-to', class: 'input', value: '08:00' });
  const zone = el('p', { class: 'muted small', id: 'notify-zone' });
  const remind = el('select', { id: 'notify-remind', class: 'input' },
    ...REMINDS.map(([value, label]) => el('option', { value: String(value) }, label)));
  const save = el('button', { type: 'button', id: 'notify-prefs-save' }, tr("Сохранить"));
  const note = el('p', { id: 'notify-prefs-status', role: 'status', 'aria-live': 'polite' });
  const controls = [...boxes, quiet, from, to, remind, save];
  function busy(on) {
    for (const control of controls) control.disabled = on;
    if (!on) from.disabled = to.disabled = !quiet.checked;
  }
  quiet.addEventListener('change', () => { from.disabled = to.disabled = !quiet.checked; });
  function paint(value) {
    boxes.forEach((box) => { box.checked = !value.off.includes(box.value); });
    quiet.checked = value.quiet_from !== null;
    if (quiet.checked) {
      from.value = value.quiet_from;
      to.value = value.quiet_to;
    }
    remind.value = String(value.remind);
    zone.textContent = tr("Тихие часы — по часовому поясу учётки: {0}. В них не приходит ничего; о ждущем вопросе агента или чата после них напомнит повтор, если напоминание включено.", value.timezone);
  }
  save.addEventListener('click', async () => {
    note.textContent = '';
    if (quiet.checked && (!from.value || !to.value || from.value === to.value)) {
      note.textContent = tr("Укажите начало и конец тихих часов — разное время");
      return;
    }
    busy(true);
    try {
      paint(await api('PUT', '/api/me/notify', {
        off: boxes.filter((box) => !box.checked).map((box) => box.value),
        quiet_from: quiet.checked ? from.value : null, quiet_to: quiet.checked ? to.value : null,
        remind: Number(remind.value),
      }));
      note.textContent = tr("Выбор уведомлений сохранён на этом устройстве");
    } catch (err) {
      note.textContent = words(err);
    } finally {
      busy(false);
    }
  });
  const block = el('section', { class: 'block', id: 'notify-prefs' }, el('h2', {}, tr("О чём уведомлять")),
    el('p', { class: 'muted small' }, tr("Для push и ntfy этого устройства: телефон и компьютер настраиваются отдельно. После выхода из учётки — заново.")),
    el('div', { class: 'checks' }, ...boxes.map((box, i) => el('label', { class: 'check' }, box, KINDS[i][1]))),
    el('div', { class: 'row' }, el('label', { class: 'check' }, quiet, tr("Не беспокоить"))),
    el('div', { class: 'row' }, field(tr("С"), from, from.id), field(tr("По"), to, to.id)), zone,
    field(tr("Напомнить о ждущем вопросе агента или чата"), remind, remind.id),
    el('p', { class: 'muted small' }, tr("Повтор приходит, если вопрос или подтверждение не закрыты и чат не открыт, — не больше трёх раз.")),
    el('div', { class: 'row' }, save), note);
  busy(true);
  api('GET', '/api/me/notify').then(paint).catch((err) => { note.textContent = words(err); }).finally(() => busy(false));
  return block;
}
