"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const equipment = require(path.join(ROOT, "src", "skills", "equipment", "skill.js"));
const inventory = require(path.join(ROOT, "src", "skills", "inventory", "skill.js"));

function makeBot(items) {
    const slots = new Array(46).fill(null);
    for (const item of items) slots[item.slot] = item;
    const bot = {
        heldItem: null,
        inventory: {
            items: () => items,
            slots,
            emptySlotCount: () => 20,
        },
        getEquipmentDestSlot(destination) {
            return { "off-hand": 45, head: 5, torso: 6, legs: 7, feet: 8 }[destination];
        },
        async equip(item, destination) {
            if (destination === "hand") bot.heldItem = item;
            else bot.inventory.slots[bot.getEquipmentDestSlot(destination)] = item;
        },
    };
    return bot;
}

async function main() {
    const plainSword = { name: "diamond_sword", displayName: "Diamond Sword", type: 1, metadata: 0, slot: 9, count: 1 };
    const enchantedSword = {
        name: "diamond_sword",
        displayName: "Diamond Sword",
        type: 1,
        metadata: 0,
        slot: 10,
        count: 1,
        nbt: { Enchantments: [{ id: "minecraft:sharpness", lvl: 5 }] },
    };
    const bot = makeBot([plainSword, enchantedSword]);

    const result = await equipment.commands.equip_item.execute(
        { bot },
        { slot: 10, destination: "hand" },
        { result: null },
    );
    assert.equal(result.ok, true);
    assert.equal(result.item.slot, 10);
    assert.equal(bot.heldItem, enchantedSword);

    const helmet = { name: "diamond_helmet", displayName: "Diamond Helmet", type: 2, metadata: 0, slot: 11, count: 1 };
    const batchBot = makeBot([helmet]);
    const batch = await equipment.commands.equip_items.execute(
        { bot: batchBot },
        { items: [{ slot: 11 }] },
        { result: null },
    );
    assert.equal(batch.ok, true);
    assert.equal(batch.succeeded[0].item.slot, 11);
    assert.equal(batch.succeeded[0].destination, "head");

    const numericBatchBot = makeBot([helmet]);
    const numericBatch = await equipment.commands.equip_items.execute(
        { bot: numericBatchBot },
        { items: 11 },
        { result: null },
    );
    assert.equal(numericBatch.ok, true);
    assert.equal(numericBatch.succeeded[0].item.slot, 11);

    const inventoryBot = makeBot([plainSword, enchantedSword]);
    const inventoryResult = await inventory.commands.equip.execute(
        { bot: inventoryBot },
        { slot: 10 },
    );
    assert.equal(inventoryResult.equipped, true);
    assert.equal(inventoryResult.item.slot, 10);
    assert.equal(inventoryBot.heldItem, enchantedSword);

    const registryFile = path.join(os.tmpdir(), `equipment-skill-${process.pid}.json`);
    process.env.MC_TASK_CATEGORY = "pvp";
    process.env.EQUIPMENT_REGISTRY_FILE = registryFile;
    try {
        const nameBot = makeBot([plainSword, enchantedSword]);
        nameBot.heldItem = plainSword;
        const nameResult = await equipment.commands.equip_item.execute(
            { bot: nameBot },
            { name: "diamond_sword", destination: "hand" },
            { result: null },
        );
        assert.equal(nameResult.ok, true);
        assert.equal(nameResult.item.slot, 10);
        assert.equal(nameBot.heldItem, enchantedSword);
    } finally {
        delete process.env.MC_TASK_CATEGORY;
        delete process.env.EQUIPMENT_REGISTRY_FILE;
        fs.rmSync(registryFile, { force: true });
        fs.rmSync(`${registryFile}.lock`, { recursive: true, force: true });
    }

    console.log("equipment slot selection tests passed");
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
