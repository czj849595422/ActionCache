"use strict";

const fs = require("fs");
const path = require("path");

const ENABLED_TASKS = new Set(["pvp", "monster_clearance", "boss_combat"]);
// Registry metadata is optional. Keep contention waits short because this
// module runs on the bridge's Node event loop and is called during perception.
const LOCK_WAIT_MS = 2;
const LOCK_TIMEOUT_MS = 20;
const LOCK_CONTENTION_CODES = new Set(["EEXIST", "EPERM", "EACCES", "EBUSY"]);
const localIdentifiers = new WeakMap();

function enabled() {
    const task = String(process.env.MC_TASK_CATEGORY || "").trim();
    return ENABLED_TASKS.has(task) && Boolean(process.env.EQUIPMENT_REGISTRY_FILE);
}

function registryPath() {
    return path.resolve(String(process.env.EQUIPMENT_REGISTRY_FILE));
}

function initialState() {
    return { version: 1, nextId: 1, items: {}, history: [] };
}

function readState() {
    try {
        const parsed = JSON.parse(fs.readFileSync(registryPath(), "utf8"));
        if (!parsed || typeof parsed !== "object") return initialState();
        return {
            version: 1,
            nextId: Number.isInteger(parsed.nextId) && parsed.nextId > 0 ? parsed.nextId : 1,
            items: parsed.items && typeof parsed.items === "object" ? parsed.items : {},
            history: Array.isArray(parsed.history) ? parsed.history : [],
        };
    } catch (err) {
        if (err.code !== "ENOENT") throw err;
        return initialState();
    }
}

function sleepSync(ms) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function withLock(callback) {
    // Registry metadata is optional; never block the bridge long enough to miss keepalives.
    const target = registryPath();
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const lock = `${target}.lock`;
    const started = Date.now();
    let acquired = false;
    while (true) {
        try {
            fs.mkdirSync(lock);
            acquired = true;
            break;
        } catch (err) {
            if (!LOCK_CONTENTION_CODES.has(err.code)) throw err;
            if (Date.now() - started >= LOCK_TIMEOUT_MS) {
                return null;
            }
            sleepSync(LOCK_WAIT_MS);
        }
    }
    if (!acquired) return null;
    try {
        return callback();
    } finally {
        fs.rmSync(lock, { recursive: true, force: true });
    }
}

function writeState(state) {
    const target = registryPath();
    const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
    try {
        fs.writeFileSync(temporary, JSON.stringify(state, null, 2), "utf8");
        fs.renameSync(temporary, target);
    } finally {
        try {
            fs.rmSync(temporary, { force: true });
        } catch (_) {
            // The successful rename already removed the temporary path.
        }
    }
}

function stableValue(value, seen = new WeakSet()) {
    if (value === null || value === undefined) return value;
    if (typeof value !== "object") return value;
    if (seen.has(value)) return "[Circular]";
    seen.add(value);
    if (Array.isArray(value)) return value.map((entry) => stableValue(entry, seen));
    const result = {};
    for (const key of Object.keys(value).sort()) {
        if (["Damage", "damage", "RepairCost", "repairCost"].includes(key)) continue;
        if (key === "value" && Object.keys(value).length === 1) {
            return stableValue(value[key], seen);
        }
        result[key] = stableValue(value[key], seen);
    }
    return result;
}

function itemName(item) {
    return String(item && (item.name || item.id || "")).replace(/^minecraft:/, "").toLowerCase();
}

function itemSignature(item) {
    const payload = {
        name: itemName(item),
        metadata: item && item.metadata !== undefined ? item.metadata : null,
        nbt: stableValue(item && item.nbt ? item.nbt : null),
    };
    return JSON.stringify(payload);
}

function nestedValue(value, key) {
    if (!value || typeof value !== "object") return null;
    if (Object.prototype.hasOwnProperty.call(value, key)) {
        return unwrapNbtValue(value[key]);
    }
    for (const child of Object.values(value)) {
        const found = nestedValue(child, key);
        if (found !== null && found !== undefined) return found;
    }
    return null;
}

function unwrapNbtValue(value) {
    let current = value;
    while (
        current
        && typeof current === "object"
        && Object.prototype.hasOwnProperty.call(current, "value")
        && (Object.prototype.hasOwnProperty.call(current, "type") || Object.keys(current).length === 1)
    ) {
        current = current.value;
    }
    if (Array.isArray(current)) return current.map(unwrapNbtValue);
    if (current && typeof current === "object") {
        const result = {};
        for (const [key, child] of Object.entries(current)) result[key] = unwrapNbtValue(child);
        return result;
    }
    return current;
}

function enchantments(item) {
    const raw = nestedValue(item && item.nbt, "Enchantments") || nestedValue(item && item.nbt, "ench") || [];
    if (!Array.isArray(raw)) return [];
    return raw
        .filter((entry) => entry && typeof entry === "object")
        .map((entry) => ({
            id: String(entry.id && entry.id.value !== undefined ? entry.id.value : entry.id || "").replace(/^minecraft:/, ""),
            level: Number(entry.lvl && entry.lvl.value !== undefined ? entry.lvl.value : entry.lvl || 0),
        }))
        .filter((entry) => entry.id);
}

function durability(item) {
    const maximum = Number(item && item.maxDurability);
    const used = Number(item && item.durabilityUsed);
    return {
        maxDurability: Number.isFinite(maximum) ? maximum : null,
        durabilityUsed: Number.isFinite(used) ? used : null,
    };
}

function isEquipmentLike(item) {
    const name = itemName(item);
    return Boolean(name) && (
        name.endsWith("_sword")
        || name.endsWith("_axe")
        || name.endsWith("_helmet")
        || name.endsWith("_chestplate")
        || name.endsWith("_leggings")
        || name.endsWith("_boots")
        || name === "turtle_helmet"
        || name === "elytra"
        || name === "shield"
        || name === "bow"
        || name === "crossbow"
        || name === "trident"
    );
}

function identifierForRecord(state, record) {
    return record ? record.identifier : null;
}

function updateRecord(record, owner, item, location) {
    const wear = durability(item);
    record.owner = owner || null;
    record.name = itemName(item);
    record.signature = itemSignature(item);
    record.slot = Number.isInteger(Number(item && item.slot)) ? Number(item.slot) : null;
    record.location = location || "unknown";
    record.enchantments = enchantments(item);
    record.maxDurability = wear.maxDurability;
    record.durabilityUsed = wear.durabilityUsed;
    record.updatedAt = Date.now();
}

function registerObservedItem(owner, item, location = "unknown") {
    if (!enabled() || !isEquipmentLike(item)) return null;
    const cached = localIdentifiers.get(item);
    try {
        return withLock(() => {
            const state = readState();
            const signature = itemSignature(item);
            const name = itemName(item);
            let record = cached ? state.items[cached] : null;
            if (!record || (record.owner && record.owner !== owner && location !== "dropped")) {
                const candidates = Object.values(state.items).filter((candidate) => {
                    if (!candidate || candidate.name !== name) return false;
                    if (candidate.owner === owner && candidate.slot === Number(item.slot)) return true;
                    if (candidate.signature !== signature) return false;
                    if (location === "dropped" && candidate.location === "transferred") return true;
                    if (owner && location === "equipment" && candidate.owner === owner && candidate.location === "inventory") return true;
                    if (candidate.owner === owner && candidate.location === "transferred") return true;
                    return candidate.owner === null || candidate.location === "dropped";
                });
                record = candidates.sort((a, b) => {
                    const aExact = a.owner === owner && a.slot === Number(item.slot) ? 0 : 1;
                    const bExact = b.owner === owner && b.slot === Number(item.slot) ? 0 : 1;
                    return aExact - bExact || String(a.identifier).localeCompare(String(b.identifier));
                })[0] || null;
            }
            if (!record) {
                const identifier = `${itemName(item)}_${state.nextId++}`;
                record = {
                    identifier,
                    name: itemName(item),
                    owner: owner || null,
                    slot: null,
                    location: location || "unknown",
                    signature,
                    enchantments: [],
                    maxDurability: null,
                    durabilityUsed: null,
                    createdAt: Date.now(),
                    updatedAt: Date.now(),
                };
                state.items[identifier] = record;
            }
            updateRecord(record, owner, item, location);
            state.items[record.identifier] = record;
            writeState(state);
            localIdentifiers.set(item, record.identifier);
            return { ...record };
        });
    } catch (_) {
        return null;
    }
}

function annotate(owner, item, location = "unknown") {
    if (!enabled() || !item || !isEquipmentLike(item)) return {};
    const record = registerObservedItem(owner, item, location);
    if (!record) return {};
    return {
        identifier: identifierForRecord(null, record),
        owner: record.owner,
        enchantments: record.enchantments,
        registryMaxDurability: record.maxDurability,
        registryDurabilityUsed: record.durabilityUsed,
    };
}

function cached(item) {
    const identifier = item && localIdentifiers.get(item);
    if (!identifier) return {};
    return { identifier };
}

function findByIdentifier(owner, identifier, items, location = "inventory") {
    if (!enabled() || typeof identifier !== "string") return null;
    const normalized = identifier.trim().toLowerCase();
    const candidates = Array.from(items || []).filter(Boolean);
    for (const item of candidates) {
        const record = registerObservedItem(owner, item, location);
        if (record && record.identifier.toLowerCase() === normalized) return item;
    }
    return null;
}

function enchantmentPriority(item) {
    const entries = enchantments(item);
    return entries.length > 0 ? 1 : 0;
}

function sortNameMatches(items) {
    return Array.from(items || []).filter(Boolean).sort((a, b) => (
        enchantmentPriority(b) - enchantmentPriority(a)
        || Number(a.slot ?? Infinity) - Number(b.slot ?? Infinity)
    ));
}

function resolve(owner, input, items, matchesName, location = "inventory") {
    if (!enabled()) return null;
    if (typeof input !== "string") return null;
    const identifier = findByIdentifier(owner, input, items, location);
    if (identifier) return identifier;
    return sortNameMatches(Array.from(items || []).filter((item) => matchesName(item, input)))[0] || null;
}

function markTransfer(owner, item, targetOwner, location = "inventory") {
    if (!enabled() || !item) return null;
    const record = registerObservedItem(owner, item, location);
    if (!record) return null;
    try {
        return withLock(() => {
            const state = readState();
            const current = state.items[record.identifier];
            if (!current) return null;
            current.owner = targetOwner || null;
            current.location = targetOwner ? "transferred" : location;
            current.updatedAt = Date.now();
            current.history = Array.isArray(current.history) ? current.history : [];
            current.history.push({ from: owner || null, to: targetOwner || null, location, at: Date.now() });
            writeState(state);
            return { ...current };
        });
    } catch (_) {
        return null;
    }
}

function markDropped(owner, item) {
    return markTransfer(owner, item, null, "dropped");
}

module.exports = {
    enabled,
    annotate,
    cached,
    resolve,
    sortNameMatches,
    markTransfer,
    markDropped,
    itemSignature,
    enchantments,
    isEquipmentLike,
};
