"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const yaml = require("../../../env/mineflayer/node_modules/js-yaml");

const containers = require("../../container/service");
const crafting = require("../../crafting/service");

const DEFAULT_MAX_DISTANCE = 16;
const DEFAULT_OPEN_TIMEOUT_MS = 5000;
const LOCK_TIMEOUT_MS = 5000;
const STALE_LOCK_MS = 30000;

let configCache = null;

function normalizeItemName(value, field = "item") {
    if (typeof value !== "string" || !value.trim()) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function namespaced(name) {
    return `minecraft:${normalizeItemName(name)}`;
}

function positiveInteger(value, field, fallback = null) {
    const selected = value === undefined || value === null ? fallback : value;
    const parsed = Number(selected);
    if (!Number.isInteger(parsed) || parsed < 1) {
        throw new Error(`${field} must be a positive integer`);
    }
    return parsed;
}

function parsePosition(value, field) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error(`${field} must contain x, y, z`);
    }
    const result = { x: Number(value.x), y: Number(value.y), z: Number(value.z) };
    if (!Object.values(result).every(Number.isInteger)) {
        throw new Error(`${field} must contain integer x, y, z`);
    }
    return result;
}

function parseItemCountMap(value, field) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error(`${field} must be an item-count mapping`);
    }
    const result = {};
    for (const [item, count] of Object.entries(value)) {
        const normalized = normalizeItemName(item, `${field}.${item}`);
        if (Object.prototype.hasOwnProperty.call(result, normalized)) {
            throw new Error(`${field} contains duplicate item ${normalized}`);
        }
        result[normalized] = positiveInteger(
            count,
            `${field}.${item}`,
        );
    }
    if (Object.keys(result).length === 0) {
        throw new Error(`${field} must not be empty`);
    }
    return result;
}

function positionKey(position) {
    return `${position.x},${position.y},${position.z}`;
}

function sameItemCountMap(left, right) {
    const leftEntries = Object.entries(left).sort(([a], [b]) => a.localeCompare(b));
    const rightEntries = Object.entries(right).sort(([a], [b]) => a.localeCompare(b));
    return JSON.stringify(leftEntries) === JSON.stringify(rightEntries);
}

function validateRecipeGraph(recipes, prices) {
    const resolvedDepths = new Map();
    const visiting = new Set();

    function visit(output) {
        if (resolvedDepths.has(output)) return resolvedDepths.get(output);
        if (visiting.has(output)) throw new Error(`recipe cycle detected at ${output}`);
        const recipe = recipes[output];
        visiting.add(output);
        let dependencyDepth = 0;
        for (const ingredient of Object.keys(recipe.ingredients)) {
            if (!prices[ingredient]) {
                throw new Error(`recipe ingredient ${ingredient} has no configured price`);
            }
            if (recipes[ingredient]) {
                dependencyDepth = Math.max(dependencyDepth, visit(ingredient));
            }
        }
        visiting.delete(output);
        const depth = dependencyDepth + 1;
        if (recipe.depth !== depth) {
            throw new Error(`recipe ${output} depth mismatch: expected ${depth}, actual ${recipe.depth}`);
        }
        resolvedDepths.set(output, depth);
        return depth;
    }

    for (const output of Object.keys(recipes)) {
        if (!prices[output]) throw new Error(`recipe output ${output} has no configured price`);
        visit(output);
    }
}

function recipeClosure(recipes, targets) {
    const outputs = new Set();
    function visit(item) {
        const recipe = recipes[item];
        if (!recipe || outputs.has(item)) return;
        outputs.add(item);
        for (const ingredient of Object.keys(recipe.ingredients)) visit(ingredient);
    }
    for (const target of Object.keys(targets)) visit(target);
    return outputs;
}

function normalizeCraftingSteps(value, agentName, recipes, targets) {
    if (!Array.isArray(value) || value.length === 0) {
        throw new Error(`${agentName}.crafting_steps must be a non-empty array`);
    }
    const steps = [];
    const seen = new Set();
    for (const [index, rawStep] of value.entries()) {
        if (!rawStep || typeof rawStep !== "object" || Array.isArray(rawStep)) {
            throw new Error(`${agentName}.crafting_steps[${index}] must be an object`);
        }
        const output = normalizeItemName(rawStep.output, `${agentName}.crafting_steps[${index}].output`);
        if (seen.has(output)) throw new Error(`${agentName}.crafting_steps duplicates ${output}`);
        const recipe = recipes[output];
        if (!recipe) throw new Error(`${agentName}.crafting_steps references unknown recipe ${output}`);
        const ingredients = parseItemCountMap(
            rawStep.ingredients,
            `${agentName}.crafting_steps[${index}].ingredients`,
        );
        const step = {
            output,
            batches: positiveInteger(rawStep.batches, `${agentName}.crafting_steps[${index}].batches`),
            output_count: positiveInteger(
                rawStep.output_count,
                `${agentName}.crafting_steps[${index}].output_count`,
            ),
            ingredients,
            depth: positiveInteger(rawStep.depth, `${agentName}.crafting_steps[${index}].depth`),
            order: index,
        };
        if (
            step.output_count !== recipe.output_count
            || step.depth !== recipe.depth
            || !sameItemCountMap(step.ingredients, recipe.ingredients)
        ) {
            throw new Error(`${agentName}.crafting_steps[${index}] does not match recipe ${output}`);
        }
        for (const ingredient of Object.keys(step.ingredients)) {
            if (recipes[ingredient] && !seen.has(ingredient)) {
                throw new Error(
                    `${agentName}.crafting_steps is not topological: ${ingredient} must precede ${output}`,
                );
            }
        }
        seen.add(output);
        steps.push(step);
    }
    const expected = recipeClosure(recipes, targets);
    if (expected.size !== seen.size || Array.from(expected).some(item => !seen.has(item))) {
        throw new Error(`${agentName}.crafting_steps does not cover exactly the assigned target recipes`);
    }
    return steps;
}

function normalizeConfig(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        throw new Error("trade config must be an object");
    }
    if (Number(raw.version) !== 1) {
        throw new Error("trade config version must be 1");
    }
    const currency = raw.currency || {};
    const currencyItem = normalizeItemName(currency.item, "currency.item");
    const currencyValue = positiveInteger(currency.unit_value, "currency.unit_value");
    const prices = parseItemCountMap(raw.prices, "prices");

    const recipes = {};
    for (const [output, value] of Object.entries(raw.recipes || {})) {
        if (!value || typeof value !== "object") {
            throw new Error(`recipes.${output} must be an object`);
        }
        const outputName = normalizeItemName(output);
        if (recipes[outputName]) throw new Error(`duplicate normalized recipe output: ${outputName}`);
        recipes[outputName] = {
            output: outputName,
            output_count: positiveInteger(value.output_count, `${output}.output_count`),
            ingredients: parseItemCountMap(value.ingredients, `${output}.ingredients`),
            requires_table: Boolean(value.requires_table),
            depth: positiveInteger(value.depth, `${output}.depth`),
        };
    }
    if (Object.keys(recipes).length === 0) throw new Error("trade config must contain recipes");
    validateRecipeGraph(recipes, prices);

    if (!Array.isArray(raw.agents) || raw.agents.length !== 4) {
        throw new Error("trade config must contain four agents");
    }
    const agents = {};
    const shops = {};
    const containerNames = new Set();
    const containerPositions = new Set();
    for (const [index, value] of raw.agents.entries()) {
        if (!value || typeof value !== "object") {
            throw new Error(`agents[${index}] must be an object`);
        }
        const name = String(value.name || "").trim();
        if (!name || agents[name]) {
            throw new Error(`agents[${index}].name is empty or duplicated`);
        }
        const personal = value.personal_chest || {};
        const shop = value.shop || {};
        const targets = {};
        for (const target of value.targets || []) {
            const item = normalizeItemName(target.item, `${name}.targets.item`);
            if (Object.prototype.hasOwnProperty.call(targets, item)) {
                throw new Error(`${name} has duplicate target ${item}`);
            }
            targets[item] = positiveInteger(target.count, `${name}.targets.${item}`);
        }
        if (Object.keys(targets).length === 0) {
            throw new Error(`${name} must have targets`);
        }
        const agent = {
            name,
            profession: String(value.profession || "").trim(),
            spawn: parsePosition(value.spawn, `${name}.spawn`),
            personal_box: {
                name: String(personal.name || "").trim(),
                position: parsePosition(personal.position, `${name}.personal_chest.position`),
            },
            shop: {
                name: String(shop.name || "").trim(),
                position: parsePosition(shop.position, `${name}.shop.position`),
                physical_blocks: Array.isArray(shop.physical_blocks)
                    ? shop.physical_blocks.map((position, blockIndex) => parsePosition(
                        position,
                        `${name}.shop.physical_blocks[${blockIndex}]`,
                    ))
                    : [],
            },
            targets,
        };
        if (agent.personal_box.name !== `${name}'s_box`) {
            throw new Error(`${name} personal box name does not match owner`);
        }
        if (agent.shop.name !== `${name}'s_shop`) {
            throw new Error(`${name} shop name does not match owner`);
        }
        if (
            agent.shop.physical_blocks.length !== 2
            || new Set(agent.shop.physical_blocks.map(positionKey)).size !== 2
            || !agent.shop.physical_blocks.some(position => samePosition(position, agent.shop.position))
        ) {
            throw new Error(`${name} shop physical blocks do not match its logical position`);
        }
        for (const target of Object.keys(targets)) {
            const recipe = recipes[target];
            if (!recipe || !prices[target]) {
                throw new Error(`${name} target ${target} has no configured recipe or price`);
            }
            if (![1, 2, 3].includes(recipe.depth)) {
                throw new Error(`${name} target ${target} must have a depth 1-3 recipe`);
            }
        }
        agent.crafting_steps = normalizeCraftingSteps(
            value.crafting_steps,
            name,
            recipes,
            targets,
        );
        for (const container of [agent.personal_box, agent.shop]) {
            if (containerNames.has(container.name)) {
                throw new Error(`duplicate container name: ${container.name}`);
            }
            const key = positionKey(container.position);
            if (containerPositions.has(key)) {
                throw new Error(`duplicate logical container position: ${key}`);
            }
            containerNames.add(container.name);
            containerPositions.add(key);
        }
        agents[name] = agent;
        if (shops[agent.shop.name]) {
            throw new Error(`duplicate shop name: ${agent.shop.name}`);
        }
        shops[agent.shop.name] = { ...agent.shop, owner: name };
    }
    const expectedAgents = ["Alice", "Bob", "Carol", "Dave"];
    if (expectedAgents.some(name => !agents[name])) {
        throw new Error("trade config agents must be Alice through Dave");
    }
    return {
        version: 1,
        currency: { item: currencyItem, unit_value: currencyValue },
        crafting_table: parsePosition(raw.crafting_table, "crafting_table"),
        prices,
        recipes,
        agents,
        shops,
    };
}

function validateParams(params, allowedKeys, requiredKeys, action) {
    const value = params === undefined || params === null ? {} : params;
    if (typeof value !== "object" || Array.isArray(value)) {
        containers.fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: "params must be an object",
        });
    }
    const unknown = Object.keys(value).filter(key => !allowedKeys.includes(key));
    const missing = requiredKeys.filter(key => value[key] === undefined || value[key] === null);
    if (unknown.length > 0 || missing.length > 0) {
        containers.fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            unknown,
            missing,
        });
    }
    return value;
}

function loadTradeConfig() {
    const configured = process.env.TRADE_CONFIG_FILE;
    if (!configured) {
        throw new Error("TRADE_CONFIG_FILE is not configured");
    }
    const filePath = path.resolve(configured);
    const stat = fs.statSync(filePath);
    if (configCache && configCache.path === filePath && configCache.mtimeMs === stat.mtimeMs) {
        return configCache.value;
    }
    const raw = yaml.load(fs.readFileSync(filePath, "utf8"));
    const value = normalizeConfig(raw);
    configCache = { path: filePath, mtimeMs: stat.mtimeMs, value };
    return value;
}

function actionContext(ctx, action) {
    let config;
    try {
        config = loadTradeConfig();
    } catch (err) {
        containers.fail(action, "trade_config_invalid", {
            ok: false,
            error_code: "trade_config_invalid",
            reason: err.message,
        });
    }
    const username = String(ctx.bot?.username || "").trim();
    const agent = config.agents[username];
    if (!agent) {
        containers.fail(action, "trade_agent_unknown", {
            ok: false,
            error_code: "trade_agent_unknown",
            username,
            configured_agents: Object.keys(config.agents),
        });
    }
    return { config, agent, username };
}

function samePosition(left, right) {
    return left && right && left.x === right.x && left.y === right.y && left.z === right.z;
}

function distanceTo(bot, position) {
    const current = bot.entity?.position;
    if (!current) return Infinity;
    const dx = current.x - position.x;
    const dy = current.y - position.y;
    const dz = current.z - position.z;
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

async function moveNearPosition(bot, position, range, action) {
    if (distanceTo(bot, position) <= range + 0.75) return;
    if (!bot.pathfinder || !bot.mcagentPathfinder) {
        containers.fail(action, "pathfinder_unavailable", {
            ok: false,
            error_code: "pathfinder_unavailable",
            position,
        });
    }
    if (!bot.mcagentDefaultMovements) {
        const { Movements } = bot.mcagentPathfinder;
        bot.mcagentDefaultMovements = new Movements(bot);
    }
    bot.pathfinder.setMovements(bot.mcagentDefaultMovements);
    const { goals } = bot.mcagentPathfinder;
    try {
        await bot.pathfinder.goto(new goals.GoalNear(position.x, position.y, position.z, range));
        bot.clearControlStates();
    } catch (err) {
        containers.fail(action, "trade_navigation_failed", {
            ok: false,
            error_code: "trade_navigation_failed",
            position,
            reason: err.message,
        });
    }
}

function registeredEntry(bot, configured, action) {
    containers.loadChestRegistry(bot);
    const entry = containers.listRegisteredChests(bot).find(item => item.name === configured.name);
    if (!entry || !samePosition(entry.position, configured.position)) {
        containers.fail(action, "trade_container_not_registered", {
            ok: false,
            error_code: "trade_container_not_registered",
            container_name: configured.name,
            expected_position: configured.position,
            registered: entry || null,
        });
    }
    return entry;
}

async function prepareContainer(bot, configured, action) {
    registeredEntry(bot, configured, action);
    await moveNearPosition(bot, configured.position, 2, action);
    registeredEntry(bot, configured, action);
}

function runtimeDirectory() {
    const configured = process.env.TRADE_RUNTIME_DIR;
    if (!configured) throw new Error("TRADE_RUNTIME_DIR is not configured");
    const directory = path.resolve(configured);
    fs.mkdirSync(directory, { recursive: true });
    return directory;
}

function lockPath(shopName) {
    const digest = crypto.createHash("sha256").update(shopName).digest("hex").slice(0, 20);
    return path.join(runtimeDirectory(), `shop-${digest}.lock`);
}

async function acquireShopLock(shopName) {
    const target = lockPath(shopName);
    const token = crypto.randomUUID();
    const deadline = Date.now() + LOCK_TIMEOUT_MS;
    while (Date.now() <= deadline) {
        try {
            const fd = fs.openSync(target, "wx");
            try {
                fs.writeFileSync(fd, JSON.stringify({ token, pid: process.pid, created_at: Date.now() }), "utf8");
                fs.fsyncSync(fd);
            } finally {
                fs.closeSync(fd);
            }
            return () => {
                try {
                    const current = JSON.parse(fs.readFileSync(target, "utf8"));
                    if (current.token === token) fs.unlinkSync(target);
                } catch (err) {
                    if (err.code !== "ENOENT") throw err;
                }
            };
        } catch (err) {
            if (err.code !== "EEXIST") throw err;
            try {
                const stat = fs.statSync(target);
                if (Date.now() - stat.mtimeMs > STALE_LOCK_MS) {
                    fs.unlinkSync(target);
                    continue;
                }
            } catch (statError) {
                if (statError.code === "ENOENT") continue;
                throw statError;
            }
            await containers.sleep(50);
        }
    }
    const err = new Error(`timed out waiting for shop lock: ${shopName}`);
    err.code = "shop_busy";
    throw err;
}

async function withConfiguredContainer(ctx, configured, action, useLock, callback) {
    await prepareContainer(ctx.bot, configured, action);
    let release = null;
    if (useLock) {
        try {
            release = await acquireShopLock(configured.name);
        } catch (err) {
            containers.fail(action, err.code || "shop_lock_failed", {
                ok: false,
                error_code: err.code || "shop_lock_failed",
                shop_name: configured.name,
                reason: err.message,
            });
        }
    }
    let window = null;
    try {
        registeredEntry(ctx.bot, configured, action);
        const opened = await containers.openChest(
            ctx,
            {
                chest_name: configured.name,
                max_distance: DEFAULT_MAX_DISTANCE,
                open_timeout_ms: DEFAULT_OPEN_TIMEOUT_MS,
            },
            action,
        );
        window = opened.window;
        const result = await callback(opened.chest, window);
        await containers.closeWindowAndSync(ctx.bot, window);
        window = null;
        return result;
    } finally {
        if (window) await containers.closeWindowAndSync(ctx.bot, window);
        if (release) release();
    }
}

function aggregateItems(items, excluded = new Set()) {
    const counts = {};
    for (const item of items || []) {
        if (!item || typeof item.name !== "string") continue;
        const name = normalizeItemName(item.name);
        if (excluded.has(name)) continue;
        counts[name] = (counts[name] || 0) + Number(item.count || 0);
    }
    return Object.entries(counts)
        .filter(([, count]) => count > 0)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([name, count]) => ({ name: namespaced(name), count }));
}

function countItem(items, itemName) {
    const target = normalizeItemName(itemName);
    return (items || []).reduce((total, item) => (
        item && typeof item.name === "string" && normalizeItemName(item.name) === target
            ? total + Number(item.count || 0)
            : total
    ), 0);
}

function containerResult(configured, window) {
    return {
        name: configured.name,
        position: configured.position,
        items: aggregateItems(window.containerItems()),
    };
}

async function inspectConfigured(ctx, configured, action, useLock) {
    return withConfiguredContainer(ctx, configured, action, useLock, async (chest, window) => ({
        ...containerResult(configured, window),
        chest: containers.blockToPlain(ctx.bot, chest),
    }));
}

async function inspectOwnContainers(ctx, params, action) {
    validateParams(params, [], [], action);
    const { agent } = actionContext(ctx, action);
    await moveNearPosition(ctx.bot, agent.spawn, 1, action);
    const personalBox = await inspectConfigured(ctx, agent.personal_box, action, false);
    const shop = await inspectConfigured(ctx, agent.shop, action, true);
    return {
        ok: true,
        spawn: agent.spawn,
        personal_box: personalBox,
        shop,
        ...containers.inventorySnapshot(ctx.bot),
    };
}

function configuredShop(config, shopName, action) {
    if (typeof shopName !== "string" || !shopName.trim()) {
        containers.fail(action, "invalid_shop_name", {
            ok: false,
            error_code: "invalid_shop_name",
            shop_name: shopName ?? null,
        });
    }
    const requested = shopName.trim();
    const shop = config.shops[requested]
        || (config.agents[requested]
            ? config.shops[config.agents[requested].shop.name]
            : null);
    if (!shop) {
        containers.fail(action, "shop_unknown", {
            ok: false,
            error_code: "shop_unknown",
            shop_name: requested,
        });
    }
    return shop;
}

function configuredOwnContainer(agent, value, action) {
    if (typeof value !== "string" || !value.trim()) {
        containers.fail(action, "invalid_container", {
            ok: false,
            error_code: "invalid_container",
            container: value ?? null,
        });
    }
    const requested = value.trim();
    if (requested === "personal_box" || requested === agent.personal_box.name) {
        return { key: "personal_box", configured: agent.personal_box };
    }
    if (requested === "shop" || requested === agent.shop.name) {
        return { key: "shop", configured: agent.shop };
    }
    containers.fail(action, "container_not_owned", {
        ok: false,
        error_code: "container_not_owned",
        container: requested,
        owner: agent.name,
        allowed: ["personal_box", "shop", agent.personal_box.name, agent.shop.name],
    });
}

async function inspectShop(ctx, params, action) {
    params = validateParams(params, ["shop_name"], ["shop_name"], action);
    const { config } = actionContext(ctx, action);
    const shop = configuredShop(config, params?.shop_name, action);
    return withConfiguredContainer(ctx, shop, action, true, async (chest, window) => ({
        ok: true,
        shop_name: shop.name,
        owner: shop.owner,
        position: shop.position,
        chest: containers.blockToPlain(ctx.bot, chest),
        sale_items: aggregateItems(window.containerItems(), new Set([config.currency.item])),
    }));
}

function normalizeTransferItems(value) {
    const rows = Array.isArray(value) ? value : [value];
    if (rows.length === 0 || rows.some(row => row === undefined || row === null)) {
        throw new Error("items must contain at least one item");
    }
    const combined = new Map();
    for (const [index, row] of rows.entries()) {
        let name;
        let count = null;
        if (typeof row === "string") {
            name = normalizeItemName(row, `items[${index}]`);
        } else if (row && typeof row === "object" && !Array.isArray(row)) {
            const unknown = Object.keys(row).filter(key => !["name", "count"].includes(key));
            if (unknown.length > 0) {
                throw new Error(`items[${index}] has unknown fields: ${unknown.join(", ")}`);
            }
            name = normalizeItemName(row.name, `items[${index}].name`);
            if (row.count !== undefined && row.count !== null) {
                count = positiveInteger(row.count, `items[${index}].count`);
            }
        } else {
            throw new Error(`items[${index}] must be an item name or object`);
        }
        const previous = combined.get(name);
        if (previous === null || count === null) {
            combined.set(name, null);
        } else {
            combined.set(name, (previous || 0) + count);
        }
    }
    return Array.from(combined, ([name, count]) => ({ name, count }));
}

function resolveTransferCounts(requests, availableItems) {
    const resolved = [];
    const missing = [];
    for (const request of requests) {
        const available = countItem(availableItems, request.name);
        const count = request.count === null ? available : request.count;
        if (count < 1 || available < count) {
            missing.push({
                name: namespaced(request.name),
                required: request.count,
                available,
                missing: request.count === null ? null : Math.max(0, request.count - available),
            });
        } else {
            resolved.push({ name: request.name, count });
        }
    }
    return { resolved, missing };
}

async function rollbackTransfers(bot, window, direction, moved) {
    const errors = [];
    for (const request of [...moved].reverse()) {
        const item = containers.resolveItem(bot, request.name);
        try {
            if (direction === "take") {
                await window.deposit(item.id, null, request.count);
            } else {
                await window.withdraw(item.id, null, request.count);
            }
        } catch (err) {
            errors.push({ item: namespaced(request.name), count: request.count, reason: err.message });
        }
    }
    return errors;
}

async function transferOwnedItems(ctx, params, action) {
    params = validateParams(
        params,
        ["container", "direction", "items"],
        ["container", "direction", "items"],
        action,
    );
    const { config, agent } = actionContext(ctx, action);
    const requestedContainer = params?.container;
    const direction = params?.direction;
    if (!["take", "store"].includes(direction)) {
        containers.fail(action, "invalid_direction", {
            ok: false,
            error_code: "invalid_direction",
            direction: direction ?? null,
        });
    }
    const containerSelection = configuredOwnContainer(agent, requestedContainer, action);
    const containerName = containerSelection.key;
    let requests;
    try {
        requests = normalizeTransferItems(params?.items);
    } catch (err) {
        containers.fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
            items: params?.items ?? null,
        });
    }
    if (containerName === "shop" && direction === "store") {
        const forbidden = requests
            .filter(request => request.name === config.currency.item || !config.prices[request.name])
            .map(request => namespaced(request.name));
        if (forbidden.length > 0) {
            containers.fail(action, "item_not_sellable", {
                ok: false,
                error_code: "item_not_sellable",
                items: forbidden,
            });
        }
    }

    const configured = containerSelection.configured;
    return withConfiguredContainer(ctx, configured, action, containerName === "shop", async (chest, window) => {
        const sourceItems = direction === "take" ? window.containerItems() : ctx.bot.inventory.items();
        const resolution = resolveTransferCounts(requests, sourceItems);
        if (resolution.missing.length > 0) {
            containers.fail(action, direction === "take" ? "container_item_missing" : "inventory_item_missing", {
                ok: false,
                error_code: direction === "take" ? "container_item_missing" : "inventory_item_missing",
                missing: resolution.missing,
                container: containerResult(configured, window),
                ...containers.inventorySnapshot(ctx.bot),
            });
        }
        const capacity = direction === "take"
            ? containers.inventorySpaceFailures(ctx.bot, resolution.resolved)
            : containers.chestSpaceFailures(ctx.bot, window, resolution.resolved);
        if (capacity.failures.length > 0) {
            containers.fail(action, direction === "take" ? "inventory_space_insufficient" : "container_space_insufficient", {
                ok: false,
                error_code: direction === "take" ? "inventory_space_insufficient" : "container_space_insufficient",
                capacity,
                container: containerResult(configured, window),
            });
        }

        const moved = [];
        try {
            for (const request of resolution.resolved) {
                const item = containers.resolveItem(ctx.bot, request.name);
                if (!item) throw new Error(`unknown item: ${request.name}`);
                if (direction === "take") {
                    await window.withdraw(item.id, null, request.count);
                } else {
                    await window.deposit(item.id, null, request.count);
                }
                moved.push(request);
            }
        } catch (err) {
            const rollbackErrors = await rollbackTransfers(ctx.bot, window, direction, moved);
            containers.fail(action, rollbackErrors.length > 0 ? "transfer_inconsistent" : "transfer_failed", {
                ok: false,
                error_code: rollbackErrors.length > 0 ? "transfer_inconsistent" : "transfer_failed",
                reason: err.message,
                moved: moved.map(item => ({ item: namespaced(item.name), count: item.count })),
                rollback_errors: rollbackErrors,
                container: containerResult(configured, window),
                ...containers.inventorySnapshot(ctx.bot),
            });
        }
        return {
            ok: true,
            direction,
            container: containerName,
            moved: moved.map(item => ({ item: namespaced(item.name), count: item.count })),
            container_contents: containerResult(configured, window),
            chest: containers.blockToPlain(ctx.bot, chest),
            ...containers.inventorySnapshot(ctx.bot),
        };
    });
}

function auditPath() {
    return path.join(runtimeDirectory(), "trade_transactions.jsonl");
}

function appendAudit(record) {
    const fd = fs.openSync(auditPath(), "a");
    try {
        fs.writeSync(fd, `${JSON.stringify({ ...record, recorded_at: new Date().toISOString() })}\n`);
        fs.fsyncSync(fd);
    } finally {
        fs.closeSync(fd);
    }
}

function bestEffortAudit(record) {
    try {
        appendAudit(record);
        return null;
    } catch (err) {
        return err.message;
    }
}

async function rollbackPurchase(bot, window, itemDef, currencyDef, itemCount, paidCount) {
    const errors = [];
    if (paidCount > 0) {
        try {
            await window.withdraw(currencyDef.id, null, paidCount);
        } catch (err) {
            errors.push({ operation: "refund_currency", count: paidCount, reason: err.message });
        }
    }
    if (itemCount > 0) {
        try {
            await window.deposit(itemDef.id, null, itemCount);
        } catch (err) {
            errors.push({ operation: "return_item", count: itemCount, reason: err.message });
        }
    }
    return errors;
}

async function buyFromShop(ctx, params, action) {
    params = validateParams(params, ["shop_name", "item", "count"], ["shop_name", "item"], action);
    const { config, username } = actionContext(ctx, action);
    const shop = configuredShop(config, params?.shop_name, action);
    if (shop.owner === username) {
        containers.fail(action, "cannot_buy_from_own_shop", {
            ok: false,
            error_code: "cannot_buy_from_own_shop",
            shop_name: shop.name,
        });
    }
    let itemName;
    let count;
    try {
        itemName = normalizeItemName(params?.item);
        count = positiveInteger(params?.count, "count", 1);
    } catch (err) {
        containers.fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
        });
    }
    const unitPrice = config.prices[itemName];
    if (!unitPrice || itemName === config.currency.item) {
        containers.fail(action, "item_not_sellable", {
            ok: false,
            error_code: "item_not_sellable",
            item: namespaced(itemName),
        });
    }
    const totalPrice = unitPrice * count;
    const itemDef = containers.resolveItem(ctx.bot, itemName);
    const currencyDef = containers.resolveItem(ctx.bot, config.currency.item);
    if (!itemDef || !currencyDef) {
        containers.fail(action, "unknown_item", {
            ok: false,
            error_code: "unknown_item",
            item: !itemDef ? namespaced(itemName) : namespaced(config.currency.item),
        });
    }

    return withConfiguredContainer(ctx, shop, action, true, async (chest, window) => {
        const beforeItems = window.containerItems();
        const stockBefore = countItem(beforeItems, itemName);
        const currencyBefore = countItem(beforeItems, config.currency.item);
        const buyerCurrency = countItem(ctx.bot.inventory.items(), config.currency.item);
        if (stockBefore < count) {
            containers.fail(action, "shop_stock_insufficient", {
                ok: false,
                error_code: "shop_stock_insufficient",
                shop_name: shop.name,
                item: namespaced(itemName),
                required: count,
                available: stockBefore,
            });
        }
        if (buyerCurrency < totalPrice) {
            containers.fail(action, "currency_insufficient", {
                ok: false,
                error_code: "currency_insufficient",
                currency: namespaced(config.currency.item),
                required: totalPrice,
                available: buyerCurrency,
            });
        }
        const inventorySpace = containers.inventorySpaceFailures(ctx.bot, [{ name: itemName, count }]);
        if (inventorySpace.failures.length > 0) {
            containers.fail(action, "inventory_space_insufficient", {
                ok: false,
                error_code: "inventory_space_insufficient",
                space: inventorySpace,
            });
        }

        const transactionId = crypto.randomUUID();
        const auditBase = {
            transaction_id: transactionId,
            buyer: username,
            seller: shop.owner,
            shop_name: shop.name,
            item: namespaced(itemName),
            count,
            unit_price: unitPrice,
            total_price: totalPrice,
            currency: namespaced(config.currency.item),
            shop_stock_before: stockBefore,
            shop_currency_before: currencyBefore,
            buyer_currency_before: buyerCurrency,
        };
        try {
            appendAudit({ ...auditBase, state: "prepared" });
        } catch (err) {
            containers.fail(action, "trade_audit_failed", {
                ok: false,
                error_code: "trade_audit_failed",
                transaction_id: transactionId,
                reason: err.message,
            });
        }

        let taken = 0;
        let paid = 0;
        try {
            await window.withdraw(itemDef.id, null, count);
            taken = stockBefore - countItem(window.containerItems(), itemName);
            if (taken !== count) {
                throw new Error(`withdrawn item count mismatch: expected ${count}, actual ${taken}`);
            }

            const paymentSpace = containers.chestSpaceFailures(
                ctx.bot,
                window,
                [{ name: config.currency.item, count: totalPrice }],
            );
            if (paymentSpace.failures.length > 0) {
                const err = new Error("shop has no space for payment");
                err.code = "shop_payment_space_insufficient";
                throw err;
            }

            await window.deposit(currencyDef.id, null, totalPrice);
            paid = countItem(window.containerItems(), config.currency.item) - currencyBefore;
            if (paid !== totalPrice) {
                throw new Error(`paid currency count mismatch: expected ${totalPrice}, actual ${paid}`);
            }

            try {
                appendAudit({
                    ...auditBase,
                    state: "committed",
                    shop_stock_after: countItem(window.containerItems(), itemName),
                    shop_currency_after: countItem(window.containerItems(), config.currency.item),
                    buyer_currency_after: countItem(ctx.bot.inventory.items(), config.currency.item),
                });
            } catch (auditError) {
                auditError.code = "trade_audit_failed";
                throw auditError;
            }
        } catch (err) {
            if (taken === 0) {
                taken = Math.max(0, stockBefore - countItem(window.containerItems(), itemName));
            }
            if (paid === 0) {
                paid = Math.max(0, countItem(window.containerItems(), config.currency.item) - currencyBefore);
            }
            const rollbackErrors = await rollbackPurchase(
                ctx.bot,
                window,
                itemDef,
                currencyDef,
                taken,
                paid,
            );
            const auditError = bestEffortAudit({
                ...auditBase,
                state: "rolled_back",
                reason: err.message,
                rollback_errors: rollbackErrors,
                shop_stock_after: countItem(window.containerItems(), itemName),
                shop_currency_after: countItem(window.containerItems(), config.currency.item),
                buyer_currency_after: countItem(ctx.bot.inventory.items(), config.currency.item),
            });
            const inconsistent = rollbackErrors.length > 0;
            containers.fail(action, inconsistent ? "purchase_inconsistent" : (err.code || "purchase_failed"), {
                ok: false,
                error_code: inconsistent ? "purchase_inconsistent" : (err.code || "purchase_failed"),
                transaction_id: transactionId,
                reason: err.message,
                rollback_errors: rollbackErrors,
                audit_error: auditError,
                shop_stock: countItem(window.containerItems(), itemName),
                shop_currency: countItem(window.containerItems(), config.currency.item),
                ...containers.inventorySnapshot(ctx.bot),
            });
        }

        return {
            ok: true,
            transaction_id: transactionId,
            buyer: username,
            seller: shop.owner,
            shop_name: shop.name,
            purchased: { item: namespaced(itemName), count },
            unit_price: unitPrice,
            total_price: totalPrice,
            currency: namespaced(config.currency.item),
            buyer_currency_remaining: countItem(ctx.bot.inventory.items(), config.currency.item),
            shop_stock_remaining: countItem(window.containerItems(), itemName),
            shop_sale_items: aggregateItems(window.containerItems(), new Set([config.currency.item])),
            chest: containers.blockToPlain(ctx.bot, chest),
        };
    });
}

function inventoryCounts(bot) {
    const counts = {};
    for (const item of bot.inventory.items()) {
        const name = normalizeItemName(item.name);
        counts[name] = (counts[name] || 0) + Number(item.count || 0);
    }
    return counts;
}

function buildCraftPlan(config, item, count, initialStock, craftingSteps = null) {
    const stock = { ...initialStock };
    const missing = {};
    const steps = [];
    const visiting = new Set();

    function ensure(output, required) {
        const available = stock[output] || 0;
        if (available >= required) return;
        const recipe = config.recipes[output];
        if (!recipe) {
            const deficit = required - available;
            missing[output] = (missing[output] || 0) + deficit;
            stock[output] = required;
            return;
        }
        if (visiting.has(output)) throw new Error(`recipe cycle detected at ${output}`);
        visiting.add(output);
        const batches = Math.ceil((required - available) / recipe.output_count);
        for (const [ingredient, amount] of Object.entries(recipe.ingredients)) {
            ensure(ingredient, amount * batches);
        }
        for (const [ingredient, amount] of Object.entries(recipe.ingredients)) {
            stock[ingredient] -= amount * batches;
        }
        stock[output] = (stock[output] || 0) + recipe.output_count * batches;
        steps.push({ output, batches, recipe });
        visiting.delete(output);
    }

    ensure(item, count);
    if (Array.isArray(craftingSteps)) {
        const order = new Map(craftingSteps.map(step => [step.output, step.order]));
        for (const step of steps) {
            if (!order.has(step.output)) {
                throw new Error(`crafting step ${step.output} is not assigned to this agent`);
            }
        }
        steps.sort((left, right) => order.get(left.output) - order.get(right.output));
    }
    return { stock, missing, steps };
}

function sameIngredients(actual, expected) {
    const left = Object.entries(actual).sort(([a], [b]) => a.localeCompare(b));
    const right = Object.entries(expected).sort(([a], [b]) => a.localeCompare(b));
    return JSON.stringify(left) === JSON.stringify(right);
}

function matchingMineflayerRecipe(bot, configured) {
    const item = crafting.resolveItem(bot, configured.output);
    if (!item) return null;
    for (const recipe of crafting.queryAllRecipes(bot, item.id)) {
        if (Number(recipe.result?.count || 1) !== configured.output_count) continue;
        if (Boolean(recipe.requiresTable) !== configured.requires_table) continue;
        const ingredients = {};
        for (const material of crafting.aggregateMaterials(bot.registry, recipe)) {
            const name = normalizeItemName(material.name);
            ingredients[name] = (ingredients[name] || 0) + Number(material.count || 0);
        }
        if (sameIngredients(ingredients, configured.ingredients)) return recipe;
    }
    return null;
}

function configuredBlock(bot, position) {
    const current = bot.entity.position;
    return bot.blockAt(current.offset(
        position.x - current.x,
        position.y - current.y,
        position.z - current.z,
    ));
}

async function craftTarget(ctx, params, action) {
    params = validateParams(params, ["item", "count"], ["item"], action);
    const { config } = actionContext(ctx, action);
    let itemName;
    try {
        itemName = normalizeItemName(params?.item);
    } catch (err) {
        containers.fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
        });
    }
    let desiredCount;
    try {
        desiredCount = positiveInteger(params?.count, "count", 1);
    } catch (err) {
        containers.fail(action, "invalid_item_request", {
            ok: false,
            error_code: "invalid_item_request",
            reason: err.message,
        });
    }

    let plan;
    try {
        plan = buildCraftPlan(
            config,
            itemName,
            desiredCount,
            inventoryCounts(ctx.bot),
            null,
        );
    } catch (err) {
        containers.fail(action, "craft_plan_invalid", {
            ok: false,
            error_code: "craft_plan_invalid",
            reason: err.message,
        });
    }
    if (Object.keys(plan.missing).length > 0) {
        containers.fail(action, "missing_materials", {
            ok: false,
            error_code: "missing_materials",
            item: namespaced(itemName),
            desired_count: desiredCount,
            missing: Object.entries(plan.missing).map(([item, count]) => ({
                item: namespaced(item),
                count,
            })),
        });
    }

    let craftingTable = null;
    if (plan.steps.some(step => step.recipe.requires_table)) {
        await moveNearPosition(ctx.bot, config.crafting_table, 2, action);
        craftingTable = configuredBlock(ctx.bot, config.crafting_table);
        if (!craftingTable || craftingTable.name !== "crafting_table") {
            containers.fail(action, "crafting_table_not_found", {
                ok: false,
                error_code: "crafting_table_not_found",
                expected_position: config.crafting_table,
            });
        }
    }

    const completedSteps = [];
    for (const step of plan.steps) {
        const recipe = matchingMineflayerRecipe(ctx.bot, step.recipe);
        if (!recipe) {
            containers.fail(action, "configured_recipe_not_found", {
                ok: false,
                error_code: "configured_recipe_not_found",
                output: namespaced(step.output),
                completed_steps: completedSteps,
            });
        }
        try {
            await ctx.bot.craft(
                recipe,
                step.batches,
                step.recipe.requires_table ? craftingTable : null,
            );
        } catch (err) {
            containers.fail(action, "craft_failed", {
                ok: false,
                error_code: "craft_failed",
                output: namespaced(step.output),
                batches: step.batches,
                reason: err.message,
                completed_steps: completedSteps,
                ...containers.inventorySnapshot(ctx.bot),
            });
        }
        completedSteps.push({
            output: namespaced(step.output),
            batches: step.batches,
            output_count: step.recipe.output_count * step.batches,
        });
    }
    return {
        ok: true,
        item: namespaced(itemName),
        desired_count: desiredCount,
        current_count: countItem(ctx.bot.inventory.items(), itemName),
        completed_steps: completedSteps,
        crafting_table: config.crafting_table,
        ...containers.inventorySnapshot(ctx.bot),
    };
}

async function takeFromContainer(ctx, params, action) {
    return transferOwnedItems(ctx, { ...params, direction: "take" }, action);
}

async function storeToContainer(ctx, params, action) {
    return transferOwnedItems(ctx, { ...params, direction: "store" }, action);
}

module.exports = {
    name: "trade",
    commands: {
        inspect_own_containers: { execute: inspectOwnContainers },
        inspect_shop: { execute: inspectShop },
        transfer_owned_items: { execute: transferOwnedItems },
        take_from_container: { execute: takeFromContainer },
        store_to_container: { execute: storeToContainer },
        buy_from_shop: { execute: buyFromShop },
        craft_target: { execute: craftTarget },
    },
    _test: {
        normalizeConfig,
        normalizeTransferItems,
        resolveTransferCounts,
        aggregateItems,
        buildCraftPlan,
        matchingMineflayerRecipe,
        acquireShopLock,
        appendAudit,
        resetConfigCache: () => { configCache = null; },
    },
};
