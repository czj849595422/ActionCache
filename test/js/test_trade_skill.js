"use strict";

const assert = require("assert").strict;
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const runtimeDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "actioncache-trade-skill-"));
process.env.CHEST_REGISTRY_FILE = path.join(runtimeDirectory, "chests.json");
process.env.TRADE_CONFIG_FILE = path.join(
    ROOT,
    "src",
    "tasks",
    "specs",
    "resources",
    "trade.trade_config.yaml",
);
process.env.TRADE_RUNTIME_DIR = path.join(runtimeDirectory, "runtime");

const trade = require(path.join(ROOT, "src", "skills", "expand", "trade", "skill"));
const containers = require(path.join(ROOT, "src", "skills", "container", "service"));
const crafting = require(path.join(ROOT, "src", "skills", "crafting", "service"));
const yaml = require(path.join(ROOT, "src", "env", "mineflayer", "node_modules", "js-yaml"));

function item(name, count, id) {
    return { name, count, type: id, metadata: 0, stackSize: 64 };
}

function makePosition(x, y, z) {
    return {
        x,
        y,
        z,
        distanceTo(other) {
            return Math.sqrt(
                (this.x - other.x) ** 2
                + (this.y - other.y) ** 2
                + (this.z - other.z) ** 2,
            );
        },
        offset(dx, dy, dz) {
            return makePosition(this.x + dx, this.y + dy, this.z + dz);
        },
    };
}

function createFixture() {
    const definitions = {
        gold_nugget: { id: 1, name: "gold_nugget", stackSize: 64 },
        iron_ingot: { id: 2, name: "iron_ingot", stackSize: 64 },
        cobblestone: { id: 3, name: "cobblestone", stackSize: 64 },
        oak_log: { id: 4, name: "oak_log", stackSize: 64 },
    };
    let inventoryItems = [item("gold_nugget", 64, 1), item("cobblestone", 4, 3)];
    const operations = [];
    const storage = {
        "Alice's_box": [item("iron_ingot", 2, 2)],
        "Alice's_shop": [item("gold_nugget", 5, 1)],
        "Bob's_box": [],
        "Bob's_shop": [item("iron_ingot", 5, 2), item("gold_nugget", 3, 1)],
        "Carol's_box": [],
        "Carol's_shop": [],
        "Dave's_box": [],
        "Dave's_shop": [],
    };
    const windows = {};
    let failCurrencyDeposit = false;
    let failItemReturn = false;

    function add(target, name, count, id) {
        const existing = target.find(entry => entry.name === name);
        if (existing) existing.count += count;
        else target.push(item(name, count, id));
    }

    function remove(target, name, count) {
        const existing = target.find(entry => entry.name === name);
        if (!existing || existing.count < count) throw new Error(`missing ${name}`);
        existing.count -= count;
        if (existing.count === 0) target.splice(target.indexOf(existing), 1);
    }

    function windowFor(name) {
        if (!windows[name]) {
            windows[name] = {
                containerItems: () => storage[name],
                slots: new Array(54).fill(null),
                inventoryStart: 54,
                async withdraw(id, metadata, count) {
                    const definition = Object.values(definitions).find(entry => entry.id === id);
                    operations.push(`withdraw:${definition.name}:${count}`);
                    remove(storage[name], definition.name, count);
                    add(inventoryItems, definition.name, count, id);
                },
                async deposit(id, metadata, count) {
                    const definition = Object.values(definitions).find(entry => entry.id === id);
                    operations.push(`deposit:${definition.name}:${count}`);
                    if (definition.name === "gold_nugget" && failCurrencyDeposit) {
                        throw new Error("controlled payment failure");
                    }
                    if (definition.name === "iron_ingot" && failItemReturn) {
                        throw new Error("controlled item rollback failure");
                    }
                    remove(inventoryItems, definition.name, count);
                    add(storage[name], definition.name, count, id);
                },
            };
        }
        return windows[name];
    }

    const rawConfig = yaml.load(fs.readFileSync(process.env.TRADE_CONFIG_FILE, "utf8"));
    const config = trade._test.normalizeConfig(rawConfig);
    const entries = Object.values(config.agents).flatMap(agent => [agent.personal_box, agent.shop]);
    const bot = {
        username: "Alice",
        entity: { position: makePosition(20, 0, 20) },
        inventory: {
            items: () => inventoryItems,
            emptySlotCount: () => 20,
        },
        registry: { itemsByName: definitions },
        heldItem: null,
        mcagentPathfinder: {
            Movements: class Movements {},
            goals: {
                GoalNear: class GoalNear {
                    constructor(x, y, z, range) {
                        Object.assign(this, { x, y, z, range });
                    }
                },
            },
        },
        pathfinder: {
            setMovements() {},
            async goto(goal) {
                bot.entity.position = makePosition(goal.x, goal.y, goal.z);
            },
        },
        clearControlStates() {},
    };

    return {
        bot,
        config,
        definitions,
        entries,
        operations,
        storage,
        windowFor,
        setInventory(value) { inventoryItems = value; },
        getInventory() { return inventoryItems; },
        setPaymentFailure(value) { failCurrencyDeposit = value; },
        setItemReturnFailure(value) { failItemReturn = value; },
    };
}

async function testPureHelpers(fixture) {
    const requests = trade._test.normalizeTransferItems([
        "minecraft:iron_ingot",
        { name: "iron_ingot", count: 2 },
        { name: "cobblestone" },
    ]);
    assert.deepEqual(requests, [
        { name: "iron_ingot", count: null },
        { name: "cobblestone", count: null },
    ]);
    assert.deepEqual(
        trade._test.resolveTransferCounts(requests, [
            item("iron_ingot", 5, 2),
            item("cobblestone", 3, 3),
        ]),
        {
            resolved: [
                { name: "iron_ingot", count: 5 },
                { name: "cobblestone", count: 3 },
            ],
            missing: [],
        },
    );

    const plan = trade._test.buildCraftPlan(
        fixture.config,
        "golden_sword",
        2,
        { oak_log: 1 },
        null,
    );
    assert.equal(plan.missing.gold_ingot, 4);
    assert.deepEqual(plan.steps.map(step => step.output), ["oak_planks", "stick", "golden_sword"]);

    const bad = structuredClone(
        yaml.load(fs.readFileSync(process.env.TRADE_CONFIG_FILE, "utf8")),
    );
    bad.recipes["minecraft:oak_planks"].ingredients = { "minecraft:stick": 1 };
    assert.throws(() => trade._test.normalizeConfig(bad), /recipe cycle detected/);

    const originalCrafting = {
        resolveItem: crafting.resolveItem,
        queryAllRecipes: crafting.queryAllRecipes,
        aggregateMaterials: crafting.aggregateMaterials,
    };
    try {
        crafting.resolveItem = () => ({ id: 99 });
        crafting.queryAllRecipes = () => [
            {
                result: { count: 1 },
                requiresTable: false,
                materials: [
                    { name: "gold_ingot", count: 2 },
                    { name: "stick", count: 1 },
                ],
            },
            {
                result: { count: 1 },
                requiresTable: true,
                materials: [
                    { name: "gold_ingot", count: 2 },
                    { name: "stick", count: 1 },
                ],
            },
        ];
        crafting.aggregateMaterials = (registry, recipe) => recipe.materials;
        const matched = trade._test.matchingMineflayerRecipe(
            { registry: {} },
            fixture.config.recipes.golden_sword,
        );
        assert.equal(matched.requiresTable, true);
    } finally {
        Object.assign(crafting, originalCrafting);
    }
}

function installContainerMocks(fixture) {
    const originals = {};
    const replacements = {
        loadChestRegistry() {},
        listRegisteredChests() {
            return fixture.entries.map((entry, index) => ({
                id: index + 1,
                name: entry.name,
                position: entry.position,
            }));
        },
        async openChest(ctx, params) {
            return {
                chest: { name: "chest", position: params.chest_name },
                window: fixture.windowFor(params.chest_name),
            };
        },
        async closeWindowAndSync() {},
        inventorySnapshot(bot) {
            return {
                inventory_items: bot.inventory.items().map(entry => ({ ...entry })),
                inventory_empty_slots: bot.inventory.emptySlotCount(),
                heldItem: null,
            };
        },
        inventorySpaceFailures() {
            return { failures: [], required_slots: 0, available_empty_slots: 20 };
        },
        chestSpaceFailures() {
            return { failures: [], required_slots: 0, available_empty_slots: 20 };
        },
        resolveItem(bot, name) {
            return fixture.definitions[String(name).replace(/^minecraft:/, "")];
        },
        blockToPlain(bot, chest) {
            return { name: chest.name, position: chest.position };
        },
    };
    for (const [name, replacement] of Object.entries(replacements)) {
        originals[name] = containers[name];
        containers[name] = replacement;
    }
    return () => {
        for (const [name, original] of Object.entries(originals)) containers[name] = original;
    };
}

async function testActions(fixture) {
    const restore = installContainerMocks(fixture);
    const ctx = { bot: fixture.bot };
    try {
        let action = {};
        const own = await trade.commands.inspect_own_containers.execute(ctx, {}, action);
        assert.equal(own.personal_box.name, "Alice's_box");
        assert.equal(own.shop.name, "Alice's_shop");
        assert.equal(own.personal_box.items[0].count, 2);

        action = {};
        const inspected = await trade.commands.inspect_shop.execute(
            ctx,
            { shop_name: "Bob's_shop" },
            action,
        );
        assert.deepEqual(inspected.sale_items, [{ name: "minecraft:iron_ingot", count: 5 }]);

        action = {};
        const inspectedByOwner = await trade.commands.inspect_shop.execute(
            ctx,
            { shop_name: "Bob" },
            action,
        );
        assert.equal(inspectedByOwner.shop_name, "Bob's_shop");
        assert.equal(inspectedByOwner.owner, "Bob");

        action = {};
        const transferred = await trade.commands.transfer_owned_items.execute(
            ctx,
            { container: "personal_box", direction: "store", items: "cobblestone" },
            action,
        );
        assert.deepEqual(transferred.moved, [{ item: "minecraft:cobblestone", count: 4 }]);
        assert.equal(fixture.storage["Alice's_box"].find(entry => entry.name === "cobblestone").count, 4);

        action = {};
        const transferredByRegisteredName = await trade.commands.take_from_container.execute(
            ctx,
            { container: "Alice's_box", items: "cobblestone" },
            action,
        );
        assert.deepEqual(transferredByRegisteredName.moved, [{ item: "minecraft:cobblestone", count: 4 }]);

        action = {};
        await assert.rejects(
            trade.commands.take_from_container.execute(
                ctx,
                { container: "Bob's_box", items: "iron_ingot" },
                action,
            ),
            /container_not_owned/,
        );
        assert.equal(action.result.error_code, "container_not_owned");

        action = {};
        await assert.rejects(
            trade.commands.transfer_owned_items.execute(
                ctx,
                { container: "shop", direction: "store", items: "gold_nugget" },
                action,
            ),
            /item_not_sellable/,
        );
        assert.equal(action.result.error_code, "item_not_sellable");

        action = {};
        const taken = await trade.commands.take_from_container.execute(
            ctx,
            { container: "personal_box", items: "iron_ingot" },
            action,
        );
        assert.deepEqual(taken.moved, [{ item: "minecraft:iron_ingot", count: 2 }]);

        action = {};
        const stocked = await trade.commands.store_to_container.execute(
            ctx,
            { container: "shop", items: { name: "iron_ingot", count: 2 } },
            action,
        );
        assert.deepEqual(stocked.moved, [{ item: "minecraft:iron_ingot", count: 2 }]);

        action = {};
        await assert.rejects(
            trade.commands.store_to_container.execute(
                ctx,
                { container: "shop", items: "gold_nugget" },
                action,
            ),
            /item_not_sellable/,
        );
        assert.equal(action.result.error_code, "item_not_sellable");

        fixture.bot.entity.position = makePosition(-1, 0, -4);
        fixture.operations.length = 0;
        action = {};
        const purchased = await trade.commands.buy_from_shop.execute(
            ctx,
            { shop_name: "Bob's_shop", item: "iron_ingot", count: 2 },
            action,
        );
        assert.deepEqual(fixture.operations, ["withdraw:iron_ingot:2", "deposit:gold_nugget:16"]);
        assert.equal(purchased.total_price, 16);
        assert.equal(purchased.buyer_currency_remaining, 48);
        assert.equal(purchased.shop_stock_remaining, 3);
        let records = fs.readFileSync(
            path.join(process.env.TRADE_RUNTIME_DIR, "trade_transactions.jsonl"),
            "utf8",
        ).trim().split(/\r?\n/).map(line => JSON.parse(line));
        assert.deepEqual(records.slice(-2).map(record => record.state), ["prepared", "committed"]);

        fixture.storage["Bob's_shop"] = [item("iron_ingot", 5, 2), item("gold_nugget", 3, 1)];
        fixture.setInventory([item("gold_nugget", 64, 1)]);
        fixture.setPaymentFailure(true);
        fixture.operations.length = 0;
        action = {};
        await assert.rejects(
            trade.commands.buy_from_shop.execute(
                ctx,
                { shop_name: "Bob's_shop", item: "iron_ingot", count: 1 },
                action,
            ),
            /purchase_failed/,
        );
        assert.equal(action.result.error_code, "purchase_failed");
        assert.deepEqual(fixture.operations, [
            "withdraw:iron_ingot:1",
            "deposit:gold_nugget:8",
            "deposit:iron_ingot:1",
        ]);
        assert.equal(fixture.storage["Bob's_shop"].find(entry => entry.name === "iron_ingot").count, 5);
        records = fs.readFileSync(
            path.join(process.env.TRADE_RUNTIME_DIR, "trade_transactions.jsonl"),
            "utf8",
        ).trim().split(/\r?\n/).map(line => JSON.parse(line));
        assert.deepEqual(records.slice(-2).map(record => record.state), ["prepared", "rolled_back"]);

        fixture.storage["Bob's_shop"] = [item("iron_ingot", 5, 2), item("gold_nugget", 3, 1)];
        fixture.setInventory([item("gold_nugget", 64, 1)]);
        fixture.setItemReturnFailure(true);
        fixture.operations.length = 0;
        action = {};
        await assert.rejects(
            trade.commands.buy_from_shop.execute(
                ctx,
                { shop_name: "Bob's_shop", item: "iron_ingot", count: 1 },
                action,
            ),
            /purchase_inconsistent/,
        );
        assert.equal(action.result.error_code, "purchase_inconsistent");
        assert.equal(action.result.rollback_errors[0].operation, "return_item");
    } finally {
        restore();
    }
}

async function testShopLock() {
    const releaseFirst = await trade._test.acquireShopLock("Carol's_shop");
    let secondAcquired = false;
    const waiting = trade._test.acquireShopLock("Carol's_shop").then(release => {
        secondAcquired = true;
        return release;
    });
    await new Promise(resolve => setTimeout(resolve, 80));
    assert.equal(secondAcquired, false);
    releaseFirst();
    const releaseSecond = await waiting;
    assert.equal(secondAcquired, true);
    releaseSecond();
}

async function testRecursiveRuntimeDiscovery() {
    const { loadSkills } = require(path.join(
        ROOT,
        "src",
        "env",
        "mineflayer",
        "skill_runtime",
        "skill_loader",
    ));
    const { SkillRuntime } = require(path.join(
        ROOT,
        "src",
        "env",
        "mineflayer",
        "skill_runtime",
        "runtime",
    ));
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "actioncache-skill-loader-"));
    const nested = path.join(directory, "expand", "sample");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(
        path.join(nested, "skill.json"),
        JSON.stringify({
            name: "sample",
            version: "1",
            runtime: "javascript",
            entry: "skill.js",
            commands: {
                public: { mode: ["oneshot"] },
                failure: { mode: ["oneshot"] },
                hidden: { mode: ["oneshot"] },
            },
        }),
    );
    fs.writeFileSync(
        path.join(nested, "skill.js"),
        "module.exports={commands:{public:{execute:async()=>({ok:true})},failure:{execute:async()=>({ok:false,error_code:'expected_failure'})},hidden:{execute:async()=>({ok:true})}}};",
    );
    assert.deepEqual(Object.keys(loadSkills(directory)), ["sample"]);

    const runtime = new SkillRuntime({
        skillsDir: directory,
        getBot: () => ({}),
        getPerception: () => null,
        allowedActions: ["sample.public", "sample.failure"],
        internalActions: ["sample.hidden"],
    });
    assert.deepEqual(runtime.list().skills.sample.commands, ["public", "failure"]);
    assert.equal((await runtime.execute({ action: "sample.hidden" })).status, "failed");
    assert.equal((await runtime.execute({ action: "sample.hidden", internal: true })).status, "completed");
    const failedResult = await runtime.execute({ action: "sample.failure" });
    assert.equal(failedResult.status, "failed");
    assert.equal(failedResult.error, "expected_failure");

    const duplicate = path.join(directory, "other");
    fs.mkdirSync(duplicate);
    fs.copyFileSync(path.join(nested, "skill.json"), path.join(duplicate, "skill.json"));
    fs.copyFileSync(path.join(nested, "skill.js"), path.join(duplicate, "skill.js"));
    assert.throws(() => loadSkills(directory), /Duplicate skill name 'sample'/);
    fs.rmSync(directory, { recursive: true, force: true });
}

async function main() {
    const fixture = createFixture();
    await testPureHelpers(fixture);
    await testActions(fixture);
    await testShopLock();
    await testRecursiveRuntimeDiscovery();
    console.log("trade skill JS tests passed");
}

main()
    .finally(() => fs.rmSync(runtimeDirectory, { recursive: true, force: true }))
    .catch(err => {
        console.error(err.stack || err);
        process.exitCode = 1;
    });
