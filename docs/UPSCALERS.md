# Upscaling and frame generation

The built-in temporal upscaler (TAAU) works on any GPU and needs no extra files. DLSS, FSR 3 and XeSS need their vendor's files,
which the launcher downloads under **Settings > Graphics > Upscaler files**. Then choose the upscaler or frame generation under
**Settings > Graphics > Upscaling and frame generation**.

## DLSS (NVIDIA RTX only)

NVIDIA's files cannot be shipped here, so DLSS stays off until you add them. In **Settings > Graphics > Upscaler files**, under
**DLSS (Streamline) folder**, press **Download the NVIDIA files** (or **Download with frame generation**). The launcher shows NVIDIA's
licence terms, asks you to accept them, then fetches only the files it needs from NVIDIA's own Streamline 2.14.16 release (about 32 MB,
not the whole 276 MB package) and checks each one.

Then choose **DLSS** (or **Automatic**, which picks DLSS when the files and an RTX GPU are there) as the Upscaler in **Settings**, and
restart the game. `fefw.log` says `Streamline ... started` and `DLSS (Streamline)` when it works. The folder can be somewhere else:
set it under **Settings > Graphics > Upscaler files > DLSS (Streamline) folder**.

<details>
<summary>Adding the files by hand</summary>

Take them from NVIDIA's [Streamline SDK](https://github.com/NVIDIAGameWorks/Streamline) (v2.x, the `bin/x64` folder of the release, not
the development one) and put these four files in a folder named `upscalers\streamline` next to `fefw-launcher.exe`:

```
sl.interposer.dll   sl.common.dll   sl.dlss.dll   nvngx_dlss.dll
```

For DLSS frame generation (RTX 40 or newer, with Windows' hardware-accelerated GPU scheduling on) add five more files from the same
folder: `sl.dlss_g.dll`, `sl.reflex.dll`, `sl.pcl.dll`, `nvngx_dlssg.dll` and `NvLowLatencyVk.dll`.

</details>

<details>
<summary>Frame generation on an RTX 30 or older GPU</summary>

NVIDIA's own files refuse frame generation on these. Some community mods replace some of these files to run it on
older GPUs. We don't ship or support them. If you use one, put its files in the same `upscalers\streamline` folder, replacing the
originals as the mod says, and set `frame_gen_any_gpu = 1` under `[dlss]` in `fefw.ini` in the run folder (or the environment
variable `NXRT_DLSS_FRAME_GEN_ANY_GPU=1`). The game then no longer turns frame generation off just because Streamline says the GPU
is too old, and the mod's own answer decides. `fefw.log` shows the outcome on its `framegen` and `dlss` lines.

</details>

## FSR 3 (any GPU)

FSR 3 needs AMD's signed FidelityFX files. In **Settings > Graphics > Upscaler files**, under **FSR 3 (AMD) folder**, press **Download the AMD files**
(the launcher shows AMD's licence first).

<details>
<summary>Adding the files by hand</summary>

Download them from AMD's [FidelityFX SDK](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) repository. Put them in
`upscalers\fsr` next to `fefw-launcher.exe` (create the folder):

```
amd_fidelityfx_upscaler_vk.dll   amd_fidelityfx_loader_vk.dll   FSR 3 upscaling (Kits/FidelityFX/signedbin of tag v2.4.0)
amd_fidelityfx_vk.dll                                            FSR 3 frame generation (PrebuiltSignedDLL of tag v1.1.4)
```

</details>

Choose **AMD FSR 3 upscaling** as the Upscaler and/or **AMD FSR 3 frame generation** as Frame generation, then restart
the game. Frame generation adds one generated frame between two real ones; the HUD and text are taken from the real frames, so they
stay sharp. It works best with VSync off on a 120 Hz or faster display: with VSync on the game renders at most half the refresh
rate, and on a display slower than twice the game's frame rate generation pauses. **FSR 3 (AMD) folder** shows whether the files
are found, and can point to another folder. If a file is missing, `fefw.log` names it and the game carries on with TAAU (or without
frame generation). FSR 3 frame generation does not work through Proton; on Linux, see [LSFG-VK](../README.md#linux).

## XeSS (any GPU, best on Intel Arc)

XeSS needs Intel's `libxess.dll`. In **Settings > Graphics > Upscaler files**, under **XeSS (Intel) folder**, press **Download the Intel file** (the
launcher shows Intel's licence first). It also needs the
[Microsoft Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe). Then choose **Intel XeSS** as the
Upscaler in **Settings** and restart the game. If the file or the runtime is missing, `fefw.log` says so and the game uses TAAU.

<details>
<summary>Adding the file by hand</summary>

Download `libxess.dll` from `bin/` of the [XeSS SDK 3.0.2](https://github.com/intel/xess/releases/tag/v3.0.2) release and put it in
`upscalers\xess` next to `fefw-launcher.exe` (create the folder).

</details>
