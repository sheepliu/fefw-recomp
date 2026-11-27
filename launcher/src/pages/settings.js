import { api, esc, icon, refreshIcons, toast, errText } from '../util.js';
import { t } from '../i18n.js';
import { fileDetail } from '../rusttext.js';

/*
 * Settings schema. Every key is described in runtime/docs/CONFIG.md.
 *   file: "game" (fefw.ini), "controls" (controls.ini, written by the game on its first start) or "config" (the launcher's own options)
 * Types: select, toggle, size, autonum, range (0 to `max`, default 1, shown as a percentage), path, text, number. A toggle with `on` / `off` writes those words
 * instead of 1 / 0 (`off`, 0, false or no read as off, anything else as on).
 * Text is a key into src/locales: `label` and `hint` are always translated, and an option name that contains a dot is a key
 * too (a plain name such as "720p" or "DLAA" is shown as it is).
 */
// The WebKitGTK launcher on Linux (the Windows one is WebView2). Asked of the launcher: WebKitGTK's user agent says macOS.
const IS_LINUX = (await api('platform').catch(() => '')) === 'linux';

/*
 * Layout: tabs, each holding groups (one card each), each holding fields. A group's `file` is the default for its fields; a field
 * can name its own. `nv: true` on a field, or as the third element of an option, marks an NVIDIA-only choice (DLSS): it is shown
 * only when gpu_info found an NVIDIA GPU, when the probe found nothing at all, or when it is the value already set.
 * Descriptions: a `hint` with a `.short` sibling key (settings.x.short next to settings.x.hint) shows the short line, and an info
 * button opens the full hint under the row; a hint with no short form is short enough to show as it is. `restart: true` puts a
 * "Next start" chip by the name (the game reads the setting only when it starts).
 */
const TABS = [
  {
    id: 'display', title: 'settings.tab.display', icon: 'monitor',
    groups: [
      {
        title: 'settings.sec.resolution', icon: 'monitor', file: 'game',
        fields: [
          { id: 'output', type: 'select', label: 'settings.output.label', sec: 'fefw', key: 'output', def: 'console',
            options: [['console', 'settings.output.console'], ['720p', '720p'], ['1080p', '1080p'], ['1440p', '1440p'], ['2160p', '2160p (4K)'],
              ['2560x1080', 'settings.output.uw2560'], ['3440x1440', 'settings.output.uw3440'], ['5120x1440', 'settings.output.uw5120'],
              ['window', 'settings.output.window']],
            hint: 'settings.output.hint', restart: true },
          { id: 'ultrawide', type: 'toggle', label: 'settings.ultrawide.label', sec: 'fefw', key: 'ultrawide', def: 'auto', on: 'auto', off: 'off',
            onLabel: 'settings.ultrawide.auto', offLabel: 'settings.ultrawide.off', hint: 'settings.ultrawide.hint' },
        ],
      },
      {
        title: 'settings.sec.window', icon: 'app-window', file: 'controls',
        fields: [
          { id: 'window_mode', type: 'select', label: 'settings.windowMode.label', sec: 'display', key: 'window_mode', def: 'windowed',
            options: [['windowed', 'settings.windowMode.windowed'], ['borderless', 'settings.windowMode.borderless'], ['exclusive', 'settings.windowMode.exclusive']],
            hint: 'settings.windowMode.hint' },
          { id: 'size', type: 'size', label: 'settings.windowSize.label', sec: 'display', keys: ['width', 'height'], def: ['1280', '720'],
            options: [
              ['1280x720', '1280 x 720'], ['1600x900', '1600 x 900'], ['1920x1080', '1920 x 1080'],
              ['2560x1440', '2560 x 1440'], ['3840x2160', '3840 x 2160'],
              ['2560x1080', 'settings.output.uw2560'], ['3440x1440', 'settings.output.uw3440'], ['5120x1440', 'settings.output.uw5120'],
            ],
            hint: 'settings.windowSize.hint',
            lockedIf: (m) => m.window_mode !== 'windowed' },
        ],
      },
      {
        title: 'settings.sec.frameRate', icon: 'gauge', file: 'game',
        fields: [
          // one choice over the game's two switches ([fefw] fps60 and fps120; 120 wins when both are on)
          { id: 'fps_target', type: 'rate', label: 'settings.fpsTarget.label', sec: 'fefw', keys: ['fps60', 'fps120'],
            options: [['30', 'settings.fpsTarget.30'], ['60', 'settings.fpsTarget.60'], ['120', 'settings.fpsTarget.120']], hint: 'settings.fpsTarget.hint' },
          { id: 'smooth_step', type: 'toggle', label: 'settings.smoothStep.label', sec: 'fefw', key: 'smooth_step', def: '0', hint: 'settings.smoothStep.hint' },
          { id: 'fps_limit', type: 'autonum', label: 'settings.fpsLimit.label', file: 'controls', sec: 'display', key: 'fps_limit', def: 'auto',
            min: 0, max: 1000, numDefault: 60, unit: 'settings.fpsLimit.unit', autoLabel: 'settings.fpsLimit.auto', hint: 'settings.fpsLimit.hint' },
          { id: 'vsync', type: 'toggle', label: 'settings.vsync.label', file: 'controls', sec: 'display', key: 'vsync', def: '1' },
        ],
      },
    ],
  },
  {
    id: 'graphics', title: 'settings.tab.graphics', icon: 'sparkles',
    groups: [
      {
        // Low / Medium / High / Ultra: render scale, shadow map size, quality and the four detail keys together (PRESETS below). Not a key of
        // its own: it shows the preset those seven hold, Custom when they hold anything else, as the F1 menu does (games/fefw/fefw_presets.h).
        title: 'settings.sec.preset', icon: 'sliders-horizontal', file: 'game',
        fields: [
          { id: 'preset', type: 'preset', label: 'settings.preset.label', hint: 'settings.preset.hint' },
        ],
      },
      {
        title: 'settings.sec.upscaling', icon: 'image-upscale', file: 'game', gpuNote: true,
        fields: [
          { id: 'upscaler', type: 'select', label: 'settings.upscaler.label', sec: 'dlss', key: 'upscaler', def: 'taau',
            options: [['taau', 'settings.upscaler.taau'], ['dlss', 'settings.upscaler.dlss', { nv: true }], ['fsr3', 'settings.upscaler.fsr3'],
              ['xess', 'settings.upscaler.xess'], ['bilinear', 'settings.upscaler.bilinear'], ['auto', 'settings.upscaler.auto']],
            hint: 'settings.upscaler.hint', restart: true },
          // One list for [fefw] render_ratio: the percentages with the usual upscaler tier names (the F1 menu's Render scale steps).
          { id: 'render_ratio', type: 'select', label: 'settings.renderRatio.label', sec: 'fefw', key: 'render_ratio', def: '0',
            options: [['0', 'settings.renderRatio.game'], ['0.33', ['33%', 'settings.srTier.ultraPerformance']], ['0.5', ['50%', 'settings.srTier.performance']],
              ['0.59', ['59%', 'settings.srTier.balanced']], ['0.67', ['67%', 'settings.srTier.quality']], ['0.75', ['75%', 'settings.srTier.ultraQuality']],
              ['0.85', ['85%', 'settings.srTier.highQuality']], ['1.0', 'settings.renderRatio.full']],
            hint: 'settings.renderRatio.hint' },
          { id: 'frame_gen', type: 'select', label: 'settings.frameGen.label', sec: 'dlss', key: 'frame_gen', def: '0',
            // Linux: the game's own FSR 3 frame generation is not supported under Proton, so it is not offered (`unsupported`)
            options: [['0', 'settings.frameGen.off'], ['dlss', 'settings.frameGen.dlss', { nv: true }], ...(IS_LINUX ? [] : [['fsr3', 'settings.frameGen.fsr3']])],
            unsupported: IS_LINUX ? ['fsr3'] : [],
            hint: IS_LINUX ? 'settings.frameGen.hintLinux' : 'settings.frameGen.hint', restart: true },
          // Linux only: LSFG-VK frame generation on the finished picture (launcher/src-tauri/src/lsfg.rs); stored in launcher.json
          ...(IS_LINUX ? [
            // can be turned on only when the layer and lsfg-vk.dll are found; always off
            { id: 'lsfg', file: 'config', type: 'toggle', label: 'settings.lsfg.label', sec: '', key: 'lsfg', def: '0', hint: 'settings.lsfg.hint', restart: true, lsfgNote: true,
              lockedIf: (m, ctx) => !m.lsfg && !ctx.lsfgReady },
            { id: 'lsfg_multiplier', file: 'config', showIf: (m) => m.lsfg, type: 'select', label: 'settings.lsfg.multiplier', sec: '', key: 'lsfg_multiplier', def: '2',
              options: [['2', '2x'], ['3', '3x'], ['4', '4x']] },
            { id: 'lsfg_flow_scale', file: 'config', showIf: (m) => m.lsfg, type: 'select', label: 'settings.lsfg.flowScale', sec: '', key: 'lsfg_flow_scale', def: '1',
              options: [['1', '100%'], ['0.75', '75%'], ['0.5', '50%'], ['0.25', '25%']], hint: 'settings.lsfg.flowScaleHint' },
            { id: 'lsfg_performance', file: 'config', showIf: (m) => m.lsfg, type: 'toggle', label: 'settings.lsfg.performance', sec: '', key: 'lsfg_performance', def: '0', hint: 'settings.lsfg.performanceHint' },
          ] : []),
        ],
      },
      {
        title: 'settings.sec.detail', icon: 'layers', file: 'game',
        fields: [
          { id: 'quality', type: 'select', label: 'settings.quality.label', sec: 'fefw', key: 'quality', def: 'console',
            options: [['console', 'settings.quality.console'], ['docked', 'settings.quality.docked'], ['handheld', 'settings.quality.handheld']] },
          { id: 'shadow_size', type: 'select', label: 'settings.shadowSize.label', sec: 'fefw', key: 'shadow_size', def: '0',
            options: [['0', 'settings.shadowSize.game'], ['1024', '1024'], ['2048', '2048'], ['4096', '4096'], ['8192', '8192']] },
          // the game's own graphics data, edited as it loads (games/fefw/fefw_detail.cpp): applies at the next start
          { id: 'reflections', type: 'select', label: 'settings.reflections.label', sec: 'fefw', key: 'reflections', def: 'full',
            options: [['full', 'settings.detail.full'], ['reduced', 'settings.reflections.reduced'], ['off', 'settings.detail.off']],
            hint: 'settings.reflections.hint', restart: true },
          { id: 'ambient_occlusion', type: 'select', label: 'settings.ao.label', sec: 'fefw', key: 'ambient_occlusion', def: '1',
            options: [['1', 'settings.detail.on'], ['0', 'settings.detail.off']], hint: 'settings.ao.hint', restart: true },
          { id: 'crowd_detail', type: 'select', label: 'settings.crowdDetail.label', sec: 'fefw', key: 'crowd_detail', def: 'full',
            options: [['full', 'settings.detail.full'], ['reduced', 'settings.detail.reduced']], hint: 'settings.crowdDetail.hint', restart: true },
          { id: 'scenery_detail', type: 'select', label: 'settings.sceneryDetail.label', sec: 'fefw', key: 'scenery_detail', def: 'full',
            options: [['full', 'settings.detail.full'], ['reduced', 'settings.detail.reduced']], hint: 'settings.sceneryDetail.hint', restart: true },
          { id: 'texture_detail', type: 'select', label: 'settings.textureDetail.label', sec: 'gfx', key: 'texture_detail', def: 'auto',
            options: [['auto', 'settings.textureDetail.auto'], ['full', 'settings.textureDetail.full'], ['half', 'settings.textureDetail.half']],
            hint: 'settings.textureDetail.hint', restart: true },
          // [gfx] astc_bc (runtime/src/gfx/nvn/nvn_texdata.h): opaque ASTC textures as BC1, half the video memory of BC7
          { id: 'astc_bc', type: 'select', label: 'settings.astcBc.label', sec: 'gfx', key: 'astc_bc', def: 'auto',
            options: [['auto', 'settings.astcBc.auto'], ['off', 'settings.astcBc.off'], ['bc1', 'settings.astcBc.bc1']],
            hint: 'settings.astcBc.hint', restart: true },
          { id: 'gpu', type: 'select', file: 'controls', label: 'settings.gpu.label', sec: 'display', key: 'gpu', def: 'auto',
            options: [['auto', 'settings.gpu.auto'], ['discrete', 'settings.gpu.discrete'], ['integrated', 'settings.gpu.integrated']],
            hint: 'settings.gpu.hint', restart: true },
        ],
      },
      {
        title: 'settings.sec.upscalerFiles', icon: 'download', file: 'game',
        fields: [
          { id: 'fsr_dll', type: 'path', label: 'settings.fsrDll.label', sec: 'upscale', key: 'fsr_dll', def: '', placeholder: 'settings.fsrDll.placeholder', status: 'fsr',
            hint: 'settings.fsrDll.hint' },
          { id: 'dlss_dll', type: 'path', label: 'settings.dlssDll.label', sec: 'upscale', key: 'dlss_dll', def: '', placeholder: 'settings.dlssDll.placeholder', status: 'dlss',
            hint: 'settings.dlssDll.hint', nv: true },
          { id: 'xess_dll', type: 'path', label: 'settings.xessDll.label', sec: 'upscale', key: 'xess_dll', def: '', placeholder: 'settings.xessDll.placeholder', status: 'xess',
            hint: 'settings.xessDll.hint' },
        ],
      },
    ],
  },
  {
    id: 'audio', title: 'settings.tab.audio', icon: 'volume-2',
    groups: [
      {
        title: 'settings.sec.sound', icon: 'volume-2', file: 'controls',
        fields: [
          { id: 'volume', type: 'range', label: 'settings.volume.label', sec: 'audio', key: 'volume', def: '1.0', max: 2, unit: 'settings.volume.unit' },
          { id: 'mute_unfocused', type: 'toggle', label: 'settings.muteUnfocused.label', sec: 'audio', key: 'mute_unfocused', def: '0' },
          // [audio] channels of controls.ini: the game's mix, stereo or 5.1 (runtime/src/gfx/play/play.cpp hands it to the audio services at start)
          { id: 'audio_channels', type: 'select', label: 'settings.audioChannels.label', sec: 'audio', key: 'channels', def: '2',
            options: [['2', 'settings.audioChannels.stereo'], ['6', 'settings.audioChannels.surround']], hint: 'settings.audioChannels.hint', restart: true },
        ],
      },
      {
        // [tweaks] language of controls.ini (runtime/docs/CONFIG.md). The names are the ones the runtime reads (services/common.cpp); the list is
        // the languages the game ships (games/fefw/fefw_data.cpp). A [system] language in fefw.ini wins over this.
        title: 'settings.sec.language', icon: 'languages', file: 'controls',
        fields: [
          { id: 'language', type: 'select', label: 'settings.gameLang.label', sec: 'tweaks', key: 'language', def: 'auto',
            options: [
              ['auto', 'settings.gameLang.auto'],
              ['AmericanEnglish', 'settings.gameLang.americanEnglish'], ['BritishEnglish', 'settings.gameLang.britishEnglish'],
              ['Japanese', 'settings.gameLang.japanese'], ['French', 'settings.gameLang.french'],
              ['CanadianFrench', 'settings.gameLang.canadianFrench'], ['German', 'settings.gameLang.german'],
              ['Italian', 'settings.gameLang.italian'], ['Spanish', 'settings.gameLang.spanish'],
              ['LatinAmericanSpanish', 'settings.gameLang.latinSpanish'], ['Korean', 'settings.gameLang.korean'],
              ['SimplifiedChinese', 'settings.gameLang.simplifiedChinese'], ['TraditionalChinese', 'settings.gameLang.traditionalChinese'],
            ],
            hint: 'settings.gameLang.hint', restart: true },
        ],
      },
    ],
  },
  {
    id: 'controls', title: 'settings.tab.controls', icon: 'gamepad-2',
    groups: [
      {
        title: 'settings.sec.buttons', icon: 'gamepad-2', file: 'controls',
        fields: [
          { id: 'prompt_style', type: 'select', label: 'settings.promptStyle.label', sec: 'prompts', key: 'style', def: 'auto',
            options: [['auto', 'settings.promptStyle.auto'], ['switch', 'settings.promptStyle.switch'], ['xbox', 'settings.promptStyle.xbox'],
              ['playstation', 'settings.promptStyle.playstation'], ['keyboard', 'settings.promptStyle.keyboard']],
            hint: 'settings.promptStyle.hint' },
          // [pad] label_abxy: which physical button the Switch's A is mapped from. The button prompts follow it (runtime/gfx/play/prompt_resolve.cpp).
          { id: 'face_buttons', type: 'select', label: 'settings.faceButtons.label', sec: 'pad', key: 'label_abxy', def: '1',
            options: [['1', 'settings.faceButtons.label_'], ['0', 'settings.faceButtons.position']],
            hint: 'settings.faceButtons.hint' },
        ],
      },
      {
        title: 'settings.sec.vibration', icon: 'vibrate', file: 'controls',
        fields: [
          // [pad] haptic: how the game's HD Rumble value reaches the pad (runtime/gfx/haptic.cpp). The launcher only writes the
          // setting; the game reads it when it starts, so a change lands on the next run.
          { id: 'haptic', type: 'select', label: 'settings.haptic.label', sec: 'pad', key: 'haptic', def: 'auto',
            options: [['auto', 'settings.haptic.auto'], ['off', 'settings.haptic.off'], ['rumble', 'settings.haptic.rumble']],
            // "dualsense" was a choice of its own that did what automatic does: an older file shows, and saves, as automatic
            unsupported: ['dualsense', 'haptics'], hint: 'settings.haptic.hint', restart: true },
          // [pad] haptic_strength: 0..100 percent for the haptic backends, or -1 to keep using the rumble strength above.
          { id: 'haptic_strength', type: 'number', label: 'settings.hapticStrength.label', sec: 'pad', key: 'haptic_strength',
            def: '-1', min: -1, max: 100, hint: 'settings.hapticStrength.hint' },
          // [pad] hd_haptics: a DualSense also gets taps on its speaker.
          { id: 'hd_haptics', type: 'toggle', label: 'settings.hdHaptics.label', sec: 'pad', key: 'hd_haptics', def: '1',
            hint: 'settings.hdHaptics.hint', restart: true },
          // [pad] dualsense_full_volume: a wired DualSense's volume goes to full while playing (Windows: the game; Linux: the
          // launcher, pad_volume.rs).
          { id: 'dualsense_full_volume', type: 'toggle', label: 'settings.padVolume.label', sec: 'pad', key: 'dualsense_full_volume',
            def: '1', hint: 'settings.padVolume.hint', restart: true },
        ],
      },
      {
        title: 'settings.sec.mouse', icon: 'mouse', file: 'game',
        fields: [
          { id: 'jc2_mouse', type: 'select', label: 'settings.jc2Mouse.label', sec: 'input', key: 'jc2_mouse', def: 'auto',
            options: [['auto', 'settings.jc2Mouse.auto'], ['on', 'settings.jc2Mouse.on'], ['off', 'settings.jc2Mouse.off']], hint: 'settings.jc2Mouse.hint' },
        ],
      },
    ],
  },
  {
    id: 'launcher', title: 'settings.tab.launcher', icon: 'wrench',
    groups: [
      {
        title: 'settings.sec.general', icon: 'sliders-horizontal', file: 'config',
        fields: [
          { id: 'minimize_on_play', type: 'toggle', label: 'settings.minimizeOnPlay.label', sec: '', key: 'minimize_on_play', def: '0' },
          { id: 'send_reports', type: 'toggle', label: 'settings.sendReports.label', sec: '', key: 'send_reports', def: '0', hint: 'settings.sendReports.hint' },
        ],
      },
      {
        title: 'settings.sec.folders', icon: 'folder', file: 'config',
        fields: [
          { id: 'game_dir', type: 'path', label: 'settings.gameDir.label', sec: '', key: 'game_dir', def: '', placeholder: 'settings.gameDir.placeholder', hint: 'settings.gameDir.hint' },
          { id: 'save_dir', type: 'path', label: 'settings.saveDir.label', file: 'game', sec: 'fs', key: 'save_dir', def: '', placeholder: 'settings.saveDir.placeholder' },
          { id: 'repo_dir', type: 'path', label: 'settings.repoDir.label', sec: '', key: 'repo_dir', def: '', placeholder: 'settings.repoDir.placeholder', hint: 'settings.repoDir.hint' },
        ],
      },
      {
        title: 'settings.sec.advanced', icon: 'terminal', file: 'config',
        fields: [
          { id: 'jobs', type: 'number', label: 'settings.jobs.label', sec: '', key: 'jobs', def: '0', min: 0, max: 256, hint: 'settings.jobs.hint' },
          // Linux only: fefw.exe runs through Wine/Proton (launcher/src-tauri/src/play.rs runner())
          ...(IS_LINUX ? [{ id: 'runner', type: 'text', label: 'settings.runner.label', sec: '', key: 'runner', def: '', placeholder: 'settings.runner.placeholder', hint: 'settings.runner.hint' }] : []),
          { id: 'log_level', type: 'select', label: 'settings.logLevel.label', file: 'game', sec: 'log', key: 'level', def: 'info',
            options: [['error', 'settings.logLevel.error'], ['warn', 'settings.logLevel.warn'], ['info', 'settings.logLevel.info'], ['debug', 'settings.logLevel.debug']] },
        ],
      },
    ],
  },
];

// The graphics presets: the same table as runtime/games/fefw/fefw_presets.h (and runtime/docs/CONFIG.md). A preset sets these fields.
const DETAIL_FULL = { reflections: 'full', ambient_occlusion: '1', crowd_detail: 'full', scenery_detail: 'full' };
const PRESETS = [
  ['low', 'settings.preset.low', { render_ratio: '0.5', shadow_size: '1024', quality: 'handheld',
    reflections: 'reduced', ambient_occlusion: '0', crowd_detail: 'reduced', scenery_detail: 'reduced' }],
  ['medium', 'settings.preset.medium', { render_ratio: '0', shadow_size: '0', quality: 'console', ...DETAIL_FULL }],
  ['high', 'settings.preset.high', { render_ratio: '0.67', shadow_size: '4096', quality: 'docked', ...DETAIL_FULL }],
  ['ultra', 'settings.preset.ultra', { render_ratio: '1.0', shadow_size: '8192', quality: 'docked', ...DETAIL_FULL }],
];
const lowerOf = (x) => String(x).trim().toLowerCase();
const switchOn = (x) => !['0', 'false', 'off', 'no'].includes(lowerOf(x));
/** The preset the fields hold now ('' = Custom), compared by meaning as the runtime reads them. A fixed [fefw] render size (fixed) wins over
 *  render_ratio, so with one in the file no preset is in force until a picked preset clears it on Save. */
const presetOf = (m, fixed) => {
  if (fixed && !m.preset) return '';
  const hit = PRESETS.find(([, , v]) => Number(m.render_ratio) === Number(v.render_ratio) && Number(m.shadow_size) === Number(v.shadow_size)
    && ['quality', 'reflections', 'crowd_detail', 'scenery_detail'].every((k) => lowerOf(m[k]) === v[k])
    && switchOn(m.ambient_occlusion) === switchOn(v.ambient_occlusion));
  return hit ? hit[0] : '';
};

// The open tab, kept while the launcher runs (saving re-renders the page; it should come back on the same tab)
let activeTab = TABS[0].id;
// The fields whose full description is open, kept across redraws like the tab
const openHints = new Set();

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v).trim().toLowerCase());
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const fwd = (s) => String(s).trim().replace(/\\/g, '/');

/** An option label: a dotted name is a key, anything else ("720p") is shown as written; [a, b] reads "a (b)". */
const optName = (label) => (Array.isArray(label) ? `${optName(label[0])} (${optName(label[1])})` : String(label).includes('.') ? t(label) : label);

export async function render(el, ctx) {
  const visit = el.dataset.visit; // this render is still the page on show (main.js go); read before the first await
  const live = () => el.dataset.visit === visit;
  const [cfg, status, gpu] = await Promise.all([api('get_config'), api('game_status'), api('gpu_info').catch(() => ({ known: false }))]);
  // NVIDIA-only choices: shown with an NVIDIA GPU, and also when the probe found no GPU at all (better shown than wrongly hidden)
  const nvOk = !gpu.known || gpu.nvidia;
  // LSFG-VK's layer and lsfg-vk.dll (lsfg.rs): its switch turns on only when both are there
  let lsfgSt = IS_LINUX ? await api('lsfg_status').catch(() => null) : null;
  let lsfgHere = false; // installLsfg's install has not returned yet
  let lsfgTimer = 0;
  const fieldCtx = { get lsfgReady() { return !!(lsfgSt && lsfgSt.layer && lsfgSt.dll); } };
  const data = await api('read_settings').catch(() => ({ game_dir: '', game: {}, controls: {}, controls_exists: false }));
  const files = { game: data.game, controls: data.controls, config: { repo_dir: cfg.repo_dir, game_dir: cfg.game_dir, jobs: String(cfg.jobs || 0), minimize_on_play: cfg.minimize_on_play ? '1' : '0', send_reports: cfg.send_reports ? '1' : '0', runner: cfg.runner || '',
    lsfg: cfg.lsfg ? '1' : '0', lsfg_multiplier: String(cfg.lsfg_multiplier || 2), lsfg_flow_scale: cfg.lsfg_flow_scale || '1', lsfg_performance: cfg.lsfg_performance ? '1' : '0' } };
  const exists = { game: true, controls: data.controls_exists, config: true };
  const keyOf = (f, k) => (f.file === 'config' ? k : `${f.sec}.${k}`);
  const get = (f, k, def) => files[f.file][keyOf(f, k)] ?? def;
  const fixedRender = String(files.game['fefw.render'] ?? '').trim() !== '';

  // ----- model: one UI value per field -----
  const READ = {
    select: (f) => { const v = get(f, f.key, f.def); return f.unsupported?.includes(v) ? f.def : v; },
    toggle: (f) => (f.off !== undefined ? ![f.off, '0', 'false', 'no'].includes(String(get(f, f.key, f.def)).trim().toLowerCase()) : truthy(get(f, f.key, f.def))),
    size: (f) => `${parseInt(get(f, f.keys[0], f.def[0]), 10) || f.def[0]}x${parseInt(get(f, f.keys[1], f.def[1]), 10) || f.def[1]}`,
    rate: (f) => (truthy(get(f, f.keys[1], '0')) ? '120' : truthy(get(f, f.keys[0], '0')) ? '60' : '30'),
    autonum: (f) => {
      const v = String(get(f, f.key, f.def));
      return v.toLowerCase() === 'auto' ? { auto: true, num: String(f.numDefault) } : { auto: false, num: v };
    },
    range: (f) => Math.round(clamp(parseFloat(get(f, f.key, f.def)) || 0, 0, f.max ?? 1) * 100),
    path: (f) => get(f, f.key, f.def),
    text: (f) => get(f, f.key, f.def),
    number: (f) => String(get(f, f.key, f.def)),
    preset: () => '', // shown from the fields it sets (presetOf), never stored
  };
  const WRITE = {
    select: (f, x) => [[f.sec, f.key, x]],
    rate: (f, x) => [[f.sec, f.keys[0], x === '60' ? '1' : '0'], [f.sec, f.keys[1], x === '120' ? '1' : '0']],
    toggle: (f, x) => [[f.sec, f.key, x ? (f.on ?? '1') : (f.off ?? '0')]],
    size: (f, x) => {
      const [w, h] = String(x).split('x');
      return [
        [f.sec, f.keys[0], String(clamp(parseInt(w, 10) || 1280, 320, 16384))],
        [f.sec, f.keys[1], String(clamp(parseInt(h, 10) || 720, 320, 16384))],
      ];
    },
    autonum: (f, x) => [[f.sec, f.key, x.auto ? 'auto' : String(clamp(Math.round(parseFloat(x.num)) || f.numDefault, f.min, f.max))]],
    range: (f, x) => [[f.sec, f.key, (x / 100).toFixed(2)]],
    path: (f, x) => [[f.sec, f.key, fwd(x)]],
    text: (f, x) => [[f.sec, f.key, String(x).trim()]],
    number: (f, x) => [[f.sec, f.key, String(clamp(parseInt(x, 10) || 0, f.min, f.max))]],
    preset: () => (fixedRender ? [['fefw', 'render', '']] : []), // a preset was picked: the fixed render size would win over it
  };

  // Every field, with its file filled in from the group. A hidden NVIDIA-only field stays in the model: it is never dirty, so never written.
  const tabs = TABS.map((tab) => ({ ...tab, groups: tab.groups.map((g) => ({ ...g, fields: g.fields.map((f) => ({ ...f, file: f.file ?? g.file, tab: tab.id })) })) }));
  const all = tabs.flatMap((tab) => tab.groups.flatMap((g) => g.fields));
  // `showIf`: a field that only makes sense with another one set (the LSFG-VK options, with LSFG-VK on)
  const shown = (f) => (!f.nv || nvOk) && (!f.showIf || f.showIf(model));
  const model = {};
  const orig = {};
  for (const f of all) {
    model[f.id] = READ[f.type](f);
    // a stored value this platform cannot use shows as the default and counts as changed, so the next Save replaces it
    orig[f.id] = f.unsupported?.includes(get(f, f.key, f.def)) ? JSON.stringify(get(f, f.key, f.def)) : JSON.stringify(model[f.id]);
  }
  // the preset row counts only when picking it has to clear a fixed render size; otherwise the fields it set are the changes
  const dirtyFields = () => all.filter((f) => (f.type !== 'preset' || fixedRender) && JSON.stringify(model[f.id]) !== orig[f.id]);

  // ----- markup -----
  const dis = (f) => (exists[f.file] && !f.lockedIf?.(model, fieldCtx) ? '' : 'disabled');
  const CONTROL = {
    select: (f, x) => {
      // an NVIDIA-only option is kept when it is the value already set, so the file is shown as it is rather than as a raw word
      const avail = f.options.filter(([v, , o]) => !o?.nv || nvOk || String(v) === String(x));
      const opts = avail.some(([v]) => String(v) === String(x)) ? avail : [...avail, [x, x]];
      return `<select id="f-${f.id}" data-f="${f.id}" ${dis(f)}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(x) ? 'selected' : ''}>${esc(optName(l))}</option>`).join('')}</select>`;
    },
    rate: (f, x) => CONTROL.select(f, x),
    toggle: (f, x) => `<label class="switch"><input type="checkbox" id="f-${f.id}" data-f="${f.id}" ${x ? 'checked' : ''} ${dis(f)}>${esc(t(x ? f.onLabel || 'common.on' : f.offLabel || 'common.off'))}</label>`,
    size: (f, x) => {
      const opts = f.options.some(([v]) => v === x) ? f.options : [...f.options, [x, String(x).replace('x', ' x ')]];
      return `<select id="f-${f.id}" data-f="${f.id}" ${dis(f)}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${v === x ? 'selected' : ''}>${esc(optName(l))}</option>`).join('')}</select>`;
    },
    autonum: (f, x) => `<label class="switch"><input type="checkbox" id="f-${f.id}" data-f="${f.id}" data-part="auto" ${x.auto ? 'checked' : ''} ${dis(f)}>${esc(optName(f.autoLabel))}</label><input type="number" data-f="${f.id}" data-part="num" min="${f.min}" max="${f.max}" value="${esc(x.num)}" ${x.auto || !exists[f.file] ? 'disabled' : ''}><span class="unit">${esc(optName(f.unit))}</span>`,
    range: (f, x) => `<input type="range" id="f-${f.id}" data-f="${f.id}" min="0" max="${Math.round((f.max ?? 1) * 100)}" step="1" value="${x}" ${dis(f)}><span class="unit" data-readout="${f.id}" style="min-width: 4.5ch; text-align: right">${x}${esc(optName(f.unit || '%'))}</span>`,
    text: (f, x) => `<input type="text" id="f-${f.id}" data-f="${f.id}" spellcheck="false" placeholder="${esc(f.placeholder ? optName(f.placeholder) : '')}" value="${esc(x)}" ${dis(f)}>`,
    number: (f, x) => `<input type="number" id="f-${f.id}" data-f="${f.id}" min="${f.min}" max="${f.max}" value="${esc(x)}" ${dis(f)}>`,
    path: (f, x) => `<input type="text" id="f-${f.id}" data-f="${f.id}" spellcheck="false" placeholder="${esc(f.placeholder ? optName(f.placeholder) : '')}" value="${esc(x)}" ${dis(f)}><button class="btn" data-browse="${f.id}" ${dis(f)}>${icon('folder-open')}${esc(t('common.browse'))}</button>`,
    preset: (f) => {
      const cur = presetOf(model, fixedRender);
      const custom = cur ? '' : `<option value="" selected disabled>${esc(t('settings.preset.custom'))}</option>`;
      return `<select id="f-${f.id}" data-f="${f.id}" ${dis(f)}>${custom}${PRESETS.map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${esc(t(l))}</option>`).join('')}</select>`;
    },
  };

  /** The short form of a hint, or '' when it has none (or it would say the same as the full one). */
  const shortOf = (f) => {
    if (!f.hint) return '';
    const k = f.hint.replace(/\.hint\w*$/, '.short') // settings.x.hint and settings.x.hintLinux share settings.x.short;
    const s = t(k);
    return s === k || s === t(f.hint) ? '' : s;
  };
  const fieldHtml = (f) => {
    const short = shortOf(f);
    const open = openHints.has(f.id);
    const more = short
      ? `<button class="more" data-more="${f.id}" aria-expanded="${open}" aria-controls="hint-${f.id}" title="${esc(t('settings.moreInfo'))}" aria-label="${esc(t('settings.moreInfo'))}">${icon('info')}</button>`
      : '';
    return `
    <div class="field ${open ? 'open' : ''}">
      <div class="name">
        <div class="name-row"><label for="f-${f.id}">${esc(t(f.label))}</label>${f.restart ? `<span class="chip restart" title="${esc(t('settings.nextStartTip'))}">${icon('rotate-cw')}${esc(t('settings.nextStart'))}</span>` : ''}</div>
        ${f.hint ? `<span class="hint">${esc(short || t(f.hint))}${more ? `&nbsp;${more}` : ''}</span>` : ''}
        ${f.status ? `<span class="hint" data-status="${f.status}">${esc(t('settings.files.checking'))}</span>` : ''}
        ${f.lsfgNote ? `<span class="hint" data-lsfg-note>${esc(t('settings.files.checking'))}</span>` : ''}
      </div>
      <div class="control">${CONTROL[f.type](f, model[f.id])}</div>
      ${short ? `<div class="hint-full" id="hint-${f.id}" ${open ? '' : 'hidden'}>${esc(t(f.hint))}</div>` : ''}
      ${f.status ? `<div class="fetch" data-fetch="${f.status}"></div>` : ''}
    </div>`;
  };

  const groupHtml = (g) => {
    const fields = g.fields.filter(shown);
    if (!fields.length) return '';
    const note = g.gpuNote && !nvOk ? `<p class="faint group-note">${esc(t('settings.gpu.noNvidia'))}</p>` : '';
    return `<div class="card"><h2>${icon(g.icon)}${esc(t(g.title))}</h2>${note}${fields.map(fieldHtml).join('')}</div>`;
  };
  // controls.ini is written by the game on its first start: the tabs with its options say so where the disabled options are
  const needsControls = (tab) => !data.controls_exists && tab.groups.some((g) => g.fields.some((f) => f.file === 'controls'));
  const panelHtml = (tab) => `
    <div class="tabpanel" role="tabpanel" id="panel-${tab.id}" aria-labelledby="tab-${tab.id}" ${tab.id === activeTab ? '' : 'hidden'}>
      ${needsControls(tab) ? `<div class="banner info">${icon('info')}<div>${esc(t('settings.noControlsBefore'))}<code>${esc(data.game_dir)}</code>${esc(t('settings.noControlsAfter'))}</div></div>` : ''}
      ${tab.groups.map(groupHtml).join('')}
    </div>`;
  const dirtyTabs = () => new Set(dirtyFields().map((f) => f.tab));
  const tabsHtml = () => {
    const dirty = dirtyTabs();
    return `<div id="tabs-anchor"></div><div class="tabbar"><div class="tabs" role="tablist" aria-label="${esc(t('settings.title'))}">${tabs.map((tab) => `
      <button class="tab ${dirty.has(tab.id) ? 'dirty' : ''}" role="tab" id="tab-${tab.id}" data-tab="${tab.id}" aria-controls="panel-${tab.id}"
        title="${esc(t(tab.title))}" aria-selected="${tab.id === activeTab}" tabindex="${tab.id === activeTab ? 0 : -1}">${icon(tab.icon)}<span>${esc(t(tab.title))}</span><span class="dot" aria-hidden="true"></span></button>`).join('')}
    </div></div>`;
  };

  const barText = (n) => (n ? (n > 1 ? t('common.unsavedMany', { count: n }) : t('common.unsavedOne')) : t('common.noChanges'));

  function draw() {
    const n = dirtyFields().length;
    el.innerHTML = `
      <div class="page">
        <div class="hero">
          <h1>${esc(t('settings.title'))}</h1>
          <p class="dim">${esc(t('settings.introBefore'))}<code>${esc(data.game_dir)}</code>${esc(t('settings.introAfter'))}</p>
        </div>
        ${status.state === 'running' ? `<div class="banner warn">${icon('triangle-alert')}<div>${esc(t('settings.gameRunning'))}</div></div>` : ''}
        ${tabsHtml()}
        ${tabs.map(panelHtml).join('')}
        <div class="savebar">
          <span class="dim">${esc(barText(n))}</span>
          <div class="row">
            <button class="btn quiet" id="revert" ${n ? '' : 'disabled'}>${icon('undo-2')}${esc(t('common.revert'))}</button>
            <button class="btn primary" id="save" ${n ? '' : 'disabled'}>${icon('save')}${esc(t('common.save'))}</button>
          </div>
        </div>
      </div>`;
    refreshIcons();
    bind();
    checkFiles();
    checkLsfg();
    drawFetch();
  }

  /** Puts the (unsaved) model back into freshly drawn markup. */
  function applyModel() {
    for (const f of all) {
      const x = model[f.id];
      if (f.type === 'autonum') {
        const a = el.querySelector(`[data-f="${f.id}"][data-part="auto"]`);
        const num = el.querySelector(`[data-f="${f.id}"][data-part="num"]`);
        if (a) a.checked = x.auto;
        if (num) {
          num.value = x.num;
          num.disabled = x.auto || !exists[f.file];
        }
      } else if (f.type === 'range') {
        const r = el.querySelector(`[data-f="${f.id}"]`);
        if (r) r.value = x;
        const out = el.querySelector(`[data-readout="${f.id}"]`);
        if (out) out.textContent = `${x}${optName(f.unit || '%')}`;
      } else if (f.type === 'toggle') {
        const c = el.querySelector(`[data-f="${f.id}"]`);
        if (c) c.checked = !!x;
      } else if (f.type !== 'preset') {
        const c = el.querySelector(`[data-f="${f.id}"]`);
        if (c) c.value = x;
      }
    }
    updateBar();
  }

  // The upscaler files the game will find with the folders as typed (found only when every file a feature needs is in one folder).
  let checkSeq = 0;
  async function checkFiles() {
    const seq = ++checkSeq;
    const r = await api('upscaler_files', { fsrDir: fwd(model.fsr_dll || ''), dlssDir: fwd(model.dlss_dll || ''), xessDir: fwd(model.xess_dll || '') })
      .catch(() => null);
    if (seq !== checkSeq || !r) return;
    const line = (name, x) => `${t(name)}: ${x.found ? t('settings.files.found', { detail: x.detail }) : t('settings.files.notFound', { detail: fileDetail(x.detail) })}`;
    // `note`: a sentence appended in its own span (the Streamline SDK files the game does not use)
    const put = (which, text, note) =>
      el.querySelectorAll(`[data-status="${which}"]`).forEach((n) => {
        n.textContent = text;
        if (note) {
          const w = document.createElement('span');
          w.className = 'status-note';
          w.textContent = ` ${note}`;
          n.appendChild(w);
        }
      });
    // FSR 3 frame generation is not offered on Linux, so its file is not listed there
    put('fsr', IS_LINUX ? `${line('settings.files.upscaling', r.fsr_upscaling)}.` : `${line('settings.files.upscaling', r.fsr_upscaling)}. ${line('settings.files.frameGen', r.fsr_framegen)}.`);
    // With DLSS chosen (upscaling or frame generation), a folder that holds the rest of the Streamline SDK gets a note about the files this game does not use.
    const dlssOn = model.upscaler === 'dlss' || model.frame_gen === 'dlss';
    const unused = dlssOn && r.dlss.found && Array.isArray(r.dlss_unused) ? r.dlss_unused : [];
    put('dlss', `${line('settings.files.dlss', r.dlss)}. ${line('settings.files.dlssFrameGen', r.dlss_framegen)}.`,
      unused.length ? t('settings.files.dlssUnused', { files: unused.join(', ') }) : '');
    if (r.xess) put('xess', `${line('settings.files.xess', r.xess)}.`);
  }
  // LSFG-VK: is the layer installed, and is Lossless Scaling on its lsfg-vk branch (lsfg.rs)?
  async function checkLsfg() {
    const note = el.querySelector('[data-lsfg-note]');
    if (!note) return;
    const was = fieldCtx.lsfgReady;
    const r = await api('lsfg_status').catch(() => null);
    if (!live()) return;
    if (!r) { note.textContent = ''; return; }
    lsfgSt = r;
    if (fieldCtx.lsfgReady !== was) { draw(); return; }
    note.textContent = [
      r.layer ? t('settings.lsfg.layerFound') : t('settings.lsfg.layerMissing'),
      r.dll ? t('settings.lsfg.dllFound', { path: r.dll }) : t(r.wrong_branch ? 'settings.lsfg.wrongBranch' : 'settings.lsfg.dllMissing'),
    ].join(' ');
    if (!r.layer) {
      // the layer is installed on request (lsfg.rs install): CachyOS's package through Shelly, else LSFG-VK's build into ~/.local
      const how = document.createElement('span');
      how.textContent = t(r.install === 'shelly' ? 'settings.lsfg.install.shelly' : 'settings.lsfg.install.local');
      const row = document.createElement('div');
      row.className = 'row';
      row.style.marginTop = 'var(--space-2)';
      const go = document.createElement('button');
      go.className = 'btn';
      const busy = r.installing || lsfgHere;
      go.disabled = busy;
      go.textContent = t(busy ? 'settings.lsfg.install.running' : 'settings.lsfg.install.button');
      go.addEventListener('click', (ev) => { ev.preventDefault(); installLsfg(); });
      const link = document.createElement('button');
      link.className = 'btn quiet';
      link.textContent = t('settings.lsfg.installLink');
      link.addEventListener('click', (ev) => { ev.preventDefault(); api('open_lsfg_install').catch((e) => toast(errText(e), 'error')); });
      row.append(go, link);
      note.append(document.createElement('br'), how, row);
      // an install started from an earlier visit of this page: follow it until it ends
      clearTimeout(lsfgTimer);
      if (r.installing && !lsfgHere) lsfgTimer = setTimeout(checkLsfg, 1000);
    }
  }
  async function installLsfg() {
    if (lsfgHere) return;
    lsfgHere = true;
    const done = api('install_lsfg');
    checkLsfg();
    try {
      toast(await done, 'ok');
    } catch (e) {
      toast(errText(e), 'error');
    }
    lsfgHere = false;
    if (live()) checkLsfg();
  }
  let checkTimer = 0;

  // ----- downloading the upscaler files from the vendors (fetch.rs) -----
  // Nothing is downloaded before the player has seen where the files come from and under whose licence, and accepted.
  const FETCH = {
    fsr: [['fsr', 'fsr']],
    xess: [['xess', 'xess']],
    dlss: [['dlss', 'dlss'], ['dlss_framegen', 'dlssFg']],
  };
  let fetchAsk = null; // the kind waiting for the player's accept
  let fetchSt = { state: 'idle', kind: '' };
  let fetchTimer = 0;

  function fetchHtml(which) {
    const kinds = FETCH[which];
    const busy = fetchSt.state === 'running';
    if (busy && kinds.some(([k]) => k === fetchSt.kind)) {
      const pct = fetchSt.total ? Math.min(100, Math.round((fetchSt.done / fetchSt.total) * 100)) : 0;
      return `<div class="banner info">${icon('download')}<div style="flex:1"><div>${esc(fetchSt.message)}</div>${fetchSt.total ? `<progress max="100" value="${pct}" style="width:100%"></progress>` : ''}</div><button class="btn quiet" data-fetch-stop>${esc(t('settings.fetch.stop'))}</button></div>`;
    }
    const ask = kinds.find(([k]) => k === fetchAsk);
    if (ask) {
      return `<div class="banner warn">${icon('triangle-alert')}<div style="flex:1">${esc(t(`settings.fetch.ask.${ask[1]}`))}
        <div class="row" style="margin-top: var(--space-3)"><button class="btn primary" data-fetch-go="${ask[0]}">${icon('download')}${esc(t('settings.fetch.accept'))}</button><button class="btn quiet" data-fetch-no>${esc(t('settings.fetch.no'))}</button></div></div></div>`;
    }
    return `<div class="row">${kinds.map(([k, id]) => `<button class="btn" data-fetch-ask="${k}" ${busy ? 'disabled' : ''}>${icon('download')}${esc(t(`settings.fetch.button.${id}`))}</button>`).join('')}</div>`;
  }

  function drawFetch() {
    el.querySelectorAll('[data-fetch]').forEach((box) => {
      box.innerHTML = fetchHtml(box.dataset.fetch);
      box.querySelectorAll('[data-fetch-ask]').forEach((b) => b.addEventListener('click', () => { fetchAsk = b.dataset.fetchAsk; drawFetch(); }));
      box.querySelector('[data-fetch-no]')?.addEventListener('click', () => { fetchAsk = null; drawFetch(); });
      box.querySelector('[data-fetch-go]')?.addEventListener('click', (ev) => startFetch(ev.currentTarget.dataset.fetchGo));
      box.querySelector('[data-fetch-stop]')?.addEventListener('click', () => api('cancel_fetch'));
    });
    refreshIcons();
  }

  async function startFetch(kind) {
    fetchAsk = null;
    try {
      // into the folder set for this upscaler, as the file check above looks there first
      const dir = kind === 'fsr' ? model.fsr_dll : kind === 'xess' ? model.xess_dll : model.dlss_dll;
      await api('fetch_upscaler', { kind, dir: fwd(dir || '') });
    } catch (e) {
      toast(errText(e), 'error');
      return;
    }
    fetchSt = { state: 'running', kind, message: '', done: 0, total: 0 };
    pollFetch();
  }

  async function pollFetch() {
    clearTimeout(fetchTimer);
    if (!el.isConnected) return; // the page was left; the download carries on and the next visit picks its state up
    try {
      fetchSt = await api('fetch_status');
    } catch (e) {
      fetchSt = { state: 'idle', kind: '' };
    }
    drawFetch();
    if (fetchSt.state === 'running') {
      fetchTimer = setTimeout(pollFetch, 400);
    } else if (fetchSt.state === 'done' || fetchSt.state === 'error' || fetchSt.state === 'cancelled') {
      if (fetchSt.state !== 'cancelled') toast(fetchSt.message, fetchSt.state === 'done' ? 'ok' : 'error');
      fetchSt = { state: 'idle', kind: '' };
      drawFetch();
      checkFiles();
    }
  }

  /** Shows one tab without redrawing: every panel is in the markup, so edits on the others stay where they are. */
  function selectTab(id, focus) {
    activeTab = id;
    el.querySelectorAll('[role="tab"]').forEach((b) => {
      const on = b.dataset.tab === id;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    el.querySelectorAll('.tabpanel').forEach((p) => { p.hidden = p.id !== `panel-${id}`; });
    // scrolled past the tabs: bring the top of the new tab into view under the (sticky) bar
    const anchor = el.querySelector('#tabs-anchor');
    if (anchor && el.scrollTop > anchor.offsetTop) el.scrollTop = anchor.offsetTop;
  }

  function bind() {
    el.querySelectorAll('[data-more]').forEach((b) =>
      b.addEventListener('click', () => {
        const id = b.dataset.more;
        const open = !openHints.has(id);
        if (open) openHints.add(id);
        else openHints.delete(id);
        b.setAttribute('aria-expanded', String(open));
        b.closest('.field').classList.toggle('open', open);
        el.querySelector(`#hint-${id}`).hidden = !open;
      }),
    );
    el.querySelectorAll('[role="tab"]').forEach((b) => {
      b.addEventListener('click', () => selectTab(b.dataset.tab));
      b.addEventListener('keydown', (ev) => {
        const i = TABS.findIndex((x) => x.id === b.dataset.tab);
        const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[ev.key];
        if (to === undefined) return;
        ev.preventDefault();
        selectTab(TABS[(to + TABS.length) % TABS.length].id, true);
      });
    });
    const byId = Object.fromEntries(all.map((f) => [f.id, f]));
    el.querySelectorAll('[data-f]').forEach((input) => {
      const f = byId[input.dataset.f];
      const part = input.dataset.part;
      const onChange = () => {
        const v = input.type === 'checkbox' ? input.checked : input.value;
        if (f.type === 'preset') {
          const p = PRESETS.find(([id]) => id === v);
          if (p) Object.assign(model, p[2], { preset: v });
          draw();
          return;
        }
        if (part) model[f.id] = { ...model[f.id], [part]: v };
        else model[f.id] = f.type === 'range' ? parseInt(v, 10) : v;
        if (f.type === 'range') {
          el.querySelector(`[data-readout="${f.id}"]`).textContent = `${model[f.id]}${optName(f.unit || '%')}`;
          updateBar();
        } else if (input.type === 'text' || input.type === 'number') {
          updateBar();
          if (f.status) {
            clearTimeout(checkTimer);
            checkTimer = setTimeout(checkFiles, 300);
          }
        } else {
          draw(); // toggles and selects change dependent markup (labels, disabled inputs)
        }
      };
      input.addEventListener(input.type === 'range' || input.type === 'text' || input.type === 'number' ? 'input' : 'change', onChange);
    });
    el.querySelectorAll('[data-browse]').forEach((b) =>
      b.addEventListener('click', async () => {
        const f = byId[b.dataset.browse];
        const picked = await api('pick_folder', { title: t('common.choose', { label: t(f.label) }), start: model[f.id] || null });
        if (picked) {
          model[f.id] = picked;
          draw();
        }
      }),
    );
    el.querySelector('#revert').addEventListener('click', () => {
      for (const f of all) model[f.id] = JSON.parse(orig[f.id]);
      draw();
    });
    el.querySelector('#save').addEventListener('click', save);
  }

  function updateBar() {
    const n = dirtyFields().length;
    const bar = el.querySelector('.savebar');
    if (!bar) return;
    bar.querySelector('span').textContent = barText(n);
    bar.querySelector('#save').disabled = !n;
    bar.querySelector('#revert').disabled = !n;
    const dirty = dirtyTabs();
    el.querySelectorAll('[role="tab"]').forEach((b) => b.classList.toggle('dirty', dirty.has(b.dataset.tab)));
  }

  async function save() {
    const edits = [];
    const cfgNew = { ...cfg };
    for (const f of dirtyFields()) {
      for (const [sec, key, value] of WRITE[f.type](f, model[f.id])) {
        if (f.file === 'config') {
          cfgNew[key] = ['minimize_on_play', 'send_reports', 'lsfg', 'lsfg_performance'].includes(key) ? value === '1' : key === 'jobs' ? parseInt(value, 10) || 0 : key === 'lsfg_multiplier' ? clamp(parseInt(value, 10) || 2, 2, 4) : key === 'lsfg_flow_scale' ? String(value) : key === 'runner' ? String(value).trim() : fwd(value);
        } else {
          edits.push({ file: f.file, section: sec, key, value });
        }
      }
    }
    try {
      if (edits.length) await api('write_settings', { edits });
      if (JSON.stringify(cfgNew) !== JSON.stringify(cfg)) await api('save_config', { config: cfgNew });
      toast(t('common.settingsSaved'), 'ok');
      // the Steam shortcut carries some launcher settings (LSFG-VK): say when it no longer matches them
      if (IS_LINUX) {
        const st = await api('steam_status').catch(() => null);
        if (st && st.stale) toast(t('play.steamStale'), 'error');
      }
      ctx.go('settings');
    } catch (e) {
      toast(errText(e), 'error');
    }
  }

  if (!live()) return; // another page was opened while this one was loading
  // a language switch: the same page in the new words, with the unsaved edits kept (draw() builds it from the model)
  ctx.onLanguage(() => {
    draw();
    applyModel();
  });
  draw();
  pollFetch(); // picks up a download still running, or tells how one that ended while the page was closed went
}
