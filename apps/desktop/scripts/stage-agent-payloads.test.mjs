import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  PAYLOAD_ITEMS,
  buildManifest,
  parseSkips,
  resolveTag,
  resolveTargets,
  wheelDownloadArgs
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
    // Invariant: every target fully specifies all three toolchain descriptors.
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
  // Invariants: frozen-lockfile-derived requirements and binary-only (an
  // sdist in the payload would try to compile at first launch — offline,
  // no toolchain). Native fetch: no --platform cross-tags belong here.
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
  // Invariant: every payload item has an entry — the bootstrap's per-stage
  // fallback logic reads presence, absence would be ambiguous.
  for (const item of PAYLOAD_ITEMS) {
    assert.ok(manifest.items[item], item)
  }
  assert.equal(manifest.items.repo.status, 'staged')
  assert.equal(manifest.items.wheels.status, 'skipped')
  assert.equal(manifest.items.wheels.reason, 'explicit-skip')
  // node was neither staged nor explicitly skipped ⇒ failed.
  assert.equal(manifest.items.node.reason, 'failed')
})
