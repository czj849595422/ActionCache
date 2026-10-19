"use strict";

const DEFAULT_PERCEPTION_RADIUS = 20;

function isPositiveFiniteNumber(value) {
    return Number.isFinite(value) && value > 0;
}

function parsePerceptionRadius(value, fallback = DEFAULT_PERCEPTION_RADIUS) {
    const parsed = Number(value);
    return isPositiveFiniteNumber(parsed) ? parsed : fallback;
}

function configuredPerceptionRadius(envValue = process.env.MC_OBSERVATION_RANGE) {
    return parsePerceptionRadius(envValue);
}

function effectivePerceptionRadius(requestedRadius, currentRadius) {
    if (isPositiveFiniteNumber(requestedRadius)) return requestedRadius;
    return parsePerceptionRadius(currentRadius);
}

module.exports = {
    DEFAULT_PERCEPTION_RADIUS,
    configuredPerceptionRadius,
    effectivePerceptionRadius,
    parsePerceptionRadius,
};
