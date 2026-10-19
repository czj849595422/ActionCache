"use strict";

const AIR_BLOCK_NAMES = new Set(["air", "cave_air", "void_air"]);
const DEFAULT_PROTECTED_BLOCK_NAMES = new Set([
    "bedrock",
    "barrier",
    "chest",
    "trapped_chest",
    "barrel",
    "furnace",
    "blast_furnace",
    "smoker",
    "crafting_table",
]);

function normalizeName(value, field = "name") {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function numberParam(value, field) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
        throw new Error(`${field} must be a finite number`);
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

function parsePositiveInteger(value, fallback, field) {
    const parsed = value === undefined || value === null ? Number(fallback) : Math.trunc(Number(value));
    if (!Number.isFinite(parsed) || parsed < 1) {
        throw new Error(`${field} must be a positive integer`);
    }
    return parsed;
}

function positionFromParams(params = {}) {
    const raw = params.position && typeof params.position === "object" ? params.position : params;
    const x = numberParam(raw.x, "x");
    const y = numberParam(raw.y, "y");
    const z = numberParam(raw.z, "z");
    return {
        x: Math.floor(x),
        y: Math.floor(y),
        z: Math.floor(z),
    };
}

function vecFromCoords(bot, x, y, z) {
    return bot.entity.position.offset(
        x - bot.entity.position.x,
        y - bot.entity.position.y,
        z - bot.entity.position.z,
    );
}

function positionToVec(bot, position) {
    return vecFromCoords(bot, position.x, position.y, position.z);
}

function targetCenterVec(bot, position) {
    return vecFromCoords(bot, position.x + 0.5, position.y + 0.5, position.z + 0.5);
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

function blockToPlain(bot, block) {
    if (!block) return null;
    const center = block.position.offset(0.5, 0.5, 0.5);
    const distance = bot.entity && bot.entity.position ? bot.entity.position.distanceTo(center) : null;
    return {
        type: block.type ?? null,
        name: block.name ?? null,
        displayName: block.displayName ?? null,
        metadata: block.metadata ?? null,
        stateId: block.stateId ?? null,
        boundingBox: block.boundingBox ?? null,
        diggable: block.diggable ?? null,
        hardness: block.hardness ?? null,
        position: positionToPlain(block.position),
        distance,
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
    action.result = {
        ok: false,
        error_code: errorCode,
        ...result,
    };
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

function distanceCheckEnabled(params) {
    return params?.distance_check_enabled === true;
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
    const center = targetCenterVec(bot, position);
    return Object.values(bot.entities || {}).filter(
        (entity) => isItemEntity(entity) && entity.position.distanceTo(center) <= radius
    );
}

async function waitForDroppedItems(bot, position, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const items = findDroppedItemsNear(bot, position, 4);
        if (items.length > 0) return items;
        await sleep(200);
    }
    return findDroppedItemsNear(bot, position, 4);
}

async function collectDroppedItems(bot, target, timeoutMs, action) {
    ensurePathfinder(bot);
    const items = await waitForDroppedItems(bot, target, Math.min(timeoutMs, 5000));
    if (items.length === 0) {
        return {
            collected_count: 0,
            collected_items: [],
            collection_supported: true,
        };
    }
    const deadline = Date.now() + timeoutMs;
    let remaining = findDroppedItemsNear(bot, target, 4);
    while (remaining.length > 0 && Date.now() < deadline) {
        const item = remaining[0];
        try {
            const { goals } = bot.mcagentPathfinder;
            const goal = new goals.GoalNear(item.position.x, item.position.y, item.position.z, 0.5);
            await withTimeout(
                bot.pathfinder.goto(goal),
                Math.min(10000, Math.max(1000, deadline - Date.now())),
                "collect-walk"
            );
        } catch (_) {
            bot.pathfinder.setGoal(null);
        }
        bot.clearControlStates();
        await waitForTicksOrSleep(bot, 10);
        remaining = findDroppedItemsNear(bot, target, 4);
    }
    return {
        collected_count: items.length - remaining.length,
        collected_items: items.map((entity) => {
            const dropped = typeof entity.getDroppedItem === "function" ? entity.getDroppedItem() : null;
            return itemToPlain(dropped);
        }),
        collection_supported: true,
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

function distanceToTarget(bot, position) {
    return bot.entity.position.distanceTo(targetCenterVec(bot, position));
}

function blockAt(bot, position) {
    return bot.blockAt(positionToVec(bot, position));
}

function isAirBlock(block) {
    return Boolean(block && AIR_BLOCK_NAMES.has(block.name));
}

function isReferenceBlock(block) {
    return Boolean(block && !isAirBlock(block) && block.boundingBox !== "empty");
}

function targetBaseResult(bot, target, block = null) {
    return {
        target,
        distance: distanceToTarget(bot, target),
        block: blockToPlain(bot, block),
        ...inventorySnapshot(bot),
    };
}

function resolveItem(bot, name) {
    const normalized = normalizeName(name, "item");
    const item = bot.registry.itemsByName?.[normalized] || bot.registry.itemsByName?.[`minecraft:${normalized}`];
    if (item) return { normalized, item };
    for (const [key, value] of Object.entries(bot.registry.itemsByName || {})) {
        if (normalizeName(key) === normalized) return { normalized, item: value };
    }
    return { normalized, item: null };
}

function resolveBlock(bot, name) {
    const normalized = normalizeName(name, "item");
    const block = bot.registry.blocksByName?.[normalized] || bot.registry.blocksByName?.[`minecraft:${normalized}`];
    if (block) return { normalized, block };
    for (const [key, value] of Object.entries(bot.registry.blocksByName || {})) {
        if (normalizeName(key) === normalized) return { normalized, block: value };
    }
    return { normalized, block: null };
}

function missingItem(name, required, available = 0) {
    return [{ name, required, available, missing: Math.max(0, required - available) }];
}

async function approachTarget(bot, target, approachRange, timeoutMs) {
    ensurePathfinder(bot);
    const distance = distanceToTarget(bot, target);
    if (distance > approachRange) {
        const { goals } = bot.mcagentPathfinder;
        const goal = new goals.GoalNear(target.x + 0.5, target.y, target.z + 0.5, approachRange);
        try {
            await withTimeout(bot.pathfinder.goto(goal), timeoutMs, "pathfinder");
        } catch (err) {
            bot.pathfinder.setGoal(null);
            bot.clearControlStates();
            throw err;
        }
        bot.clearControlStates();
    }
    await bot.lookAt(targetCenterVec(bot, target), true);
}

const FACE_VECTORS = [
    { x: 0, y: 1, z: 0 },
    { x: 0, y: -1, z: 0 },
    { x: 1, y: 0, z: 0 },
    { x: -1, y: 0, z: 0 },
    { x: 0, y: 0, z: 1 },
    { x: 0, y: 0, z: -1 },
];

function findReferenceBlock(bot, target) {
    for (const faceVector of FACE_VECTORS) {
        const referencePosition = {
            x: target.x - faceVector.x,
            y: target.y - faceVector.y,
            z: target.z - faceVector.z,
        };
        const referenceBlock = blockAt(bot, referencePosition);
        if (isReferenceBlock(referenceBlock)) {
            return { referenceBlock, faceVector, referencePosition };
        }
    }
    return null;
}

function parseStringList(value, field) {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value)) {
        throw new Error(`${field} must be an array`);
    }
    return value.map((item, index) => normalizeName(item, `${field}[${index}]`));
}

function parseOrder(value, fallback) {
    const order = value === undefined || value === null ? fallback : String(value);
    if (!["nearest", "natural", "bottom_up", "top_down"].includes(order)) {
        throw new Error("order must be one of nearest, natural, bottom_up, top_down");
    }
    return order;
}

function parseReplacePolicy(value) {
    const policy = value === undefined || value === null ? "air_only" : String(value);
    if (!["air_only", "replace_matching", "replace_any"].includes(policy)) {
        throw new Error("replace_policy must be one of air_only, replace_matching, replace_any");
    }
    return policy;
}

function parseRegion(params, defaultMaxBlocks) {
    if (!params || typeof params !== "object") {
        throw new Error("params must be an object");
    }
    if (!params.from || !params.to) {
        throw new Error("from and to are required");
    }
    const from = positionFromParams(params.from);
    const to = positionFromParams(params.to);
    const min = {
        x: Math.min(from.x, to.x),
        y: Math.min(from.y, to.y),
        z: Math.min(from.z, to.z),
    };
    const max = {
        x: Math.max(from.x, to.x),
        y: Math.max(from.y, to.y),
        z: Math.max(from.z, to.z),
    };
    const size = {
        x: max.x - min.x + 1,
        y: max.y - min.y + 1,
        z: max.z - min.z + 1,
    };
    const volume = size.x * size.y * size.z;
    const maxBlocks = parsePositiveInteger(params.max_blocks, defaultMaxBlocks, "max_blocks");
    return {
        from,
        to,
        min,
        max,
        size,
        volume,
        max_blocks: maxBlocks,
    };
}

function regionToPlain(region) {
    return {
        from: region.from,
        to: region.to,
        min: region.min,
        max: region.max,
        size: region.size,
        volume: region.volume,
        max_blocks: region.max_blocks,
    };
}

function enumerateRegionPositions(bot, region, order) {
    const positions = [];
    for (let y = region.min.y; y <= region.max.y; y += 1) {
        for (let x = region.min.x; x <= region.max.x; x += 1) {
            for (let z = region.min.z; z <= region.max.z; z += 1) {
                positions.push({ x, y, z });
            }
        }
    }
    if (order === "nearest") {
        positions.sort((a, b) => distanceToTarget(bot, a) - distanceToTarget(bot, b));
    } else if (order === "top_down") {
        positions.sort((a, b) => (b.y - a.y) || (a.x - b.x) || (a.z - b.z));
    } else if (order === "bottom_up") {
        positions.sort((a, b) => (a.y - b.y) || (a.x - b.x) || (a.z - b.z));
    }
    return positions;
}

function incrementCount(counts, name) {
    const key = name || "unknown";
    counts[key] = (counts[key] || 0) + 1;
}

function inventoryCountByItemId(bot, itemId) {
    return bot.inventory.items()
        .filter((item) => item && item.type === itemId)
        .reduce((total, item) => total + (item.count || 0), 0);
}

function normalizeActionResult(actionName, result, err) {
    const payload = result && typeof result === "object" ? result : {};
    return {
        ok: false,
        action: actionName,
        error_code: payload.error_code || err?.code || "action_failed",
        reason: payload.reason || err?.message || "unknown_error",
        result: payload,
    };
}

async function executeSingleBreak(ctx, target, params) {
    const action = { result: null };
    try {
        const result = await breakBlock(ctx, { ...params, ...target }, action);
        return { ok: true, result };
    } catch (err) {
        return normalizeActionResult("block.break", action.result, err);
    }
}

async function executeSinglePlace(ctx, target, params) {
    const action = { result: null };
    try {
        const result = await placeBlock(ctx, { ...params, ...target }, action);
        return { ok: true, result };
    } catch (err) {
        return normalizeActionResult("block.place", action.result, err);
    }
}

function finishRegionResult(action, errorCode, result) {
    if (result.failed_count > 0) {
        fail(action, errorCode, result);
    }
    return result;
}

function scanRegion(bot, region, positions, maxDistance) {
    return positions.map((position) => {
        const distance = distanceToTarget(bot, position);
        const block = distance <= maxDistance ? blockAt(bot, position) : null;
        return {
            position,
            distance,
            block,
            block_plain: blockToPlain(bot, block),
            out_of_range: distance > maxDistance,
        };
    });
}

async function inspectRegion(ctx, params, action) {
    const bot = ctx.bot;
    let region;
    let maxDistance;
    try {
        region = parseRegion(params || {}, 256);
        maxDistance = distanceCheckEnabled(params)
            ? parseNonNegativeNumber(params?.max_distance, 45, "max_distance")
            : Infinity;
    } catch (err) {
        fail(action, "invalid_region", {
            reason: err.message,
            params: params || {},
        });
    }
    if (region.volume > region.max_blocks) {
        fail(action, "region_too_large", {
            region: regionToPlain(region),
            requested_volume: region.volume,
            max_blocks: region.max_blocks,
        });
    }

    const includeAir = Boolean(params?.include_air);
    const positions = enumerateRegionPositions(bot, region, "natural");
    const scanned = scanRegion(bot, region, positions, maxDistance);
    const blockCounts = {};
    const blocks = [];
    const skipped = [];

    for (const entry of scanned) {
        if (entry.out_of_range) {
            skipped.push({ target: entry.position, reason: "out_of_range", distance: entry.distance });
            continue;
        }
        if (!entry.block) {
            skipped.push({ target: entry.position, reason: "block_not_loaded" });
            continue;
        }
        incrementCount(blockCounts, entry.block.name);
        if (includeAir || !isAirBlock(entry.block)) {
            blocks.push(entry.block_plain);
        }
    }

    return {
        ok: true,
        region: regionToPlain(region),
        include_air: includeAir,
        max_distance: maxDistance,
        scanned_count: scanned.length,
        loaded_count: scanned.length - skipped.length,
        block_counts: blockCounts,
        blocks,
        skipped,
    };
}

async function breakRegion(ctx, params, action) {
    const bot = ctx.bot;
    let region;
    let includeBlocks;
    let excludeBlocks;
    let maxDistance;
    let approachRange;
    let order;
    let perBlockTimeoutMs;
    try {
        region = parseRegion(params || {}, 128);
        includeBlocks = new Set(parseStringList(params?.include_blocks, "include_blocks"));
        excludeBlocks = new Set(parseStringList(params?.exclude_blocks, "exclude_blocks"));
        maxDistance = distanceCheckEnabled(params)
            ? parseNonNegativeNumber(params?.max_distance, 45, "max_distance")
            : Infinity;
        approachRange = parseNonNegativeNumber(params?.approach_range, 3, "approach_range");
        order = parseOrder(params?.order, "nearest");
        parsePositiveInteger(params?.timeout_ms, 120000, "timeout_ms");
        perBlockTimeoutMs = parsePositiveInteger(params?.per_block_timeout_ms, 20000, "per_block_timeout_ms");
    } catch (err) {
        fail(action, "invalid_region_params", {
            reason: err.message,
            params: params || {},
            ...inventorySnapshot(bot),
        });
    }
    if (region.volume > region.max_blocks) {
        fail(action, "region_too_large", {
            region: regionToPlain(region),
            requested_volume: region.volume,
            max_blocks: region.max_blocks,
            ...inventorySnapshot(bot),
        });
    }

    const strict = Boolean(params?.strict);
    const collectDrops = params?.collect_drops !== false;
    const allowProtected = Boolean(params?.allow_protected);
    const skipped = [];
    const candidates = [];
    const positions = enumerateRegionPositions(bot, region, order);
    const scanned = scanRegion(bot, region, positions, maxDistance);

    for (const entry of scanned) {
        const target = entry.position;
        const block = entry.block;
        const blockPlain = entry.block_plain;
        if (entry.out_of_range) {
            skipped.push({ target, reason: "out_of_range", distance: entry.distance });
            continue;
        }
        if (!block) {
            skipped.push({ target, reason: "block_not_loaded" });
            continue;
        }
        if (isAirBlock(block)) {
            skipped.push({ target, reason: "air", block: blockPlain });
            continue;
        }
        if (includeBlocks.size > 0 && !includeBlocks.has(normalizeName(block.name))) {
            skipped.push({ target, reason: "not_included", block: blockPlain });
            continue;
        }
        if (excludeBlocks.has(normalizeName(block.name))) {
            skipped.push({ target, reason: "excluded", block: blockPlain });
            continue;
        }
        if (!allowProtected && DEFAULT_PROTECTED_BLOCK_NAMES.has(normalizeName(block.name))) {
            skipped.push({ target, reason: "protected_block", block: blockPlain });
            continue;
        }
        if (block.diggable === false || (typeof bot.canDigBlock === "function" && !bot.canDigBlock(block))) {
            skipped.push({ target, reason: "block_not_diggable", block: blockPlain });
            continue;
        }
        candidates.push({ target, block: blockPlain });
    }

    const succeeded = [];
    const failed = [];
    for (const candidate of candidates) {
        const single = await executeSingleBreak(ctx, candidate.target, {
            max_distance: maxDistance,
            approach_range: approachRange,
            timeout_ms: perBlockTimeoutMs,
            collect_drops: collectDrops,
        });
        if (single.ok) {
            succeeded.push({
                target: candidate.target,
                broken_block: single.result.broken_block,
                block_after: single.result.block_after,
            });
        } else {
            failed.push({
                target: candidate.target,
                block: candidate.block,
                error_code: single.error_code,
                reason: single.reason,
                result: single.result,
            });
            if (strict) break;
        }
    }

    const result = {
        ok: failed.length === 0,
        region: regionToPlain(region),
        max_distance: maxDistance,
        approach_range: approachRange,
        order,
        strict,
        collect_drops: collectDrops,
        allow_protected: allowProtected,
        include_blocks: Array.from(includeBlocks),
        exclude_blocks: Array.from(excludeBlocks),
        planned_count: candidates.length,
        success_count: succeeded.length,
        failed_count: failed.length,
        skipped_count: skipped.length,
        succeeded,
        failed,
        skipped,
        ...inventorySnapshot(bot),
    };
    return finishRegionResult(action, strict ? "region_failed" : "region_partial_failed", result);
}

async function placeRegion(ctx, params, action) {
    const bot = ctx.bot;
    let region;
    let itemName;
    let resolvedItem;
    let resolvedBlock;
    let replacePolicy;
    let replaceBlocks;
    let maxDistance;
    let approachRange;
    let order;
    let perBlockTimeoutMs;
    try {
        region = parseRegion(params || {}, 128);
        itemName = normalizeName(params?.item, "item");
        resolvedItem = resolveItem(bot, itemName);
        resolvedBlock = resolveBlock(bot, itemName);
        replacePolicy = parseReplacePolicy(params?.replace_policy);
        replaceBlocks = new Set(parseStringList(params?.replace_blocks, "replace_blocks"));
        maxDistance = distanceCheckEnabled(params)
            ? parseNonNegativeNumber(params?.max_distance, 45, "max_distance")
            : Infinity;
        approachRange = parseNonNegativeNumber(params?.approach_range, 3, "approach_range");
        order = parseOrder(params?.order, "bottom_up");
        parsePositiveInteger(params?.timeout_ms, 120000, "timeout_ms");
        perBlockTimeoutMs = parsePositiveInteger(params?.per_block_timeout_ms, 20000, "per_block_timeout_ms");
    } catch (err) {
        fail(action, "invalid_region_params", {
            reason: err.message,
            params: params || {},
            ...inventorySnapshot(bot),
        });
    }
    if (region.volume > region.max_blocks) {
        fail(action, "region_too_large", {
            region: regionToPlain(region),
            requested_volume: region.volume,
            max_blocks: region.max_blocks,
            ...inventorySnapshot(bot),
        });
    }
    if (!resolvedItem.item) {
        fail(action, "unknown_item", {
            item: itemName,
            reason: `No item named ${itemName}`,
            ...inventorySnapshot(bot),
        });
    }
    if (!resolvedBlock.block) {
        fail(action, "item_not_placeable", {
            item: itemName,
            reason: `${itemName} is not known as a placeable block`,
            ...inventorySnapshot(bot),
        });
    }
    if (replacePolicy === "replace_matching" && replaceBlocks.size === 0) {
        fail(action, "replace_blocks_required", {
            item: itemName,
            replace_policy: replacePolicy,
            reason: "replace_blocks is required when replace_policy is replace_matching",
            ...inventorySnapshot(bot),
        });
    }

    const strict = Boolean(params?.strict);
    const allowProtected = Boolean(params?.allow_protected);
    const collectDrops = params?.collect_drops !== false;
    const skipped = [];
    const candidates = [];
    const positions = enumerateRegionPositions(bot, region, order);
    const scanned = scanRegion(bot, region, positions, maxDistance);

    for (const entry of scanned) {
        const target = entry.position;
        const block = entry.block;
        const blockPlain = entry.block_plain;
        if (entry.out_of_range) {
            skipped.push({ target, reason: "out_of_range", distance: entry.distance });
            continue;
        }
        if (!block) {
            skipped.push({ target, reason: "block_not_loaded" });
            continue;
        }
        if (normalizeName(block.name) === itemName) {
            skipped.push({ target, reason: "already_target_block", block: blockPlain });
            continue;
        }
        if (isAirBlock(block)) {
            candidates.push({ target, target_block_before: blockPlain, needs_break: false });
            continue;
        }
        if (replacePolicy === "air_only") {
            skipped.push({ target, reason: "target_occupied", block: blockPlain });
            continue;
        }
        if (!allowProtected && DEFAULT_PROTECTED_BLOCK_NAMES.has(normalizeName(block.name))) {
            skipped.push({ target, reason: "protected_block", block: blockPlain });
            continue;
        }
        if (replacePolicy === "replace_matching" && !replaceBlocks.has(normalizeName(block.name))) {
            skipped.push({ target, reason: "not_replace_match", block: blockPlain });
            continue;
        }
        candidates.push({ target, target_block_before: blockPlain, needs_break: true });
    }

    const availableCount = inventoryCountByItemId(bot, resolvedItem.item.id);
    if (availableCount < candidates.length) {
        fail(action, "inventory_item_missing", {
            ok: false,
            item: itemName,
            region: regionToPlain(region),
            planned_count: candidates.length,
            missing: missingItem(itemName, candidates.length, availableCount),
            skipped,
            ...inventorySnapshot(bot),
        });
    }

    const succeeded = [];
    const failed = [];
    for (const candidate of candidates) {
        let breakResult = null;
        if (candidate.needs_break) {
            breakResult = await executeSingleBreak(ctx, candidate.target, {
                max_distance: maxDistance,
                approach_range: approachRange,
                timeout_ms: perBlockTimeoutMs,
                collect_drops: collectDrops,
            });
            if (!breakResult.ok) {
                failed.push({
                    target: candidate.target,
                    stage: "break",
                    block: candidate.target_block_before,
                    error_code: breakResult.error_code,
                    reason: breakResult.reason,
                    result: breakResult.result,
                });
                if (strict) break;
                continue;
            }
        }
        const placeResult = await executeSinglePlace(ctx, candidate.target, {
            item: itemName,
            max_distance: maxDistance,
            approach_range: approachRange,
            timeout_ms: perBlockTimeoutMs,
        });
        if (placeResult.ok) {
            succeeded.push({
                target: candidate.target,
                replaced_block: candidate.needs_break ? candidate.target_block_before : null,
                break_result: breakResult ? breakResult.result : null,
                target_block_after: placeResult.result.target_block_after,
            });
        } else {
            failed.push({
                target: candidate.target,
                stage: "place",
                block: candidate.target_block_before,
                error_code: placeResult.error_code,
                reason: placeResult.reason,
                result: placeResult.result,
            });
            if (strict) break;
        }
    }

    const result = {
        ok: failed.length === 0,
        region: regionToPlain(region),
        item: itemName,
        replace_policy: replacePolicy,
        replace_blocks: Array.from(replaceBlocks),
        max_distance: maxDistance,
        approach_range: approachRange,
        order,
        strict,
        allow_protected: allowProtected,
        collect_drops: collectDrops,
        planned_count: candidates.length,
        success_count: succeeded.length,
        failed_count: failed.length,
        skipped_count: skipped.length,
        succeeded,
        failed,
        skipped,
        ...inventorySnapshot(bot),
    };
    return finishRegionResult(action, strict ? "region_failed" : "region_partial_failed", result);
}

async function inspectBlock(ctx, params, action) {
    const bot = ctx.bot;
    let target;
    try {
        target = positionFromParams(params || {});
    } catch (err) {
        fail(action, "invalid_position", {
            reason: err.message,
            params: params || {},
        });
    }
    let maxDistance;
    try {
        maxDistance = parseNonNegativeNumber(params?.max_distance, 45, "max_distance");
    } catch (err) {
        fail(action, "invalid_params", {
            reason: err.message,
            target,
            params: params || {},
        });
    }
    const distance = distanceToTarget(bot, target);
    const block = blockAt(bot, target);
    if (distanceCheckEnabled(params) && distance > maxDistance) {
        fail(action, "block_out_of_range", {
            target,
            max_distance: maxDistance,
            distance,
            block: blockToPlain(bot, block),
        });
    }
    if (!block) {
        fail(action, "block_not_loaded", {
            target,
            max_distance: maxDistance,
            distance,
        });
    }
    return {
        ok: true,
        target,
        max_distance: maxDistance,
        distance,
        block: blockToPlain(bot, block),
        is_air: isAirBlock(block),
    };
}

async function breakBlock(ctx, params, action) {
    const bot = ctx.bot;
    let target;
    try {
        target = positionFromParams(params || {});
    } catch (err) {
        fail(action, "invalid_position", {
            reason: err.message,
            params: params || {},
        });
    }
    let maxDistance;
    let approachRange;
    let timeoutMs;
    try {
        maxDistance = parseNonNegativeNumber(params?.max_distance, 45, "max_distance");
        approachRange = parseNonNegativeNumber(params?.approach_range, 3, "approach_range");
        timeoutMs = parsePositiveInteger(params?.timeout_ms, 20000, "timeout_ms");
    } catch (err) {
        fail(action, "invalid_params", {
            reason: err.message,
            target,
            params: params || {},
            ...inventorySnapshot(bot),
        });
    }
    const collectDrops = params?.collect_drops !== false;

    let targetBlock = blockAt(bot, target);
    const distance = distanceToTarget(bot, target);
    if (distanceCheckEnabled(params) && distance > maxDistance) {
        fail(action, "block_out_of_range", {
            target,
            max_distance: maxDistance,
            distance,
            block: blockToPlain(bot, targetBlock),
            ...inventorySnapshot(bot),
        });
    }
    if (!targetBlock) {
        fail(action, "block_not_loaded", {
            target,
            max_distance: maxDistance,
            distance,
            ...inventorySnapshot(bot),
        });
    }
    if (isAirBlock(targetBlock)) {
        fail(action, "target_is_air", targetBaseResult(bot, target, targetBlock));
    }
    if (targetBlock.diggable === false) {
        fail(action, "block_not_diggable", targetBaseResult(bot, target, targetBlock));
    }

    const before = blockToPlain(bot, targetBlock);
    try {
        await approachTarget(bot, target, approachRange, timeoutMs);
        targetBlock = blockAt(bot, target);
        if (!targetBlock) {
            fail(action, "block_not_loaded", {
                target,
                before_block: before,
                ...inventorySnapshot(bot),
            });
        }
        if (isAirBlock(targetBlock)) {
            fail(action, "target_became_air", {
                target,
                broken_block: before,
                block_after: blockToPlain(bot, targetBlock),
                ...inventorySnapshot(bot),
            });
        }
        if (targetBlock.diggable === false || (typeof bot.canDigBlock === "function" && !bot.canDigBlock(targetBlock))) {
            fail(action, "block_not_diggable", {
                target,
                block: blockToPlain(bot, targetBlock),
                ...inventorySnapshot(bot),
            });
        }
        if (collectDrops) {
            if (!bot.collectBlock) {
                fail(action, "collect_plugin_missing", {
                    target,
                    reason: "mineflayer-collectblock is not loaded",
                    ...inventorySnapshot(bot),
                });
            }
            await withTimeout(
                bot.collectBlock.collect([targetBlock], { ignoreNoPath: true, count: 1 }),
                timeoutMs,
                "collect"
            );
            const dug = blockAt(bot, target);
            if (!dug || !isAirBlock(dug)) {
                fail(action, "mining_tool_required", {
                    target,
                    block: blockToPlain(bot, dug),
                    reason: "no suitable mining tool for this block",
                    ...inventorySnapshot(bot),
                });
            }
        } else {
            await withTimeout(bot.dig(targetBlock, "raycast"), timeoutMs, "dig");
        }
        await waitForTicksOrSleep(bot, 4);
    } catch (err) {
        if (err.code) throw err;
        fail(action, "dig_failed", {
            target,
            broken_block: before,
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }

    const after = blockAt(bot, target);
    let collection = { collection_supported: false };
    if (collectDrops) {
        collection = {
            collection_supported: true,
            collected_count: null,
            collected_items: [],
        };
    }
    return {
        ok: true,
        target,
        max_distance: maxDistance,
        approach_range: approachRange,
        collect_drops: collectDrops,
        ...collection,
        broken_block: before,
        block_after: blockToPlain(bot, after),
        ...inventorySnapshot(bot),
    };
}

async function placeBlock(ctx, params, action) {
    const bot = ctx.bot;
    let target;
    try {
        target = positionFromParams(params || {});
    } catch (err) {
        fail(action, "invalid_position", {
            reason: err.message,
            params: params || {},
        });
    }

    let itemName;
    let maxDistance;
    let approachRange;
    let timeoutMs;
    try {
        itemName = normalizeName(params?.item, "item");
        maxDistance = parseNonNegativeNumber(params?.max_distance, 45, "max_distance");
        approachRange = parseNonNegativeNumber(params?.approach_range, 3, "approach_range");
        timeoutMs = parsePositiveInteger(params?.timeout_ms, 20000, "timeout_ms");
    } catch (err) {
        fail(action, "invalid_params", {
            reason: err.message,
            target,
            params: params || {},
            ...inventorySnapshot(bot),
        });
    }
    const distance = distanceToTarget(bot, target);

    const resolvedItem = resolveItem(bot, itemName);
    if (!resolvedItem.item) {
        fail(action, "unknown_item", {
            target,
            item: itemName,
            reason: `No item named ${itemName}`,
            ...inventorySnapshot(bot),
        });
    }
    const resolvedBlock = resolveBlock(bot, itemName);
    if (!resolvedBlock.block) {
        fail(action, "item_not_placeable", {
            target,
            item: itemName,
            reason: `${itemName} is not known as a placeable block`,
            ...inventorySnapshot(bot),
        });
    }

    const inventoryItem = bot.inventory.findInventoryItem(resolvedItem.item.id, null);
    if (!inventoryItem) {
        fail(action, "inventory_item_missing", {
            target,
            item: itemName,
            missing: missingItem(itemName, 1, 0),
            ...inventorySnapshot(bot),
        });
    }

    let targetBlock = blockAt(bot, target);
    if (distanceCheckEnabled(params) && distance > maxDistance) {
        fail(action, "block_out_of_range", {
            target,
            item: itemName,
            max_distance: maxDistance,
            distance,
            target_block_before: blockToPlain(bot, targetBlock),
            ...inventorySnapshot(bot),
        });
    }
    if (!targetBlock) {
        fail(action, "block_not_loaded", {
            target,
            item: itemName,
            distance,
            ...inventorySnapshot(bot),
        });
    }
    if (!isAirBlock(targetBlock)) {
        fail(action, "target_occupied", {
            target,
            item: itemName,
            target_block_before: blockToPlain(bot, targetBlock),
            ...inventorySnapshot(bot),
        });
    }

    let reference = findReferenceBlock(bot, target);
    if (!reference) {
        fail(action, "no_reference_block", {
            target,
            item: itemName,
            target_block_before: blockToPlain(bot, targetBlock),
            ...inventorySnapshot(bot),
        });
    }

    const before = blockToPlain(bot, targetBlock);
    try {
        await approachTarget(bot, target, approachRange, timeoutMs);
        targetBlock = blockAt(bot, target);
        if (!targetBlock) {
            fail(action, "block_not_loaded", {
                target,
                item: itemName,
                target_block_before: before,
                ...inventorySnapshot(bot),
            });
        }
        if (!isAirBlock(targetBlock)) {
            fail(action, "target_occupied", {
                target,
                item: itemName,
                target_block_before: blockToPlain(bot, targetBlock),
                ...inventorySnapshot(bot),
            });
        }
        reference = findReferenceBlock(bot, target);
        if (!reference) {
            fail(action, "no_reference_block", {
                target,
                item: itemName,
                target_block_before: before,
                ...inventorySnapshot(bot),
            });
        }
        const latestItem = bot.inventory.findInventoryItem(resolvedItem.item.id, null);
        if (!latestItem) {
            fail(action, "inventory_item_missing", {
                target,
                item: itemName,
                missing: missingItem(itemName, 1, 0),
                target_block_before: before,
                ...inventorySnapshot(bot),
            });
        }
        await withTimeout(bot.equip(latestItem, "hand"), timeoutMs, "equip");
        await withTimeout(bot.placeBlock(reference.referenceBlock, reference.faceVector), timeoutMs, "placeBlock");
        await waitForTicksOrSleep(bot, 4);
    } catch (err) {
        if (err.code) throw err;
        fail(action, "place_failed", {
            target,
            item: itemName,
            target_block_before: before,
            reference_block: reference ? blockToPlain(bot, reference.referenceBlock) : null,
            reference_face: reference ? positionToPlain(reference.faceVector) : null,
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }

    const after = blockAt(bot, target);
    return {
        ok: true,
        target,
        max_distance: maxDistance,
        approach_range: approachRange,
        placed_item: itemToPlain(inventoryItem),
        target_block_before: before,
        target_block_after: blockToPlain(bot, after),
        reference_block: reference ? blockToPlain(bot, reference.referenceBlock) : null,
        reference_face: reference ? positionToPlain(reference.faceVector) : null,
        ...inventorySnapshot(bot),
    };
}

// ---------------------------------------------------------------------------
// block.mine：按坐标组连续挖掘（可中断、可跳过、可破障）
// ---------------------------------------------------------------------------

const MINE_FACE_OFFSETS = [
    { x: 1, y: 0, z: 0 },
    { x: -1, y: 0, z: 0 },
    { x: 0, y: 1, z: 0 },
    { x: 0, y: -1, z: 0 },
    { x: 0, y: 0, z: 1 },
    { x: 0, y: 0, z: -1 },
];

function minePositionKey(position) {
    return `${Math.floor(position.x)},${Math.floor(position.y)},${Math.floor(position.z)}`;
}

function findConnectedGroup(bot, seed, blockName, maxDistance, maxGroupSize) {
    const start = { x: Math.floor(seed.x), y: Math.floor(seed.y), z: Math.floor(seed.z) };
    const queue = [start];
    const seen = new Set([minePositionKey(start)]);
    const group = [];
    while (queue.length > 0 && group.length < maxGroupSize) {
        const pos = queue.shift();
        const block = blockAt(bot, pos);
        if (!block || block.name !== blockName) continue;
        if (distanceToTarget(bot, pos) > maxDistance) continue;
        group.push(pos);
        for (const offset of MINE_FACE_OFFSETS) {
            const next = { x: pos.x + offset.x, y: pos.y + offset.y, z: pos.z + offset.z };
            const key = minePositionKey(next);
            if (seen.has(key)) continue;
            const neighbor = blockAt(bot, next);
            if (neighbor && neighbor.name === blockName && distanceToTarget(bot, next) <= maxDistance) {
                seen.add(key);
                queue.push(next);
            }
        }
    }
    return group;
}

function findCoverBlock(bot, targetPos, blockName) {
    let best = null;
    let bestDistance = Infinity;
    for (const offset of MINE_FACE_OFFSETS) {
        const pos = { x: targetPos.x + offset.x, y: targetPos.y + offset.y, z: targetPos.z + offset.z };
        const block = blockAt(bot, pos);
        if (!block || isAirBlock(block)) continue;
        if (block.name === blockName) continue;
        if (DEFAULT_PROTECTED_BLOCK_NAMES.has(normalizeName(block.name))) continue;
        if (block.diggable === false) continue;
        const distance = distanceToTarget(bot, pos);
        if (distance < bestDistance) {
            bestDistance = distance;
            best = pos;
        }
    }
    return best;
}

async function digMineTarget(bot, target, opts) {
    await approachTarget(bot, target, opts.approachRange, opts.timeoutMs);
    const current = blockAt(bot, target);
    if (!current || isAirBlock(current)) {
        throw new Error("target_missing");
    }
    await withTimeout(bot.dig(current, "raycast"), opts.timeoutMs, "mine-dig");
    await waitForTicksOrSleep(bot, 4);
    if (opts.collectDrops) {
        await collectDroppedItems(bot, target, Math.min(opts.timeoutMs, 15000), opts.action);
    }
}

async function mineBlockWithObstacles(bot, blockPos, opts, action) {
    let obstaclesBroken = 0;
    for (let attempt = 0; attempt <= opts.maxObstaclesPerBlock; attempt++) {
        if (action.runtimeState.done) {
            return { mined: false, skipped: false, obstacles_broken: obstaclesBroken };
        }
        const current = blockAt(bot, blockPos);
        if (!current || current.name !== opts.blockName) {
            return { mined: false, skipped: true, obstacles_broken: obstaclesBroken };
        }
        try {
            await digMineTarget(bot, blockPos, opts);
            return { mined: true, skipped: false, obstacles_broken: obstaclesBroken };
        } catch (err) {
            if (bot.pathfinder) bot.pathfinder.setGoal(null);
            bot.clearControlStates();
            if (action.runtimeState.done) {
                return { mined: false, skipped: false, obstacles_broken: obstaclesBroken };
            }
            const cover = findCoverBlock(bot, blockPos, opts.blockName);
            if (!cover) {
                return { mined: false, skipped: true, obstacles_broken: obstaclesBroken };
            }
            try {
                await digMineTarget(bot, cover, { ...opts, collectDrops: false });
                obstaclesBroken += 1;
            } catch (err2) {
                if (bot.pathfinder) bot.pathfinder.setGoal(null);
                bot.clearControlStates();
                return { mined: false, skipped: true, obstacles_broken: obstaclesBroken };
            }
        }
    }
    return { mined: false, skipped: true, obstacles_broken: obstaclesBroken };
}

async function mineGroupOnce(bot, seed, opts, action, onProgress) {
    const group = findConnectedGroup(bot, seed, opts.blockName, opts.maxDistance, opts.maxGroupSize);
    const result = {
        group_size: group.length,
        mined_count: 0,
        skipped_count: 0,
        obstacles_broken: 0,
    };
    for (const pos of group) {
        if (action.runtimeState.done) break;
        const current = blockAt(bot, pos);
        if (!current || current.name !== opts.blockName) {
            result.skipped_count += 1;
            continue;
        }
        const outcome = await mineBlockWithObstacles(bot, pos, opts, action);
        result.mined_count += outcome.mined ? 1 : 0;
        result.skipped_count += outcome.skipped ? 1 : 0;
        result.obstacles_broken += outcome.obstacles_broken;
        if (onProgress) await onProgress(result);
        if (action.runtimeState.done) break;
    }
    return result;
}

async function startMineWorker(bot, action) {
    const seed = action.runtimeState.seed;
    const seedBlock = blockAt(bot, seed);
    if (!seedBlock || isAirBlock(seedBlock)) {
        action.status = "failed";
        action.error = "block_not_found";
        action.result = {
            ok: false,
            error_code: "block_not_found",
            target: seed,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }
    action.runtimeState.blockName = seedBlock.name;
    action.runtimeState.progress = {
        group_size: 0,
        mined_count: 0,
        skipped_count: 0,
        obstacles_broken: 0,
    };
    const opts = {
        blockName: seedBlock.name,
        approachRange: parseNonNegativeNumber(action.params?.approach_range, 3, "approach_range"),
        timeoutMs: parsePositiveInteger(action.params?.per_block_timeout_ms, 20000, "per_block_timeout_ms"),
        maxDistance: parseNonNegativeNumber(action.params?.max_distance, 45, "max_distance"),
        maxGroupSize: parsePositiveInteger(action.params?.max_group_size, 16, "max_group_size"),
        maxObstaclesPerBlock: parsePositiveInteger(
            action.params?.max_obstacles_per_block,
            8,
            "max_obstacles_per_block"
        ),
        collectDrops: action.params?.collect_drops !== false,
        action,
    };
    await mineGroupOnce(bot, seed, opts, action, (progress) => {
        action.runtimeState.progress = { ...progress };
        action.result = {
            ok: true,
            mining: true,
            block_name: action.runtimeState.blockName,
            ...progress,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    });
    if (action.runtimeState.done) {
        // 由 runtime.stop 置为 interrupted，这里只保留结果不覆盖状态
        action.result = {
            ok: true,
            mining: false,
            block_name: action.runtimeState.blockName,
            reason: "stopped",
            ...action.runtimeState.progress,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } else {
        action.status = "completed";
        action.result = {
            ok: true,
            mining: false,
            block_name: action.runtimeState.blockName,
            reason: "group_mined",
            ...action.runtimeState.progress,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        if (bot.pathfinder) bot.pathfinder.setGoal(null);
        bot.clearControlStates();
    }
}

async function mineStart(ctx, params, action) {
    const bot = ctx.bot;
    let seed;
    try {
        seed = positionFromParams(params || {});
    } catch (err) {
        fail(action, "invalid_position", {
            ok: false,
            error_code: "invalid_position",
            reason: err.message,
            params: params || {},
        });
    }
    const seedBlock = blockAt(bot, seed);
    if (!seedBlock || isAirBlock(seedBlock)) {
        fail(action, "block_not_found", {
            ok: false,
            error_code: "block_not_found",
            target: seed,
        });
    }
    action.runtimeState.seed = seed;
    action.runtimeState.done = false;
    action.runtimeState.worker = startMineWorker(bot, action);
    const base = {
        ok: true,
        mining: true,
        block_name: seedBlock.name,
        seed,
        mined_count: 0,
    };
    return action.status !== "running" && action.result ? action.result : base;
}

async function mineUpdate(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    return action.result || { ok: true, updated: true, mining: true };
}

async function mineStop(ctx, reason, action) {
    action.runtimeState.done = true;
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    const progress = action.runtimeState.progress || {};
    return {
        ok: true,
        stopped: true,
        reason,
        block_name: action.runtimeState.blockName || null,
        ...progress,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function mineStatus(ctx, action) {
    return {
        ok: true,
        status: action.status,
        block_name: action.runtimeState.blockName || null,
        result: action.result || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

module.exports = {
    name: "block",
    commands: {
        inspect: { execute: inspectBlock },
        "break": { execute: breakBlock },
        place: { execute: placeBlock },
        inspect_region: { execute: inspectRegion },
        break_region: { execute: breakRegion },
        place_region: { execute: placeRegion },
        mine: { start: mineStart, update: mineUpdate, stop: mineStop, status: mineStatus },
    },
    mineService: {
        findConnectedGroup,
        mineGroupOnce,
        mineBlockWithObstacles,
    },
};
