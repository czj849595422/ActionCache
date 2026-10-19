"use strict";

function normalizeName(value, field = "name") {
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

function parseOpenTimeoutMs(value, fallback = 45000) {
    const parsed = Math.trunc(Number(value === undefined || value === null ? fallback : value));
    if (!Number.isFinite(parsed) || parsed < 1000) {
        throw new Error("open_timeout_ms must be an integer >= 1000");
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
        maxDurability: item.maxDurability ?? null,
        durabilityUsed: item.durabilityUsed ?? null,
    };
}

function itemsToPlain(items) {
    return Array.from(items || []).filter(Boolean).map(itemToPlain);
}

function windowInventoryItems(window) {
    if (!window || !Number.isInteger(window.inventoryStart) || !Number.isInteger(window.inventoryEnd)) {
        return [];
    }
    const items = [];
    for (let slot = window.inventoryStart; slot < window.inventoryEnd; slot++) {
        const item = window.slots?.[slot];
        if (item) items.push(item);
    }
    return items;
}

function windowInventoryEmptySlotCount(window) {
    if (!window || !Number.isInteger(window.inventoryStart) || !Number.isInteger(window.inventoryEnd)) {
        return null;
    }
    let empty = 0;
    for (let slot = window.inventoryStart; slot < window.inventoryEnd; slot++) {
        if (!window.slots?.[slot]) empty += 1;
    }
    return empty;
}

function blockToPlain(bot, block) {
    if (!block) return null;
    const center = block.position.offset(0.5, 0.5, 0.5);
    return {
        type: block.type ?? null,
        name: block.name ?? null,
        displayName: block.displayName ?? null,
        position: positionToPlain(block.position),
        distance: bot.entity && bot.entity.position ? bot.entity.position.distanceTo(center) : null,
    };
}

function inventorySnapshot(bot, window = null) {
    const inventoryItems = window ? windowInventoryItems(window) : bot.inventory.items();
    const emptySlots = window ? windowInventoryEmptySlotCount(window) : bot.inventory.emptySlotCount();
    return {
        inventory_items: itemsToPlain(inventoryItems),
        inventory_empty_slots: emptySlots ?? bot.inventory.emptySlotCount(),
        heldItem: itemToPlain(bot.heldItem),
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

function resolveItem(bot, name) {
    const normalized = normalizeName(name, "item");
    const item = bot.registry.itemsByName?.[normalized] || bot.registry.itemsByName?.[`minecraft:${normalized}`];
    if (item) return item;
    for (const [key, value] of Object.entries(bot.registry.itemsByName || {})) {
        if (normalizeName(key, "item") === normalized) return value;
    }
    return null;
}

function furnaceIds(bot) {
    const ids = [];
    for (const name of ["furnace", "blast_furnace", "smoker", "lit_furnace"]) {
        const block = bot.registry.blocksByName?.[name];
        if (block) ids.push(block.id);
    }
    return ids;
}

function isFurnaceWindow(window) {
    const type = String(window?.type || "");
    return ["minecraft:furnace", "minecraft:blast_furnace", "minecraft:smoker"].some((allowed) => (
        type.startsWith(allowed)
    ));
}

function positionFromParam(value) {
    if (!value || typeof value !== "object") return null;
    const x = Number(value.x);
    const y = Number(value.y);
    const z = Number(value.z);
    if (![x, y, z].every(Number.isFinite)) return null;
    return { x: Math.floor(x), y: Math.floor(y), z: Math.floor(z) };
}

function targetPosition(params) {
    return positionFromParam(params?.furnace?.position || params?.position);
}

function positionsEqual(a, b) {
    return a && b && a.x === b.x && a.y === b.y && a.z === b.z;
}

function findNearbyFurnaces(bot, radius, count) {
    const ids = furnaceIds(bot);
    if (ids.length === 0) return [];
    return bot.findBlocks({ matching: ids, maxDistance: radius, count })
        .map((position) => bot.blockAt(position))
        .filter(Boolean)
        .map((block) => blockToPlain(bot, block))
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
}

function findSpecifiedFurnace(bot, params, maxDistance) {
    const requested = targetPosition(params);
    if (!requested) return { block: null, requested, reason: "missing_position" };

    const ids = furnaceIds(bot);
    if (ids.length === 0) return { block: null, requested, reason: "furnace_block_unavailable" };

    const block = bot.blockAt(bot.entity.position.offset(
        requested.x - bot.entity.position.x,
        requested.y - bot.entity.position.y,
        requested.z - bot.entity.position.z,
    ));
    if (!block || !positionsEqual(positionToPlain(block.position), requested)) {
        return { block: null, requested, reason: "block_not_loaded" };
    }
    if (!ids.includes(block.type)) {
        return { block: null, requested, reason: "not_furnace", found_block: blockToPlain(bot, block) };
    }

    const distance = bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5));
    if (distance > maxDistance) {
        return { block: null, requested, reason: "out_of_range", found_block: blockToPlain(bot, block), distance };
    }
    return { block, requested, reason: null };
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

async function approachFurnace(bot, block, approachRange) {
    ensurePathfinder(bot);
    const center = block.position.offset(0.5, 0, 0.5);
    const distance = bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5));
    if (distance > Math.max(0.5, approachRange)) {
        const { goals } = bot.mcagentPathfinder;
        await bot.pathfinder.goto(new goals.GoalNear(center.x, center.y, center.z, approachRange));
        bot.clearControlStates();
    }
    await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true);
}

function currentSession(bot) {
    const session = bot.mcagentFurnaceSession;
    if (!session || !session.window) return null;
    if (bot.currentWindow !== session.window) return null;
    return session;
}

function clearSession(bot) {
    bot.mcagentFurnaceSession = null;
}

async function closeSession(bot) {
    const session = bot.mcagentFurnaceSession;
    const window = session?.window;
    const furnace = session?.furnace || null;
    if (!window) {
        clearSession(bot);
        return { alreadyClosed: true, furnace };
    }
    try {
        if (typeof window.close === "function") {
            window.close();
        } else {
            bot.closeWindow(window);
        }
    } catch (err) {
        clearSession(bot);
        throw err;
    }
    clearSession(bot);
    await sleep(250);
    return { alreadyClosed: false, furnace };
}

function decorateFurnaceWindow(bot, furnace) {
    if (furnace.mcagentFurnaceDecorated) return furnace;
    furnace.mcagentFurnaceDecorated = true;
    furnace.totalFuel = null;
    furnace.fuel = null;
    furnace.fuelSeconds = null;
    furnace.totalProgress = null;
    furnace.progress = null;
    furnace.progressSeconds = null;
    furnace.takeInput = () => takeWindowSlot(bot, furnace.inputItem());
    furnace.takeFuel = () => takeWindowSlot(bot, furnace.fuelItem());
    furnace.takeOutput = () => takeWindowSlot(bot, furnace.outputItem());
    furnace.putInput = (itemType, metadata, count) => putWindowSlot(bot, furnace, 0, itemType, metadata, count);
    furnace.putFuel = (itemType, metadata, count) => putWindowSlot(bot, furnace, 1, itemType, metadata, count);
    furnace.inputItem = function inputItem() { return this.slots[0]; };
    furnace.fuelItem = function fuelItem() { return this.slots[1]; };
    furnace.outputItem = function outputItem() { return this.slots[2]; };

    const onUpdateWindowProperty = (packet) => {
        if (packet.windowId !== furnace.id) return;
        if (packet.property === 0) {
            furnace.fuel = 0;
            furnace.fuelSeconds = 0;
            if (furnace.totalFuel) {
                furnace.fuel = packet.value / furnace.totalFuel;
                furnace.fuelSeconds = furnace.fuel * furnace.totalFuelSeconds;
            }
        } else if (packet.property === 1) {
            furnace.totalFuel = packet.value;
            furnace.totalFuelSeconds = ticksToSeconds(furnace.totalFuel);
        } else if (packet.property === 2) {
            furnace.progress = 0;
            furnace.progressSeconds = 0;
            if (furnace.totalProgress) {
                furnace.progress = packet.value / furnace.totalProgress;
                furnace.progressSeconds = furnace.totalProgressSeconds - (furnace.progress * furnace.totalProgressSeconds);
            }
        } else if (packet.property === 3) {
            furnace.totalProgress = packet.value;
            furnace.totalProgressSeconds = ticksToSeconds(furnace.totalProgress);
        }
        furnace.emit("update");
    };
    bot._client.on("craft_progress_bar", onUpdateWindowProperty);
    furnace.once("close", () => {
        bot._client.removeListener("craft_progress_bar", onUpdateWindowProperty);
    });
    return furnace;
}

function ticksToSeconds(ticks) {
    return ticks * 0.05;
}

async function takeWindowSlot(bot, item) {
    if (!item) throw new Error("slot is empty");
    await bot.putAway(item.slot);
    return item;
}

async function putWindowSlot(bot, window, destSlot, itemType, metadata, count) {
    await bot.transfer({
        window,
        itemType,
        metadata,
        count,
        sourceStart: window.inventoryStart,
        sourceEnd: window.inventoryEnd,
        destStart: destSlot,
        destEnd: destSlot + 1,
    });
}

function openFurnaceWindow(bot, block, timeoutMs) {
    return new Promise((resolve, reject) => {
        let settled = false;
        let timer = null;
        const cleanup = () => {
            if (timer) clearTimeout(timer);
            bot.removeListener("windowOpen", onWindowOpen);
        };
        const settle = (callback, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            callback(value);
        };
        const onWindowOpen = (window) => {
            if (!isFurnaceWindow(window)) {
                try {
                    bot.closeWindow(window);
                } catch (err) {
                    // Closing the wrong window is best effort only.
                }
                settle(reject, new Error(`opened window is not furnace-like: ${window?.type || "unknown"}`));
                return;
            }
            settle(resolve, decorateFurnaceWindow(bot, window));
        };
        timer = setTimeout(() => {
            settle(reject, new Error(`furnace open timed out after ${timeoutMs}ms`));
        }, timeoutMs);
        bot.once("windowOpen", onWindowOpen);
        try {
            bot.activateBlock(block);
        } catch (err) {
            settle(reject, err);
        }
    });
}

function furnaceState(window) {
    if (!window) {
        return {
            input: null,
            fuel: null,
            output: null,
            fuel_ratio: null,
            progress_ratio: null,
            fuel_seconds: null,
            progress_seconds: null,
        };
    }
    return {
        input: itemToPlain(typeof window.inputItem === "function" ? window.inputItem() : window.slots?.[0]),
        fuel: itemToPlain(typeof window.fuelItem === "function" ? window.fuelItem() : window.slots?.[1]),
        output: itemToPlain(typeof window.outputItem === "function" ? window.outputItem() : window.slots?.[2]),
        fuel_ratio: window.fuel ?? null,
        progress_ratio: window.progress ?? null,
        fuel_seconds: window.fuelSeconds ?? null,
        progress_seconds: window.progressSeconds ?? null,
    };
}

function sessionSnapshot(bot, session) {
    return {
        ok: true,
        session_active: Boolean(session?.window),
        furnace: session?.furnace || null,
        furnace_state: furnaceState(session?.window),
        ...inventorySnapshot(bot, session?.window || null),
    };
}

function countInventoryItem(bot, itemDef, window = null) {
    const items = window ? windowInventoryItems(window) : bot.inventory.items();
    return items
        .filter((item) => item && item.type === itemDef.id)
        .reduce((total, item) => total + (item.count || 0), 0);
}

function stackCapacityForItem(stacks, itemDef) {
    const stackSize = Math.max(1, itemDef.stackSize || 64);
    let capacity = 0;
    for (const stack of stacks) {
        if (!stack || stack.type !== itemDef.id) continue;
        if (stack.metadata !== undefined && stack.metadata !== null && stack.metadata !== 0) continue;
        capacity += Math.max(0, stackSize - (stack.count || 0));
    }
    return capacity;
}

function inventoryHasSpaceFor(bot, item, count, window = null) {
    const emptySlots = window ? (windowInventoryEmptySlotCount(window) ?? bot.inventory.emptySlotCount()) : bot.inventory.emptySlotCount();
    if (!item || count <= 0) return { ok: true, required_slots: 0, available_empty_slots: emptySlots };
    const itemDef = bot.registry.items?.[item.type] || { id: item.type, stackSize: item.stackSize || 64 };
    const stackCapacity = stackCapacityForItem(window ? windowInventoryItems(window) : bot.inventory.items(), itemDef);
    const remaining = Math.max(0, count - stackCapacity);
    const stackSize = Math.max(1, item.stackSize || itemDef.stackSize || 64);
    const requiredSlots = Math.ceil(remaining / stackSize);
    return {
        ok: requiredSlots <= emptySlots,
        required_slots: requiredSlots,
        available_empty_slots: emptySlots,
        stack_capacity: stackCapacity,
    };
}

async function ensureOpenSession(ctx, params, action) {
    const bot = ctx.bot;
    const activeParams = params || {};
    const requested = targetPosition(activeParams);
    if (!requested) {
        fail(action, "invalid_furnace_target", {
            ok: false,
            error_code: "invalid_furnace_target",
            reason: "missing_position",
            requested_furnace: activeParams.furnace || activeParams.position || null,
            ...inventorySnapshot(bot),
        });
    }
    const existing = currentSession(bot);
    if (existing) {
        if (
            existing.furnace
            && positionsEqual(existing.furnace.position, requested)
        ) {
            return existing;
        }
        await closeSession(bot);
    }
    // 未打开或当前会话指向其他熔炉时，自动打开/切换到指定熔炉。
    await openFurnace(ctx, activeParams, action);
    const session = currentSession(bot);
    if (!session) {
        fail(action, "furnace_not_open", {
            ok: false,
            error_code: "furnace_not_open",
            reason: "auto-open did not establish a furnace session",
            ...inventorySnapshot(bot),
        });
    }
    return session;
}

function parseItemActionParams(bot, params, action, session = null) {
    let itemName;
    let count;
    try {
        itemName = normalizeName(params?.item, "item");
    count = parsePositiveInteger(params?.count ?? 1, "count");
    } catch (err) {
        fail(action, "invalid_item_request", {
            ...inventorySnapshot(bot, session?.window || null),
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
            item: params?.item ?? null,
            count: params?.count ?? null,
        });
    }

    const itemDef = resolveItem(bot, itemName);
    if (!itemDef) {
        fail(action, "unknown_item", {
            ...inventorySnapshot(bot, session?.window || null),
            ok: false,
            error_code: "unknown_item",
            item: itemName,
            count,
        });
    }
    return { itemName, itemDef, count };
}

function slotFailure(action, errorCode, result) {
    fail(action, errorCode, {
        ...result,
        ok: false,
        error_code: errorCode,
    });
}

function assertSlotItem(bot, session, slotName, slotIndex, itemDef, itemName, count, action) {
    const item = session.window.slots[slotIndex] || null;
    const snapshot = sessionSnapshot(bot, session);
    if (!item) {
        slotFailure(action, "slot_empty", { slot: slotName, requested: { name: itemName, count }, ...snapshot });
    }
    if (item.type !== itemDef.id) {
        slotFailure(action, "slot_item_mismatch", {
            slot: slotName,
            requested: { name: itemName, count },
            actual: itemToPlain(item),
            ...snapshot,
        });
    }
    if ((item.count || 0) < count) {
        slotFailure(action, "slot_item_insufficient", {
            slot: slotName,
            requested: { name: itemName, count },
            actual: itemToPlain(item),
            missing: [{ name: itemName, required: count, available: item.count || 0, missing: count - (item.count || 0) }],
            ...snapshot,
        });
    }
    return item;
}

function assertInventoryItem(bot, session, itemDef, itemName, count, action) {
    const available = countInventoryItem(bot, itemDef, session.window);
    if (available < count) {
        fail(action, "inventory_item_missing", {
            ...sessionSnapshot(bot, session),
            ok: false,
            error_code: "inventory_item_missing",
            missing: [{ name: itemName, required: count, available, missing: count - available }],
        });
    }
}

function assertInventorySpace(bot, session, item, count, action, errorCode = "inventory_space_insufficient") {
    const space = inventoryHasSpaceFor(bot, item, count, session.window);
    if (!space.ok) {
        fail(action, errorCode, {
            ...sessionSnapshot(bot, session),
            space,
            item: itemToPlain(item),
            count,
            ok: false,
            error_code: errorCode,
        });
    }
}

async function transferSlotToInventory(bot, window, slotIndex, itemType, metadata, count) {
    await bot.transfer({
        window,
        itemType,
        metadata,
        count,
        sourceStart: slotIndex,
        sourceEnd: slotIndex + 1,
        destStart: window.inventoryStart,
        destEnd: window.inventoryEnd,
    });
}

async function recoverSlot(bot, session, slotName, slotIndex, action) {
    const item = session.window.slots[slotIndex] || null;
    if (!item) return [];
    const recoveredItem = itemToPlain(item);
    const recoveredCount = item.count || 0;
    assertInventorySpace(bot, session, item, recoveredCount, action);
    try {
        await transferSlotToInventory(bot, session.window, slotIndex, item.type, item.metadata ?? null, recoveredCount || 1);
    } catch (err) {
        fail(action, `${slotName}_recovery_failed`, {
            ...sessionSnapshot(bot, session),
            slot: slotName,
            item: recoveredItem,
            reason: err.message,
            ok: false,
            error_code: `${slotName}_recovery_failed`,
        });
    }
    return [{ ...recoveredItem, recovered: recoveredCount }];
}

async function findNearby(ctx, params, action) {
    const bot = ctx.bot;
    let radius;
    let count;
    try {
        radius = parseNonNegativeNumber(params?.radius, 20, "radius");
        count = parsePositiveInteger(params?.count ?? 16, "count");
    } catch (err) {
        fail(action, "invalid_find_request", {
            ok: false,
            error_code: "invalid_find_request",
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
    return {
        ok: true,
        radius,
        count,
        furnaces: findNearbyFurnaces(bot, radius, count),
        ...inventorySnapshot(bot),
    };
}

async function openFurnace(ctx, params, action) {
    const bot = ctx.bot;
    let maxDistance;
    let approachRange;
    let openTimeoutMs;
    try {
        maxDistance = parseNonNegativeNumber(params?.max_distance, 20, "max_distance");
        approachRange = parseNonNegativeNumber(params?.approach_range, 2.5, "approach_range");
        openTimeoutMs = parseOpenTimeoutMs(params?.open_timeout_ms, 45000);
    } catch (err) {
        fail(action, "invalid_furnace_target", {
            ok: false,
            error_code: "invalid_furnace_target",
            reason: err.message,
            requested_furnace: params?.furnace || params?.position || null,
            ...inventorySnapshot(bot),
        });
    }
    const found = findSpecifiedFurnace(bot, params || {}, maxDistance);
    if (!found.block) {
        const errorCode = found.reason === "out_of_range"
            ? "furnace_too_far"
            : (found.reason === "missing_position" || found.reason === "not_furnace" ? "invalid_furnace_target" : "furnace_not_found");
        fail(action, errorCode, {
            ok: false,
            error_code: errorCode,
            reason: found.reason,
            requested_furnace: params?.furnace || params?.position || null,
            max_distance: maxDistance,
            distance: found.distance ?? null,
            found_block: found.found_block ?? null,
            ...inventorySnapshot(bot),
        });
    }

    const existing = currentSession(bot);
    if (existing) {
        await closeSession(bot);
    } else if (bot.currentWindow) {
        bot.closeWindow(bot.currentWindow);
        await sleep(250);
    }

    let block = found.block;
    try {
        await approachFurnace(bot, block, approachRange);
        const refreshed = findSpecifiedFurnace(bot, { position: positionToPlain(block.position) }, maxDistance);
        block = refreshed.block || block;
        const window = await openFurnaceWindow(bot, block, openTimeoutMs);
        const session = {
            window,
            furnace: blockToPlain(bot, block),
            openedAt: Date.now(),
            openTimeoutMs,
        };
        bot.mcagentFurnaceSession = session;
        window.once("close", () => {
            if (bot.mcagentFurnaceSession?.window === window) {
                clearSession(bot);
            }
        });
        return sessionSnapshot(bot, session);
    } catch (err) {
        clearSession(bot);
        fail(action, "furnace_open_failed", {
            ok: false,
            error_code: "furnace_open_failed",
            furnace: blockToPlain(bot, block),
            max_distance: maxDistance,
            open_timeout_ms: openTimeoutMs,
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
}

async function putFuel(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    const { itemName, itemDef, count } = parseItemActionParams(bot, params, action, session);
    const recovered = await recoverSlot(bot, session, "fuel", 1, action);
    const available = countInventoryItem(bot, itemDef, session.window);
    if (available < count) {
        fail(action, "inventory_item_missing", {
            ...sessionSnapshot(bot, session),
            ok: false,
            error_code: "inventory_item_missing",
            missing: [{ name: itemName, required: count, available, missing: count - available }],
            recovered,
        });
    }
    try {
        await session.window.putFuel(itemDef.id, null, count);
        return {
            ...sessionSnapshot(bot, session),
            placed: [{ name: itemName, requested: count, placed: count }],
            recovered,
        };
    } catch (err) {
        fail(action, "put_fuel_failed", {
            ...sessionSnapshot(bot, session),
            requested: { name: itemName, count },
            recovered,
            reason: err.message,
            ok: false,
            error_code: "put_fuel_failed",
        });
    }
}

async function putInput(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    const { itemName, itemDef, count } = parseItemActionParams(bot, params, action, session);
    const recovered = await recoverSlot(bot, session, "input", 0, action);
    const available = countInventoryItem(bot, itemDef, session.window);
    if (available < count) {
        fail(action, "inventory_item_missing", {
            ...sessionSnapshot(bot, session),
            ok: false,
            error_code: "inventory_item_missing",
            missing: [{ name: itemName, required: count, available, missing: count - available }],
            recovered,
        });
    }
    try {
        await session.window.putInput(itemDef.id, null, count);
        return {
            ...sessionSnapshot(bot, session),
            placed: [{ name: itemName, requested: count, placed: count }],
            recovered,
        };
    } catch (err) {
        fail(action, "put_input_failed", {
            ...sessionSnapshot(bot, session),
            requested: { name: itemName, count },
            recovered,
            reason: err.message,
            ok: false,
            error_code: "put_input_failed",
        });
    }
}

async function takeWholeSlot(bot, session, slotName, slotIndex, action, errorCode) {
    const item = session.window.slots[slotIndex] || null;
    if (!item) {
        slotFailure(action, "slot_empty", {
            slot: slotName,
            ...sessionSnapshot(bot, session),
        });
    }
    const count = item.count || 0;
    assertInventorySpace(bot, session, item, count, action);
    try {
        await transferSlotToInventory(
            bot,
            session.window,
            slotIndex,
            item.type,
            item.metadata ?? null,
            count || 1,
        );
        return {
            ...sessionSnapshot(bot, session),
            taken: [{ name: itemToPlain(item).name, requested: count, taken: count }],
        };
    } catch (err) {
        fail(action, errorCode, {
            ...sessionSnapshot(bot, session),
            slot: slotName,
            item: itemToPlain(item),
            reason: err.message,
            ok: false,
            error_code: errorCode,
        });
    }
}

async function takeFuel(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    return takeWholeSlot(bot, session, "fuel", 1, action, "take_fuel_failed");
}

async function takeInput(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    return takeWholeSlot(bot, session, "input", 0, action, "take_input_failed");
}

async function takeOutput(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    const output = session.window.slots[2] || null;
    if (!output) {
        fail(action, "output_empty", {
            ...sessionSnapshot(bot, session),
            ok: false,
            error_code: "output_empty",
        });
    }
    assertInventorySpace(bot, session, output, output.count || 1, action);
    const takenItem = itemToPlain(output);
    try {
        await session.window.takeOutput();
        return {
            ...sessionSnapshot(bot, session),
            taken: [{ ...takenItem, taken: takenItem.count || 0 }],
        };
    } catch (err) {
        fail(action, "take_output_failed", {
            ...sessionSnapshot(bot, session),
            output: takenItem,
            reason: err.message,
            ok: false,
            error_code: "take_output_failed",
        });
    }
}

// ---------------------------------------------------------------------------
// smelt_item：一体化烧炼（对齐原版 smeltItem）
//  - wait=true  (默认)：放燃料/原料后轮询等待烧完，逐个取出产物；
//  - wait=false：只配置燃料与原料后立即关闭，由上层决定何时取产物。
// ---------------------------------------------------------------------------

async function waitForOutput(bot, session, action, itemName, timeoutMs = 25000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (session.window.outputItem && session.window.outputItem()) return;
        await sleep(200);
    }
    fail(action, "smelt_timeout", {
        ...sessionSnapshot(bot, session),
        ok: false,
        error_code: "smelt_timeout",
        item: itemName,
        reason: `smelting did not finish within ${timeoutMs}ms`,
    });
}

async function smeltItem(ctx, params, action) {
    const bot = ctx.bot;
    const session = await ensureOpenSession(ctx, params, action);
    const { itemName, itemDef, count } = parseItemActionParams(bot, params, action, session);
    const fuelName = normalizeName(params?.fuel ?? params?.fuel_name, "fuel");
    const fuelDef = resolveItem(bot, fuelName);
    if (!fuelDef) {
        fail(action, "unknown_fuel", {
            ...sessionSnapshot(bot, session),
            ok: false,
            error_code: "unknown_fuel",
            fuel: fuelName,
        });
    }
    const wait = params?.wait !== false;

    try {
        const placed = [];
        let successCount = 0;
        for (let i = 0; i < count; i++) {
            const availableItem = countInventoryItem(bot, itemDef, session.window);
            if (availableItem < 1) {
                fail(action, "inventory_item_missing", {
                    ...sessionSnapshot(bot, session),
                    ok: false,
                    error_code: "inventory_item_missing",
                    missing: [{ name: itemName, required: 1, available: 0, missing: 1 }],
                });
            }
            const fuelSeconds = session.window.fuelSeconds ?? 0;
            const currentFuel = typeof session.window.fuelItem === "function" ? session.window.fuelItem() : null;
            if (fuelSeconds < 15 && (!currentFuel || currentFuel.type !== fuelDef.id)) {
                const availableFuel = countInventoryItem(bot, fuelDef, session.window);
                if (availableFuel < 1) {
                    fail(action, "inventory_item_missing", {
                        ...sessionSnapshot(bot, session),
                        ok: false,
                        error_code: "inventory_item_missing",
                        missing: [{ name: fuelName, required: 1, available: 0, missing: 1 }],
                        stage: "fuel",
                    });
                }
                await session.window.putFuel(fuelDef.id, null, 1);
            }
            await session.window.putInput(itemDef.id, null, 1);
            placed.push({ name: itemName, placed: 1 });
            if (!wait) continue;
            await waitForOutput(bot, session, action, itemName);
            await session.window.takeOutput();
            successCount += 1;
        }

        const snapshot = sessionSnapshot(bot, session);
        return {
            ok: true,
            item: itemName,
            requested_count: count,
            smelted_count: successCount,
            waited: wait,
            configured_count: wait ? successCount : placed.length,
            placed,
            furnace_state: snapshot.furnace_state,
            ...inventorySnapshot(bot),
        };
    } finally {
        // 一体化动作结束（无论成功失败）都关闭熔炉会话，避免窗口泄漏。
        try {
            await closeSession(bot);
        } catch (_) {
            // 关闭失败不影响动作结果
        }
    }
}

async function closeFurnace(ctx, params, action) {
    const bot = ctx.bot;
    try {
        const result = await closeSession(bot);
        return {
            ok: true,
            closed: !result.alreadyClosed,
            already_closed: result.alreadyClosed,
            furnace: result.furnace,
            ...inventorySnapshot(bot),
        };
    } catch (err) {
        fail(action, "furnace_close_failed", {
            ok: false,
            error_code: "furnace_close_failed",
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
}

module.exports = {
    name: "furnace",
    commands: {
        find_nearby: { execute: findNearby },
        open: { execute: openFurnace },
        put_fuel: { execute: putFuel },
        take_fuel: { execute: takeFuel },
        put_input: { execute: putInput },
        take_input: { execute: takeInput },
        take_output: { execute: takeOutput },
        smelt_item: { execute: smeltItem },
        close: { execute: closeFurnace },
    },
};
