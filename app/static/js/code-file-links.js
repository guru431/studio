/* Local links never become HTTP requests containing machine paths. */
export function localFileHref(value) {
  if (typeof value !== 'string') return null;
  if (/^https?:/i.test(value)) {
    try {
      const url = new URL(value);
      if (url.origin !== location.origin) return null;
      value = url.pathname;
    } catch { return null; }
  }
  value = value.split('#')[0].replace(/^file:\/\/+([a-z]:)/i, '$1');
  let decoded = value;
  try { decoded = decodeURIComponent(value); } catch { /* malformed links remain local and cannot become HTTP */ }
  if (!/^\/?[a-z]:[\\/]/i.test(decoded)) return null;
  return '/' + value.replace(/^\//, '').replaceAll('\\', '/');
}

export function visualizationFilePath(href) {
  const local = localFileHref(href);
  if (!local) return null;
  let path;
  try { path = decodeURIComponent(local).slice(1); } catch { return null; }
  if (!path.toLowerCase().endsWith('.html') || path.length > 1024 || /[\x00-\x1f\x7f?"<>|]/.test(path)
      || path.split('/').some(part => part === '..' || part === '.')) return null;
  // Разрешённый корень повторно проверяет мост; путь никогда не попадает в URL запроса.
  return path;
}

export function projectFilePath(href, agent, project) {
  if (typeof href !== 'string' || !agent || !project) return null;
  const local = localFileHref(href);
  let path;
  if (local) {
    if (typeof agent.projects_root !== 'string') return null;
    const root = `/${agent.projects_root.replaceAll('\\', '/').replace(/\/+$/, '')}/${project.name}/`;
    let decoded;
    try { decoded = decodeURIComponent(local); } catch { return null; }
    if (!decoded.toLowerCase().startsWith(root.toLowerCase())) return null;
    path = decoded.slice(root.length);
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
