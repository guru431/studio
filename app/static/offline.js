// Service Worker возвращает оболочку на исходный адрес навигации. Reload сохраняет путь и фрагмент.
const retry = () => location.reload();
document.getElementById('retry').addEventListener('click', retry);

let reconnecting = false;
window.addEventListener('online', async () => {
  if (reconnecting) return;
  reconnecting = true;
  try {
    // Браузер сообщает о появлении сети раньше, чем путь до Studio может стать доступен.
    for (const delay of [0, 350, 1250]) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      if (!navigator.onLine) return;
      try {
        const response = await fetch('/health', { method: 'HEAD', cache: 'no-store',
          credentials: 'omit', signal: AbortSignal.timeout(2000) });
        if (response.ok) { retry(); return; }
      } catch { /* проверим ещё раз после паузы */ }
    }
  } finally {
    reconnecting = false;
  }
});
