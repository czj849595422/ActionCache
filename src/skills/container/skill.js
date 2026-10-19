"use strict";

const fs = require("fs");
const path = require("path");

// 共享注册表文件：所有 bridge 进程（不同 agent）读写同一份文件即可共享箱子编号/名称。
const CHEST_REGISTRY_FILE = process.env.CHEST_REGISTRY_FILE
    || path.resolve(__dirname, "..", "..", "..", "runtime", "chest_registry.json");

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

function parseOpenTimeoutMs(value) {
    const parsed = value === undefined || value === null ? 5000 : Math.trunc(Number(value));
    if (!Number.isFinite(parsed) || parsed < 100) {
        throw new Error("open_timeout_ms must be a number greater than or equal to 100");
    }
    return parsed;
}

function positionToPlain(position) {
    if (!position) return null;
    return { x: position.x, y: position.y, z: position.z };
}

function vecFromPosition(bot, position) {
    return bot.entity.position.offset(
        position.x - bot.entity.position.x,
        position.y - bot.entity.position.y,
        position.z - bot.entity.position.z,
    );
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

function inventorySnapshot(bot) {
    return {
        inventory_items: itemsToPlain(bot.inventory.items()),
        inventory_empty_slots: bot.inventory.emptySlotCount(),
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

async function closeWindowAndSync(bot, window) {
    const target = window || bot.currentWindow || null;
    if (target) {
        try {
            bot.closeWindow(target);
        } catch (err) {
            if (bot.currentWindow && bot.currentWindow !== target) {
                try {
                    bot.closeWindow(bot.currentWindow);
                } catch (_) {
                    // Best-effort cleanup; the original action result is more useful than close errors.
                }
            }
        }
    }
    await sleep(250);
}

async function closeCurrentContainerIfAny(bot) {
    if (bot.currentWindow) {
        await closeWindowAndSync(bot, bot.currentWindow);
    }
}

function resolveItem(bot, name) {
    const normalized = normalizeName(name);
    const item = bot.registry.itemsByName?.[normalized] || bot.registry.itemsByName?.[`minecraft:${normalized}`];
    if (item) return item;
    for (const [key, value] of Object.entries(bot.registry.itemsByName || {})) {
        if (normalizeName(key) === normalized) return value;
    }
    return null;
}

function normalizeItemRequests(items) {
    if (!Array.isArray(items) || items.length === 0) {
        throw new Error("items must be a non-empty array");
    }
    return items.map((item, index) => {
        if (typeof item === "string") {
            return {
                name: normalizeName(item, `items[${index}]`),
                count: 1,
            };
        }
        if (!item || typeof item !== "object" || Array.isArray(item)) {
            throw new Error(`items[${index}] must be an item name string or an object with name/count`);
        }
        const name = item.name ?? item.item;
        return {
            name: normalizeName(name, `items[${index}].name`),
            count: parsePositiveInteger(item.count ?? 1, `items[${index}].count`),
        };
    });
}

function countItemsByName(items, name) {
    const normalized = normalizeName(name);
    return items
        .filter((item) => item && typeof item.name === "string" && normalizeName(item.name) === normalized)
        .reduce((total, item) => total + (item.count || 0), 0);
}

function itemCapacityInStacks(stacks, itemDef, requestedCount, emptySlots) {
    let remaining = requestedCount;
    const stackSize = Math.max(1, itemDef.stackSize || 64);
    for (const stack of stacks) {
        if (!stack || stack.type !== itemDef.id) continue;
        if (stack.metadata !== undefined && stack.metadata !== null && stack.metadata !== 0) continue;
        remaining -= Math.max(0, stackSize - (stack.count || 0));
        if (remaining <= 0) {
            return { ok: true, required_slots: 0, remaining_after_stacks: 0 };
        }
    }
    const requiredSlots = Math.ceil(Math.max(0, remaining) / stackSize);
    return {
        ok: requiredSlots <= emptySlots,
        required_slots: requiredSlots,
        remaining_after_stacks: Math.max(0, remaining),
    };
}

function containerEmptySlots(window) {
    let empty = 0;
    for (let slot = 0; slot < window.inventoryStart; slot++) {
        if (!window.slots[slot]) empty += 1;
    }
    return empty;
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

function chestIds(bot) {
    const ids = [];
    for (const name of ["chest", "trapped_chest", "barrel"]) {
        const block = bot.registry.blocksByName?.[name];
        if (block) ids.push(block.id);
    }
    return ids;
}

// ---------------------------------------------------------------------------
// 箱子注册表：chest_id（编号）<-> 坐标
// ---------------------------------------------------------------------------

function chestRegistry(bot) {
    if (!bot._chestRegistry) {
        bot._chestRegistry = {
            nextId: 1,
            byId: {},
            byPos: {},
            byName: {},
        };
    }
    return bot._chestRegistry;
}

function resetChestRegistry(bot) {
    bot._chestRegistry = {
        nextId: 1,
        byId: {},
        byPos: {},
        byName: {},
    };
}

function loadChestRegistry(bot) {
    // 以磁盘文件为唯一事实来源：每次操作前重建，从而能读到其他 agent 的注册。
    resetChestRegistry(bot);
    const reg = bot._chestRegistry;
    let raw = null;
    try {
        raw = fs.readFileSync(CHEST_REGISTRY_FILE, "utf8");
    } catch (err) {
        if (err.code !== "ENOENT") {
            // 文件损坏时不阻断操作，按空表继续
            console.error(`[container] 读取箱子注册表失败: ${err.message}`);
        }
        return reg;
    }
    try {
        const data = JSON.parse(raw);
        reg.nextId = Math.max(1, Number(data.next_id) || 1);
        for (const item of data.chests || []) {
            if (!item || !item.position) continue;
            const id = Number(item.id);
            if (!Number.isFinite(id) || id < 1) continue;
            const key = chestPositionKey(item.position);
            const entry = {
                id,
                position: {
                    x: item.position.x,
                    y: item.position.y,
                    z: item.position.z,
                },
                type: item.type || "chest",
                name: item.name || null,
                registered_at: item.registered_at || null,
            };
            reg.byId[id] = entry;
            reg.byPos[key] = id;
            if (entry.name) reg.byName[entry.name] = id;
            if (id >= reg.nextId) reg.nextId = id + 1;
        }
    } catch (err) {
        console.error(`[container] 解析箱子注册表失败: ${err.message}`);
        resetChestRegistry(bot);
    }
    return reg;
}

function saveChestRegistry(bot) {
    const reg = bot._chestRegistry || { nextId: 1, byId: {}, byPos: {}, byName: {} };
    const data = {
        next_id: reg.nextId,
        chests: Object.values(reg.byId).sort((a, b) => a.id - b.id),
    };
    try {
        fs.mkdirSync(path.dirname(CHEST_REGISTRY_FILE), { recursive: true });
        const tmp = `${CHEST_REGISTRY_FILE}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
        fs.renameSync(tmp, CHEST_REGISTRY_FILE);
    } catch (err) {
        console.error(`[container] 保存箱子注册表失败: ${err.message}`);
    }
}

function chestPositionKey(position) {
    return `${position.x},${position.y},${position.z}`;
}

function listRegisteredChests(bot) {
    return Object.values(chestRegistry(bot).byId).sort((a, b) => a.id - b.id);
}

function registerChestEntry(bot, position, typeName, name = null) {
    const reg = chestRegistry(bot);
    const key = chestPositionKey(position);
    if (reg.byPos[key]) {
        const existing = reg.byId[reg.byPos[key]];
        if (name !== null && name !== undefined && name !== existing.name) {
            setChestName(bot, existing, name);
        }
        return existing;
    }
    const id = reg.nextId++;
    const entry = {
        id,
        position: { x: position.x, y: position.y, z: position.z },
        type: typeName || "chest",
        name: null,
        registered_at: Date.now(),
    };
    reg.byId[id] = entry;
    reg.byPos[key] = id;
    if (name !== null && name !== undefined) {
        setChestName(bot, entry, name);
    }
    return entry;
}

function setChestName(bot, entry, name) {
    const reg = chestRegistry(bot);
    const normalized = String(name).trim();
    if (!normalized) {
        throw new Error("chest name must be a non-empty string");
    }
    if (reg.byName[normalized] && reg.byName[normalized] !== entry.id) {
        throw new Error(
            `chest name '${normalized}' is already used by chest #${reg.byName[normalized]}`
        );
    }
    if (entry.name && reg.byName[entry.name] === entry.id) {
        delete reg.byName[entry.name];
    }
    entry.name = normalized;
    reg.byName[normalized] = entry.id;
}

// 大箱子由两个相邻方块组成，只注册"字典序较小"的那个半箱，避免同一箱子出现两个 id。
function isCanonicalChestPosition(bot, position, ids) {
    for (const offset of [{ x: -1, y: 0, z: 0 }, { x: 0, y: 0, z: -1 }]) {
        const neighbor = bot.blockAt(position.offset(offset.x, offset.y, offset.z));
        if (neighbor && ids.includes(neighbor.type)) return false;
    }
    return true;
}

function chestIdFromParams(params) {
    const raw = params?.chest_id ?? params?.chestId ?? null;
    if (raw === undefined || raw === null) return null;
    const id = Math.trunc(Number(raw));
    if (!Number.isFinite(id) || id < 1) {
        throw new Error("chest_id must be a positive integer");
    }
    return id;
}

function chestNameFromParams(params) {
    const raw = params?.chest_name ?? params?.chestName ?? null;
    if (raw === undefined || raw === null) return null;
    const name = String(raw).trim();
    if (!name) {
        throw new Error("chest_name must be a non-empty string");
    }
    return name;
}

function positionFromParam(value) {
    if (!value || typeof value !== "object") return null;
    const x = Number(value.x);
    const y = Number(value.y);
    const z = Number(value.z);
    if (![x, y, z].every(Number.isFinite)) return null;
    return { x: Math.floor(x), y: Math.floor(y), z: Math.floor(z) };
}

function chestPositionFromParams(params) {
    if (!params || typeof params !== "object") return null;
    const chest = params.chest;
    if (chest && typeof chest === "object") {
        return positionFromParam(chest.position) || positionFromParam(chest);
    }
    return positionFromParam(params.position);
}

function findChestAt(bot, position, maxDistance, ids) {
    const requestedPosition = bot.entity.position.offset(
        position.x - bot.entity.position.x,
        position.y - bot.entity.position.y,
        position.z - bot.entity.position.z,
    );
    const block = bot.blockAt(requestedPosition);
    if (block && ids.includes(block.type)) {
        const distance = bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5));
        return distance <= maxDistance ? block : null;
    }
    const found = bot.findBlock({
        matching: ids,
        maxDistance,
        point: bot.entity.position,
    });
    if (found && found.position.x === position.x && found.position.y === position.y && found.position.z === position.z) {
        return found;
    }
    return null;
}

function findChest(bot, params, maxDistance) {
    const ids = chestIds(bot);
    if (ids.length === 0) return null;

    const chestId = chestIdFromParams(params);
    const chestName = chestNameFromParams(params);
    if (chestId !== null || chestName !== null) {
        loadChestRegistry(bot);
        let entry = chestId !== null ? chestRegistry(bot).byId[chestId] : null;
        if (chestName !== null) {
            const namedId = chestRegistry(bot).byName[chestName];
            const namedEntry = namedId ? chestRegistry(bot).byId[namedId] : null;
            if (entry && namedEntry && entry.id !== namedEntry.id) {
                throw new Error("chest_id and chest_name refer to different chests");
            }
            entry = namedEntry;
        }
        if (!entry) return null;
        return findChestAt(bot, entry.position, maxDistance, ids);
    }

    const requested = chestPositionFromParams(params);
    if (requested) {
        return findChestAt(bot, requested, maxDistance, ids);
    }
    return bot.findBlock({ matching: ids, maxDistance });
}

async function approachChest(bot, chest) {
    ensurePathfinder(bot);
    const center = chest.position.offset(0.5, 0, 0.5);
    const distance = bot.entity.position.distanceTo(chest.position.offset(0.5, 0.5, 0.5));
    if (distance > 3) {
        const { goals } = bot.mcagentPathfinder;
        await bot.pathfinder.goto(new goals.GoalNear(center.x, center.y, center.z, 2));
        bot.clearControlStates();
    }
    await bot.lookAt(chest.position.offset(0.5, 0.5, 0.5), true);
}

function isContainerWindow(window) {
    return Boolean(window && typeof window.containerItems === "function" && Array.isArray(window.slots));
}

function waitForContainerWindow(bot, timeoutMs) {
    return new Promise((resolve, reject) => {
        let done = false;
        let timeout = null;

        const cleanup = () => {
            if (timeout) clearTimeout(timeout);
            if (typeof bot.removeListener === "function") {
                bot.removeListener("windowOpen", onWindowOpen);
            }
        };

        const finish = (window) => {
            if (done) return;
            done = true;
            cleanup();
            resolve(window);
        };

        const onWindowOpen = (window) => {
            if (isContainerWindow(window)) {
                finish(window);
            }
        };

        timeout = setTimeout(() => {
            if (isContainerWindow(bot.currentWindow)) {
                finish(bot.currentWindow);
                return;
            }
            done = true;
            cleanup();
            reject(new Error(`container window did not open within ${timeoutMs}ms`));
        }, timeoutMs);
        bot.once("windowOpen", onWindowOpen);
    });
}

async function openChestWindow(bot, chest, openTimeoutMs) {
    const waiter = waitForContainerWindow(bot, openTimeoutMs);
    try {
        await bot.activateBlock(chest);
        return await waiter;
    } catch (err) {
        waiter.catch(() => {});
        if (isContainerWindow(bot.currentWindow)) {
            return bot.currentWindow;
        }
        throw err;
    }
}

async function openChest(ctx, params, action) {
    const bot = ctx.bot;
    const maxDistance = parseNonNegativeNumber(params.max_distance, 45, "max_distance");
    const openTimeoutMs = parseOpenTimeoutMs(params.open_timeout_ms);
    let chest;
    try {
        chest = findChest(bot, params, maxDistance);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params,
        });
    }
    if (!chest) {
        loadChestRegistry(bot);
        let chestId = null;
        let chestName = null;
        try {
            chestId = chestIdFromParams(params);
        } catch (_) {
            // 无效 chest_id 已在 findChest 抛出
        }
        try {
            chestName = chestNameFromParams(params);
        } catch (_) {
            // 无效 chest_name 已在 findChest 抛出
        }
        const unknownCode = chestName !== null && chestId === null
            ? "chest_name_unknown"
            : "chest_id_unknown";
        fail(action, unknownCode, {
            ok: false,
            error_code: unknownCode,
            chest_id: chestId,
            chest_name: chestName,
            max_distance: maxDistance,
            requested_chest: params.chest || params.position || null,
            registered_chests: listRegisteredChests(bot),
        });
    }
    try {
        await closeCurrentContainerIfAny(bot);
        await approachChest(bot, chest);
        chest = findChest(bot, { chest: { position: positionToPlain(chest.position) } }, maxDistance) || chest;
        const window = await openChestWindow(bot, chest, openTimeoutMs);
        return { chest, window, maxDistance, openTimeoutMs };
    } catch (err) {
        await closeCurrentContainerIfAny(bot);
        fail(action, "chest_open_failed", {
            ok: false,
            error_code: "chest_open_failed",
            max_distance: maxDistance,
            open_timeout_ms: openTimeoutMs,
            chest: blockToPlain(bot, chest),
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
}

function windowSnapshot(bot, chest, window) {
    return {
        ok: true,
        chest: blockToPlain(bot, chest),
        chest_items: itemsToPlain(window.containerItems()),
        chest_empty_slots: containerEmptySlots(window),
        ...inventorySnapshot(bot),
    };
}

function chestWindowSnapshot(bot, chest, window) {
    return {
        ok: true,
        chest: blockToPlain(bot, chest),
        chest_items: itemsToPlain(window.containerItems()),
        chest_empty_slots: containerEmptySlots(window),
    };
}

async function inspectChest(ctx, params, action) {
    const { chest, window } = await openChest(ctx, params || {}, action);
    try {
        return windowSnapshot(ctx.bot, chest, window);
    } finally {
        await closeWindowAndSync(ctx.bot, window);
    }
}

function missingItems(requests, availableItems) {
    const missing = [];
    for (const request of requests) {
        const available = countItemsByName(availableItems, request.name);
        if (available < request.count) {
            missing.push({
                name: request.name,
                required: request.count,
                available,
                missing: request.count - available,
            });
        }
    }
    return missing;
}

function inventorySpaceFailures(bot, requests) {
    let emptySlots = bot.inventory.emptySlotCount();
    const stacks = bot.inventory.items().map((item) => ({ ...item }));
    const failures = [];
    let requiredSlotsTotal = 0;
    for (const request of requests) {
        const itemDef = resolveItem(bot, request.name);
        if (!itemDef) {
            failures.push({ name: request.name, reason: "unknown_item" });
            continue;
        }
        const check = itemCapacityInStacks(stacks, itemDef, request.count, emptySlots);
        if (!check.ok) {
            failures.push({
                name: request.name,
                required: request.count,
                required_slots: check.required_slots,
                available_empty_slots: emptySlots,
            });
        } else if (check.required_slots > 0) {
            emptySlots -= check.required_slots;
            requiredSlotsTotal += check.required_slots;
            stacks.push({ type: itemDef.id, name: request.name, metadata: 0, count: request.count, stackSize: itemDef.stackSize });
        }
    }
    return { failures, required_slots: requiredSlotsTotal, available_empty_slots: bot.inventory.emptySlotCount() };
}

function chestSpaceFailures(bot, window, requests) {
    let emptySlots = containerEmptySlots(window);
    const stacks = window.containerItems().map((item) => ({ ...item }));
    const failures = [];
    let requiredSlotsTotal = 0;
    for (const request of requests) {
        const itemDef = resolveItem(bot, request.name);
        if (!itemDef) {
            failures.push({ name: request.name, reason: "unknown_item" });
            continue;
        }
        const check = itemCapacityInStacks(stacks, itemDef, request.count, emptySlots);
        if (!check.ok) {
            failures.push({
                name: request.name,
                required: request.count,
                required_slots: check.required_slots,
                available_empty_slots: emptySlots,
            });
        } else if (check.required_slots > 0) {
            emptySlots -= check.required_slots;
            requiredSlotsTotal += check.required_slots;
            stacks.push({ type: itemDef.id, name: request.name, metadata: 0, count: request.count, stackSize: itemDef.stackSize });
        }
    }
    return { failures, required_slots: requiredSlotsTotal, available_empty_slots: containerEmptySlots(window) };
}

async function pickObjectFromChest(ctx, params, action) {
    let requests;
    try {
        requests = normalizeItemRequests(params?.items);
    } catch (err) {
        fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
            items: params?.items ?? null,
        });
    }
    const opened = await openChest(ctx, params || {}, action);
    const chest = opened.chest;
    let window = opened.window;
    try {
        const before = windowSnapshot(ctx.bot, chest, window);
        const missing = missingItems(requests, before.chest_items);
        if (missing.length > 0) {
            fail(action, "chest_item_missing", {
                ok: false,
                error_code: "chest_item_missing",
                missing,
                ...before,
            });
        }
        const space = inventorySpaceFailures(ctx.bot, requests);
        if (space.failures.length > 0) {
            fail(action, "inventory_space_insufficient", {
                ok: false,
                error_code: "inventory_space_insufficient",
                space,
                ...before,
            });
        }

        const picked = [];
        for (const request of requests) {
            const itemDef = resolveItem(ctx.bot, request.name);
            try {
                await window.withdraw(itemDef.id, null, request.count);
                picked.push({ name: request.name, requested: request.count, picked: request.count });
            } catch (err) {
                fail(action, "withdraw_failed", {
                    ok: false,
                    error_code: "withdraw_failed",
                    picked,
                    failed_item: request,
                    reason: err.message,
                    ...windowSnapshot(ctx.bot, chest, window),
                });
            }
        }
        const chestAfter = chestWindowSnapshot(ctx.bot, chest, window);
        await closeWindowAndSync(ctx.bot, window);
        window = null;
        return {
            ...chestAfter,
            ...inventorySnapshot(ctx.bot),
            picked,
        };
    } finally {
        if (window) await closeWindowAndSync(ctx.bot, window);
    }
}

async function placeObjectToChest(ctx, params, action) {
    let requests;
    try {
        requests = normalizeItemRequests(params?.items);
    } catch (err) {
        fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
            items: params?.items ?? null,
        });
    }
    const opened = await openChest(ctx, params || {}, action);
    const chest = opened.chest;
    let window = opened.window;
    try {
        const before = windowSnapshot(ctx.bot, chest, window);
        const missing = missingItems(requests, before.inventory_items);
        if (missing.length > 0) {
            fail(action, "inventory_item_missing", {
                ok: false,
                error_code: "inventory_item_missing",
                missing,
                ...before,
            });
        }
        const space = chestSpaceFailures(ctx.bot, window, requests);
        if (space.failures.length > 0) {
            fail(action, "chest_space_insufficient", {
                ok: false,
                error_code: "chest_space_insufficient",
                space,
                ...before,
            });
        }

        const placed = [];
        for (const request of requests) {
            const itemDef = resolveItem(ctx.bot, request.name);
            try {
                await window.deposit(itemDef.id, null, request.count);
                placed.push({ name: request.name, requested: request.count, placed: request.count });
            } catch (err) {
                fail(action, "deposit_failed", {
                    ok: false,
                    error_code: "deposit_failed",
                    placed,
                    failed_item: request,
                    reason: err.message,
                    ...windowSnapshot(ctx.bot, chest, window),
                });
            }
        }
        const chestAfter = chestWindowSnapshot(ctx.bot, chest, window);
        await closeWindowAndSync(ctx.bot, window);
        window = null;
        return {
            ...chestAfter,
            ...inventorySnapshot(ctx.bot),
            placed,
        };
    } finally {
        if (window) await closeWindowAndSync(ctx.bot, window);
    }
}

// ---------------------------------------------------------------------------
// register_chests / list_chests：为附近箱子分配稳定编号（chest_id）
// ---------------------------------------------------------------------------

function parseChestNames(params) {
    const names = params?.names;
    if (names === undefined || names === null) return null;
    if (typeof names !== "object" || Array.isArray(names)) {
        throw new Error("names must be an object mapping 'x,y,z' to a unique chest name");
    }
    const result = {};
    for (const [key, value] of Object.entries(names)) {
        const name = String(value ?? "").trim();
        if (!name) {
            throw new Error(`names['${key}'] must be a non-empty string`);
        }
        result[key] = name;
    }
    return result;
}

async function registerChests(ctx, params, action) {
    const bot = ctx.bot;
    const maxDistance = parseNonNegativeNumber(params?.max_distance, 45, "max_distance");
    loadChestRegistry(bot);
    let nameMap;
    try {
        nameMap = parseChestNames(params);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    const ids = chestIds(bot);
    if (ids.length === 0) {
        fail(action, "chest_type_unknown", {
            ok: false,
            error_code: "chest_type_unknown",
            reason: "no chest-like block types registered in this game version",
        });
    }
    const found = bot.findBlocks({ matching: ids, maxDistance, count: 128 });
    const registered = [];
    const skipped = [];
    for (const position of found) {
        if (!isCanonicalChestPosition(bot, position, ids)) {
            skipped.push({ position: positionToPlain(position), reason: "double_chest_half" });
            continue;
        }
        const block = bot.blockAt(position);
        let name = null;
        if (nameMap) {
            const key = chestPositionKey(position);
            if (Object.prototype.hasOwnProperty.call(nameMap, key)) {
                name = nameMap[key];
            }
        }
        try {
            registered.push(registerChestEntry(bot, position, block ? block.name : "chest", name));
        } catch (err) {
            fail(action, "chest_name_duplicate", {
                ok: false,
                error_code: "chest_name_duplicate",
                reason: err.message,
                position: positionToPlain(position),
                chests: listRegisteredChests(bot),
            });
        }
    }
    const chests = listRegisteredChests(bot);
    saveChestRegistry(bot);
    return {
        ok: true,
        max_distance: maxDistance,
        registered,
        registered_count: registered.length,
        skipped_count: skipped.length,
        chests,
        chest_count: chests.length,
    };
}

async function listChests(ctx, params, action) {
    const bot = ctx.bot;
    loadChestRegistry(bot);
    const ids = chestIds(bot);
    const chests = listRegisteredChests(bot).map((entry) => {
        const block = bot.blockAt(vecFromPosition(bot, entry.position));
        const present = Boolean(block && ids.includes(block.type));
        return {
            id: entry.id,
            name: entry.name,
            position: entry.position,
            type: entry.type,
            registered_at: entry.registered_at,
            present,
            distance: present
                ? bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5))
                : null,
        };
    });
    return { ok: true, chests, count: chests.length };
}

module.exports = {
    name: "container",
    commands: {
        inspect_chest: { execute: inspectChest },
        pick_object_from_chest: { execute: pickObjectFromChest },
        place_object_to_chest: { execute: placeObjectToChest },
        register_chests: { execute: registerChests },
        list_chests: { execute: listChests },
    },
    service: {
        normalizeName,
        parsePositiveInteger,
        parseNonNegativeNumber,
        parseOpenTimeoutMs,
        positionToPlain,
        itemToPlain,
        itemsToPlain,
        blockToPlain,
        inventorySnapshot,
        fail,
        sleep,
        closeWindowAndSync,
        closeCurrentContainerIfAny,
        resolveItem,
        countItemsByName,
        itemCapacityInStacks,
        containerEmptySlots,
        loadChestRegistry,
        listRegisteredChests,
        findChestAt,
        findChest,
        approachChest,
        openChestWindow,
        openChest,
        windowSnapshot,
        chestWindowSnapshot,
        missingItems,
        inventorySpaceFailures,
        chestSpaceFailures,
    },
};
