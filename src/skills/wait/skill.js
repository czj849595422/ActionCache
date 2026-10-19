"use strict";

function secondsFromParams(params) {
    const seconds = Number(params.seconds ?? 0);
    if (!Number.isFinite(seconds) || seconds < 0) {
        throw new Error("seconds must be a non-negative number");
    }
    return seconds;
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function execute(ctx, params) {
    const seconds = secondsFromParams(params);
    await sleep(seconds * 1000);
    return { seconds, completed: true };
}

async function start(ctx, params, action) {
    const seconds = secondsFromParams(params);
    schedule(action, seconds);
    return { seconds, completed: false };
}

async function update(ctx, patch, action) {
    const seconds = secondsFromParams({ seconds: patch.seconds ?? action.params.seconds });
    clearTimer(action);
    schedule(action, seconds);
    return { seconds, completed: false, updated: true };
}

async function stop(ctx, reason, action) {
    clearTimer(action);
    return { stopped: true, reason };
}

function status(ctx, action) {
    return {
        seconds: action.params.seconds,
        completed: action.status === "completed",
        remainingMs: Math.max(0, (action.runtimeState.endsAt || Date.now()) - Date.now()),
    };
}

function schedule(action, seconds) {
    clearTimer(action);
    action.runtimeState.endsAt = Date.now() + seconds * 1000;
    action.runtimeState.timer = setTimeout(() => {
        action.status = "completed";
        action.result = { seconds, completed: true };
        action.updatedAt = Date.now();
    }, seconds * 1000);
}

function clearTimer(action) {
    if (action.runtimeState.timer) {
        clearTimeout(action.runtimeState.timer);
        action.runtimeState.timer = null;
    }
}

module.exports = {
    name: "wait",
    commands: {
        wait: { execute, start, update, stop, status },
    },
};
