"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const combat = require(path.join(ROOT, "src", "skills", "combat", "skill.js"));
const hawkEyeTypes = require(path.join(
    ROOT,
    "src",
    "env",
    "mineflayer",
    "node_modules",
    "minecrafthawkeye",
    "dist",
    "types",
    "index.js",
));

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

function makeBot() {
    const bow = { name: "bow", count: 1 };
    const arrows = { name: "arrow", count: 8 };
    const target = {
        id: 2,
        name: "zombie",
        type: "hostile",
        height: 1.95,
        isValid: true,
        position: makePosition(10, 0, 0),
    };
    const events = new Map();
    let autoAttackCalls = 0;
    let stopCalls = 0;
    let autoAttackTarget = null;

    const bot = {
        username: "combat_test",
        entity: { id: 1, position: makePosition(0, 0, 0) },
        entities: { 2: target },
        players: {},
        heldItem: bow,
        inventory: { items: () => [bow, arrows] },
        pathfinder: { setMovements() {}, setGoal() {} },
        mcagentPathfinder: {
            Movements: class Movements {
                constructor() {}
            },
            goals: { GoalFollow: class GoalFollow {} },
        },
        clearControlStates() {},
        deactivateItem() {},
        on(name, handler) {
            events.set(name, handler);
        },
        removeListener(name, handler) {
            if (events.get(name) === handler) events.delete(name);
        },
        hawkEye: {
            autoAttack(targetToAttack) {
                autoAttackCalls += 1;
                autoAttackTarget = targetToAttack;
                return true;
            },
            stop() {
                stopCalls += 1;
            },
        },
        nearestEntity(predicate) {
            return Object.values(this.entities).find(predicate) || null;
        },
    };

    return {
        bot,
        get autoAttackCalls() {
            return autoAttackCalls;
        },
        get stopCalls() {
            return stopCalls;
        },
        get autoAttackTarget() {
            return autoAttackTarget;
        },
    };
}

async function main() {
    assert.equal(hawkEyeTypes.weaponsProps.bow.waitTime, 1000);

    const fixture = makeBot();
    const action = { params: {}, runtimeState: {} };
    const result = await combat.commands.fight.start(
        { bot: fixture.bot, perceptionRadius: 20 },
        { weapon: "bow", target: { entity_id: 2 }, max_duration_ms: 5000 },
        action,
    );

    assert.equal(result.combat_type, "ranged");
    assert.equal(result.engine, "hawkEye");
    assert.equal(fixture.autoAttackCalls, 1);
    assert.notEqual(fixture.autoAttackTarget, fixture.bot.entities[2]);
    assert.equal(fixture.autoAttackTarget.height, 1.95 / 2);
    assert.equal(fixture.autoAttackTarget.position, fixture.bot.entities[2].position);

    fixture.bot.entities[2].isValid = false;
    assert.equal(fixture.autoAttackTarget.isValid, false);

    await combat.commands.fight.stop(
        { bot: fixture.bot },
        "test",
        action,
    );
    assert.equal(fixture.stopCalls, 1);

    const bowFixture = makeBot();
    const bowAction = { params: {}, runtimeState: {} };
    const bowResult = await combat.commands.fight_using_bow.start(
        { bot: bowFixture.bot, perceptionRadius: 20 },
        { bow: "bow", target: { entity_id: 2 }, max_duration_ms: 5000 },
        bowAction,
    );

    assert.equal(bowResult.engine, "hawkEye");
    assert.equal(bowFixture.autoAttackCalls, 1);
    assert.equal(bowFixture.autoAttackTarget.height, 1.95 / 2);
    assert.equal(bowFixture.autoAttackTarget.position, bowFixture.bot.entities[2].position);

    await combat.commands.fight_using_bow.stop(
        { bot: bowFixture.bot },
        "test",
        bowAction,
    );
    assert.equal(bowFixture.stopCalls, 1);
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
