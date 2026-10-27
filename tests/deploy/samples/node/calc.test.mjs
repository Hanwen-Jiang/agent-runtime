import { test } from "node:test";
import assert from "node:assert/strict";
import { add } from "./calc.mjs";

test("add", () => {
  assert.equal(add(2, 3), 5);
});
