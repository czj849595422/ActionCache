"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const registryPath = path.join(os.tmpdir(), `mcagent-equipment-${process.pid}.json`);
process.env.MC_TASK_CATEGORY = "pvp";
process.env.EQUIPMENT_REGISTRY_FILE = registryPath;
try { fs.rmSync(registryPath, { force: true }); } catch (_) {}
try { fs.rmSync(`${registryPath}.lock`, { recursive: true, force: true }); } catch (_) {}

const registry = require(path.resolve(__dirname, "..", "..", "src", "env", "mineflayer", "equipment_registry.js"));
const equipment = require(path.resolve(__dirname, "..", "..", "src", "skills", "equipment", "skill.js"));

function sword(slot, enchanted = false, durabilityUsed = 0) {
    return {
        name: "diamond_sword",
        displayName: "Diamond Sword",
        slot,
        count: 1,
        metadata: 0,
        maxDurability: 1561,
        durabilityUsed,
        nbt: enchanted ? { Enchantments: [{ id: "sharpness", lvl: 5 }] } : null,
    };
}

async function main() {
    const plain = sword(9);
    const enchanted = sword(10, true, 12);
    const plainRecord = registry.annotate("Alice", plain, "inventory");
    const enchantedRecord = registry.annotate("Alice", enchanted, "inventory");

    assert.equal(plainRecord.identifier, "diamond_sword_1");
    assert.equal(enchantedRecord.identifier, "diamond_sword_2");
    assert.equal(enchantedRecord.enchantments[0].id, "sharpness");
    assert.equal(enchantedRecord.registryDurabilityUsed, 12);

    enchanted.nbt = { Enchantments: [{ id: "sharpness", lvl: 5 }, { id: "unbreaking", lvl: 3 }] };
    const updatedEnchantedRecord = registry.annotate("Alice", enchanted, "inventory");
    assert.equal(updatedEnchantedRecord.identifier, enchantedRecord.identifier);
    assert.equal(updatedEnchantedRecord.enchantments.length, 2);

    const selected = registry.resolve(
        "Alice",
        "diamond_sword",
        [plain, enchanted],
        (item, input) => item.name === input,
    );
    assert.equal(selected, enchanted);
    assert.equal(registry.resolve("Alice", plainRecord.identifier, [plain, enchanted], () => false), plain);

    const slots = new Array(46).fill(null);
    slots[9] = plain;
    slots[10] = enchanted;
    const bot = {
        username: "Alice",
        heldItem: null,
        inventory: {
            slots,
            items: () => [plain, enchanted],
            emptySlotCount: () => 20,
        },
        getEquipmentDestSlot: (destination) => ({ "off-hand": 45, head: 5, torso: 6, legs: 7, feet: 8 }[destination]),
        async equip(item, destination) {
            if (destination === "hand") bot.heldItem = item;
        },
    };
    const equipped = await equipment.commands.equip_item.execute(
        { bot },
        { item: enchantedRecord.identifier, destination: "hand" },
        { result: null },
    );
    assert.equal(equipped.item.identifier, enchantedRecord.identifier);
    assert.equal(bot.heldItem, enchanted);

    registry.markTransfer("Alice", enchanted, "Bob");
    const dropped = sword(4, true, 13);
    dropped.nbt = { Enchantments: [{ id: "sharpness", lvl: 5 }, { id: "unbreaking", lvl: 3 }] };
    const droppedRecord = registry.annotate(null, dropped, "dropped");
    assert.equal(droppedRecord.identifier, enchantedRecord.identifier);
    const received = sword(4, true, 13);
    received.nbt = { Enchantments: [{ id: "sharpness", lvl: 5 }, { id: "unbreaking", lvl: 3 }] };
    const receivedRecord = registry.annotate("Bob", received, "inventory");
    assert.equal(receivedRecord.identifier, enchantedRecord.identifier);
    assert.equal(receivedRecord.owner, "Bob");
    assert.equal(receivedRecord.registryDurabilityUsed, 13);

    fs.mkdirSync(`${registryPath}.lock`);
    assert.deepEqual(registry.annotate("Carol", sword(3), "inventory"), {});
    fs.rmSync(`${registryPath}.lock`, { recursive: true, force: true });

    process.env.MC_TASK_CATEGORY = "trade";
    assert.deepEqual(registry.annotate("Carol", sword(2), "inventory"), {});
    assert.equal(fs.existsSync(registryPath), true);
    console.log("equipment registry tests passed");
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
}).finally(() => {
    try { fs.rmSync(registryPath, { force: true }); } catch (_) {}
    try { fs.rmSync(`${registryPath}.lock`, { recursive: true, force: true }); } catch (_) {}
});
