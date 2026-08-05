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
      uvPython: "linux-x86_64-gnu",
    },
    "linux-arm64": {
      uvTarget: "aarch64-unknown-linux-gnu",
      pythonPlatform: "aarch64-unknown-linux-gnu",
      nodeDist: "linux-arm64",
      uvPython: "linux-aarch64-gnu",
    },
    "darwin-x64": {
      uvTarget: "x86_64-apple-darwin",
      pythonPlatform: "x86_64-apple-darwin",
      nodeDist: "darwin-x64",
      uvPython: "macos-x86_64-none",
    },
    "darwin-arm64": {
      uvTarget: "aarch64-apple-darwin",
      pythonPlatform: "aarch64-apple-darwin",
      nodeDist: "darwin-arm64",
      uvPython: "macos-aarch64-none",
    },
    "win32-x64": {
      uvTarget: "x86_64-pc-windows-msvc",
      pythonPlatform: "x86_64-pc-windows-msvc",
      nodeDist: "win-x64",
      uvPython: "windows-x86_64-none",
    },
    "win32-arm64": {
      uvTarget: "aarch64-pc-windows-msvc",
      pythonPlatform: "aarch64-pc-windows-msvc",
      nodeDist: "win-arm64",
      uvPython: "windows-aarch64-none",
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
 * The full uv python-install request for a target: version AND platform.
 * A bare version request ("3.11") lets uv fall back to another
 * architecture when the native build is unavailable — the arm64 Windows
 * test box got a silent x86_64 CPython that way. The full request either
 * installs the right build or fails loudly.
 */
export function pythonRequest(target, version = process.env.HERMES_PAYLOAD_PYTHON || "3.11") {
  return `cpython-${version}-${target.uvPython}`
}

/**
 * Assert that a staged tool's own version banner names the target triple.
 * `uv --version` and `python -VV` both print their build triple/platform.
 * A mismatch means the payload carries the WRONG architecture (for
 * example, an x64 uv copied from PATH into an arm64 artifact — it runs
 * on the build host through emulation and ships broken). The manifest
 * would then lie about the payload's contents. Fail the build instead.
 */
export function assertBanner(item, banner, mustContain) {
  if (!banner.includes(mustContain)) {
    throw new Error(
      `${item}: staged binary reports "${banner.trim()}" which does not ` +
        `contain the build target "${mustContain}" — wrong-architecture ` +
        `payload. Provide a matching binary (HERMES_PAYLOAD_UV for uv) or ` +
        `build on a native runner.`
    )
  }
}

/**
 * The substring that each staged tool's banner must contain for a target.
 * uv prints a full triple (x86_64-pc-windows-msvc). CPython's `python -VV`
 * prints a compiler/platform line that differs per OS, so the check keys
 * on the architecture words for it. Node prints nothing useful in
 * --version, so its check uses `node -p process.arch` = target arch.
 */
export function bannerExpectations(target) {
  const archWords = {
    x64: ["x86_64", "AMD64", "x64"],
    arm64: ["aarch64", "ARM64", "arm64"],
  }[target.arch]

  return {
    uv: target.uvTarget,
    pythonAny: archWords,
    node: target.arch,
  }
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
  // Wipe before staging (stageRepo does the same). A rerun after a failed
  // or wrong-arch attempt must not leave a stale interpreter beside the
  // new one — the banner probe would find the old build first.
  fs.rmSync(uvDir, { recursive: true, force: true })
  fs.rmSync(pythonDir, { recursive: true, force: true })
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
  const uvStaged = path.join(uvDir, uvName)
  fs.copyFileSync(uvSource, uvStaged)

  const expect = bannerExpectations(target)

  // The staged uv must be built FOR the target triple, not merely run on
  // this host (emulation makes a wrong-arch binary run fine here).
  assertBanner("uv", execSync(`${JSON.stringify(uvStaged)} --version`, { encoding: "utf8" }), expect.uv)

  // --no-bin: staging must not write launcher shims into the build
  // host's ~/.local/bin (it collided with a preexisting python3.11.exe
  // on the Windows test box).
  run("uv", ["python", "install", "--no-bin", "--install-dir", pythonDir, pythonRequest(target)])

  // The installed CPython names its architecture in `python -VV`.
  const pythonBinary = findPythonBinary(pythonDir, target)
  const pythonBanner = execSync(`${JSON.stringify(pythonBinary)} -VV`, { encoding: "utf8" })
  if (!expect.pythonAny.some((word) => pythonBanner.includes(word))) {
    assertBanner("python", pythonBanner, expect.pythonAny.join("|"))
  }
}

function findPythonBinary(pythonDir, target) {
  // uv installs into <dir>/cpython-<ver>-<os>-<triple>/… — search only
  // inside directories that match the REQUESTED build (pythonRequest),
  // so a stray install of another architecture can never satisfy the
  // probe. The wipe above should prevent strays; this is the backstop.
  const name = target.platform === "win32" ? "python.exe" : "python3"
  const wanted = pythonRequest(target)
  const roots = fs
    .readdirSync(pythonDir, { withFileTypes: true })
    .filter((e) => e.isDirectory() && e.name.startsWith(wanted))
    .map((e) => path.join(pythonDir, e.name))
  if (roots.length === 0) {
    throw new Error(`python: no ${wanted}* directory under ${pythonDir} after uv python install`)
  }
  const stack = [...roots]
  while (stack.length) {
    const dir = stack.pop()
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        stack.push(full)
      } else if (entry.name === name) {
        return full
      }
    }
  }
  throw new Error(`python: no ${name} found under ${roots.join(", ")}`)
}

/**
 * Check a wheelhouse file list against the target. Returns the offending
 * names. A wheel is wrong when its platform tag names another OS or
 * another architecture. Pure-python wheels (`none-any`) are always fine.
 * pip resolves tags for the interpreter that RUNS it, so a wrong-arch
 * python (or an emulated one) fills the wheelhouse with wheels the
 * payload's CPython cannot import — at first launch, offline.
 */
export function wrongArchWheels(fileNames, target) {
  const want = {
    "linux-x64": /manylinux.*x86_64|musllinux.*x86_64|linux_x86_64/,
    "linux-arm64": /manylinux.*aarch64|musllinux.*aarch64|linux_aarch64/,
    "darwin-x64": /macosx.*(x86_64|universal2|intel)/,
    "darwin-arm64": /macosx.*(arm64|universal2)/,
    "win32-x64": /win_amd64/,
    "win32-arm64": /win_arm64/,
  }[target.key]

  return fileNames.filter((name) => {
    if (!name.endsWith(".whl")) return false
    const platformTag = name.slice(0, -4).split("-").pop() || ""
    if (platformTag === "any") return false
    return !want.test(platformTag)
  })
}

function stageWheels(target, outDir) {
  const wheelsDir = path.join(outDir, "wheels")
  fs.mkdirSync(wheelsDir, { recursive: true })
  // Export the lock to a requirements file. Then fetch wheels natively
  // through uvx pip, so no host pip is necessary. --only-binary means
  // "download published wheels" and never compile.
  run("uv", ["export", "--frozen", "--no-emit-project", "-o", "requirements-payload.txt"], { cwd: REPO_ROOT })
  run("uvx", ["pip", ...wheelDownloadArgs({ wheelsDir })], { cwd: REPO_ROOT })

  const bad = wrongArchWheels(fs.readdirSync(wheelsDir), target)
  if (bad.length > 0) {
    throw new Error(
      `wheels: ${bad.length} wheel(s) carry a platform tag for another ` +
        `target than ${target.key}:\n  ${bad.join("\n  ")}\n` +
        `The pip that filled this wheelhouse resolves tags for its OWN ` +
        `interpreter — build on a native runner.`
    )
  }
}

function stageNode(target, outDir) {
  const nodeDir = path.join(outDir, "node")
  fs.mkdirSync(nodeDir, { recursive: true })
  const src = process.env.HERMES_PAYLOAD_NODE_DIST
  if (!src) {
    throw new Error("HERMES_PAYLOAD_NODE_DIST must point at the extracted node dist for the target")
  }
  fs.cpSync(src, nodeDir, { recursive: true })

  // The dist must be FOR the target. Running the staged node is not a
  // valid probe here: a wrong-arch binary can still run through the
  // build host's emulation. `node -p process.arch` names the arch the
  // binary was BUILT for, so execute it only to read that value; when
  // the binary cannot run at all, that is the same wrong-arch verdict.
  const nodeBinary = target.platform === "win32" ? path.join(nodeDir, "node.exe") : path.join(nodeDir, "bin", "node")
  let reportedArch = null
  try {
    reportedArch = execSync(`${JSON.stringify(nodeBinary)} -p process.arch`, { encoding: "utf8" }).trim()
  } catch {
    // Unrunnable on this host — for example an arm64 dist on an x64
    // builder with no emulation. That is not proof of a wrong payload,
    // but it IS unverifiable; refuse rather than ship unchecked.
    throw new Error(`node: staged binary at ${nodeBinary} did not run, so its architecture is unverified`)
  }
  assertBanner("node", reportedArch, bannerExpectations(target).node)
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
