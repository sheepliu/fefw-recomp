// SPDX-License-Identifier: GPL-3.0-or-later
// The job's progress bar, shared by the Quick Access panel, the game page's tabs and the Play button. While the part that runs cannot
// say how far it is (linking fefw.exe says "0 of 1" until it ends), a light sweeps along the bar, so that it does not look stuck.

const SWEEP = "fefw-recomp-sweep";

export function ProgressTrack({
  pct,
  working,
  height = 6,
  fill = "#1a9fff",
  track = "rgba(255, 255, 255, 0.2)",
  rounded = true,
}: {
  pct: number;
  working: boolean;
  height?: number;
  fill?: string;
  track?: string;
  rounded?: boolean;
}) {
  return (
    <div
      style={{
        position: "relative",
        height: `${height}px`,
        borderRadius: rounded ? `${height / 2}px` : undefined,
        overflow: "hidden",
        background: track,
      }}
    >
      <div style={{ width: `${pct}%`, height: "100%", background: fill, transition: "width 0.5s" }} />
      {working && (
        <>
          <style>{`@keyframes ${SWEEP} { from { transform: translateX(-100%); } to { transform: translateX(250%); } }`}</style>
          <div
            style={{
              position: "absolute",
              top: 0,
              bottom: 0,
              left: 0,
              width: "40%",
              background: "linear-gradient(to right, transparent, rgba(255, 255, 255, 0.55), transparent)",
              animation: `${SWEEP} 1.6s ease-in-out infinite`,
            }}
          />
        </>
      )}
    </div>
  );
}

// What the readout beside the bar says: the percentage, or that the part that runs is at work without a count.
export const progressText = (pct: number | null, working: boolean) => (working ? "Working…" : pct !== null ? `${pct}%` : "");
