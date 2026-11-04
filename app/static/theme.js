/* Тема до первой отрисовки: последняя выбранная, из localStorage. Значение профиля (/api/me)
 * потом ставит app.js и оно главнее. Скрипт обычный, не модуль: исполняется сразу, в <head>. */
(function () {
  try {
    var theme = localStorage.getItem('studio-theme');
    if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
    var font = Number(localStorage.getItem('studio-font-size')) || 100;
    var scale = Number(localStorage.getItem('studio-ui-scale')) || 100;
    if (font >= 85 && font <= 150) document.documentElement.style.setProperty('--font-scale', font / 100);
    if ([50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100].indexOf(scale) !== -1) {
      document.documentElement.style.setProperty('--ui-scale', scale / 100);
      document.documentElement.style.zoom = String(scale / 100);
    }
  } catch (e) {
    // хранилище закрыто (приватный режим) — тема по настройке устройства
  }
  // Start the shell and selected section in parallel, before their import waterfall.
  // This downloads public modules; it does not execute the section or read its API.
  var script = document.currentScript;
  if (script && script.dataset.modulePreloads) {
    try {
      var modules = JSON.parse(script.dataset.modulePreloads);
      var section = /^#\/([a-z]+)/.exec(location.hash);
      var selected = section ? section[1] : 'chat';
      var loaded = new Set();
      script.preloadSection = function (name) {
        var paths = modules.shell.concat(Array.isArray(modules[name]) ? modules[name] : []);
        if (name === 'code' && /^#\/code\/\d+\/[a-f0-9]{16}\/[a-f0-9-]{36}(?:\?|$)/.test(location.hash)) {
          paths = paths.concat(modules['code-chat']);
        }
        paths.forEach(function (path) {
          if (loaded.has(path)) return;
          loaded.add(path);
          var link = document.createElement('link');
          link.rel = 'modulepreload';
          link.href = new URL(path, script.src).href;
          link.crossOrigin = 'anonymous';
          document.head.append(link);
        });
        if (paths.indexOf('js/markdown.js') !== -1) {
          modules.markdown.forEach(function (path) {
            if (loaded.has(path)) return;
            loaded.add(path);
            var link = document.createElement('link');
            link.rel = 'preload';
            link.as = 'script';
            link.crossOrigin = 'anonymous';
            link.href = new URL(path, script.src).href;
            document.head.append(link);
          });
        }
      };
      script.preloadSection(selected);
      delete script.dataset.modulePreloads;
    } catch (e) {
      // An unavailable preload hint must never prevent the normal module loader.
    }
  }
})();
