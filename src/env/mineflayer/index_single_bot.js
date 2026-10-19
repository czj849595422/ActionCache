// 单 agent 简易桥接 (演示用)
// 复用现有资源: ./lib 下的观测库/技能库与 node_modules 依赖。
// 启动: node src/env/mineflayer/index_single_bot.js [HTTP_PORT]
// 前置: 在本目录执行 npm install --ignore-scripts

const express = require("express");
const bodyParser = require("body-parser");
const mineflayer = require("mineflayer");

const skills = require("./lib/skillLoader");
const obs = require("./lib/observation/base");
const OnChat = require("./lib/observation/onChat");
const OnError = require("./lib/observation/onError");
const { Voxels, BlockRecords } = require("./lib/observation/voxels");
const Status = require("./lib/observation/status");
const Inventory = require("./lib/observation/inventory");
const OnSave = require("./lib/observation/onSave");
const Chests = require("./lib/observation/chests");
const Furnaces = require("./lib/observation/furnaces");
const { initCounter } = require("./lib/utils");

const app = express();
app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: false }));

let bot = null;
let waitTicks = 5;

app.get("/health", (req, res) => {
    res.json({ ok: true, botReady: bot !== null });
});

app.post("/start", (req, res) => {
    if (bot) {
        bot.end();
        bot = null;
    }
    const port = req.body.port;
    const username = req.body.username || "test_agent";
    waitTicks = req.body.waitTicks || 5;
    console.log(`Creating bot "${username}" -> localhost:${port}`);

    let responseSent = false;

    function onConnectionFailed(err) {
        console.error("Connection error:", err);
        bot = null;
        if (!responseSent) {
            responseSent = true;
            res.status(400).json({ error: String(err) });
        }
    }

    bot = mineflayer.createBot({
        host: "localhost",
        port: port,
        username: username,
        disableChatSigning: true,
        checkTimeoutInterval: 60 * 60 * 1000,
    });

    bot.once("error", onConnectionFailed);
    bot.once("kicked", (reason) => {
        console.error("Bot kicked:", reason);
        bot = null;
    });

    bot.once("spawn", async () => {
        bot.removeListener("error", onConnectionFailed);
        try {
            const { pathfinder } = require("mineflayer-pathfinder");
            const tool = require("mineflayer-tool").plugin;
            const collectBlock = require("./mineflayer-collectblock").plugin;
            const pvp = require("mineflayer-pvp").plugin;
            const minecraftHawkEye = require("minecrafthawkeye");

            bot.loadPlugin(pathfinder);
            bot.loadPlugin(tool);
            bot.loadPlugin(collectBlock);
            bot.loadPlugin(pvp);
            bot.loadPlugin(minecraftHawkEye);

            obs.inject(bot, [
                OnChat,
                OnError,
                Voxels,
                Status,
                Inventory,
                OnSave,
                Chests,
                BlockRecords,
                Furnaces,
            ]);
            skills.inject(bot);
            initCounter(bot);

            await bot.waitForTicks(waitTicks * 5);
            if (!responseSent) {
                responseSent = true;
                res.json({ bot: bot.observe() });
            }
        } catch (err) {
            console.error("Spawn setup failed:", err);
            if (!responseSent) {
                responseSent = true;
                res.status(500).json({ error: String(err) });
            }
        }
    });
});

app.post("/step", async (req, res) => {
    if (!bot) {
        res.status(400).json({ error: "Bot not spawned" });
        return;
    }
    const code = req.body.code || "";
    const programs = req.body.programs || "";

    const mcData = require("minecraft-data")(bot.version);
    const {
        Movements,
        goals: {
            Goal,
            GoalBlock,
            GoalNear,
            GoalXZ,
            GoalNearXZ,
            GoalY,
            GoalGetToBlock,
            GoalLookAtBlock,
            GoalBreakBlock,
            GoalCompositeAny,
            GoalCompositeAll,
            GoalInvert,
            GoalFollow,
            GoalPlaceBlock,
        },
        pathfinder,
        Move,
        ComputedPath,
        PartiallyComputedPath,
        XZCoordinates,
        XYZCoordinates,
        SafeBlock,
        GoalPlaceBlockOptions,
    } = require("mineflayer-pathfinder");
    const { Vec3 } = require("vec3");

    await bot.waitForTicks(waitTicks);
    const r = await evaluateCode(code, programs);
    if (r !== "success") {
        bot.emit("error", handleError(r));
    }
    await bot.waitForTicks(waitTicks);
    res.json({ bot: bot.observe() });

    async function evaluateCode(code, programs) {
        const lines = code.split(";").filter((line) => line.trim() !== "");
        const actions = lines.map((line) => {
            const completeCode = `
                (async () => {
                    ${programs}
                    return (async () => { ${line} })();
                })()
            `;
            return eval(completeCode).catch((error) => {
                console.error(`Action failed:`, error);
            });
        });
        await Promise.all(actions);
        return "success";
    }

    function handleError(err) {
        if (!err) return "unknown error";
        if (err.stack) console.error(err.stack);
        return err.message || String(err);
    }
});

app.post("/stop", (req, res) => {
    if (bot) {
        bot.end();
        bot = null;
    }
    res.json({ message: "Bot stopped" });
});

const DEFAULT_PORT = 3000;
const PORT = process.argv[2] || DEFAULT_PORT;
app.listen(PORT, () => {
    console.log(`Server started on port ${PORT}`);
});
