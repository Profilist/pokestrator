import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { buildDemoStateFromLog } from "./demo/logParser.js";

const configDir = path.dirname(fileURLToPath(import.meta.url));
const workspaceRoot = path.resolve(configDir, "..");

function resolveLogPath() {
  const configuredPath =
    process.env.POKESTRATOR_LOG_FILE ||
    process.env.VITE_POKESTRATOR_LOG_FILE ||
    "logs/pokestrator.log";
  if (path.isAbsolute(configuredPath)) {
    return configuredPath;
  }
  return path.resolve(workspaceRoot, configuredPath);
}

async function readDemoState(logPath) {
  try {
    const contents = await fs.readFile(logPath, "utf-8");
    return buildDemoStateFromLog(contents, logPath);
  } catch (error) {
    return {
      generatedAt: new Date().toISOString(),
      logPath,
      warnings: [
        `Could not read log file at ${logPath}: ${String(
          error?.message ?? error
        )}`,
      ],
      orchestrator: {
        status: "idle",
        requestId: null,
        taskDescription: "",
        branch: "unknown",
        startedAt: null,
        lastUpdatedAt: null,
        logs: [],
      },
      subagents: [],
      recentRequests: [],
    };
  }
}

function registerStateEndpoint(middlewares, logPath) {
  middlewares.use(async (req, res, next) => {
    if (!req.url || !req.url.startsWith("/api/demo-state")) {
      return next();
    }

    const state = await readDemoState(logPath);
    res.statusCode = 200;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    res.end(JSON.stringify(state));
  });
}

function demoStatePlugin() {
  const logPath = resolveLogPath();

  return {
    name: "pokestrator-demo-state",
    configureServer(server) {
      registerStateEndpoint(server.middlewares, logPath);
    },
    configurePreviewServer(server) {
      registerStateEndpoint(server.middlewares, logPath);
    },
  };
}

export default defineConfig({
  plugins: [react(), demoStatePlugin()],
});
