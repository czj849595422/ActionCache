"use strict";

const path = require("path");

const { PluginManager } = require("./plugin_manager");
const { loadSkills, parseAction } = require("./skill_loader");
const { DEFAULT_PERCEPTION_RADIUS } = require("../perception_config");

function resultFailure(result) {
    return Boolean(result && typeof result === "object" && result.ok === false);
}

function resultError(result) {
    if (!result || typeof result !== "object") return "skill returned ok=false";
    return String(result.error_code || result.reason || "skill returned ok=false");
}

class SkillRuntime {
    constructor(options) {
        this.skillsDir = options.skillsDir;
        this.getBot = options.getBot;
        this.getPerception = options.getPerception;
        this.getPerceptionRadius = options.getPerceptionRadius ?? (() => DEFAULT_PERCEPTION_RADIUS);
        this.getAutoPickup = options.getAutoPickup ?? (() => false);
        this.setAutoPickup = options.setAutoPickup ?? (() => {});
        this.getTaskCategory = options.getTaskCategory ?? (() => null);
        this.getTradeQueryableItems = options.getTradeQueryableItems ?? (() => null);
        this.allowedActions = options.allowedActions === null || options.allowedActions === undefined
            ? null
            : new Set(options.allowedActions);
        this.internalActions = new Set(options.internalActions || []);
        this.pluginManager = new PluginManager();
        this.skills = loadSkills(this.skillsDir);
        this.actions = new Map();
    }

    attachBot(bot) {
        return this.pluginManager.attach(bot);
    }

    async detachBot(reason = "bot_detached") {
        const actionIds = Array.from(this.actions.keys());
        for (const actionId of actionIds) {
            await this.stop({ actionId, reason });
        }
        this.actions.clear();
    }

    list() {
        const skills = {};
        for (const [name, skill] of Object.entries(this.skills)) {
            const commands = Object.keys(skill.manifest.commands || {}).filter(command => (
                this.allowedActions === null || this.allowedActions.has(`${name}.${command}`)
            ));
            if (commands.length === 0) continue;
            skills[name] = {
                version: skill.manifest.version,
                runtime: skill.manifest.runtime,
                commands,
                plugins: skill.manifest.plugins || {},
            };
        }
        return {
            skills,
            capabilities: this.pluginManager.getCapabilities(),
            skillsDir: path.normalize(this.skillsDir),
        };
    }

    async execute(request) {
        const action = this._createAction(request, "oneshot");
        this.actions.set(action.actionId, action);
        try {
            const command = this._getCommand(action);
            this._checkPlugins(action);
            action.status = "running";
            const ctx = this._context(action);
            const handler = command.execute || command.run;
            if (typeof handler !== "function") {
                throw new Error(`Skill command ${action.action} has no execute handler.`);
            }
            action.result = await handler(ctx, action.params, action);
            if (resultFailure(action.result)) {
                action.status = "failed";
                action.error = resultError(action.result);
            } else {
                action.status = "completed";
            }
            return this._publicAction(action);
        } catch (err) {
            action.status = "failed";
            action.error = err.message;
            return this._publicAction(action);
        }
    }

    async start(request) {
        const action = this._createAction(request, "continuous");
        this.actions.set(action.actionId, action);
        try {
            const command = this._getCommand(action);
            this._checkPlugins(action);
            action.status = "running";
            const ctx = this._context(action);
            if (typeof command.start !== "function") {
                throw new Error(`Skill command ${action.action} has no start handler.`);
            }
            action.result = await command.start(ctx, action.params, action);
            if (resultFailure(action.result)) {
                action.status = "failed";
                action.error = resultError(action.result);
            }
            return this._publicAction(action);
        } catch (err) {
            action.status = "failed";
            action.error = err.message;
            return this._publicAction(action);
        }
    }

    async update(request) {
        const action = this._findAction(request.actionId);
        if (action.status !== "running") {
            return this._publicAction(action);
        }

        const patch = request.params || {};
        action.params = { ...action.params, ...patch };
        action.updatedAt = Date.now();
        const command = this._getCommand(action);
        if (typeof command.update === "function") {
            const ctx = this._context(action);
            action.result = await command.update(ctx, patch, action);
            if (resultFailure(action.result)) {
                action.status = "failed";
                action.error = resultError(action.result);
            }
        }
        return this._publicAction(action);
    }

    async stop(request) {
        const action = this._findAction(request.actionId);
        const command = this._getCommand(action);
        if (action.status === "running" && typeof command.stop === "function") {
            const ctx = this._context(action);
            action.result = await command.stop(ctx, request.reason || "interrupted", action);
        }
        action.status = request.reason === "switch" ? "stopped" : "interrupted";
        action.stoppedAt = Date.now();
        this.actions.delete(action.actionId);
        return this._publicAction(action);
    }

    status(actionId = null) {
        if (actionId) {
            const action = this._findAction(actionId);
            const command = this._getCommand(action);
            if (typeof command.status === "function") {
                const statusResult = command.status(this._context(action), action);
                if (statusResult !== undefined) action.result = statusResult;
            }
            return this._publicAction(action);
        }

        return {
            status: "ok",
            actions: Array.from(this.actions.values()).map((action) => this._refreshPublicAction(action)),
        };
    }

    _refreshPublicAction(action) {
        const command = this._getCommand(action);
        if (typeof command.status === "function") {
            const statusResult = command.status(this._context(action), action);
            if (statusResult !== undefined) action.result = statusResult;
        }
        return this._publicAction(action);
    }

    _createAction(request, defaultMode) {
        const { skillName, commandName } = parseAction(request.action);
        const actionId = request.actionId || `js-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        return {
            actionId,
            action: request.action,
            skillName,
            commandName,
            mode: request.mode || defaultMode,
            params: request.params || {},
            perception: request.perception || null,
            timeout: request.timeout || null,
            interruptible: request.interruptible !== false,
            internal: request.internal === true,
            status: "pending",
            result: null,
            error: undefined,
            startedAt: Date.now(),
            updatedAt: Date.now(),
            runtimeState: {},
        };
    }

    _getSkill(action) {
        this._checkActionAccess(action);
        const skill = this.skills[action.skillName];
        if (!skill) {
            throw new Error(`Unknown skill: ${action.skillName}`);
        }
        return skill;
    }

    _checkActionAccess(action) {
        if (action.internal) {
            if (!this.internalActions.has(action.action)) {
                throw new Error(`Internal skill action is not allowed: ${action.action}`);
            }
            return;
        }
        if (this.allowedActions !== null && !this.allowedActions.has(action.action)) {
            throw new Error(`Skill action is not allowed in this task: ${action.action}`);
        }
    }

    _getCommand(action) {
        const skill = this._getSkill(action);
        const command = skill.implementation.commands
            ? skill.implementation.commands[action.commandName]
            : skill.implementation[action.commandName];
        if (!command) {
            throw new Error(`Unknown command: ${action.action}`);
        }
        return command;
    }

    _checkPlugins(action) {
        const skill = this._getSkill(action);
        const required = ((skill.manifest.plugins || {}).required || []);
        for (const pluginName of required) {
            this.pluginManager.requireCapability(pluginName);
        }
    }

    _context(action) {
        const bot = this.getBot();
        if (!bot) {
            throw new Error("Bot not started");
        }
        return {
            bot,
            action,
            runtime: this,
            capabilities: this.pluginManager.getCapabilities(),
            getPerception: this.getPerception,
            perceptionRadius: this.getPerceptionRadius(),
            getAutoPickup: () => this.getAutoPickup(),
            setAutoPickup: (value) => this.setAutoPickup(value),
            getTaskCategory: () => this.getTaskCategory(),
            getTradeQueryableItems: () => this.getTradeQueryableItems(),
        };
    }

    _findAction(actionId) {
        const action = this.actions.get(actionId);
        if (!action) {
            throw new Error(`Unknown actionId: ${actionId}`);
        }
        return action;
    }

    _publicAction(action) {
        const result = {
            status: action.status,
            actionId: action.actionId,
            action: action.action,
            mode: action.mode,
            params: action.params,
            result: action.result,
            startedAt: action.startedAt,
            updatedAt: action.updatedAt,
        };
        if (action.error) {
            result.error = action.error;
        }
        return result;
    }
}

module.exports = { SkillRuntime };
