// Fails when a launcher translation lacks a string (CI runs it).
//
//   npm run check-translations
//
// src/locales/en.js is the list of strings. Every other built-in language, and every pack committed to locales/, has to
// translate all of them, have no others and keep their {placeholders}. locales/template.json lists the same keys, left empty.
// (A pack a player adds may be partial: the launcher falls back to English.)
import { readdir, readFile } from 'node:fs/promises';
import { en } from '../src/locales/en.js';

const root = new URL('..', import.meta.url);
const problems = [];

const placeholders = (s) => (s.match(/\{\w+\}/g) ?? []).sort().join(' ');

function check(file, strings, { empty = false } = {}) {
  for (const key of Object.keys(en.strings)) {
    if (!(key in strings)) {
      problems.push(`${file}: missing ${key}`);
    } else if (empty) {
      if (strings[key] !== '') problems.push(`${file}: ${key} should be empty`);
    } else if (typeof strings[key] !== 'string' || !strings[key].trim()) {
      problems.push(`${file}: no translation for ${key}`);
    } else if (placeholders(strings[key]) !== placeholders(en.strings[key])) {
      problems.push(`${file}: ${key} has placeholders "${placeholders(strings[key])}", English has "${placeholders(en.strings[key])}"`);
    }
  }
  for (const key of Object.keys(strings)) {
    if (!(key in en.strings)) problems.push(`${file}: ${key} is not in en.js`);
  }
}

const list = async (dir, ext) => (await readdir(new URL(dir, root))).filter((f) => f.endsWith(ext)).sort();

for (const name of await list('src/locales/', '.js')) {
  if (name === 'en.js') continue;
  const code = name.slice(0, -'.js'.length);
  const pack = (await import(new URL(`src/locales/${name}`, root)))[code];
  if (pack?.strings) check(`src/locales/${name}`, pack.strings);
  else problems.push(`src/locales/${name}: no export "${code}" with strings`);
}

const packs = await list('locales/', '.json');
if (!packs.includes('template.json')) problems.push('locales/template.json: missing');
for (const name of packs) {
  const file = `locales/${name}`;
  let pack;
  try {
    pack = JSON.parse((await readFile(new URL(file, root), 'utf8')).replace(/^﻿/, ''));
  } catch (e) {
    problems.push(`${file}: ${e.message}`);
    continue;
  }
  const template = name === 'template.json';
  // the launcher skips a pack whose code is not its file name
  if (!template && `${pack.code}.json` !== name) problems.push(`${file}: "code" is "${pack.code}", which does not match the file name`);
  check(file, pack.strings ?? {}, { empty: template });
}

if (problems.length) {
  console.error(problems.join('\n'));
  console.error(`\n${problems.length} translation problem(s). Strings are listed in launcher/src/locales/en.js.`);
  process.exit(1);
}
console.log(`Launcher translations are complete (${Object.keys(en.strings).length} strings).`);
