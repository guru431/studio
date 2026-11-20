/* Local links never become HTTP requests containing machine paths. */
export function localFileHref(value) {
  if (typeof value !== 'string') return null;
  let web = false;
  if (/^https?:/i.test(value)) {
    try {
      const url = new URL(value);
      if (url.origin !== location.origin) return null;
      value = url.pathname;
      web = true;
    } catch { return null; }
  }
  value = value.split('#')[0].replace(/^file:\/\/+([a-z]:)/i, '$1').replace(/^file:\/\/(?=\/)/i, '');
  let decoded = value;
  try { decoded = decodeURIComponent(value); } catch { /* malformed links remain local and cannot become HTTP */ }
  // POSIX: абсолютный путь моста macOS/Linux (`/Users/…`, `/home/…`); адрес своего сайта — только с буквой диска.
  if (!/^\/?[a-z]:[\\/]/i.test(decoded) && (web || !/^\/(?![\/\\])/.test(decoded))) return null;
  return '/' + value.replace(/^\//, '').replaceAll('\\', '/');
}

function drive(path) {
  return /^\/[a-z]:\//i.test(path);
}

export function visualizationFilePath(href) {
  const local = localFileHref(href);
  if (!local) return null;
  let path;
  try { path = decodeURIComponent(local); } catch { return null; }
  if (drive(path)) path = path.slice(1);
  const image = /\.(?:png|jpe?g|webp|gif)$/i.test(path) && path.split('/').some(part => part.toLowerCase() === 'generated_images');
  if ((!path.toLowerCase().endsWith('.html') && !image) || path.length > 1024 || /[\x00-\x1f\x7f?"<>|]/.test(path)
      || path.split('/').some(part => part === '..' || part === '.')) return null;
  // Разрешённый корень повторно проверяет мост; путь никогда не попадает в URL запроса.
  return path;
}

export function projectFilePath(href, agent, project) {
  if (typeof href !== 'string' || !agent || !project) return null;
  const local = localFileHref(href);
  let path;
  if (local) {
    // `project.dir` — настоящая папка от моста (второй корень, одноимённые проекты); прежний мост — первый корень + имя.
    const folder = typeof project.dir === 'string' ? project.dir
      : typeof agent.projects_root === 'string' && agent.projects_root
        ? `${agent.projects_root.replace(/[\\/]+$/, '')}/${project.name}` : null;
    let decoded;
    try { decoded = decodeURIComponent(local); } catch { return null; }
    // Регистр не различают только пути Windows; на macOS/Linux `Alpha` и `alpha` — разные папки.
    const fold = drive(folder === null ? decoded : `/${folder.replaceAll('\\', '/')}`)
      ? (text) => text.toLowerCase() : (text) => text;
    if (folder === null) {
      // Участнику мост не отдаёт ни корня, ни папки, а путь в шаге он видит: проект — `<корень>/<папка>/…`, папка —
      // первая с его именем (у одноимённых подпись «имя · родитель · хвост id» — ещё и с родителем). Чужой файл
      // проекта так не откроется: путь проверяет мост в пределах проекта.
      const [base, parent] = String(project.name).split(' · ');
      const parts = decoded.split('/');
      const at = parts.findIndex((part, i) => i > 0 && fold(part) === fold(base)
        && (parent === undefined || fold(parts[i - 1]) === fold(parent)));
      if (at < 0) return null;
      path = parts.slice(at + 1).join('/');
    } else {
      const root = `/${folder.replaceAll('\\', '/').replace(/^\/+|\/+$/g, '')}/`;
      if (!fold(decoded).startsWith(fold(root))) return null;
      path = decoded.slice(root.length);
    }
  } else {
    if (/^(?:[\/#?]|[a-z][a-z\d+.-]*:)/i.test(href)) return null;
    try { path = decodeURIComponent(href.split('#')[0]); } catch { return null; }
  }
  path = path.replace(/:\d+(?::\d+)?$/, '').replaceAll('\\', '/');
  while (path.startsWith('./')) path = path.slice(2);
  if (!path || path.length > 1024 || /[\x00-\x1f\x7f:*?"<>|]/.test(path)
      || path.split('/').some((part) => !part || part === '.' || part === '..')) return null;
  return path;
}
