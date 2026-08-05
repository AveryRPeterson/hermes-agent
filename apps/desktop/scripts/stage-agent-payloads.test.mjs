import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  assertBanner,
  bannerExpectations,
  buildManifest,
  parseSkips,
  PAYLOAD_ITEMS,
  pythonDirPattern,
  pythonRequest,
  resolveTag,
  resolveTargets,
  wheelDownloadArgs,
  wrongArchWheels
} from '../scripts/stage-agent-payloads.mjs'

// ─── resolveTargets ────────────────────────────────────────────────

test('resolveTargets covers every shipping (platform, arch) pair', () => {
  for (const [platform, arch] of [
    ['linux', 'x64'],
    ['linux', 'arm64'],
    ['darwin', 'x64'],
    ['darwin', 'arm64'],
    ['win32', 'x64'],
    ['win32', 'arm64']
  ]) {
    const t = resolveTargets(platform, arch)
    // Invariant: every target specifies all three toolchain descriptors.
    assert.ok(t.uvTarget && t.pythonPlatform && t.nodeDist, `${platform}-${arch}`)
    assert.equal(t.platform, platform)
    assert.equal(t.arch, arch)
  }
})

test('resolveTargets rejects unknown pairs (no universal2, no ia32)', () => {
  assert.throws(() => resolveTargets('darwin', 'universal'), /unsupported/)
  assert.throws(() => resolveTargets('win32', 'ia32'), /unsupported/)
})

test('windows targets map to msvc toolchains, darwin to apple, linux to gnu', () => {
  assert.match(resolveTargets('win32', 'x64').pythonPlatform, /windows-msvc$/)
  assert.match(resolveTargets('darwin', 'arm64').pythonPlatform, /apple-darwin$/)
  assert.match(resolveTargets('linux', 'x64').pythonPlatform, /linux-gnu$/)
})

// ─── wheelDownloadArgs ─────────────────────────────────────────────

test('wheel fetch refuses sdists and targets the wheelhouse dir', () => {
  const args = wheelDownloadArgs({ wheelsDir: '/out/wheels' })
  // Invariants: the requirements come from the frozen lockfile, and the
  // fetch is binary-only. An sdist in the payload tries to compile at
  // first launch, which is offline and has no toolchain. The fetch is
  // native, so no --platform cross-tags belong here.
  assert.equal(args[0], 'wheel')
  assert.ok(args.includes('--only-binary'))
  assert.equal(args[args.indexOf('-r') + 1], 'requirements-payload.txt')
  assert.equal(args[args.indexOf('-w') + 1], '/out/wheels')
  assert.ok(!args.includes('--platform'))
})

// ─── resolveTag ────────────────────────────────────────────────────

test('explicit --tag wins and must be a final release', () => {
  assert.equal(resolveTag(['--tag=v1.2.3'], () => null), 'v1.2.3')
  assert.throws(() => resolveTag(['--tag=v1.2.3-rc1'], () => null), /final release/)
  assert.throws(() => resolveTag(['--tag=main'], () => null), /final release/)
})

test('falls back to git describe only for exact release tags', () => {
  assert.equal(resolveTag([], () => 'v0.17.0'), 'v0.17.0')
  assert.throws(() => resolveTag([], () => 'v0.17.0-14-gdeadbeef'), /no release tag/)
  assert.throws(() => resolveTag([], () => null), /no release tag/)
})

// ─── parseSkips ────────────────────────────────────────────────────

test('parseSkips accepts known items and rejects unknown ones', () => {
  assert.deepEqual([...parseSkips(['--skip=wheels,node'])].sort(), ['node', 'wheels'])
  assert.equal(parseSkips([]).size, 0)
  assert.throws(() => parseSkips(['--skip=venv']), /unknown --skip/)
})

// ─── buildManifest ─────────────────────────────────────────────────

test('manifest records staged vs explicitly-skipped vs failed per item', () => {
  const target = resolveTargets('linux', 'x64')
  const manifest = buildManifest({
    tag: 'v1.0.0',
    commit: 'a'.repeat(40),
    target,
    staged: ['repo', 'uv', 'python'],
    skipped: new Set(['wheels'])
  })
  assert.equal(manifest.tag, 'v1.0.0')
  // Invariant: every payload item has an entry. The per-stage fallback
  // logic of the bootstrap reads presence. An absent entry is ambiguous.
  for (const item of PAYLOAD_ITEMS) {
    assert.ok(manifest.items[item], item)
  }
  assert.equal(manifest.items.repo.status, 'staged')
  assert.equal(manifest.items.wheels.status, 'skipped')
  assert.equal(manifest.items.wheels.reason, 'explicit-skip')
  // node was not staged and not explicitly skipped, so its status is failed.
  assert.equal(manifest.items.node.reason, 'failed')
})

// ─── arch guards ────────────────────────────────────────────────────

test('assertBanner passes on a matching triple and throws on a foreign one', () => {
  const target = resolveTargets('win32', 'arm64')
  const expect = bannerExpectations(target)

  assert.doesNotThrow(() =>
    assertBanner('uv', 'uv 0.12.1 (329541a50 aarch64-pc-windows-msvc)', expect.uv)
  )
  // The exact failure from the first Windows test build: an x64 uv from
  // PATH staged into an arm64 artifact (it ran via emulation).
  assert.throws(
    () => assertBanner('uv', 'uv 0.12.1 (329541a50 x86_64-pc-windows-msvc)', expect.uv),
    /wrong-architecture/
  )
})

test('wheel arch check flags foreign tags and accepts pure + native wheels', () => {
  const win64 = resolveTargets('win32', 'x64')
  const names = [
    'charset_normalizer-3.4.0-cp311-cp311-win_amd64.whl',
    'requests-2.32.3-py3-none-any.whl',
    'pydantic_core-2.27.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl',
    'requirements-payload.txt'
  ]

  assert.deepEqual(wrongArchWheels(names, win64), [
    'pydantic_core-2.27.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl'
  ])

  // macOS universal2 satisfies both mac targets.
  const macArm = resolveTargets('darwin', 'arm64')
  assert.deepEqual(wrongArchWheels(['x-1.0-cp311-cp311-macosx_11_0_universal2.whl'], macArm), [])
})

test('banner expectations name the target, not the build host', () => {
  const linuxArm = resolveTargets('linux', 'arm64')
  assert.equal(bannerExpectations(linuxArm).uv, 'aarch64-unknown-linux-gnu')
  assert.equal(bannerExpectations(linuxArm).node, 'arm64')
  assert.ok(bannerExpectations(linuxArm).pythonAny.includes('aarch64'))
})

test('python install requests name the full build, not just the version', () => {
  // A bare "3.11" lets uv substitute another architecture when the native
  // build is missing — the silent x86_64-on-arm64 failure. The request
  // must pin cpython-<ver>-<os>-<arch>-<libc>.
  assert.equal(pythonRequest(resolveTargets('win32', 'arm64'), '3.11'), 'cpython-3.11-windows-aarch64-none')
  assert.equal(pythonRequest(resolveTargets('linux', 'x64'), '3.11'), 'cpython-3.11-linux-x86_64-gnu')
  assert.equal(pythonRequest(resolveTargets('darwin', 'arm64'), '3.12'), 'cpython-3.12-macos-aarch64-none')
})

test('python dir matcher accepts patch-versioned installs and rejects foreign builds', () => {
  const winArm = resolveTargets('win32', 'arm64')
  const pattern = pythonDirPattern(winArm, '3.11')

  // uv creates the patch-versioned directory plus a minor-version alias.
  assert.ok(pattern.test('cpython-3.11.15-windows-aarch64-none'))
  assert.ok(pattern.test('cpython-3.11-windows-aarch64-none'))
  // Another arch, another version, or a partial name must not match.
  assert.ok(!pattern.test('cpython-3.11.15-windows-x86_64-none'))
  assert.ok(!pattern.test('cpython-3.12.1-windows-aarch64-none'))
  assert.ok(!pattern.test('cpython-3.115-windows-aarch64-none'))
})
