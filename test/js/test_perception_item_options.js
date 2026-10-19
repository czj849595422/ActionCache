"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const { PERCEPTION_ITEM_OPTIONS } = require(path.resolve(
    __dirname,
    "..",
    "..",
    "src",
    "env",
    "mineflayer",
    "perception_item_options.js",
));

assert.equal(PERCEPTION_ITEM_OPTIONS.register, false);
assert.equal(Object.isFrozen(PERCEPTION_ITEM_OPTIONS), true);
assert.deepEqual(Object.keys(PERCEPTION_ITEM_OPTIONS), ["register"]);
console.log("perception item options tests passed");
