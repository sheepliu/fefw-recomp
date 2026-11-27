import { api, esc, icon, refreshIcons, toast, errText } from '../util.js';
import { t } from '../i18n.js';
import { confirmUpscalerFiles } from '../upscalerCheck.js';
import { statusText, stepTitle } from '../rusttext.js';

const hex = (c) => '0x' + (c >>> 0).toString(16).toUpperCase().padStart(8, '0');

function statusChip(s) {
  if (s.state === 'running') return `<span class="chip warn">${icon('loader-circle', 'spin')}${esc(t('play.running'))}</span>`;
  if (s.state === 'exited') {
    const code = s.code === null || s.code === undefined ? t('play.noExitCode') : t('play.codeWithHex', { code: s.code, hex: hex(s.code) });
    return s.crashed
      ? `<span class="chip danger">${icon('circle-alert')}${esc(t('play.crashed', { code }))}</span>`
      : `<span class="chip ok">${icon('circle-check')}${esc(t('play.exited', { code }))}</span>`;
  }
  return `<span class="chip">${esc(t('play.ready'))}</span>`;
}

/* The line a run ends with comes from play.rs in English; rusttext.js translates it. */
const statusMsg = (s) => statusText(s);

function reportHtml(s) {
  if (s.state !== 'exited') return '';
  const files = s.files.filter((f) => s.crashed || f.name === 'fefw.log');
  return `
    <div class="card">
      <h2>${icon(s.crashed ? 'circle-alert' : 'file-text')}${esc(t(s.crashed ? 'play.reportCrashed' : 'play.reportClean'))}</h2>
      <p class="dim">${esc(statusMsg(s))}</p>
      ${files.length ? `<div class="row wrap" style="margin-top: var(--space-4)">${files
        .map((f) => `<button class="btn" data-open="${esc(f.name)}">${icon('file-text')}${esc(f.name)}${f.fresh && f.name !== 'fefw.log' ? esc(t('play.fileNew')) : ''}</button>`)
        .join('')}</div>` : ''}
    </div>`;
}

export async function render(el, ctx) {
  const visit = el.dataset.visit; // changes when the page is left (main.js)
  const isLinux = (await api('platform').catch(() => '')) === 'linux';
  const [cfg, ov] = await Promise.all([api('get_config'), api('get_overview')]);
  const setup = ov.repo_ok ? await api('setup_status') : null;
  const report = cfg.dump_dir && ov.repo_ok ? await api('validate_dump', { path: cfg.dump_dir }) : null;
  const chosen = report && report.chosen;
  let status = await api('game_status');
  // Windows: little memory left for the game to take (memory.rs)
  let memory = await api('memory_warning').catch(() => null);
  let reportsPrompted = !!cfg.reports_prompted;
  // Linux: fefw.exe as a non-Steam game (steam.rs)
  let steam = await api('steam_status').catch(() => ({ available: false }));
  let addingSteam = false;
  // Linux handhelds: the Decky plugin, when it is not installed
  let decky = await api('decky_offer').catch(() => null);

  function memoryHtml() {
    if (!memory || status.state === 'running') return '';
    const key = memory.kind === 'noPageFile' ? 'play.memoryNoPageFile' : 'play.memoryLow';
    return `<div class="banner warn">${icon('triangle-alert')}<div>${esc(t(key, { free: memory.free_gb.toFixed(1) }))}</div></div>`;
  }

  // Asks again, and redraws only the warning: the rest of the page stays as the player left it.
  async function checkMemory() {
    const now = await api('memory_warning').catch(() => null);
    if (el.dataset.visit !== visit || JSON.stringify(now) === JSON.stringify(memory)) return;
    memory = now;
    const box = el.querySelector('#memory');
    if (box) box.innerHTML = memoryHtml();
    refreshIcons();
  }

  function draw() {
    const running = status.state === 'running';
    const problems = [];
    if (!ov.repo_ok) problems.push(t('play.problemRepo'));
    else if (!cfg.dump_dir) problems.push(t('play.problemNoDump'));
    else if (!chosen) problems.push(t('play.problemBadDump', { path: cfg.dump_dir, errors: report.errors.join(' ') }));
    else if (!setup.complete) problems.push(t(setup.progress.state === 'running' ? 'play.problemSetupRunning' : 'play.problemSetupUnfinished'));
    const ready = problems.length === 0;
    const left = setup ? setup.steps.filter((x) => x.state !== 'done').map((x) => stepTitle(x)) : [];

    el.innerHTML = `
      <div class="page">
        <div class="hero">
          <h1>${esc(t('play.title'))}</h1>
          <p class="dim">${esc(t('play.intro'))}</p>
        </div>
        ${problems.map((p) => `<div class="banner danger">${icon('circle-x')}<div>${esc(p)}</div></div>`).join('')}
        <div id="memory" style="display: contents">${memoryHtml()}</div>
        ${problems.length && ov.repo_ok ? `<div><button class="btn primary" id="to-setup">${icon(chosen ? 'hammer' : 'folder-open')}${esc(t(chosen ? 'play.openSetup' : 'play.chooseFiles'))}</button></div>` : ''}

        <div class="row wrap">
          <button class="btn primary big" id="play" ${ready && !running ? '' : 'disabled'}>${icon('play')}${esc(t(running ? 'play.running' : 'play.title'))}</button>
          ${statusChip(status)}
        </div>
        ${reportHtml(status)}
        ${!reportsPrompted ? `
        <div class="card" id="reports-prompt">
          <h2>${icon('activity')}${esc(t('setup.reportsTitle'))}</h2>
          <p class="dim">${esc(t('setup.reportsToggle'))}</p>
          <p class="faint" style="margin-top: var(--space-2)">${esc(t('setup.reportsHint'))}</p>
          <div class="row" style="margin-top: var(--space-4)">
            <button class="btn primary" id="reports-enable">${icon('circle-check')}${esc(t('play.reportsEnable'))}</button>
            <button class="btn quiet" id="reports-dismiss">${esc(t('play.reportsDismiss'))}</button>
          </div>
        </div>` : ''}
        ${decky ? `
        <div class="card" id="decky-offer">
          <h2>${icon('gamepad-2')}${esc(t('play.deckyTitle'))}</h2>
          <p class="dim">${esc(t('play.deckyText'))}</p>
          <p class="faint" style="margin-top: var(--space-2)">${esc(t('play.deckyHint'))}</p>
          ${decky.folder ? `<p class="faint" style="margin-top: var(--space-2)">${esc(t('play.deckyFolder'))}</p><div class="path">${esc(decky.folder)}</div>` : ''}
          <div class="row" style="margin-top: var(--space-4)">
            <button class="btn primary" id="decky-get">${icon('download')}${esc(t('play.deckyGet'))}</button>
            <button class="btn quiet" id="decky-dismiss">${esc(t('play.reportsDismiss'))}</button>
          </div>
        </div>` : ''}
        ${steam.available && steam.stale ? `<div class="banner warn">${icon('triangle-alert')}<div style="flex:1">${esc(t('play.steamStale'))}</div><button class="btn" id="update-steam" ${ov.exe_exists && !addingSteam ? '' : 'disabled'}>${icon('gamepad-2')}${esc(t('play.updateSteam'))}</button></div>` : ''}

        <div class="card">
          <h2>${icon('hard-drive')}${esc(t('play.install'))}</h2>
          <dl class="kv">
            <dt>${esc(t('play.gameFiles'))}</dt>
            <dd>${chosen ? `${esc(chosen.kind === 'update' ? t('common.update', { version: chosen.version }) : chosen.kind === 'base' ? t('common.baseGame') : t('common.folder'))}<div class="path">${esc(chosen.path)}</div>` : `<span class="faint">${esc(t('common.notSet'))}</span>`}</dd>
            <dt>${esc(t('play.dlc'))}</dt>
            <dd>${report && report.dlc && report.dlc.length ? `${esc(t(report.dlc.length === 1 ? 'play.dlcFound' : 'play.dlcFoundMany', { count: report.dlc.length }))}<div class="path">${esc(report.dlc_dir)}</div>` : `<span class="faint">${esc(t('play.dlcNone'))}</span>`}</dd>
            <dt>${esc(t('play.setup'))}</dt>
            <dd>${setup && setup.complete ? esc(t('common.complete')) : `<span class="faint">${esc(left.length ? t('common.todo', { list: left.join(', ') }) : t('common.notStarted'))}</span>`}</dd>
            <dt>${esc(t('play.exe'))}</dt>
            <dd>${ov.exe_exists ? esc(t('common.built')) : `<span class="faint">${esc(t('common.notBuiltYet'))}</span>`}<div class="path">${esc(ov.exe)}</div></dd>
            <dt>${esc(t('play.runFolder'))}</dt>
            <dd><span class="faint">${esc(t('play.runFolderContents'))}</span><div class="path">${esc(ov.game_dir)}</div></dd>
            <dt>${esc(t('play.mods'))}</dt>
            <dd><ul class="faint bullets">${['modsRomfs', 'modsExefs', 'modsOff', 'modsOrder', 'modsRebuild'].map((k) => `<li>${esc(t(`play.${k}`))}</li>`).join('')}</ul></dd>
          </dl>
          <div class="row" style="margin-top: var(--space-4)">
            <button class="btn" id="open-dir" ${ov.repo_ok ? '' : 'disabled'}>${icon('folder-open')}${esc(t('play.openRunFolder'))}</button>
            <button class="btn" id="open-mods" ${ov.repo_ok ? '' : 'disabled'}>${icon('package')}${esc(t('play.openModsFolder'))}</button>
            ${steam.available ? `<button class="btn" id="add-steam" title="${esc(t('play.addToSteamHint'))}" ${ov.exe_exists && !addingSteam ? '' : 'disabled'}>${icon(addingSteam ? 'loader-circle' : 'gamepad-2', addingSteam ? 'spin' : '')}${esc(t(steam.added ? 'play.updateSteam' : 'play.addToSteam'))}</button>` : ''}
          </div>
        </div>
        <p class="faint">${esc(t('play.footer'))}</p>
      </div>`;
    refreshIcons();
    el.querySelector('#play')?.addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      const go = await confirmUpscalerFiles(isLinux);
      btn.disabled = false;
      if (!go) return;
      api('play').catch((e) => toast(errText(e), 'error'));
    });
    const answerReports = async (enabled) => {
      try {
        await api('set_send_reports', { enabled });
        reportsPrompted = true;
        draw();
      } catch (e) {
        toast(errText(e), 'error');
      }
    };
    el.querySelector('#reports-enable')?.addEventListener('click', () => answerReports(true));
    el.querySelector('#reports-dismiss')?.addEventListener('click', () => answerReports(false));
    el.querySelector('#decky-get')?.addEventListener('click', () =>
      api('open_decky_installer').then(() => toast(t('play.deckyOpened'), 'ok')).catch((e) => toast(errText(e), 'error')),
    );
    el.querySelector('#decky-dismiss')?.addEventListener('click', async () => {
      try {
        await api('decline_decky');
        decky = null;
        draw();
      } catch (e) {
        toast(errText(e), 'error');
      }
    });
    el.querySelector('#to-setup')?.addEventListener('click', () => ctx.go('setup'));
    el.querySelector('#open-dir')?.addEventListener('click', () => api('open_game_dir').catch((e) => toast(errText(e), 'error')));
    el.querySelector('#open-mods')?.addEventListener('click', () => api('open_mods_dir').catch((e) => toast(errText(e), 'error')));
    const addSteam = async () => {
      addingSteam = true;
      draw();
      try {
        toast(await api('add_to_steam'), 'ok');
      } catch (e) {
        toast(errText(e), 'error');
      }
      steam = await api('steam_status').catch(() => steam);
      addingSteam = false;
      draw();
    };
    el.querySelector('#add-steam')?.addEventListener('click', addSteam);
    el.querySelector('#update-steam')?.addEventListener('click', addSteam);
    el.querySelectorAll('[data-open]').forEach((b) =>
      b.addEventListener('click', () => api('open_game_file', { name: b.dataset.open }).catch((e) => toast(errText(e), 'error'))),
    );
  }

  ctx.onStatus((s) => {
    status = s;
    draw();
    if (s.state === 'exited') checkMemory();
  });
  // programs closed or opened while the page is on show
  const memoryTimer = setInterval(() => {
    if (el.dataset.visit !== visit) clearInterval(memoryTimer);
    else if (status.state !== 'running') checkMemory();
  }, 5000);
  ctx.onLanguage(draw);
  draw();
}
