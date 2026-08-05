// app-updater.ts — electron-updater integration for bundled desktop installs.
//
// Bundled installs update through GitHub Releases: electron-updater reads
// latest*.yml from the release that the desktop-bundled-release workflow
// attached, downloads the new installer, and applies it. After the relaunch,
// the bootstrap compares the marker tag with the new stamp tag. On a
// mismatch, it rebuilds the agent runtime offline from the new payloads
// (see bundled-runtime.needsRematerialization).
//
// Source installs never reach this module. The callers gate on the install
// manifest first and fall through to the git-based update path.
//
// The decision helpers are pure so vitest covers them. The impure wrapper
// at the bottom lazy-loads electron-updater, because the module must not
// cost anything on thin builds.

export interface UpdaterGateFacts {
  stampHasPayload: boolean
  installMode: string | null // from .hermes-install.json; null = no manifest
  isPackaged: boolean
}

/**
 * True when this launch must use electron-updater for app updates.
 *
 * All three conditions are necessary:
 * - the build carries payloads (a thin build has no matching feed artifacts),
 * - the checkout opted into desktop management (installMode bundled) — an
 *   ejected or source checkout keeps the git update path,
 * - the app is packaged (dev runs have no app-update.yml).
 */
export function shouldUseAppUpdater(facts: UpdaterGateFacts): boolean {
  return facts.stampHasPayload === true && facts.installMode === 'bundled' && facts.isPackaged === true
}

/**
 * Map an electron-updater check result to the renderer's update-check shape
 * (the shape hermes:updates:check already returns for the git path). The
 * renderer then needs no new states: `updateAvailable` plus `mechanism`
 * drive the existing UI.
 */
export function describeFeedCheck(
  current: string,
  info: { version?: string } | null | undefined
): {
  supported: true
  mechanism: 'app-updater'
  currentVersion: string
  latestVersion: string | null
  updateAvailable: boolean
  fetchedAt: number
} {
  const latest = info && typeof info.version === 'string' ? info.version : null

  return {
    supported: true,
    mechanism: 'app-updater',
    currentVersion: current,
    latestVersion: latest,
    updateAvailable: latest !== null && latest !== current,
    fetchedAt: Date.now()
  }
}

// ── impure wrapper ──────────────────────────────────────────────────────────

let cachedUpdater: any = null

/**
 * Lazy singleton for electron-updater's autoUpdater. The require sits inside
 * the function so thin builds and tests never pay for the module load.
 * autoDownload stays off: the renderer asks the user before the download
 * starts (same consent model as the git path).
 */
export function getAutoUpdater(): any {
  if (cachedUpdater) {
    return cachedUpdater
  }

   
  const { autoUpdater } = require('electron-updater')

  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = true
  cachedUpdater = autoUpdater

  return autoUpdater
}

/** Check the GitHub Releases feed. Returns the renderer-shaped result. */
export async function checkAppUpdate(currentVersion: string): Promise<ReturnType<typeof describeFeedCheck>> {
  const updater = getAutoUpdater()
  const result = await updater.checkForUpdates()

  return describeFeedCheck(currentVersion, result?.updateInfo)
}

/**
 * Download the update, then quit and install. `onProgress` receives percent
 * values from electron-updater's download events. The returned promise
 * resolves after the download; quitAndInstall exits the process.
 */
export async function applyAppUpdate(onProgress?: (percent: number) => void): Promise<{ ok: true }> {
  const updater = getAutoUpdater()

  if (onProgress) {
    updater.on('download-progress', (p: { percent: number }) => onProgress(p.percent))
  }

  await updater.downloadUpdate()
  updater.quitAndInstall()

  return { ok: true }
}
