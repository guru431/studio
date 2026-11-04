/* На телефоне — 3–4 личных раздела и «Ещё». Исходный рельс остаётся на компьютере. */
import { el } from './ui.js';

const DEFAULT = ['chat', 'photo', 'code'];

export function setupMobileNavigation(me) {
  const rail = document.getElementById('rail');
  const tabs = [...rail.querySelectorAll('a.tab[data-section]')];
  const names = new Map(tabs.map(tab => [tab.dataset.section, tab.lastElementChild.textContent]));
  const key = `studio-mobile-nav:${me.id}`;
  const mobile = matchMedia('(max-width: 760px)');
  let active = '';
  let primary = [...DEFAULT];
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (Array.isArray(saved) && saved.length >= 3 && saved.length <= 4
      && new Set(saved).size === saved.length && saved.every(section => names.has(section))) primary = saved;
  } catch { /* Закрытое или повреждённое хранилище — стандартные разделы. */ }

  const label = el('span', { id: 'mobile-more-label' }, 'Ещё');
  const more = el('button', { id: 'mobile-more', class: 'mobile-more', type: 'button',
    'aria-haspopup': 'dialog', 'aria-controls': 'mobile-sections', 'aria-expanded': 'false' },
    el('span', { class: 'mobile-more-icon', 'aria-hidden': 'true' }, '•••'), label);
  rail.insertBefore(more, rail.querySelector('.spacer'));
  const close = el('button', { id: 'mobile-sections-close', class: 'btn-link', type: 'button' }, 'Закрыть');
  const links = el('nav', { id: 'mobile-section-links', 'aria-label': 'Остальные разделы' });
  const edit = el('button', { id: 'mobile-nav-edit', class: 'btn', type: 'button' }, 'Настроить нижние вкладки');
  const editor = el('form', { id: 'mobile-nav-editor', hidden: true });
  const choices = el('fieldset', {}, el('legend', {}, 'Выберите 3–4 основных раздела'));
  const count = el('p', { class: 'muted', role: 'status', 'aria-live': 'polite' });
  const save = el('button', { id: 'mobile-nav-save', class: 'btn btn-accent', type: 'submit' }, 'Сохранить');
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Отмена');
  const reset = el('button', { id: 'mobile-nav-reset', class: 'btn-link', type: 'button' }, 'Стандартный порядок');
  const inputs = tabs.map(tab => {
    const section = tab.dataset.section;
    const input = el('input', { type: 'checkbox', value: section, 'data-nav-section': section });
    choices.append(el('label', {}, input, names.get(section)));
    return input;
  });
  editor.append(choices, count, el('div', { class: 'actions' }, save, cancel), reset);
  const dialog = el('dialog', { id: 'mobile-sections', class: 'dialog mobile-sections',
    'aria-labelledby': 'mobile-sections-title' },
    el('div', { class: 'mobile-sections-head' }, el('h2', { id: 'mobile-sections-title' }, 'Ещё'), close), links, edit, editor);
  document.body.append(dialog);

  function setActive(section) {
    active = section;
    const secondary = names.has(active) && !primary.includes(active);
    label.textContent = secondary ? `Ещё: ${names.get(active)}` : 'Ещё';
    more.setAttribute('aria-label', secondary ? `Ещё: ${names.get(active)}` : 'Ещё · все разделы');
    if (secondary) more.setAttribute('aria-current', 'page');
    else more.removeAttribute('aria-current');
    for (const link of links.children) {
      if (link.dataset.section === active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  }

  function paint() {
    for (const tab of tabs) tab.classList.toggle('mobile-secondary', !primary.includes(tab.dataset.section));
    links.replaceChildren(...tabs.filter(tab => !primary.includes(tab.dataset.section)).map(tab => {
      const link = el('a', { href: tab.getAttribute('href'), 'data-section': tab.dataset.section },
        tab.querySelector('svg').cloneNode(true), el('span', {}, names.get(tab.dataset.section)));
      return link;
    }));
    setActive(active);
  }

  function showEditor(show) {
    editor.hidden = !show;
    links.hidden = edit.hidden = show;
    if (show) {
      for (const input of inputs) input.checked = primary.includes(input.value);
      updateCount();
      inputs.find(input => !input.disabled)?.focus();
    } else if (dialog.open) {
      edit.focus();
    }
  }

  function updateCount() {
    const selected = inputs.filter(input => input.checked).length;
    count.textContent = `Выбрано ${selected} из 3–4. Настройка только этого браузера и учётки.`;
    save.disabled = selected < 3 || selected > 4;
    for (const input of inputs) input.disabled = selected === 4 && !input.checked;
  }

  function store(next) {
    primary = next;
    try { localStorage.setItem(key, JSON.stringify(primary)); } catch { /* До закрытия окна. */ }
    paint();
    showEditor(false);
  }

  more.addEventListener('click', () => {
    showEditor(false);
    dialog.showModal();
    more.setAttribute('aria-expanded', 'true');
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => more.setAttribute('aria-expanded', 'false'));
  links.addEventListener('click', event => { if (event.target.closest('a')) dialog.close(); });
  edit.addEventListener('click', () => showEditor(true));
  cancel.addEventListener('click', () => showEditor(false));
  reset.addEventListener('click', () => store([...DEFAULT]));
  editor.addEventListener('change', updateCount);
  editor.addEventListener('submit', event => {
    event.preventDefault();
    const selected = inputs.filter(input => input.checked).map(input => input.value);
    if (selected.length >= 3 && selected.length <= 4) store(selected);
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
