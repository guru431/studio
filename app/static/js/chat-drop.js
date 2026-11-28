export function isFileDrag(event) {
  return Array.from(event.dataTransfer?.types || []).includes('Files');
}

/** A drop uses the same file handler as the picker. Nested targets take precedence. */
export function dropFiles(target, add, enabled = () => true) {
  const highlight = 'file-drop-active';
  target.addEventListener('dragover', (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    const accepts = enabled();
    event.dataTransfer.dropEffect = accepts ? 'copy' : 'none';
    target.classList.toggle(highlight, accepts);
  });
  target.addEventListener('dragleave', (event) => {
    if (!(event.relatedTarget instanceof Node && target.contains(event.relatedTarget))) target.classList.remove(highlight);
  });
  target.addEventListener('drop', (event) => {
    target.classList.remove(highlight);
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) return;
    event.preventDefault();
    event.stopPropagation();
    if (enabled()) add(files);
  });
}

/* Буфер с текстом (например, таблица Excel с картинкой) вставляется как текст. */
export function fileDrop(form, add, target = form, enabled) {
  form.addEventListener('paste', (event) => {
    if (event.clipboardData?.getData('text/plain') || (enabled && !enabled())) return;
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    add(files);
  });
  dropFiles(target, add, enabled);
}
