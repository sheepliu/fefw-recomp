// SPDX-License-Identifier: GPL-3.0-or-later
// The Quick Access Menu panel and Steam game-page tabs: the game's folder, its version, settings, update and rebuild.
// The work is in main.py; this polls it while open.
import {
  afterPatch,
  ButtonItem,
  createReactTreePatcher,
  DropdownItem,
  Field,
  findInReactTree,
  Navigation,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { addEventListener, definePlugin, removeEventListener, routerHook, toaster } from "@decky/api";
import { useEffect, useState, type ReactElement } from "react";
import { FaDownload } from "react-icons/fa";
import { pickSettingChoice } from "./game-tabs";
import { ProgressTrack, progressText } from "./progress-bar";
import { AppStore, SteamApps, startGame } from "./play";
import { askToUpdate } from "./self_update";
import { shownSettings } from "./settings";
import { SleepKeeper, SleepStatus, SteamSettings } from "./sleep";
import {
  bootstrapStore,
  busyText,
  cancelJob,
  chooseInstallFolder,
  clock,
  decky,
  getState,
  holdSleep,
  jobStepTitle,
  needsRebuild,
  NOT_ASKED,
  note,
  releaseSleep,
  isGameRunning,
  rereadState,
  requestPluginUpdate,
  retainPolling,
  runCheck,
  setStateSnapshot,
  SLEEP_TEXT,
  startJob,
  stopStore,
  toggleKeepAwake,
  useStore,
  type Result,
} from "./store";
import { createGamePageTabsWrapper } from "./tabs-patch";

interface GameSessions {
  RegisterForAppLifetimeNotifications?(cb: (n: { unAppID: number; bRunning: boolean }) => void): { unregister(): void };
}

declare const SteamClient: { Settings?: SteamSettings; Apps?: SteamApps; GameSessions?: GameSessions } | undefined;
declare const appStore: AppStore | undefined;

const Note = ({ text }: { text: string }) => (
  <PanelSectionRow>
    <div style={{ fontSize: "0.85em", overflowWrap: "anywhere" }}>{text}</div>
  </PanelSectionRow>
);

// Drawn here: Decky's progress bar puts its label beside the bar, which then runs off the narrow panel.
const Progress = ({ title, message, fraction, working }: { title: string; message: string; fraction: number | null; working: boolean }) => {
  const pct = fraction !== null ? Math.max(0, Math.min(100, Math.round(fraction * 100))) : null;
  return (
    <PanelSectionRow>
      <div style={{ display: "flex", flexDirection: "column", gap: "6px", width: "100%", minWidth: 0, overflowWrap: "anywhere" }}>
        <div style={{ fontWeight: "bold" }}>{title}</div>
        <div style={{ fontSize: "0.85em" }}>{message}</div>
        {(pct !== null || working) && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <div style={{ flex: 1 }}>
              <ProgressTrack pct={pct ?? 0} working={working} />
            </div>
            <div style={{ fontSize: "0.85em" }}>{progressText(pct, working)}</div>
          </div>
        )}
      </div>
    </PanelSectionRow>
  );
};

const Button = (props: { label: string; onClick: () => void; disabled?: boolean }) => (
  <PanelSectionRow>
    <ButtonItem layout="below" onClick={props.onClick} disabled={props.disabled}>
      {props.label}
    </ButtonItem>
  </PanelSectionRow>
);

function Content({ sleep }: { sleep: () => SleepStatus }) {
  const { state, checking, saving, pluginFailed, tabsPatchActive } = useStore();
  const [playFailed, setPlayFailed] = useState(false);

  useEffect(() => retainPolling("qam"), []);

  if (!state) return <PanelSection><Note text="Loading" /></PanelSection>;
  const { status, update, job, result, error, plugin_update: plugin } = state;

  const busy = busyText(status);
  const rebuildNeeded = needsRebuild(status);

  // only a game that is built for the installed release, with nothing at work on it
  const playable = status && status.setup_complete && !rebuildNeeded && !job && !status.busy;
  const play = () => {
    const started = startGame(
      typeof SteamClient === "undefined" ? undefined : SteamClient.Apps,
      typeof appStore === "undefined" ? undefined : appStore,
      status?.steam_shortcuts ?? [],
    );
    setPlayFailed(!started);
    if (started) Navigation.CloseSideMenus();
  };

  // what the panel was most likely opened for goes first: a job and how it ended, an update, or what stands in the way
  const attention = !!(job || result || error || busy || status?.problem || plugin || pluginFailed || update?.newer || rebuildNeeded);

  return (
    <>
      {attention && (
        <PanelSection title="Update">
          {job ? (
            <>
              <Progress
                title={jobStepTitle(job.step)}
                message={job.cancelling ? "Cancelling" : job.message}
                fraction={job.fraction}
                working={!job.cancelling && job.counting === false}
              />
              <Note text={`Time elapsed: ${clock(job.elapsed)}`} />
              <PanelSectionRow>
                <ToggleField
                  label="Prevent auto-sleep"
                  description={state.keep_awake ? SLEEP_TEXT[sleep()] : "Steam's auto-sleep settings apply: the device may sleep while this runs"}
                  checked={state.keep_awake}
                  onChange={(on: boolean) => void toggleKeepAwake(on)}
                />
              </PanelSectionRow>
              <Note
                text={`${job.step === "rebuild" ? "Some steps take several minutes without moving. " : ""}You can close this panel. Do not start the game until this has finished.`}
              />
              <Button label="Cancel" onClick={() => void cancelJob()} disabled={job.cancelling} />
            </>
          ) : (
            <>
              {result && <Note text={result.message} />}
              {error && <Note text={error} />}
              {status?.problem && <Note text={`${status.problem} Open the launcher in Desktop Mode.`} />}
              {busy && <Note text={busy} />}
              {plugin && (
                <>
                  <Note text="This plugin is older than the installed release. Decky asks you to confirm, then loads the new one." />
                  <Button label={`Update the plugin to ${plugin.version}`} onClick={() => void requestPluginUpdate(plugin)} />
                </>
              )}
              {pluginFailed && <Note text={NOT_ASKED} />}
              {update?.newer && (
                <Button label={`Update to ${update.latest?.version}`} onClick={() => void startJob("update")} disabled={!!busy || checking} />
              )}
              {rebuildNeeded && (
                <>
                  <Note text={`The game needs building${status?.rebuild_reason && status.rebuild ? `: ${status.rebuild_reason}` : "."}`} />
                  <Button label="Build the game" onClick={() => void startJob("rebuild")} disabled={!!busy || checking} />
                </>
              )}
            </>
          )}
        </PanelSection>
      )}
      <PanelSection title="Game">
        {playable && !!status.steam_shortcuts?.length && <Button label="Play" onClick={play} disabled={checking} />}
        {playable && status.steam_shortcuts?.length === 0 && (
          <Note text="To play from here, add the game to Steam: in Desktop Mode, open the launcher and press Add to Steam." />
        )}
        {playFailed && <Note text="Steam did not start the game: start it from your library." />}
        <PanelSectionRow>
          <Field label="Installed version" description={state.install_dir ?? "No folder chosen"}>
            {status?.version ?? ""}
          </Field>
        </PanelSectionRow>
        <Button label="Choose the game's folder" onClick={() => void chooseInstallFolder()} disabled={!!job} />
      </PanelSection>
      {!job && state.settings && (
        <PanelSection title="Settings">
          {shownSettings(state.settings).map((setting) => (
            <PanelSectionRow key={setting.id}>
              <DropdownItem
                label={setting.label}
                layout="below"
                strDefaultLabel="Custom"
                rgOptions={setting.options.map((o) => ({ data: o.choice, label: o.label }))}
                selectedOption={setting.current}
                disabled={saving || checking || status?.busy === "job"}
                onChange={(option) => {
                  const chosen = setting.options.find((o) => o.choice === option.data);
                  // picking the one in force again still offers its files when they are missing
                  if (chosen && (chosen.choice !== setting.current || chosen.download)) {
                    pickSettingChoice(state, setting.id, chosen.choice, chosen.download);
                  }
                }}
              />
            </PanelSectionRow>
          ))}
          <Note text="Changes apply the next time the game starts." />
        </PanelSection>
      )}
      <PanelSection>
        {!job && update && !update.newer && !rebuildNeeded && <Note text="You have the latest version." />}
        {!job && <Button label={checking ? "Checking" : "Check for updates"} onClick={() => void runCheck()} disabled={checking || !state.install_dir} />}
        {tabsPatchActive && !!status?.steam_shortcuts?.length && (
          <Note text="All settings and updates are also on the game's Steam page under Setup, Settings, and About." />
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // also while the panel is closed
  const done = addEventListener<[result: Result]>("fefw_job_done", (result) => {
    const title = result.state === "done" ? "FEFW Recomp: finished" : result.state === "cancelled" ? "FEFW Recomp: cancelled" : "FEFW Recomp: failed";
    toaster.toast({ title, body: result.message });
    // the game was updated: Decky asks whether the plugin may follow. On a no, the panel's button stays.
    if (result.plugin_update) {
      void askToUpdate(decky(), result.plugin_update).then((asked) => {
        if (!asked) toaster.toast({ title: "FEFW Recomp: plugin not updated", body: NOT_ASKED });
      });
    }
  });
  // also while the panel is closed: auto-sleep follows the job, and timers left off by an earlier run are put back
  const keeper = new SleepKeeper(
    typeof SteamClient === "undefined" ? undefined : SteamClient.Settings,
    { hold: holdSleep, release: releaseSleep },
    (text) => void note(text),
  );
  const follow = async () => {
    const state = await getState();
    setStateSnapshot(state);
    await keeper.tick(!!state.job && state.keep_awake, state.held_sleep);
  };
  const timer = setInterval(() => void follow(), 2000);
  void bootstrapStore();

  // Refresh status once when a game exits so the game page updates without polling during gameplay.
  const lifetimeSub =
    typeof SteamClient !== "undefined"
      ? SteamClient.GameSessions?.RegisterForAppLifetimeNotifications?.((n) => {
          // only this game's end matters, and the status saw it running; polling is held back while it runs
          if (!n.bRunning && isGameRunning()) void rereadState();
        })
      : undefined;

  // Patch the Steam game-detail page to replace the default tabs with Setup, Settings, and About on the fefw-recomp shortcut.
  const wrapGamePageTabs = createGamePageTabsWrapper(() => keeper.status);
  const patchHandler = createReactTreePatcher(
    [
      (t: unknown) =>
        findInReactTree(t, (x: { props?: { children?: { props?: { overview?: unknown } } } }) => !!x?.props?.children?.props?.overview)
          ?.props?.children,
    ],
    (_: Array<Record<string, unknown>>, ret?: ReactElement) => wrapGamePageTabs(ret),
  );
  let renderPatch: { unpatch(): void } | null = null;
  const appPagePatch = routerHook.addPatch(
    "/library/app/:appid",
    (tree: unknown) => {
      const routeProps = findInReactTree(tree, (x: { renderFunc?: unknown }) => !!x?.renderFunc);
      if (!routeProps) return tree;
      renderPatch?.unpatch();
      renderPatch = afterPatch(routeProps, "renderFunc", patchHandler);
      return tree;
    },
  );

  return {
    name: "FEFW Recomp",
    titleView: <div className={staticClasses.Title}>FEFW Recomp</div>,
    content: <Content sleep={() => keeper.status} />,
    icon: <FaDownload />,
    onDismount() {
      removeEventListener("fefw_job_done", done);
      clearInterval(timer);
      lifetimeSub?.unregister();
      stopStore();
      renderPatch?.unpatch();
      routerHook.removePatch("/library/app/:appid", appPagePatch);
      // put the timers back now; if this is cut short the record stays, and the next load does it
      void getState()
        .then((state) => keeper.tick(false, state.held_sleep))
        .finally(() => keeper.close());
    },
  };
});
