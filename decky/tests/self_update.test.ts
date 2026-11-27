import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { askToUpdate, PLUGIN_NAME, type DeckyRoutes } from "../src/self_update.ts";

const offer = { version: "1.2.0", artifact: "file:///data/plugin-abc.zip", hash: "abc" };

const decky = (calls: unknown[][], fail = false) =>
  ({
    callable: (route: string) => async (...args: unknown[]) => {
      if (fail) throw new Error("no");
      calls.push([route, ...args]);
    },
  }) as unknown as DeckyRoutes;

test("asks Decky to update this plugin from the checked ZIP", async () => {
  const calls: unknown[][] = [];
  assert.equal(await askToUpdate(decky(calls), offer), true);
  assert.deepEqual(calls, [["utilities/install_plugin", offer.artifact, "FEFW Recomp", "1.2.0", "abc", 2]]);
});

test("says when Decky cannot be asked", async () => {
  assert.equal(await askToUpdate(undefined, offer), false);
  assert.equal(await askToUpdate(decky([], true), offer), false);
});

test("names the plugin as plugin.json does", () => {
  const about = JSON.parse(readFileSync(new URL("../plugin.json", import.meta.url), "utf8"));
  assert.equal(PLUGIN_NAME, about.name);
});
