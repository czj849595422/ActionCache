"use strict";

const HOSTILE_NAMES = new Set([
    "blaze", "cave_spider", "creeper", "drowned", "elder_guardian", "enderman",
    "endermite", "evoker", "ghast", "guardian", "hoglin", "husk", "magma_cube",
    "phantom", "piglin_brute", "pillager", "ravager", "shulker", "silverfish",
    "skeleton", "slime", "spider", "stray", "vex", "vindicator", "warden",
    "witch", "wither_skeleton", "zoglin", "zombie", "zombie_villager"
]);

function numberParam(params, name, fallback) {
    const value = params[name] ?? fallback;
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`${name} must be a finite number`);
    return number;
}

function isHostile(entity) {
    const name = String(entity && (entity.name || entity.displayName) || "").toLowerCase();
    return Boolean(entity) && (entity.type === "hostile" || HOSTILE_NAMES.has(name));
}

function ensurePathfinder(bot) {
    if (!bot.pathfinder || !bot.mcagentPathfinder) {
        throw new Error("mineflayer-pathfinder is not loaded");
    }
    if (!bot.mcagentDefaultMovements) {
        const { Movements } = bot.mcagentPathfinder;
        bot.mcagentDefaultMovements = new Movements(bot);
    }
    bot.pathfinder.setMovements(bot.mcagentDefaultMovements);
}

function nearestHostile(bot, radius) {
    return bot.nearestEntity((entity) => {
        if (!isHostile(entity)) return false;
        return entity.position.distanceTo(bot.entity.position) <= radius;
    });
}

function positionToPlain(position) {
    return { x: position.x, y: position.y, z: position.z };
}

function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
}

async function start(ctx, params, action) {
    ensurePathfinder(ctx.bot);
    action.runtimeState.interval = setInterval(() => tickFlee(ctx.bot, action), 350);
    tickFlee(ctx.bot, action);
    return action.result || { active: true };
}

function tickFlee(bot, action) {
    const safeDistance = numberParam(action.params, "safeDistance", 14);
    const detectRadius = numberParam(action.params, "detectRadius", 32);
    const fleeDistance = numberParam(action.params, "fleeDistance", 10);
    const minBound = numberParam(action.params, "minBound", -100);
    const maxBound = numberParam(action.params, "maxBound", 100);
    const hostile = nearestHostile(bot, detectRadius);

    if (!hostile) {
        bot.pathfinder.setGoal(null);
        bot.clearControlStates();
        action.result = { safe: true, reason: "no_hostile", position: positionToPlain(bot.entity.position) };
        action.updatedAt = Date.now();
        return;
    }

    const distance = hostile.position.distanceTo(bot.entity.position);
    if (distance >= safeDistance) {
        bot.pathfinder.setGoal(null);
        bot.clearControlStates();
        action.result = {
            safe: true,
            reason: "distance_ok",
            distance,
            hostile: positionToPlain(hostile.position),
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }

    const dx = bot.entity.position.x - hostile.position.x;
    const dz = bot.entity.position.z - hostile.position.z;
    const length = Math.sqrt(dx * dx + dz * dz) || 1;
    const target = {
        x: clamp(bot.entity.position.x + (dx / length) * fleeDistance, minBound, maxBound),
        y: bot.entity.position.y,
        z: clamp(bot.entity.position.z + (dz / length) * fleeDistance, minBound, maxBound),
    };
    const { goals } = bot.mcagentPathfinder;
    bot.pathfinder.setGoal(new goals.GoalNear(target.x, target.y, target.z, 1.5), false);
    action.result = {
        safe: false,
        reason: "fleeing",
        distance,
        target,
        hostile: positionToPlain(hostile.position),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

async function update(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    tickFlee(ctx.bot, action);
    return action.result;
}

async function stop(ctx, reason, action) {
    if (action.runtimeState.interval) {
        clearInterval(action.runtimeState.interval);
    }
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    return { stopped: true, reason, position: positionToPlain(ctx.bot.entity.position) };
}

function status(ctx, action) {
    return action.result || { active: true };
}

module.exports = {
    name: "demo_survival",
    commands: {
        flee_nearest_hostile: { start, update, stop, status },
    },
};
