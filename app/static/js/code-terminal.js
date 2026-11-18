import { api } from './api.js';
import { tr } from './i18n.js';
import { el, words } from './ui.js';

export function terminalView(host, { locked }) {
  const title = el('h2', { id: 'code-terminal-title' });
  const content = el('div', { class: 'code-computer-content', 'aria-live': 'polite' });
  const close = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
  const dialog = el('dialog', { id: 'code-terminal-dialog', class: 'dialog code-computer-dialog',
    'aria-labelledby': 'code-terminal-title' }, title, content, el('footer', { class: 'actions' }, close));
  let controller = null;
  host.append(dialog);
  function clear() {
    controller?.abort();
    controller = null;
    content.replaceChildren();
  }
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (!dialog.open) clear(); });
  function conceal() {
    if (dialog.open && (document.hidden || document.documentElement.classList.contains('shrouded'))) {
      clear();
      dialog.close();
    }
  }
  const observer = new MutationObserver(conceal);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  document.addEventListener('visibilitychange', conceal);
  async function show(agent) {
    clear();
    title.textContent = tr("Терминал Linux · {0}", agent.name);
    if (!dialog.open) dialog.showModal();
    if (!agent.online || !agent.terminal) {
      content.textContent = !agent.online ? tr("Компьютер не на связи. Запустите Studio Agent, чтобы получить адрес терминала.")
        : tr("Обновите Studio Agent на Linux, чтобы открыть терминал.");
      return;
    }
    controller = new AbortController();
    const { signal } = controller;
    content.textContent = tr("Загрузка…");
    try {
      const data = await api('GET', `/api/code/agents/${agent.id}/terminal`, undefined, { signal });
      if (signal.aborted || !dialog.open) return;
      content.replaceChildren();
      if (!data.ssh_uri && !data.web_url) {
        content.textContent = tr("На Linux откройте studio-agent menu и укажите адрес в пункте «SSH и веб-консоль».");
        return;
      }
      if (data.ssh_uri) {
        const command = el('input', { type: 'text', readOnly: true, value: data.ssh_command,
          'aria-label': tr("Команда SSH"), spellcheck: 'false' });
        const copy = el('button', { type: 'button', class: 'btn' }, tr("Скопировать команду SSH"));
        copy.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(command.value);
            copy.textContent = tr("Скопировано");
          } catch {
            command.focus();
            command.select();
            copy.textContent = tr("Скопируйте выделенную команду");
          }
        });
        content.append(command, el('div', { class: 'actions' }, copy,
          el('a', { class: 'btn', href: data.ssh_uri, rel: 'noreferrer' }, tr("Открыть SSH-клиент"))));
      }
      if (data.web_url) content.append(el('code', { class: 'code-terminal-url' }, data.web_url),
        el('a', { class: 'btn', href: data.web_url, target: '_blank',
          rel: 'noopener noreferrer', referrerpolicy: 'no-referrer' }, tr("Открыть веб-консоль")));
      content.append(el('p', { class: 'muted small' },
        tr("Войдите с учётной записью Linux. Если компьютер в частной сети, подключитесь к этой сети или к VPN.")));
    } catch (error) {
      if (signal.aborted || !dialog.open) return;
      if (error.code === 'closed') { dialog.close(); locked(agent.id); }
      else content.textContent = words(error);
    }
  }
  return { show, unmount() {
    clear();
    observer.disconnect();
    document.removeEventListener('visibilitychange', conceal);
    dialog.remove();
  } };
}
