// SPDX-License-Identifier: GPL-3.0-or-later
// Replaces the Steam game page's tab strip with Setup, Settings, and About tabs for the fefw-recomp shortcut.
//
// The game page renders its tabs through a component it reads from a React context (a Provider above the page sets it).
// The component tree between the page and that consumer includes a class whose `render` is redefined on the instance after
// the first call, so a tree patch through it only sees the first render. Instead, the page's output is wrapped in a nearer
// Provider for the same context whose value is the original tabs component with its output patched.
//
// Under the tabs component, the component holding `fnOnCancelFromTabHeader` builds the tab list and renders the tab strip
// (`tabs: [{ id, title, content }]`); for the fefw-recomp shortcut, `tabs` is replaced with Setup, Settings, and About.
import { ConfirmModal, createReactTreePatcher, findInReactTree, showModal } from "@decky/ui";
import { cloneElement, useContext, type ComponentType, type Context, type ReactElement, type ReactNode } from "react";
import { FaHammer, FaTimes } from "react-icons/fa";
import { AboutTab, SettingsTab, SetupTab } from "./game-tabs";
import { ProgressTrack } from "./progress-bar";
import { ACTIVITY_TAB_ID, replaceTabStrip, toUnsignedAppId } from "./shortcuts";
import type { SleepStatus } from "./sleep";
import {
  cancelJob,
  getPlayButtonState,
  hasUpdateAvailable,
  setTabsPatchActive,
  startJob,
  useHasUpdateAvailable,
  useIsFefwShortcut,
  usePlayButtonKey,
} from "./store";

const SETTINGS_TAB_ID = "FefwSettings";
const ABOUT_TAB_ID = "FefwAbout";

type Tab = { id: string; title: string; content: ReactElement };
type TabsComponent = ComponentType<Record<string, unknown>>;

const warned = new Set<string>();
function warnOnce(message: string, ...rest: unknown[]) {
  if (warned.has(message)) return;
  warned.add(message);
  console.warn("[FEFW Recomp]", message, ...rest);
}

// The context is found by its default value: a fallback tabs component taking `setSections` and `concise`.
// Matching on that, not the module id or export name, survives Steam rebuilds.
//
// The module defining it is loaded after Decky indexes Steam's modules at startup, so `findModuleExport` misses it.
// This searches webpack's module factories directly, requiring only the few whose source mentions the same names.
//
// The default is read from `_currentValue2`: this runs while the page renders, inside Steam's Provider, and React DOM
// swaps the Provider's value into `_currentValue` for that time. `_currentValue2` keeps the default.
function isTabsContext(e: unknown): e is Context<TabsComponent> {
  const ctx = e as { $$typeof?: symbol; _currentValue2?: unknown } | null | undefined;
  const fallback = ctx?._currentValue2;
  return (
    ctx?.$$typeof === Symbol.for("react.context") &&
    typeof fallback === "function" &&
    /setSections/.test(`${fallback}`) &&
    /concise/.test(`${fallback}`)
  );
}

let tabsContext: Context<TabsComponent> | null | undefined;
function findTabsContext(): Context<TabsComponent> | null {
  if (tabsContext !== undefined) return tabsContext;
  tabsContext = null;
  try {
    type WebpackRequire = ((id: string) => Record<string, unknown> | undefined) & { m?: Record<string, unknown> };
    const chunks = (window as unknown as { webpackChunksteamui?: unknown[] }).webpackChunksteamui;
    if (!Array.isArray(chunks)) {
      warnOnce("Steam webpack chunk array not found; game-page tabs disabled.");
      return null;
    }
    let req: WebpackRequire | undefined;
    chunks.push([[Symbol("fefw-recomp")], {}, (r: WebpackRequire) => (req = r)]);
    for (const id of Object.keys(req?.m ?? {})) {
      try {
        const src = `${req?.m?.[id]}`;
        if (!src.includes("createContext(") || !src.includes("concise") || !src.includes("setSections")) continue;
        const found = Object.values(req?.(id) ?? {}).find(isTabsContext);
        if (found) return (tabsContext = found);
      } catch {
        // Ignore a candidate module that throws or has uninitialized circular exports.
      }
    }
  } catch (err) {
    warnOnce("Searching for the game page tabs context failed; game-page tabs disabled.", err);
    return null;
  }
  warnOnce("Game page tabs context not found; game-page tabs disabled.");
  return null;
}

function createTabsPatcher(sleep: () => SleepStatus) {
  return createReactTreePatcher(
    [(t: unknown) => findInReactTree(t, (x: { props?: { fnOnCancelFromTabHeader?: unknown } }) => !!x?.props?.fnOnCancelFromTabHeader)],
    (_args: Array<Record<string, unknown>>, ret?: ReactElement) => {
      try {
        const strip = findInReactTree(
          ret,
          (x: { props?: { tabs?: Tab[] } }) =>
            Array.isArray(x?.props?.tabs) &&
            x.props.tabs.some((t: Tab) => t?.id === ACTIVITY_TAB_ID),
        );
        if (!strip) {
          warnOnce("Activity tab not found on the game page; game-page tabs disabled.");
          return ret;
        }
        const replacementTabs: Tab[] = [
          { id: ACTIVITY_TAB_ID, title: "Setup", content: <SetupTab sleep={sleep} /> },
          { id: SETTINGS_TAB_ID, title: "Settings", content: <SettingsTab sleep={sleep} /> },
          { id: ABOUT_TAB_ID, title: hasUpdateAvailable() ? "About •" : "About", content: <AboutTab sleep={sleep} /> },
        ];
        const next = replaceTabStrip(replacementTabs, strip.props.activeTab);
        strip.props.tabs = next.tabs;
        if (next.activeTab !== undefined && "activeTab" in strip.props) {
          strip.props.activeTab = next.activeTab;
        }
        setTabsPatchActive(true);
      } catch (err) {
        warnOnce("Failed to patch the game page tabs:", err);
      }
      return ret;
    },
    "FefwGamePageTabs",
  );
}

const hasPlayRow = (x: { props?: Record<string, unknown> }) =>
  !!x?.props && "bShowingLaunchDetails" in x.props && "onGameInfoToggle" in x.props;
const hasPlayBtn = (x: { props?: Record<string, unknown> }) =>
  !!x?.props && "bShowStreamingSelector" in x.props;

// Matches the project's icon / Steam library art purple (launcher/icon/make_steam_art.py INNER = #3a2c70).
const FEFW_PURPLE_BG = "linear-gradient(to right, #5340a0, #3a2c70)";
const FEFW_PROGRESS_FILL = "#7b66d4";

// Native-style status readout beside the Play/Cancel button, matching Steam's "DOWNLOADING / 2% Complete" block.
function FefwPlayStatus() {
  usePlayButtonKey();
  const playState = getPlayButtonState();
  if (playState.kind !== "job") return null;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        marginLeft: "20px",
        marginRight: "8px",
      }}
    >
      <div
        style={{
          fontSize: "12px",
          fontWeight: 500,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "rgba(255, 255, 255, 0.65)",
          lineHeight: "1.2",
        }}
      >
        {playState.statusTitle}
      </div>
      <div
        style={{
          fontSize: "15px",
          fontWeight: 500,
          color: "#ffffff",
          lineHeight: "1.3",
          marginTop: "2px",
        }}
      >
        {playState.statusDetail}
      </div>
    </div>
  );
}

// Preserves Steam's native Play button flex alignment (left-aligned 24px icon + 16px margin + label).
function FefwPlayButton({ orig }: { orig: ReactElement<Record<string, unknown>> }) {
  usePlayButtonKey();
  const playState = getPlayButtonState();
  if (playState.kind === "play") return orig;

  const baseStyle = (orig.props.style as Record<string, unknown> | undefined) ?? {};
  const iconStyle = { width: "20px", height: "20px", marginRight: "16px", flexShrink: 0 };
  const labelStyle = { fontSize: "16px", fontWeight: 500 };

  if (playState.kind === "blocked") {
    // nothing here can fix it yet (the game files are chosen in the launcher's window): say so rather than start a broken shortcut
    const explain = () =>
      showModal(
        <ConfirmModal
          strTitle="Finish setting up in Desktop Mode"
          strDescription={`${playState.problem} Open fefw-launcher in Desktop Mode, then come back here to play.`}
          strOKButtonText="OK"
          bAlertDialog
        />,
      );
    return cloneElement(orig, {
      onMouseUp: undefined,
      onActivate: undefined,
      onClick: explain,
      style: { ...baseStyle, background: FEFW_PURPLE_BG, color: "#ffffff" },
      children: (
        <>
          <FaHammer style={iconStyle} />
          <div style={labelStyle}>{playState.label}</div>
        </>
      ),
    });
  }

  if (playState.kind === "setup") {
    return cloneElement(orig, {
      onMouseUp: undefined,
      onActivate: undefined,
      onClick: playState.disabled ? undefined : () => void startJob("rebuild"),
      style: {
        ...baseStyle,
        background: FEFW_PURPLE_BG,
        color: "#ffffff",
        opacity: playState.disabled ? 0.6 : undefined,
      },
      children: (
        <>
          <FaHammer style={iconStyle} />
          <div style={labelStyle}>{playState.label}</div>
        </>
      ),
    });
  }

  const pct = playState.fraction !== null ? Math.max(0, Math.min(100, Math.round(playState.fraction * 100))) : null;

  const confirmCancel = () => {
    showModal(
      <ConfirmModal
        strTitle={`Cancel ${playState.statusTitle.toLowerCase()}?`}
        strDescription="The current job will stop. You can start it again from Setup."
        strOKButtonText="Cancel job"
        strCancelButtonText="Keep running"
        onOK={() => void cancelJob()}
      />,
    );
  };

  return cloneElement(orig, {
    onMouseUp: undefined,
    onActivate: undefined,
    onClick: playState.cancelling ? undefined : confirmCancel,
    style: {
      ...baseStyle,
      position: "relative",
      overflow: "hidden",
      background: FEFW_PURPLE_BG,
      color: "#ffffff",
      opacity: playState.cancelling ? 0.7 : undefined,
    },
    children: (
      <>
        <FaTimes style={iconStyle} />
        <div style={labelStyle}>{playState.buttonLabel}</div>
        {(pct !== null || playState.working) && (
          <div style={{ position: "absolute", left: 0, right: 0, bottom: 0 }}>
            <ProgressTrack
              pct={pct ?? 0}
              working={playState.working}
              height={4}
              fill={FEFW_PROGRESS_FILL}
              track="rgba(0, 0, 0, 0.35)"
              rounded={false}
            />
          </div>
        )}
      </>
    ),
  });
}

function createPlayButtonPatcher() {
  return createReactTreePatcher(
    [
      (t: unknown) => findInReactTree(t, hasPlayRow),
      (t: unknown) => findInReactTree(t, hasPlayRow),
      (t: unknown) => findInReactTree(t, hasPlayBtn),
      (t: unknown) => findInReactTree(t, hasPlayBtn),
    ],
    (_args: Array<Record<string, unknown>>, ret?: ReactElement) => {
      try {
        const isPlayFocusable = (x: { props?: Record<string, unknown> }) =>
          !!x?.props && (typeof x.props.onClick === "function" || typeof x.props.onMouseUp === "function");
        const parent = findInReactTree(
          ret,
          (x: { props?: { children?: unknown } }) => {
            const c = x?.props?.children;
            if (Array.isArray(c)) return c.some(isPlayFocusable);
            return isPlayFocusable(c as { props?: Record<string, unknown> });
          },
        );
        if (!parent) {
          warnOnce("Play button not found on the game page; Play button replacement skipped.");
          return ret;
        }
        const c = parent.props.children;
        if (Array.isArray(c)) {
          // Replace only the first clickable child (the Play button itself), leaving sibling controls like streaming arrows intact.
          const idx = c.findIndex((child: unknown) => isPlayFocusable(child as { props?: Record<string, unknown> }));
          if (idx >= 0) {
            parent.props.children = c.map((child: unknown, i: number) =>
              i === idx ? <FefwPlayButton key="fefw-play" orig={child as ReactElement<Record<string, unknown>>} /> : child,
            );
          }
        } else {
          parent.props.children = <FefwPlayButton orig={c as ReactElement<Record<string, unknown>>} />;
        }
      } catch (err) {
        warnOnce("Failed to patch the Play button:", err);
      }
      return ret;
    },
    "FefwPlayButton",
  );
}

// Inserts the native-style "BUILDING / 42% Complete" + progress bar block right beside the Play/Cancel button.
function createPlayStatusPatcher() {
  return createReactTreePatcher(
    [
      (t: unknown) => findInReactTree(t, hasPlayRow),
      (t: unknown) => findInReactTree(t, hasPlayRow),
    ],
    (_args: Array<Record<string, unknown>>, ret?: ReactElement) => {
      try {
        const containsPlayBtn = (n: unknown) => !!findInReactTree(n, hasPlayBtn);
        const row = findInReactTree(
          ret,
          (x: { props?: { children?: unknown } }) =>
            Array.isArray(x?.props?.children) && x.props.children.some(containsPlayBtn),
        );
        if (!row) return ret;
        const children: unknown[] = row.props.children;
        // Deduplicate so repeated renders or hot-reloads never insert a second status block.
        const filtered = children.filter((c) => (c as { key?: string } | null)?.key !== "fefw-play-status");
        const at = filtered.findIndex(containsPlayBtn);
        if (at < 0) return ret;
        row.props.children = [
          ...filtered.slice(0, at + 1),
          <FefwPlayStatus key="fefw-play-status" />,
          ...filtered.slice(at + 1),
        ];
      } catch (err) {
        warnOnce("Failed to insert Play bar status:", err);
      }
      return ret;
    },
    "FefwPlayStatus",
  );
}

// Creates a stable wrapper per original tabs component so React never remounts the tab strip across renders.
export function createGamePageTabsWrapper(sleep: () => SleepStatus) {
  const tabsPatcher = createTabsPatcher(sleep);
  const playButtonPatcher = createPlayButtonPatcher();
  const playStatusPatcher = createPlayStatusPatcher();
  const wrapped = new WeakMap<TabsComponent, TabsComponent>();

  function wrapTabs(Orig: TabsComponent): TabsComponent {
    // Only a plain function component can be called inline like this.
    if (typeof Orig !== "function" || Orig.prototype?.render) return Orig;
    let w = wrapped.get(Orig);
    if (!w) {
      const render = Orig as (props: Record<string, unknown>) => ReactElement;
      w = function FefwAppDetailsTabs(props: Record<string, unknown>) {
        const overview = props?.overview as { appid?: number } | undefined;
        const appid = toUnsignedAppId(overview?.appid);
        const isShortcut = useIsFefwShortcut(appid);
        useHasUpdateAvailable();
        const ret = render(props);
        if (!isShortcut) return ret;
        return playButtonPatcher([props], playStatusPatcher([props], tabsPatcher([props], ret)));
      };
      wrapped.set(Orig, w);
    }
    return w;
  }

  function GamePageTabsProvider({
    context,
    children,
  }: {
    context: Context<TabsComponent>;
    children: ReactNode;
  }) {
    const Orig = useContext(context);
    return <context.Provider value={wrapTabs(Orig)}>{children}</context.Provider>;
  }

  return function wrapGamePageTabs(ret: ReactElement | undefined): ReactElement | undefined {
    const context = findTabsContext();
    if (!context || !ret) return ret;
    return <GamePageTabsProvider context={context}>{ret}</GamePageTabsProvider>;
  };
}
