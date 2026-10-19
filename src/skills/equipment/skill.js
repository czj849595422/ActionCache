"use strict";

const VALID_DESTINATIONS = new Set(["hand", "off-hand", "head", "torso", "legs", "feet"]);
const equipmentRegistry = require("../../env/mineflayer/equipment_registry");
const ARMOR_DESTINATIONS = ["head", "torso", "legs", "feet"];
const MATERIAL_SCORE = {
    netherite: 600,
    diamond: 500,
    iron: 400,
    chainmail: 320,
    stone: 300,
    golden: 220,
    gold: 220,
    leather: 120,
    wooden: 100,
    wood: 100,
};
const WEAPON_TYPE_SCORE = {
    sword: 30,
    axe: 20,
};

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeName(value, field = "name") {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim().toLowerCase().replace(/^minecraft:/, "");
}

function normalizeSlot(value) {
    if (value === undefined || value === null || value === "") return null;
    const slot = Number(value);
    if (!Number.isInteger(slot) || slot < 0) {
        throw new Error("slot must be a non-negative integer");
    }
    return slot;
}

function parseBoolean(value, fallback) {
    if (value === undefined || value === null) return fallback;
    return Boolean(value);
}

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
        ...equipmentRegistry.cached(item),
    };
}

function itemsToPlain(items) {
    return Array.from(items || []).filter(Boolean).map(itemToPlain);
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

function assertDestination(destination) {
    const normalized = typeof destination === "string" ? destination.trim().toLowerCase() : "";
    if (!VALID_DESTINATIONS.has(normalized)) {
        const err = new Error("invalid_destination");
        err.destination = destination;
        throw err;
    }
    return normalized;
}

function equipmentSlot(bot, destination) {
    return bot.getEquipmentDestSlot(destination);
}

function equippedItem(bot, destination) {
    if (destination === "hand") return bot.heldItem || null;
    const slot = equipmentSlot(bot, destination);
    return bot.inventory.slots[slot] || null;
}

function equipmentSnapshot(bot) {
    const equipment = {};
    for (const destination of VALID_DESTINATIONS) {
        try {
            const item = equippedItem(bot, destination);
            equipmentRegistry.annotate(bot.username, item, "equipment");
            equipment[destination] = itemToPlain(item);
        } catch (err) {
            equipment[destination] = null;
        }
    }
    return equipment;
}

function inventorySnapshot(bot) {
    return {
        inventory_items: bot.inventory.items().filter(Boolean).map((item) => {
            equipmentRegistry.annotate(bot.username, item, "inventory");
            return itemToPlain(item);
        }),
        inventory_empty_slots: bot.inventory.emptySlotCount(),
        heldItem: itemToPlain(bot.heldItem),
        equipment: equipmentSnapshot(bot),
    };
}

function itemSameIdentity(a, b) {
    if (!a || !b) return false;
    return a.type === b.type && a.metadata === b.metadata && a.slot === b.slot;
}

function itemNameMatches(item, normalizedName) {
    if (!item) return false;
    const itemName = typeof item.name === "string" ? item.name.toLowerCase() : "";
    const displayName = typeof item.displayName === "string" ? item.displayName.toLowerCase() : "";
    return (
        itemName === normalizedName
        || displayName === normalizedName
        || displayName.replace(/\s+/g, "_") === normalizedName
    );
}

function inventoryItems(bot) {
    return bot.inventory.items().filter(Boolean);
}

function matchingItems(bot, name) {
    const normalized = normalizeName(name, "item");
    const items = inventoryItems(bot);
    const matches = items.filter((item) => itemNameMatches(item, normalized));
    const resolved = equipmentRegistry.resolve(
        bot.username,
        name,
        items,
        (item, input) => itemNameMatches(item, normalizeName(input, "item")),
    );
    if (resolved && matches.length === 0) return [resolved];
    return equipmentRegistry.enabled() && matches.some(equipmentRegistry.isEquipmentLike)
        ? equipmentRegistry.sortNameMatches(matches)
        : matches;
}

function itemAtSlot(bot, slot) {
    return inventoryItems(bot).find((item) => Number(item.slot) === slot) || null;
}

function destinationForItemName(name) {
    if (name.endsWith("_helmet") || name === "turtle_helmet") return "head";
    if (name.endsWith("_chestplate") || name === "elytra") return "torso";
    if (name.endsWith("_leggings")) return "legs";
    if (name.endsWith("_boots")) return "feet";
    if (name === "shield") return "off-hand";
    return "hand";
}

function materialPrefix(name) {
    for (const prefix of Object.keys(MATERIAL_SCORE)) {
        if (name.startsWith(`${prefix}_`)) return prefix;
    }
    return null;
}

function materialScore(name) {
    const prefix = materialPrefix(name);
    return prefix ? MATERIAL_SCORE[prefix] : 0;
}

function weaponScore(item) {
    if (!item || typeof item.name !== "string") return -1;
    if (item.name.endsWith("_sword")) return materialScore(item.name) + WEAPON_TYPE_SCORE.sword;
    if (item.name.endsWith("_axe")) return materialScore(item.name) + WEAPON_TYPE_SCORE.axe;
    return -1;
}

function armorScore(item, destination) {
    if (!item || typeof item.name !== "string") return -1;
    if (destinationForItemName(item.name) !== destination) return -1;
    return materialScore(item.name);
}

function bestByScore(items, scoreFn) {
    let best = null;
    let bestScore = -1;
    for (const item of items) {
        const score = scoreFn(item);
        if (score > bestScore) {
            best = item;
            bestScore = score;
        }
    }
    return best ? { item: best, score: bestScore } : null;
}

function isTargetAlreadyEquipped(bot, item, destination) {
    return itemSameIdentity(item, equippedItem(bot, destination));
}

async function equipResolvedItem(bot, item, destination) {
    equipmentRegistry.annotate(bot.username, item, "inventory");
    await bot.equip(item, destination);
    await sleep(250);
}

async function equipItem(ctx, params, action) {
    const bot = ctx.bot;
    let destination;
    let itemName = null;
    let slot = null;
    try {
        slot = normalizeSlot(params.slot);
        if (slot === null) {
            itemName = normalizeName(params.item ?? params.name, "item");
        } else if (
            (params.item !== undefined && params.item !== null)
            || (params.name !== undefined && params.name !== null)
        ) {
            throw new Error("item/name and slot cannot be used together");
        }
    } catch (err) {
        const code = err.message === "invalid_destination" ? "invalid_destination" : "invalid_item_request";
        fail(action, code, {
            ok: false,
            error_code: code,
            reason: err.message,
            item: params.item ?? params.name ?? null,
            slot,
            destination: params.destination ?? null,
            ...inventorySnapshot(bot),
        });
    }

    const candidates = slot === null
        ? matchingItems(bot, itemName)
        : [itemAtSlot(bot, slot)].filter(Boolean);
    if (candidates.length === 0) {
        fail(action, "item_missing", {
            ok: false,
            error_code: "item_missing",
            item: itemName,
            slot,
            destination: params.destination ?? null,
            ...inventorySnapshot(bot),
        });
    }

    const selectedName = candidates[0].name;
    try {
        destination = assertDestination(params.destination || destinationForItemName(selectedName));
    } catch (err) {
        fail(action, "invalid_destination", {
            ok: false,
            error_code: "invalid_destination",
            reason: err.message,
            item: selectedName,
            slot,
            destination: params.destination ?? null,
            ...inventorySnapshot(bot),
        });
    }

    const current = equippedItem(bot, destination);
    // candidates 已按装备注册器规则排序；不能因当前同名而跳过更优候选。
    const item = candidates[0];
    if (isTargetAlreadyEquipped(bot, item, destination)) {
        return {
            ok: true,
            equipped: true,
            already_equipped: true,
            destination,
            item: itemToPlain(item),
            ...inventorySnapshot(bot),
        };
    }

    try {
        await equipResolvedItem(bot, item, destination);
        return {
            ok: true,
            equipped: true,
            already_equipped: false,
            destination,
            item: itemToPlain(item),
            replaced: itemToPlain(current),
            ...inventorySnapshot(bot),
        };
    } catch (err) {
        fail(action, "equip_failed", {
            ok: false,
            error_code: "equip_failed",
            item: selectedName,
            slot,
            destination,
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
}

async function unequipItem(ctx, params, action) {
    const bot = ctx.bot;
    let destination;
    try {
        destination = assertDestination(params.destination);
    } catch (err) {
        fail(action, "invalid_destination", {
            ok: false,
            error_code: "invalid_destination",
            destination: params.destination ?? null,
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }

    const current = equippedItem(bot, destination);
    if (!current) {
        fail(action, "slot_empty", {
            ok: false,
            error_code: "slot_empty",
            destination,
            ...inventorySnapshot(bot),
        });
    }
    if (bot.inventory.emptySlotCount() <= 0) {
        fail(action, "inventory_space_insufficient", {
            ok: false,
            error_code: "inventory_space_insufficient",
            destination,
            item: itemToPlain(current),
            inventory_empty_slots: bot.inventory.emptySlotCount(),
            ...inventorySnapshot(bot),
        });
    }

    try {
        await bot.unequip(destination);
        await sleep(250);
        return {
            ok: true,
            unequipped: true,
            destination,
            item: itemToPlain(current),
            ...inventorySnapshot(bot),
        };
    } catch (err) {
        fail(action, "unequip_failed", {
            ok: false,
            error_code: "unequip_failed",
            destination,
            item: itemToPlain(current),
            reason: err.message,
            ...inventorySnapshot(bot),
        });
    }
}

function normalizeEquipEntries(raw) {
    const values =
        raw === undefined || raw === null ? [] : (Array.isArray(raw) ? raw : [raw]);
    const entries = [];
    for (const value of values) {
        if (typeof value === "string" && value.trim()) {
            entries.push({ item: value.trim(), slot: undefined, destination: undefined, valid: true });
        } else if (value && typeof value === "object") {
            const item = value.item ?? value.name;
            const hasItem = typeof item === "string" && item.trim();
            const hasSlot = value.slot !== undefined && value.slot !== null && value.slot !== "";
            if ((hasItem && !hasSlot) || (!hasItem && hasSlot)) {
                entries.push({
                    item: hasItem ? item.trim() : null,
                    slot: hasSlot ? value.slot : undefined,
                    destination: value.destination,
                    valid: true,
                });
            } else {
                entries.push({
                    item: hasItem ? item.trim() : null,
                    slot: hasSlot ? value.slot : undefined,
                    destination: value.destination,
                    valid: false,
                });
            }
        } else if (Number.isInteger(value) && value >= 0) {
            entries.push({ item: null, slot: value, destination: undefined, valid: true });
        } else {
            entries.push({ item: value, slot: undefined, destination: undefined, valid: false });
        }
    }
    return entries;
}

function buildBatchResult(succeeded, failed, bot) {
    // 全部成功或部分成功均视为成功；全部失败（无任何成功项）才为失败。
    const ok = succeeded.length > 0;
    return {
        ok,
        total: succeeded.length + failed.length,
        succeeded,
        failed,
        partial: succeeded.length > 0 && failed.length > 0,
        ...(ok ? {} : { error_code: "all_failed" }),
        ...inventorySnapshot(bot),
    };
}

async function equipItems(ctx, params, action) {
    const bot = ctx.bot;
    const raw =
        params.items === undefined ? (params.item ?? params.name) : params.items;
    const entries = normalizeEquipEntries(raw);
    const succeeded = [];
    const failed = [];
    for (const entry of entries) {
        if (!entry.valid) {
            failed.push({
                destination: entry.destination || null,
                item: entry.item,
                slot: entry.slot ?? null,
                error_code: "invalid_item_request",
                reason: "item must be a non-empty string or {item/name, slot, destination}",
            });
            continue;
        }
        try {
            const request = { destination: entry.destination };
            if (entry.item !== null && entry.item !== undefined) request.item = entry.item;
            if (entry.slot !== undefined) request.slot = entry.slot;
            const single = await equipItem(
                ctx,
                request,
                action,
            );
            succeeded.push({
                destination: single.destination,
                item: itemToPlain(single.item),
                error: null,
            });
        } catch (err) {
            const code = err.code || err.message || "equip_failed";
            const reason =
                (action.result && action.result.reason) || err.message || code;
            failed.push({
                destination: entry.destination || null,
                item: entry.item,
                slot: entry.slot ?? null,
                error_code: code,
                reason,
            });
        }
    }
    if (succeeded.length === 0 && failed.length === 0) {
        return {
            ok: false,
            total: 0,
            succeeded: [],
            failed: [{ error_code: "invalid_items", reason: "no items provided" }],
            partial: false,
            error_code: "all_failed",
            ...inventorySnapshot(bot),
        };
    }
    return buildBatchResult(succeeded, failed, bot);
}

async function unequipItems(ctx, params, action) {
    const bot = ctx.bot;
    const raw =
        params.destinations === undefined
            ? params.destination
            : params.destinations;
    const destinations =
        raw === undefined || raw === null
            ? []
            : (Array.isArray(raw) ? raw : [raw]);
    const succeeded = [];
    const failed = [];
    for (const dest of destinations) {
        try {
            const single = await unequipItem(ctx, { destination: dest }, action);
            succeeded.push({
                destination: single.destination,
                item: itemToPlain(single.item),
                error: null,
            });
        } catch (err) {
            const code = err.code || err.message || "unequip_failed";
            const reason =
                (action.result && action.result.reason) || err.message || code;
            failed.push({ destination: dest, error_code: code, reason });
        }
    }
    if (succeeded.length === 0 && failed.length === 0) {
        return {
            ok: false,
            total: 0,
            succeeded: [],
            failed: [{
                error_code: "invalid_destinations",
                reason: "no destinations provided",
            }],
            partial: false,
            error_code: "all_failed",
            ...inventorySnapshot(bot),
        };
    }
    return buildBatchResult(succeeded, failed, bot);
}

async function autoEquipCombat(ctx, params, action) {
    const bot = ctx.bot;
    const preferWeapon = params.prefer_weapon || "best";
    const equipShield = parseBoolean(params.equip_shield, true);
    const equipArmor = parseBoolean(params.equip_armor, true);
    const equipped = [];
    const skipped = [];
    const missingSlots = [];

    if (preferWeapon !== "none") {
        const candidates = inventoryItems(bot).filter((item) => weaponScore(item) >= 0);
        const best = bestByScore(candidates, weaponScore);
        const current = equippedItem(bot, "hand");
        const currentScore = weaponScore(current);
        if (!best) {
            skipped.push({ destination: "hand", reason: "no_candidate" });
            missingSlots.push("hand");
        } else if (current && currentScore >= best.score) {
            skipped.push({ destination: "hand", reason: "current_not_worse", current: itemToPlain(current), candidate: itemToPlain(best.item) });
        } else {
            try {
                await equipResolvedItem(bot, best.item, "hand");
                equipped.push({ destination: "hand", item: itemToPlain(best.item), score: best.score });
            } catch (err) {
                skipped.push({ destination: "hand", reason: "equip_failed", item: itemToPlain(best.item), error: err.message });
            }
        }
    }

    if (equipShield) {
        const shield = inventoryItems(bot).find((item) => item && item.name === "shield");
        const current = equippedItem(bot, "off-hand");
        if (current && current.name === "shield") {
            skipped.push({ destination: "off-hand", reason: "already_equipped", current: itemToPlain(current) });
        } else if (!shield) {
            skipped.push({ destination: "off-hand", reason: "no_candidate" });
            missingSlots.push("off-hand");
        } else {
            try {
                await equipResolvedItem(bot, shield, "off-hand");
                equipped.push({ destination: "off-hand", item: itemToPlain(shield) });
            } catch (err) {
                skipped.push({ destination: "off-hand", reason: "equip_failed", item: itemToPlain(shield), error: err.message });
            }
        }
    }

    if (equipArmor) {
        for (const destination of ARMOR_DESTINATIONS) {
            const candidates = inventoryItems(bot).filter((item) => armorScore(item, destination) >= 0);
            const best = bestByScore(candidates, (item) => armorScore(item, destination));
            const current = equippedItem(bot, destination);
            const currentScore = armorScore(current, destination);
            if (!best) {
                skipped.push({ destination, reason: "no_candidate" });
                missingSlots.push(destination);
            } else if (current && currentScore >= best.score) {
                skipped.push({ destination, reason: "current_not_worse", current: itemToPlain(current), candidate: itemToPlain(best.item) });
            } else {
                try {
                    await equipResolvedItem(bot, best.item, destination);
                    equipped.push({ destination, item: itemToPlain(best.item), score: best.score });
                } catch (err) {
                    skipped.push({ destination, reason: "equip_failed", item: itemToPlain(best.item), error: err.message });
                }
            }
        }
    }

    return {
        ok: true,
        prefer_weapon: preferWeapon,
        equipped,
        skipped,
        missing_slots: missingSlots,
        ...inventorySnapshot(bot),
    };
}

module.exports = {
    name: "equipment",
    commands: {
        equip_item: { execute: equipItem },
        unequip_item: { execute: unequipItem },
        equip_items: { execute: equipItems },
        unequip_items: { execute: unequipItems },
        auto_equip_combat: { execute: autoEquipCombat },
    },
};
