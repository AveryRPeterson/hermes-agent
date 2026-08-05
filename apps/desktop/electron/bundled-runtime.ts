// bundled-runtime.ts — decision logic for the bundled desktop runtime:
// payload discovery, marker-tag invalidation (when does an app update force
// offline re-materialization?), and silent adoption eligibility for pristine
// legacy checkouts.
//
// Design: .hermes/plans/2026-08-05_desktop-bundled-payloads-channels-eject.md
// (§1.4 adoption, §4.3 bundled update flow).
//
// Everything here is pure (dependencies injected) so vitest covers the whole
// decision surface; the impure executors live in main.ts / bootstrap-runner.

import fs from 'node:fs'
import path from 'node:path'

// ─── payload discovery ──────────────────────────────────────────────────────

export interface PayloadInfo {
  dir: string
  tag: string | null
  items: Record<string, { status: string }>
}

/**
 * Resolve the agent-payload directory shipped in the packaged app's
 * resources. Returns null for thin builds (stub manifest with thin:true),
 * dev runs (no resourcesPath), or unreadable manifests — every caller treats
 * null as "behave exactly like today's network bootstrap".
 */
export function resolvePayload(
  resourcesPath: string | null | undefined,
  readFile: (p: string) => string = p => fs.readFileSync(p, 'utf8')
): PayloadInfo | null {
  if (!resourcesPath) {
    return null
  }

  const dir = path.join(resourcesPath, 'agent-payload')

  let parsed

  try {
    parsed = JSON.parse(readFile(path.join(dir, 'manifest.json')))
  } catch {
    return null
  }

  if (!parsed || typeof parsed !== 'object' || parsed.thin === true) {
    return null
  }

  const items = parsed.items && typeof parsed.items === 'object' ? parsed.items : {}
  const hasAny = Object.values(items).some((v: any) => v && v.status === 'staged')

  if (!hasAny) {
    return null
  }

  return { dir, tag: typeof parsed.tag === 'string' ? parsed.tag : null, items }
}

/** Installer argv addition for a payload-backed bootstrap. */
export function payloadArgs(installerKind: 'posix' | 'powershell', payload: PayloadInfo | null): string[] {
  if (!payload) {
    return []
  }

  return installerKind === 'posix' ? ['--payload-dir', payload.dir] : ['-PayloadDir', payload.dir]
}

// ─── marker-tag invalidation ────────────────────────────────────────────────

/**
 * Does the completed-bootstrap marker need invalidating because the app
 * updated to a build carrying NEWER payloads?
 *
 * True only when this build ships payloads (stamp.payload) with a real tag,
 * the checkout's install manifest says installMode:bundled (it opted into
 * desktop-managed materialization), AND the marker's pinnedTag differs.
 *
 * Deliberately false for everything else:
 * - no install manifest (legacy checkout) — ONLY the adoption flow, with its
 *   pristineness gates, may move a legacy checkout. Re-materializing here
 *   would be silent adoption without consent checks.
 * - installMode:source (ejected / user-managed) — the user owns updates.
 * - thin builds, missing marker (normal bootstrap-needed logic owns that).
 */
export function needsRematerialization(
  marker: { pinnedTag?: string | null } | null,
  stamp: { payload?: boolean; tag?: string | null } | null,
  installManifest?: { installMode?: string } | null
): boolean {
  if (!stamp || stamp.payload !== true || !stamp.tag) {
    return false
  }

  if (!marker) {
    return false
  }

  if (!installManifest || installManifest.installMode !== 'bundled') {
    return false
  }

  return marker.pinnedTag !== stamp.tag
}

// ─── silent adoption (plan §1.4) ────────────────────────────────────────────

export interface AdoptionFacts {
  // From the packaged build:
  stampHasPayload: boolean
  stampTag: string | null
  // From the checkout:
  installManifest: { installMode?: string; manageStyle?: string } | null
  gitCheckoutExists: boolean
  workingTreeClean: boolean
  currentBranch: string | null
  headIsAncestorOfTag: boolean | null // null = could not determine (offline, fetch failed)
  // From update-state:
  recentManualUpdateDays: number | null // null = never / unknown
}

export type AdoptionDecision =
  | { adopt: true }
  | { adopt: false; reason: string }

export const RECENT_MANUAL_UPDATE_WINDOW_DAYS = 30

/**
 * Should this launch silently adopt the checkout into the bundled path?
 *
 * The bias is "when unsure, don't adopt": every ambiguous or unverifiable
 * input returns adopt:false with a reason (logged, never shown to the user —
 * failure to adopt is silent and retried at a later launch/release).
 */
export function decideAdoption(facts: AdoptionFacts): AdoptionDecision {
  if (!facts.stampHasPayload || !facts.stampTag) {
    return { adopt: false, reason: 'thin build (no payloads)' }
  }

  const manifest = facts.installManifest

  if (manifest) {
    if (manifest.manageStyle === 'ejected') {
      return { adopt: false, reason: 'checkout is ejected (sticky opt-out)' }
    }

    if (manifest.installMode === 'bundled') {
      return { adopt: false, reason: 'already bundled' }
    }

    if (manifest.manageStyle) {
      return { adopt: false, reason: `manageStyle=${manifest.manageStyle} present` }
    }

    // A manifest with installMode:source but NO manageStyle is a deliberate
    // source install (written by install.sh without payloads) — legacy
    // checkouts have no manifest at all. Both are adoptable per plan §1.4
    // only when style is absent; mode source alone doesn't opt out, the
    // remaining pristine checks decide.
  }

  if (!facts.gitCheckoutExists) {
    return { adopt: false, reason: 'no git checkout' }
  }

  if (!facts.workingTreeClean) {
    return { adopt: false, reason: 'working tree not clean' }
  }

  if (facts.currentBranch !== 'main') {
    return { adopt: false, reason: `on branch ${facts.currentBranch || '<detached>'}, not main` }
  }

  if (
    facts.recentManualUpdateDays !== null &&
    facts.recentManualUpdateDays < RECENT_MANUAL_UPDATE_WINDOW_DAYS
  ) {
    return {
      adopt: false,
      reason: `manual hermes update ${facts.recentManualUpdateDays}d ago (cohabiting CLI user)`
    }
  }

  if (facts.headIsAncestorOfTag !== true) {
    return {
      adopt: false,
      reason:
        facts.headIsAncestorOfTag === null
          ? 'ancestry unknown (offline or fetch failed) — deferring'
          : 'HEAD not an ancestor of the release tag (local commits or ahead of release)'
    }
  }

  return { adopt: true }
}

/**
 * The manifest to write after a successful adoption. auto-adopted is kept
 * distinct from adopted so a bad auto-adoption cohort can be bulk-reverted
 * without touching users who chose bundled explicitly.
 */
export function adoptionManifest(tag: string) {
  return {
    schemaVersion: 1,
    installMode: 'bundled',
    channel: 'stable',
    manageStyle: 'auto-adopted',
    pinnedTag: tag
  }
}

// ─── adoption fact-gathering + execution ────────────────────────────────────

export type GitRunner = (args: string[], cwd: string) => { code: number; stdout: string }

/**
 * Gather the git-side AdoptionFacts for a checkout. Network is touched only
 * for the ancestry probe (a tag-scoped fetch; --unshallow first when the
 * checkout is a depth-1 installer clone). Every failure degrades to the
 * "don't adopt" side of the fact: null ancestry, dirty tree, etc.
 */
export function gatherGitFacts(
  activeRoot: string,
  tag: string,
  git: GitRunner
): Pick<AdoptionFacts, 'gitCheckoutExists' | 'workingTreeClean' | 'currentBranch' | 'headIsAncestorOfTag'> {
  const probe = git(['rev-parse', '--git-dir'], activeRoot)

  if (probe.code !== 0) {
    return { gitCheckoutExists: false, workingTreeClean: false, currentBranch: null, headIsAncestorOfTag: null }
  }

  // -uno: untracked files don't block adoption (mirrors write-build-stamp's
  // dirty probe and install.sh's lockfile-churn tolerance is upstream of us —
  // npm churn shows as tracked modifications, which DO block. Conservative.)
  const status = git(['status', '--porcelain', '-uno'], activeRoot)
  const workingTreeClean = status.code === 0 && status.stdout.trim() === ''

  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD'], activeRoot)
  const currentBranch = branch.code === 0 ? branch.stdout.trim() : null

  let headIsAncestorOfTag: boolean | null = null

  if (workingTreeClean && currentBranch === 'main') {
    const shallow = git(['rev-parse', '--is-shallow-repository'], activeRoot)

    if (shallow.code === 0 && shallow.stdout.trim() === 'true') {
      const unshallow = git(['fetch', '--unshallow', 'origin', 'main'], activeRoot)

      if (unshallow.code !== 0) {
        return { gitCheckoutExists: true, workingTreeClean, currentBranch, headIsAncestorOfTag: null }
      }
    }

    const fetch = git(['fetch', 'origin', 'tag', tag], activeRoot)

    if (fetch.code === 0) {
      const ancestor = git(['merge-base', '--is-ancestor', 'HEAD', `${tag}^{commit}`], activeRoot)

      // merge-base --is-ancestor: 0 = yes, 1 = no, anything else = error.
      headIsAncestorOfTag = ancestor.code === 0 ? true : ancestor.code === 1 ? false : null
    }
  }

  return { gitCheckoutExists: true, workingTreeClean, currentBranch, headIsAncestorOfTag }
}

/**
 * Execute an adoption the decision already approved: fast-forward main to
 * the release tag. Returns true on success; on any failure the caller leaves
 * the checkout in source mode (the reflog preserves the previous state, and
 * checkout -B is itself atomic per-ref — no partial adoption state exists).
 * The caller re-runs the bootstrap afterwards so venv/js re-materialize from
 * payloads, then writes adoptionManifest().
 */
export function executeAdoptionCheckout(activeRoot: string, tag: string, git: GitRunner): boolean {
  return git(['checkout', '-B', 'main', `${tag}^{commit}`], activeRoot).code === 0
}
