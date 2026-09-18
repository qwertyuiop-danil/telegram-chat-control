import { spawn, type ChildProcess } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const HOME = process.env.HOME ?? homedir();
const OPENCLAW_HOME = process.env.OPENCLAW_HOME ?? join(HOME, ".openclaw");
const CWD = process.env.TELEGRAM_CHAT_CONTROL_SKILL_DIR
  ?? join(OPENCLAW_HOME, "skills", "telegram-chat-control");
const ROOT = process.env.TELEGRAM_CHAT_CONTROL_HOME
  ?? join(OPENCLAW_HOME, "telegram-chat-control");
const UV = process.env.TELEGRAM_CHAT_CONTROL_UV ?? "uv";
const INTERVAL_MS = 60_000;

export default definePluginEntry({
  id: "telegram-sync-minute",
  name: "Telegram Sync Minute",
  description: "Updates the local Telegram index once per minute without an agent.",
  register(api) {
    let child: ChildProcess | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;

    api.registerService({
      id: "telegram-sync-minute",
      start(ctx) {
        const sync = () => {
          if (child) return;

          child = spawn(UV, ["run", "scripts/telegram.py", "sync", "run"], {
            cwd: CWD,
            env: {
              ...process.env,
              HOME,
              TELEGRAM_CHAT_CONTROL_HOME: ROOT,
            },
            stdio: "ignore",
          });
          child.once("error", (error) => {
            ctx.logger.error(`Telegram sync failed to start: ${error.message}`);
            child = undefined;
          });
          child.once("exit", (code, signal) => {
            if (code && code !== 0) {
              ctx.logger.warn(`Telegram sync exited with code ${code}${signal ? ` (${signal})` : ""}`);
            }
            child = undefined;
          });
        };

        sync();
        timer = setInterval(sync, INTERVAL_MS);
        ctx.logger.info("Telegram index sync scheduled every 60 seconds");
      },
      stop() {
        if (timer) clearInterval(timer);
        child?.kill("SIGTERM");
        timer = undefined;
        child = undefined;
      },
    });
  },
});
