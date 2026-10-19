/* Чтение потока text/event-stream из ответа fetch (EventSource не умеет POST и заголовки).
 *
 * for await (const { event, data } of readSSE(response)) — event без поля `event:` — 'message',
 * data — JSON, если разбирается, иначе строка (например '[DONE]'). Комментарии (': ping') и
 * события без данных пропускаются. Выход из цикла (break) обрывает поток. */

function parse(block) {
  let event = 'message';
  const data = [];
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (!data.length) return null;
  const text = data.join('\n');
  try {
    return { event, data: JSON.parse(text) };
  } catch {
    return { event, data: text };
  }
}

export async function* readSSE(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  let cr = '';           // \r в конце куска: следующий может начаться с \n
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      let text = cr + value;
      cr = text.endsWith('\r') ? '\r' : '';
      if (cr) text = text.slice(0, -1);
      buffer += text.replace(/\r\n?/g, '\n');
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const item = parse(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (item) yield item;
      }
    }
    const tail = parse(buffer);      // поток кончился без пустой строки в конце
    if (tail) yield tail;
  } finally {
    reader.cancel().catch(() => {});
  }
}
