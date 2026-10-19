"use strict";

const fs = require("fs");
const path = require("path");

function readJson(filePath) {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function findManifestPaths(skillsDir) {
    const manifests = [];
    const root = path.resolve(skillsDir);

    function visit(directory) {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
            if (!entry.isDirectory()) continue;
            if (["node_modules", "__pycache__"].includes(entry.name)) continue;
            const skillDir = path.join(directory, entry.name);
            const manifestPath = path.join(skillDir, "skill.json");
            if (fs.existsSync(manifestPath)) {
                manifests.push(manifestPath);
            }
            visit(skillDir);
        }
    }

    visit(root);
    return manifests.sort();
}

function loadSkills(skillsDir) {
    const skills = {};
    if (!fs.existsSync(skillsDir)) return skills;

    for (const manifestPath of findManifestPaths(skillsDir)) {
        const skillDir = path.dirname(manifestPath);
        const manifest = readJson(manifestPath);
        if (manifest.runtime !== "javascript") continue;
        if (skills[manifest.name]) {
            throw new Error(
                `Duplicate skill name '${manifest.name}': ${skills[manifest.name].path} and ${skillDir}`,
            );
        }

        const entryFile = path.join(skillDir, manifest.entry || "skill.js");
        const implementation = require(entryFile);
        skills[manifest.name] = {
            manifest,
            implementation,
            path: skillDir,
        };
    }

    return skills;
}

function parseAction(action) {
    if (typeof action !== "string" || action.length === 0) {
        throw new Error("action must be a non-empty string");
    }
    if (action.includes(".")) {
        const parts = action.split(".");
        return { skillName: parts[0], commandName: parts.slice(1).join(".") };
    }
    return { skillName: action, commandName: action };
}

module.exports = { findManifestPaths, loadSkills, parseAction };
