"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const navigation = require(path.join(ROOT, "src", "skills", "navigation", "skill.js"));

function makePosition(x, y, z) {
    return {
        x,
        y,
        z,
        distanceTo(other) {
            return Math.hypot(this.x - other.x, this.y - other.y, this.z - other.z);
        },
    };
}

function makeBot() {
    const self = {
        id: 1,
        username: "Aria",
        name: "player",
        displayName: "Aria",
        position: makePosition(0, 0, 0),
    };
    const faye = {
        id: 2,
        username: "Faye",
        name: "player",
        displayName: "Faye",
        position: makePosition(3, 0, 0),
    };
    const grey = {
        id: 3,
        username: "Grey",
        name: "player",
        displayName: "Grey",
        position: makePosition(4, 0, 0),
    };
    const zombie = {
        id: 4,
        name: "zombie",
        displayName: "Zombie",
        position: makePosition(6, 0, 0),
    };
    const bot = {
        entity: self,
        entities: { 3: grey, 4: zombie },
        players: {
            Aria: { entity: self, displayName: "Aria" },
            Faye: { entity: faye, displayName: "Faye" },
        },
    };
    bot.nearestEntity = (predicate) => (
        Object.values(bot.entities).find((entity) => predicate(entity)) || null
    );
    return { bot, faye, grey, zombie };
}

async function main() {
    const manifest = JSON.parse(fs.readFileSync(
        path.join(ROOT, "src", "skills", "navigation", "skill.json"),
        "utf8",
    ));
    assert.deepEqual(manifest.commands.goto_position.mode, ["continuous"]);

    assert.equal(navigation._test.normalizeEntityName("minecraft:Zombie"), "zombie");

    const { bot, faye, grey, zombie } = makeBot();
    assert.equal(navigation._test.findEntityByRef(bot, { name: "Faye" }), faye);
    assert.equal(navigation._test.findEntityByRef(bot, { name: "faye" }), faye);
    assert.equal(navigation._test.findEntityByRef(bot, { name: "Grey" }), grey);
    assert.equal(navigation._test.findEntityByRef(bot, { name: "minecraft:zombie" }), zombie);
    assert.equal(navigation._test.findEntityByRef(bot, { name: "Aria" }), null);

    bot.pathfinder = {
        setMovements() {},
        setGoal(goal) {
            if (goal && goal.entity) {
                bot.entity.position = makePosition(
                    goal.entity.position.x,
                    goal.entity.position.y,
                    goal.entity.position.z,
                );
            }
        },
    };
    bot.mcagentPathfinder = {
        goals: {
            GoalFollow: class {
                constructor(entity, range) {
                    this.entity = entity;
                    this.range = range;
                }
            },
        },
    };
    bot.mcagentDefaultMovements = {};
    const result = await navigation.commands.goto_position.execute(
        { bot },
        {
            entity_name: "Faye",
            range: 1,
            range_enabled: true,
            max_range: 1,
        },
        {},
    );
    assert.equal(result.ok, true);
    assert.equal(result.reached, true);

    let resolveGoto;
    const continuousBot = {
        entity: { position: makePosition(0, 0, 0) },
        entities: {},
        pathfinder: {
            goal: null,
            setMovements() {},
            setGoal(goal) {
                this.goal = goal;
            },
            goto(goal) {
                this.goal = goal;
                return new Promise((resolve) => {
                    resolveGoto = resolve;
                });
            },
        },
        mcagentPathfinder: {
            goals: {
                GoalNear: class {
                    constructor(x, y, z, range) {
                        this.x = x;
                        this.y = y;
                        this.z = z;
                        this.range = range;
                    }
                },
            },
        },
        mcagentDefaultMovements: {},
        clearControlStates() {},
    };
    const continuousAction = { status: "running", runtimeState: {} };
    const startResult = await navigation.commands.goto_position.start(
        { bot: continuousBot },
        { x: 8, y: 0, z: 0 },
        continuousAction,
    );
    assert.equal(startResult.moving, true);
    assert.equal(continuousAction.status, "running");
    assert.ok(continuousBot.pathfinder.goal);

    resolveGoto();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(continuousAction.status, "completed");
    assert.equal(continuousAction.result.reason, "target_reached");

    let ignoredResolve;
    const stoppableBot = {
        ...continuousBot,
        pathfinder: {
            ...continuousBot.pathfinder,
            goto(goal) {
                this.goal = goal;
                return new Promise((resolve) => {
                    ignoredResolve = resolve;
                });
            },
        },
    };
    const stoppableAction = { status: "running", runtimeState: {} };
    await navigation.commands.goto_position.start(
        { bot: stoppableBot },
        { x: 8, y: 0, z: 0 },
        stoppableAction,
    );
    const stopResult = await navigation.commands.goto_position.stop(
        { bot: stoppableBot },
        "planning_interrupt",
        stoppableAction,
    );
    assert.equal(stopResult.stopped, true);
    assert.equal(stoppableBot.pathfinder.goal, null);
    ignoredResolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(stoppableAction.status, "running");

    const entityTarget = {
        id: 2,
        name: "zombie",
        position: makePosition(3, 0, 0),
        isValid: true,
    };
    const entityBot = {
        entity: { position: makePosition(0, 0, 0) },
        entities: { 2: entityTarget },
        players: {},
        nearestEntity(predicate) {
            return Object.values(this.entities).find(predicate) || null;
        },
        pathfinder: {
            goal: null,
            setMovements() {},
            setGoal(goal) {
                this.goal = goal;
            },
        },
        mcagentPathfinder: {
            goals: {
                GoalFollow: class {
                    constructor(entity, range) {
                        this.entity = entity;
                        this.range = range;
                    }
                },
            },
        },
        mcagentDefaultMovements: {},
        clearControlStates() {},
    };
    const entityAction = { status: "running", runtimeState: {} };
    await navigation.commands.goto_position.start(
        { bot: entityBot },
        { entity_id: 2, range: 1 },
        entityAction,
    );
    assert.equal(entityAction.status, "running");
    entityBot.entity.position = makePosition(3, 0, 0);
    await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(entityAction.status, "completed");
    assert.equal(entityAction.result.reason, "target_reached");
    assert.equal(entityBot.pathfinder.goal, null);
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
