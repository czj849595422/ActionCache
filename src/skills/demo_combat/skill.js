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
    if (!entity || entity === entity.bot?.entity) return false;
    const name = String(entity.name || entity.displayName || "").toLowerCase();
    return entity.type === "hostile" || HOSTILE_NAMES.has(name);
}

function entityToPlain(entity, bot) {
    if (!entity) return null;
    return {
        id: entity.id,
        uuid: entity.uuid || null,
        name: entity.name || null,
        mobType: entity.displayName || null,
        type: entity.type || null,
        position: entity.position ? { x: entity.position.x, y: entity.position.y, z: entity.position.z } : null,
        distance: entity.position ? entity.position.distanceTo(bot.entity.position) : null,
        health: entity.health ?? null,
    };
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

function getEntityById(bot, id) {
    return bot.entities[id] || bot.entities[String(id)] || null;
}

async function start(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    const radius = numberParam(params, "radius", 32);
    const target = nearestHostile(bot, radius);
    if (!target) {
        throw new Error(`No hostile entity within ${radius}`);
    }

    action.runtimeState.targetId = target.id;
    action.runtimeState.targetInfo = entityToPlain(target, bot);
    action.runtimeState.lastAttackAt = 0;
    action.runtimeState.interval = setInterval(() => tickCombat(bot, action), 250);

    return {
        locked: true,
        target: action.runtimeState.targetInfo,
    };
}

function tickCombat(bot, action) {
    const target = getEntityById(bot, action.runtimeState.targetId);
    if (!target) {
        stopPathfinder(bot);
        clearInterval(action.runtimeState.interval);
        action.status = "completed";
        action.result = {
            completed: true,
            reason: "target_removed",
            target: action.runtimeState.targetInfo,
        };
        action.updatedAt = Date.now();
        return;
    }

    const followRange = numberParam(action.params, "followRange", 2.5);
    const attackRange = numberParam(action.params, "attackRange", 3.1);
    const cooldown = numberParam(action.params, "attackCooldownMs", 700);
    const distance = target.position.distanceTo(bot.entity.position);

    try {
        if (distance > attackRange) {
            const { goals } = bot.mcagentPathfinder;
            bot.pathfinder.setGoal(new goals.GoalFollow(target, followRange), true);
        } else {
            stopPathfinder(bot);
            bot.lookAt(target.position.offset(0, target.height || 1, 0), true).catch(() => {});
            const now = Date.now();
            if (now - action.runtimeState.lastAttackAt >= cooldown) {
                bot.attack(target);
                action.runtimeState.lastAttackAt = now;
            }
        }
        action.result = {
            locked: true,
            target: entityToPlain(target, bot),
            distance,
            attacking: distance <= attackRange,
        };
        action.updatedAt = Date.now();
    } catch (err) {
        action.status = "failed";
        action.error = err.message;
        clearInterval(action.runtimeState.interval);
        stopPathfinder(bot);
    }
}

async function update(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    return action.result || { updated: true };
}

async function stop(ctx, reason, action) {
    if (action.runtimeState.interval) {
        clearInterval(action.runtimeState.interval);
    }
    stopPathfinder(ctx.bot);
    return { stopped: true, reason, target: action.runtimeState.targetInfo || null };
}

function status(ctx, action) {
    const target = getEntityById(ctx.bot, action.runtimeState.targetId);
    return {
        status: action.status,
        lockedTargetId: action.runtimeState.targetId,
        target: entityToPlain(target, ctx.bot) || action.runtimeState.targetInfo || null,
    };
}

function stopPathfinder(bot) {
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
}

module.exports = {
    name: "demo_combat",
    commands: {
        lock_nearest_hostile: { start, update, stop, status },
    },
};
