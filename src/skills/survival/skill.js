"use strict";

const {
    consumeWithRetry,
    findConsumableItem,
    normalizeConsumableName,
} = require("../consumable_utils");

function itemToPlain(item) {
    if (!item) return null;
    return {
        type: item.type ?? null,
        name: item.name ?? null,
        displayName: item.displayName ?? null,
        metadata: item.metadata ?? null,
        count: item.count ?? null,
        slot: item.slot ?? null,
        stackSize: item.stackSize ?? null,
        maxDurability: item.maxDurability ?? null,
        durabilityUsed: item.durabilityUsed ?? null,
    };
}

function inventorySnapshot(bot) {
    return {
        health: bot.health ?? null,
        food: bot.food ?? null,
        foodSaturation: bot.foodSaturation ?? null,
        heldItem: itemToPlain(bot.heldItem),
        inventory_items: bot.inventory.items().map(itemToPlain),
        inventory_empty_slots: bot.inventory.emptySlotCount(),
    };
}

function itemMatchesSnapshot(item, snapshot) {
    if (!item || !snapshot) return false;
    if (snapshot.type !== null && snapshot.type !== undefined) {
        if (item.type !== snapshot.type) return false;
    } else if (snapshot.name && item.name !== snapshot.name) {
        return false;
    }
    if (snapshot.metadata !== null && snapshot.metadata !== undefined) {
        return item.metadata === snapshot.metadata;
    }
    return true;
}

function findItemBySnapshot(bot, snapshot) {
    if (!snapshot) return null;
    return (bot.inventory.slots || []).find((item) => itemMatchesSnapshot(item, snapshot)) || null;
}

async function restoreHeldItem(bot, previousHeldItem) {
    if (!previousHeldItem) {
        return {
            restored_held_item: false,
            restore_reason: "no_previous_held_item",
            previous_held_item: null,
            current_held_item: itemToPlain(bot.heldItem),
        };
    }
    if (itemMatchesSnapshot(bot.heldItem, previousHeldItem)) {
        return {
            restored_held_item: true,
            restore_reason: null,
            previous_held_item: previousHeldItem,
            current_held_item: itemToPlain(bot.heldItem),
        };
    }
    const item = findItemBySnapshot(bot, previousHeldItem);
    if (!item) {
        return {
            restored_held_item: false,
            restore_reason: "original_item_not_found",
            previous_held_item: previousHeldItem,
            current_held_item: itemToPlain(bot.heldItem),
        };
    }
    try {
        await bot.equip(item, "hand");
        return {
            restored_held_item: true,
            restore_reason: null,
            previous_held_item: previousHeldItem,
            current_held_item: itemToPlain(bot.heldItem),
        };
    } catch (err) {
        return {
            restored_held_item: false,
            restore_reason: "restore_failed",
            restore_error: err.message,
            previous_held_item: previousHeldItem,
            current_held_item: itemToPlain(bot.heldItem),
        };
    }
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

async function useFood(ctx, params, action) {
    const bot = ctx.bot;
    const food = normalizeConsumableName(params.food);
    const before = inventorySnapshot(bot);
    const previousHeldItem = itemToPlain(bot.heldItem);
    const item = findConsumableItem(bot, food);
    if (!item) {
        fail(action, "food_not_found", {
            ok: false,
            error_code: "food_not_found",
            food,
            required_food: food,
            before,
        });
    }

    let consumeResult;
    try {
        await bot.equip(item, "hand");
        consumeResult = await consumeWithRetry(bot, food);
    } catch (err) {
        const restore = await restoreHeldItem(bot, previousHeldItem);
        const message = String((err && err.message) || err || "consume failed");
        fail(action, "consume_failed", {
            ok: false,
            error_code: "consume_failed",
            food,
            before,
            after: inventorySnapshot(bot),
            reason: message,
            ...restore,
        });
    }

    const restore = await restoreHeldItem(bot, previousHeldItem);
    const after = inventorySnapshot(bot);
    if (!consumeResult.ok) {
        fail(action, consumeResult.error_code || "consume_failed", {
            ...consumeResult,
            food,
            before,
            after,
            ...restore,
        });
    }
    return {
        ...consumeResult,
        food,
        before,
        after,
        ...restore,
    };
}

module.exports = {
    name: "survival",
    commands: {
        use_food: { execute: useFood },
    },
};
