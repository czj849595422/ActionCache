"use strict";

function safeRequire(moduleName) {
    try {
        return { ok: true, module: require(moduleName) };
    } catch (err) {
        return { ok: false, error: err.message };
    }
}

class PluginManager {
    constructor() {
        this.capabilities = {};
        this.errors = {};
    }

    attach(bot) {
        this.capabilities = {};
        this.errors = {};
        this._loadPathfinder(bot);
        this._loadCollectBlock(bot);
        this._loadHawkEye(bot);
        return this.getCapabilities();
    }

    getCapabilities() {
        return {
            ...this.capabilities,
            errors: { ...this.errors },
        };
    }

    requireCapability(name) {
        if (!this.capabilities[name]) {
            const error = this.errors[name] || `Missing plugin capability: ${name}`;
            const err = new Error(error);
            err.code = "missing_plugin";
            throw err;
        }
    }

    _loadPathfinder(bot) {
        const pluginName = "mineflayer-pathfinder";
        const loaded = safeRequire(pluginName);
        if (!loaded.ok) {
            this.capabilities[pluginName] = false;
            this.errors[pluginName] = loaded.error;
            return;
        }

        try {
            bot.loadPlugin(loaded.module.pathfinder);
            this.capabilities[pluginName] = true;
            this.capabilities.pathfinder = true;
            bot.mcagentPathfinder = loaded.module;
        } catch (err) {
            this.capabilities[pluginName] = false;
            this.capabilities.pathfinder = false;
            this.errors[pluginName] = err.message;
        }
    }

    _loadCollectBlock(bot) {
        const pluginName = "mineflayer-collectblock";
        const loaded = safeRequire(pluginName);
        if (!loaded.ok) {
            this.capabilities[pluginName] = false;
            this.errors[pluginName] = loaded.error;
            return;
        }

        try {
            bot.loadPlugin(loaded.module.plugin);
            this.capabilities[pluginName] = true;
            this.capabilities.collectBlock = true;
        } catch (err) {
            this.capabilities[pluginName] = false;
            this.capabilities.collectBlock = false;
            this.errors[pluginName] = err.message;
        }
    }

    _loadHawkEye(bot) {
        const pluginName = "minecrafthawkeye";
        const loaded = safeRequire(pluginName);
        if (!loaded.ok) {
            this.capabilities[pluginName] = false;
            this.errors[pluginName] = loaded.error;
            return;
        }

        try {
            bot.loadPlugin(loaded.module);
            this.capabilities[pluginName] = true;
            this.capabilities.hawkEye = this._hawkEyeReady(bot);
            if (!this.capabilities.hawkEye && typeof bot.once === "function") {
                // mineflayer 在连接后的 inject_allowed 阶段才执行插件，
                // 因此 spawn 后再复核一次能力，避免上报 false。
                bot.once("spawn", () => {
                    this.capabilities.hawkEye = this._hawkEyeReady(bot);
                });
            }
        } catch (err) {
            this.capabilities[pluginName] = false;
            this.capabilities.hawkEye = false;
            this.errors[pluginName] = err.message;
        }
    }

    _hawkEyeReady(bot) {
        return (
            typeof bot.hawkEye === "object" &&
            typeof bot.hawkEye.autoAttack === "function"
        );
    }
}

module.exports = { PluginManager };
