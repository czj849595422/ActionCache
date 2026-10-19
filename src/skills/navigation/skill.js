"use strict";

function numberParam(params, name, fallback = null) {
    const value = params[name] ?? fallback;
    const number = Number(value);
    if (!Number.isFinite(number)) {
        throw new Error(`${name} must be a finite number`);
    }
    return number;
}

function targetFromParams(params) {
    return {
        x: numberParam(params, "x"),
        y: numberParam(params, "y"),
        z: numberParam(params, "z"),
        range: Math.max(0, numberParam(params, "range", 1)),
    };
}

function positionToPlain(position) {
    return { x: position.x, y: position.y, z: position.z };
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
        position: positionToPlain(entity.position),
        distance: entity.position && bot.entity && bot.entity.position
            ? entity.position.distanceTo(bot.entity.position)
            : null,
        isValid: entity.isValid ?? null,
    };
}

function distanceToEntity(bot, entity) {
    return entity && entity.position && bot.entity && bot.entity.position
        ? entity.position.distanceTo(bot.entity.position)
        : Infinity;
}

function ensurePathfinder(bot) {
    if (!bot.pathfinder) {
        throw new Error("mineflayer-pathfinder is not loaded");
    }
    if (!bot.mcagentPathfinder) {
        throw new Error("mineflayer-pathfinder module is not available");
    }
    if (!bot.mcagentDefaultMovements) {
        const { Movements } = bot.mcagentPathfinder;
        bot.mcagentDefaultMovements = new Movements(bot);
    }
    bot.pathfinder.setMovements(bot.mcagentDefaultMovements);
}

function goalFromTarget(bot, target) {
    const { goals } = bot.mcagentPathfinder;
    return new goals.GoalNear(target.x, target.y, target.z, target.range);
}

function distanceTo(bot, target) {
    return bot.entity.position.distanceTo({ x: target.x, y: target.y, z: target.z });
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

function withTimeout(promise, timeoutMs, label) {
    if (!timeoutMs || timeoutMs <= 0) return promise;
    let timeoutId = null;
    try {
        return Promise.race([
            promise,
            new Promise((_, reject) => {
                timeoutId = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
            }),
        ]);
    } finally {
        if (timeoutId) clearTimeout(timeoutId);
    }
}

// ---------------------------------------------------------------------------
// 实体引用解析：entity_id / entity_uuid / entity_name（对应感知 entities[].id/uuid/name）
// ---------------------------------------------------------------------------

function entityRefFromParams(params) {
    return {
        id: params.entity_id ?? params.entityId ?? null,
        uuid: params.entity_uuid ?? params.entityUuid ?? null,
        name: params.entity_name ?? params.entityName ?? null,
    };
}

function hasEntityRef(ref) {
    if (ref.id !== undefined && ref.id !== null) return true;
    if (ref.uuid !== undefined && ref.uuid !== null && String(ref.uuid).trim().length > 0) return true;
    if (ref.name !== undefined && ref.name !== null && String(ref.name).trim().length > 0) return true;
    return false;
}

function findEntityByRef(bot, ref) {
    if (ref.id !== undefined && ref.id !== null) {
        const entity = bot.entities[ref.id] || bot.entities[String(ref.id)];
        if (entity) return entity;
    }
    if (ref.uuid !== undefined && ref.uuid !== null && String(ref.uuid).trim()) {
        const wanted = String(ref.uuid).trim();
        return Object.values(bot.entities || {}).find((entity) => entity && entity.uuid === wanted) || null;
    }
    if (ref.name !== undefined && ref.name !== null && String(ref.name).trim()) {
        const wantedRaw = String(ref.name).trim();
        const wanted = normalizeEntityName(wantedRaw);
        const exactPlayer = bot.players && bot.players[wantedRaw] ? bot.players[wantedRaw].entity : null;
        if (exactPlayer && exactPlayer !== bot.entity) return exactPlayer;
        const namedPlayer = Object.entries(bot.players || {}).find(([username, player]) => {
            if (!player || !player.entity || player.entity === bot.entity) return false;
            return entityMatchesName(player.entity, wanted)
                || normalizeEntityName(username) === wanted
                || normalizeEntityName(player.displayName) === wanted;
        });
        if (namedPlayer) return namedPlayer[1].entity;
        return bot.nearestEntity((entity) => {
            if (!entity || !entity.position || entity === bot.entity) return false;
            return entityMatchesName(entity, wanted);
        }) || null;
    }
    return null;
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

// ---------------------------------------------------------------------------
// 范围约束：range_enabled + max_range
// ---------------------------------------------------------------------------

function rangeParamsFromParams(params) {
    const rangeEnabled = params.range_enabled === true || params.rangeEnabled === true;
    const rawMaxRange = params.max_range !== undefined
        ? params.max_range
        : (params.maxRange !== undefined ? params.maxRange : null);
    const maxRange = rawMaxRange === null || rawMaxRange === undefined
        ? null
        : Number(rawMaxRange);
    if (rangeEnabled) {
        if (maxRange === null || !Number.isFinite(maxRange) || maxRange <= 0) {
            throw new Error("max_range must be a positive number when range_enabled is true");
        }
    }
    return { rangeEnabled, maxRange: rangeEnabled ? maxRange : null };
}

// ---------------------------------------------------------------------------
// goto_position：坐标模式（原行为）或实体模式（靠近实体后结束）
// ---------------------------------------------------------------------------

async function gotoPosition(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);

    const entityRef = entityRefFromParams(params);
    const hasEntity = hasEntityRef(entityRef);
    const hasCoords = params.x !== undefined || params.y !== undefined || params.z !== undefined;
    if (hasEntity && hasCoords) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "entity ref and coordinates are mutually exclusive",
        });
    }
    if (hasEntity) {
        return await gotoEntityPosition(ctx, params, action, entityRef);
    }

    const target = targetFromParams(params);
    const timeout = params.timeout ? Number(params.timeout) * 1000 : null;
    const goal = goalFromTarget(bot, target);
    const gotoPromise = bot.pathfinder.goto(goal);
    if (timeout && Number.isFinite(timeout) && timeout > 0) {
        await Promise.race([
            gotoPromise,
            new Promise((_, reject) => setTimeout(() => reject(new Error("navigation timeout")), timeout)),
        ]);
    } else {
        await gotoPromise;
    }
    return {
        reached: distanceTo(bot, target) <= target.range + 0.5,
        target,
        position: positionToPlain(bot.entity.position),
    };
}

function navigationTimeoutMs(params, action) {
    if (params.timeout === undefined || params.timeout === null || Number(params.timeout) === 0) {
        return null;
    }
    const seconds = Number(params.timeout);
    if (!Number.isFinite(seconds) || seconds < 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "timeout must be a non-negative number",
        });
    }
    return seconds * 1000;
}

function gotoActionRunning(action) {
    return !action.runtimeState.cancelled
        && (!action.status || action.status === "running");
}

function clearGotoRuntime(action) {
    if (action.runtimeState.timeoutHandle) {
        clearTimeout(action.runtimeState.timeoutHandle);
        action.runtimeState.timeoutHandle = null;
    }
    if (action.runtimeState.interval) {
        clearInterval(action.runtimeState.interval);
        action.runtimeState.interval = null;
    }
    action.runtimeState.gotoPromise = null;
}

function stopGotoPath(bot) {
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    if (typeof bot.clearControlStates === "function") bot.clearControlStates();
}

function finishGotoSuccess(ctx, action, result) {
    if (!gotoActionRunning(action)) return;
    clearGotoRuntime(action);
    stopGotoPath(ctx.bot);
    const payload = {
        ok: true,
        ...result,
        position: positionToPlain(ctx.bot.entity.position),
    };
    action.runtimeState.lastResult = payload;
    action.result = payload;
    action.status = "completed";
    action.updatedAt = Date.now();
}

function finishGotoFailure(ctx, action, errorCode, reason, extra = {}) {
    if (!gotoActionRunning(action)) return;
    clearGotoRuntime(action);
    stopGotoPath(ctx.bot);
    const payload = {
        ok: false,
        error_code: errorCode,
        reason,
        ...extra,
        position: positionToPlain(ctx.bot.entity.position),
    };
    action.runtimeState.lastResult = payload;
    action.result = payload;
    action.status = "failed";
    action.error = errorCode;
    action.updatedAt = Date.now();
}

function startGotoEntityPosition(ctx, params, action, entityRef) {
    const bot = ctx.bot;
    const entity = findEntityByRef(bot, entityRef);
    if (!entity) {
        fail(action, "entity_not_found", {
            ok: false,
            error_code: "entity_not_found",
            requested: entityRef,
        });
    }

    const range = Math.max(0, numberParam(params, "range", 1));
    const timeoutMs = navigationTimeoutMs(params, action);
    action.runtimeState.cancelled = false;
    action.runtimeState.mode = "entity";
    action.runtimeState.entityId = entity.id;
    action.runtimeState.entityRef = entityRef;
    action.runtimeState.range = range;
    action.runtimeState.targetInfo = entityToPlain(bot, entity);
    action.runtimeState.lastResult = {
        ok: true,
        moving: true,
        reached: false,
        target: action.runtimeState.targetInfo,
        position: positionToPlain(bot.entity.position),
    };
    action.result = action.runtimeState.lastResult;
    action.runtimeState.interval = setInterval(() => tickGotoEntityPosition(ctx, action), 200);
    if (timeoutMs) {
        action.runtimeState.timeoutHandle = setTimeout(() => {
            const current = currentGotoEntity(bot, action);
            finishGotoFailure(
                ctx,
                action,
                "timeout",
                "navigation timeout while following entity",
                {
                    target: entityToPlain(bot, current) || action.runtimeState.targetInfo,
                },
            );
        }, timeoutMs);
    }
    tickGotoEntityPosition(ctx, action);
    return action.result;
}

function currentGotoEntity(bot, action) {
    return bot.entities[action.runtimeState.entityId]
        || (action.runtimeState.entityRef
            ? findEntityByRef(bot, action.runtimeState.entityRef)
            : null)
        || null;
}

function tickGotoEntityPosition(ctx, action) {
    if (!gotoActionRunning(action)) return;
    const bot = ctx.bot;
    const current = currentGotoEntity(bot, action);
    if (!current || current.isValid === false) {
        finishGotoFailure(
            ctx,
            action,
            "entity_lost",
            "target entity disappeared",
            { requested: action.runtimeState.entityRef },
        );
        return;
    }

    const distance = distanceToEntity(bot, current);
    if (distance <= action.runtimeState.range) {
        finishGotoSuccess(ctx, action, {
            reached: true,
            reason: "target_reached",
            target: entityToPlain(bot, current),
        });
        return;
    }

    const { goals } = bot.mcagentPathfinder;
    bot.pathfinder.setGoal(new goals.GoalFollow(current, action.runtimeState.range), true);
    action.runtimeState.targetInfo = entityToPlain(bot, current);
    action.runtimeState.lastResult = {
        ok: true,
        moving: true,
        reached: false,
        target: action.runtimeState.targetInfo,
        distance,
        position: positionToPlain(bot.entity.position),
    };
    action.result = action.runtimeState.lastResult;
    action.updatedAt = Date.now();
}

async function startGotoPosition(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);

    const entityRef = entityRefFromParams(params);
    const hasEntity = hasEntityRef(entityRef);
    const hasCoords = params.x !== undefined || params.y !== undefined || params.z !== undefined;
    if (hasEntity && hasCoords) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "entity ref and coordinates are mutually exclusive",
        });
    }
    if (hasEntity) return startGotoEntityPosition(ctx, params, action, entityRef);

    const target = targetFromParams(params);
    const timeoutMs = navigationTimeoutMs(params, action);
    const goal = goalFromTarget(bot, target);
    action.runtimeState.cancelled = false;
    action.runtimeState.mode = "coordinate";
    action.runtimeState.target = target;
    action.runtimeState.lastResult = {
        ok: true,
        moving: true,
        reached: false,
        target,
        position: positionToPlain(bot.entity.position),
    };
    action.result = action.runtimeState.lastResult;
    if (timeoutMs) {
        action.runtimeState.timeoutHandle = setTimeout(() => {
            finishGotoFailure(ctx, action, "timeout", "navigation timeout", { target });
        }, timeoutMs);
    }

    try {
        const gotoPromise = bot.pathfinder.goto(goal);
        action.runtimeState.gotoPromise = Promise.resolve(gotoPromise)
            .then(() => finishGotoSuccess(ctx, action, {
                reached: true,
                reason: "target_reached",
                target,
            }))
            .catch((err) => finishGotoFailure(
                ctx,
                action,
                err.code || "navigation_failed",
                err.message || "navigation failed",
                { target },
            ));
    } catch (err) {
        finishGotoFailure(
            ctx,
            action,
            err.code || "navigation_failed",
            err.message || "navigation failed",
            { target },
        );
    }
    return action.result;
}

async function stopGotoPosition(ctx, reason, action) {
    action.runtimeState.cancelled = true;
    clearGotoRuntime(action);
    stopGotoPath(ctx.bot);
    const target = action.runtimeState.mode === "entity"
        ? action.runtimeState.targetInfo || null
        : action.runtimeState.target || null;
    const result = {
        ok: true,
        stopped: true,
        reason,
        target,
        position: positionToPlain(ctx.bot.entity.position),
    };
    action.runtimeState.lastResult = result;
    action.result = result;
    action.updatedAt = Date.now();
    return result;
}

function gotoPositionStatus(ctx, action) {
    if (gotoActionRunning(action) && action.runtimeState.mode === "coordinate") {
        const target = action.runtimeState.target;
        if (target && distanceTo(ctx.bot, target) <= target.range + 0.5) {
            finishGotoSuccess(ctx, action, {
                reached: true,
                reason: "target_reached",
                target,
            });
        }
    } else if (gotoActionRunning(action) && action.runtimeState.mode === "entity") {
        tickGotoEntityPosition(ctx, action);
    }

    const result = action.runtimeState.lastResult || action.result || {};
    return {
        ...result,
        ok: action.status === "failed" ? false : result.ok !== false,
        status: action.status,
        moving: action.status === "running",
        position: positionToPlain(ctx.bot.entity.position),
    };
}

async function gotoEntityPosition(ctx, params, action, entityRef) {
    const bot = ctx.bot;
    const entity = findEntityByRef(bot, entityRef);
    if (!entity) {
        fail(action, "entity_not_found", {
            ok: false,
            error_code: "entity_not_found",
            requested: entityRef,
        });
    }

    const range = Math.max(0, numberParam(params, "range", 1));
    let timeoutMs = null;
    if (params.timeout !== undefined && params.timeout !== null && params.timeout !== 0) {
        timeoutMs = Number(params.timeout) * 1000;
        if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
            fail(action, "invalid_params", {
                ok: false,
                error_code: "invalid_params",
                reason: "timeout must be a positive number",
            });
        }
    }
    return new Promise((resolve, reject) => {
        let settled = false;
        let interval = null;
        let timeoutHandle = null;

        const cleanup = () => {
            if (interval) clearInterval(interval);
            if (timeoutHandle) clearTimeout(timeoutHandle);
            bot.pathfinder.setGoal(null);
        };

        const finishOk = (result) => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(result);
        };

        const finishFail = (errorCode, result) => {
            if (settled) return;
            settled = true;
            cleanup();
            action.result = result;
            const err = new Error(errorCode);
            err.code = errorCode;
            reject(err);
        };

        interval = setInterval(() => {
            const current = bot.entities[entity.id] || findEntityByRef(bot, entityRef);
            if (!current || current.isValid === false) {
                finishFail("entity_lost", {
                    ok: false,
                    error_code: "entity_lost",
                    reason: "target entity disappeared",
                    requested: entityRef,
                });
                return;
            }
            const distance = distanceToEntity(bot, current);
            if (distance <= range) {
                finishOk({
                    ok: true,
                    reached: true,
                    reason: "target_reached",
                    target: entityToPlain(bot, current),
                    position: positionToPlain(bot.entity.position),
                });
                return;
            }
            const { goals } = bot.mcagentPathfinder;
            bot.pathfinder.setGoal(new goals.GoalFollow(current, range), true);
        }, 200);

        if (timeoutMs) {
            timeoutHandle = setTimeout(() => {
                finishFail("timeout", {
                    ok: false,
                    error_code: "timeout",
                    reason: "navigation timeout while following entity",
                    entity: entityToPlain(bot, bot.entities[entity.id] || entity),
                    position: positionToPlain(bot.entity.position),
                });
            }, timeoutMs);
        }
    });
}

// ---------------------------------------------------------------------------
// follow_position：坐标持续跟随（原行为）
// ---------------------------------------------------------------------------

async function startFollowPosition(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    const target = targetFromParams(params);
    action.runtimeState.target = target;
    bot.pathfinder.setGoal(goalFromTarget(bot, target), true);
    return {
        moving: true,
        target,
        position: positionToPlain(bot.entity.position),
    };
}

async function updateFollowPosition(ctx, patch, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    const target = targetFromParams({ ...action.params, ...patch });
    action.runtimeState.target = target;
    bot.pathfinder.setGoal(goalFromTarget(bot, target), true);
    return {
        moving: true,
        target,
        position: positionToPlain(bot.entity.position),
        updated: true,
    };
}

async function stopFollowPosition(ctx, reason) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    bot.pathfinder.setGoal(null);
    bot.clearControlStates();
    return {
        stopped: true,
        reason,
        position: positionToPlain(bot.entity.position),
    };
}

function followStatus(ctx, action) {
    const bot = ctx.bot;
    const target = action.runtimeState.target || targetFromParams(action.params);
    return {
        moving: Boolean(bot.pathfinder && bot.pathfinder.isMoving && bot.pathfinder.isMoving()),
        target,
        distance: distanceTo(bot, target),
        position: positionToPlain(bot.entity.position),
    };
}

// ---------------------------------------------------------------------------
// follow_entity：实体持续跟随 + 动态修正（GoalFollow 每 tick 重设目标）
// ---------------------------------------------------------------------------

async function startFollowEntity(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    const entityRef = entityRefFromParams(params);
    if (!hasEntityRef(entityRef)) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "entity_id / entity_uuid / entity_name is required",
        });
    }
    const entity = findEntityByRef(bot, entityRef);
    if (!entity) {
        fail(action, "entity_not_found", {
            ok: false,
            error_code: "entity_not_found",
            requested: entityRef,
        });
    }
    const followRange = Math.max(0, numberParam(params, "follow_range", 2.5));
    let rangeConfig;
    try {
        rangeConfig = rangeParamsFromParams(params);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
        });
    }
    const distance = distanceToEntity(bot, entity);
    if (rangeConfig.rangeEnabled && distance > rangeConfig.maxRange) {
        fail(action, "entity_not_in_range", {
            ok: false,
            error_code: "entity_not_in_range",
            entity: entityToPlain(bot, entity),
            distance,
            max_range: rangeConfig.maxRange,
        });
    }

    action.runtimeState.entityId = entity.id;
    action.runtimeState.entityRef = entityRef;
    action.runtimeState.targetInfo = entityToPlain(bot, entity);
    action.runtimeState.interval = setInterval(() => tickFollowEntity(bot, action), 250);
    tickFollowEntity(bot, action);
    return action.result || {
        ok: true,
        moving: true,
        target: action.runtimeState.targetInfo,
        distance,
        position: positionToPlain(bot.entity.position),
    };
}

function tickFollowEntity(bot, action) {
    const followRange = Math.max(0, numberParam(action.params, "follow_range", 2.5));
    let rangeConfig;
    try {
        rangeConfig = rangeParamsFromParams(action.params);
    } catch (err) {
        stopFollowEntityInternal(bot, action);
        action.status = "failed";
        action.error = "invalid_params";
        action.result = { ok: false, error_code: "invalid_params", reason: err.message };
        action.updatedAt = Date.now();
        return;
    }

    const current = bot.entities[action.runtimeState.entityId]
        || (action.runtimeState.entityRef ? findEntityByRef(bot, action.runtimeState.entityRef) : null);
    if (!current || current.isValid === false) {
        stopFollowEntityInternal(bot, action);
        action.status = "completed";
        action.result = {
            ok: true,
            completed: true,
            reason: "entity_lost",
            target: action.runtimeState.targetInfo || null,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }

    const distance = distanceToEntity(bot, current);
    if (rangeConfig.rangeEnabled && distance > rangeConfig.maxRange) {
        stopFollowEntityInternal(bot, action);
        action.status = "completed";
        action.result = {
            ok: true,
            completed: true,
            reason: "entity_out_of_range",
            target: entityToPlain(bot, current),
            distance,
            max_range: rangeConfig.maxRange,
            position: positionToPlain(bot.entity.position),
        };
        action.updatedAt = Date.now();
        return;
    }

    const { goals } = bot.mcagentPathfinder;
    bot.pathfinder.setGoal(new goals.GoalFollow(current, followRange), true);
    action.result = {
        ok: true,
        moving: true,
        target: entityToPlain(bot, current),
        distance,
        position: positionToPlain(bot.entity.position),
    };
    action.updatedAt = Date.now();
}

function stopFollowEntityInternal(bot, action) {
    if (action.runtimeState.interval) {
        clearInterval(action.runtimeState.interval);
        action.runtimeState.interval = null;
    }
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    bot.clearControlStates();
}

async function updateFollowEntity(ctx, patch, action) {
    const merged = { ...action.params, ...patch };
    try {
        if (patch.follow_range !== undefined) {
            Math.max(0, numberParam(merged, "follow_range", 2.5));
        }
        rangeParamsFromParams(merged);
    } catch (err) {
        return { ok: false, error_code: "invalid_params", reason: err.message, updated: false };
    }
    action.params = merged;
    tickFollowEntity(ctx.bot, action);
    return action.result || { ok: true, updated: true, moving: true };
}

async function stopFollowEntity(ctx, reason, action) {
    const bot = ctx.bot;
    stopFollowEntityInternal(bot, action);
    const current = currentFollowEntity(bot, action);
    return {
        ok: true,
        stopped: true,
        reason,
        target: entityToPlain(bot, current) || action.runtimeState.targetInfo || null,
        distance: current ? distanceToEntity(bot, current) : null,
        position: positionToPlain(bot.entity.position),
    };
}

function followEntityStatus(ctx, action) {
    const bot = ctx.bot;
    const target = currentFollowEntity(bot, action);
    return {
        ok: true,
        status: action.status,
        moving: Boolean(action.status === "running" && target && target.isValid !== false),
        target: entityToPlain(bot, target) || action.runtimeState.targetInfo || null,
        distance: target ? distanceToEntity(bot, target) : null,
        position: positionToPlain(bot.entity.position),
        result: action.result || null,
    };
}

function currentFollowEntity(bot, action) {
    return bot.entities[action.runtimeState.entityId]
        || (action.runtimeState.entityRef ? findEntityByRef(bot, action.runtimeState.entityRef) : null)
        || null;
}

// ---------------------------------------------------------------------------
// goto_block：按方块名查找并移动到方块附近（还原旧版 goto 语义）
// ---------------------------------------------------------------------------

function normalizeBlockName(value) {
    return String(value).trim().toLowerCase().replace(/^minecraft:/, "");
}

function findBlockDef(bot, name) {
    const table = bot.registry && bot.registry.blocksByName ? bot.registry.blocksByName : null;
    if (!table) return null;
    if (table[name]) return table[name];
    for (const key of Object.keys(table)) {
        const def = table[key];
        const display = String(def && def.displayName || "").toLowerCase().replace(/\s+/g, "_");
        if (display === name) return def;
    }
    return null;
}

async function gotoBlock(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);
    if (params.block_name === undefined || params.block_name === null || String(params.block_name).trim() === "") {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "block_name is required",
        });
    }
    const blockName = normalizeBlockName(params.block_name);
    const blockDef = findBlockDef(bot, blockName);
    if (!blockDef) {
        fail(action, "block_name_unknown", {
            ok: false,
            error_code: "block_name_unknown",
            block_name: blockName,
        });
    }
    const searchMaxDistance = numberParam(params, "search_max_distance", 48);
    if (searchMaxDistance <= 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "search_max_distance must be a positive number",
        });
    }
    const block = bot.findBlock({ matching: blockDef.id, maxDistance: searchMaxDistance });
    if (!block) {
        fail(action, "block_not_found", {
            ok: false,
            error_code: "block_not_found",
            block_name: blockName,
            searched_distance: searchMaxDistance,
        });
    }

    const range = Math.max(0, numberParam(params, "range", 3));
    const timeoutSeconds = params.timeout === undefined || params.timeout === null
        ? 60
        : Number(params.timeout);
    const target = {
        x: block.position.x + 0.5,
        y: block.position.y + 0.5,
        z: block.position.z + 0.5,
        range,
    };
    const goal = goalFromTarget(bot, target);
    const gotoPromise = bot.pathfinder.goto(goal);
    if (Number.isFinite(timeoutSeconds) && timeoutSeconds > 0) {
        await Promise.race([
            gotoPromise,
            new Promise((_, reject) => setTimeout(() => reject(new Error("navigation timeout")), timeoutSeconds * 1000)),
        ]);
    } else {
        await gotoPromise;
    }
    const blockDistance = block.position.offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position);
    return {
        ok: true,
        found: true,
        block: {
            name: block.name ?? null,
            displayName: block.displayName ?? null,
            position: positionToPlain(block.position),
            distance: blockDistance,
        },
        reached: blockDistance <= range + 0.5,
        target,
        position: positionToPlain(bot.entity.position),
    };
}

// ---------------------------------------------------------------------------
// explore：定向搜寻 / 通用探索
// ---------------------------------------------------------------------------

const EXPLORE_ORDER = ["east", "west", "south", "north", "up", "down"];
const EXPLORE_VECTORS = {
    north: { x: 0, y: 0, z: -1 },
    south: { x: 0, y: 0, z: 1 },
    east: { x: 1, y: 0, z: 0 },
    west: { x: -1, y: 0, z: 0 },
    up: { x: 0, y: 1, z: 0 },
    down: { x: 0, y: -1, z: 0 },
};
const EXPLORE_AIR_BLOCKS = new Set(["air", "cave_air", "void_air"]);
const EXPLORE_UNDERGROUND_MARKERS = ["_ore", "_debris"];

function normalizeExploreTargets(value) {
    const list = Array.isArray(value) ? value : [value];
    if (list.length === 0) {
        throw new Error("target must be a non-empty name or array");
    }
    return list.map((item) => {
        if (typeof item !== "string" || item.trim().length === 0) {
            throw new Error("target entries must be non-empty strings");
        }
        return item.trim().toLowerCase().replace(/^minecraft:/, "");
    });
}

function resolveExploreTargets(bot, names) {
    return names.map((name) => {
        const direct = bot.registry.blocksByName?.[name]
            || bot.registry.blocksByName?.[`minecraft:${name}`];
        if (direct) return { name, id: direct.id };
        for (const [key, value] of Object.entries(bot.registry.blocksByName || {})) {
            if (key.toLowerCase().replace(/^minecraft:/, "") === name) {
                return { name, id: value.id };
            }
        }
        return { name, id: null };
    });
}

function parseExploreDirection(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === "string") {
        const name = value.trim().toLowerCase();
        const vector = EXPLORE_VECTORS[name];
        if (vector) return { name, vector };
        throw new Error("direction must be one of north/south/east/west/up/down or a {x,y,z} vector");
    }
    if (typeof value === "object" && value !== null) {
        const x = Number(value.x ?? 0);
        const y = Number(value.y ?? 0);
        const z = Number(value.z ?? 0);
        if (![x, y, z].every(Number.isFinite)) {
            throw new Error("direction vector must have finite x/y/z");
        }
        if (x === 0 && y === 0 && z === 0) {
            throw new Error("direction vector cannot be 0,0,0");
        }
        const vector = { x: Math.sign(x), y: Math.sign(y), z: Math.sign(z) };
        const name = Object.keys(EXPLORE_VECTORS).find((key) => {
            const v = EXPLORE_VECTORS[key];
            return v.x === vector.x && v.y === vector.y && v.z === vector.z;
        }) || `${vector.x},${vector.y},${vector.z}`;
        return { name, vector };
    }
    throw new Error("direction must be a string name or {x,y,z} vector");
}

function isUndergroundExploreTarget(name) {
    return EXPLORE_UNDERGROUND_MARKERS.some((marker) => name.includes(marker));
}

function explorePositionKey(position) {
    return `${Math.floor(position.x)},${Math.floor(position.y)},${Math.floor(position.z)}`;
}

function exploreDistance(a, b) {
    return Math.sqrt(
        (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
    );
}

async function explore(ctx, params, action) {
    const bot = ctx.bot;
    ensurePathfinder(bot);

    // ---- 参数解析 ----
    let targets = null;
    try {
        if (params?.target !== undefined && params?.target !== null) {
            targets = resolveExploreTargets(bot, normalizeExploreTargets(params.target));
        }
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    if (targets) {
        const unknown = targets.filter((entry) => entry.id === null);
        if (unknown.length > 0) {
            fail(action, "unknown_target", {
                ok: false,
                error_code: "unknown_target",
                targets: unknown.map((entry) => entry.name),
                params: params || {},
            });
        }
    }

    let direction;
    try {
        direction = parseExploreDirection(params?.direction);
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    if (!direction) {
        const underground = targets && targets.some((entry) => isUndergroundExploreTarget(entry.name));
        direction = underground
            ? { name: "down", vector: EXPLORE_VECTORS.down }
            : { name: "east", vector: EXPLORE_VECTORS.east };
    }

    const durationSeconds = Number(params?.duration ?? 60);
    const durationTicks = params?.duration_ticks !== undefined && params?.duration_ticks !== null
        ? Number(params.duration_ticks)
        : null;
    let durationMs;
    if (Number.isFinite(durationTicks) && durationTicks > 0) {
        durationMs = durationTicks * 50;
    } else if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "duration must be a positive number of seconds",
            params: params || {},
        });
    } else {
        durationMs = Math.min(durationSeconds * 1000, 600000);
    }

    const scanInterval = Number(params?.scan_interval ?? 4);
    if (!Number.isFinite(scanInterval) || scanInterval < 1) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "scan_interval must be a positive number",
            params: params || {},
        });
    }
    const step = Math.min(scanInterval, 16);

    const maxDistance = Number(params?.max_distance ?? 128);
    if (!Number.isFinite(maxDistance) || maxDistance <= 0) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "max_distance must be a positive number",
            params: params || {},
        });
    }
    const maxDistanceCap = Math.min(maxDistance, 512);

    const mode = String(params?.type ?? "first").toLowerCase();
    if (!["first", "record"].includes(mode)) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "type must be one of first/record",
            params: params || {},
        });
    }

    // ---- 探索循环 ----
    const startedAt = Date.now();
    const origin = {
        x: bot.entity.position.x,
        y: bot.entity.position.y,
        z: bot.entity.position.z,
    };
    let current = { ...origin };
    const dirVectors = { ...EXPLORE_VECTORS, [direction.name]: direction.vector };
    const order = [direction.name, ...EXPLORE_ORDER.filter((name) => name !== direction.name)];
    const exhausted = new Set();
    const visitedKeys = new Set([explorePositionKey(origin)]);
    const foundByPos = new Map();
    const discoveryByPos = new Map();
    const stuck = [];
    const scanRadius = Math.max(8, Math.min(16, step * 2));
    let steps = 0;
    let distanceTraveled = 0;
    let stopReason = "duration_reached";

    function recordScan(atPosition) {
        if (targets) {
            const ids = targets.map((entry) => entry.id);
            const positions = bot.findBlocks({ matching: ids, maxDistance: scanRadius, count: 64 });
            for (const pos of positions) {
                const block = bot.blockAt(pos);
                if (!block) continue;
                const key = explorePositionKey(pos);
                if (foundByPos.has(key)) continue;
                foundByPos.set(key, {
                    name: block.name ?? null,
                    position: positionToPlain(pos),
                    distance: bot.entity.position.distanceTo(pos.offset(0.5, 0.5, 0.5)),
                });
            }
        } else {
            const blocks = bot.findBlocks({
                matching: (block) => block && !EXPLORE_AIR_BLOCKS.has(block.name),
                maxDistance: scanRadius,
                count: 128,
            });
            for (const pos of blocks) {
                const block = bot.blockAt(pos);
                if (!block) continue;
                const key = explorePositionKey(pos);
                if (discoveryByPos.has(key)) continue;
                discoveryByPos.set(key, block.name || "unknown");
            }
        }
    }

    recordScan(current);
    let timeUp = false;
    while (Date.now() < deadline()) {
        if (targets && mode === "first" && foundByPos.size > 0) {
            stopReason = "target_found";
            break;
        }
        let moved = false;
        const episodeFailed = new Set();
        for (const dirName of order) {
            if (Date.now() >= deadline()) {
                timeUp = true;
                break;
            }
            if (exhausted.has(dirName) || episodeFailed.has(dirName)) continue;
            const vector = dirVectors[dirName];
            const next = {
                x: current.x + vector.x * step,
                y: current.y + vector.y * step,
                z: current.z + vector.z * step,
            };
            if (exploreDistance(origin, next) > maxDistanceCap) {
                exhausted.add(dirName);
                continue;
            }
            const goal = new bot.mcagentPathfinder.goals.GoalNear(next.x, next.y, next.z, 1);
            try {
                await withTimeout(bot.pathfinder.goto(goal), 8000, "explore-step");
                bot.clearControlStates();
            } catch (err) {
                bot.pathfinder.setGoal(null);
                episodeFailed.add(dirName);
                stuck.push({
                    position: positionToPlain(current),
                    direction: dirName,
                    reason: err.message,
                });
                if (stuck.length > 20) stuck.shift();
                if (episodeFailed.size >= 4) break;
                continue;
            }
            distanceTraveled += step;
            steps += 1;
            current = next;
            visitedKeys.add(explorePositionKey(current));
            moved = true;
            break;
        }
        if (timeUp) {
            stopReason = "duration_reached";
            break;
        }
        if (!moved) {
            stopReason = exhausted.size >= order.length ? "max_distance_reached" : "stuck";
            break;
        }
    }
    recordScan(current);
    if (targets && mode === "first" && foundByPos.size > 0) {
        stopReason = "target_found";
    }

    // ---- 结果 ----
    const found = Array.from(foundByPos.values()).slice(0, 50);
    const discoveries = {};
    for (const name of discoveryByPos.values()) {
        discoveries[name] = (discoveries[name] || 0) + 1;
    }
    const elapsedMs = Date.now() - startedAt;
    const base = {
        ok: true,
        reason: stopReason,
        mode,
        direction: direction.name,
        duration_ms: elapsedMs,
        blocks_moved: steps,
        distance_traveled: distanceTraveled,
        visited_count: visitedKeys.size,
        found,
        discoveries,
        stuck,
        position: positionToPlain(bot.entity.position),
    };

    if (targets && mode === "first" && found.length === 0) {
        fail(action, "target_not_found", {
            ...base,
            ok: false,
            error_code: "target_not_found",
        });
    }
    return base;

    function deadline() {
        return startedAt + durationMs;
    }
}

module.exports = {
    name: "navigation",
    commands: {
        goto_position: {
            execute: gotoPosition,
            start: startGotoPosition,
            stop: stopGotoPosition,
            status: gotoPositionStatus,
        },
        explore: { execute: explore },
        follow_position: {
            start: startFollowPosition,
            update: updateFollowPosition,
            stop: stopFollowPosition,
            status: followStatus,
        },
        follow_entity: {
            start: startFollowEntity,
            update: updateFollowEntity,
            stop: stopFollowEntity,
            status: followEntityStatus,
        },
        goto_block: { execute: gotoBlock },
    },
    _test: {
        entityMatchesName,
        findEntityByRef,
        normalizeEntityName,
    },
};
