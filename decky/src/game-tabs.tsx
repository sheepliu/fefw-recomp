// SPDX-License-Identifier: GPL-3.0-or-later
// The Setup, Settings, and About tabs rendered inside the Steam game page for the fefw-recomp shortcut,
// using Steam's native Game-Mode dialog/field styling from @decky/ui.
import {
  ConfirmModal,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  DropdownItem,
  Field,
  Focusable,
  showModal,
  ToggleField,
} from "@decky/ui";
import { useEffect, useRef, type ReactNode } from "react";
import { ProgressTrack, progressText } from "./progress-bar";
import { groupedSettings } from "./settings";
import type { SleepStatus } from "./sleep";
import {
  applySetting,
  busyText,
  cancelJob,
  chooseInstallFolder,
  clock,
  jobStepTitle,
  needsRebuild,
  NOT_ASKED,
  requestPluginUpdate,
  retainPolling,
  runCheck,
  SLEEP_TEXT,
  startDownload,
  startJob,
  toggleKeepAwake,
  useStore,
  type State,
} from "./store";

// Shared setting-pick helper (used by both the QAM panel and SettingsTab) so upscaler download consent stays in one place.
export function pickSettingChoice(state: State, setting: string, choice: string, needs: string | null) {
  if (needs) {
    // nothing of a vendor's is downloaded before the player has seen whose it is and on what terms, and accepted
    showModal(
      <ConfirmModal
        strTitle="Download the files?"
        strDescription={state.settings?.consent[needs]}
        strOKButtonText="I accept the licence, download"
        onOK={() => void startDownload(needs, setting, choice)}
      />,
    );
    return;
  }
  void applySetting(setting, choice);
}

// Ref-counted so switching between Setup / Settings / About keeps `overflow-x: clip` active across tab mounts
// and only restores the original inline style when leaving the game page completely.
const clippedTargets = new WeakMap<HTMLElement, { count: number; prev: string }>();

// Steam's game-page scroll container has `overflow-x: hidden; overflow-y: scroll`, while its direct child holds a
// blurred hero `<canvas>` wider than the viewport (`scrollWidth > clientWidth`). Chromium's focus `scrollIntoView`
// can therefore still shift `scrollLeft > 0` when focusing a right-aligned button after switching tabs.
// Clipping horizontal overflow on the game-page scroller's direct child makes `scrollWidth === clientWidth` so
// `scrollLeft` stays 0. We stop at the first `overflow-y: scroll` ancestor (the game page's own scroller) so outer
// Steam containers are never restyled.
function clipHorizontalOverflow(el: HTMLElement | null): () => void {
  const view = el?.ownerDocument?.defaultView;
  if (!el || !view) return () => {};
  let child: HTMLElement | null = el;
  let parent: HTMLElement | null = el.parentElement;
  while (parent) {
    const cs = view.getComputedStyle(parent);
    if (cs.overflowY === "scroll") {
      const target = child;
      const entry = clippedTargets.get(target);
      if (entry) {
        entry.count += 1;
      } else {
        clippedTargets.set(target, { count: 1, prev: target.style.overflowX });
        target.style.overflowX = "clip";
      }
      if (parent.scrollLeft !== 0) parent.scrollLeft = 0;
      return () => {
        const cur = clippedTargets.get(target);
        if (!cur) return;
        cur.count -= 1;
        if (cur.count <= 0) {
          target.style.overflowX = cur.prev;
          clippedTargets.delete(target);
        }
      };
    }
    child = parent;
    parent = parent.parentElement;
  }
  return () => {};
}

function TabContainer({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => retainPolling("page"), []);
  useEffect(() => clipHorizontalOverflow(ref.current), []);

  return (
    <div ref={ref} style={{ width: "100%" }}>
      <Focusable flow-children="column" style={{ padding: "16px 0 32px", width: "100%" }}>
        {children}
      </Focusable>
    </div>
  );
}

function JobProgress({ state, sleep }: { state: State; sleep: () => SleepStatus }) {
  const job = state.job;
  if (!job) return null;
  const pct = job.fraction !== null ? Math.max(0, Math.min(100, Math.round(job.fraction * 100))) : null;
  const working = !job.cancelling && job.counting === false;

  return (
    <DialogControlsSection>
      <DialogControlsSectionHeader>{jobStepTitle(job.step)}</DialogControlsSectionHeader>
      <Field
        label={job.cancelling ? "Cancelling" : job.message}
        description={`Time elapsed: ${clock(job.elapsed)}${job.step === "rebuild" ? " · Some steps take several minutes without moving." : ""}`}
        bottomSeparator="standard"
      >
        {progressText(pct, working)}
      </Field>
      {(pct !== null || working) && (
        <div style={{ padding: "8px 0 12px" }}>
          <ProgressTrack pct={pct ?? 0} working={working} />
        </div>
      )}
      <ToggleField
        label="Prevent auto-sleep"
        description={state.keep_awake ? SLEEP_TEXT[sleep()] : "Steam's auto-sleep settings apply: the device may sleep while this runs"}
        checked={state.keep_awake}
        onChange={(on: boolean) => void toggleKeepAwake(on)}
        bottomSeparator="standard"
      />
      <Field label="Do not start the game until this has finished" bottomSeparator="none">
        <DialogButton onClick={() => void cancelJob()} disabled={job.cancelling}>
          Cancel
        </DialogButton>
      </Field>
    </DialogControlsSection>
  );
}

export function SetupTab({ sleep }: { sleep: () => SleepStatus }) {
  const { state, checking, pluginFailed } = useStore();

  if (!state) {
    return (
      <TabContainer>
        <DialogControlsSection>
          <Field label="Loading..." bottomSeparator="none" />
        </DialogControlsSection>
      </TabContainer>
    );
  }

  const { status, update, job, result, error, plugin_update: plugin } = state;
  const busy = busyText(status);
  const rebuildNeeded = needsRebuild(status);
  const statusSummary = status?.problem
    ? `${status.problem} Open the launcher in Desktop Mode.`
    : rebuildNeeded
      ? `The game needs building${status?.rebuild_reason && status.rebuild ? `: ${status.rebuild_reason}` : "."}`
      : status?.setup_complete
        ? "Ready to play."
        : !state.install_dir
          ? "Choose the game's folder to check setup status."
          : error && !checking
            ? "Could not read setup status."
            : "Checking setup status…";

  return (
    <TabContainer>
      {job ? (
        <JobProgress state={state} sleep={sleep} />
      ) : (
        <DialogControlsSection>
          <DialogControlsSectionHeader>Setup</DialogControlsSectionHeader>
          <Field label="Status" description={statusSummary} bottomSeparator="standard">
            {rebuildNeeded && (
              <DialogButton onClick={() => void startJob("rebuild")} disabled={!!busy || checking}>
                Build the game
              </DialogButton>
            )}
          </Field>
          {busy && <Field label="Busy" description={busy} bottomSeparator="standard" />}
          {result && <Field label="Last action" description={result.message} bottomSeparator="standard" />}
          {error && <Field label="Error" description={error} bottomSeparator="standard" />}
          {update?.newer && (
            <Field
              label={`Update available: ${update.latest?.version ?? ""}`}
              description="Downloads and installs the newer release, then rebuilds the game if needed."
              bottomSeparator="standard"
            >
              <DialogButton onClick={() => void startJob("update")} disabled={!!busy || checking}>
                {`Update to ${update.latest?.version ?? ""}`}
              </DialogButton>
            </Field>
          )}
          {plugin && (
            <Field
              label={`Plugin update: ${plugin.version}`}
              description="This plugin is older than the installed release. Decky asks you to confirm, then loads the new one."
              bottomSeparator="standard"
            >
              <DialogButton onClick={() => void requestPluginUpdate(plugin)}>
                {`Update plugin to ${plugin.version}`}
              </DialogButton>
            </Field>
          )}
          {pluginFailed && <Field label="Plugin update" description={NOT_ASKED} bottomSeparator="standard" />}
        </DialogControlsSection>
      )}

      <DialogControlsSection>
        <DialogControlsSectionHeader>Installation</DialogControlsSectionHeader>
        <Field label="Installed version" description={state.install_dir ?? "No folder chosen"} bottomSeparator="standard">
          {status?.version ?? "—"}
        </Field>
        {status?.dump_dir && (
          <Field label="Game dump folder" description={status.dump_dir} bottomSeparator="standard" />
        )}
        <Field label="Game folder" description="The folder containing fefw-launcher.AppImage." bottomSeparator="none">
          <DialogButton onClick={() => void chooseInstallFolder()} disabled={!!job}>
            Choose the game's folder
          </DialogButton>
        </Field>
      </DialogControlsSection>
    </TabContainer>
  );
}

export function SettingsTab({ sleep }: { sleep: () => SleepStatus }) {
  const { state, checking, saving } = useStore();

  if (!state) {
    return (
      <TabContainer>
        <DialogControlsSection>
          <Field label="Loading..." bottomSeparator="none" />
        </DialogControlsSection>
      </TabContainer>
    );
  }

  const { status, job, error } = state;
  const groups = state.settings ? groupedSettings(state.settings) : [];

  const unavailableDescription = !state.install_dir
    ? "Choose the game's folder in Setup first."
    : checking
      ? "Loading settings…"
      : error && !status
        ? "Could not load settings."
        : "Run Setup first so fefw.ini exists, or update the launcher to a release that supports --settings.";

  return (
    <TabContainer>
      {job?.step === "download" && <JobProgress state={state} sleep={sleep} />}

      {error && (
        <DialogControlsSection>
          <Field label="Error" description={error} bottomSeparator="none" />
        </DialogControlsSection>
      )}

      {groups.length > 0 ? (
        <>
          {groups.map((group) => (
            <DialogControlsSection key={group.title}>
              <DialogControlsSectionHeader>{group.title}</DialogControlsSectionHeader>
              {group.settings.map((setting, idx) => (
                <DropdownItem
                  key={setting.id}
                  label={setting.label}
                  description={setting.help || undefined}
                  strDefaultLabel="Custom"
                  rgOptions={setting.options.map((o) => ({ data: o.choice, label: o.label }))}
                  selectedOption={setting.current}
                  disabled={saving || checking || !!job || status?.busy === "job"}
                  bottomSeparator={idx === group.settings.length - 1 ? "none" : "standard"}
                  onChange={(option) => {
                    const chosen = setting.options.find((o) => o.choice === option.data);
                    // picking the one in force again still offers its files when they are missing
                    if (chosen && (chosen.choice !== setting.current || chosen.download)) {
                      pickSettingChoice(state, setting.id, chosen.choice, chosen.download);
                    }
                  }}
                />
              ))}
            </DialogControlsSection>
          ))}
          <DialogControlsSection>
            <Field label="Changes apply the next time the game starts." bottomSeparator="none" />
          </DialogControlsSection>
        </>
      ) : (
        <DialogControlsSection>
          <DialogControlsSectionHeader>Settings</DialogControlsSectionHeader>
          <Field
            label={checking ? "Loading settings" : "Settings unavailable"}
            description={unavailableDescription}
            bottomSeparator="none"
          />
        </DialogControlsSection>
      )}
    </TabContainer>
  );
}

export function AboutTab({ sleep }: { sleep: () => SleepStatus }) {
  const { state, checking, pluginFailed } = useStore();

  if (!state) {
    return (
      <TabContainer>
        <DialogControlsSection>
          <Field label="Loading..." bottomSeparator="none" />
        </DialogControlsSection>
      </TabContainer>
    );
  }

  const { status, update, job, result, error, plugin_update: plugin } = state;
  const busy = busyText(status);

  return (
    <TabContainer>
      {job?.step === "update" ? (
        <JobProgress state={state} sleep={sleep} />
      ) : (
        <DialogControlsSection>
          <DialogControlsSectionHeader>Updates</DialogControlsSectionHeader>
          {result && <Field label="Last action" description={result.message} bottomSeparator="standard" />}
          {error && <Field label="Error" description={error} bottomSeparator="standard" />}
          {busy && <Field label="Busy" description={busy} bottomSeparator="standard" />}
          {update?.newer ? (
            <Field
              label={`Version ${update.latest?.version ?? ""} is available`}
              description={update.latest?.notes ? update.latest.notes.slice(0, 320) : "A newer release is available."}
              bottomSeparator="standard"
            >
              <DialogButton onClick={() => void startJob("update")} disabled={!!busy || !!job || checking}>
                {`Update to ${update.latest?.version ?? ""}`}
              </DialogButton>
            </Field>
          ) : (
            <Field
              label="Release status"
              description={update ? "You have the latest version." : "Check GitHub for a newer release."}
              bottomSeparator="standard"
            >
              <DialogButton onClick={() => void runCheck()} disabled={checking || !!job || !state.install_dir}>
                {checking ? "Checking..." : "Check for updates"}
              </DialogButton>
            </Field>
          )}
          {update?.newer && (
            <Field label="Refresh" bottomSeparator="standard">
              <DialogButton onClick={() => void runCheck()} disabled={checking || !!job || !state.install_dir}>
                {checking ? "Checking..." : "Check for updates"}
              </DialogButton>
            </Field>
          )}
          {plugin && (
            <Field
              label={`Plugin update: ${plugin.version}`}
              description="This plugin is older than the installed release. Decky asks you to confirm, then loads the new one."
              bottomSeparator="standard"
            >
              <DialogButton onClick={() => void requestPluginUpdate(plugin)}>
                {`Update plugin to ${plugin.version}`}
              </DialogButton>
            </Field>
          )}
          {pluginFailed && <Field label="Plugin update" description={NOT_ASKED} bottomSeparator="none" />}
        </DialogControlsSection>
      )}

      <DialogControlsSection>
        <DialogControlsSectionHeader>About</DialogControlsSectionHeader>
        <Field label="Installed version" bottomSeparator="standard">
          {status?.version ?? "—"}
        </Field>
        <Field label="Install folder" description={state.install_dir ?? "No folder chosen"} bottomSeparator="standard" />
        <Field label="Licence" bottomSeparator="standard">
          GPL-3.0-or-later
        </Field>
        <Field label="Repository" bottomSeparator="none">
          metrist38/fefw-recomp
        </Field>
      </DialogControlsSection>
    </TabContainer>
  );
}
