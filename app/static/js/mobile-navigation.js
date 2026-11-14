import { tr } from './i18n.js';
/* На телефоне — все разделы либо 3–4 личных и «Ещё». На компьютере остаётся исходный рельс.
 * Порядок и имена — из реестра sections-meta.js (вкладки рельса строит app.js); по умолчанию — Чат, Фото, Агенты.
 * Сохранённый выбор — набор разделов, не порядок: смена порядка рельса его не ломает. Подписи видны на узком экране. */
import { el } from './ui.js';
import { icon } from './icons.js';
import { NAMES } from './sections-meta.js';

const DEFAULT = ['chat', 'photo', 'code'];

let openEditor = null;

export function setupMobileNavigation(me) {
  const rail = document.getElementById('rail');
  const tabs = [...rail.querySelectorAll('a.tab[data-section]')];
  const names = new Map(tabs.map(tab => [tab.dataset.section, NAMES[tab.dataset.section]]));
  const key = `studio-mobile-nav:${me.id}`;
  const modeKey = `studio-mobile-nav-mode:${me.id}`;
  const mobile = matchMedia('(max-width: 760px)');
  let active = '';
  let primary = [...DEFAULT];
  let mode = 'compact';
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (Array.isArray(saved) && saved.length >= 3 && saved.length <= 4
      && new Set(saved).size === saved.length && saved.every(section => names.has(section))) primary = saved;
    if (localStorage.getItem(modeKey) === 'all') mode = 'all';
  } catch { /* Закрытое или повреждённое хранилище — стандартные разделы. */ }

  const label = el('span', { id: 'mobile-more-label' }, tr("Ещё"));
  const more = el('button', { id: 'mobile-more', class: 'mobile-more', type: 'button',
    'aria-haspopup': 'dialog', 'aria-controls': 'mobile-sections', 'aria-expanded': 'false' },
    el('span', { class: 'tab-icon' }, icon('more', 'ico mobile-more-icon'),
      el('span', { id: 'mobile-running-count', class: 'tab-count', 'aria-hidden': 'true', hidden: true })), label);
  rail.insertBefore(more, rail.querySelector('.spacer'));
  const close = el('button', { id: 'mobile-sections-close', class: 'btn-link', type: 'button' }, tr("Закрыть"));
  const links = el('nav', { id: 'mobile-section-links', 'aria-label': tr("Остальные разделы") });
  const edit = el('button', { id: 'mobile-nav-edit', class: 'btn', type: 'button' }, tr("Настроить нижнюю панель"));
  const editor = el('form', { id: 'mobile-nav-editor', hidden: true });
  const compact = el('input', { id: 'mobile-nav-mode-compact', type: 'radio', name: 'mobile-nav-mode', value: 'compact' });
  const all = el('input', { id: 'mobile-nav-mode-all', type: 'radio', name: 'mobile-nav-mode', value: 'all' });
  const modes = el('fieldset', {}, el('legend', {}, tr("Нижняя панель")),
    el('label', {}, compact, tr("3–4 раздела и «Ещё»")),
    el('label', {}, all, tr("Все разделы · 8 иконок")));
  const choices = el('fieldset', {}, el('legend', {}, tr("Выберите 3–4 основных раздела")));
  const count = el('p', { class: 'muted', role: 'status', 'aria-live': 'polite' });
  const save = el('button', { id: 'mobile-nav-save', class: 'btn btn-accent', type: 'submit' }, tr("Сохранить"));
  const cancel = el('button', { class: 'btn', type: 'button' }, tr("Отмена"));
  const reset = el('button', { id: 'mobile-nav-reset', class: 'btn-link', type: 'button' }, tr("Стандартный порядок"));
  const inputs = tabs.map(tab => {
    const section = tab.dataset.section;
    const input = el('input', { type: 'checkbox', value: section, 'data-nav-section': section });
    choices.append(el('label', {}, input, names.get(section)));
    return input;
  });
  editor.append(modes, choices, count, el('div', { class: 'actions' }, save, cancel), reset);
  const heading = el('h2', { id: 'mobile-sections-title' }, tr("Ещё"));
  const dialog = el('dialog', { id: 'mobile-sections', class: 'dialog mobile-sections',
    'aria-labelledby': 'mobile-sections-title' },
    el('div', { class: 'mobile-sections-head' }, heading, close), links, edit, editor);
  document.body.append(dialog);
  let returnFocus = more;

  function setActive(section) {
    active = section;
    const secondary = mode !== 'all' && names.has(active) && !primary.includes(active);
    label.textContent = secondary ? tr("Ещё: {0}", names.get(active)) : tr("Ещё");
    more.setAttribute('aria-label', secondary ? tr("Ещё: {0}", names.get(active)) : tr("Ещё · все разделы"));
    if (secondary) more.setAttribute('aria-current', 'page');
    else more.removeAttribute('aria-current');
    for (const link of links.children) {
      if (link.dataset.section === active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    document.dispatchEvent(new Event('studio-navigation'));
  }

  function paint() {
    rail.classList.toggle('mobile-all', mode === 'all');
    more.hidden = mode === 'all';
    for (const tab of tabs) tab.classList.toggle('mobile-secondary', mode !== 'all' && !primary.includes(tab.dataset.section));
    links.replaceChildren(...tabs.filter(tab => !primary.includes(tab.dataset.section)).map(tab => {
      const link = el('a', { href: tab.getAttribute('href'), 'data-section': tab.dataset.section },
        el('span', { class: 'section-icon' }, icon(tab.dataset.section),
          el('span', { class: 'section-running-count', 'aria-hidden': 'true', hidden: true })),
        el('span', {}, names.get(tab.dataset.section)));
      return link;
    }));
    setActive(active);
  }

  function showEditor(show) {
    heading.textContent = show ? tr("Нижняя панель") : tr("Ещё");
    editor.hidden = !show;
    links.hidden = edit.hidden = show;
    if (show) {
      compact.checked = mode === 'compact';
      all.checked = mode === 'all';
      for (const input of inputs) input.checked = primary.includes(input.value);
      updateCount();
      (all.checked ? all : compact).focus();
    } else if (dialog.open) {
      edit.focus();
    }
  }

  function updateCount() {
    const selected = inputs.filter(input => input.checked).length;
    choices.hidden = all.checked;
    count.textContent = (all.checked ? tr("Все разделы видны в нижней панели.") : tr("Выбрано {0} из 3–4.", selected))
      + tr(" Настройка только этого браузера и учётки.");
    save.disabled = !all.checked && (selected < 3 || selected > 4);
    for (const input of inputs) input.disabled = all.checked || selected === 4 && !input.checked;
  }

  function store(next, nextMode = 'compact') {
    primary = next;
    mode = nextMode;
    try {
      localStorage.setItem(key, JSON.stringify(primary));
      localStorage.setItem(modeKey, mode);
    } catch { /* До закрытия окна. */ }
    paint();
    showEditor(false);
    if (mode === 'all') dialog.close();
  }

  more.addEventListener('click', () => {
    returnFocus = more;
    showEditor(false);
    dialog.showModal();
    more.setAttribute('aria-expanded', 'true');
  });
  openEditor = (from) => {      // «Настройки → Оформление»: фокус после закрытия — на открывшую кнопку
    returnFocus = from;
    dialog.showModal();
    showEditor(true);
  };
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    more.setAttribute('aria-expanded', 'false');
    (mode === 'all' && returnFocus === more ? document.getElementById('avatar') : returnFocus).focus();
  });
  links.addEventListener('click', event => { if (event.target.closest('a')) dialog.close(); });
  edit.addEventListener('click', () => showEditor(true));
  cancel.addEventListener('click', () => showEditor(false));
  reset.addEventListener('click', () => store([...DEFAULT]));
  editor.addEventListener('change', updateCount);
  editor.addEventListener('submit', event => {
    event.preventDefault();
    const selected = inputs.filter(input => input.checked).map(input => input.value);
    if (all.checked) store(primary, 'all');
    else if (selected.length >= 3 && selected.length <= 4) store(selected);
  });

  // Высота следует реальному переносу подписей, включая увеличенный текст.
  function measure() {
    document.documentElement.style.setProperty('--tabs-h', mobile.matches ? `${rail.offsetHeight}px` : '0px');
  }
  new ResizeObserver(measure).observe(rail);
  mobile.addEventListener('change', () => { if (!mobile.matches && dialog.open) dialog.close(); measure(); });
  paint();
  measure();
  return { setActive };
}

/** Настройка нижней панели из «Настройки → Оформление» (setup ещё не прошёл — ничего). */
export function openMobileNavigationEditor(from) {
  if (openEditor) openEditor(from);
}
