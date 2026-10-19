"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const combat = require(path.join(ROOT, "src", "skills", "combat", "skill.js"));
const inventory = require(path.join(ROOT, "src", "skills", "inventory", "skill.js"));
process.env.MINING_ORE_BLOCKS = "iron_ore";
const mining = require(path.join(ROOT, "src", "skills", "expand", "mining", "skill.js"));
const blockSkill = require(path.join(ROOT, "src", "skills", "block", "skill.js"));

const TARGET_ERROR = "目标未找到或位于观察范围之外";
const COMBAT_TARGET_ERROR = "目标未找到或位于观察范围之外，请先靠近目标";
const MISSING_TARGET_ERROR = "目标未找到";

function makePosition(x, y, z) {
    return {
        x,
        y,
        z,
        distanceTo(other) {
            return Math.hypot(this.x - other.x, this.y - other.y, this.z - other.z);
        },
        offset(dx, dy, dz) {
            return makePosition(this.x + dx, this.y + dy, this.z + dz);
        },
    };
}

function makeAction() {
    return { params: {}, runtimeState: {} };
}

function makeCombatBot(targetPosition, targetType = "hostile", weaponName = null) {
    const self = { id: 1, position: makePosition(0, 0, 0) };
    const target = {
        id: 2,
        name: targetType === "player" ? "Steve" : "zombie",
        username: targetType === "player" ? "Steve" : undefined,
        type: targetType,
        isValid: true,
        position: makePosition(...targetPosition),
    };
    const inventoryItems = weaponName
        ? [{ name: weaponName, displayName: weaponName, count: 1 }, { name: "arrow", count: 1 }]
        : [];
    const goalCalls = [];
    const attackCalls = [];
    const bot = {
        entity: self,
        entities: { 2: target },
        players: {},
        nearestEntity(predicate) {
            return Object.values(this.entities).find(predicate) || null;
        },
        pathfinder: {
            setMovements() {},
            setGoal(...args) {
                goalCalls.push(args);
            },
        },
        mcagentPathfinder: { goals: { GoalFollow: class GoalFollow {} } },
        mcagentDefaultMovements: {},
        heldItem: inventoryItems[0] || null,
        inventory: { items: () => inventoryItems },
        clearControlStates() {},
        lookAt: async () => {},
        activateItem() {},
        deactivateItem() {},
        attack(...args) {
            attackCalls.push(args);
        },
        goalCalls,
        attackCalls,
    };
    return bot;
}

async function testCombatObserveReturnsRadiusMetadata() {
    const bot = makeCombatBot([3, 0, 0]);
    bot.entities = {};
    const result = await combat.commands.observe.execute(
        { bot, perceptionRadius: 10 },
        {},
        makeAction(),
    );
    assert.equal(result.ok, true);
    assert.equal(result.base_radius, 10);
    assert.equal(result.radius, 20);
}

async function assertScopeFailure(
    command,
    ctx,
    params,
    expectedError = TARGET_ERROR,
    expectedCode = "target_not_found_or_outside_observe_range",
) {
    const action = makeAction();
    await assert.rejects(
        command(ctx, params, action),
        (error) => error.message === expectedError,
    );
    assert.equal(action.result.reason, expectedError);
    assert.equal(action.result.error_code, expectedCode);
}

async function testCombatRange() {
    const ctx = { bot: makeCombatBot([3, 0, 0]), perceptionRadius: 10 };
    const action = makeAction();
    const result = await combat.commands.fight.start(ctx, { target: { entity_id: 2 } }, action);
    assert.equal(result.ok, true);
    await combat.commands.fight.stop(ctx, "test", action);

    const distantHostileBot = makeCombatBot([41, 0, 0]);
    const distantHostileAction = makeAction();
    const distantHostileResult = await combat.commands.fight.start(
        { bot: distantHostileBot, perceptionRadius: 10 },
        { target: { entity_id: 2 } },
        distantHostileAction,
    );
    assert.equal(distantHostileResult.ok, true);
    await combat.commands.fight.stop(
        { bot: distantHostileBot, perceptionRadius: 10 },
        "test",
        distantHostileAction,
    );

    const rangedBot = makeCombatBot([50, 0, 0], "hostile", "bow");
    const rangedAction = makeAction();
    const rangedResult = await combat.commands.fight.start(
        { bot: rangedBot, perceptionRadius: 10 },
        { weapon: "bow", target: { entity_id: 2 }, shoot_range: 40 },
        rangedAction,
    );
    assert.equal(rangedResult.ok, true);
    assert.equal(rangedResult.phase, "approaching");
    assert.equal(rangedBot.goalCalls.length, 1);
    assert.equal(rangedBot.attackCalls.length, 0);
    await combat.commands.fight.stop(
        { bot: rangedBot, perceptionRadius: 10 },
        "test",
        rangedAction,
    );

    const defaultRangeBot = makeCombatBot([39, 0, 0], "player");
    const defaultRangeAction = makeAction();
    const defaultRangeResult = await combat.commands.fight.start(
        { bot: defaultRangeBot },
        { target: { entity_id: 2 } },
        defaultRangeAction,
    );
    assert.equal(defaultRangeResult.ok, true);
    await combat.commands.fight.stop(
        { bot: defaultRangeBot },
        "test",
        defaultRangeAction,
    );

    await assertScopeFailure(
        combat.commands.fight.start,
        { bot: makeCombatBot([41, 0, 0], "player"), perceptionRadius: 10 },
        { target: { entity_id: 2 } },
        COMBAT_TARGET_ERROR,
    );
}

async function testPickRange() {
    const ctx = {
        bot: {
            entity: { position: makePosition(0, 0, 0) },
            entities: {},
        },
        perceptionRadius: 10,
    };
    await assertScopeFailure(
        inventory.commands.pick.start,
        ctx,
        { positions: [{ x: 21, y: 0, z: 0 }] },
    );
    await assertScopeFailure(
        inventory.commands.pick.start,
        ctx,
        { positions: [{ x: 1, y: 0, z: 0 }] },
    );
}

async function testMiningRangeAndMissingTarget() {
    const baseCtx = {
        bot: {
            entity: { position: makePosition(0, 0, 0) },
            blockAt: () => null,
        },
        perceptionRadius: 10,
    };
    await assertScopeFailure(
        mining.commands.mine_iron.start,
        baseCtx,
        { positions: [{ x: 21, y: 0, z: 0 }] },
        MISSING_TARGET_ERROR,
        "target_not_found",
    );
    await assertScopeFailure(
        mining.commands.mine_iron.start,
        baseCtx,
        { positions: [{ x: 1, y: 0, z: 0 }] },
        MISSING_TARGET_ERROR,
        "target_not_found",
    );
}

async function testMiningFallsBackToThreeDimensionalNeighbor() {
    const originalMineGroupOnce = blockSkill.mineService.mineGroupOnce;
    const seeds = [];
    blockSkill.mineService.mineGroupOnce = async (bot, seed) => {
        seeds.push(seed);
        return { mined_count: 1, skipped_count: 0, obstacles_broken: 0 };
    };
    try {
        const bot = {
            entity: { position: makePosition(0, 0, 0) },
            blockAt(position) {
                const key = `${Math.floor(position.x)},${Math.floor(position.y)},${Math.floor(position.z)}`;
                if (key === "3,1,0") return { name: "iron_ore", harvestTools: {} };
                return { name: "air", harvestTools: {} };
            },
            clearControlStates() {},
            pathfinder: { setGoal() {} },
        };
        const action = makeAction();
        const result = await mining.commands.mine_iron.start(
            { bot, perceptionRadius: 10 },
            { positions: [{ x: 2, y: 0, z: 0 }] },
            action,
        );
        await action.runtimeState.worker;
        assert.equal(result.ok, true);
        assert.deepEqual(seeds, [{ x: 3, y: 1, z: 0 }]);
    } finally {
        blockSkill.mineService.mineGroupOnce = originalMineGroupOnce;
    }
}

async function main() {
    await testCombatObserveReturnsRadiusMetadata();
    await testCombatRange();
    await testPickRange();
    await testMiningRangeAndMissingTarget();
    await testMiningFallsBackToThreeDimensionalNeighbor();
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
