"use strict";

// Stable internal boundary for task-specific skills. The public container
// command surface remains defined by skill.json and skill.js.
module.exports = require("./skill").service;
