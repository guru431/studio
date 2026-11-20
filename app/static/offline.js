// Service Worker возвращает оболочку на исходный адрес навигации. Reload сохраняет путь и фрагмент.
// `data-reason="restart"` у <body> — прокси ответил 502/503/504 (выкладка, перезапуск), а не обрыв связи.
// Слова меняются до DOMContentLoaded (скрипт defer): data-i18n их ещё переведёт.
const retry = () => location.reload();
document.getElementById('retry').addEventListener('click', retry);
if (document.body.dataset.reason === 'restart') {
  document.title = 'Студия перезапускается';
  document.querySelector('.brand p').textContent = 'Студия перезапускается и откроется сама, когда будет готова.';
}

/** Служба отвечает — открыть адрес заново. Без сети не спрашивает. */
async function healthy() {
  if (!navigator.onLine) return false;
  try {
    const response = await fetch('/health', { method: 'HEAD', cache: 'no-store',
      credentials: 'omit', signal: AbortSignal.timeout(2000) });
    return response.ok;
  } catch {
    return false;
  }
}

let reconnecting = false;
window.addEventListener('online', async () => {
  if (reconnecting) return;
  reconnecting = true;
  try {
    // Браузер сообщает о появлении сети раньше, чем путь до Studio может стать доступен.
    for (const delay of [0, 350, 1250]) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      if (!navigator.onLine) return;
      if (await healthy()) { retry(); return; }
    }
  } finally {
    reconnecting = false;
  }
});

// Сеть формально есть, а служба не отвечала (перезапуск, обрыв за роутером) — события `online` не будет: раз в
// POLL_MS, пока страница на экране. Сама /offline.html (открыта по прямому адресу) себя не перезагружает.
const POLL_MS = 5000;
setInterval(async () => {
  if (reconnecting || document.visibilityState !== 'visible' || location.pathname === '/offline.html') return;
  reconnecting = true;
  try {
    if (await healthy()) retry();
  } finally {
    reconnecting = false;
  }
}, POLL_MS);
