"use strict";

const blockSkill = require("../../block/skill.js");

const TIER_ORDER = ["wooden", "stone", "iron", "diamond", "netherite"];
const MINE_MINERALS = ["lapis", "iron", "gold", "emerald", "diamond", "netherite"];
const TARGET_NOT_FOUND_ERROR = "目标未找到";
const THREE_DIMENSIONAL_NEIGHBOR_OFFSETS = [];
for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 1; dy++) {
        for (let dz = -1; dz <= 1; dz++) {
            if (dx !== 0 || dy !== 0 || dz !== 0) {
                THREE_DIMENSIONAL_NEIGHBOR_OFFSETS.push({ x: dx, y: dy, z: dz });
            }
        }
    }
}

function normalizeName(value, field = "ore") {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function parsePositiveInteger(value, fallback, field) {
    const parsed = value === undefined || value === null ? Number(fallback) : Math.trunc(Number(value));
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

function positionFromParam(value) {
    if (!value || typeof value !== "object") return null;
    const x = Number(value.x);
    const y = Number(value.y);
    const z = Number(value.z);
    if (![x, y, z].every(Number.isFinite)) return null;
    return { x: Math.floor(x), y: Math.floor(y), z: Math.floor(z) };
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

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

function positionDistance(bot, position) {
    return Math.hypot(
        position.x + 0.5 - bot.entity.position.x,
        position.y + 0.5 - bot.entity.position.y,
        position.z + 0.5 - bot.entity.position.z,
    );
}

function failTargetNotFound(action, target, distance) {
    action.result = {
        ok: false,
        error_code: "target_not_found",
        reason: TARGET_NOT_FOUND_ERROR,
        target,
        distance,
    };
    const err = new Error(TARGET_NOT_FOUND_ERROR);
    err.code = "target_not_found";
    throw err;
}

// ---------------------------------------------------------------------------
// 矿物配置（env MINING_ORE_BLOCKS：逗号分隔 minecraft:block，如 iron_block,iron_ore）
// ---------------------------------------------------------------------------

let oreFamiliesCache = null;

function oreConfig() {
    if (oreFamiliesCache) return oreFamiliesCache;
    const raw = process.env.MINING_ORE_BLOCKS;
    if (!raw || !String(raw).trim()) return null;
    const families = new Map();
    for (const item of String(raw).split(",")) {
        const name = normalizeName(item);
        if (!name) continue;
        const mineral = name.replace(/_(block|ore|debris)$/, "");
        if (!families.has(mineral)) families.set(mineral, new Set());
        families.get(mineral).add(name);
    }
    oreFamiliesCache = families;
    return families;
}

function familyBlocks(mineral) {
    const families = oreConfig();
    return families && families.get(mineral) ? Array.from(families.get(mineral)) : null;
}

function tierOf(itemName) {
    const name = String(itemName || "").toLowerCase();
    for (const tier of TIER_ORDER) {
        if (name.includes(`${tier}_pickaxe`)) return tier;
    }
    return null;
}

function toolTypeOf(itemName) {
    const name = String(itemName || "").toLowerCase();
    for (const type of ["pickaxe", "axe", "shovel", "hoe"]) {
        if (name.includes(type)) return type;
    }
    return null;
}

function toolSatisfies(heldName, requiredName) {
    const held = String(heldName || "").toLowerCase();
    const required = String(requiredName || "").toLowerCase();
    const heldType = toolTypeOf(held);
    const requiredType = toolTypeOf(required);
    if (!heldType || heldType !== requiredType) return false;
    const heldTier = tierOf(held);
    const requiredTier = tierOf(required);
    if (!heldTier || !requiredTier) return true;
    return TIER_ORDER.indexOf(heldTier) >= TIER_ORDER.indexOf(requiredTier);
}

function canCollectWithHeldTool(bot, block) {
    const harvestTools = block && block.harvestTools ? block.harvestTools : {};
    const toolIds = Object.keys(harvestTools);
    if (toolIds.length === 0) return { ok: true };
    // harvestTools 为 {itemId: true} 映射（如 {742: true}），按注册表解析工具名
    const requiredNames = [];
    for (const idStr of toolIds) {
        const item = bot.registry.items ? bot.registry.items[Number(idStr)] : null;
        if (item && item.name) requiredNames.push(String(item.name));
    }
    if (requiredNames.length === 0) return { ok: true };
    const held = bot.heldItem;
    const heldName = held ? String(held.name || "") : "";
    if (!heldName) return { ok: false, required: requiredNames[0] };
    for (const requiredName of requiredNames) {
        if (toolSatisfies(heldName, requiredName)) return { ok: true };
    }
    return { ok: false, required: requiredNames[0] };
}

function parsePositions(params) {
    if (Array.isArray(params?.positions) && params.positions.length > 0) {
        return params.positions.map((value, index) => {
            const pos = positionFromParam(value);
            if (!pos) throw new Error(`positions[${index}] must contain x, y, z`);
            return pos;
        });
    }
    const single = positionFromParam(params || {});
    if (single) return [single];
    throw new Error("positions or x/y/z is required");
}

function findNearbyOreSeeds(bot, target, blocks) {
    const seeds = [];
    for (const offset of THREE_DIMENSIONAL_NEIGHBOR_OFFSETS) {
        const position = {
            x: target.x + offset.x,
            y: target.y + offset.y,
            z: target.z + offset.z,
        };
        const block = bot.blockAt(vecFromPosition(bot, position));
        const blockName = block ? normalizeName(block.name || "") : "";
        if (block && blocks.includes(blockName)) seeds.push(position);
    }
    return seeds;
}

function positionKey(position) {
    return `${position.x},${position.y},${position.z}`;
}

// ---------------------------------------------------------------------------
// mine_<矿物>：continuous，逐坐标 pre-check + 工具校验 + 调用 block.mine 核心
// ---------------------------------------------------------------------------

function publishOreProgress(action, bot) {
    const p = action.runtimeState.progress;
    action.result = {
        ok: true,
        mining: true,
        mineral: action.runtimeState.mineral,
        mined_count: p.mined_count,
        skipped_count: p.skipped_count,
        obstacles_broken: p.obstacles_broken,
        groups_mined: p.groups_mined,
        skipped_coords: p.skipped_coords.slice(-20),
        tool_required_coords: p.tool_required_coords.slice(-20),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

function finalizeOre(bot, action, reason, failed = false, error = null) {
    action.status = failed ? "failed" : "completed";
    if (failed) action.error = error || "mine_failed";
    const p = action.runtimeState.progress;
    action.result = {
        ok: !failed,
        mining: false,
        mineral: action.runtimeState.mineral,
        reason,
        mined_count: p.mined_count,
        skipped_count: p.skipped_count,
        obstacles_broken: p.obstacles_broken,
        groups_mined: p.groups_mined,
        skipped_coords: p.skipped_coords.slice(-20),
        tool_required_coords: p.tool_required_coords.slice(-20),
        ...(failed ? { error_code: action.error } : {}),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
}

async function runMineOreWorker(bot, action, opts) {
    const params = action.params;
    const mineOpts = {
        approachRange: parseNonNegativeNumber(params?.approach_range, 3, "approach_range"),
        timeoutMs: parsePositiveInteger(params?.per_block_timeout_ms, 20000, "per_block_timeout_ms"),
        maxDistance: opts.maxDistance,
        maxGroupSize: parsePositiveInteger(params?.max_group_size, 16, "max_group_size"),
        maxObstaclesPerBlock: parsePositiveInteger(
            params?.max_obstacles_per_block,
            8,
            "max_obstacles_per_block"
        ),
        // Mining progress is inventory-based, so task mining always collects drops.
        collectDrops: true,
        action,
    };

    for (const pos of action.runtimeState.positions) {
        if (action.runtimeState.done) break;
        const block = bot.blockAt(vecFromPosition(bot, pos));
        const name = block ? normalizeName(block.name || "") : null;
        if (!block || !action.runtimeState.blocks.includes(name)) {
            action.runtimeState.progress.skipped_coords.push({ position: pos, reason: "not_ore" });
            continue;
        }
        const tool = canCollectWithHeldTool(bot, block);
        if (!tool.ok) {
            action.runtimeState.progress.tool_required_coords.push({
                position: pos,
                required: tool.required,
            });
            if (opts.strict) {
                finalizeOre(bot, action, "tool_required", true, "tool_required");
                return;
            }
            continue;
        }
        const groupResult = await blockSkill.mineService.mineGroupOnce(
            bot,
            pos,
            { ...mineOpts, blockName: block.name },
            action,
            () => publishOreProgress(action, bot)
        );
        action.runtimeState.progress.mined_count += groupResult.mined_count;
        action.runtimeState.progress.skipped_count += groupResult.skipped_count;
        action.runtimeState.progress.obstacles_broken += groupResult.obstacles_broken;
        action.runtimeState.progress.groups_mined += 1;
        publishOreProgress(action, bot);
    }

    if (action.runtimeState.done) {
        publishOreProgress(action, bot);
        action.result = {
            ...action.result,
            mining: false,
            reason: "stopped",
        };
        action.updatedAt = Date.now();
    } else {
        finalizeOre(bot, action, "all_groups_mined");
    }
}

async function startMineOre(ctx, params, action, mineral) {
    const bot = ctx.bot;
    if (!oreConfig()) {
        fail(action, "mining_config_missing", {
            ok: false,
            error_code: "mining_config_missing",
            reason: "MINING_ORE_BLOCKS is not configured",
            mineral,
        });
    }
    const blocks = familyBlocks(mineral);
    if (!blocks) {
        fail(action, "ore_not_in_task", {
            ok: false,
            error_code: "ore_not_in_task",
            mineral,
        });
    }
    let positions;
    try {
        positions = parsePositions(params);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    const resolvedPositions = [];
    const resolvedKeys = new Set();
    for (const target of positions) {
        const distance = positionDistance(bot, target);
        const block = bot.blockAt(vecFromPosition(bot, target));
        const blockName = block ? normalizeName(block.name || "") : "";
        const seeds = block && blocks.includes(blockName)
            ? [target]
            : findNearbyOreSeeds(bot, target, blocks);
        if (seeds.length === 0) {
            failTargetNotFound(action, target, distance);
        }
        for (const seed of seeds) {
            const key = positionKey(seed);
            if (!resolvedKeys.has(key)) {
                resolvedKeys.add(key);
                resolvedPositions.push(seed);
            }
        }
    }
    action.runtimeState.mineral = mineral;
    action.runtimeState.blocks = blocks;
    action.runtimeState.positions = resolvedPositions;
    action.runtimeState.done = false;
    action.runtimeState.progress = {
        mined_count: 0,
        skipped_count: 0,
        obstacles_broken: 0,
        groups_mined: 0,
        skipped_coords: [],
        tool_required_coords: [],
    };
    action.runtimeState.worker = runMineOreWorker(bot, action, {
        strict: params?.strict === true,
        maxDistance: Infinity,
    });
    const base = {
        ok: true,
        mining: true,
        mineral,
        positions: resolvedPositions.length,
        mined_count: 0,
    };
    // worker 若同步完成（如全部坐标被跳过），直接返回其最终结果，避免被 start 返回值覆盖
    return action.status !== "running" && action.result ? action.result : base;
}

async function updateMineOre(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    if (Array.isArray(patch?.positions)) {
        return {
            ok: true,
            updated: true,
            note: "positions 变更请 stop 后重新 start",
        };
    }
    return action.result || { ok: true, updated: true, mining: true };
}

async function stopMineOre(ctx, reason, action) {
    action.runtimeState.done = true;
    if (ctx.bot.pathfinder) ctx.bot.pathfinder.setGoal(null);
    ctx.bot.clearControlStates();
    const p = action.runtimeState.progress || {};
    return {
        ok: true,
        stopped: true,
        reason,
        mineral: action.runtimeState.mineral || null,
        mined_count: p.mined_count || 0,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function statusMineOre(ctx, action) {
    return {
        ok: true,
        status: action.status,
        mineral: action.runtimeState.mineral || null,
        result: action.result || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function buildMiningCommands() {
    const commands = {};
    for (const mineral of MINE_MINERALS) {
        commands[`mine_${mineral}`] = {
            start: (ctx, params, action) => startMineOre(ctx, params, action, mineral),
            update: updateMineOre,
            stop: stopMineOre,
            status: statusMineOre,
        };
    }
    return commands;
}

module.exports = {
    name: "mining",
    commands: buildMiningCommands(),
};
