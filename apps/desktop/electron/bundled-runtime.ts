// bundled-runtime.ts: decision logic for the bundled desktop runtime.
// This module finds payloads, decides marker-tag invalidation, and decides
// silent adoption for pristine legacy checkouts. Marker-tag invalidation
// tells us when an app update forces offline re-materialization.
//
// Design: .hermes/plans/2026-08-05_desktop-bundled-payloads-channels-eject.md
// (§1.4 adoption, §4.3 bundled update flow).
//
// All functions in this file are pure, and the callers inject the
// dependencies. Thus vitest covers the whole decision surface. The impure
// executors live in main.ts and bootstrap-runner.

import fs from 'node:fs'
import path from 'node:path'

// ─── payload discovery ──────────────────────────────────────────────────────

export interface PayloadInfo {
  dir: string
  tag: string | null
  items: Record<string, { status: string }>
}

/**
 * Resolve the agent-payload directory that ships in the resources of the
 * packaged app. Returns null for thin builds (a stub manifest with
 * thin:true), for dev runs (no resourcesPath), and for unreadable manifests.
 * Every caller treats null as "behave exactly like the current network
 * bootstrap".
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

/** Build the extra installer arguments for a payload-backed bootstrap. */
export function payloadArgs(installerKind: 'posix' | 'powershell', payload: PayloadInfo | null): string[] {
  if (!payload) {
    return []
  }

  return installerKind === 'posix' ? ['--payload-dir', payload.dir] : ['-PayloadDir', payload.dir]
}

// ─── marker-tag invalidation ────────────────────────────────────────────────

/**
 * Decide if the completed-bootstrap marker is stale because the app updated
 * to a build that carries NEWER payloads.
 *
 * Returns true only when all of these conditions are true:
 * - This build ships payloads (stamp.payload) with a real tag.
 * - The install manifest of the checkout says installMode:bundled. That
 *   means the checkout opted into desktop-managed materialization.
 * - The pinnedTag of the marker differs from the stamp tag.
 *
 * Returns false, deliberately, for all other inputs:
 * - No install manifest (a legacy checkout). ONLY the adoption flow, with
 *   its pristineness gates, can move a legacy checkout. Re-materialization
 *   here is silent adoption without consent checks.
 * - installMode:source (an ejected or user-managed checkout). The user owns
 *   the updates.
 * - Thin builds, and a missing marker. The normal bootstrap-needed logic
 *   owns those cases.
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
  headIsAncestorOfTag: boolean | null // null = unknown (offline, or the fetch failed)
  // From update-state:
  recentManualUpdateDays: number | null // null = never fetched, or unknown
}

export type AdoptionDecision =
  | { adopt: true }
  | { adopt: false; reason: string }

export const RECENT_MANUAL_UPDATE_WINDOW_DAYS = 30

/**
 * Decide if this launch adopts the checkout into the bundled path silently.
 *
 * The bias is: when unsure, do not adopt. Every ambiguous or unverifiable
 * input returns adopt:false with a reason. The reason is logged and never
 * shown to the user. A refused adoption is silent, and a later launch or
 * release tries again.
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
    // source install. install.sh writes this manifest when it has no
    // payloads. Legacy checkouts have no manifest at all. Per plan §1.4,
    // both are adoptable only when manageStyle is absent. installMode:source
    // alone does not opt out. The remaining pristine checks decide.
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
 * Build the manifest to write after a successful adoption. The manifest
 * keeps auto-adopted distinct from adopted. Thus we can bulk-revert a bad
 * auto-adoption cohort without a change for users who chose bundled
 * explicitly.
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
 * Gather the git-side AdoptionFacts for a checkout. Only the ancestry probe
 * touches the network. That probe is a tag-scoped fetch, with --unshallow
 * first when the checkout is a depth-1 installer clone. Every failure
 * degrades to the "do not adopt" side of the fact, for example null
 * ancestry or a dirty tree.
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

  // -uno: untracked files do not block adoption. This mirrors the dirty
  // probe of write-build-stamp. The lockfile-churn tolerance of install.sh
  // is upstream of us. npm churn shows as tracked modifications, and those
  // DO block adoption. This is conservative.
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

      // merge-base --is-ancestor: 0 = yes, 1 = no, other codes = error.
      headIsAncestorOfTag = ancestor.code === 0 ? true : ancestor.code === 1 ? false : null
    }
  }

  return { gitCheckoutExists: true, workingTreeClean, currentBranch, headIsAncestorOfTag }
}

/**
 * Execute an adoption that the decision already approved: fast-forward main
 * to the release tag. Returns true on success. On any failure, the caller
 * leaves the checkout in source mode. The reflog keeps the previous state,
 * and checkout -B is atomic per ref, so no partial adoption state exists.
 * The caller then re-runs the bootstrap, so the venv and js re-materialize
 * from payloads. Then the caller writes adoptionManifest().
 */
export function executeAdoptionCheckout(activeRoot: string, tag: string, git: GitRunner): boolean {
  return git(['checkout', '-B', 'main', `${tag}^{commit}`], activeRoot).code === 0
}

// ─── update channel ─────────────────────────────────────────────────────────

/**
 * The update channel of a checkout. Mirrors the resolution in
 * hermes_cli/install_manifest.py: a bundled install is always stable, a
 * source manifest carries its own channel, and a missing or unreadable
 * manifest means main. The channel decides what the version pill compares
 * against. The install mode decides only the apply mechanism.
 */
export function resolveChannel(
  manifest: { installMode?: string; channel?: string } | null | undefined
): 'stable' | 'main' {
  if (manifest?.installMode === 'bundled') {
    return 'stable'
  }

  return manifest?.channel === 'stable' ? 'stable' : 'main'
}

/**
 * Pick the newest final release tag (vX.Y.Z, no prerelease suffix) from
 * `git ls-remote --tags` output. Numeric ordering, so v0.10.0 > v0.9.0.
 * Returns null when the output has no final release tag.
 *
 * A peeled entry (`refs/tags/v1.2.3^{}`) resolves the commit that an
 * annotated tag points at. It wins over the unpeeled line of the same tag.
 */
export function latestReleaseFromLsRemote(output: string): { tag: string; sha: string } | null {
  const versions = new Map<string, { key: [number, number, number]; sha: string; peeled: boolean }>()

  for (const line of output.split('\n')) {
    // The major component is capped at three digits: the historical CalVer
    // tags (v2026.7.20) would win every numeric sort. This mirrors
    // _RELEASE_TAG_RE in hermes_cli/update_cmd.py and _SEMVER_TAG_RE in
    // scripts/write_install_stamp.py.
    const m = line.match(/^([0-9a-f]{40})\trefs\/tags\/(v(?:0|[1-9]\d{0,2})\.\d+\.\d+)(\^\{\})?$/)

    if (!m) {
      continue
    }

    const [, sha, tag, peel] = m
    const existing = versions.get(tag)

    if (!existing || (peel && !existing.peeled)) {
      const [major, minor, patch] = tag.slice(1).split('.').map(Number)

      versions.set(tag, { key: [major, minor, patch], sha, peeled: Boolean(peel) })
    }
  }

  let best: { tag: string; sha: string; key: [number, number, number] } | null = null

  for (const [tag, { key, sha }] of versions) {
    const newer =
      !best ||
      key[0] > best.key[0] ||
      (key[0] === best.key[0] && (key[1] > best.key[1] || (key[1] === best.key[1] && key[2] > best.key[2])))

    if (newer) {
      best = { tag, sha, key }
    }
  }

  return best ? { tag: best.tag, sha: best.sha } : null
}
