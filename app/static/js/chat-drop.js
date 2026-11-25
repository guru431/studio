/* Файлы в поле «Чата» из буфера и перетаскиванием (спека 2026-11-25-studio-skills-status-local-models, п. 24) — тем
 * же путём, что скрепка: `add(files)`. Вставка без файлов (текст) и перетаскивание не файлов — обычные. */
export function fileDrop(form, add) {
  form.addEventListener('paste', (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    add(files);
  });
  form.addEventListener('dragover', (event) => {
    if ([...(event.dataTransfer?.types || [])].includes('Files')) event.preventDefault();
  });
  form.addEventListener('drop', (event) => {
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    add(files);
  });
}
