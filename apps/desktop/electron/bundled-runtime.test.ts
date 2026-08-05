import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  type AdoptionFacts,
  adoptionManifest,
  decideAdoption,
  executeAdoptionCheckout,
  gatherGitFacts,
  type GitRunner,
  needsRematerialization,
  payloadArgs,
  resolvePayload
} from '../electron/bundled-runtime'

// ─── resolvePayload ────────────────────────────────────────────────

const readerFor = (manifest: unknown) => (p: string) => {
  if (!p.endsWith('manifest.json')) {throw new Error('ENOENT')}

  return JSON.stringify(manifest)
}

test('resolvePayload returns null for dev runs, thin stubs, and garbage', () => {
  assert.equal(resolvePayload(null), null)
  assert.equal(resolvePayload(undefined), null)
  assert.equal(resolvePayload('/res', readerFor({ schemaVersion: 1, thin: true, items: {} })), null)
  assert.equal(
    resolvePayload('/res', () => {
      throw new Error('ENOENT')
    }),
    null
  )
  assert.equal(resolvePayload('/res', readerFor('not-an-object')), null)
  // Manifest with items but nothing actually staged ⇒ null (all-skipped payload).
  assert.equal(
    resolvePayload('/res', readerFor({ tag: 'v1.0.0', items: { repo: { status: 'skipped' } } })),
    null
  )
})

test('resolvePayload returns dir + tag for a real payload', () => {
  const p = resolvePayload('/res', readerFor({ tag: 'v1.2.3', items: { repo: { status: 'staged' } } }))
  assert.ok(p)
  assert.match(p.dir, /agent-payload$/)
  assert.equal(p.tag, 'v1.2.3')
})

test('payloadArgs maps installer kind, empty for null payload', () => {
  const p = { dir: '/res/agent-payload', tag: 'v1.0.0', items: {} }
  assert.deepEqual(payloadArgs('posix', p), ['--payload-dir', '/res/agent-payload'])
  assert.deepEqual(payloadArgs('powershell', p), ['-PayloadDir', '/res/agent-payload'])
  assert.deepEqual(payloadArgs('posix', null), [])
})

// ─── needsRematerialization ────────────────────────────────────────

const bundledStamp = { payload: true, tag: 'v2.0.0' }
const bundledManifest = { installMode: 'bundled' }

test('fires only on bundled-mode checkouts when the stamp tag moved', () => {
  assert.equal(needsRematerialization({ pinnedTag: 'v1.0.0' }, bundledStamp, bundledManifest), true)
  assert.equal(needsRematerialization({ pinnedTag: 'v2.0.0' }, bundledStamp, bundledManifest), false)
})

test('never fires for thin builds, missing markers, or non-bundled checkouts', () => {
  assert.equal(needsRematerialization({ pinnedTag: 'v1.0.0' }, { payload: false, tag: null }, bundledManifest), false)
  assert.equal(needsRematerialization({ pinnedTag: 'v1.0.0' }, null, bundledManifest), false)
  assert.equal(needsRematerialization(null, bundledStamp, bundledManifest), false)
  // Ejected / source-managed: user owns updates.
  assert.equal(needsRematerialization({ pinnedTag: 'v1.0.0' }, bundledStamp, { installMode: 'source' }), false)
  // Legacy checkout without a manifest: only adoption may touch it.
  assert.equal(needsRematerialization({ pinnedTag: 'v1.0.0' }, bundledStamp, null), false)
})

// ─── decideAdoption ────────────────────────────────────────────────

const pristine: AdoptionFacts = {
  stampHasPayload: true,
  stampTag: 'v2.0.0',
  installManifest: null,
  gitCheckoutExists: true,
  workingTreeClean: true,
  currentBranch: 'main',
  headIsAncestorOfTag: true,
  recentManualUpdateDays: null
}

test('a pristine legacy checkout adopts', () => {
  assert.deepEqual(decideAdoption(pristine), { adopt: true })
})

test('every non-pristine variation refuses, with a reason', () => {
  const cases: Array<[Partial<AdoptionFacts>, RegExp]> = [
    [{ stampHasPayload: false }, /thin build/],
    [{ stampTag: null }, /thin build/],
    [{ installManifest: { installMode: 'source', manageStyle: 'ejected' } }, /ejected/],
    [{ installManifest: { installMode: 'bundled', manageStyle: 'adopted' } }, /already bundled/],
    [{ installManifest: { installMode: 'source', manageStyle: 'auto-adopted' } }, /manageStyle/],
    [{ gitCheckoutExists: false }, /no git checkout/],
    [{ workingTreeClean: false }, /not clean/],
    [{ currentBranch: 'ethie/my-branch' }, /not main/],
    [{ currentBranch: null }, /not main/],
    [{ recentManualUpdateDays: 3 }, /cohabiting/],
    [{ headIsAncestorOfTag: false }, /not an ancestor/],
    [{ headIsAncestorOfTag: null }, /deferring/]
  ]

  for (const [override, reason] of cases) {
    const decision = decideAdoption({ ...pristine, ...override })
    assert.equal(decision.adopt, false, JSON.stringify(override))
    assert.match((decision as { adopt: false; reason: string }).reason, reason)
  }
})

test('a plain source manifest without manageStyle stays adoptable', () => {
  // install.sh writes {installMode: source, channel: main} with NO style on
  // network installs — those users are exactly the silent-adoption cohort.
  const decision = decideAdoption({ ...pristine, installManifest: { installMode: 'source' } })
  assert.deepEqual(decision, { adopt: true })
})

test('a stale manual update (>= 30d) no longer blocks', () => {
  assert.deepEqual(decideAdoption({ ...pristine, recentManualUpdateDays: 45 }), { adopt: true })
})

test('adoptionManifest is bundled/stable/auto-adopted at the tag', () => {
  assert.deepEqual(adoptionManifest('v2.0.0'), {
    schemaVersion: 1,
    installMode: 'bundled',
    channel: 'stable',
    manageStyle: 'auto-adopted',
    pinnedTag: 'v2.0.0'
  })
})

// ─── gatherGitFacts ────────────────────────────────────────────────

function fakeGit(responses: Record<string, { code: number; stdout: string }>): GitRunner & { calls: string[] } {
  const calls: string[] = []

  const runner = ((args: string[], _cwd: string) => {
    const key = args.join(' ')
    calls.push(key)

    for (const [prefix, result] of Object.entries(responses)) {
      if (key.startsWith(prefix)) {return result}
    }

    return { code: 0, stdout: '' }
  }) as GitRunner & { calls: string[] }

  runner.calls = calls

  return runner
}

const cleanMainRepo = {
  'rev-parse --git-dir': { code: 0, stdout: '.git\n' },
  'status --porcelain -uno': { code: 0, stdout: '' },
  'rev-parse --abbrev-ref HEAD': { code: 0, stdout: 'main\n' },
  'rev-parse --is-shallow-repository': { code: 0, stdout: 'false\n' },
  'fetch origin tag v2.0.0': { code: 0, stdout: '' },
  'merge-base --is-ancestor': { code: 0, stdout: '' }
}

test('clean main checkout at an ancestor of the tag ⇒ fully adoptable facts', () => {
  const git = fakeGit(cleanMainRepo)
  const facts = gatherGitFacts('/root', 'v2.0.0', git)
  assert.deepEqual(facts, {
    gitCheckoutExists: true,
    workingTreeClean: true,
    currentBranch: 'main',
    headIsAncestorOfTag: true
  })
})

test('merge-base exit 1 means not-ancestor; other exits mean unknown', () => {
  const notAncestor = gatherGitFacts(
    '/root',
    'v2.0.0',
    fakeGit({ ...cleanMainRepo, 'merge-base --is-ancestor': { code: 1, stdout: '' } })
  )

  assert.equal(notAncestor.headIsAncestorOfTag, false)

  const broken = gatherGitFacts(
    '/root',
    'v2.0.0',
    fakeGit({ ...cleanMainRepo, 'merge-base --is-ancestor': { code: 128, stdout: '' } })
  )

  assert.equal(broken.headIsAncestorOfTag, null)
})

test('offline fetch leaves ancestry unknown (defer), not false', () => {
  const facts = gatherGitFacts(
    '/root',
    'v2.0.0',
    fakeGit({ ...cleanMainRepo, 'fetch origin tag v2.0.0': { code: 128, stdout: '' } })
  )

  assert.equal(facts.headIsAncestorOfTag, null)
})

test('shallow checkouts unshallow first; failed unshallow defers', () => {
  const git = fakeGit({
    ...cleanMainRepo,
    'rev-parse --is-shallow-repository': { code: 0, stdout: 'true\n' },
    'fetch --unshallow origin main': { code: 0, stdout: '' }
  })

  const facts = gatherGitFacts('/root', 'v2.0.0', git)
  assert.equal(facts.headIsAncestorOfTag, true)
  assert.ok(git.calls.some(c => c.startsWith('fetch --unshallow')))

  const offline = gatherGitFacts(
    '/root',
    'v2.0.0',
    fakeGit({
      ...cleanMainRepo,
      'rev-parse --is-shallow-repository': { code: 0, stdout: 'true\n' },
      'fetch --unshallow origin main': { code: 128, stdout: '' }
    })
  )

  assert.equal(offline.headIsAncestorOfTag, null)
})

test('dirty or off-main checkouts never touch the network', () => {
  const dirty = fakeGit({ ...cleanMainRepo, 'status --porcelain -uno': { code: 0, stdout: ' M cli.py\n' } })
  gatherGitFacts('/root', 'v2.0.0', dirty)
  assert.ok(!dirty.calls.some(c => c.startsWith('fetch')))

  const offMain = fakeGit({ ...cleanMainRepo, 'rev-parse --abbrev-ref HEAD': { code: 0, stdout: 'dev\n' } })
  gatherGitFacts('/root', 'v2.0.0', offMain)
  assert.ok(!offMain.calls.some(c => c.startsWith('fetch')))
})

test('not a git repo ⇒ all-negative facts, no further probes', () => {
  const git = fakeGit({ 'rev-parse --git-dir': { code: 128, stdout: '' } })
  const facts = gatherGitFacts('/root', 'v2.0.0', git)
  assert.equal(facts.gitCheckoutExists, false)
  assert.equal(git.calls.length, 1)
})

// ─── executeAdoptionCheckout ───────────────────────────────────────

test('adoption executes exactly one checkout -B main at the tag commit', () => {
  const git = fakeGit({ 'checkout -B main v2.0.0^{commit}': { code: 0, stdout: '' } })
  assert.equal(executeAdoptionCheckout('/root', 'v2.0.0', git), true)
  assert.deepEqual(git.calls, ['checkout -B main v2.0.0^{commit}'])

  const failing = fakeGit({ 'checkout -B main': { code: 1, stdout: '' } })
  assert.equal(executeAdoptionCheckout('/root', 'v2.0.0', failing), false)
})
