import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const windows = process.platform === "win32";
const script = fileURLToPath(new URL(windows ? "./resource-pipeline.ps1" : "./resource-pipeline.sh", import.meta.url));
const args = windows ? ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] : [script];
const result = spawnSync(windows ? "powershell" : "sh", [...args, ...process.argv.slice(2)], { stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exitCode = result.status ?? 1;
