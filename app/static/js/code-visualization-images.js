/* Runs inside the visualization sandbox, where the parent session and filesystem are inaccessible. */
(() => {
  const images = new Map();
  for (const [paths, source] of JSON.parse(document.currentScript.previousElementSibling.textContent)) {
    for (const path of paths) images.set(path, source);
  }
  for (const img of document.querySelectorAll('img[data-studio-visualization-src]')) {
    const source = images.get(img.dataset.studioVisualizationSrc);
    if (source) img.src = source;
  }

  document.addEventListener('click', (event) => {
    const link = event.target?.closest?.('a[data-studio-visualization-href]');
    const source = images.get(link?.dataset.studioVisualizationHref);
    if (event.defaultPrevented || !source) return;
    event.preventDefault();

    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'margin:auto;max-width:calc(100% - 24px);max-height:calc(100% - 24px);overflow:auto;padding:12px';
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', document.documentElement.lang === 'ru' ? 'Закрыть изображение' : 'Close image');
    close.style.cssText = 'position:sticky;top:0;left:0;min-width:32px;min-height:32px';
    close.addEventListener('click', () => dialog.close());

    const image = document.createElement('img');
    image.alt = link.querySelector('img')?.alt || link.textContent.trim();
    image.style.cssText = 'display:block;max-width:none;width:auto;height:auto';
    image.src = source;
    dialog.setAttribute('aria-label', image.alt);
    dialog.append(close, image);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
})();
