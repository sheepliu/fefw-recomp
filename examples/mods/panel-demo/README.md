# Sample code mod: a settings panel

A small DLL mod that puts its settings on the **Mods** tab of the **F1** menu. It changes nothing in the game. Use it to try the
tab, or as the start of your own mod. The interface is [`runtime/include/nxrt/mod_api.h`](../../../runtime/include/nxrt/mod_api.h).

It needs a port with mod API version 4. It shows:

- checking the API version before using `add_panel`
- every panel widget: plain, greyed and coloured text, a checkbox, both sliders, buttons on one line, a separator, a disabled
  block, a tooltip and a tree
- the two threads: the panel only changes the mod's own values, and `on_frame` writes the file
- saving settings, which each mod does for itself

## Build

Run one of these from the root of this repository. Each writes `panel_demo.dll`.

With the compiler that ships in a release package (nothing else to install; replace `<game>` with the extracted release folder).
On Windows:

```bat
<game>\tools\build\zig\zig.exe c++ -target x86_64-windows-gnu -shared -O2 -std=c++20 -s -I runtime\include -I recomp\include examples\mods\panel-demo\panel_demo.cpp -o panel_demo.dll
```

On Linux (the Linux package):

```sh
<game>/tools/build/zig/zig c++ -target x86_64-windows-gnu -shared -O2 -std=c++20 -s -I runtime/include -I recomp/include examples/mods/panel-demo/panel_demo.cpp -o panel_demo.dll
```

Or with Visual Studio, in an *x64 Native Tools Command Prompt*:

```bat
cl /LD /MT /O2 /EHsc /std:c++20 /I runtime\include /I recomp\include examples\mods\panel-demo\panel_demo.cpp /Fe:panel_demo.dll
```

## Install

Copy the DLL to a folder of its own under `mods\`, next to the launcher:

```text
mods\panel-demo\exefs\panel_demo.dll
```

Press **Play**. The mod adds no hook sites, so there is no need to press **Set up** again. In the game press **F1** and open the
**Mods** tab. **Save** writes `mods\panel-demo\settings.txt`, which is read at the next start.

If the tab is missing, look in `fefw.log` for lines tagged `mods`. To switch the mod off, rename its folder to `-panel-demo`.

A DLL mod runs with the same rights as the game: only install mods you trust.
