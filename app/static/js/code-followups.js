/* Позиция уточнения — число уже показанных шагов и UTF-16 символов текста помощника. */
export function withFollowups(parts, followups, textLength, stepCount) {
  const pending = (Array.isArray(followups) ? followups : []).filter((row) => typeof row?.id === 'string').map((data) => {
    const p = data.position;
    const valid = Number.isInteger(p?.text) && p.text >= 0 && p.text <= textLength
      && Number.isInteger(p?.steps) && p.steps >= 0 && p.steps <= stepCount;
    return { data, position: valid ? p : { text: textLength, steps: stepCount } };
  }).sort((a, b) => a.position.steps - b.position.steps || a.position.text - b.position.text);
  const ordered = [];
  let steps = 0, offset = 0;
  function insert() { ordered.push({ kind: 'followup', data: pending.shift().data }); }
  for (const part of parts) {
    if (part.kind === 'step') {
      while (pending.length && pending[0].position.steps <= steps && pending[0].position.text <= offset) insert();
      ordered.push(part);
      steps++;
    } else {
      let start = part.start;
      while (pending.length && pending[0].position.steps <= steps && pending[0].position.text <= part.end) {
        const cut = Math.max(start, pending[0].position.text);
        if (cut > start) ordered.push({ ...part, start, end: cut });
        insert();
        start = cut;
      }
      if (start < part.end || (start === part.start && !ordered.length)) ordered.push({ ...part, start });
      offset = part.end;
    }
  }
  while (pending.length) insert();
  return ordered;
}
