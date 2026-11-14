/* Fixed interface strings only. Never scan generated content, messages or filenames. */
(function () {
  'use strict';
  const KEY = 'studio-language';
  let language = 'ru';
  try { if (localStorage.getItem(KEY) === 'en') language = 'en'; } catch { /* Russian without storage. */ }
  const english = globalThis.StudioEnglish || {};
  const locale = () => language === 'en' ? 'en-US' : 'ru-RU';

  function format(pattern, values) {
    if (!values.length) return pattern;
    return pattern.replace(/\{(\d+)\}/g, (match, index) => index < values.length ? String(values[index]) : match);
  }

  function tr(source, ...values) {
    const pattern = language === 'en' && Object.hasOwn(english, source) ? english[source] : source;
    return format(pattern, values);
  }

  let patterns;
  function message(value) {
    const source = String(value ?? '');
    if (language !== 'en') return source;
    if (Object.hasOwn(english, source)) return english[source];
    // Server errors may start with either case. Only their fixed wording is translated.
    const lower = source.charAt(0).toLowerCase() + source.slice(1);
    if (Object.hasOwn(english, lower)) {
      const out = english[lower];
      return source === lower ? out : out.charAt(0).toUpperCase() + out.slice(1);
    }
    if (source.length > 4096) return source;
    patterns ||= (globalThis.StudioEnglishMessages || []).map((key) => {
      const slots = [];
      let next = 0;
      const parts = key.split(/(\{\d*\})/g).map((part) => {
        if (/^\{\d*\}$/.test(part)) {
          slots.push(part === '{}' ? next++ : Number(part.slice(1, -1)));
          return '([\\s\\S]*?)';
        }
        return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      });
      return { pattern: new RegExp('^' + parts.join('') + '$', 'iu'), key, slots };
    });
    for (const { pattern, key, slots } of patterns) {
      const match = pattern.exec(source);
      if (!match) continue;
      const values = [];
      slots.forEach((index, capture) => { values[index] = match[capture + 1]; });
      let auto = 0;
      return format(english[key].replace(/\{\}/g, () => '{' + auto++ + '}'), values);
    }
    return source;
  }

  function choose(select, status) {
    select.value = language;
    select.addEventListener('change', () => {
      const value = select.value;
      if (!['ru', 'en'].includes(value) || value === language) return;
      try { localStorage.setItem(KEY, value); }
      catch {
        select.value = language;
        status.textContent = tr('Браузер не разрешает сохранить язык. Разрешите хранилище сайта и повторите.');
        return;
      }
      location.reload();
    });
  }

  function languagePicker(id = 'interface-language') {
    const box = document.createElement('div');
    box.className = 'field interface-language';
    const label = document.createElement('label');
    label.htmlFor = id;
    label.textContent = tr('Язык интерфейса / Language');
    const select = document.createElement('select');
    select.id = id;
    select.className = 'input';
    select.autocomplete = 'off';
    for (const [value, name] of [['ru', 'Русский'], ['en', 'English']]) {
      const option = document.createElement('option');
      option.value = value;
      option.lang = value;
      option.textContent = name;
      select.append(option);
    }
    const status = document.createElement('p');
    status.className = 'error';
    status.setAttribute('role', 'status');
    choose(select, status);
    box.append(label, select, status);
    return box;
  }

  globalThis.StudioI18n = Object.freeze({ tr, message, locale, language: () => language, languagePicker });
  if (typeof document === 'undefined') return;
  document.documentElement.lang = language;
  document.addEventListener('DOMContentLoaded', () => {
    for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = tr(node.textContent);
    for (const node of document.querySelectorAll('[data-i18n-error]')) node.textContent = message(node.textContent);
    for (const node of document.querySelectorAll('[data-i18n-attrs]')) {
      for (const attribute of node.dataset.i18nAttrs.split(' ')) {
        node.setAttribute(attribute, tr(node.getAttribute(attribute)));
      }
    }
    for (const host of document.querySelectorAll('[data-language-picker]')) {
      host.append(languagePicker(host.dataset.languagePicker));
    }
  }, { once: true });
})();
