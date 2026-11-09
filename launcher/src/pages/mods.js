import { api, esc, icon, refreshIcons, toast, errText } from '../util.js';

const VALID_ID = /^[a-z][a-z0-9-]{0,63}$/;

function suggestedId(path) {
  const stem = path.split(/[\\/]/).pop().replace(/\.ips(?:32)?$/i, '');
  if (/^[a-f0-9]{32,64}$/i.test(stem)) return '';
  return stem.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
}

export async function render(el, ctx) {
  const cfg = await api('get_config');
  let mods = null;
  let setup = null;
  let error = '';
  let pending = '';
  let formError = '';
  let draft = { id: '', name: '' };
  let busy = false;

  async function reload() {
    setup = await api('setup_status');
    if (!cfg.dump_dir) {
      mods = null;
      error = 'Choose your game files in Setup before importing mods.';
      return;
    }
    try {
      mods = await api('list_mods');
      error = '';
    } catch (e) {
      mods = null;
      error = errText(e);
    }
  }

  function draw() {
    const entries = mods?.mods ?? [];
    const enabled = entries.filter((m) => m.enabled && m.type === 'exefs-ips').length;
    const buildReady = setup?.complete;
    const setupRunning = setup?.progress?.state === 'running';
    const installing = Boolean(pending);
    el.innerHTML = `
      <div class="page">
        <div class="hero">
          <h1>Mods</h1>
          <p class="dim">Import ExeFS patches or supported runtime mods. Run Setup for IPS changes; restart the game for runtime changes.</p>
        </div>
        ${error ? `<div class="banner danger">${icon('circle-alert')}<div>${esc(error)}</div></div>` : ''}
        ${setupRunning ? `<div class="banner warn">${icon('hammer')}<div>Setup is running. Mod changes are available after it finishes or pauses.</div></div>` : ''}
        ${mods && !buildReady && !setupRunning ? `<div class="banner warn">${icon('hammer')}<div>${enabled
          ? 'The selected patches are not in the current build. Run Setup to rebuild before playing.'
          : 'Setup is not complete. Run Setup before playing.'}</div></div>` : ''}
        ${mods && buildReady && !setupRunning ? `<div class="banner ok">${icon('circle-check')}<div>Current build matches the selected mods.</div></div>` : ''}
        <div class="row wrap">
          <button class="btn primary" id="import-ips" ${busy || !mods || setupRunning ? 'disabled' : ''}>${icon('file-archive')}Import IPS</button>
          <button class="btn" id="import-folder" ${busy || !mods || setupRunning ? 'disabled' : ''}>${icon('folder-open')}Import package folder</button>
          <button class="btn quiet" id="refresh-mods" ${busy ? 'disabled' : ''} title="Refresh the mod list">${icon('refresh-cw')}Refresh</button>
        </div>
        ${installing ? `<form class="card stack" id="import-form">
          <h2>${icon('file-archive')}Import IPS patch</h2>
          <div class="path">${esc(pending)}</div>
          <label for="mod-id">Mod ID</label>
          <input id="mod-id" type="text" spellcheck="false" required maxlength="64" pattern="[a-z][a-z0-9-]*" value="${esc(draft.id)}" placeholder="e.g. item-gain-2x" ${busy || setupRunning ? 'disabled' : ''} />
          <label for="mod-name">Display name (optional)</label>
          <input id="mod-name" type="text" maxlength="120" value="${esc(draft.name)}" placeholder="Name shown in the list" ${busy || setupRunning ? 'disabled' : ''} />
          ${formError ? `<p class="faint mod-error">${esc(formError)}</p>` : ''}
          <div class="row wrap">
            <button class="btn primary" type="submit" ${busy || setupRunning ? 'disabled' : ''}>${icon('circle-check')}Import</button>
            <button class="btn quiet" type="button" id="cancel-import" ${busy ? 'disabled' : ''}>Cancel</button>
          </div>
        </form>` : ''}
        <section class="card">
          <h2>${icon('puzzle')}Installed mods</h2>
          ${entries.length ? `<div class="mod-list">${entries.map((m) => {
            const supported = m.status === 'release_build_supported' || m.status === 'runtime_supported';
            return `<div class="mod-row">
              <div class="mod-detail">
                <div class="row wrap"><strong>${esc(m.name)}</strong>
                  <span class="chip ${supported ? '' : 'warn'}">${m.type === 'exefs-ips' ? 'ExeFS IPS' : (supported ? 'Runtime (F1 menu)' : 'Runtime (not supported)')}</span>
                </div>
                <span class="faint mono">${esc(m.id)} · v${esc(m.version)}</span>
              </div>
              <label class="switch" title="${supported ? (m.type === 'runtime' ? 'Restart the game to apply this change' : 'Change this mod for the next build') : 'Runtime mod execution is not available for this package'}">
                <input type="checkbox" data-mod="${esc(m.id)}" ${m.enabled ? 'checked' : ''} ${busy || setupRunning || !supported ? 'disabled' : ''} />
                <span>${supported ? (m.enabled ? 'Enabled' : 'Disabled') : 'Unavailable'}</span>
              </label>
            </div>`;
          }).join('')}</div>` : `<p class="dim">No mods installed. Import an IPS patch or a package folder to begin.</p>`}
        </section>
        <div class="row wrap">
          <button class="btn ${mods && !buildReady ? 'primary' : ''}" id="mods-setup">${icon('hammer')}Open Setup</button>
          <button class="btn quiet" id="mods-dir" ${mods ? '' : 'disabled'}>${icon('folder-open')}Open run folder</button>
        </div>
        ${mods ? `<p class="faint">Game Build ID: <code>${esc(mods.game_build_id)}</code>. IPS changes require Setup to rebuild; supported runtime mods appear in the in-game F1 menu after a restart.</p>` : ''}
      </div>`;
    refreshIcons();
    el.querySelector('#mods-setup').addEventListener('click', () => ctx.go('setup'));
    el.querySelector('#mods-dir')?.addEventListener('click', () => api('open_game_dir').catch((e) => toast(errText(e), 'error')));
    el.querySelector('#refresh-mods').addEventListener('click', async () => {
      try { await reload(); draw(); } catch (e) { toast(errText(e), 'error'); }
    });
    el.querySelector('#import-ips')?.addEventListener('click', async () => {
      try {
        const file = await api('pick_mod_file');
        if (file) { pending = file; draft = { id: suggestedId(file), name: '' }; formError = ''; draw(); el.querySelector('#mod-id')?.focus(); }
      } catch (e) { toast(errText(e), 'error'); }
    });
    el.querySelector('#import-folder')?.addEventListener('click', async () => {
      try {
        const folder = await api('pick_folder', { title: 'Choose a mod package folder', start: null });
        if (folder) await update(() => api('import_mod', { source: folder, id: null, name: null }), 'Mod package imported.');
      } catch (e) { toast(errText(e), 'error'); }
    });
    el.querySelector('#cancel-import')?.addEventListener('click', () => { pending = ''; formError = ''; draft = { id: '', name: '' }; draw(); });
    el.querySelector('#mod-id')?.addEventListener('input', (event) => { draft.id = event.target.value; });
    el.querySelector('#mod-name')?.addEventListener('input', (event) => { draft.name = event.target.value; });
    el.querySelector('#import-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const id = el.querySelector('#mod-id').value.trim();
      draft.id = id;
      if (!VALID_ID.test(id)) {
        formError = 'Use a lowercase ID starting with a letter; only a-z, 0-9 and hyphens are allowed.';
        const note = el.querySelector('.mod-error');
        if (note) note.textContent = formError;
        else toast(formError, 'error');
        return;
      }
      const name = el.querySelector('#mod-name').value.trim() || null;
      draft.name = name ?? '';
      await update(() => api('import_mod', { source: pending, id, name }), 'IPS patch imported.', true);
    });
    el.querySelectorAll('[data-mod]').forEach((checkbox) => checkbox.addEventListener('change', async () => {
      const id = checkbox.dataset.mod;
      const enabled = checkbox.checked;
      checkbox.disabled = true;
      const runtime = entries.find((m) => m.id === id)?.type === 'runtime';
      await update(() => api('set_mod_enabled', { id, enabled }), runtime
        ? (enabled ? 'Mod enabled. Restart the game to use it in F1.' : 'Mod disabled. Restart the game to apply.')
        : (enabled ? 'Mod enabled. Run Setup to rebuild.' : 'Mod disabled. Run Setup to rebuild.'));
    }));
  }

  async function update(command, message, fromForm = false) {
    busy = true;
    draw();
    try {
      await command();
      await reload();
      if (!error) {
        if (fromForm) { pending = ''; formError = ''; draft = { id: '', name: '' }; }
        toast(message, 'ok');
      } else {
        toast(error, 'error');
      }
    } catch (e) {
      if (fromForm) formError = errText(e);
      else error = errText(e);
      toast(errText(e), 'error');
    } finally {
      busy = false;
      draw();
    }
  }

  ctx.onSetup(async () => { if (!busy) { await reload(); draw(); } });
  await reload();
  draw();
}
