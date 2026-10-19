"use strict";

const { DEFAULT_PERCEPTION_RADIUS } = require("../../env/mineflayer/perception_config");
const equipmentRegistry = require("../../env/mineflayer/equipment_registry");

function stringOrNull(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === "string") return value;
    try {
        return String(value);
    } catch (err) {
        return null;
    }
}

function normalizeName(name) {
    if (typeof name !== "string" || name.trim().length === 0) {
        throw new Error("name must be a non-empty string");
    }
    return name.trim().toLowerCase().replace(/^minecraft:/, "");
}

function normalizeSlot(value) {
    if (value === undefined || value === null || value === "") return null;
    const slot = Number(value);
    if (!Number.isInteger(slot) || slot < 0) {
        throw new Error("slot must be a non-negative integer");
    }
    return slot;
}

function itemToPlain(item) {
    if (!item) return null;
    return {
        type: item.type ?? null,
        name: item.name ?? null,
        displayName: stringOrNull(item.displayName),
        count: item.count ?? null,
        slot: item.slot ?? null,
        metadata: item.metadata ?? null,
        stackSize: item.stackSize ?? null,
        maxDurability: item.maxDurability ?? null,
        durabilityUsed: item.durabilityUsed ?? null,
        ...equipmentRegistry.cached(item),
    };
}

function inventoryItems(bot) {
    return bot.inventory.items().filter(Boolean);
}

function itemMatches(item, normalizedName) {
    if (!item) return false;
    const itemName = stringOrNull(item.name);
    const displayName = stringOrNull(item.displayName);
    return (
        (itemName && itemName.toLowerCase() === normalizedName)
        || (displayName && displayName.toLowerCase() === normalizedName)
        || (displayName && displayName.toLowerCase().replace(/\s+/g, "_") === normalizedName)
    );
}

function matchingItems(bot, name) {
    const normalizedName = normalizeName(name);
    const items = inventoryItems(bot);
    const matches = items.filter((item) => itemMatches(item, normalizedName));
    const resolved = equipmentRegistry.resolve(
        bot.username,
        name,
        items,
        (item, input) => itemMatches(item, normalizeName(input)),
    );
    if (resolved && matches.length === 0) return [resolved];
    return equipmentRegistry.enabled() && matches.some(equipmentRegistry.isEquipmentLike)
        ? equipmentRegistry.sortNameMatches(matches)
        : matches;
}

function itemAtSlot(bot, slot) {
    return inventoryItems(bot).find((item) => Number(item.slot) === slot) || null;
}

function countMatchingItems(bot, name) {
    return matchingItems(bot, name).reduce((total, item) => total + (item.count || 0), 0);
}

function equipmentSnapshot(bot) {
    const destinations = ["hand", "off-hand", "head", "torso", "legs", "feet"];
    const equipment = {};
    for (const destination of destinations) {
        try {
            const slot = bot.getEquipmentDestSlot(destination);
            const item = bot.inventory.slots[slot];
            equipmentRegistry.annotate(bot.username, item, "equipment");
            equipment[destination] = itemToPlain(item);
        } catch (err) {
            equipment[destination] = null;
        }
    }
    return equipment;
}

async function listInventory(ctx) {
    const bot = ctx.bot;
    const items = inventoryItems(bot).map((item) => {
        equipmentRegistry.annotate(bot.username, item, "inventory");
        return itemToPlain(item);
    });
    return {
        ok: true,
        items,
        inventory_items: items,
        inventory_empty_slots: bot.inventory.emptySlotCount(),
        itemCount: items.reduce((total, item) => total + (item.count || 0), 0),
        occupiedSlots: items.length,
        occupied_slots: items.length,
        heldItem: itemToPlain(bot.heldItem),
        equipment: equipmentSnapshot(bot),
    };
}

async function countItem(ctx, params) {
    const name = normalizeName(params.name);
    const matches = matchingItems(ctx.bot, name);
    return {
        name,
        count: matches.reduce((total, item) => total + (item.count || 0), 0),
        matchingItems: matches.map(itemToPlain),
    };
}

async function hasItem(ctx, params) {
    const name = normalizeName(params.name);
    const required = Number(params.count ?? 1);
    if (!Number.isFinite(required) || required <= 0) {
        throw new Error("count must be a positive number");
    }
    const count = countMatchingItems(ctx.bot, name);
    return {
        name,
        required,
        count,
        has: count >= required,
    };
}

async function equipItem(ctx, params) {
    const bot = ctx.bot;
    const slot = normalizeSlot(params.slot);
    if (slot !== null && params.name !== undefined && params.name !== null) {
        throw new Error("name and slot cannot be used together");
    }
    const name = slot === null ? normalizeName(params.name) : null;
    const destination = params.destination || "hand";
    const item = slot === null ? matchingItems(bot, name)[0] : itemAtSlot(bot, slot);
    if (!item) {
        throw new Error(slot === null ? `No ${name} in inventory` : `No item at inventory slot ${slot}`);
    }
    equipmentRegistry.annotate(bot.username, item, "inventory");
    await bot.equip(item, destination);
    return {
        equipped: true,
        destination,
        item: itemToPlain(item),
        heldItem: itemToPlain(bot.heldItem),
        equipment: equipmentSnapshot(bot),
    };
}

// ---------------------------------------------------------------------------
// 拾取辅助（路径、超时、掉落物实体识别）
// ---------------------------------------------------------------------------

function positionToPlain(position) {
    if (!position) return null;
    return { x: position.x, y: position.y, z: position.z };
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

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

const TARGET_OBSERVE_RANGE_ERROR = "目标未找到或位于观察范围之外";

function observeRadius(ctx) {
    const base = Number.isFinite(ctx.perceptionRadius) ? ctx.perceptionRadius : DEFAULT_PERCEPTION_RADIUS;
    return base * 2;
}

function positionDistance(bot, position) {
    return Math.hypot(
        position.x + 0.5 - bot.entity.position.x,
        position.y + 0.5 - bot.entity.position.y,
        position.z + 0.5 - bot.entity.position.z,
    );
}

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

function findObservedDroppedItemsNear(bot, position, radius, observeRange) {
    return findDroppedItemsNear(bot, position, radius).filter(
        (entity) => entity.position.distanceTo(bot.entity.position) <= observeRange
    );
}

function failTargetOutsideObserveRange(action, target, ctx, distance) {
    action.result = {
        ok: false,
        error_code: "target_not_found_or_outside_observe_range",
        reason: TARGET_OBSERVE_RANGE_ERROR,
        target,
        distance,
        observe_radius: observeRadius(ctx),
    };
    const err = new Error(TARGET_OBSERVE_RANGE_ERROR);
    err.code = "target_not_found_or_outside_observe_range";
    throw err;
}

// ---------------------------------------------------------------------------
// pick（continuous）：按位置列表拾取掉落物，可中断
// ---------------------------------------------------------------------------

function normalizePositions(positions) {
    if (!Array.isArray(positions) || positions.length === 0) {
        throw new Error("positions must be a non-empty array");
    }
    return positions.map((raw, index) => {
        if (!raw || typeof raw !== "object") {
            throw new Error(`positions[${index}] must be an object`);
        }
        const x = Number(raw.x);
        const y = Number(raw.y);
        const z = Number(raw.z);
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
            throw new Error(`positions[${index}] must have numeric x/y/z`);
        }
        return { x, y, z };
    });
}

function parsePickMode(mode) {
    const parsed = mode === undefined || mode === null ? 1 : Number(mode);
    if (!Number.isFinite(parsed) || (parsed !== 1 && parsed !== 2)) {
        throw new Error("mode must be 1 or 2");
    }
    return parsed;
}

function orderPickPositions(bot, positions, mode) {
    if (mode === 1) return positions.slice();
    const ordered = [];
    const remaining = positions.slice();
    let current = { x: bot.entity.position.x, y: bot.entity.position.y, z: bot.entity.position.z };
    while (remaining.length > 0) {
        let bestIndex = 0;
        let bestDistance = Infinity;
        for (let i = 0; i < remaining.length; i += 1) {
            const distance = Math.hypot(
                remaining[i].x - current.x,
                remaining[i].y - current.y,
                remaining[i].z - current.z,
            );
            if (distance < bestDistance) {
                bestDistance = distance;
                bestIndex = i;
            }
        }
        const next = remaining.splice(bestIndex, 1)[0];
        ordered.push(next);
        current = next;
    }
    return ordered;
}

function pickedSnapshot(runtimeState) {
    return Object.entries(runtimeState.picked)
        .filter(([, count]) => count > 0)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([name, count]) => ({ name, count }));
}

function pickedTotal(runtimeState) {
    return Object.values(runtimeState.picked).reduce((total, count) => total + count, 0);
}

function registerPickupListener(bot, runtimeState) {
    const handler = (collector, collected) => {
        if (!collector || collector !== bot.entity) return;
        if (!collected || typeof collected.getDroppedItem !== "function") return;
        const item = collected.getDroppedItem();
        if (!item) return;
        const name = item.name || item.displayName;
        if (!name) return;
        const count = item && typeof item.count === "number" ? item.count : 1;
        equipmentRegistry.markTransfer(null, item, bot.username, "inventory");
        runtimeState.picked[name] = (runtimeState.picked[name] || 0) + count;
    };
    runtimeState.pickupHandler = handler;
    bot.on("playerCollect", handler);
}

function finalizePick(ctx, action, reason, failed = false, error = null) {
    const bot = ctx.bot;
    action.status = failed ? "failed" : "completed";
    if (failed) action.error = error || "pick_failed";
    const runtime = action.runtimeState;
    action.result = {
        ok: !failed,
        status: action.status,
        mode: runtime.mode,
        total_positions: runtime.total,
        completed_positions: runtime.completed.length,
        pending_positions: runtime.pending.slice(),
        picked_items: pickedSnapshot(runtime),
        picked_total: pickedTotal(runtime),
        reason,
        ...(failed ? { error_code: action.error } : {}),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
    if (runtime.pickupHandler) {
        bot.removeListener("playerCollect", runtime.pickupHandler);
        runtime.pickupHandler = null;
    }
    if (typeof ctx.setAutoPickup === "function") {
        ctx.setAutoPickup(runtime.prevAutoPickup ?? false);
    }
}

async function runPickWorker(ctx, action) {
    const bot = ctx.bot;
    const runtime = action.runtimeState;
    while (!runtime.done && runtime.pending.length > 0) {
        const target = runtime.pending[0];
        try {
            ensurePathfinder(bot);
            const { goals } = bot.mcagentPathfinder;
            const goal = new goals.GoalNear(target.x, target.y, target.z, 1.0);
            await withTimeout(bot.pathfinder.goto(goal), 15000, "pick-walk");
            bot.clearControlStates();
            await waitForTicksOrSleep(bot, 10);
            const deadline = Date.now() + 10000;
            let nearby = findObservedDroppedItemsNear(bot, target, 5, runtime.observeRadius);
            while (nearby.length > 0 && Date.now() < deadline && !runtime.done) {
                const item = nearby[0];
                try {
                    ensurePathfinder(bot);
                    const { goals: itemGoals } = bot.mcagentPathfinder;
                    const itemGoal = new itemGoals.GoalNear(item.position.x, item.position.y, item.position.z, 0.5);
                    await withTimeout(bot.pathfinder.goto(itemGoal), 8000, "pick-item");
                    bot.clearControlStates();
                    await waitForTicksOrSleep(bot, 10);
                } catch (_) {
                    if (bot.pathfinder) bot.pathfinder.setGoal(null);
                }
                nearby = findObservedDroppedItemsNear(bot, target, 5, runtime.observeRadius);
            }
        } catch (_) {
            // 单个位置失败不致命，跳过继续
        }
        runtime.completed.push(target);
        runtime.pending.shift();
        runtime.result = {
            ok: true,
            status: "running",
            mode: runtime.mode,
            total_positions: runtime.total,
            completed_positions: runtime.completed.length,
            pending_positions: runtime.pending.slice(),
            picked_items: pickedSnapshot(runtime),
            picked_total: pickedTotal(runtime),
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        await sleep(100);
    }
    if (runtime.pending.length === 0 && !runtime.done) {
        finalizePick(ctx, action, "all_positions_cleared");
    }
}

async function startPick(ctx, params, action) {
    const bot = ctx.bot;
    let positions;
    let mode;
    try {
        positions = normalizePositions(params && params.positions);
        mode = parsePickMode(params && params.mode);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    const ordered = orderPickPositions(bot, positions, mode);
    const range = observeRadius(ctx);
    for (const target of ordered) {
        const distance = positionDistance(bot, target);
        const nearby = distance <= range
            ? findObservedDroppedItemsNear(bot, target, 5, range)
            : [];
        if (distance > range || nearby.length === 0) {
            failTargetOutsideObserveRange(action, target, ctx, distance);
        }
    }
    const prevAutoPickup = typeof ctx.getAutoPickup === "function" ? ctx.getAutoPickup() : false;
    if (typeof ctx.setAutoPickup === "function") {
        ctx.setAutoPickup(true);
    }
    const initialPending = ordered.slice();
    action.runtimeState = {
        mode,
        pending: ordered,
        completed: [],
        total: ordered.length,
        picked: {},
        done: false,
        prevAutoPickup,
        observeRadius: range,
        pickupHandler: null,
        worker: null,
        result: null,
    };
    registerPickupListener(bot, action.runtimeState);
    action.runtimeState.worker = runPickWorker(ctx, action);
    return {
        ok: true,
        status: "running",
        mode,
        total_positions: ordered.length,
        completed_positions: 0,
        pending_positions: initialPending,
        picked_items: [],
        picked_total: 0,
        position: positionToPlain(bot.entity.position),
    };
}

async function stopPick(ctx, reason, action) {
    const runtime = action.runtimeState;
    runtime.done = true;
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    if (runtime.pickupHandler) {
        ctx.bot.removeListener("playerCollect", runtime.pickupHandler);
        runtime.pickupHandler = null;
    }
    if (typeof ctx.setAutoPickup === "function") {
        ctx.setAutoPickup(runtime.prevAutoPickup ?? false);
    }
    return {
        ok: true,
        stopped: true,
        reason,
        status: "stopped",
        mode: runtime.mode,
        total_positions: runtime.total,
        completed_positions: runtime.completed.length,
        pending_positions: runtime.pending.slice(),
        picked_items: pickedSnapshot(runtime),
        picked_total: pickedTotal(runtime),
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function pickStatus(ctx, action) {
    const runtime = action.runtimeState;
    return {
        ok: true,
        status: action.status,
        mode: runtime.mode,
        total_positions: runtime.total,
        completed_positions: runtime.completed.length,
        pending_positions: runtime.pending.slice(),
        picked_items: pickedSnapshot(runtime),
        picked_total: pickedTotal(runtime),
        result: action.result || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

// ---------------------------------------------------------------------------
// drop（oneshot）：原地丢弃 items 列表；give（continuous）：靠近目标后丢弃
// ---------------------------------------------------------------------------

function normalizeItemRequests(items) {
    if (!Array.isArray(items) || items.length === 0) {
        throw new Error("items must be a non-empty array");
    }
    return items.map((raw, index) => {
        let name;
        let count;
        if (typeof raw === "string") {
            name = raw;
            count = 1;
        } else if (raw && typeof raw === "object") {
            name = raw.name;
            const rawCount = raw.count === undefined ? 1 : Number(raw.count);
            if (!Number.isInteger(rawCount) || rawCount < 1) {
                throw new Error(`items[${index}].count must be a positive integer or omitted`);
            }
            count = rawCount;
        } else {
            throw new Error(`items[${index}] must be a string or {name, count?}`);
        }
        if (typeof name !== "string" || !name.trim()) {
            throw new Error(`items[${index}].name is required`);
        }
        return { name: normalizeName(name), count };
    });
}

function resolveTransferRequests(bot, requests, action) {
    // 原子校验：任一物品完全不存在则整体失败；数量不足按可用最大数量。
    const resolved = [];
    for (const request of requests) {
        const matches = matchingItems(bot, request.name);
        if (matches.length === 0) {
            fail(action, "missing_item", {
                ok: false,
                error_code: `missing_item:${request.name}`,
                name: request.name,
                reason: `missing_item:${request.name}`,
            });
        }
        const available = matches.reduce(
            (total, item) => total + (item.count || 0),
            0
        );
        resolved.push({
            name: request.name,
            count: Math.min(request.count, available),
            sourceItems: matches,
        });
    }
    return resolved;
}

async function tossItems(bot, resolved, transfer = null) {
    const transferred = [];
    let total = 0;
    for (const entry of resolved) {
        let remaining = entry.count;
        for (const item of entry.sourceItems) {
            if (remaining <= 0) break;
            const take = Math.min(remaining, item.count);
            if (transfer && transfer.kind === "give") {
                equipmentRegistry.markTransfer(bot.username, item, transfer.target, "inventory");
            } else if (transfer && transfer.kind === "drop") {
                equipmentRegistry.markDropped(bot.username, item);
            }
            await bot.tossStack(item, take);
            transferred.push({
                name: entry.name,
                count: take,
                ...equipmentRegistry.cached(item),
            });
            total += take;
            remaining -= take;
        }
    }
    return { transferred, total };
}

async function dropItems(ctx, params, action) {
    const bot = ctx.bot;
    const requests = normalizeItemRequests(params && params.items);
    const resolved = resolveTransferRequests(bot, requests, action);
    const { transferred, total } = await tossItems(bot, resolved, { kind: "drop" });
    return {
        ok: true,
        status: "completed",
        position: positionToPlain(bot.entity.position),
        dropped_items: transferred,
        dropped_total: total,
    };
}

async function startGive(ctx, params, action) {
    const bot = ctx.bot;
    const targetName = params && params.target;
    if (typeof targetName !== "string" || !targetName.trim()) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "target is required",
        });
    }
    const requests = normalizeItemRequests(params && params.items);
    const resolved = resolveTransferRequests(bot, requests, action);
    const target = bot.players[targetName] && bot.players[targetName].entity;
    if (!target) {
        fail(action, "target_not_found", {
            ok: false,
            error_code: "target_not_found",
            target: targetName,
        });
    }
    const targetPosition = positionToPlain(target.position);
    action.runtimeState = {
        targetName,
        targetPosition,
        pending: resolved,
        transferred: [],
        done: false,
        worker: null,
    };
    action.runtimeState.worker = runGiveWorker(ctx, action);
    return {
        ok: true,
        status: "running",
        target: targetName,
        target_position: targetPosition,
        total_items: resolved.length,
        given_items: [],
        given_total: 0,
        position: positionToPlain(bot.entity.position),
    };
}

async function runGiveWorker(ctx, action) {
    const bot = ctx.bot;
    const runtime = action.runtimeState;
    try {
        ensurePathfinder(bot);
        const { goals } = bot.mcagentPathfinder;
        const goal = new goals.GoalNear(
            runtime.targetPosition.x,
            runtime.targetPosition.y,
            runtime.targetPosition.z,
            2.0
        );
        await withTimeout(bot.pathfinder.goto(goal), 20000, "give-walk");
        bot.clearControlStates();
        await waitForTicksOrSleep(bot, 10);
        const { transferred, total } = await tossItems(bot, runtime.pending, {
            kind: "give",
            target: runtime.targetName,
        });
        runtime.transferred = transferred;
        runtime.done = true;
        finalizeGive(ctx, action, "all_items_given");
    } catch (err) {
        if (runtime.done) return;
        runtime.done = true;
        finalizeGive(ctx, action, "give_failed", true, err.message || "give_failed");
    }
}

function finalizeGive(ctx, action, reason, failed = false, error = null) {
    action.status = failed ? "failed" : "completed";
    if (failed) action.error = error || "give_failed";
    const runtime = action.runtimeState;
    const givenTotal = runtime.transferred.reduce(
        (total, item) => total + (item.count || 0),
        0
    );
    action.result = {
        ok: !failed,
        status: action.status,
        mode: "continuous",
        target: runtime.targetName,
        target_position: runtime.targetPosition,
        position: positionToPlain(ctx.bot.entity.position),
        given_items: runtime.transferred,
        given_total: givenTotal,
        reason,
        ...(failed ? { error_code: action.error } : {}),
    };
    action.updatedAt = Date.now();
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
}

function giveStatus(ctx, action) {
    const runtime = action.runtimeState;
    const givenTotal = (runtime.transferred || []).reduce(
        (total, item) => total + (item.count || 0),
        0
    );
    return {
        ok: true,
        status: action.status,
        mode: "continuous",
        target: runtime.targetName,
        target_position: runtime.targetPosition,
        given_items: runtime.transferred || [],
        given_total: givenTotal,
        result: action.result || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function stopGive(ctx, reason, action) {
    const runtime = action.runtimeState;
    runtime.done = true;
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    const givenTotal = (runtime.transferred || []).reduce(
        (total, item) => total + (item.count || 0),
        0
    );
    action.status = "stopped";
    action.result = {
        ok: true,
        stopped: true,
        status: "stopped",
        reason,
        mode: "continuous",
        target: runtime.targetName,
        target_position: runtime.targetPosition,
        given_items: runtime.transferred || [],
        given_total: givenTotal,
        position: positionToPlain(ctx.bot.entity.position),
    };
    action.updatedAt = Date.now();
    return action.result;
}

module.exports = {
    name: "inventory",
    commands: {
        list: { execute: listInventory },
        inspect: { execute: listInventory },
        count_item: { execute: countItem },
        has_item: { execute: hasItem },
        equip: { execute: equipItem },
        pick: { start: startPick, stop: stopPick, status: pickStatus },
        drop: { execute: dropItems },
        give: { start: startGive, stop: stopGive, status: giveStatus },
    },
};
