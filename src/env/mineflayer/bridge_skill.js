/**
 * bridge.js - Mineflayer 桥接服务器
 *
 * 在 Node.js 进程中创建 Mineflayer bot，通过 HTTP API 暴露控制接口。
 * Python 端通过 HTTP 请求发送指令，避免 JSPyBridge 的事件注册问题。
 *
 * API:
 *   POST /start   - 创建 bot 并连接服务器
 *   POST /control - 键盘控制 (setControlState)
 *   POST /lookAt  - 看向坐标（异步等待完成）
 *   POST /look    - 设置视角角度（异步等待完成）
 *   POST /action  - 通用动作调用
 *   GET  /skill/list - 查询 skill/runtime 能力
 *   POST /skill/execute - 执行非持续 skill
 *   POST /skill/start - 启动持续 skill
 *   POST /skill/update - 更新持续 skill 参数
 *   POST /skill/stop - 停止持续 skill
 *   GET  /skill/status - 查询 skill action 状态
 *   GET  /perception/raw - 查询原始感知信息
 *   POST /viewer/start - 启动默认 prismarine-viewer 网页视角
 *   POST /viewer/stop - 关闭默认 prismarine-viewer 网页视角
 *   GET  /viewer/status - 查询默认 prismarine-viewer 状态
 *   POST /viewers/start - 启动一个或多个 prismarine-viewer channel
 *   POST /viewers/stop - 关闭全部 prismarine-viewer channel
 *   GET  /viewers/status - 查询全部 prismarine-viewer channel 状态
 *   GET  /status  - 查询 bot 状态
 *   POST /stop    - 断开连接
 */

const express = require("express");
const path = require("path");
const prismarineNbt = require("prismarine-nbt");
const { SkillRuntime } = require("./skill_runtime/runtime");
const {
    DEFAULT_PERCEPTION_RADIUS,
    configuredPerceptionRadius,
    effectivePerceptionRadius,
} = require("./perception_config");
const equipmentRegistry = require("./equipment_registry");
const { PERCEPTION_ITEM_OPTIONS } = require("./perception_item_options");

const app = express();
app.use(express.json());

const PORT = process.env.BRIDGE_PORT || 3100;
const PERCEPTION_BLOCK_YIELD_INTERVAL = 256;

function yieldToEventLoop() {
    return new Promise((resolve) => setImmediate(resolve));
}

function actionSetFromEnv(name) {
    const raw = process.env[name];
    if (!raw) return name === "SKILL_ACTION_ALLOWLIST" ? null : [];
    try {
        const value = JSON.parse(raw);
        if (!Array.isArray(value) || value.some(item => typeof item !== "string" || !item.trim())) {
            throw new Error("expected a JSON string array");
        }
        return value.map(item => item.trim());
    } catch (err) {
        throw new Error(`${name} is invalid: ${err.message}`);
    }
}

function stringSetFromEnv(name) {
    const values = actionSetFromEnv(name);
    return new Set(values);
}

const tradeQueryableItems = new Set(
    Array.from(stringSetFromEnv("TRADE_QUERYABLE_ITEMS"), (item) => (
        item.toLowerCase().replace(/^minecraft:/, "")
    )),
);

const skillRuntime = new SkillRuntime({
    // 本项目 skills 位于仓库根目录 src/skills（D:\MC\ActionCache\src\skills）。
    skillsDir: path.resolve(__dirname, "..", "..", "skills"),
    getBot: () => bot,
    getPerception: (options) => collectRawPerception(options || {}),
    getPerceptionRadius: () => botState.perceptionRadius ?? configuredPerceptionRadius(),
    getAutoPickup: () => botState.autoPickup ?? false,
    setAutoPickup: (value) => {
        botState.autoPickup = Boolean(value);
    },
    getTaskCategory: () => botState.taskCategory ?? null,
    getTradeQueryableItems: () => tradeQueryableItems,
    allowedActions: actionSetFromEnv("SKILL_ACTION_ALLOWLIST"),
    internalActions: actionSetFromEnv("SKILL_INTERNAL_ACTIONS"),
});

const vec3 = require("vec3");

let bot = null;
let httpServer = null;
let botState = {
    spawned: false,
    username: null,
    options: null,
    connectionState: "idle",
    lastDisconnectReason: null,
    disconnectCount: 0,
    perceptionRadius: configuredPerceptionRadius(),
    autoPickup: false,
    taskCategory: process.env.MC_TASK_CATEGORY || null,
};
let selfAttackState = {
    eventSequence: 0,
    lastAttackAt: null,
};
const DEFAULT_VIEWER_CHANNEL_ID = "default";
let viewerChannels = {};

const DEFAULT_SKIN_PARTS = {
    showCape: true,
    showJacket: true,
    showLeftSleeve: true,
    showRightSleeve: true,
    showLeftPants: true,
    showRightPants: true,
    showHat: true,
};
const OBSERVER_USERNAME_PREFIXES = ["observer"];

function numberOrDefault(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function nullableNumber(value) {
    if (value === undefined || value === null || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function stringOrNull(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === "string") return value;
    try {
        return String(value);
    } catch (err) {
        return null;
    }
}

function isObserverUsername(username) {
    if (typeof username !== "string") return false;
    const normalized = username.trim().toLowerCase();
    return OBSERVER_USERNAME_PREFIXES.some(prefix => normalized.startsWith(prefix));
}

function isSpectatorGameMode(value) {
    if (value === 3) return true;
    if (value === undefined || value === null) return false;
    const normalized = String(value).trim().toLowerCase();
    return normalized === "3" || normalized === "spectator";
}

function isSpectatorPlayer(username) {
    if (!username || !bot || !bot.players) return false;
    const player = bot.players[username];
    return player ? isSpectatorGameMode(player.gamemode) : false;
}

function normalizeAuth(value) {
    const auth = typeof value === "string" && value.trim() ? value.trim() : "offline";
    if (auth !== "offline") {
        throw new Error(`Unsupported auth mode '${auth}'. This project runs on a Minecraft server with online-mode=false and uses Mineflayer auth='offline'.`);
    }
    return auth;
}

function normalizeVersion(value) {
    if (value === undefined || value === null || value === "") return undefined;
    return String(value);
}

function normalizeViewDistance(value) {
    if (value === undefined || value === null || value === "") return "far";
    if (typeof value === "number" && Number.isFinite(value) && value > 0) return value;
    const text = String(value).trim();
    const asNumber = Number(text);
    if (Number.isFinite(asNumber) && asNumber > 0) return asNumber;
    if (["far", "normal", "short", "tiny"].includes(text)) return text;
    return "far";
}

function normalizeViewerViewDistance(value) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return Math.floor(parsed);
    return 6;
}

function normalizeViewerPort(value) {
    const fallback = Number(process.env.VIEWER_PORT || Number(PORT) + 1000);
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed > 0 && parsed < 65536) return parsed;
    if (Number.isInteger(fallback) && fallback > 0 && fallback < 65536) return fallback;
    return 4100;
}

function normalizePrefix(value) {
    if (value === undefined || value === null || value === "") return "";
    const text = String(value).trim();
    if (!text) return "";
    return text.startsWith("/") ? text : `/${text}`;
}

function normalizeMainHand(value) {
    if (value === "left" || value === "right") return value;
    return "right";
}

function normalizeSkinParts(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return { ...DEFAULT_SKIN_PARTS };
    }
    const result = { ...DEFAULT_SKIN_PARTS };
    for (const key of Object.keys(DEFAULT_SKIN_PARTS)) {
        if (Object.prototype.hasOwnProperty.call(value, key)) {
            result[key] = Boolean(value[key]);
        }
    }
    return result;
}

function positionToPlain(position) {
    if (!position) return null;
    return { x: position.x, y: position.y, z: position.z };
}

function vectorToPlain(vector) {
    if (!vector) return null;
    return { x: vector.x, y: vector.y, z: vector.z };
}

function sanitize(value, depth = 2, seen = new WeakSet()) {
    if (value === undefined) return null;
    if (value === null) return null;
    const valueType = typeof value;
    if (valueType === "string" || valueType === "number" || valueType === "boolean") return value;
    if (valueType === "bigint") return value.toString();
    if (valueType === "function") return null;
    if (depth <= 0) return stringOrNull(value);
    if (Array.isArray(value)) {
        return value.slice(0, 64).map((item) => sanitize(item, depth - 1, seen));
    }
    if (valueType === "object") {
        if (seen.has(value)) return "[Circular]";
        seen.add(value);
        const result = {};
        for (const [key, item] of Object.entries(value).slice(0, 64)) {
            result[key] = sanitize(item, depth - 1, seen);
        }
        seen.delete(value);
        return result;
    }
    return stringOrNull(value);
}

function sanitizeComplete(value, depth = 32, seen = new WeakSet()) {
    if (value === undefined || value === null) return null;
    const valueType = typeof value;
    if (valueType === "string" || valueType === "number" || valueType === "boolean") return value;
    if (valueType === "bigint") return value.toString();
    if (valueType === "function") return null;
    if (depth <= 0) return stringOrNull(value);
    if (Array.isArray(value)) {
        return value.map((item) => sanitizeComplete(item, depth - 1, seen));
    }
    if (valueType === "object") {
        if (seen.has(value)) return "[Circular]";
        seen.add(value);
        const result = {};
        for (const [key, item] of Object.entries(value)) {
            result[key] = sanitizeComplete(item, depth - 1, seen);
        }
        seen.delete(value);
        return result;
    }
    return stringOrNull(value);
}

function nbtToPlain(value) {
    if (!value) return null;
    try {
        return sanitizeComplete(prismarineNbt.simplify(value));
    } catch (err) {
        return sanitizeComplete(value);
    }
}

function itemToPlain(item, options = {}) {
    if (!item) return null;
    const register = options.register !== false;
    const owner = Object.prototype.hasOwnProperty.call(options, "owner")
        ? options.owner
        : (bot ? bot.username : null);
    const registered = register
        ? equipmentRegistry.annotate(owner, item, options.location || "unknown")
        : {};
    return {
        type: item.type ?? null,
        name: item.name ?? null,
        displayName: stringOrNull(item.displayName),
        count: item.count ?? null,
        slot: item.slot ?? null,
        metadata: item.metadata ?? null,
        stackSize: item.stackSize ?? null,
        maxDurability: item.maxDurability ?? null,
        durabilityUsed: item.durabilityUsed ?? null,
        nbt: nbtToPlain(item.nbt),
        ...registered,
    };
}

function getDroppedItemPlain(entity, options = PERCEPTION_ITEM_OPTIONS) {
    if (!entity || typeof entity.getDroppedItem !== "function") return null;
    try {
        return itemToPlain(entity.getDroppedItem(), options);
    } catch (err) {
        return null;
    }
}

function itemArrayToPlain(items, options = {}) {
    if (!items) return [];
    return Array.from(items).filter(Boolean).map((item) => itemToPlain(item, options));
}

function equipmentSlotsToPlain(equipment, options = {}) {
    const items = equipment || [];
    return {
        mainHand: itemToPlain(items[0], options),
        offHand: itemToPlain(items[1], options),
        feet: itemToPlain(items[2], options),
        legs: itemToPlain(items[3], options),
        chest: itemToPlain(items[4], options),
        head: itemToPlain(items[5], options),
    };
}

function harvestToolNames(harvestTools) {
    if (!harvestTools || typeof harvestTools !== "object") return [];
    const registryItems = bot && bot.registry ? bot.registry.items : null;
    return Object.entries(harvestTools)
        .filter(([, enabled]) => Boolean(enabled))
        .map(([toolId]) => {
            const numericId = Number(toolId);
            const item = registryItems && Number.isInteger(numericId)
                ? registryItems[numericId]
                : null;
            return item && item.name ? item.name : String(toolId);
        })
        .sort();
}

function effectsToPlain(effects) {
    if (!effects || typeof effects !== "object") return {};
    const registryEffects = bot && bot.registry ? bot.registry.effects : null;
    const result = {};
    for (const [key, effect] of Object.entries(effects)) {
        if (!effect || typeof effect !== "object") continue;
        const effectId = Number(effect.id ?? key);
        const registryEffect = registryEffects && Number.isInteger(effectId)
            ? registryEffects[effectId]
            : null;
        const registryName = registryEffect && registryEffect.name
            ? String(registryEffect.name).toLowerCase().replace(/\s+/g, "_")
            : null;
        result[key] = {
            ...sanitizeComplete(effect),
            name: registryName,
            displayName: registryEffect ? stringOrNull(registryEffect.displayName) : null,
        };
    }
    return result;
}

function attributeValue(attribute) {
    if (!attribute || typeof attribute.value !== "number") return null;
    const modifiers = Array.isArray(attribute.modifiers) ? attribute.modifiers : [];
    let base = attribute.value;
    for (const modifier of modifiers) {
        if (modifier.operation === 0) base += Number(modifier.amount || 0);
    }
    let value = base;
    for (const modifier of modifiers) {
        if (modifier.operation === 1) value += base * Number(modifier.amount || 0);
    }
    for (const modifier of modifiers) {
        if (modifier.operation === 2) value += value * Number(modifier.amount || 0);
    }
    return value;
}

function maxHealthOf(entity) {
    if (!entity || !entity.attributes) return null;
    const attribute = entity.attributes["minecraft:generic.max_health"]
        || entity.attributes["generic.max_health"];
    return attributeValue(attribute);
}

function entityHealth(entity) {
    // mineflayer 实体对象默认没有 health 字段；实测实体血量位于
    // entity.metadata 数组的 index 9（Minecraft 实体元数据 Health 槽位）。
    // 仅当存在明确数值时返回，否则保持 null，避免误判。
    if (!entity) return null;
    if (typeof entity.health === "number" && Number.isFinite(entity.health)) {
        return entity.health;
    }
    const metadata = entity.metadata;
    if (!metadata) return null;
    const value = metadata[9];
    if (typeof value === "number" && Number.isFinite(value)) {
        return value;
    }
    return null;
}

function distanceToSelf(position) {
    if (!bot || !bot.entity || !bot.entity.position || !position) return null;
    try {
        return bot.entity.position.distanceTo(position);
    } catch (err) {
        return null;
    }
}

function viewDirection(yaw, pitch) {
    const cosPitch = Math.cos(pitch || 0);
    return {
        x: -Math.sin(yaw || 0) * cosPitch,
        y: Math.sin(pitch || 0),
        z: -Math.cos(yaw || 0) * cosPitch,
    };
}

function vectorLength(vector) {
    return Math.sqrt(vector.x * vector.x + vector.y * vector.y + vector.z * vector.z);
}

function isInView(position, fovDegrees) {
    if (!bot || !bot.entity || !bot.entity.position || !position) return false;
    const origin = bot.entity.position.offset(0, bot.entity.eyeHeight || 1.62, 0);
    const target = {
        x: position.x - origin.x,
        y: position.y - origin.y,
        z: position.z - origin.z,
    };
    const targetLength = vectorLength(target);
    if (targetLength <= 0.0001) return true;
    const direction = viewDirection(bot.entity.yaw, bot.entity.pitch);
    const dot = (
        direction.x * target.x +
        direction.y * target.y +
        direction.z * target.z
    ) / targetLength;
    const halfFovRadians = (fovDegrees * Math.PI / 180) / 2;
    return dot >= Math.cos(halfFovRadians);
}

function entityToPlain(entity) {
    if (!entity) return null;
    const position = positionToPlain(entity.position);
    const isPlayer = Boolean(entity.username);
    const registryOptions = isPlayer
        ? { ...PERCEPTION_ITEM_OPTIONS, owner: entity.username, location: "equipment" }
        : PERCEPTION_ITEM_OPTIONS;
    return {
        id: entity.id ?? null,
        uuid: entity.uuid ?? null,
        type: entity.type ?? null,
        name: entity.name ?? null,
        displayName: stringOrNull(entity.displayName),
        username: entity.username ?? null,
        kind: entity.kind ?? null,
        position,
        velocity: vectorToPlain(entity.velocity),
        yaw: entity.yaw ?? null,
        pitch: entity.pitch ?? null,
        headYaw: entity.headYaw ?? null,
        orientationSource: "entity",
        height: entity.height ?? null,
        width: entity.width ?? null,
        distance: distanceToSelf(entity.position),
        health: entityHealth(entity),
        // 玩家（self）基础最大生命值为 20；attribute 缺失时兜底，保证
        // 观测中的生命值以"当前/最大"形式呈现。
        maxHealth: maxHealthOf(entity) ?? 20,
        onGround: entity.onGround ?? null,
        isBurning: entity.isBurning ?? entity.onFire ?? null,
        isValid: entity.isValid ?? null,
        droppedItem: getDroppedItemPlain(
            entity,
            typeof entity.getDroppedItem === "function"
                ? { ...PERCEPTION_ITEM_OPTIONS, owner: null, location: "dropped" }
                : registryOptions,
        ),
        heldItem: itemToPlain(entity.heldItem, registryOptions),
        equipment: itemArrayToPlain(entity.equipment || [], registryOptions),
        equipmentSlots: equipmentSlotsToPlain(entity.equipment || [], registryOptions),
        attributes: sanitize(entity.attributes || {}, 3),
        effects: effectsToPlain(entity.effects || {}),
        metadata: sanitize(entity.metadata || {}, 2),
        objectType: ["object", "projectile"].includes(entity.type)
            ? stringOrNull(entity.displayName)
            : null,
        mobType: ["hostile", "animal", "living", "mob"].includes(entity.type)
            ? stringOrNull(entity.displayName)
            : null,
    };
}

function blockToPlain(block) {
    if (!block) return null;
    let properties = block.properties || {};
    if (typeof block.getProperties === "function") {
        try {
            properties = block.getProperties();
        } catch (err) {
            properties = block.properties || {};
        }
    }
    return {
        type: block.type ?? null,
        name: block.name ?? null,
        displayName: stringOrNull(block.displayName),
        position: positionToPlain(block.position),
        distance: distanceToSelf(block.position),
        boundingBox: block.boundingBox ?? null,
        hardness: block.hardness ?? null,
        diggable: block.diggable ?? null,
        material: block.material ?? null,
        transparent: block.transparent ?? null,
        light: block.light ?? null,
        skyLight: block.skyLight ?? null,
        stateId: block.stateId ?? null,
        metadata: block.metadata ?? null,
        biome: sanitize(block.biome, 1),
        properties: sanitize(properties, 2),
        drops: sanitize(block.drops || [], 2),
        harvestTools: sanitize(block.harvestTools || {}, 2),
        harvestToolNames: harvestToolNames(block.harvestTools),
    };
}

function getBotStatus() {
    const entity = bot ? bot.entity : null;
    const position = entity && entity.position ? {
        x: entity.position.x,
        y: entity.position.y,
        z: entity.position.z,
    } : null;
    const selfState = getSelfState();

    return {
        running: bot !== null,
        spawned: botState.spawned,
        username: botState.username,
        connectionState: botState.connectionState,
        lastDisconnectReason: botState.lastDisconnectReason,
        disconnectCount: botState.disconnectCount,
        uuid: bot && bot._client ? bot._client.uuid ?? null : null,
        entityId: entity ? entity.id ?? null : null,
        auth: botState.options ? botState.options.auth : null,
        version: botState.options ? botState.options.version ?? null : null,
        settings: bot ? sanitize(bot.settings || {}, 2) : (botState.options ? botState.options.settings : null),
        position,
        yaw: entity ? entity.yaw : null,
        pitch: entity ? entity.pitch : null,
        headYaw: entity ? entity.headYaw ?? null : null,
        orientationSource: "status",
        viewer: getViewerStatus(),
        viewers: getViewerChannelsStatus(),
        self: selfState,
        health: selfState.health,
        food: selfState.food,
        foodSaturation: selfState.foodSaturation,
        oxygenLevel: selfState.oxygenLevel,
        gameMode: selfState.gameMode,
        dimension: selfState.dimension,
        heldItem: selfState.heldItem,
        inventory: selfState.inventory,
        equipment: selfState.equipment,
        effects: selfState.effects,
        controlState: selfState.controlState,
    };
}

function normalizeViewerChannelId(value) {
    if (value === undefined || value === null || value === "") return DEFAULT_VIEWER_CHANNEL_ID;
    const text = String(value).trim();
    if (!text) return DEFAULT_VIEWER_CHANNEL_ID;
    return text.replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 64) || DEFAULT_VIEWER_CHANNEL_ID;
}

function viewerChannelStatus(channel) {
    if (!channel) {
        return {
            id: DEFAULT_VIEWER_CHANNEL_ID,
            enabled: false,
            port: null,
            url: null,
            firstPerson: false,
            viewDistance: null,
            prefix: "",
            error: null,
        };
    }
    return {
        id: channel.id,
        enabled: Boolean(channel.enabled),
        port: channel.port ?? null,
        url: channel.url ?? null,
        firstPerson: Boolean(channel.firstPerson),
        viewDistance: channel.viewDistance ?? null,
        prefix: channel.prefix || "",
        error: channel.error || null,
        lastStopReason: channel.lastStopReason || null,
    };
}

function getViewerChannelsStatus() {
    const result = {};
    for (const [id, channel] of Object.entries(viewerChannels)) {
        result[id] = viewerChannelStatus(channel);
    }
    return result;
}

function getViewerStatus(channelId = DEFAULT_VIEWER_CHANNEL_ID) {
    const id = normalizeViewerChannelId(channelId);
    if (viewerChannels[id]) return viewerChannelStatus(viewerChannels[id]);
    const firstEnabled = Object.values(viewerChannels).find((channel) => channel && channel.enabled);
    if (firstEnabled) return viewerChannelStatus(firstEnabled);
    return viewerChannelStatus({ id, enabled: false });
}

function restoreBotViewerReference(excludingId = null) {
    if (!bot) return;
    const active = Object.values(viewerChannels).find((channel) => (
        channel &&
        channel.id !== excludingId &&
        channel.enabled &&
        channel.viewer
    ));
    bot.viewer = active ? active.viewer : null;
}

function stopViewerChannel(channelId = DEFAULT_VIEWER_CHANNEL_ID, reason = "viewer_stop") {
    const id = normalizeViewerChannelId(channelId);
    const channel = viewerChannels[id];
    if (!channel) {
        return viewerChannelStatus({
            id,
            enabled: false,
            lastStopReason: reason,
        });
    }
    if (channel.viewer && typeof channel.viewer.close === "function") {
        try {
            channel.viewer.close();
        } catch (err) {
            console.error(`[bridge] viewer ${id} close error: ${err.message}`);
        }
    }
    viewerChannels[id] = {
        id,
        enabled: false,
        port: channel.port ?? null,
        url: null,
        firstPerson: Boolean(channel.firstPerson),
        viewDistance: channel.viewDistance ?? null,
        prefix: channel.prefix || "",
        error: null,
        lastStopReason: reason,
    };
    restoreBotViewerReference(id);
    return viewerChannelStatus(viewerChannels[id]);
}

function stopViewer(reason = "viewer_stop") {
    const stopped = {};
    for (const id of Object.keys(viewerChannels)) {
        stopped[id] = stopViewerChannel(id, reason);
    }
    viewerChannels = {};
    if (bot) bot.viewer = null;
    return stopped;
}

function startViewerChannel(channelId = DEFAULT_VIEWER_CHANNEL_ID, options = {}) {
    if (!bot || !botState.spawned) {
        throw new Error("Bot not started or not spawned");
    }
    const id = normalizeViewerChannelId(channelId);
    if (viewerChannels[id] && viewerChannels[id].enabled) {
        return getViewerStatus(id);
    }

    const port = normalizeViewerPort(options.port);
    const firstPerson = Boolean(options.firstPerson);
    const viewDistance = normalizeViewerViewDistance(options.viewDistance);
    const prefix = normalizePrefix(options.prefix);

    try {
        const mineflayerViewer = require("prismarine-viewer").mineflayer;
        mineflayerViewer(bot, { port, firstPerson, viewDistance, prefix });
        const channelViewer = bot.viewer;
        viewerChannels[id] = {
            id,
            enabled: true,
            port,
            url: `http://localhost:${port}${prefix}`,
            firstPerson,
            viewDistance,
            prefix,
            error: null,
            viewer: channelViewer,
        };
        return getViewerStatus(id);
    } catch (err) {
        viewerChannels[id] = {
            id,
            enabled: false,
            port,
            url: null,
            firstPerson,
            viewDistance,
            prefix,
            error: err.message,
        };
        throw err;
    }
}

function startViewer(options = {}) {
    return startViewerChannel(DEFAULT_VIEWER_CHANNEL_ID, options);
}

function startViewerChannels(options = {}) {
    const rawChannels = Array.isArray(options.channels) ? options.channels : [options];
    const results = {};
    for (const raw of rawChannels) {
        const channel = raw || {};
        const id = normalizeViewerChannelId(channel.id || channel.channelId || channel.channel_id);
        results[id] = startViewerChannel(id, channel);
    }
    return results;
}

async function stopCurrentBot(reason) {
    const currentBot = bot;
    stopViewer(reason);
    if (currentBot) {
        try {
            await skillRuntime.detachBot(reason);
            currentBot.quit();
        } catch (err) {
            console.error(`[bridge] Error quitting bot: ${err}`);
        }
    }
    if (bot === currentBot) bot = null;
    botState.spawned = false;
    botState.username = null;
    botState.options = null;
    botState.connectionState = "idle";
    botState.perceptionRadius = configuredPerceptionRadius();
    selfAttackState = { eventSequence: 0, lastAttackAt: null };
}

function getSelfState() {
    const entity = bot ? bot.entity : null;
    return {
        username: bot ? bot.username : null,
        entity: entityToPlain(entity),
        position: entity ? positionToPlain(entity.position) : null,
        velocity: entity ? vectorToPlain(entity.velocity) : null,
        yaw: entity ? entity.yaw : null,
        pitch: entity ? entity.pitch : null,
        headYaw: entity ? entity.headYaw ?? null : null,
        orientationSource: "self",
        health: bot ? bot.health : null,
        attackEventSequence: selfAttackState.eventSequence,
        lastAttackAt: selfAttackState.lastAttackAt,
        maxHealth: maxHealthOf(entity),
        absorption: bot ? bot.absorption ?? null : null,
        food: bot ? bot.food : null,
        maxFood: 20,
        foodSaturation: bot ? bot.foodSaturation : null,
        oxygenLevel: bot ? bot.oxygenLevel : null,
        maxOxygenLevel: 20,
        gameMode: bot && bot.game ? bot.game.gameMode : null,
        dimension: bot && bot.game ? bot.game.dimension : null,
        experience: bot ? sanitize(bot.experience || {}, 2) : null,
        quickBarSlot: bot ? bot.quickBarSlot : null,
        heldItem: bot ? itemToPlain(bot.heldItem, PERCEPTION_ITEM_OPTIONS) : null,
        inventory: bot && bot.inventory
            ? itemArrayToPlain(bot.inventory.items(), PERCEPTION_ITEM_OPTIONS)
            : [],
        equipment: entity
            ? itemArrayToPlain(entity.equipment || [], PERCEPTION_ITEM_OPTIONS)
            : [],
        equipmentSlots: entity
            ? equipmentSlotsToPlain(entity.equipment || [], PERCEPTION_ITEM_OPTIONS)
            : equipmentSlotsToPlain([], PERCEPTION_ITEM_OPTIONS),
        effects: entity ? effectsToPlain(entity.effects || {}) : {},
        controlState: bot ? sanitize(bot.controlState || {}, 1) : {},
        isInWater: entity ? entity.isInWater : null,
        isInLava: entity ? entity.isInLava : null,
        isSleeping: entity ? entity.isSleeping : null,
        isBurning: entity ? entity.isBurning ?? entity.onFire ?? null : null,
        onGround: entity ? entity.onGround : null,
    };
}

function getWorldEnvironment() {
    const time = bot && bot.time ? bot.time : {};
    const game = bot && bot.game ? bot.game : {};
    return {
        dimension: game.dimension ?? null,
        difficulty: game.difficulty ?? null,
        gameMode: game.gameMode ?? null,
        time: {
            timeOfDay: time.timeOfDay ?? null,
            day: time.day ?? null,
            isDay: time.isDay ?? null,
            moonPhase: time.moonPhase ?? null,
            doDaylightCycle: time.doDaylightCycle ?? null,
        },
        weather: {
            isRaining: bot ? bot.isRaining ?? null : null,
            rainState: bot ? bot.rainState ?? null : null,
            thunderState: bot ? bot.thunderState ?? null : null,
        },
    };
}

function shouldIncludePosition(position, options) {
    const distance = distanceToSelf(position);
    if (options.radius !== null && distance !== null && distance > options.radius) return false;
    if (options.mode === "local" && options.subMode === "view") {
        return isInView(position, options.fovDegrees);
    }
    return true;
}

function collectEntities(options) {
    const entities = [];
    for (const entity of Object.values(bot.entities || {})) {
        if (!entity) continue;
        if (entity.username && entity.username === bot.username) continue;
        if (entity.username && isObserverUsername(entity.username)) continue;
        if (entity.username && isSpectatorPlayer(entity.username)) continue;
        const health = entityHealth(entity);
        if (health !== null && health <= 0) continue;
        if (!shouldIncludePosition(entity.position, options)) continue;
        const plain = entityToPlain(entity);
        if (plain) entities.push(plain);
    }
    entities.sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return entities;
}

function collectPlayers() {
    return Object.entries(bot.players || {})
        .filter(([username]) => {
            if (username === bot.username) return true;
            if (isObserverUsername(username)) return false;
            return !isSpectatorPlayer(username);
        })
        .map(([username, player]) => {
            const entity = player ? player.entity : null;
            return {
                username,
                displayName: player ? stringOrNull(player.displayName) : null,
                ping: player ? player.ping : null,
                gamemode: player ? player.gamemode : null,
                entity: entityToPlain(entity),
                isSelf: username === bot.username,
            };
        });
}

function droppedItemToPlain(entity) {
    const item = getDroppedItemPlain(
        entity,
        { ...PERCEPTION_ITEM_OPTIONS, owner: null, location: "dropped" },
    );
    if (!item) return null;
    return {
        entityId: entity.id ?? null,
        uuid: entity.uuid ?? null,
        entityType: entity.type ?? null,
        entityName: entity.name ?? null,
        displayName: stringOrNull(entity.displayName),
        objectType: stringOrNull(entity.displayName),
        position: positionToPlain(entity.position),
        velocity: vectorToPlain(entity.velocity),
        yaw: entity.yaw ?? null,
        pitch: entity.pitch ?? null,
        distance: distanceToSelf(entity.position),
        onGround: entity.onGround ?? null,
        isValid: entity.isValid ?? null,
        item,
        metadata: sanitize(entity.metadata || {}, 2),
    };
}

function collectDroppedItems(options) {
    const items = [];
    for (const entity of Object.values(bot.entities || {})) {
        if (!entity) continue;
        if (!shouldIncludePosition(entity.position, options)) continue;
        const droppedItem = droppedItemToPlain(entity);
        if (droppedItem) items.push(droppedItem);
    }
    items.sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return items;
}

function collectBlocks(options) {
    const blockRadius = Math.max(1, Math.min(16, Math.floor(options.blockRadius)));
    const origin = bot.entity.position;
    const baseX = Math.floor(origin.x);
    const baseY = Math.floor(origin.y);
    const baseZ = Math.floor(origin.z);
    const blocks = [];
    let scanned = 0;
    let air = 0;
    let unloaded = 0;

    for (let dx = -blockRadius; dx <= blockRadius; dx += 1) {
        for (let dy = -blockRadius; dy <= blockRadius; dy += 1) {
            for (let dz = -blockRadius; dz <= blockRadius; dz += 1) {
                scanned += 1;
                const pos = vec3(baseX + dx, baseY + dy, baseZ + dz);
                let block = null;
                try {
                    block = bot.blockAt(pos, false);
                } catch (err) {
                    unloaded += 1;
                    continue;
                }
                if (!block) {
                    unloaded += 1;
                    continue;
                }
                if (block.name === "air" || block.name === "cave_air" || block.name === "void_air") {
                    air += 1;
                    continue;
                }
                const blockCenter = block.position.offset(0.5, 0.5, 0.5);
                if (!shouldIncludePosition(blockCenter, options)) continue;
                const plain = blockToPlain(block);
                if (plain) blocks.push(plain);
            }
        }
    }
    blocks.sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return { blocks, scanned, air, unloaded, blockRadius };
}

async function collectBlocksAsync(options) {
    const blockRadius = Math.max(1, Math.min(16, Math.floor(options.blockRadius)));
    const origin = bot.entity.position;
    const baseX = Math.floor(origin.x);
    const baseY = Math.floor(origin.y);
    const baseZ = Math.floor(origin.z);
    const blocks = [];
    let scanned = 0;
    let air = 0;
    let unloaded = 0;

    for (let dx = -blockRadius; dx <= blockRadius; dx += 1) {
        for (let dy = -blockRadius; dy <= blockRadius; dy += 1) {
            for (let dz = -blockRadius; dz <= blockRadius; dz += 1) {
                scanned += 1;
                const pos = vec3(baseX + dx, baseY + dy, baseZ + dz);
                let block = null;
                let blockLookupFailed = false;
                try {
                    block = bot.blockAt(pos, false);
                } catch (err) {
                    unloaded += 1;
                    blockLookupFailed = true;
                }
                if (!block) {
                    if (!blockLookupFailed) unloaded += 1;
                } else if (block.name === "air" || block.name === "cave_air" || block.name === "void_air") {
                    air += 1;
                } else {
                    const blockCenter = block.position.offset(0.5, 0.5, 0.5);
                    if (shouldIncludePosition(blockCenter, options)) {
                        const plain = blockToPlain(block);
                        if (plain) blocks.push(plain);
                    }
                }

                if (scanned % PERCEPTION_BLOCK_YIELD_INTERVAL === 0) {
                    await yieldToEventLoop();
                }
            }
        }
    }
    blocks.sort((a, b) => (a.distance ?? Infinity) - (b.distance ?? Infinity));
    return { blocks, scanned, air, unloaded, blockRadius };
}

function collectRawPerception(options) {
    const entities = collectEntities(options);
    const players = collectPlayers();
    const items = collectDroppedItems(options);
    const blockResult = collectBlocks(options);
    return {
        self: getSelfState(),
        entities,
        players,
        items,
        blocks: blockResult.blocks,
        environment: getWorldEnvironment(),
        metadata: {
            mode: options.mode,
            subMode: options.subMode,
            radius: options.radius,
            blockRadius: blockResult.blockRadius,
            fovDegrees: options.fovDegrees,
            timestamp: Date.now(),
            counts: {
                entities: entities.length,
                players: players.length,
                items: items.length,
                blocks: blockResult.blocks.length,
                scannedBlocks: blockResult.scanned,
                airBlocks: blockResult.air,
                unloadedBlocks: blockResult.unloaded,
            },
        },
    };
}

async function collectRawPerceptionAsync(options) {
    const entities = collectEntities(options);
    await yieldToEventLoop();
    const players = collectPlayers();
    const items = collectDroppedItems(options);
    const blockResult = await collectBlocksAsync(options);
    return {
        self: getSelfState(),
        entities,
        players,
        items,
        blocks: blockResult.blocks,
        environment: getWorldEnvironment(),
        metadata: {
            mode: options.mode,
            subMode: options.subMode,
            radius: options.radius,
            blockRadius: blockResult.blockRadius,
            fovDegrees: options.fovDegrees,
            timestamp: Date.now(),
            counts: {
                entities: entities.length,
                players: players.length,
                items: items.length,
                blocks: blockResult.blocks.length,
                scannedBlocks: blockResult.scanned,
                airBlocks: blockResult.air,
                unloadedBlocks: blockResult.unloaded,
            },
        },
    };
}

// ============================================================
// POST /start - 创建 bot 并连接服务器
// ============================================================
app.post("/start", async (req, res) => {
    const { host, port, username } = req.body;
    const initialYaw = numberOrDefault(req.body.initialYaw, 0);
    const initialPitch = numberOrDefault(req.body.initialPitch, 0);
    let auth;
    try {
        auth = normalizeAuth(req.body.auth);
    } catch (err) {
        return res.status(400).json({ error: err.message });
    }
    const version = normalizeVersion(req.body.version);
    const viewDistance = normalizeViewDistance(req.body.viewDistance);
    const mainHand = normalizeMainHand(req.body.mainHand);
    const skinParts = normalizeSkinParts(req.body.skinParts);

    if (bot && botState.spawned) {
        return res.json({ status: "already_running", username: bot.username });
    }
    if (bot) {
        try {
            await stopCurrentBot("restart_after_disconnect");
        } catch (err) {
            console.error(`[bridge] stale bot cleanup failed: ${err.message}`);
            return res.status(503).json({
                status: "failed",
                error: "stale_bot_cleanup_failed",
                reason: err.message,
                retryable: true,
            });
        }
    }

    botState.perceptionRadius = configuredPerceptionRadius();
    botState.connectionState = "connecting";
    botState.lastDisconnectReason = null;

    const mineflayer = require("mineflayer");
    const botOptions = {
        host: host || "localhost",
        port: port || 25565,
        username: username || "bot",
        auth,
        viewDistance,
        mainHand,
        skinParts,
        hideErrors: false,
    };
    if (version) botOptions.version = version;
    botState.options = {
        auth,
        version: version || null,
        settings: { viewDistance, mainHand, skinParts },
    };
    console.log(`[bridge] createBot options: ${JSON.stringify(botState.options)}`);

    let startDone = false;
    let timeout = null;
    let capabilities = {};

    function reasonText(reason) {
        if (reason instanceof Error) return reason.message || String(reason);
        if (typeof reason === "string") return reason;
        try {
            return JSON.stringify(sanitize(reason, 2));
        } catch (err) {
            return String(reason);
        }
    }

    function finishStart(statusCode, payload) {
        if (startDone) return false;
        startDone = true;
        if (timeout) clearTimeout(timeout);
        if (!res.headersSent) {
            res.status(statusCode).json(payload);
        }
        return true;
    }

    function failStart(errorCode, reason, statusCode = 502) {
        if (startDone) return false;
        const text = reasonText(reason);
        console.error(`[bridge] start failed ${errorCode}: ${text}`);
        return finishStart(statusCode, {
            status: "failed",
            error: errorCode,
            reason: text,
            username: botOptions.username,
        });
    }

    try {
        bot = mineflayer.createBot(botOptions);
    } catch (err) {
        bot = null;
        botState.options = null;
        botState.connectionState = "disconnected";
        return failStart("create_bot_failed", err, 500);
    }
    const startedBot = bot;

    selfAttackState = {
        eventSequence: 0,
        lastAttackAt: null,
    };

    startedBot.on("entityHurt", (entity) => {
        if (bot !== startedBot || !startedBot.entity || !entity) return;
        if (String(entity.id) !== String(startedBot.entity.id)) return;
        selfAttackState.eventSequence += 1;
        selfAttackState.lastAttackAt = Date.now();
    });

    startedBot.on("kicked", (reason) => {
        if (bot !== startedBot) return;
        console.log(`[bridge] Bot kicked: ${reason}`);
        botState.connectionState = "disconnected";
        botState.lastDisconnectReason = reasonText(reason);
        botState.disconnectCount += 1;
        if (!botState.spawned) {
            failStart("bot_kicked", reason, 502);
        }
        botState.spawned = false;
    });

    startedBot.on("end", (reason) => {
        if (bot !== startedBot) return;
        console.log(`[bridge] Bot disconnected: ${reason}`);
        const endedBot = startedBot;
        const wasSpawned = botState.spawned;
        botState.connectionState = "disconnected";
        botState.lastDisconnectReason = reasonText(reason || "socket closed");
        botState.disconnectCount += 1;
        if (!wasSpawned) {
            failStart("bot_disconnected", reason || "socket closed before spawn", 502);
        }
        stopViewer("bot_end");
        botState.spawned = false;
        botState.options = null;
        botState.perceptionRadius = configuredPerceptionRadius();
        selfAttackState = { eventSequence: 0, lastAttackAt: null };
        skillRuntime.detachBot("bot_end").catch((err) => {
            console.error(`[bridge] skill cleanup error: ${err.message}`);
        }).finally(() => {
            if (bot === endedBot) bot = null;
        });
    });

    startedBot.on("error", (err) => {
        if (bot !== startedBot) return;
        console.error(`[bridge] Bot error: ${err}`);
        if (!botState.spawned) {
            failStart("bot_error", err, 502);
        }
    });

    try {
        capabilities = skillRuntime.attachBot(startedBot);
        console.log(`[bridge] Skill capabilities: ${JSON.stringify(capabilities)}`);
    } catch (err) {
        stopCurrentBot("skill_attach_failed").catch((cleanupErr) => {
            console.error(`[bridge] cleanup after skill attach failed: ${cleanupErr.message}`);
        });
        return failStart("skill_attach_failed", err, 500);
    }

    // 等待 spawn 事件
    timeout = setTimeout(() => {
        if (bot !== startedBot || botState.spawned) return;
        if (!botState.spawned) {
            if (finishStart(504, {
                status: "timeout",
                error: "spawn_timeout",
                message: "Spawn timeout (30s)",
                username: botOptions.username,
            })) {
                stopCurrentBot("spawn_timeout").catch((err) => {
                    console.error(`[bridge] cleanup after spawn timeout failed: ${err.message}`);
                });
            }
        }
    }, 30000);

    startedBot.once("spawn", async () => {
        if (bot !== startedBot) return;
        botState.spawned = true;
        botState.connectionState = "connected";
        botState.lastDisconnectReason = null;
        botState.username = startedBot.username;
        try {
            await startedBot.look(initialYaw, initialPitch, true);
            console.log(
                `[bridge] Bot ${startedBot.username} spawned yaw=${initialYaw} pitch=${initialPitch}`
            );
            if (bot !== startedBot) return;
            // spawn 后 mineflayer 插件已注入，刷新能力（如 minecrafthawkeye）。
            capabilities = skillRuntime.pluginManager.getCapabilities();
            console.log(
                `[bridge] Bot ${startedBot.username} capabilities after spawn: ` +
                `${JSON.stringify(capabilities)}`
            );
            finishStart(200, {
                status: "spawned",
                username: startedBot.username,
                yaw: startedBot.entity ? startedBot.entity.yaw : initialYaw,
                pitch: startedBot.entity ? startedBot.entity.pitch : initialPitch,
                capabilities,
            });
        } catch (err) {
            if (bot !== startedBot) return;
            console.error(`[bridge] initial look error: ${err.message}`);
            finishStart(500, {
                status: "failed",
                error: "initial_look_failed",
                reason: err.message,
                username: startedBot.username,
            });
        }
    });
});

// ============================================================
// POST /control - 键盘控制
// ============================================================
app.post("/control", (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });

    const { control, state } = req.body;
    console.log(`[bridge] control: ${control}=${state}`);
    try {
        bot.setControlState(control, state);
        res.json({ status: "ok", control, state });
    } catch (err) {
        console.error(`[bridge] control error: ${err.message}`);
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// POST /lookAt - 看向坐标（异步等待完成）
// ============================================================
app.post("/lookAt", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });

    const { x, y, z } = req.body;
    try {
        await bot.lookAt(vec3(x, y, z));
        res.json({ status: "ok", target: { x, y, z } });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// POST /look - 设置视角角度（异步等待完成）
// ============================================================
app.post("/look", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });

    const { yaw, pitch } = req.body;
    try {
        await bot.look(yaw, pitch);
        res.json({ status: "ok", yaw, pitch });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// POST /action - 通用动作调用
// ============================================================
app.post("/action", (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });

    const { action, args } = req.body;
    try {
        const result = bot[action](...(args || []));
        res.json({ status: "ok", result: result !== undefined ? String(result) : null });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// POST /clearControls - 清除所有按键状态
// ============================================================
app.post("/clearControls", (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });

    try {
        bot.clearControlStates();
        res.json({ status: "ok" });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// Prismarine viewer API
// ============================================================
app.post("/viewers/start", (req, res) => {
    try {
        const viewers = startViewerChannels(req.body || {});
        res.json({ status: "ok", viewers, viewer: getViewerStatus() });
    } catch (err) {
        console.error(`[bridge] viewers start error: ${err.message}`);
        res.status(500).json({ error: err.message, viewers: getViewerChannelsStatus(), viewer: getViewerStatus() });
    }
});

app.post("/viewers/stop", (req, res) => {
    try {
        const stopped = stopViewer("viewers_stop");
        res.json({ status: "ok", viewers: getViewerChannelsStatus(), stopped, viewer: getViewerStatus() });
    } catch (err) {
        res.status(500).json({ error: err.message, viewers: getViewerChannelsStatus(), viewer: getViewerStatus() });
    }
});

app.get("/viewers/status", (req, res) => {
    res.json({ status: "ok", viewers: getViewerChannelsStatus(), viewer: getViewerStatus() });
});

app.post("/viewers/:id/start", (req, res) => {
    try {
        const viewer = startViewerChannel(req.params.id, req.body || {});
        res.json({ status: "ok", viewer, viewers: getViewerChannelsStatus() });
    } catch (err) {
        console.error(`[bridge] viewer channel ${req.params.id} start error: ${err.message}`);
        res.status(500).json({ error: err.message, viewer: getViewerStatus(req.params.id), viewers: getViewerChannelsStatus() });
    }
});

app.post("/viewers/:id/stop", (req, res) => {
    try {
        const viewer = stopViewerChannel(req.params.id, "viewer_channel_stop");
        res.json({ status: "ok", viewer, viewers: getViewerChannelsStatus() });
    } catch (err) {
        res.status(500).json({ error: err.message, viewer: getViewerStatus(req.params.id), viewers: getViewerChannelsStatus() });
    }
});

app.get("/viewers/:id/status", (req, res) => {
    res.json({ status: "ok", viewer: getViewerStatus(req.params.id), viewers: getViewerChannelsStatus() });
});

app.post("/viewer/start", (req, res) => {
    try {
        const viewer = startViewer(req.body || {});
        res.json({ status: "ok", viewer, viewers: getViewerChannelsStatus() });
    } catch (err) {
        console.error(`[bridge] viewer start error: ${err.message}`);
        res.status(500).json({ error: err.message, viewer: getViewerStatus(), viewers: getViewerChannelsStatus() });
    }
});

app.post("/viewer/stop", (req, res) => {
    try {
        const viewer = stopViewerChannel(DEFAULT_VIEWER_CHANNEL_ID, "viewer_stop");
        res.json({ status: "ok", viewer, viewers: getViewerChannelsStatus() });
    } catch (err) {
        res.status(500).json({ error: err.message, viewer: getViewerStatus(), viewers: getViewerChannelsStatus() });
    }
});

app.get("/viewer/status", (req, res) => {
    res.json({ status: "ok", viewer: getViewerStatus(), viewers: getViewerChannelsStatus() });
});

// ============================================================
// GET /status - 查询 bot 状态
// ============================================================
app.get("/status", (req, res) => {
    res.json(getBotStatus());
});

// ============================================================
// GET /perception/raw - 查询原始感知信息
// ============================================================
app.get("/perception/raw", async (req, res) => {
    if (!bot || !botState.spawned) {
        return res.status(503).json({
            error: "Bot not started or not spawned",
            retryable: true,
        });
    }

    const requestedRadius = nullableNumber(req.query.radius);
    const radius = effectivePerceptionRadius(requestedRadius, botState.perceptionRadius);
    if (requestedRadius !== null && requestedRadius > 0) {
        botState.perceptionRadius = radius;
    }
    const blockRadius = nullableNumber(req.query.blockRadius) ?? 8;
    const options = {
        mode: req.query.mode || "global",
        subMode: req.query.subMode || "radius",
        radius,
        blockRadius,
        fovDegrees: numberOrDefault(req.query.fovDegrees, 90),
    };

    try {
        res.json(await collectRawPerceptionAsync(options));
    } catch (err) {
        console.error(`[bridge] perception error: ${err.message}`);
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// Skill runtime API
// ============================================================
function botUnavailable(res) {
    return res.status(503).json({
        error: "Bot not started or not spawned",
        retryable: true,
        connectionState: botState.connectionState,
    });
}

app.get("/skill/list", (req, res) => {
    try {
        res.json(skillRuntime.list());
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post("/skill/execute", async (req, res) => {
    if (!bot || !botState.spawned) {
        return botUnavailable(res);
    }
    try {
        res.json(await skillRuntime.execute(req.body || {}));
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post("/skill/start", async (req, res) => {
    if (!bot || !botState.spawned) {
        return botUnavailable(res);
    }
    try {
        res.json(await skillRuntime.start(req.body || {}));
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post("/skill/update", async (req, res) => {
    if (!bot || !botState.spawned) {
        return botUnavailable(res);
    }
    try {
        res.json(await skillRuntime.update(req.body || {}));
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.post("/skill/stop", async (req, res) => {
    if (!bot || !botState.spawned) {
        return botUnavailable(res);
    }
    try {
        res.json(await skillRuntime.stop(req.body || {}));
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.get("/skill/status", (req, res) => {
    if (!bot || !botState.spawned) {
        return botUnavailable(res);
    }
    try {
        res.json(skillRuntime.status(req.query.actionId || null));
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// ============================================================
// POST /stop - 断开连接
// ============================================================
app.post("/stop", async (req, res) => {
    await stopCurrentBot("bot_stop");
    res.json({ status: "stopped" });
});

// ============================================================
// POST /shutdown - 关闭 bridge 进程（供 Python 侧清理孤儿进程）
// ============================================================
app.post("/shutdown", async (req, res) => {
    await stopCurrentBot("bridge_shutdown");
    res.json({ status: "shutting_down" });
    setTimeout(() => {
        if (httpServer) {
            httpServer.close(() => process.exit(0));
        } else {
            process.exit(0);
        }
    }, 50);
});

// ============================================================
// 启动服务器
// ============================================================
httpServer = app.listen(PORT, () => {
    console.log(`[bridge] Bridge server started on port ${PORT}`);
});
