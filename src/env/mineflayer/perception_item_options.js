"use strict";

// Perception must not persist equipment metadata on the bridge event loop.
const PERCEPTION_ITEM_OPTIONS = Object.freeze({ register: false });

module.exports = { PERCEPTION_ITEM_OPTIONS };
