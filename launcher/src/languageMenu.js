import { api, esc, icon, refreshIcons, errText, toast } from './util.js';
import { getLang, langName, listLangs, setLang } from './i18n.js';

/*
 * The language switcher at the foot of the sidebar. It is labelled with the current language's own name, and its
 * tooltip is not translated, so that someone who cannot read the current language can still find it. The choice
 * covers the launcher and the in-game F1 menu.
 */

const TITLE = 'Language / 语言';

let close = null; // shuts the open list; null while it is shut

async function choose(code, redraw) {
  // saved even when it is the language on show: that one may be the system's, and picking it keeps it
  try {
    await api('set_language', { code });
  } catch (e) {
    toast(errText(e), 'error');
  }
  await setLang(code, redraw);
}

function open(button, redraw) {
  const list = document.createElement('div');
  list.className = 'lang-menu';
  list.setAttribute('role', 'menu');
  list.setAttribute('aria-label', TITLE);
  list.innerHTML = listLangs()
    .map((l) => {
      const on = l.code === getLang();
      return `<button class="nav-item ${on ? 'active' : ''}" role="menuitemradio" aria-checked="${on}" lang="${esc(l.code === 'zh' ? 'zh-CN' : l.code)}" data-lang="${esc(l.code)}">${icon('check', on ? '' : 'blank')}<span>${esc(l.name)}</span></button>`;
    })
    .join('');
  document.body.append(list);
  // above the button, from its left edge
  const r = button.getBoundingClientRect();
  list.style.left = `${r.left}px`;
  list.style.bottom = `${window.innerHeight - r.top + 4}px`;
  button.setAttribute('aria-expanded', 'true');
  refreshIcons();

  const outside = (ev) => {
    if (!list.contains(ev.target) && !button.contains(ev.target)) close();
  };
  const escape = (ev) => {
    if (ev.key !== 'Escape') return;
    close();
    button.focus();
  };
  close = () => {
    list.remove();
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside);
    document.removeEventListener('keydown', escape);
    close = null;
  };
  document.addEventListener('pointerdown', outside);
  document.addEventListener('keydown', escape);
  list.querySelectorAll('[data-lang]').forEach((b) =>
    b.addEventListener('click', () => {
      close();
      choose(b.dataset.lang, redraw);
    }),
  );
  (list.querySelector('.active') || list.firstElementChild).focus();
}

/** The sidebar's last elements: a spacer and the button. `redraw` repaints the launcher after a switch. */
export function languageButton(redraw) {
  if (close) close(); // the sidebar is being redrawn under an open list
  const spacer = document.createElement('div');
  spacer.className = 'spacer';
  const button = document.createElement('button');
  button.className = 'nav-item';
  button.title = TITLE;
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = `${icon('languages')}<span class="label">${esc(langName(getLang()))}</span>`;
  button.addEventListener('click', () => (close ? close() : open(button, redraw)));
  return [spacer, button];
}
