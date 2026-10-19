"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const combat = require(path.join(ROOT, "src", "skills", "combat", "skill.js"));
const consumables = require(path.join(ROOT, "src", "skills", "consumable_utils.js"));
const potionType = 373;

function entity(name, id) {
    return {
        name,
        id,
        isValid: true,
        position: { x: id, y: 0, z: 0 },
    };
}

function makeBot(entities) {
    return {
        entities,
        players: {},
        nearestEntity(predicate) {
            return Object.values(entities).find(predicate) || null;
        },
    };
}

function makeInventory(items) {
    const inventory = new EventEmitter();
    inventory.slots = Array(46).fill(null);
    for (const item of items) {
        inventory.slots[item.slot] = item;
    }
    inventory.items = () => inventory.slots.filter(Boolean);
    inventory.updateSlot = (slot, item) => {
        const oldItem = inventory.slots[slot] || null;
        if (item) item.slot = slot;
        inventory.slots[slot] = item;
        inventory.emit("updateSlot", slot, oldItem, item);
    };
    return inventory;
}

async function main() {
    const zombie8 = entity("zombie", 8);
    const zombie10 = entity("zombie", 10);
    const skeleton9 = entity("skeleton", 9);
    const bot = makeBot({ 8: zombie8, 9: skeleton9, 10: zombie10 });

    assert.equal(
        combat._test.getEntityByTarget(bot, {
            name: "zombie_8",
            entity_id: "zombie_8",
        }),
        zombie8,
    );
    assert.equal(
        combat._test.getEntityByTarget(bot, {
            name: "zombie_8",
            entity_id: "skeleton_9",
        }),
        zombie8,
    );
    assert.equal(
        combat._test.getEntityByTarget(bot, {
            name: "creeper_77",
            entity_id: "skeleton_9",
        }),
        skeleton9,
    );
    assert.equal(
        combat._test.getEntityByTarget(bot, { name: "zombie", entity_id: 8 }),
        zombie8,
    );
    assert.equal(
        combat._test.getEntityByTarget(bot, {
            name: "creeper_77",
            entity_id: "spider_99",
        }),
        null,
    );
    assert.equal(
        combat._test.getEntityByTarget(bot, {
            name: "zombie_77",
            entity_id: "zombie_99",
        }),
        null,
    );

    const healingPotion = {
        name: "potion",
        displayName: "Potion",
        type: potionType,
        metadata: 0,
        nbt: { Potion: "minecraft:healing" },
    };
    assert.equal(
        consumables.itemMatchesConsumable(healingPotion, "potion[healing]"),
        true,
    );

    const bread = { name: "bread", type: 297, count: 1, slot: 36 };
    const inventory = makeInventory([bread]);
    const client = new EventEmitter();
    let consumeCalls = 0;
    const consumeBot = {
        inventory,
        _client: client,
        entity: { id: 1 },
        get heldItem() {
            return inventory.slots[36];
        },
        consume() {
            consumeCalls += 1;
            return new Promise((resolve, reject) => {
                setTimeout(() => inventory.updateSlot(36, { name: "bread", type: 297, count: 2 }), 5);
                setTimeout(() => inventory.updateSlot(36, { name: "bread", type: 297, count: 1 }), 10);
                setTimeout(() => reject(new Error("consume timed out")), 15);
            });
        },
        deactivateItem() {},
    };
    const consumeResult = await consumables.consumeWithRetry(consumeBot, "bread");
    assert.equal(consumeResult.ok, true);
    assert.equal(consumeResult.consumed, true);
    assert.equal(consumeResult.consume_attempts, 1);
    assert.equal(consumeResult.consume_confirmation, "inventory_slot_decreased");
    assert.equal(consumeCalls, 1);

    const lateBread = { name: "bread", type: 297, count: 1, slot: 36 };
    const lateInventory = makeInventory([lateBread]);
    let lateConsumeCalls = 0;
    const lateBot = {
        inventory: lateInventory,
        entity: { id: 1 },
        get heldItem() {
            return lateInventory.slots[36];
        },
        consume() {
            lateConsumeCalls += 1;
            setTimeout(
                () => lateInventory.updateSlot(36, { name: "bread", type: 297, count: 0 }),
                1050,
            );
            return Promise.reject(new Error("consume timed out"));
        },
        deactivateItem() {},
    };
    const lateResult = await consumables.consumeWithRetry(lateBot, "bread");
    assert.equal(lateResult.ok, true);
    assert.equal(lateResult.consume_attempts, 1);
    assert.equal(lateConsumeCalls, 1);

    console.log("combat target and consumable tests passed");
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
