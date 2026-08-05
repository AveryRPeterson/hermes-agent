/**
 * stage-agent-payloads.mjs: assemble the offline agent payload tree that
 * ships inside the bundled desktop artifact. Design:
 * .hermes/plans/2026-08-05_desktop-bundled-payloads-channels-eject.md §2.
 *
 * Output: apps/desktop/build/agent-payload/
 *   manifest.json          schemaVersion, tag, commit, platform, arch, per-item status
 *   repo/                  a shallow git clone at the release tag. It keeps
 *                          .git, which makes `hermes update --eject` almost
 *                          free and keeps the checkout git-shaped.
 *   uv/                    static uv binary for this platform/arch
 *   python/                uv-managed CPython (python-build-standalone)
 *   wheels/                the resolved wheelhouse from uv.lock for this platform/arch
 *   node/                  official node dist for this platform/arch
 *   js-prebuilt.tar.zst    PREBUILT JS surfaces + node_modules (ui-tui dist +
 *                          hermes-ink, web_dist). Thus the first launch never
 *                          runs npm install or npm run build.
 *
 * Gating: the script does nothing unless HERMES_DESKTOP_BUNDLED=1. That
 * variable is an internal build-time env for CI wiring, not user config.
 * Thus dev builds and current CI keep producing thin builds. You can skip
 * individual items with --skip=<item,item> for incremental CI caching.
 * The manifest.json records every skip. Thus the bootstrap knows to fall
 * back to its network path for that stage (per-stage fallback rule, plan §3).
 *
 * The heavy work shells out to git, uv, npm, and tar. The decision logic
 * (target resolution, uv arg construction, manifest shape) is exported as
 * pure functions. Thus vitest covers it without network or toolchains.
 */

import { execSync, spawnSync } from "node:child_process"
import fs from "node:fs"
import path from "node:path"

import { isMain } from "./utils.mjs"

export const PAYLOAD_SCHEMA_VERSION = 1

const DESKTOP_ROOT = path.resolve(import.meta.dirname, "..")
const REPO_ROOT = path.resolve(DESKTOP_ROOT, "..", "..")
const OUT_DIR = path.join(DESKTOP_ROOT, "build", "agent-payload")

export const PAYLOAD_ITEMS = ["repo", "uv", "python", "wheels", "node", "js-prebuilt"]

/**
 * Map (process.platform, process.arch) to the uv, python-build-standalone,
 * and node target descriptors. There is one artifact per (os, arch) pair.
 * Mac universal2 is deliberately NOT a target. We ship two artifacts
 * (plan §6).
 *
 * There are no cross-platform wheel tags here, on purpose. A CI runner per
 * (os, arch) pair assembles the payloads. electron-builder needs per-OS
 * runners for signing anyway. Thus the script fetches wheels NATIVELY with
 * `uvx pip wheel --only-binary=:all:`. The platform of the runner is the
 * target platform.
 */
export function resolveTargets(platform = process.platform, arch = process.arch) {
  const table = {
    "linux-x64": {
      uvTarget: "x86_64-unknown-linux-gnu",
      pythonPlatform: "x86_64-unknown-linux-gnu",
      nodeDist: "linux-x64",
    },
    "linux-arm64": {
      uvTarget: "aarch64-unknown-linux-gnu",
      pythonPlatform: "aarch64-unknown-linux-gnu",
      nodeDist: "linux-arm64",
    },
    "darwin-x64": {
      uvTarget: "x86_64-apple-darwin",
      pythonPlatform: "x86_64-apple-darwin",
      nodeDist: "darwin-x64",
    },
    "darwin-arm64": {
      uvTarget: "aarch64-apple-darwin",
      pythonPlatform: "aarch64-apple-darwin",
      nodeDist: "darwin-arm64",
    },
    "win32-x64": {
      uvTarget: "x86_64-pc-windows-msvc",
      pythonPlatform: "x86_64-pc-windows-msvc",
      nodeDist: "win-x64",
    },
    "win32-arm64": {
      uvTarget: "aarch64-pc-windows-msvc",
      pythonPlatform: "aarch64-pc-windows-msvc",
      nodeDist: "win-arm64",
    },
  }
  const key = `${platform}-${arch}`
  const target = table[key]
  if (!target) {
    throw new Error(`unsupported payload target: ${key}`)
  }
  return { key, platform, arch, ...target }
}

/**
 * Build the `pip wheel` argument list. The caller invokes it through
 * `uvx pip …`, so no host pip install is necessary. It runs NATIVELY on
 * the target runner. With --only-binary=:all:, it downloads prebuilt
 * wheels for this platform and never compiles. An sdist in the payload
 * tries to build at first launch, which is offline and has no toolchain.
 * Thus the arguments refuse sdists outright. The consumer runs
 * `uv sync --frozen --offline --no-index --find-links`.
 */
export function wheelDownloadArgs({ wheelsDir }) {
  return [
    "wheel",
    "--only-binary", ":all:",
    "-r", "requirements-payload.txt",
    "-w", wheelsDir,
  ]
}

/**
 * Resolve the release tag to stage. CI passes --tag=vX.Y.Z. Local runs can
 * fall back to `git describe` for smoke tests. When bundling was requested
 * and no tag exists, payload staging is a hard error. A bundled artifact
 * without a pinned tag produces un-adoptable, un-updatable installs.
 */
export function resolveTag(argv, describeFn) {
  const explicit = argv.find((a) => a.startsWith("--tag="))
  if (explicit) {
    const tag = explicit.slice("--tag=".length).trim()
    if (!/^v\d+\.\d+\.\d+$/.test(tag)) {
      throw new Error(`--tag must be a final release tag (vX.Y.Z), got: ${tag}`)
    }
    return tag
  }
  const described = describeFn()
  if (described && /^v\d+\.\d+\.\d+$/.test(described)) {
    return described
  }
  throw new Error(
    "no release tag: pass --tag=vX.Y.Z (CI) or run from a checkout at an exact release tag"
  )
}

export function parseSkips(argv) {
  const flag = argv.find((a) => a.startsWith("--skip="))
  if (!flag) return new Set()
  const skips = new Set(
    flag
      .slice("--skip=".length)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  )
  for (const s of skips) {
    if (!PAYLOAD_ITEMS.includes(s)) {
      throw new Error(`unknown --skip item: ${s} (valid: ${PAYLOAD_ITEMS.join(", ")})`)
    }
  }
  return skips
}

/**
 * Build the manifest that describes the contents of the payload tree.
 * `items` records per-item presence. Thus the --payload-dir stages of
 * install.sh/ps1 can fall back to their network path for each missing
 * item. A partly assembled payload degrades and does not fail the whole
 * bootstrap.
 */
export function buildManifest({ tag, commit, target, staged, skipped }) {
  const items = {}
  for (const item of PAYLOAD_ITEMS) {
    items[item] = staged.includes(item)
      ? { status: "staged" }
      : { status: "skipped", reason: skipped.has(item) ? "explicit-skip" : "failed" }
  }
  return {
    schemaVersion: PAYLOAD_SCHEMA_VERSION,
    tag,
    commit,
    platform: target.platform,
    arch: target.arch,
    builtAt: new Date().toISOString(),
    items,
  }
}

// ─── impure staging steps (they shell out, have no unit tests, and run in CI) ──────

function run(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, { stdio: "inherit", ...opts })
  if (result.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} exited ${result.status}`)
  }
}

function stageRepo(tag, outDir) {
  const repoDir = path.join(outDir, "repo")
  fs.rmSync(repoDir, { recursive: true, force: true })
  // Clone from the local checkout when it has the tag. Otherwise clone
  // from origin. The clone is depth 1 at the tag. .git is kept on purpose.
  run("git", [
    "clone", "--depth", "1", "--branch", tag,
    "--config", "remote.origin.url=https://github.com/NousResearch/hermes-agent.git",
    REPO_ROOT, repoDir,
  ])
  run("git", ["-C", repoDir, "gc", "--aggressive", "--prune=now"])
  return execSync(`git -C ${JSON.stringify(repoDir)} rev-parse HEAD`, { encoding: "utf8" }).trim()
}

function stageUvAndPython(target, outDir) {
  const uvDir = path.join(outDir, "uv")
  const pythonDir = path.join(outDir, "python")
  fs.mkdirSync(uvDir, { recursive: true })
  fs.mkdirSync(pythonDir, { recursive: true })
  // Native runner: the uv that runs this build IS the target-platform uv.
  // HERMES_PAYLOAD_UV overrides this for unusual setups. The default is
  // `uv` on PATH.
  const uvName = target.platform === "win32" ? "uv.exe" : "uv"
  const uvSource =
    process.env.HERMES_PAYLOAD_UV ||
    execSync(
      target.platform === "win32" ? "where uv" : "command -v uv",
      { encoding: "utf8" }
    ).split(/\r?\n/)[0].trim()
  fs.copyFileSync(uvSource, path.join(uvDir, uvName))
  run("uv", ["python", "install", "--install-dir", pythonDir, process.env.HERMES_PAYLOAD_PYTHON || "3.11"])
}

function stageWheels(target, outDir) {
  const wheelsDir = path.join(outDir, "wheels")
  fs.mkdirSync(wheelsDir, { recursive: true })
  // Export the lock to a requirements file. Then fetch wheels natively
  // through uvx pip, so no host pip is necessary. --only-binary means
  // "download published wheels" and never compile.
  run("uv", ["export", "--frozen", "--no-emit-project", "-o", "requirements-payload.txt"], { cwd: REPO_ROOT })
  run("uvx", ["pip", ...wheelDownloadArgs({ wheelsDir })], { cwd: REPO_ROOT })
}

function stageNode(target, outDir) {
  const nodeDir = path.join(outDir, "node")
  fs.mkdirSync(nodeDir, { recursive: true })
  const src = process.env.HERMES_PAYLOAD_NODE_DIST
  if (!src) {
    throw new Error("HERMES_PAYLOAD_NODE_DIST must point at the extracted node dist for the target")
  }
  fs.cpSync(src, nodeDir, { recursive: true })
}

function stageJsPrebuilt(outDir) {
  // CI builds ui-tui (with hermes-ink) and web_dist BEFORE this script
  // runs. Here we only tar what exists. The tar excludes apps/desktop on
  // purpose. The bundled shell IS the desktop app (plan §2.1).
  const listFile = path.join(outDir, ".js-prebuilt-paths")
  const candidates = ["ui-tui/dist", "ui-tui/node_modules", "web_dist"].filter((p) =>
    fs.existsSync(path.join(REPO_ROOT, p))
  )
  if (candidates.length === 0) {
    throw new Error("no prebuilt JS surfaces found — run the ui-tui/web builds first")
  }
  fs.writeFileSync(listFile, candidates.join("\n") + "\n")
  run("tar", [
    "--zstd", "-cf", path.join(outDir, "js-prebuilt.tar.zst"),
    "-C", REPO_ROOT, "-T", listFile,
  ])
  fs.rmSync(listFile, { force: true })
}

function main() {
  if (process.env.HERMES_DESKTOP_BUNDLED !== "1") {
    // Thin build: write a stub manifest anyway. Then the extraResources
    // entry always has a real directory to copy. The behavior of
    // electron-builder for a missing `from` changes between versions. The
    // stub also lets runtime code read manifest.json uniformly and learn
    // that there are no payloads.
    fs.mkdirSync(OUT_DIR, { recursive: true })
    fs.writeFileSync(
      path.join(OUT_DIR, "manifest.json"),
      JSON.stringify({ schemaVersion: PAYLOAD_SCHEMA_VERSION, thin: true, items: {} }, null, 2) + "\n"
    )
    console.log("[stage-agent-payloads] HERMES_DESKTOP_BUNDLED != 1 — wrote thin stub manifest")
    return
  }
  const target = resolveTargets()
  const skips = parseSkips(process.argv.slice(2))
  const tag = resolveTag(process.argv.slice(2), () => {
    try {
      return execSync("git describe --tags --exact-match", { cwd: REPO_ROOT, encoding: "utf8" }).trim()
    } catch {
      return null
    }
  })

  fs.mkdirSync(OUT_DIR, { recursive: true })
  const staged = []
  let commit = null

  const steps = {
    repo: () => {
      commit = stageRepo(tag, OUT_DIR)
    },
    uv: () => stageUvAndPython(target, OUT_DIR),
    python: () => {}, // The uv step stages python too (one uv invocation).
    wheels: () => stageWheels(target, OUT_DIR),
    node: () => stageNode(target, OUT_DIR),
    "js-prebuilt": () => stageJsPrebuilt(OUT_DIR),
  }

  for (const item of PAYLOAD_ITEMS) {
    if (skips.has(item)) {
      console.log(`[stage-agent-payloads] skip: ${item}`)
      continue
    }
    console.log(`[stage-agent-payloads] staging: ${item} (${target.key}, ${tag})`)
    steps[item]()
    staged.push(item)
  }

  const manifest = buildManifest({ tag, commit, target, staged, skipped: skips })
  fs.writeFileSync(path.join(OUT_DIR, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n")
  console.log(`[stage-agent-payloads] wrote ${path.join(OUT_DIR, "manifest.json")}`)
}

if (isMain(import.meta.url)) {
  main()
}
