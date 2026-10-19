"use strict";

const HEALING_POTION = "potion[healing]";
const STRONG_HEALING_POTION = "potion[strong_healing]";
const CONSUME_RETRY_COUNT = 2;
const CONSUME_CONFIRM_TIMEOUT_MS = 1000;
const CONSUME_RETRY_DELAY_MS = 150;
const ACTIVE_CONSUMERS = new WeakSet();

function normalizeConsumableName(value) {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error("food must be a non-empty consumable item name");
    }
    const normalized = value.trim().toLowerCase().replace(/^minecraft:/, "");
    if (normalized === "potion" || normalized === HEALING_POTION) {
        return HEALING_POTION;
    }
    if (normalized === STRONG_HEALING_POTION) {
        return STRONG_HEALING_POTION;
    }
    return normalized;
}

function itemText(value) {
    return String(value || "").trim().toLowerCase().replace(/\s+/g, "_");
}

function unwrapNbt(value) {
    if (!value || typeof value !== "object") return value;
    if (Object.prototype.hasOwnProperty.call(value, "value")) {
        return unwrapNbt(value.value);
    }
    return value;
}

function findNbtValue(value, wantedKey) {
    const current = unwrapNbt(value);
    if (!current || typeof current !== "object") return null;
    for (const [key, child] of Object.entries(current)) {
        if (key.toLowerCase() === wantedKey.toLowerCase()) {
            return unwrapNbt(child);
        }
        const nested = findNbtValue(child, wantedKey);
        if (nested !== null && nested !== undefined) return nested;
    }
    return null;
}

function potionProfile(item) {
    const value = findNbtValue(item && item.nbt, "Potion");
    if (value === null || value === undefined) return null;
    return String(value).toLowerCase().replace(/^minecraft:/, "");
}

function itemMatchesConsumable(item, requestedName) {
    if (!item) return false;
    const requested = normalizeConsumableName(requestedName);
    const itemName = itemText(item.name);
    const displayName = itemText(item.displayName);

    if (requested === HEALING_POTION || requested === STRONG_HEALING_POTION) {
        if (itemName !== "potion" && displayName !== "potion") return false;
        const profile = potionProfile(item);
        if (!profile) return true;
        return profile === requested.slice("potion[".length, -1);
    }
    return itemName === requested || displayName === requested;
}

function findConsumableItem(bot, requestedName) {
    const requested = normalizeConsumableName(requestedName);
    return bot.inventory.items().find((item) => itemMatchesConsumable(item, requested)) || null;
}

function countConsumableItems(bot, requestedName) {
    const requested = normalizeConsumableName(requestedName);
    return bot.inventory.items().reduce(
        (total, item) => itemMatchesConsumable(item, requested) ? total + (item.count || 0) : total,
        0,
    );
}

function sleep(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function numericCount(item) {
    return item && Number.isFinite(item.count) ? item.count : null;
}

function consumptionTarget(bot, requestedName) {
    const heldItem = bot.heldItem && itemMatchesConsumable(bot.heldItem, requestedName)
        ? bot.heldItem
        : findConsumableItem(bot, requestedName);
    if (!heldItem) return null;
    return {
        slot: Number.isInteger(heldItem.slot) ? heldItem.slot : null,
        type: heldItem.type ?? null,
        name: heldItem.name ?? null,
        metadata: heldItem.metadata ?? null,
        initialCount: numericCount(heldItem),
    };
}

function targetMatches(item, target, requestedName) {
    if (!item || !target) return false;
    if (target.slot !== null && Number.isInteger(item.slot) && item.slot !== target.slot) {
        return false;
    }
    if (target.type !== null && item.type !== target.type) return false;
    if (target.name && item.name && item.name !== target.name) return false;
    if (target.metadata !== null && item.metadata !== target.metadata) return false;
    return itemMatchesConsumable(item, requestedName);
}

function targetAtSlot(bot, target) {
    if (!target || target.slot === null || !bot.inventory || !Array.isArray(bot.inventory.slots)) {
        return null;
    }
    return bot.inventory.slots[target.slot] || null;
}

function createConsumptionEvidence(bot, requestedName) {
    const target = consumptionTarget(bot, requestedName);
    const evidence = {
        target,
        confirmed: false,
        confirmedBy: null,
        slotUpdates: 0,
    };

    const confirm = (source) => {
        evidence.confirmed = true;
        evidence.confirmedBy = evidence.confirmedBy || source;
    };

    const onSlotUpdate = (slot, oldItem, newItem) => {
        if (!target || (target.slot !== null && Number(slot) !== target.slot)) return;
        evidence.slotUpdates += 1;
        const oldMatches = targetMatches(oldItem, target, requestedName);
        if (!oldMatches) return;

        const oldCount = numericCount(oldItem);
        const newCount = numericCount(newItem);
        if (newItem === null || !targetMatches(newItem, target, requestedName)) {
            confirm("inventory_slot_replaced");
            return;
        }
        if (oldCount !== null && (newCount === null || newCount < oldCount)) {
            confirm("inventory_slot_decreased");
        }
    };

    const inventory = bot.inventory;
    if (inventory && typeof inventory.on === "function") {
        inventory.on("updateSlot", onSlotUpdate);
    }

    const observeCurrentSlot = () => {
        if (evidence.confirmed || !target) return;
        const current = targetAtSlot(bot, target);
        if (!current) {
            confirm("inventory_slot_empty");
            return;
        }
        const currentCount = numericCount(current);
        if (target.initialCount !== null && currentCount !== null && currentCount < target.initialCount) {
            confirm("inventory_slot_decreased");
        } else if (!targetMatches(current, target, requestedName)) {
            confirm("inventory_slot_replaced");
        }
    };

    let interval = null;
    const dispose = () => {
        if (interval) clearInterval(interval);
        if (inventory && typeof inventory.removeListener === "function") {
            inventory.removeListener("updateSlot", onSlotUpdate);
        }
    };

    const wait = (milliseconds) => new Promise((resolve) => {
        observeCurrentSlot();
        if (evidence.confirmed || milliseconds <= 0) {
            resolve(evidence.confirmed);
            return;
        }
        const deadline = Date.now() + milliseconds;
        interval = setInterval(() => {
            observeCurrentSlot();
            if (evidence.confirmed || Date.now() >= deadline) {
                clearInterval(interval);
                interval = null;
                resolve(evidence.confirmed);
            }
        }, 50);
    });

    return { evidence, confirm, dispose, wait };
}

async function deactivateItem(bot) {
    try {
        const result = bot.deactivateItem();
        if (result && typeof result.then === "function") await result;
    } catch (_) {}
}

async function consumeWithRetry(bot, requestedName) {
    const requested = normalizeConsumableName(requestedName);
    if (ACTIVE_CONSUMERS.has(bot)) {
        return {
            ok: false,
            error_code: "consume_in_progress",
            reason: "another consumable action is already in progress",
            consume_attempts: 0,
        };
    }

    ACTIVE_CONSUMERS.add(bot);
    const consumptionEvidence = createConsumptionEvidence(bot, requested);
    let lastError = "consume failed";
    let attempts = 0;
    try {
        for (let attempt = 1; attempt <= CONSUME_RETRY_COUNT; attempt += 1) {
            attempts = attempt;
            try {
                await bot.consume();
                consumptionEvidence.confirm("consume_resolved");
                return {
                    ok: true,
                    consumed: true,
                    consume_attempts: attempt,
                    consume_confirmation: consumptionEvidence.evidence.confirmedBy,
                };
            } catch (err) {
                const message = String((err && err.message) || err || "");
                if (/food is full/i.test(message)) {
                    return {
                        ok: true,
                        consumed: false,
                        consume_reason: "food_full",
                        consume_attempts: attempt,
                    };
                }
                lastError = message || lastError;
                if (!/timed out/i.test(message)) break;

                if (await consumptionEvidence.wait(CONSUME_CONFIRM_TIMEOUT_MS)) {
                    return {
                        ok: true,
                        consumed: true,
                        consume_reason: "completed_after_timeout",
                        consume_attempts: attempt,
                        consume_confirmation: consumptionEvidence.evidence.confirmedBy,
                    };
                }
                if (attempt < CONSUME_RETRY_COUNT) {
                    await deactivateItem(bot);
                    await sleep(CONSUME_RETRY_DELAY_MS);
                    if (consumptionEvidence.evidence.confirmed) {
                        return {
                            ok: true,
                            consumed: true,
                            consume_reason: "completed_after_timeout",
                            consume_attempts: attempt,
                            consume_confirmation: consumptionEvidence.evidence.confirmedBy,
                        };
                    }
                }
            }
        }
        return {
            ok: false,
            error_code: "consume_failed",
            reason: lastError,
            consume_attempts: attempts,
            consume_confirmation: consumptionEvidence.evidence.confirmedBy,
        };
    } finally {
        consumptionEvidence.dispose();
        ACTIVE_CONSUMERS.delete(bot);
    }
}

module.exports = {
    HEALING_POTION,
    normalizeConsumableName,
    itemMatchesConsumable,
    findConsumableItem,
    countConsumableItems,
    consumeWithRetry,
};
