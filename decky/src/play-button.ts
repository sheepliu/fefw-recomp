// SPDX-License-Identifier: GPL-3.0-or-later
// Decides whether Steam's Play button on the fefw-recomp shortcut page stays as Play, switches to Set up, or shows
// a Cancel button + native-style progress readout beside it while a job runs. An install the launcher cannot build from here (no game
// files chosen, say) shows Set up too, which says what is needed instead of starting a shortcut to a fefw.exe that may not exist. This file imports nothing, so the tests run it as it is.

export interface PlayButtonInput {
  status: {
    setup_complete: boolean;
    rebuild: boolean | null;
    problem: string | null;
    busy: "game" | "job" | null;
  } | null;
  job: {
    step: "update" | "rebuild" | "download";
    message?: string;
    fraction: number | null;
    counting?: boolean; // the part that runs says how far it is; not told by a plugin backend from before it
    cancelling: boolean;
  } | null;
  checking: boolean;
}

export type PlayButtonState =
  | { kind: "play" }
  | { kind: "setup"; label: string; disabled: boolean }
  | { kind: "blocked"; label: string; problem: string }
  | {
      kind: "job";
      buttonLabel: string;
      cancelling: boolean;
      statusTitle: string;
      statusDetail: string;
      fraction: number | null;
      working: boolean; // at work on a part that cannot count: the bar shows it is moving
    };

// The game has to be built first: not set up yet, or built for another release. An install the launcher cannot read is not.
export const needsRebuild = (s: PlayButtonInput["status"] | undefined): boolean =>
  !!(s && !s.problem && (s.rebuild === true || !s.setup_complete));

export function resolvePlayButton(input: PlayButtonInput | null): PlayButtonState {
  if (!input) return { kind: "play" };
  if (input.job) {
    const { step, fraction, cancelling, message, counting } = input.job;
    const working = !cancelling && counting === false;
    const pct = fraction !== null ? Math.max(0, Math.min(100, Math.round(fraction * 100))) : null;
    const statusTitle = cancelling
      ? "Cancelling"
      : step === "update"
        ? "Updating"
        : step === "download"
          ? "Downloading"
          : "Building";
    const statusDetail = working ? message || "Working" : pct !== null ? `${pct}% Complete` : message || "In progress";
    return {
      kind: "job",
      buttonLabel: cancelling ? "Cancelling…" : "Cancel",
      cancelling,
      statusTitle,
      statusDetail,
      fraction,
      working,
    };
  }
  const s = input.status;
  if (s?.problem) {
    return { kind: "blocked", label: "Set up", problem: s.problem };
  }
  if (s && needsRebuild(s)) {
    return { kind: "setup", label: "Set up", disabled: !!s.busy || input.checking };
  }
  return { kind: "play" };
}

// Stable primitive key for useSyncExternalStore so the game page only re-renders when visible Play-bar state changes.
export function playButtonKey(state: PlayButtonState): string {
  if (state.kind === "play") return "play";
  if (state.kind === "setup") return `setup:${state.disabled ? "1" : "0"}:${state.label}`;
  if (state.kind === "blocked") return `blocked:${state.problem}`;
  return `job:${state.buttonLabel}:${state.statusTitle}:${state.statusDetail}:${state.fraction ?? ""}:${state.working ? "1" : "0"}`;
}
