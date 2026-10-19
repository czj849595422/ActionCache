"use strict";

function normalizeText(value, field) {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return value.trim();
}

function fail(action, errorCode, result) {
    action.result = result;
    const err = new Error(errorCode);
    err.code = errorCode;
    throw err;
}

async function say(ctx, params, action) {
    let message;
    try {
        message = normalizeText(params?.message, "message");
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    try {
        await ctx.bot.chat(message);
        return {
            ok: true,
            message,
            sent_at: Date.now(),
            message_type: "say",
        };
    } catch (err) {
        fail(action, "chat_failed", {
            ok: false,
            error_code: "chat_failed",
            message,
            reason: err.message,
        });
    }
}

async function whisper(ctx, params, action) {
    let username;
    let message;
    try {
        username = normalizeText(params?.username, "username");
        message = normalizeText(params?.message, "message");
    } catch (err) {
        fail(action, "invalid_params", {
            ok: false,
            error_code: "invalid_params",
            reason: err.message,
            params: params || {},
        });
    }
    try {
        await ctx.bot.whisper(username, message);
        return {
            ok: true,
            username,
            message,
            sent_at: Date.now(),
            message_type: "whisper",
        };
    } catch (err) {
        fail(action, "whisper_failed", {
            ok: false,
            error_code: "whisper_failed",
            username,
            message,
            reason: err.message,
        });
    }
}

module.exports = {
    name: "chat",
    commands: {
        say: { execute: say },
        whisper: { execute: whisper },
    },
};
