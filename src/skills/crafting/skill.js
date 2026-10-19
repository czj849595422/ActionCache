"use strict";

function normalizeName(value) {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error("item must be a non-empty string");
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function parsePositiveInteger(value, fallback = 1, name = "count") {
    const parsed = Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : Math.trunc(Number(fallback));
    if (!Number.isFinite(parsed) || parsed < 1) {
        throw new Error(`${name} must be a positive integer`);
    }
    return parsed;
}

function parseZeroBasedInteger(value, fallback = 0, name = "recipe_index") {
    const parsed = Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : Math.trunc(Number(fallback));
    if (!Number.isFinite(parsed) || parsed < 0) {
        throw new Error(`${name} must be a non-negative integer`);
    }
    return parsed;
}

function parseNonNegativeNumber(value, fallback = 8, name = "crafting_table_max_distance") {
    const parsed = Number.isFinite(Number(value)) ? Number(value) : Number(fallback);
    if (!Number.isFinite(parsed) || parsed < 0) {
        throw new Error(`${name} must be a non-negative number`);
    }
    return parsed;
}

function itemToPlain(registry, item) {
    if (!item) return null;
    return {
        id: item.id ?? null,
        name: item.name ?? null,
        displayName: item.displayName ?? null,
        count: item.count ?? null,
        metadata: item.metadata ?? null,
        stackSize: item.stackSize ?? null,
    };
}

function registryItemById(registry, id) {
    if (id == null || id < 0) return null;
    const item = registry.items?.[id];
    return item || null;
}

function itemNameById(registry, id) {
    const item = registryItemById(registry, id);
    return item ? item.name : null;
}

function resolveItem(bot, itemName) {
    const normalized = normalizeName(itemName);
    const direct = bot.registry.itemsByName?.[normalized]
        || bot.registry.itemsByName?.[`minecraft:${normalized}`];
    if (direct) return direct;

    for (const [name, item] of Object.entries(bot.registry.itemsByName || {})) {
        if (normalizeName(name) === normalized) {
            return item;
        }
    }
    return null;
}

function compactEntry(registry, entry) {
    if (!entry) return null;
    return {
        id: entry.id ?? null,
        name: itemNameById(registry, entry.id),
        metadata: entry.metadata ?? null,
        count: entry.count ?? null,
    };
}

function aggregateMaterials(registry, recipe) {
    const source = Array.isArray(recipe.delta) ? recipe.delta : [];
    const aggregated = new Map();
    for (const entry of source) {
        if (!entry || (entry.count ?? 0) >= 0) continue;
        const key = `${entry.id}:${entry.metadata ?? "null"}`;
        const current = aggregated.get(key) || {
            id: entry.id ?? null,
            name: itemNameById(registry, entry.id),
            metadata: entry.metadata ?? null,
            count: 0,
        };
        current.count += Math.abs(entry.count ?? 0);
        aggregated.set(key, current);
    }
    return Array.from(aggregated.values());
}

function recipeToPlain(bot, recipe, index) {
    const registry = bot.registry;
    const result = recipe.result || {};
    return {
        recipe_index: index,
        requires_table: Boolean(recipe.requiresTable),
        craft_yield: result.count ?? 1,
        result: itemToPlain(registry, result),
        materials: aggregateMaterials(registry, recipe),
        delta: Array.isArray(recipe.delta) ? recipe.delta.map((entry) => compactEntry(registry, entry)) : [],
        ingredients: Array.isArray(recipe.ingredients) ? recipe.ingredients.map((entry) => compactEntry(registry, entry)) : null,
        inShape: Array.isArray(recipe.inShape)
            ? recipe.inShape.map((row) => row.map((entry) => compactEntry(registry, entry)))
            : null,
        outShape: Array.isArray(recipe.outShape)
            ? recipe.outShape.map((row) => row.map((entry) => compactEntry(registry, entry)))
            : null,
    };
}

function queryAllRecipes(bot, itemId) {
    const recipes = bot.recipesAll(itemId, null, true) || [];
    return recipes.slice();
}

function requiredCrafts(recipe, desiredCount) {
    const resultCount = Math.max(1, Number(recipe.result?.count ?? 1));
    return Math.max(1, Math.ceil(desiredCount / resultCount));
}

function inventoryCount(bot, id, metadata) {
    return bot.inventory.count(id, metadata ?? null);
}

function missingMaterialsForRecipe(bot, recipe, desiredCount) {
    const crafts = requiredCrafts(recipe, desiredCount);
    const materials = aggregateMaterials(bot.registry, recipe);
    const missing = [];
    for (const material of materials) {
        const required = (material.count ?? 0) * crafts;
        const available = inventoryCount(bot, material.id, material.metadata);
        const shortage = Math.max(0, required - available);
        if (shortage > 0) {
            missing.push({
                id: material.id,
                name: material.name,
                metadata: material.metadata,
                required,
                available,
                missing: shortage,
            });
        }
    }
    return {
        crafts,
        materials,
        missing,
    };
}

function findCraftingTable(bot, maxDistance) {
    const tableId = bot.registry.blocksByName?.crafting_table?.id;
    if (tableId == null) return null;
    return bot.findBlock({
        matching: tableId,
        maxDistance,
    });
}

function positionToPlain(position) {
    if (!position) return null;
    return { x: position.x, y: position.y, z: position.z };
}

function blockToPlain(bot, block) {
    if (!block) return null;
    const center = block.position.offset(0.5, 0.5, 0.5);
    return {
        type: block.type ?? null,
        name: block.name ?? null,
        position: positionToPlain(block.position),
        center: positionToPlain(center),
        distance: bot.entity?.position ? bot.entity.position.distanceTo(center) : null,
    };
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

async function moveNearCraftingTable(bot, craftingTable) {
    ensurePathfinder(bot);
    const { goals } = bot.mcagentPathfinder;
    const center = craftingTable.position.offset(0.5, 0, 0.5);
    const goal = new goals.GoalNear(center.x, center.y, center.z, 1);
    await bot.pathfinder.goto(goal);
    bot.clearControlStates();
}

async function prepareCraftingTable(bot, maxDistance) {
    let craftingTable = findCraftingTable(bot, maxDistance);
    if (!craftingTable) {
        return {
            ok: false,
            error_code: "crafting_table_not_found",
            max_distance: maxDistance,
            crafting_table: null,
        };
    }

    let before = blockToPlain(bot, craftingTable);
    try {
        await moveNearCraftingTable(bot, craftingTable);
    } catch (err) {
        return {
            ok: false,
            error_code: "crafting_table_approach_failed",
            max_distance: maxDistance,
            crafting_table: before,
            reason: err.message,
        };
    }

    craftingTable = findCraftingTable(bot, maxDistance);
    if (!craftingTable) {
        return {
            ok: false,
            error_code: "crafting_table_not_found_after_approach",
            max_distance: maxDistance,
            crafting_table_before_approach: before,
            crafting_table: null,
        };
    }

    const target = craftingTable.position.offset(0.5, 0.5, 0.5);
    try {
        await bot.lookAt(target, true);
    } catch (err) {
        return {
            ok: false,
            error_code: "crafting_table_look_failed",
            max_distance: maxDistance,
            crafting_table: blockToPlain(bot, craftingTable),
            reason: err.message,
        };
    }

    try {
        const window = await bot.openBlock(craftingTable);
        if (window) {
            bot.closeWindow(window);
        }
    } catch (err) {
        return {
            ok: false,
            error_code: "crafting_table_open_failed",
            max_distance: maxDistance,
            crafting_table: blockToPlain(bot, craftingTable),
            crafting_table_before_approach: before,
            reason: err.message,
        };
    }

    return {
        ok: true,
        craftingTable,
        crafting_table: blockToPlain(bot, craftingTable),
        crafting_table_before_approach: before,
    };
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

async function queryRecipe(ctx, params) {
    const bot = ctx.bot;
    const itemName = normalizeName(params.item);
    if (ctx.getTaskCategory?.() === "trade") {
        const queryable = ctx.getTradeQueryableItems?.();
        if (queryable instanceof Set && queryable.size > 0 && !queryable.has(itemName)) {
            fail(ctx.action, "trade_recipe_item_not_allowed", {
                ok: false,
                error_code: "trade_recipe_item_not_allowed",
                item: itemName,
                allowed_items: Array.from(queryable).sort(),
                recipes: [],
            });
        }
    }
    const item = resolveItem(bot, itemName);
    if (!item) {
        fail(ctx.action, "unknown_item", {
            ok: false,
            error_code: "unknown_item",
            item: itemName,
            recipes: [],
        });
    }

    const recipes = queryAllRecipes(bot, item.id);
    const plainRecipes = recipes.map((recipe, index) => recipeToPlain(bot, recipe, index));
    const requiresTableAny = plainRecipes.some((recipe) => recipe.requires_table);
    const requiresTableAll = plainRecipes.length > 0 && plainRecipes.every((recipe) => recipe.requires_table);
    const craftableWithoutTableCount = plainRecipes.filter((recipe) => !recipe.requires_table).length;

    return {
        ok: true,
        item: itemToPlain(bot.registry, item),
        recipe_count: plainRecipes.length,
        requires_table_any: requiresTableAny,
        requires_table_all: requiresTableAll,
        craftable_without_table_count: craftableWithoutTableCount,
        recipes: plainRecipes,
    };
}

async function craftWithRecipe(ctx, params, action) {
    const bot = ctx.bot;
    const itemName = normalizeName(params.item);
    const item = resolveItem(bot, itemName);
    if (!item) {
        fail(action, "unknown_item", {
            ok: false,
            error_code: "unknown_item",
            item: itemName,
        });
    }

    const recipeIndex = parseZeroBasedInteger(params.recipe_index ?? 0);
    const desiredCount = parsePositiveInteger(params.count ?? 1, 1, "count");
    const tableMaxDistance = parseNonNegativeNumber(params.crafting_table_max_distance ?? 8);
    const recipes = queryAllRecipes(bot, item.id);

    if (recipeIndex < 0 || recipeIndex >= recipes.length) {
        fail(action, "invalid_recipe_index", {
            ok: false,
            error_code: "invalid_recipe_index",
            item: itemName,
            recipe_index: recipeIndex,
            recipe_count: recipes.length,
        });
    }

    const recipe = recipes[recipeIndex];
    const check = missingMaterialsForRecipe(bot, recipe, desiredCount);
    if (check.missing.length > 0) {
        fail(action, "missing_materials", {
            ok: false,
            error_code: "missing_materials",
            item: itemName,
            recipe_index: recipeIndex,
            requires_table: Boolean(recipe.requiresTable),
            craft_count: check.crafts,
            crafted_item_count: (recipe.result?.count ?? 1) * check.crafts,
            missing: check.missing,
            consumed_ingredients: check.materials,
        });
    }

    let craftingTable = null;
    let tablePreparation = null;
    if (recipe.requiresTable) {
        tablePreparation = await prepareCraftingTable(bot, tableMaxDistance);
        if (!tablePreparation.ok) {
            fail(action, tablePreparation.error_code, {
                ok: false,
                error_code: tablePreparation.error_code,
                item: itemName,
                recipe_index: recipeIndex,
                requires_table: true,
                max_distance: tableMaxDistance,
                crafting_table: tablePreparation.crafting_table ?? null,
                crafting_table_before_approach: tablePreparation.crafting_table_before_approach ?? null,
                reason: tablePreparation.reason ?? null,
            });
        }
        craftingTable = tablePreparation.craftingTable;
    }

    const craftedCount = (recipe.result?.count ?? 1) * check.crafts;
    try {
        await bot.craft(recipe, check.crafts, craftingTable);
    } catch (err) {
        fail(action, "craft_failed", {
            ok: false,
            error_code: "craft_failed",
            item: itemName,
            recipe_index: recipeIndex,
            requires_table: Boolean(recipe.requiresTable),
            craft_count: check.crafts,
            crafted_item_count: craftedCount,
            missing: [],
            consumed_ingredients: check.materials,
            crafting_table: tablePreparation ? tablePreparation.crafting_table : null,
            crafting_table_before_approach: tablePreparation ? tablePreparation.crafting_table_before_approach : null,
            reason: err.message,
        });
    }

    return {
        ok: true,
        item: itemName,
        recipe_index: recipeIndex,
        requires_table: Boolean(recipe.requiresTable),
        craft_count: check.crafts,
        crafted_item_count: craftedCount,
        consumed_ingredients: check.materials,
        crafting_table: tablePreparation ? tablePreparation.crafting_table : null,
        crafting_table_before_approach: tablePreparation ? tablePreparation.crafting_table_before_approach : null,
    };
}

async function craftItem(ctx, params, action) {
    const bot = ctx.bot;
    const itemName = normalizeName(params.item);
    const item = resolveItem(bot, itemName);
    if (!item) {
        fail(action, "unknown_item", {
            ok: false,
            error_code: "unknown_item",
            item: itemName,
        });
    }

    const desiredCount = parsePositiveInteger(params.count ?? 1, 1, "count");
    const tableMaxDistance = parseNonNegativeNumber(params.crafting_table_max_distance ?? 8);
    const recipes = queryAllRecipes(bot, item.id);
    const candidateRecipes = [];
    let selected = null;
    let selectedIndex = -1;

    for (let index = 0; index < recipes.length; index++) {
        const recipe = recipes[index];
        const check = missingMaterialsForRecipe(bot, recipe, desiredCount);
        const candidate = {
            recipe_index: index,
            requires_table: Boolean(recipe.requiresTable),
            craft_count: check.crafts,
            crafted_item_count: (recipe.result?.count ?? 1) * check.crafts,
            missing: check.missing,
            consumed_ingredients: check.materials,
        };
        candidateRecipes.push(candidate);
        if (!selected && check.missing.length === 0) {
            selected = { recipe, check };
            selectedIndex = index;
        }
    }

    if (!selected) {
        fail(action, "missing_materials", {
            ok: false,
            error_code: "missing_materials",
            item: itemName,
            candidate_recipes: candidateRecipes,
        });
    }

    const recipe = selected.recipe;
    const check = selected.check;
    let craftingTable = null;
    let tablePreparation = null;
    if (recipe.requiresTable) {
        tablePreparation = await prepareCraftingTable(bot, tableMaxDistance);
        if (!tablePreparation.ok) {
            fail(action, tablePreparation.error_code, {
                ok: false,
                error_code: tablePreparation.error_code,
                item: itemName,
                selected_recipe_index: selectedIndex,
                requires_table: true,
                max_distance: tableMaxDistance,
                crafting_table: tablePreparation.crafting_table ?? null,
                crafting_table_before_approach: tablePreparation.crafting_table_before_approach ?? null,
                candidate_recipes: candidateRecipes,
                reason: tablePreparation.reason ?? null,
            });
        }
        craftingTable = tablePreparation.craftingTable;
    }

    const craftedCount = (recipe.result?.count ?? 1) * check.crafts;
    try {
        await bot.craft(recipe, check.crafts, craftingTable);
    } catch (err) {
        fail(action, "craft_failed", {
            ok: false,
            error_code: "craft_failed",
            item: itemName,
            selected_recipe_index: selectedIndex,
            requires_table: Boolean(recipe.requiresTable),
            craft_count: check.crafts,
            crafted_item_count: craftedCount,
            missing: [],
            consumed_ingredients: check.materials,
            candidate_recipes: candidateRecipes,
            crafting_table: tablePreparation ? tablePreparation.crafting_table : null,
            crafting_table_before_approach: tablePreparation ? tablePreparation.crafting_table_before_approach : null,
            reason: err.message,
        });
    }

    return {
        ok: true,
        item: itemName,
        selected_recipe_index: selectedIndex,
        requires_table: Boolean(recipe.requiresTable),
        craft_count: check.crafts,
        crafted_item_count: craftedCount,
        consumed_ingredients: check.materials,
        crafting_table: tablePreparation ? tablePreparation.crafting_table : null,
        crafting_table_before_approach: tablePreparation ? tablePreparation.crafting_table_before_approach : null,
    };
}

module.exports = {
    name: "crafting",
    commands: {
        query_recipe: { execute: queryRecipe },
        craft_with_recipe: { execute: craftWithRecipe },
        craft_item: { execute: craftItem },
    },
    service: {
        normalizeName,
        itemToPlain,
        aggregateMaterials,
        queryAllRecipes,
        resolveItem,
        prepareCraftingTable,
        inventoryCount,
    },
};
