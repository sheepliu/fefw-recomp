# In-game menu translations

The F1 settings menu reads optional translation tables from `locales/<name>.ini`
next to `fefw_rt.dll` (or `fefw.exe`, wherever the binary lives).

## Enabling a language

By default (`[gfx] ui_language = auto` in `fefw.ini`, or no entry) the menu uses
the table for the Windows display language when there is one: `zh.ini` for
Simplified Chinese, and for any other language a table named after it
(`pt-BR.ini`, then `pt.ini`). Otherwise it is in English.

A language picked in the launcher (the button at the foot of its sidebar) is
written to `ui_language` and covers the menu as well. Setting the key by hand to
a table name without `.ini` also works; the launcher keeps a name it does not
know about. `en` forces English. The menu reads the setting when the game starts.

## Table format

UTF-8 text, one pair per line:

```
English source string = Translated string
```

- The key is the exact English string as it appears in the menu (trimmed).
- Lines starting with `#` or `;` are comments.
- A tab can be used as the separator instead of `=` for keys that themselves
  contain an equals sign.
- A missing key, a partial table, or a broken file is safe: the menu simply
  falls back to the English string.
- Format specifiers (`%s`, `%d`, `%.1f`, `%%`, ...) in a translation must match
  the English key in type and order. The menu hands some strings to printf, so
  an entry whose specifiers differ is dropped at load, stays English, and is
  listed in the log. `%%` is a literal `%` and needs no counterpart beyond the
  key's own `%%`.
- A `*` width or precision reads an argument of its own, so it is part of the
  comparison: `%*d` must stay `%*d` and may not become `%d`, and `%.*f` may not
  become `%.2f`. Both sides are compared as a list of arguments
  (`%*d` is `*` then `d`, `%ls` is `ls`), so moving an argument from the width
  to the value, or reordering two conversions, is caught as well.
- A space is a printf flag, so `99% de` reads as the conversion `% d`: such a
  translation is dropped. Write the percent as `%%` in a string that has format
  specifiers.
- `%n` is refused outright: it writes through a pointer the caller never
  passed, and no menu string uses one, so any entry that contains it is dropped
  whatever the English key says.
- A percent that is not a conversion is plain text and is left alone, which is
  why prose such as `1% low` or `100% (native)` needs no escaping.

A table committed to this repository has to translate the menu's strings, with no line the menu would drop: CI runs
`tools/check_menu_translations.py`, which reads the strings from the menu's sources. It does not see every string (one a
function returns for the menu to translate, for example).

## Where the tables live

`make-release.ps1` puts this folder into the release package. The launcher
copies any `<name>.ini` the run folder (`<game folder>/locales/`) does not have
yet, when it opens, on a language switch, on Set up and before every Play. A
table that is already there is left alone, so a translation the player edited or
added wins over the shipped one.

## Keeping the shipped files up to date

An update does not replace a file in `locales/`, so the launcher carries every
language file of its release inside itself: the `.ini` tables here and the
launcher's `.json` packs. When it opens, and before the game uses a table, it
replaces a copy the player never edited. It knows an untouched copy from the
hash it records beside it (`zh.ini.shipped`, `<name>.json.shipped`), or, for
`zh.ini` from releases before that record existed, from a built-in list.

An edited file is kept as it is; to take the new translations instead, delete it
and its `.shipped` record, then restore the file from the release package, or
merge the two by hand. A new file added to either folder of the repository is
covered from the release that first ships it, with nothing else to change.

Only the launcher does this: a game started without ever opening the new
launcher keeps its old tables.

## Two kinds of language packs

The project has two independent language systems; they do not share files:

| | In-game F1 menu | Launcher window |
| --- | --- | --- |
| File | `locales/<code>.ini` (next to `fefw_rt.dll`) | `locales/<code>.json` (next to `fefw-launcher.exe`) |
| Format | `English string = translation` lines | JSON object with `code`, `name`, `strings` |
| Selected by | `[gfx] ui_language` in `fefw.ini` (`auto`: the system language) | `lang` in `launcher.json` (empty: the system language) |
| Shipped here | `zh.ini` | `template.json` (an empty starting point) |

`zh.ini` and the launcher's JSON packs are separate features: this folder holds
the in-game table, and the launcher's packs live in its own install folder. A
translation of one does not translate the other. One
language choice in the launcher selects both.

## Font

Chinese/Japanese/Korean text needs a font that has those glyphs. The runtime
loads the first font it finds from:

1. `[gfx] ui_font` in `fefw.ini` (a path to a `.ttf`/`.ttc` font file),
2. `fonts/ui.ttf` next to the binary,
3. `C:\Windows\Fonts\msyh.ttc` (Microsoft YaHei),
4. `C:\Windows\Fonts\simhei.ttf`, then `Deng.ttf`.

Without one of these the menu renders boxes for CJK characters; English
tables need no font setup.

## Ship checklist for a new language

1. Copy `zh.ini` as a starting point or translate from the English keys.
2. Keep the file UTF-8 **without** BOM.
3. Name it after the language code (`de.ini`, `fr.ini`, ...).
4. Test with `[gfx] ui_language = <code>` and confirm the menu after a game
   restart.
