# Controls

## Keyboard and controller

Any controller works, and so does the keyboard, with no setup. The button prompts in the game follow the one you used last: press a
key and they show keys, touch the controller and they show its buttons. The keyboard drives the Switch buttons the game knows:

| Keys | Switch | |
|---|---|---|
| W A S D | Left stick | Move |
| Arrow keys, I J K L | Right stick | Camera |
| Enter, Space | A | Confirm |
| Esc, Backspace | B | Back |
| R | X | |
| F | Y | |
| Q / E | L / R | E: dash in town |
| Z / C | ZL / ZR | C: map in town |
| Tab | + | Menu |
| M | - | |
| 1 2 3 4, or numpad 8 2 4 6 | D-pad up, down, left, right | |
| V / B | Left / right stick click | |

The movement keys push the stick all the way. **F1** (settings), **F2** (frame rate counter) and **Alt+Enter** (full screen) are
the runtime's own keys. **F1 > Controls** lists the keys as they are now. To leave the game cleanly, use **Quit game** at the
bottom of the F1 menu (click it twice) or close the window (**Alt+F4**): both save the settings and end the game normally.

To change a key, close the game and edit `[bindings.all]` in `controls.ini` in the run folder (the game creates it on its first
start). Each line is `key = action`:

```
[bindings.all]
W = move_forward
Return = btn_a
G = btn_x
X = none
```

Left side: a key name (`Return`, `Space`, `Left Shift`, `Up`, `Keypad 8` ...). Right side: `move_*`, `look_*`, `dpad_*`, `btn_a`,
`btn_b`, `btn_x`, `btn_y`, `btn_l`, `btn_r`, `btn_zl`, `btn_zr`, `btn_plus`, `btn_minus`, `btn_lstick`, `btn_rstick`, or `none` to
switch a key off. An older `controls.ini` gets the keys above added at the next start (only keys you have not bound yourself; the
old file is kept as `controls.ini.bak`). To go back to the default keys, delete the `[bindings.*]` sections. Controller options (stick dead zones,
button mapping, prompts) are under **Settings** and **F1 > Controls**.

## Mouse

The game has its own mouse controls, made for the Switch 2's Joy-Con 2 used as a mouse, and the PC mouse drives them. Move the mouse
or click and the game switches to its mouse controls (its prompts show mouse buttons); press a key or touch the controller and it
switches straight back. Whichever you used last is in control.

- **Bases, dungeons:** the mouse turns the camera, left click talks, opens and attacks, right click cancels or opens the menu, the
  wheel zooms the camera while standing still. Walk with WASD or the stick.
- **Battle maps:** the mouse moves the map cursor (faster when you move it faster), left click selects and confirms, right click
  cancels.
- **Menus:** moving the mouse up or down moves the selection, left click confirms, right click goes back.

While the game is in its mouse controls the Windows pointer is hidden and kept in the window (it comes back where the game's pointer
was). A key, the controller, **Alt+Tab** or **F1** frees it. The game's own **System > Mouse Controls** page shows every button, and
its **System > Settings** set the speeds: the rows marked (Mouse) on the General, Battle and Bases/Dungeons tabs (camera
sensitivity, map cursor speed, menu cursor speed), and **Method for Switching to Mouse**, which should stay automatic. Leave the
game's angle calibration alone (a desk mouse needs none; **Reset Calibration** undoes it). **F1 > Controls > Mouse** has the PC side:
mouse controls automatic, always or off, and the mouse speed before the game's own (`[mouse] jc2_speed` in `controls.ini`).

On battle maps, hold **Y** (keyboard **F**) to move the cursor faster with the stick or keys.
