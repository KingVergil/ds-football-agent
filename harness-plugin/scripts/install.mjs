#!/usr/bin/env node
/**
 * ds-agents-lota-data 跨平台一次性安装器（macOS / Linux / Windows）。
 *
 * 把原来三句 Unix 专属提示（复制插件到 node_modules / 写 cordis.patch.yml /
 * 创建 ~/.zshrc 密钥文件）收敛成一条命令，且不需要管理员权限（全部写入用户目录）：
 *
 *   node scripts/install.mjs --profile-dir <dsh profile 目录> \
 *     --set-keys DEEPSEEK_API_KEY=... LOTA_API_KEY=...
 *
 * 做的事情（幂等，可重复跑）：
 *   1. 把本插件装进 profile 的 node_modules/ds-agents-lota-data
 *      （POSIX 用软链；Windows 用 junction，实在不行退化成复制）；
 *   2. 把挂载条目写进 <profile>/cordis.patch.yml（已有则跳过）；
 *   3. 密钥写入 <engineRoot>/.env（Windows 友好；POSIX 上再顺手追加 ~/.zshrc
 *      export 块，让命令行 CLI 也能用）。
 *
 * 用法：
 *   --profile-dir <path>    dsh profile 目录（默认 ~/.dsh/profiles/web）
 *   --engine-root <path>    python-engine 目录（默认 <仓库>/python-engine）
 *   --cache-dir <path>      缓存目录（默认 <engineRoot>/data）
 *   --python-bin <path>     显式指定 python 解释器（可选；缺省按平台自动选）
 *   --env-dest <path>       密钥文件（默认 <engineRoot>/.env）
 *   --set-keys K=V [K2=V2]  要写入的密钥（可多次传）
 *   --keys-file <path>      从文件读 KEY=V 行（与 --set-keys 合并）
 *   --copy                  强制复制而不是软链/junction
 *   --no-zshrc              POSIX 上不追加 ~/.zshrc
 *   --dry-run               只打印将要做的操作，不写盘
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, symlinkSync, cpSync, rmSync } from "node:fs";
import { homedir, platform } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_NAME = "ds-agents-lota-data";
const PLUGIN_ID = "lota-data";
const ZSHRC_MARKER_HEAD = `# >>> ${PLUGIN_NAME} keys >>>`;
const ZSHRC_MARKER_TAIL = `# <<< ${PLUGIN_NAME} keys <<<`;

const scriptDir = dirname(fileURLToPath(import.meta.url));
const pluginDir = resolve(scriptDir, "..");
const repoRoot = resolve(pluginDir, "..");

function parseArgs(argv) {
  const args = { setKeys: [] };
  const next = (i) => argv[i + 1];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const take = (fallback) => {
      const v = next(i);
      if (v === undefined) return fallback;
      i += 1;
      return v;
    };
    switch (a) {
      case "--profile-dir": args.profileDir = take(""); break;
      case "--engine-root": args.engineRoot = take(""); break;
      case "--cache-dir": args.cacheDir = take(""); break;
      case "--python-bin": args.pythonBin = take(""); break;
      case "--env-dest": args.envFile = take(""); break;
      case "--keys-file": args.keysFile = take(""); break;
      case "--set-keys": {
        // 支持 --set-keys A=1 B=2（吃满后续非 -- 参数）或 --set-keys A=1,B=2
        while (i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
          for (const part of argv[i + 1].split(",")) {
            const t = part.trim();
            if (t) args.setKeys.push(t);
          }
          i += 1;
        }
        break;
      }
      case "--copy": args.forceCopy = true; break;
      case "--no-zshrc": args.noZshrc = true; break;
      case "--dry-run": args.dryRun = true; break;
      case "--help":
      case "-h":
        console.log(`
用法：node scripts/install.mjs [选项]

  --profile-dir <path>   dsh profile 目录（默认 ~/.dsh/profiles/web）
  --engine-root <path>   python-engine 目录（默认 <仓库>/python-engine）
  --cache-dir <path>     缓存目录（默认 <engineRoot>/data）
  --python-bin <path>    显式指定 python 解释器（可选）
  --env-dest <path>      密钥文件（默认 <engineRoot>/.env）
  --set-keys K=V ...     写入的密钥，例如 --set-keys DEEPSEEK_API_KEY=sk-xxx LOTA_API_KEY=yyy
  --keys-file <path>     从文件读 KEY=V 行（与 --set-keys 合并）
  --copy                 强制复制而不是软链/junction
  --no-zshrc             POSIX 上不追加 ~/.zshrc
  --dry-run              只打印操作，不写盘
`);
        process.exit(0);
      default:
        console.warn(`[install] 忽略未知参数: ${a}`);
    }
  }
  return args;
}

function yamlPath(p) {
  // Windows 盘符路径转 C:/Users/... 正斜杠形式，YAML 里不需要转义
  return p.split(sep).join("/");
}

function collectKeys(args) {
  const keys = new Map();
  const addLine = (line) => {
    const t = line.trim();
    if (!t || t.startsWith("#")) return;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(t);
    if (!m || !m[2].trim()) return;
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    keys.set(m[1], v);
  };
  for (const pair of args.setKeys) {
    const i = pair.indexOf("=");
    if (i <= 0) {
      console.warn(`[install] 跳过无法解析的密钥项: ${pair}`);
      continue;
    }
    keys.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim());
  }
  if (args.keysFile) {
    for (const line of readFileSync(args.keysFile, "utf8").split(/\r?\n/)) addLine(line);
  }
  return keys;
}

function renderPatchBlock(args) {
  const cacheDir = yamlPath(args.cacheDir);
  const engineRoot = yamlPath(args.engineRoot);
  const lines = [
    "",
    `- insert:`,
    `    - id: ${PLUGIN_ID}`,
    `      name: '${PLUGIN_NAME}'`,
    `      config:`,
    `        cacheDir: ${cacheDir}`,
    `        engineRoot: ${engineRoot}`,
  ];
  if (args.pythonBin) lines.push(`        pythonBin: ${yamlPath(args.pythonBin)}`);
  return lines.join("\n") + "\n";
}

function upsertPatchYml(patchPath, args, dryRun) {
  const block = renderPatchBlock(args);
  const existed = existsSync(patchPath);
  let text = existed ? readFileSync(patchPath, "utf8") : "";
  if (text.includes(`name: '${PLUGIN_NAME}'`)) {
    console.log(`[install] cordis.patch.yml 已有 ${PLUGIN_NAME} 挂载条目，跳过写入: ${patchPath}`);
    return;
  }
  if (dryRun) {
    console.log(`[install] ${existed ? "追加" : "创建"}挂载条目 → ${patchPath}\n${block.trimEnd()}`);
    return;
  }
  if (!text.endsWith("\n")) text += "\n";
  writeFileSync(patchPath, text + block, "utf8");
  console.log(`[install] ${existed ? "追加" : "创建"}挂载条目 → ${patchPath}`);
}

function installPluginIntoProfile(profileDir, dryRun, forceCopy) {
  const target = join(profileDir, "node_modules", PLUGIN_NAME);
  if (existsSync(target)) {
    console.log(`[install] 插件已存在，跳过安装: ${target}`);
    return;
  }
  if (dryRun) {
    console.log(`[install] 安装插件 → ${target}`);
    return;
  }
  mkdirSync(dirname(target), { recursive: true });
  const isWin = platform() === "win32";
  let linked = false;
  if (!forceCopy) {
    try {
      // POSIX 软链 / Windows junction（目录 junction 不需要管理员权限）
      symlinkSync(pluginDir, target, isWin ? "junction" : "dir");
      linked = true;
    } catch (e) {
      console.warn(`[install] 软链失败（${e.message}），退化为复制`);
    }
  }
  if (!linked) {
    cpSync(pluginDir, target, {
      recursive: true,
      filter: (src) => {
        const name = src.split(sep).pop();
        return name !== "node_modules" && name !== ".git";
      },
    });
  }
  console.log(`[install] 插件已安装 → ${target}${linked ? "（链接）" : "（复制）"}`);
}

function upsertEnvFile(envPath, keys, dryRun) {
  const existed = existsSync(envPath);
  const lines = existed ? readFileSync(envPath, "utf8").split(/\r?\n/) : [];
  const out = [];
  const seen = new Set();
  for (const line of lines) {
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/.exec(line.trim());
    if (m && keys.has(m[1])) {
      out.push(`${m[1]}=${quoteEnv(keys.get(m[1]))}`);
      seen.add(m[1]);
    } else {
      out.push(line);
    }
  }
  if (!existed && out.length && out[out.length - 1] === "") out.pop();
  for (const [k, v] of keys) {
    if (!seen.has(k)) out.push(`${k}=${quoteEnv(v)}`);
  }
  if (dryRun) {
    console.log(`[install] 更新密钥文件 → ${envPath}（${keys.size} 个 key）`);
    return;
  }
  mkdirSync(dirname(envPath), { recursive: true });
  const body = out.join("\n").replace(/\n+$/, "\n");
  writeFileSync(envPath, body, "utf8");
  console.log(`[install] 密钥已写入 → ${envPath}（${keys.size} 个 key）`);
}

function quoteEnv(v) {
  return /[\s#]/.test(v) ? `"${v}"` : v;
}

function upsertZshrc(keys, dryRun, noZshrc) {
  if (platform() === "win32" || noZshrc) return;
  const rc = join(homedir(), ".zshrc");
  const existed = existsSync(rc);
  let text = existed ? readFileSync(rc, "utf8") : "";
  const blockLines = [ZSHRC_MARKER_HEAD, ...[...keys].map(([k, v]) => `export ${k}=${quoteEnv(v)}`), ZSHRC_MARKER_TAIL];
  const block = blockLines.join("\n") + "\n";
  // 幂等：替换旧的 marker 块（如果有），否则追加
  const headIdx = text.indexOf(ZSHRC_MARKER_HEAD);
  const tailIdx = text.indexOf(ZSHRC_MARKER_TAIL);
  if (headIdx >= 0 && tailIdx > headIdx) {
    text = text.slice(0, headIdx) + block + text.slice(tailIdx + ZSHRC_MARKER_TAIL.length);
  } else {
    if (!text.endsWith("\n")) text += "\n";
    text += "\n" + block;
  }
  if (dryRun) {
    console.log(`[install] 追加密钥到 → ${rc}`);
    return;
  }
  writeFileSync(rc, text, "utf8");
  console.log(`[install] 密钥已追加到 → ${rc}`);
}

function platformTips(args, engineRoot, envFile, keyCount) {
  const isWin = platform() === "win32";
  console.log("");
  console.log("✅ 安装完成。接下来：");
  console.log("  1. 装引擎依赖（Python 3.10+）：");
  console.log(`     python -m pip install -r ${engineRoot}/requirements.txt   # requests + langgraph`);
  console.log("  2. 确认 python 可用（Windows: `python --version` 或 `py -3 --version`；");
  console.log("     macOS/Linux: `python3 --version`）。如果解释器不在 PATH，把");
  console.log(`     pythonBin: <解释器绝对路径> 加进 cordis.patch.yml 的 config（脚本也支持 --python-bin）。`);
  console.log(`  3. 密钥文件：${envFile}（bridge.js 会自动读取；优先级 环境变量 > .env > ~/.zshrc）`);
  if (!keyCount) {
    console.log(`     ⚠️ 还没写入密钥，手动编辑 ${envFile} 加一行如：`);
    console.log(`        DEEPSEEK_API_KEY=sk-...`);
    console.log(`        LOTA_API_KEY=...`);
    if (isWin) {
      console.log("     或 PowerShell 里 setx（对已打开的终端不生效，需重开）：");
      console.log('       setx DEEPSEEK_API_KEY "sk-..."');
    }
  }
  console.log("  4. 重启 dsh（或重开 profile），挂载即生效。");
  console.log("  5. 本安装全部写在用户目录，不需要管理员/提权。");
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const isWin = platform() === "win32";
  const home = homedir();
  const defaultProfile = join(home, ".dsh", "profiles", "web");
  const profileDir = args.profileDir ? resolve(args.profileDir) : defaultProfile;
  const engineRoot = args.engineRoot ? resolve(args.engineRoot) : join(repoRoot, "python-engine");
  const cacheDir = args.cacheDir ? resolve(args.cacheDir) : join(engineRoot, "data");
  const envFile = args.envFile ? resolve(args.envFile) : join(engineRoot, ".env");
  const patchPath = join(profileDir, "cordis.patch.yml");

  console.log(`[install] platform=${process.platform} profile=${profileDir}`);
  console.log(`[install] plugin=${pluginDir}`);
  console.log(`[install] engineRoot=${engineRoot} cacheDir=${cacheDir}`);

  if (!existsSync(engineRoot)) {
    console.error(`[install] 引擎目录不存在: ${engineRoot}（用 --engine-root 指定）`);
    process.exit(1);
  }
  if (!existsSync(cacheDir)) {
    console.warn(`[install] 缓存目录不存在（不阻塞，首次跑数据准备时会自动创建）: ${cacheDir}`);
  }
  if (!existsSync(profileDir)) {
    if (args.dryRun) console.log(`[install] 将创建 profile 目录 → ${profileDir}`);
    else mkdirSync(profileDir, { recursive: true });
  }

  installPluginIntoProfile(profileDir, args.dryRun, args.forceCopy);
  upsertPatchYml(patchPath, { ...args, cacheDir, engineRoot }, args.dryRun);

  const keys = collectKeys(args);
  if (keys.size) {
    upsertEnvFile(envFile, keys, args.dryRun);
    if (!isWin) upsertZshrc(keys, args.dryRun, args.noZshrc);
  } else {
    console.log("[install] 未提供密钥（--set-keys / --keys-file），跳过密钥写入");
  }
  platformTips(args, engineRoot, envFile, keys.size);
}

main();
