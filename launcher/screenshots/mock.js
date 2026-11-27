// Stands in for the Rust side so the launcher runs in a plain browser, for screenshots (capture.mjs puts this module in
// front of src/main.js). `?scenario=` picks the state to show. Not part of the app: `npm run build` never includes it.
import { mockIPC, mockWindows } from '@tauri-apps/api/mocks';
import '@fontsource-variable/open-sans';
import '@fontsource/cascadia-mono';
import conf from '../src-tauri/tauri.conf.json';

// A made-up release-package install on Windows, which is what most players have.
const INSTALL = 'C:/Games/fefw-recomp';
const DUMP = 'D:/Dumps/FortunesWeave';
const UPDATE = '131072';

const config = {
  repo_dir: '', dump_dir: DUMP, game_dir: '', jobs: 0, minimize_on_play: false, skip_update_check: true,
  send_reports: false, reports_prompted: true, decky_declined: false, runner: '', lang: 'en',
  lsfg: false, lsfg_multiplier: 2, lsfg_flow_scale: '1', lsfg_performance: false,
};

const candidate = {
  kind: 'update',
  version: UPDATE,
  path: `${DUMP}/updates/${UPDATE}`,
  exefs: `${DUMP}/updates/${UPDATE}/exefs`,
  romfs: `${DUMP}/updates/${UPDATE}/romfs`,
  ok: true,
  checks: [
    ...['main', 'sdk', 'subsdk0', 'subsdk1', 'subsdk2', 'rtld'].map((f) => `exefs/${f}`),
    'romfs/',
    'exefs/main is decrypted',
    `supported version (update ${UPDATE})`,
  ].map((label) => ({ label, ok: true })),
};

const STEPS = [
  ['build', 'Recompile the game code from your dump'],
  ['shaders', 'Translate the shaders'],
  ['ini', 'Write fefw.ini'],
  ['prepare', 'Prepare the shaders for your GPU'],
];

function setupStatus({ running, log = [] } = {}) {
  return {
    progress: running ?? { state: 'idle', step: '', done: 0, total: 0, message: '' },
    steps: STEPS.map(([id, title]) => ({ id, title, detail: '', state: !running ? 'done' : id === running.step ? 'running' : 'todo' })),
    complete: !running,
    tools: [],
    log,
    jobs: 15,
    package: true,
    cpu_problem: null,
    cpu_note: null,
  };
}

const found = (detail) => ({ found: true, detail });

/** Replies every scenario shares: a command name, then its value or a function of the arguments. */
const defaults = {
  platform: 'windows',
  get_config: config,
  ui_language: 'en',
  list_locales: [],
  get_overview: { repo_dir: INSTALL, repo_ok: true, game_dir: INSTALL, exe: `${INSTALL}/fefw.exe`, exe_exists: true },
  setup_status: setupStatus(),
  validate_dump: {
    path: DUMP, exists: true, errors: [], candidates: [candidate], chosen: candidate,
    dlc_dir: `${DUMP}/dlc`, dlc: ['04007EF01F8AF001'],
  },
  game_status: { state: 'idle', code: null, crashed: false, files: [], message: '' },
  memory_warning: null,
  steam_status: { available: false },
  decky_offer: null,
  gpu_info: { known: true, nvidia: true, amd: false, intel: false },
  read_settings: {
    game_dir: INSTALL,
    game_exists: true,
    controls_exists: true,
    game: {
      'fefw.output': '1440p', 'fefw.fps60': '1', 'fefw.render_ratio': '0.67', 'fefw.shadow_size': '4096',
      'fefw.quality': 'docked', 'dlss.upscaler': 'dlss',
    },
    controls: {},
  },
  upscaler_files: {
    fsr_upscaling: found(`${INSTALL}/upscalers/fsr3`),
    fsr_framegen: found(`${INSTALL}/upscalers/fsr3`),
    dlss: found(`${INSTALL}/upscalers/streamline`),
    dlss_framegen: found(`${INSTALL}/upscalers/streamline`),
    dlss_dir: `${INSTALL}/upscalers/streamline`,
    dlss_unused: [],
    xess: found(`${INSTALL}/upscalers/xess`),
  },
  fetch_status: { state: 'idle', kind: '', message: '', done: 0, total: 0 },
  update_status: { state: 'idle', phase: '', done: 0, total: 0, message: '' },
  check_update: { current: conf.version, latest: null, newer: false, package: true },
  'plugin:app|version': conf.version,
};

/** What each scenario changes. */
const SCENARIOS = {
  ready: {},
  building: {
    get_overview: { ...defaults.get_overview, exe_exists: false },
    setup_status: setupStatus({
      running: { state: 'running', step: 'build', done: 214, total: 486, message: 'Compiling: 214 of 486' },
      log: ['Recompiling the game code from your dump', 'Game files match update 131072', 'Game code translated', 'Compiling: 214 of 486'],
    }),
  },
};

const name = new URLSearchParams(location.search).get('scenario') ?? 'ready';
if (!SCENARIOS[name]) throw new Error(`unknown scenario: ${name}`);
const replies = { ...defaults, ...SCENARIOS[name] };

// Commands with no reply here. The launcher swallows some failed calls, so capture.mjs reads this list and fails on it.
window.__unmocked = [];

mockWindows('main');
mockIPC(
  (cmd, args) => {
    if (!(cmd in replies)) {
      window.__unmocked.push(cmd);
      throw new Error(`no mock reply for ${cmd}`);
    }
    const reply = replies[cmd];
    // a copy: a page must not be able to change what the next call returns
    return structuredClone(typeof reply === 'function' ? reply(args) : reply);
  },
  { shouldMockEvents: true },
);

// The theme asks for Windows fonts. Naming bundled ones keeps captures the same on every machine, CI included.
const root = document.documentElement.style;
root.setProperty('--font-ui', '"Open Sans Variable", sans-serif');
root.setProperty('--font-display', '"Open Sans Variable", sans-serif');
root.setProperty('--font-mono', '"Cascadia Mono", monospace');
