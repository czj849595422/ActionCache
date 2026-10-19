"use strict";

const FARM_CROPS = new Set(["wheat", "carrots", "potatoes", "beetroots", "nether_wart"]);

function normalizeName(value, field = "crop") {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function parsePositiveInteger(value, field = "count") {
    const parsed = Math.trunc(Number(value));
    if (!Number.isFinite(parsed) || parsed < 1) {
        throw new Error(`${field} must be a positive integer`);
    }
    return parsed;
}

function parseNonNegativeNumber(value, fallback, field) {
    const parsed = value === undefined || value === null ? Number(fallback) : Number(value);
    if (!Number.isFinite(parsed) || parsed < 0) {
        throw new Error(`${field} must be a non-negative number`);
    }
    return parsed;
}

function positionToPlain(position) {
    if (!position) return null;
    return { x: position.x, y: position.y, z: position.z };
}

function itemToPlain(item) {
    if (!item) return null;
    return {
        type: item.type ?? null,
        name: item.name ?? null,
        displayName: item.displayName ?? null,
        metadata: item.metadata ?? null,
        count: item.count ?? null,
        slot: item.slot ?? null,
        stackSize: item.stackSize ?? null,
    };
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForTicksOrSleep(bot, ticks) {
    if (typeof bot.waitForTicks === "function") {
        await bot.waitForTicks(ticks);
        return;
    }
    await sleep(ticks * 50);
}

async function withTimeout(promise, timeoutMs, label) {
    if (!timeoutMs || timeoutMs <= 0) return promise;
    let timeoutId = null;
    try {
        return await Promise.race([
            promise,
            new Promise((_, reject) => {
                timeoutId = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
            }),
        ]);
    } finally {
        if (timeoutId) clearTimeout(timeoutId);
    }
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

// ---------------------------------------------------------------------------
// 成熟判定与扫描
// ---------------------------------------------------------------------------

function cropId(bot, name) {
    const def = bot.registry.blocksByName?.[name] || bot.registry.blocksByName?.[`minecraft:${name}`];
    return def ? def.id : null;
}

function isMatureCrop(block) {
    if (!block) return false;
    switch (block.name) {
        case "wheat":
        case "carrots":
        case "potatoes":
            return block.metadata === 7;
        case "beetroots":
        case "nether_wart":
            return block.metadata === 3;
        default:
            return false;
    }
}

function scanMatureCrops(bot, name, range) {
    const id = cropId(bot, name);
    if (id === null) return [];
    const positions = bot.findBlocks({ matching: id, maxDistance: range, count: 256 });
    return positions
        .map((pos) => bot.blockAt(pos))
        .filter((block) => block && isMatureCrop(block))
        .sort((a, b) => {
            const da = bot.entity.position.distanceTo(a.position.offset(0.5, 0.5, 0.5));
            const db = bot.entity.position.distanceTo(b.position.offset(0.5, 0.5, 0.5));
            return da - db;
        });
}

function cropKey(position) {
    return `${Math.floor(position.x)},${Math.floor(position.y)},${Math.floor(position.z)}`;
}

// ---------------------------------------------------------------------------
// 掉落收集（手动拾取）
// ---------------------------------------------------------------------------

function isItemEntity(entity) {
    if (!entity || !entity.position) return false;
    if (entity.name === "item") return true;
    if (entity.type === "object" && typeof entity.getDroppedItem === "function") {
        try {
            return entity.getDroppedItem() !== null;
        } catch (_) {
            return false;
        }
    }
    return false;
}

function findDroppedItemsNear(bot, position, radius) {
    const center = bot.entity.position.offset(
        position.x + 0.5 - bot.entity.position.x,
        position.y + 0.5 - bot.entity.position.y,
        position.z + 0.5 - bot.entity.position.z,
    );
    return Object.values(bot.entities || {}).filter(
        (entity) => isItemEntity(entity) && entity.position.distanceTo(center) <= radius
    );
}

async function collectDroppedItemsNear(bot, position, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    let items = findDroppedItemsNear(bot, position, 5);
    let collected = 0;
    while (items.length > 0 && Date.now() < deadline) {
        const item = items[0];
        try {
            ensurePathfinder(bot);
            const { goals } = bot.mcagentPathfinder;
            const goal = new goals.GoalNear(item.position.x, item.position.y, item.position.z, 0.5);
            await withTimeout(bot.pathfinder.goto(goal), 8000, "farm-collect");
            bot.clearControlStates();
            await waitForTicksOrSleep(bot, 10);
        } catch (_) {
            bot.pathfinder.setGoal(null);
        }
        const before = items.length;
        items = findDroppedItemsNear(bot, position, 5);
        collected += Math.max(0, before - items.length);
    }
    return collected;
}

async function harvestBlock(bot, block) {
    ensurePathfinder(bot);
    const below = block.position.offset(0, -1, 0);
    const { goals } = bot.mcagentPathfinder;
    const goal = new goals.GoalLookAtBlock(below, bot.world);
    await withTimeout(bot.pathfinder.goto(goal), 15000, "farm-approach");
    bot.clearControlStates();
    await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true);
    await withTimeout(bot.dig(block, "raycast"), 20000, "farm-dig");
    await waitForTicksOrSleep(bot, 4);
    await collectDroppedItemsNear(bot, block.position, 10000);
}

// ---------------------------------------------------------------------------
// harvest（continuous）：持续收获指定数量的成熟作物，可中断
// ---------------------------------------------------------------------------

function finalizeHarvest(bot, action, reason, failed = false, error = null) {
    action.status = failed ? "failed" : "completed";
    if (failed) action.error = error || "harvest_failed";
    action.result = {
        ok: !failed,
        harvesting: false,
        crop: action.runtimeState.crop,
        harvested: action.runtimeState.harvested,
        reason,
        ...(failed ? { error_code: action.error } : {}),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
}

async function runHarvestWorker(bot, action) {
    const startedAt = Date.now();
    while (!action.runtimeState.done) {
        const maxDuration = Number(action.params?.max_duration_ms ?? 600000);
        if (Date.now() - startedAt >= maxDuration) {
            finalizeHarvest(bot, action, "max_duration_exceeded");
            return;
        }
        if (
            action.runtimeState.targetCount !== null
            && action.runtimeState.harvested >= action.runtimeState.targetCount
        ) {
            finalizeHarvest(bot, action, "count_reached");
            return;
        }
        const crops = scanMatureCrops(bot, action.runtimeState.crop, action.runtimeState.range);
        const pending = crops.filter((block) => !action.runtimeState.doneKeys.has(cropKey(block.position)));
        if (pending.length === 0) {
            finalizeHarvest(bot, action, "all_harvested");
            return;
        }
        const target = pending[0];
        try {
            await harvestBlock(bot, target);
            action.runtimeState.doneKeys.add(cropKey(target.position));
            action.runtimeState.harvested += 1;
            action.runtimeState.failures = 0;
        } catch (err) {
            action.runtimeState.doneKeys.add(cropKey(target.position));
            action.runtimeState.failures = (action.runtimeState.failures || 0) + 1;
            if (action.runtimeState.failures >= 10) {
                finalizeHarvest(bot, action, "harvest_failed", true, err.message);
                return;
            }
        }
        action.result = {
            ok: true,
            harvesting: true,
            crop: action.runtimeState.crop,
            harvested: action.runtimeState.harvested,
            remaining: Math.max(0, pending.length - 1),
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        await sleep(150);
    }
}

async function startHarvest(ctx, params, action) {
    const bot = ctx.bot;
    let crop;
    try {
        crop = normalizeName(params?.crop, "crop");
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    if (!FARM_CROPS.has(crop)) {
        fail(action, "unknown_crop", {
            ok: false,
            error_code: "unknown_crop",
            crop,
            supported: Array.from(FARM_CROPS),
        });
    }
    let targetCount = null;
    if (params?.count !== undefined && params?.count !== null) {
        try {
            targetCount = parsePositiveInteger(params.count, "count");
        } catch (err) {
            fail(action, "invalid_params", {
                ok: false,
                error_code: "invalid_params",
                reason: err.message,
                params: params || {},
            });
        }
    }
    const range = parseNonNegativeNumber(params?.range, 8, "range");
    if (range < 1) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "range must be at least 1",
            params: params || {},
        });
    }

    const scan = scanMatureCrops(bot, crop, range);
    if (scan.length === 0) {
        fail(action, "no_mature_crop", {
            ok: false,
            error_code: "no_mature_crop",
            crop,
            range,
            reason: `no mature ${crop} within ${range} blocks`,
        });
    }

    action.runtimeState.crop = crop;
    action.runtimeState.range = range;
    action.runtimeState.targetCount = targetCount;
    action.runtimeState.harvested = 0;
    action.runtimeState.doneKeys = new Set();
    action.runtimeState.failures = 0;
    action.runtimeState.done = false;
    action.runtimeState.worker = runHarvestWorker(bot, action);
    return {
        ok: true,
        harvesting: true,
        crop,
        range,
        count: targetCount,
        harvested: 0,
        found: scan.length,
    };
}

async function updateHarvest(ctx, patch, action) {
    const merged = { ...action.params, ...patch };
    try {
        if (patch?.count !== undefined && patch?.count !== null) {
            const parsed = parsePositiveInteger(patch.count, "count");
            merged.count = parsed;
            action.runtimeState.targetCount = parsed;
        }
        if (patch?.range !== undefined && patch?.range !== null) {
            const parsed = parseNonNegativeNumber(patch.range, 8, "range");
            if (parsed < 1) throw new Error("range must be at least 1");
            merged.range = parsed;
            action.runtimeState.range = parsed;
        }
    } catch (err) {
        return { ok: false, error_code: "invalid_params", reason: err.message, updated: false };
    }
    action.params = merged;
    return action.result || { ok: true, updated: true, harvesting: true };
}

async function stopHarvest(ctx, reason, action) {
    action.runtimeState.done = true;
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    return {
        ok: true,
        stopped: true,
        reason,
        crop: action.runtimeState.crop || null,
        harvested: action.runtimeState.harvested || 0,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function harvestStatus(ctx, action) {
    return {
        ok: true,
        status: action.status,
        crop: action.runtimeState.crop || null,
        harvested: action.runtimeState.harvested || 0,
        target_count: action.runtimeState.targetCount,
        result: action.result || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

module.exports = {
    name: "farm",
    commands: {
        harvest: {
            start: startHarvest,
            update: updateHarvest,
            stop: stopHarvest,
            status: harvestStatus,
        },
    },
};
