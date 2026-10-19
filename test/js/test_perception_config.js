"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const config = require(path.join(
    __dirname,
    "..",
    "..",
    "src",
    "env",
    "mineflayer",
    "perception_config",
));

assert.equal(config.DEFAULT_PERCEPTION_RADIUS, 20);
assert.equal(config.parsePerceptionRadius("25"), 25);
assert.equal(config.parsePerceptionRadius("0"), 20);
assert.equal(config.parsePerceptionRadius("invalid"), 20);
assert.equal(config.configuredPerceptionRadius("25"), 25);
assert.equal(config.configuredPerceptionRadius("-1"), 20);
assert.equal(config.effectivePerceptionRadius(null, 25), 25);
assert.equal(config.effectivePerceptionRadius(30, 25), 30);
assert.equal(config.effectivePerceptionRadius(-1, null), 20);

console.log("perception config JS tests passed");
