/* Концы строк файла после правки в textarea: она отдаёт только \n, а файл бывает со смешанными CRLF, LF и CR. */

/** Неизменённые строки в начале и конце сохраняют прежний разделитель, изменённые — самый частый в файле. */
export function restoreEndings(original, edited) {
  const seps = original.match(/\r\n|\r|\n/g) || [];
  if (seps.every((sep) => sep === '\n')) return edited;
  const counts = new Map();
  for (const sep of seps) counts.set(sep, (counts.get(sep) || 0) + 1);
  const usual = [...counts].sort((a, b) => b[1] - a[1])[0][0];
  const before = original.split(/\r\n|\r|\n/);
  const after = edited.split('\n');
  const limit = Math.min(before.length, after.length) - 1;
  let head = 0;
  while (head < limit && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < limit - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
  let out = after[0];
  for (let j = 0; j < after.length - 1; j++) {
    const sep = j < head ? seps[j] : j >= after.length - 1 - tail ? seps[j + before.length - after.length] : usual;
    out += sep + after[j + 1];
  }
  return out;
}
