"use strict";

const { DEFAULT_PERCEPTION_RADIUS } = require("../../env/mineflayer/perception_config");
const equipmentRegistry = require("../../env/mineflayer/equipment_registry");
const {
    consumeWithRetry,
    findConsumableItem,
    normalizeConsumableName,
} = require("../consumable_utils");

const HOSTILE_NAMES = new Set([
    "blaze", "cave_spider", "creeper", "drowned", "elder_guardian", "enderman",
    "endermite", "evoker", "ghast", "guardian", "hoglin", "husk", "magma_cube",
    "phantom", "piglin_brute", "pillager", "ravager", "shulker", "silverfish",
    "skeleton", "slime", "spider", "stray", "vex", "vindicator", "warden",
    "witch", "wither_skeleton", "zoglin", "zombie", "zombie_villager"
]);

const DEFAULT_PATROL_RADIUS = 45;
const DEFAULT_RETREAT_DISTANCE = 12;
const OBSERVER_USERNAME_PREFIXES = ["observer"];
const TARGET_OBSERVE_RANGE_ERROR = "目标未找到或位于观察范围之外，请先靠近目标";

function numberParam(params, name, fallback) {
    const value = params[name] ?? fallback;
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`${name} must be a finite number`);
    return number;
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
        ...equipmentRegistry.cached(item),
    };
}

function isHostile(entity) {
    const name = String(entity && (entity.name || entity.displayName) || "").toLowerCase();
    return Boolean(entity) && entity.isValid !== false && (entity.type === "hostile" || HOSTILE_NAMES.has(name));
}

function isObserverUsername(username) {
    const text = String(username || "").trim().toLowerCase();
    return Boolean(text) && OBSERVER_USERNAME_PREFIXES.some((prefix) => text.startsWith(prefix));
}

function playerGamemode(bot, username) {
    if (!username || !bot.players) return null;
    const player = bot.players[username];
    if (!player) return null;
    return player.gamemode ?? player.gameMode ?? player.game_mode ?? null;
}

function isSpectatorGamemode(value) {
    return value === 3 || String(value || "").toLowerCase() === "spectator";
}

function isObservableAgent(bot, entity) {
    if (!entity || entity.isValid === false || entity === bot.entity) return false;
    if (entity.type !== "player") return false;
    const username = entity.username || entity.name || null;
    if (isObserverUsername(username)) return false;
    return !isSpectatorGamemode(playerGamemode(bot, username));
}

function entityToPlain(bot, entity) {
    if (!entity) return null;
    return {
        id: entity.id ?? null,
        uuid: entity.uuid ?? null,
        type: entity.type ?? null,
        name: entity.name ?? null,
        username: entity.username ?? null,
        displayName: entity.displayName ?? null,
        kind: entity.kind ?? null,
        // prismarine-entity's mobType getter emits a stack trace on every access.
        mobType: entity.displayName ?? null,
        position: positionToPlain(entity.position),
        distance: bot.entity && bot.entity.position && entity.position
            ? bot.entity.position.distanceTo(entity.position)
            : null,
        health: entity.health ?? null,
        height: entity.height ?? null,
        width: entity.width ?? null,
        isValid: entity.isValid ?? null,
    };
}

function normalizeEntityName(value) {
    return String(value || "").trim().toLowerCase()
        .replace(/^minecraft:/, "")
        .replace(/\s+/g, "_");
}

function entityMatchesName(entity, wanted) {
    return [
        entity.name,
        entity.username,
        entity.displayName,
    ].some((value) => normalizeEntityName(value) === wanted);
}

function findPlayerEntityByName(bot, rawName) {
    const wantedRaw = String(rawName || "").trim();
    if (!wantedRaw) return null;
    const exactPlayer = bot.players && bot.players[wantedRaw] ? bot.players[wantedRaw].entity : null;
    if (exactPlayer && exactPlayer !== bot.entity) return exactPlayer;
    const wanted = normalizeEntityName(wantedRaw);
    const namedPlayer = Object.entries(bot.players || {}).find(([username, player]) => {
        if (!player || !player.entity || player.entity === bot.entity) return false;
        return entityMatchesName(player.entity, wanted)
            || normalizeEntityName(username) === wanted
            || normalizeEntityName(player.displayName) === wanted;
    });
    return namedPlayer ? namedPlayer[1].entity : null;
}

function isUsableEntity(entity) {
    return Boolean(entity && entity.isValid !== false);
}

function parseEntityShorthand(value) {
    const normalized = normalizeEntityName(value);
    const match = normalized.match(/^(.+)_(\d+)$/);
    if (!match) return null;
    return { name: match[1], id: Number(match[2]) };
}

function findEntityByNumericId(bot, rawId) {
    let id = null;
    if (typeof rawId === "number" && Number.isInteger(rawId)) {
        id = rawId;
    } else if (typeof rawId === "string" && /^\d+$/.test(rawId.trim())) {
        id = Number(rawId.trim());
    }
    if (id === null || !Number.isSafeInteger(id)) return null;
    const entity = bot.entities[id] || bot.entities[String(id)] || null;
    return isUsableEntity(entity) ? entity : null;
}

function findNearestEntityByName(bot, rawName) {
    const wanted = normalizeEntityName(rawName);
    if (!wanted) return null;
    const predicate = (entity) => (
        isUsableEntity(entity)
        && entity.position
        && entityMatchesName(entity, wanted)
    );
    if (typeof bot.nearestEntity === "function") {
        return bot.nearestEntity(predicate) || null;
    }
    return Object.values(bot.entities || {}).find(predicate) || null;
}

function resolveEntityReference(bot, reference, { strictShorthand = false } = {}) {
    if (reference === undefined || reference === null) return null;
    const shorthand = parseEntityShorthand(reference);
    if (shorthand) {
        const entity = findEntityByNumericId(bot, shorthand.id);
        if (entity && entityMatchesName(entity, shorthand.name)) return entity;
        if (strictShorthand) return null;
        const namedEntity = findNearestEntityByName(bot, shorthand.name);
        if (namedEntity) return namedEntity;
        return findPlayerEntityByName(bot, shorthand.name);
    }

    const entity = findEntityByNumericId(bot, reference);
    if (entity) return entity;
    return findNearestEntityByName(bot, reference)
        || findPlayerEntityByName(bot, reference);
}

function playerNameFromTarget(target) {
    return target.username || target.player_name || target.playerName || (
        target.type === "player" ? target.name : null
    );
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

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

function observeRadius(ctx) {
    const base = Number.isFinite(ctx.perceptionRadius) ? ctx.perceptionRadius : DEFAULT_PERCEPTION_RADIUS;
    return base * 2;
}

function isWithinObserveRange(ctx, entity) {
    return Boolean(
        entity
        && entity.position
        && ctx.bot.entity
        && ctx.bot.entity.position
        && entity.position.distanceTo(ctx.bot.entity.position) <= observeRadius(ctx)
    );
}

function failTargetOutsideObserveRange(action, requestedTarget, target, ctx, extra = {}) {
    action.result = {
        ok: false,
        error_code: "target_not_found_or_outside_observe_range",
        reason: TARGET_OBSERVE_RANGE_ERROR,
        requested_target: requestedTarget ?? null,
        target: target ? entityToPlain(ctx.bot, target) : null,
        observe_radius: observeRadius(ctx),
        ...extra,
    };
    const err = new Error(TARGET_OBSERVE_RANGE_ERROR);
    err.code = "target_not_found_or_outside_observe_range";
    throw err;
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

function getEntityByTarget(bot, target) {
    if (!target) return null;
    if (typeof target === "string") {
        return resolveEntityReference(bot, target.trim());
    }
    if (typeof target !== "object") return null;

    const nameReference = target.name || target.type;
    const entityIdReference = target.entity_id;
    const hasCompoundReference = Boolean(
        parseEntityShorthand(nameReference) || parseEntityShorthand(entityIdReference),
    );

    // 对模型误生成的 {name: "zombie_8", entity_id: "zombie_8"}，按 name 后 entity_id 尝试。
    if (hasCompoundReference) {
        const byName = resolveEntityReference(bot, nameReference, { strictShorthand: true });
        if (byName) return byName;
        const byEntityId = resolveEntityReference(bot, entityIdReference, { strictShorthand: true });
        if (byEntityId) return byEntityId;
    }

    if (entityIdReference !== undefined && entityIdReference !== null) {
        const entity = findEntityByNumericId(bot, entityIdReference);
        if (entity) return entity;
    }
    if (target.id !== undefined && target.id !== null) {
        const entity = findEntityByNumericId(bot, target.id);
        if (entity) return entity;
    }
    if (target.uuid) {
        return Object.values(bot.entities || {}).find(
            (entity) => isUsableEntity(entity) && entity.uuid === target.uuid,
        ) || null;
    }
    const playerName = playerNameFromTarget(target);
    if (playerName) {
        const player = findPlayerEntityByName(bot, playerName);
        if (player) return player;
    }
    if (nameReference) {
        return resolveEntityReference(bot, nameReference, { strictShorthand: hasCompoundReference });
    }
    return null;
}

function stopCombat(bot, action) {
    if (action.runtimeState.interval) {
        clearInterval(action.runtimeState.interval);
        action.runtimeState.interval = null;
    }
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
}

async function safeDeactivateHeldItem(bot) {
    try {
        const result = bot.deactivateItem();
        if (result && typeof result.then === "function") {
            await result;
        }
    } catch (_) {}
}

function stopHawkEyeAction(bot, action) {
    const state = action && action.runtimeState ? action.runtimeState : null;
    if (!state || (!state.hawkActive && !state.onShotStopped)) return;

    state.hawkActive = false;
    if (bot.hawkEye && typeof bot.hawkEye.stop === "function") {
        try {
            bot.hawkEye.stop();
        } catch (_) {}
    }
    if (state.onShotStopped && typeof bot.removeListener === "function") {
        bot.removeListener("auto_shot_stopped", state.onShotStopped);
    }
    state.onShotStopped = null;
}

// ---------------------------------------------------------------------------
// 掉落拾取（三档）与通用工具
// ---------------------------------------------------------------------------

const ANIMAL_NAMES = new Set([
    "cow", "mooshroom", "rabbit", "sheep", "chicken", "pig",
]);

const COLLECT_MODES = new Set(["none", "optional", "required"]);

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
    if (!position) return [];
    const center = bot.entity.position.offset(
        position.x + 0.5 - bot.entity.position.x,
        position.y + 0.5 - bot.entity.position.y,
        position.z + 0.5 - bot.entity.position.z,
    );
    return Object.values(bot.entities || {}).filter(
        (entity) => isItemEntity(entity) && entity.position.distanceTo(center) <= radius
    );
}

function resolveCollectMode(action) {
    const target = action.runtimeState.targetInfo || {};
    const name = String(target.name || target.type || "").toLowerCase();
    const isAnimal = target.type === "passive" || ANIMAL_NAMES.has(name);
    const raw = String(action.params?.collect_drops ?? "optional").toLowerCase();
    const mode = COLLECT_MODES.has(raw) ? raw : "optional";
    return isAnimal ? "required" : mode;
}

async function collectDropsForTarget(bot, position, mode) {
    if (!position) {
        return {
            collection_supported: false,
            collect_mode: mode,
            reason: "no_position",
            collected_count: 0,
            collected_items: [],
            failed_count: 0,
            failed: false,
        };
    }
    const items = findDroppedItemsNear(bot, position, 6);
    if (items.length === 0) {
        return {
            collection_supported: true,
            collect_mode: mode,
            reason: "no_drops",
            collected_count: 0,
            collected_items: [],
            failed_count: 0,
            failed: false,
        };
    }
    const deadline = Date.now() + 15000;
    let remaining = findDroppedItemsNear(bot, position, 6);
    let failures = 0;
    while (remaining.length > 0 && Date.now() < deadline) {
        const item = remaining[0];
        try {
            if (!bot.mcagentPathfinder || !bot.pathfinder) {
                failures += 1;
                break;
            }
            const { goals } = bot.mcagentPathfinder;
            const goal = new goals.GoalNear(item.position.x, item.position.y, item.position.z, 0.5);
            await withTimeout(bot.pathfinder.goto(goal), 8000, "collect-walk");
            bot.clearControlStates();
            await waitForTicksOrSleep(bot, 10);
        } catch (_) {
            failures += 1;
            if (bot.pathfinder) bot.pathfinder.setGoal(null);
            if (mode === "required") break;
        }
        remaining = findDroppedItemsNear(bot, position, 6);
    }
    const collectedCount = items.length - remaining.length;
    return {
        collection_supported: true,
        collect_mode: mode,
        collected_count: collectedCount,
        collected_items: items.map((entity) => {
            const dropped = typeof entity.getDroppedItem === "function" ? entity.getDroppedItem() : null;
            return itemToPlain(dropped);
        }),
        failed: remaining.length > 0,
        failed_count: remaining.length,
        failures,
    };
}

function onTargetRemoved(bot, action) {
    stopHawkEyeAction(bot, action);
    stopCombat(bot, action);
    const base = {
        ok: true,
        target_dead: true,
        reason: "target_removed",
        combat_type: action.runtimeState.combatType || null,
        weapon: action.runtimeState.weaponName || null,
        target: action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        shots_fired: action.runtimeState.shotsFired || 0,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        position: positionToPlain(bot.entity.position),
    };
    if (action.runtimeState.arrowName) {
        base.arrows_remaining = countMatchingItems(bot, action.runtimeState.arrowName);
    }
    if (action.runtimeState.weaponName && isThrowableWeapon(action.runtimeState.weaponName)) {
        base.ammo_remaining = countMatchingItems(bot, action.runtimeState.weaponName);
    }

    const collectMode = resolveCollectMode(action);
    const autoPickup = action.runtimeState.autoPickup === true;
    const taskCategory = action.runtimeState.taskCategory ?? null;
    const collectAllowed = collectMode === "required"
        || (collectMode !== "none" && (taskCategory !== "monster_clearance" || autoPickup));
    if (!collectAllowed) {
        action.status = "completed";
        action.result = base;
        action.updatedAt = Date.now();
        return;
    }

    const position = action.runtimeState.lastTargetPosition
        || (action.runtimeState.targetInfo && action.runtimeState.targetInfo.position);
    action.runtimeState.finalizing = true;
    collectDropsForTarget(bot, position, collectMode)
        .then((collection) => {
            action.result = { ...base, ...collection };
            action.status = collection.failed && collectMode === "required" ? "failed" : "completed";
            if (action.status === "failed") action.error = "collect_failed";
            action.updatedAt = Date.now();
        })
        .catch((err) => {
            action.result = {
                ...base,
                collection_failed: true,
                reason: err.message,
                collected_count: 0,
            };
            action.status = collectMode === "required" ? "failed" : "completed";
            if (action.status === "failed") action.error = "collect_failed";
            action.updatedAt = Date.now();
        });
}

function chestBlocks(bot, radius) {
    const ids = [];
    for (const name of ["chest", "trapped_chest", "barrel"]) {
        const block = bot.registry.blocksByName?.[name];
        if (block) ids.push(block.id);
    }
    if (ids.length === 0) return [];
    return bot.findBlocks({ matching: ids, maxDistance: radius, count: 64 })
        .map((position) => bot.blockAt(position))
        .filter(Boolean)
        .map((block) => blockToPlain(bot, block))
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
}

async function patrol(ctx) {
    const bot = ctx.bot;
    const radius = DEFAULT_PATROL_RADIUS;
    const hostiles = Object.values(bot.entities || {})
        .filter((entity) => isHostile(entity) && entity.position && entity.position.distanceTo(bot.entity.position) <= radius)
        .map((entity) => entityToPlain(bot, entity))
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return {
        ok: true,
        radius,
        hostiles,
        chests: chestBlocks(bot, radius),
        self: {
            position: positionToPlain(bot.entity.position),
            health: bot.health ?? null,
            food: bot.food ?? null,
            foodSaturation: bot.foodSaturation ?? null,
            heldItem: itemToPlain(bot.heldItem),
        },
    };
}

async function observe(ctx) {
    const bot = ctx.bot;
    const base = Number.isFinite(ctx.perceptionRadius) ? ctx.perceptionRadius : DEFAULT_PERCEPTION_RADIUS;
    const radius = observeRadius(ctx);
    const agents = Object.values(bot.entities || {})
        .filter((entity) => isObservableAgent(bot, entity) && entity.position && entity.position.distanceTo(bot.entity.position) <= radius)
        .map((entity) => entityToPlain(bot, entity))
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    const hostiles = Object.values(bot.entities || {})
        .filter((entity) => isHostile(entity) && entity.position && entity.position.distanceTo(bot.entity.position) <= radius)
        .map((entity) => entityToPlain(bot, entity))
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    const items = Object.values(bot.entities || {})
        .filter((entity) => isItemEntity(entity) && entity.position && entity.position.distanceTo(bot.entity.position) <= radius)
        .map((entity) => {
            const dropped = typeof entity.getDroppedItem === "function" ? entity.getDroppedItem() : null;
            return {
                ...(itemToPlain(dropped) || {}),
                position: positionToPlain(entity.position),
                distance: bot.entity.position.distanceTo(entity.position),
            };
        })
        .sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return {
        ok: true,
        base_radius: base,
        radius,
        agents,
        hostiles,
        items,
        self: {
            position: positionToPlain(bot.entity.position),
            health: bot.health ?? null,
            food: bot.food ?? null,
            foodSaturation: bot.foodSaturation ?? null,
            heldItem: itemToPlain(bot.heldItem),
        },
    };
}

async function startAttack(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    const target = getEntityByTarget(bot, params.target || {});
    if (!target) {
        fail(action, "target_not_found", {
            ok: false,
            error_code: "target_not_found",
            requested_target: params.target || null,
        });
    }
    action.runtimeState.targetId = target.id;
    action.runtimeState.targetUuid = target.uuid || null;
    action.runtimeState.targetInfo = entityToPlain(bot, target);
    action.runtimeState.attackCount = 0;
    action.runtimeState.lastAttackAt = 0;
    action.runtimeState.autoPickup = typeof ctx.getAutoPickup === "function" ? ctx.getAutoPickup() : false;
    action.runtimeState.taskCategory = typeof ctx.getTaskCategory === "function" ? ctx.getTaskCategory() : null;
    action.runtimeState.interval = setInterval(() => tickAttack(bot, action), 250);
    tickAttack(bot, action);
    return action.result || {
        ok: true,
        attacking: true,
        target: action.runtimeState.targetInfo,
        attack_count: 0,
    };
}

function tickAttack(bot, action) {
    if (action.runtimeState.finalizing) return;
    const target = bot.entities[action.runtimeState.targetId] || null;
    if (target) action.runtimeState.lastTargetPosition = target.position;
    if (!target || target.isValid === false) {
        onTargetRemoved(bot, action);
        return;
    }
    if (action.runtimeState.eatingFood) {
        return;
    }

    const followRange = numberParam(action.params, "follow_range", 2.5);
    const attackRange = numberParam(action.params, "attack_range", 3.1);
    const cooldown = numberParam(action.params, "attack_cooldown_ms", 700);
    const distance = target.position.distanceTo(bot.entity.position);

    try {
        if (distance > attackRange) {
            const { goals } = bot.mcagentPathfinder;
            bot.pathfinder.setGoal(new goals.GoalFollow(target, followRange), true);
        } else {
            if (bot.pathfinder) bot.pathfinder.setGoal(null);
            bot.lookAt(target.position.offset(0, target.height || 1, 0), true).catch(() => {});
            const now = Date.now();
            if (now - action.runtimeState.lastAttackAt >= cooldown) {
                bot.attack(target);
                action.runtimeState.lastAttackAt = now;
                action.runtimeState.attackCount += 1;
            }
        }
        action.result = {
            ok: true,
            attacking: distance <= attackRange,
            target_dead: false,
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            last_distance: distance,
            last_food_result: action.runtimeState.lastFoodResult || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } catch (err) {
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "attack_failed";
        action.result = {
            ok: false,
            error_code: "attack_failed",
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            last_food_result: action.runtimeState.lastFoodResult || null,
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

async function updateAttack(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    const foodResult = await maybeHandleCombatFoodUpdate(ctx, action, patch);
    if (foodResult) {
        return foodResult;
    }
    return action.result || { ok: true, updated: true };
}

async function stopAttack(ctx, reason, action) {
    stopCombat(ctx.bot, action);
    return {
        ok: true,
        stopped: true,
        reason,
        target: action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        last_food_result: action.runtimeState.lastFoodResult || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function attackStatus(ctx, action) {
    const target = ctx.bot.entities[action.runtimeState.targetId] || null;
    return {
        ok: true,
        status: action.status,
        target: entityToPlain(ctx.bot, target) || action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        last_food_result: action.runtimeState.lastFoodResult || null,
        result: action.result || null,
    };
}

// --- retreat handlers ---

function retreatResult(bot, action, overrides = {}) {
    const target = bot.entities[action.runtimeState.targetId] || null;
    return {
        ok: true,
        target: entityToPlain(bot, target) || action.runtimeState.targetInfo || null,
        requested_distance: action.runtimeState.requestedDistance,
        distance: action.runtimeState.lastDistance ?? null,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        position: positionToPlain(bot.entity.position),
        ...overrides,
    };
}

async function startRetreat(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);

    let requestedDistance;
    try {
        requestedDistance = numberParam(params, "distance", DEFAULT_RETREAT_DISTANCE);
        if (requestedDistance < 0) {
            throw new Error("distance must be greater than or equal to 0");
        }
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }

    const target = getEntityByTarget(bot, params.target || {});
    if (!target || target.isValid === false || !target.position) {
        fail(action, "target_not_found", {
            ok: false,
            error_code: "target_not_found",
            requested_target: params.target || null,
        });
    }

    action.runtimeState.targetId = target.id;
    action.runtimeState.targetInfo = entityToPlain(bot, target);
    action.runtimeState.requestedDistance = requestedDistance;
    action.runtimeState.lastDistance = target.position.distanceTo(bot.entity.position);
    action.runtimeState.startedAt = Date.now();

    if (action.runtimeState.lastDistance >= requestedDistance) {
        stopCombat(bot, action);
        action.status = "completed";
        action.result = retreatResult(bot, action, {
            retreated: true,
            reason: "distance_reached",
        });
        action.updatedAt = Date.now();
        return action.result;
    }

    action.runtimeState.interval = setInterval(() => tickRetreat(bot, action), 250);
    tickRetreat(bot, action);
    return action.result || retreatResult(bot, action, {
        retreating: true,
        reason: "retreating",
    });
}

function tickRetreat(bot, action) {
    const target = bot.entities[action.runtimeState.targetId] || null;
    if (!target || target.isValid === false || !target.position) {
        stopCombat(bot, action);
        action.status = "completed";
        action.result = retreatResult(bot, action, {
            retreated: true,
            reason: "target_lost",
        });
        action.updatedAt = Date.now();
        return;
    }

    const distance = target.position.distanceTo(bot.entity.position);
    action.runtimeState.lastDistance = distance;
    action.runtimeState.targetInfo = entityToPlain(bot, target);

    if (distance >= action.runtimeState.requestedDistance) {
        stopCombat(bot, action);
        action.status = "completed";
        action.result = retreatResult(bot, action, {
            retreated: true,
            reason: "distance_reached",
        });
        action.updatedAt = Date.now();
        return;
    }

    try {
        const lastGoalPosition = action.runtimeState.goalTargetPosition;
        const targetMoved = !lastGoalPosition
            || target.position.distanceSquared(lastGoalPosition) >= 0.25;
        if (targetMoved) {
            const { goals } = bot.mcagentPathfinder;
            const followGoal = new goals.GoalFollow(target, action.runtimeState.requestedDistance);
            bot.pathfinder.setGoal(new goals.GoalInvert(followGoal), true);
            action.runtimeState.goalTargetPosition = target.position.clone();
        }
        action.result = retreatResult(bot, action, {
            retreating: true,
            reason: "retreating",
        });
        action.updatedAt = Date.now();
    } catch (err) {
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "retreat_failed";
        action.result = {
            ...retreatResult(bot, action),
            ok: false,
            error_code: "retreat_failed",
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

async function stopRetreat(ctx, reason, action) {
    stopCombat(ctx.bot, action);
    return retreatResult(ctx.bot, action, {
        stopped: true,
        reason,
    });
}

function retreatStatus(ctx, action) {
    const target = ctx.bot.entities[action.runtimeState.targetId] || null;
    if (target && target.position) {
        action.runtimeState.lastDistance = target.position.distanceTo(ctx.bot.entity.position);
    }
    return {
        ...retreatResult(ctx.bot, action),
        status: action.status,
        result: action.result || null,
    };
}

// --- fight_using_weapon handlers ---

function normalizeName(name) {
    if (typeof name !== "string" || name.trim().length === 0) {
        throw new Error("weapon must be a non-empty string");
    }
    return name.trim().toLowerCase().replace(/^minecraft:/, "");
}

function findWeaponInInventory(bot, weaponName) {
    const normalized = normalizeName(weaponName);
    const items = bot.inventory.items();
    const matches = items.filter((item) => itemMatchesName(item, normalized));
    const resolved = equipmentRegistry.resolve(
        bot.username,
        weaponName,
        items,
        (item, input) => itemMatchesName(item, normalizeName(input)),
    );
    if (resolved && matches.length === 0) return resolved;
    return equipmentRegistry.enabled()
        && matches.some(equipmentRegistry.isEquipmentLike)
        ? equipmentRegistry.sortNameMatches(matches)[0] || null
        : matches[0] || null;
}

function itemMatchesName(item, normalized) {
    if (!item) return false;
    const itemName = (item.name || "").toLowerCase();
    const displayName = (item.displayName || "").toLowerCase();
    return itemName === normalized
        || displayName === normalized
        || displayName.replace(/\s+/g, "_") === normalized;
}

function itemMatchesInput(bot, item, input) {
    if (!item || typeof input !== "string") return false;
    const normalized = normalizeName(input);
    if (equipmentRegistry.enabled()) {
        const resolved = equipmentRegistry.resolve(
            bot.username,
            input,
            [item],
            (candidate, value) => itemMatchesName(candidate, normalizeName(value)),
            "equipment",
        );
        if (resolved === item) return true;
    }
    return itemMatchesName(item, normalized);
}

async function startFightUsingWeapon(ctx, params, action) {
    const bot = ctx.bot;

    // 1. 参数校验
    if (!params.weapon || typeof params.weapon !== "string" || params.weapon.trim().length === 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "weapon must be a non-empty string",
        });
    }

    // 2. 查找武器
    const heldMatches = itemMatchesInput(bot, bot.heldItem, params.weapon);
    const weaponItem = findWeaponInInventory(bot, params.weapon) || (heldMatches ? bot.heldItem : null);
    if (!weaponItem) {
        fail(action, "missing_weapon", {
            ok: false,
            error_code: "missing_weapon",
            required_weapon: params.weapon,
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 3. 查找目标
    ensurePathfinder(bot);
    const target = getEntityByTarget(bot, params.target || {});
    if (!target || (!isHostile(target) && !isWithinObserveRange(ctx, target))) {
        failTargetOutsideObserveRange(action, params.target || {}, target, ctx, {
            weapon: params.weapon,
        });
    }

    // 4. 装备武器
    const weaponName = normalizeName(weaponItem.name);
    let weaponEquipped = true;
    try {
        if (heldMatches) {
            // 已装备，无需切换
            weaponEquipped = false;
        } else {
            await bot.equip(weaponItem, "hand");
        }
    } catch (err) {
        fail(action, "equip_failed", {
            ok: false,
            error_code: "equip_failed",
            weapon: params.weapon,
            reason: err.message || "failed to equip weapon",
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 5. 初始化战斗状态机
    action.runtimeState.targetId = target.id;
    action.runtimeState.targetUuid = target.uuid || null;
    action.runtimeState.targetInfo = entityToPlain(bot, target);
    action.runtimeState.attackCount = 0;
    action.runtimeState.lastAttackAt = 0;
    action.runtimeState.startedAt = Date.now();
    action.runtimeState.weaponName = params.weapon;
    action.runtimeState.weaponEquipped = weaponEquipped;
    action.runtimeState.interval = setInterval(() => tickFightUsingWeapon(bot, action), 250);

    tickFightUsingWeapon(bot, action);

    return action.result || {
        ok: true,
        attacking: true,
        weapon: params.weapon,
        weapon_equipped: weaponEquipped,
        target: action.runtimeState.targetInfo,
        attack_count: 0,
    };
}

function tickFightUsingWeapon(bot, action) {
    if (action.runtimeState.finalizing) return;
    const maxDuration = numberParam(action.params, "max_duration_ms", 60000);

    // 超时检查
    if (Date.now() - action.runtimeState.startedAt >= maxDuration) {
        stopCombat(bot, action);
        action.status = "completed";
        action.result = {
            ok: true,
            target_dead: false,
            timeout: true,
            reason: "max_duration_exceeded",
            weapon: action.runtimeState.weaponName,
            target: action.runtimeState.targetInfo,
            attack_count: action.runtimeState.attackCount || 0,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }

    const target = bot.entities[action.runtimeState.targetId] || null;
    if (target) action.runtimeState.lastTargetPosition = target.position;
    if (!target || target.isValid === false) {
        onTargetRemoved(bot, action);
        return;
    }
    if (action.runtimeState.eatingFood) {
        return;
    }

    const followRange = numberParam(action.params, "follow_range", 2.5);
    const attackRange = numberParam(action.params, "attack_range", 3.1);
    const cooldown = numberParam(action.params, "attack_cooldown_ms", 700);
    const distance = target.position.distanceTo(bot.entity.position);

    try {
        if (distance > attackRange) {
            const { goals } = bot.mcagentPathfinder;
            bot.pathfinder.setGoal(new goals.GoalFollow(target, followRange), true);
        } else {
            if (bot.pathfinder) bot.pathfinder.setGoal(null);
            bot.lookAt(target.position.offset(0, target.height || 1, 0), true).catch(() => {});
            const now = Date.now();
            if (now - action.runtimeState.lastAttackAt >= cooldown) {
                bot.attack(target);
                action.runtimeState.lastAttackAt = now;
                action.runtimeState.attackCount += 1;
            }
        }
        action.result = {
            ok: true,
            attacking: distance <= attackRange,
            target_dead: false,
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            last_distance: distance,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } catch (err) {
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "attack_failed";
        action.result = {
            ok: false,
            error_code: "attack_failed",
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

async function stopFightUsingWeapon(ctx, reason, action) {
    stopCombat(ctx.bot, action);
    return {
        ok: true,
        stopped: true,
        reason,
        weapon: action.runtimeState.weaponName || null,
        target: action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        last_food_result: action.runtimeState.lastFoodResult || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function statusFightUsingWeapon(ctx, action) {
    const target = ctx.bot.entities[action.runtimeState.targetId] || null;
    return {
        ok: true,
        status: action.status,
        weapon: action.runtimeState.weaponName || null,
        target: entityToPlain(ctx.bot, target) || action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        last_food_result: action.runtimeState.lastFoodResult || null,
        result: action.result || null,
    };
}

async function updateFightUsingWeapon(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    const foodResult = await maybeHandleCombatFoodUpdate(ctx, action, patch);
    if (foodResult) {
        return foodResult;
    }
    return action.result || { ok: true, updated: true };
}

// --- fight_using_bow handlers ---

const BOW_WATCHDOG_MS = 50;

function findItemInInventory(bot, itemName) {
    const normalized = normalizeName(itemName);
    const items = bot.inventory.items();
    const matches = items.filter((item) => itemMatchesName(item, normalized));
    const resolved = equipmentRegistry.resolve(
        bot.username,
        itemName,
        items,
        (item, input) => itemMatchesName(item, normalizeName(input)),
    );
    if (resolved && matches.length === 0) return resolved;
    return equipmentRegistry.enabled()
        && matches.some(equipmentRegistry.isEquipmentLike)
        ? equipmentRegistry.sortNameMatches(matches)[0] || null
        : matches[0] || null;
}

function countMatchingItems(bot, itemName) {
    const normalized = normalizeName(itemName);
    const items = bot.inventory.items();
    let count = 0;
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (!item) continue;
        if (itemMatchesName(item, normalized)) {
            count += item.count || 0;
        }
    }
    return count;
}

const BOW_PHASE = {
    APPROACHING: "approaching",
    AIMING: "aiming",
    CHARGING: "charging",
    RELEASING: "releasing",
};

const THROW_PHASE = {
    APPROACHING: "approaching",
    AIMING: "aiming",
    THROWING: "throwing",
    COOLDOWN: "cooldown",
};

const THROWABLE_WEAPONS = new Set([
    "snowball",
    "egg",
    "ender_pearl",
    "splash_potion",
    "lingering_potion",
]);

function isThrowableWeapon(weaponName) {
    return THROWABLE_WEAPONS.has(normalizeName(weaponName));
}

function getBowAimHeight(target) {
    if (target && target.type === "player") return 1.16;
    const height = Number(target && target.height);
    return Number.isFinite(height) && height > 0 ? height / 2 : 0.9;
}

function getBowAimPosition(target) {
    if (!target || !target.position) return null;
    return target.position.offset(0, getBowAimHeight(target), 0);
}

function createHawkEyeBowTarget(target) {
    if (!target || target.type === "player") return target;

    // Keep position and validity dynamic while overriding only HawkEye's aim height.
    const hawkTarget = Object.create(target);
    Object.defineProperty(hawkTarget, "height", {
        configurable: true,
        enumerable: true,
        value: getBowAimHeight(target),
        writable: false,
    });
    return hawkTarget;
}

async function startFightUsingBow(ctx, params, action) {
    const bot = ctx.bot;

    // 1. 参数校验
    if (!params.bow || typeof params.bow !== "string" || params.bow.trim().length === 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "bow must be a non-empty string",
        });
    }

    // 2. 查找弓
    const bowHeld = itemMatchesInput(bot, bot.heldItem, params.bow);
    const bowItem = findItemInInventory(bot, params.bow) || (bowHeld ? bot.heldItem : null);
    if (!bowItem) {
        fail(action, "missing_bow", {
            ok: false,
            error_code: "missing_bow",
            required_bow: params.bow,
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 3. 检查箭矢
    const arrowName = params.arrow || "arrow";
    const initialArrowCount = countMatchingItems(bot, arrowName);
    if (initialArrowCount <= 0) {
        fail(action, "missing_ammo", {
            ok: false,
            error_code: "missing_ammo",
            required_ammo: arrowName,
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 4. 查找目标
    ensurePathfinder(bot);
    const target = getEntityByTarget(bot, params.target || {});
    if (!target || (!isHostile(target) && !isWithinObserveRange(ctx, target))) {
        failTargetOutsideObserveRange(action, params.target || {}, target, ctx, {
            bow: params.bow,
        });
    }

    // 5. 装备弓
    let bowEquipped = false;
    try {
        if (bowHeld) {
            bowEquipped = false;
        } else {
            await bot.equip(bowItem, "hand");
            bowEquipped = true;
        }
    } catch (err) {
        fail(action, "equip_failed", {
            ok: false,
            error_code: "equip_failed",
            bow: params.bow,
            reason: err.message || "failed to equip bow",
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 6. 初始化状态
    action.runtimeState.targetId = target.id;
    action.runtimeState.targetUuid = target.uuid || null;
    action.runtimeState.targetInfo = entityToPlain(bot, target);
    action.runtimeState.shotsFired = 0;
    action.runtimeState.bowName = params.bow;
    action.runtimeState.arrowName = arrowName;
    action.runtimeState.initialArrowCount = initialArrowCount;
    action.runtimeState.startedAt = Date.now();
    action.runtimeState.finalizing = false;
    action.runtimeState.hawkActive = false;
    action.runtimeState.lastTargetPosition = target.position;

    // 7. hawkEye 不可用时给出明确错误
    if (!bot.hawkEye || typeof bot.hawkEye.autoAttack !== "function") {
        fail(action, "hawk_eye_unavailable", {
            ok: false,
            error_code: "hawk_eye_unavailable",
            reason: "minecrafthawkeye plugin is not loaded",
        });
    }

    // 8. 启动 hawkEye（弹道预测 + 目标拦截 + 自动蓄力连射）
    const hawkStarted = bot.hawkEye.autoAttack(
        createHawkEyeBowTarget(target),
        normalizeName(bowItem.name),
        false,
    );
    if (!hawkStarted) {
        fail(action, "target_not_found", {
            ok: false,
            error_code: "target_not_found",
            requested_target: params.target || null,
            bow: params.bow,
        });
    }
    action.runtimeState.hawkActive = true;
    action.runtimeState.onShotStopped = () => {
        action.runtimeState.hawkActive = false;
    };
    bot.on("auto_shot_stopped", action.runtimeState.onShotStopped);

    // 9. watchdog: 超时/目标消失/弹药耗尽时收尾
    action.runtimeState.interval = setInterval(
        () => tickHawkEyeBow(bot, action),
        BOW_WATCHDOG_MS,
    );

    return {
        ok: true,
        shooting: true,
        engine: "hawkEye",
        bow: params.bow,
        bow_equipped: bowEquipped,
        target: action.runtimeState.targetInfo,
        shots_fired: 0,
        arrows_remaining: initialArrowCount,
    };
}

function finalizeBowAction(bot, action, baseResult) {
    if (action.runtimeState.finalizing) return;
    action.runtimeState.finalizing = true;
    stopHawkEyeAction(bot, action);
    stopCombat(bot, action);
    action.status = "completed";
    action.result = {
        ...baseResult,
        bow: action.runtimeState.bowName,
        target: action.runtimeState.targetInfo,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: countMatchingItems(bot, action.runtimeState.arrowName),
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

function tickHawkEyeBow(bot, action, unifiedFight = false) {
    if (action.runtimeState.finalizing) return;
    const maxDuration = numberParam(
        action.params,
        "max_duration_ms",
        unifiedFight ? 20000 : 120000,
    );

    // 超时检查
    if (Date.now() - action.runtimeState.startedAt >= maxDuration) {
        const baseResult = {
            ok: true,
            target_dead: false,
            timeout: true,
            reason: "max_duration_exceeded",
        };
        if (unifiedFight) {
            finalizeUnifiedHawkEyeAction(bot, action, baseResult);
        } else {
            finalizeBowAction(bot, action, baseResult);
        }
        return;
    }

    // 目标存活检查
    const target = bot.entities[action.runtimeState.targetId] || null;
    if (target) action.runtimeState.lastTargetPosition = target.position;
    if (!target || target.isValid === false) {
        if (unifiedFight) {
            onTargetRemoved(bot, action);
        } else {
            finalizeBowAction(bot, action, {
                ok: true,
                target_dead: true,
                reason: "target_removed",
            });
        }
        return;
    }

    // 弹药检查
    const arrowsLeft = countMatchingItems(bot, action.runtimeState.arrowName);
    action.runtimeState.shotsFired = Math.max(
        (action.runtimeState.initialArrowCount ?? action.runtimeState.initialAmmoCount) - arrowsLeft,
        action.runtimeState.shotsFired || 0,
    );
    if (arrowsLeft <= 0) {
        const baseResult = {
            ok: true,
            target_dead: false,
            out_of_ammo: true,
            reason: "out_of_ammo",
        };
        if (unifiedFight) {
            finalizeUnifiedHawkEyeAction(bot, action, baseResult);
        } else {
            finalizeBowAction(bot, action, baseResult);
        }
        return;
    }

    // hawkEye 因目标失效自动停止时，由本 watchdog 收尾
    if (!action.runtimeState.hawkActive) {
        const baseResult = {
            ok: true,
            target_dead: false,
            reason: "hawk_eye_stopped",
        };
        if (unifiedFight) {
            finalizeUnifiedHawkEyeAction(bot, action, baseResult);
        } else {
            finalizeBowAction(bot, action, {
                ...baseResult,
                target_dead: true,
            });
        }
        return;
    }

    action.result = {
        ok: true,
        phase: "hawk_attack",
        ...(unifiedFight ? { combat_type: "ranged", engine: "hawkEye" } : {}),
        target_dead: false,
        bow: action.runtimeState.bowName || action.runtimeState.weaponName,
        ...(unifiedFight ? { weapon: action.runtimeState.weaponName } : {}),
        target: entityToPlain(bot, target),
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: arrowsLeft,
        duration_ms: Date.now() - action.runtimeState.startedAt,
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

async function stopFightUsingBow(ctx, reason, action) {
    const bot = ctx.bot;
    if (action.runtimeState.finalizing) {
        return {
            ok: true,
            stopped: true,
            reason,
        };
    }
    action.runtimeState.finalizing = true;
    stopHawkEyeAction(bot, action);
    stopCombat(bot, action);
    await safeDeactivateHeldItem(bot);
    return {
        ok: true,
        stopped: true,
        reason,
        bow: action.runtimeState.bowName || null,
        target: action.runtimeState.targetInfo || null,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: countMatchingItems(bot, action.runtimeState.arrowName || "arrow"),
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        position: positionToPlain(bot.entity.position),
    };
}

function statusFightUsingBow(ctx, action) {
    const target = ctx.bot.entities[action.runtimeState.targetId] || null;
    return {
        ok: true,
        status: action.status,
        phase: action.runtimeState.hawkActive
            ? "hawk_attack"
            : (action.status === "running" ? "stopping" : action.status),
        bow: action.runtimeState.bowName || null,
        target: entityToPlain(ctx.bot, target) || action.runtimeState.targetInfo || null,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: countMatchingItems(ctx.bot, action.runtimeState.arrowName || "arrow"),
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        last_food_result: action.runtimeState.lastFoodResult || null,
        result: action.result || null,
    };
}

async function updateFightUsingBow(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    const foodResult = await maybeHandleCombatFoodUpdate(ctx, action, patch);
    if (foodResult) {
        return foodResult;
    }
    return action.result || { ok: true, updated: true };
}

// --- fight (unified melee+ranged) handlers ---

const RANGED_WEAPONS = new Set(["bow", "crossbow"]);

function isRangedWeapon(weaponName) {
    return RANGED_WEAPONS.has(normalizeName(weaponName));
}

function startHawkEyeForFight(bot, target, action) {
    const state = action.runtimeState;
    if (state.weaponName !== "bow") return false;
    if (!bot.hawkEye || typeof bot.hawkEye.autoAttack !== "function") return false;

    let started = false;
    try {
        started = bot.hawkEye.autoAttack(
            createHawkEyeBowTarget(target),
            "bow",
            false,
        ) === true;
    } catch (_) {
        started = false;
    }
    if (!started) return false;

    state.engine = "hawkEye";
    state.bowName = state.weaponName;
    state.hawkActive = true;
    state.onShotStopped = () => {
        state.hawkActive = false;
    };
    if (typeof bot.on === "function") {
        bot.on("auto_shot_stopped", state.onShotStopped);
    }
    return true;
}

function finalizeUnifiedHawkEyeAction(bot, action, baseResult) {
    if (action.runtimeState.finalizing) return;
    action.runtimeState.finalizing = true;
    stopHawkEyeAction(bot, action);
    stopCombat(bot, action);
    action.status = "completed";
    action.result = {
        ...baseResult,
        combat_type: "ranged",
        engine: "hawkEye",
        weapon: action.runtimeState.weaponName,
        target: entityToPlain(
            bot,
            bot.entities[action.runtimeState.targetId] || null,
        ) || action.runtimeState.targetInfo || null,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: countMatchingItems(bot, action.runtimeState.arrowName),
        duration_ms: Date.now() - action.runtimeState.startedAt,
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

async function startFight(ctx, params, action) {
    const bot = ctx.bot;

    // 1. 解析武器：显式 weapon 优先；缺省使用当前手持物；仍无则空手近战
    const explicitWeapon =
        params.weapon && typeof params.weapon === "string"
            ? params.weapon.trim()
            : "";
    const heldItem = bot.heldItem || null;
    const heldName = heldItem
        ? String(heldItem.name || heldItem.displayName || "").toLowerCase()
        : "";
    const explicitItem = explicitWeapon ? findItemInInventory(bot, explicitWeapon) : null;
    const heldMatches = Boolean(heldItem) && (
        explicitWeapon ? itemMatchesInput(bot, heldItem, explicitWeapon) : true
    );
    const weaponItem = explicitItem || (heldMatches ? heldItem : null);
    const weaponName = weaponItem
        ? String(weaponItem.name || weaponItem.displayName || "").toLowerCase()
        : (explicitWeapon || heldName);
    const weaponNorm = weaponName ? normalizeName(weaponName) : "";

    // 2. 武器类型判定：显式 combat_type 优先，否则按武器自动识别（空手→melee）
    const explicitType = params.combat_type ? String(params.combat_type).toLowerCase() : null;
    let combatType;
    if (explicitType === "melee" || explicitType === "ranged" || explicitType === "throwable") {
        combatType = explicitType;
    } else if (weaponNorm && isRangedWeapon(weaponNorm)) {
        combatType = "ranged";
    } else if (weaponNorm && isThrowableWeapon(weaponNorm)) {
        combatType = "throwable";
    } else {
        combatType = "melee";
    }
    const ranged = combatType === "ranged";
    const throwable = combatType === "throwable";

    // 3. 查找武器：空手无需武器；显式指定的武器不在背包且非手持时失败
    if (!weaponItem && explicitWeapon && !heldMatches) {
        fail(action, "missing_weapon", {
            ok: false,
            error_code: "missing_weapon",
            required_weapon: explicitWeapon,
            combat_type: combatType,
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        });
    }

    // 4. 远程/投掷物额外检查弹药
    let initialAmmoCount = null;
    if ((ranged || throwable) && weaponNorm) {
        const ammoName = ranged ? (params.arrow || "arrow") : weaponNorm;
        initialAmmoCount = countMatchingItems(bot, ammoName);
        if (initialAmmoCount <= 0) {
            fail(action, "missing_ammo", {
                ok: false,
                error_code: "missing_ammo",
                required_ammo: ammoName,
                combat_type: combatType,
                inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
            });
        }
    }

    // 5. 查找目标
    ensurePathfinder(bot);
    const target = getEntityByTarget(bot, params.target || {});
    if (!target || (!isHostile(target) && !isWithinObserveRange(ctx, target))) {
        failTargetOutsideObserveRange(action, params.target || {}, target, ctx, {
            weapon: params.weapon,
            combat_type: combatType,
        });
    }

    // 6. 装备武器（仅当有武器且未手持匹配；空手不装备）
    let weaponEquipped = false;
    if (weaponItem && weaponNorm) {
        try {
            if (heldMatches) {
                weaponEquipped = false;
            } else {
                await bot.equip(weaponItem, "hand");
                weaponEquipped = true;
            }
        } catch (err) {
            fail(action, "equip_failed", {
                ok: false,
                error_code: "equip_failed",
                weapon: weaponNorm,
                combat_type: combatType,
                reason: err.message || "failed to equip weapon",
                inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
            });
        }
    }

    // 7. 初始化状态机
    action.runtimeState.targetId = target.id;
    action.runtimeState.targetUuid = target.uuid || null;
    action.runtimeState.targetInfo = entityToPlain(bot, target);
    action.runtimeState.startedAt = Date.now();
    action.runtimeState.weaponName = weaponNorm || null;
    action.runtimeState.combatType = combatType;
    action.runtimeState.weaponEquipped = weaponEquipped;
    action.runtimeState.engine = "native";

    if (ranged || throwable) {
        action.runtimeState.shotsFired = 0;
        action.runtimeState.initialAmmoCount = initialAmmoCount;
        if (ranged) {
            action.runtimeState.arrowName = params.arrow || "arrow";
            action.runtimeState.initialArrowCount = initialAmmoCount;
        }
        action.runtimeState.phase = ranged ? BOW_PHASE.APPROACHING : THROW_PHASE.APPROACHING;
        action.runtimeState.phaseStartedAt = Date.now();
    } else {
        action.runtimeState.attackCount = 0;
        action.runtimeState.lastAttackAt = 0;
    }

    action.runtimeState.interval = setInterval(
        () => tickFight(bot, action),
        ranged ? BOW_WATCHDOG_MS : (throwable ? 100 : 250),
    );
    action.runtimeState.autoPickup = typeof ctx.getAutoPickup === "function" ? ctx.getAutoPickup() : false;
    action.runtimeState.taskCategory = typeof ctx.getTaskCategory === "function" ? ctx.getTaskCategory() : null;
    tickFight(bot, action);

    return action.result || {
        ok: true,
        combat_type: combatType,
        weapon: params.weapon,
        weapon_equipped: weaponEquipped,
        target: action.runtimeState.targetInfo,
        attack_count: 0,
        shots_fired: 0,
    };
}

function tickFight(bot, action) {
    if (action.runtimeState.finalizing) return;
    const maxDuration = numberParam(action.params, "max_duration_ms", 20000);

    // 超时检查
    if (Date.now() - action.runtimeState.startedAt >= maxDuration) {
        stopHawkEyeAction(bot, action);
        stopCombat(bot, action);
        action.status = "completed";
        action.result = {
            ok: true,
            target_dead: false,
            timeout: true,
            reason: "max_duration_exceeded",
            combat_type: action.runtimeState.combatType,
            weapon: action.runtimeState.weaponName,
            target: action.runtimeState.targetInfo,
            attack_count: action.runtimeState.attackCount || 0,
            shots_fired: action.runtimeState.shotsFired || 0,
            arrows_remaining: action.runtimeState.combatType === "ranged"
                ? countMatchingItems(bot, action.runtimeState.arrowName) : null,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }

    // 目标存活检查
    const target = bot.entities[action.runtimeState.targetId] || null;
    if (target) action.runtimeState.lastTargetPosition = target.position;
    if (!target || target.isValid === false) {
        onTargetRemoved(bot, action);
        return;
    }

    // 弹药检查（仅远程）
    if (action.runtimeState.combatType === "ranged") {
        const arrowsLeft = countMatchingItems(bot, action.runtimeState.arrowName);
        if (arrowsLeft <= 0) {
            stopHawkEyeAction(bot, action);
            stopCombat(bot, action);
            action.status = "completed";
            action.result = {
                ok: true,
                target_dead: false,
                out_of_ammo: true,
                reason: "out_of_ammo",
                combat_type: "ranged",
                weapon: action.runtimeState.weaponName,
                target: entityToPlain(bot, target),
                attack_count: 0,
                shots_fired: action.runtimeState.shotsFired || 0,
                arrows_remaining: 0,
                duration_ms: Date.now() - action.runtimeState.startedAt,
                position: positionToPlain(bot.entity.position),
            };
            action.updatedAt = Date.now();
            return;
        }
    }
    // 弹药检查（投掷物：弹药即武器本身）
    if (action.runtimeState.combatType === "throwable") {
        const ammoLeft = countMatchingItems(bot, action.runtimeState.weaponName);
        if (ammoLeft <= 0) {
            stopCombat(bot, action);
            action.status = "completed";
            action.result = {
                ok: true,
                target_dead: false,
                out_of_ammo: true,
                reason: "out_of_ammo",
                combat_type: "throwable",
                weapon: action.runtimeState.weaponName,
                target: entityToPlain(bot, target),
                attack_count: 0,
                shots_fired: action.runtimeState.shotsFired || 0,
                ammo_remaining: 0,
                duration_ms: Date.now() - action.runtimeState.startedAt,
                position: positionToPlain(bot.entity.position),
            };
            action.updatedAt = Date.now();
            return;
        }
    }

    if (action.runtimeState.eatingFood) {
        return;
    }

    // 路由到对应战斗逻辑
    if (action.runtimeState.combatType === "ranged") {
        if (action.runtimeState.engine === "hawkEye") {
            tickHawkEyeBow(bot, action, true);
        } else {
            tickFightRanged(bot, action, target);
        }
    } else if (action.runtimeState.combatType === "throwable") {
        tickFightThrowable(bot, action, target);
    } else {
        tickFightMelee(bot, action, target);
    }
}

// --- 投掷物 tick 逻辑 ---

function tickFightThrowable(bot, action, target) {
    const throwRange = numberParam(action.params, "throw_range", 8);
    const cooldown = numberParam(action.params, "attack_cooldown_ms", 700);
    const distance = target.position.distanceTo(bot.entity.position);

    try {
        switch (action.runtimeState.phase) {
            case THROW_PHASE.APPROACHING: {
                if (distance > throwRange) {
                    const { goals } = bot.mcagentPathfinder;
                    bot.pathfinder.setGoal(new goals.GoalFollow(target, throwRange), true);
                } else {
                    if (bot.pathfinder) bot.pathfinder.setGoal(null);
                    action.runtimeState.phase = THROW_PHASE.AIMING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            case THROW_PHASE.AIMING: {
                bot.lookAt(target.position.offset(0, target.height || 1, 0), true).catch(() => {});
                if (Date.now() - action.runtimeState.phaseStartedAt >= 100) {
                    bot.activateItem();
                    action.runtimeState.shotsFired += 1;
                    action.runtimeState.phase = THROW_PHASE.COOLDOWN;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            case THROW_PHASE.COOLDOWN: {
                if (Date.now() - action.runtimeState.phaseStartedAt >= cooldown) {
                    action.runtimeState.phase = THROW_PHASE.APPROACHING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            default: {
                action.runtimeState.phase = THROW_PHASE.APPROACHING;
                action.runtimeState.phaseStartedAt = Date.now();
            }
        }

        action.result = {
            ok: true,
            combat_type: "throwable",
            phase: action.runtimeState.phase,
            target_dead: false,
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: 0,
            shots_fired: action.runtimeState.shotsFired || 0,
            ammo_remaining: countMatchingItems(bot, action.runtimeState.weaponName),
            last_distance: distance,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } catch (err) {
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "shoot_failed";
        action.result = {
            ok: false,
            error_code: "shoot_failed",
            combat_type: "throwable",
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            shots_fired: action.runtimeState.shotsFired || 0,
            ammo_remaining: countMatchingItems(bot, action.runtimeState.weaponName),
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

// --- 近战 tick 逻辑 ---

function tickFightMelee(bot, action, target) {
    const followRange = numberParam(action.params, "follow_range", 2.5);
    const attackRange = numberParam(action.params, "attack_range", 3.1);
    const cooldown = numberParam(action.params, "attack_cooldown_ms", 700);
    const distance = target.position.distanceTo(bot.entity.position);

    try {
        if (distance > attackRange) {
            const { goals } = bot.mcagentPathfinder;
            bot.pathfinder.setGoal(new goals.GoalFollow(target, followRange), true);
        } else {
            if (bot.pathfinder) bot.pathfinder.setGoal(null);
            bot.lookAt(target.position.offset(0, target.height || 1, 0), true).catch(() => {});
            const now = Date.now();
            if (now - action.runtimeState.lastAttackAt >= cooldown) {
                bot.attack(target);
                action.runtimeState.lastAttackAt = now;
                action.runtimeState.attackCount += 1;
            }
        }
        action.result = {
            ok: true,
            combat_type: "melee",
            attacking: distance <= attackRange,
            target_dead: false,
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            shots_fired: 0,
            last_distance: distance,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } catch (err) {
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "attack_failed";
        action.result = {
            ok: false,
            error_code: "attack_failed",
            combat_type: "melee",
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: action.runtimeState.attackCount || 0,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

// --- 远程 tick 逻辑 ---

function tickFightRanged(bot, action, target) {
    const shootRange = numberParam(action.params, "shoot_range", 40);
    const chargeMs = numberParam(action.params, "charge_ms", 1000);
    const distance = target.position.distanceTo(bot.entity.position);
    const arrowsLeft = countMatchingItems(bot, action.runtimeState.arrowName);

    try {
        switch (action.runtimeState.phase) {
            case BOW_PHASE.APPROACHING: {
                // Hostile targets may be selected from any distance, but firing still waits for shoot_range.
                if (distance > shootRange) {
                    const { goals } = bot.mcagentPathfinder;
                    bot.pathfinder.setGoal(new goals.GoalFollow(target, shootRange), true);
                } else {
                    if (bot.pathfinder) bot.pathfinder.setGoal(null);
                    if (startHawkEyeForFight(bot, target, action)) {
                        action.runtimeState.phase = "hawk_attack";
                        tickHawkEyeBow(bot, action, true);
                        return;
                    }
                    action.runtimeState.phase = BOW_PHASE.AIMING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            case BOW_PHASE.AIMING: {
                bot.lookAt(getBowAimPosition(target), true).catch(() => {});
                if (Date.now() - action.runtimeState.phaseStartedAt >= 150) {
                    bot.activateItem();
                    action.runtimeState.phase = BOW_PHASE.CHARGING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            case BOW_PHASE.CHARGING: {
                bot.lookAt(getBowAimPosition(target), true).catch(() => {});
                if (Date.now() - action.runtimeState.phaseStartedAt >= chargeMs) {
                    bot.deactivateItem();
                    action.runtimeState.shotsFired += 1;
                    action.runtimeState.phase = BOW_PHASE.RELEASING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            case BOW_PHASE.RELEASING: {
                if (Date.now() - action.runtimeState.phaseStartedAt >= 200) {
                    action.runtimeState.phase = BOW_PHASE.APPROACHING;
                    action.runtimeState.phaseStartedAt = Date.now();
                }
                break;
            }
            default: {
                action.runtimeState.phase = BOW_PHASE.APPROACHING;
                action.runtimeState.phaseStartedAt = Date.now();
            }
        }

        action.result = {
            ok: true,
            combat_type: "ranged",
            phase: action.runtimeState.phase,
            target_dead: false,
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            attack_count: 0,
            shots_fired: action.runtimeState.shotsFired || 0,
            arrows_remaining: arrowsLeft,
            last_distance: distance,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
    } catch (err) {
        safeDeactivateHeldItem(bot).catch(() => {});
        stopCombat(bot, action);
        action.status = "failed";
        action.error = "shoot_failed";
        action.result = {
            ok: false,
            error_code: "shoot_failed",
            combat_type: "ranged",
            weapon: action.runtimeState.weaponName,
            target: entityToPlain(bot, target),
            shots_fired: action.runtimeState.shotsFired || 0,
            arrows_remaining: arrowsLeft,
            duration_ms: Date.now() - action.runtimeState.startedAt,
            last_food_result: action.runtimeState.lastFoodResult || null,
            reason: err.message,
        };
        action.updatedAt = Date.now();
    }
}

async function stopFight(ctx, reason, action) {
    if (action.runtimeState.engine === "hawkEye") {
        stopHawkEyeAction(ctx.bot, action);
    }
    if (action.runtimeState.combatType === "ranged" || action.runtimeState.combatType === "throwable") {
        await safeDeactivateHeldItem(ctx.bot);
    }
    stopCombat(ctx.bot, action);
    return {
        ok: true,
        stopped: true,
        reason,
        combat_type: action.runtimeState.combatType || null,
        weapon: action.runtimeState.weaponName || null,
        target: action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: action.runtimeState.combatType === "ranged"
            ? countMatchingItems(ctx.bot, action.runtimeState.arrowName || "arrow") : null,
        ammo_remaining: action.runtimeState.combatType === "throwable"
            ? countMatchingItems(ctx.bot, action.runtimeState.weaponName || "") : null,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        last_food_result: action.runtimeState.lastFoodResult || null,
        position: positionToPlain(ctx.bot.entity.position),
    };
}

function statusFight(ctx, action) {
    const target = ctx.bot.entities[action.runtimeState.targetId] || null;
    return {
        ok: true,
        status: action.status,
        combat_type: action.runtimeState.combatType || null,
        phase: action.runtimeState.phase || null,
        engine: action.runtimeState.engine || "native",
        weapon: action.runtimeState.weaponName || null,
        target: entityToPlain(ctx.bot, target) || action.runtimeState.targetInfo || null,
        attack_count: action.runtimeState.attackCount || 0,
        shots_fired: action.runtimeState.shotsFired || 0,
        arrows_remaining: action.runtimeState.combatType === "ranged"
            ? countMatchingItems(ctx.bot, action.runtimeState.arrowName || "arrow") : null,
        ammo_remaining: action.runtimeState.combatType === "throwable"
            ? countMatchingItems(ctx.bot, action.runtimeState.weaponName || "") : null,
        duration_ms: Date.now() - (action.runtimeState.startedAt || Date.now()),
        last_food_result: action.runtimeState.lastFoodResult || null,
        result: action.result || null,
    };
}

async function updateFight(ctx, patch, action) {
    action.params = { ...action.params, ...patch };
    const foodResult = await maybeHandleCombatFoodUpdate(ctx, action, patch);
    if (foodResult) {
        return foodResult;
    }
    return action.result || { ok: true, updated: true };
}

function extractCombatFood(patch) {
    if (!patch || typeof patch.food !== "string") return "";
    return patch.food.trim();
}

async function maybeHandleCombatFoodUpdate(ctx, action, patch) {
    const food = extractCombatFood(patch);
    if (!food) {
        return null;
    }

    action.runtimeState.eatingFood = true;
    let foodResult;
    try {
        await safeDeactivateHeldItem(ctx.bot);
        resetRangedPhaseForFood(action);
        foodResult = await executeUseFood(ctx, { food });
    } catch (err) {
        foodResult = {
            ok: false,
            error_code: "food_update_failed",
            food,
            reason: err.message || "failed to consume food during combat",
        };
    } finally {
        action.runtimeState.eatingFood = false;
        action.runtimeState.lastFoodResult = foodResult || null;
        if (action.params && typeof action.params === "object") {
            delete action.params.food;
        }
    }

    const snapshot = action.result && typeof action.result === "object" ? { ...action.result } : { ok: true, updated: true };
    const merged = {
        ...snapshot,
        food_result: foodResult,
        last_food_result: foodResult,
    };
    action.result = merged;
    action.updatedAt = Date.now();
    return merged;
}

function resetRangedPhaseForFood(action) {
    const state = action.runtimeState || {};
    if (state.phase === BOW_PHASE.CHARGING || state.phase === BOW_PHASE.RELEASING) {
        state.phase = BOW_PHASE.AIMING;
        state.phaseStartedAt = Date.now();
    }
}

// --- use_food (combat-aware eating) ---

function healthSnapshot(bot) {
    return {
        health: bot.health ?? null,
        food: bot.food ?? null,
        foodSaturation: bot.foodSaturation ?? null,
    };
}

async function executeUseFood(ctx, params) {
    const bot = ctx.bot;
    const food = normalizeConsumableName(params.food);

    // 1. 查找可消费物（食物或普通药水）
    const foodItem = findConsumableItem(bot, food);
    if (!foodItem) {
        return {
            ok: false,
            error_code: "missing_food",
            food,
            required_food: food,
            reason: `No consumable item ${food} in inventory`,
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        };
    }

    // 2. 记录使用前状态
    const before = healthSnapshot(bot);
    const previousWeapon = itemToPlain(bot.heldItem);

    // 3. 装备可消费物
    try {
        await bot.equip(foodItem, "hand");
    } catch (err) {
        return {
            ok: false,
            error_code: "equip_failed",
            food,
            reason: err.message || "failed to equip consumable item",
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        };
    }

    // 4. 使用并等待完成；超时后确认物品数量并最多重试一次。
    const consumeResult = await consumeWithRetry(bot, food);
    if (!consumeResult.ok) {
        await safeDeactivateHeldItem(bot);
        let weaponRestored = false;
        if (previousWeapon && previousWeapon.name) {
            try {
                const weaponItem = findItemInInventory(bot, previousWeapon.name);
                if (weaponItem) {
                    await bot.equip(weaponItem, "hand");
                    weaponRestored = true;
                }
            } catch (_) {}
        }
        return {
            ...consumeResult,
            food,
            previous_weapon: previousWeapon,
            weapon_restored: weaponRestored,
            before,
            after: healthSnapshot(bot),
            inventory_items: bot.inventory.items().filter(Boolean).map(itemToPlain),
        };
    }

    // 5. 恢复之前手持的武器
    let weaponRestored = false;
    if (previousWeapon && previousWeapon.name) {
        try {
            const weaponItem = findItemInInventory(bot, previousWeapon.name);
            if (weaponItem) {
                await bot.equip(weaponItem, "hand");
                weaponRestored = true;
            }
        } catch (_) {}
    }

    // 6. 记录使用后状态
    const after = healthSnapshot(bot);

    return {
        ...consumeResult,
        food,
        previous_weapon: previousWeapon,
        weapon_restored: weaponRestored,
        before,
        after,
        health_change: (after.health ?? 0) - (before.health ?? 0),
        food_change: (after.food ?? 0) - (before.food ?? 0),
    };
}

module.exports = {
    name: "combat",
    commands: {
        patrol: { execute: patrol },
        observe: { execute: observe },
        attack: {
            start: startAttack,
            update: updateAttack,
            stop: stopAttack,
            status: attackStatus,
        },
        retreat: {
            start: startRetreat,
            stop: stopRetreat,
            status: retreatStatus,
        },
        fight_using_weapon: {
            start: startFightUsingWeapon,
            stop: stopFightUsingWeapon,
            status: statusFightUsingWeapon,
            update: updateFightUsingWeapon,
        },
        fight_using_bow: {
            start: startFightUsingBow,
            stop: stopFightUsingBow,
            status: statusFightUsingBow,
            update: updateFightUsingBow,
        },
        fight: {
            start: startFight,
            stop: stopFight,
            status: statusFight,
            update: updateFight,
        },
        use_food: { execute: executeUseFood },
    },
    _test: {
        getEntityByTarget,
        parseEntityShorthand,
    },
};
