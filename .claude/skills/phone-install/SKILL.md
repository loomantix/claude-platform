---
name: phone-install
description: Build a release APK from the consumer repo and install it on a tethered Android device over wireless ADB. Saves the ~8 commands otherwise needed for a device test.
argument-hint: <adb-port> [--staging | --apk <path> | --no-launch | --no-pull]
---

# /phone-install — release-build + wireless-install on Android

You are building a release-signed Android APK from the **current consumer repo** and installing it on the developer's phone over wireless ADB. The flow is generic: any product whose mobile app exposes `just mobile-build-apk` (or accepts a `--apk` override) can use this skill.

The skill is the answer to "I want this branch on my phone right now" — typical run is ~5–10 minutes end-to-end with the cache warm.

## Arguments

First positional arg — the ADB **connect** port from the phone's Wireless debugging screen. **Always ask the developer for this if not provided; the connect port rotates every time Wireless debugging is toggled.** Never assume a previously-used port.

Flags (any order, after the port):

- `--staging` — build against the staging API + test Clerk key (or whatever staging env vars the consumer's `mobile-build-apk` recipe expects). Default is **prod**.
- `--apk <path>` — install an existing APK instead of building. Skips Phase 3 (pull) and Phase 4 (build).
- `--no-launch` — don't `am start` the app after install.
- `--no-pull` — skip the `git pull --ff-only origin main` step (use current working-tree state).

## Required environment

The skill reads these once at start; surface anything missing as an actionable error rather than silently defaulting:

| Variable         | Purpose                                                                                                         | Default                                    |
| ---------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `PHONE_IP`       | LAN IP of the device. Set once per developer machine (e.g. in `~/.bashrc`).                                     | _required_ — fail fast if unset            |
| `ANDROID_HOME`   | Path to Android SDK. The consumer's `just mobile-build-apk` recipe normally exports this; respect it if set.    | recipe-dependent                           |
| `APK_OUTPUT_DIR` | Where to drop the built APK. The consumer's recipe should honor this; if not, the recipe's hardcoded path wins. | recipe-dependent (e.g. `~/builds`)         |
| Build secrets    | Whatever the consumer's `just mobile-build-apk` requires (e.g. `CLERK_PUBLISHABLE_KEY`, API URL overrides).     | consumer-specific — see consumer CLAUDE.md |

If `PHONE_IP` is unset, ask the developer for it and suggest adding `export PHONE_IP=…` to their shell rc so they don't have to repeat themselves.

## Prerequisites

- Wireless debugging ON on the phone (Settings → Developer options → Wireless debugging).
- `adb` in PATH (Linux / macOS / WSL2 all work — the flow is OS-agnostic once the SDK is reachable).
- Consumer repo defines `just mobile-build-apk` (or the developer is invoking with `--apk <path>`).
- Consumer repo defines `apps/mobile/app.config.ts` exposing both `version` and `android.package` (Expo convention). The skill derives both at runtime.

## Flow

### 1. Confirm pre-state

- Confirm cwd is the consumer repo root (presence of `apps/mobile/app.config.ts` is a sufficient signal).
- `git status --short` — note any unstaged changes; flag if surprising, but don't block. The release build comes from the working tree as-is, so committed + uncommitted edits both ship.
- `git rev-parse --abbrev-ref HEAD` — capture the branch; you'll need it for the report and for the "skip pull on feature branch" rule.

### 2. Connect ADB

```bash
adb connect "$PHONE_IP:<port>"
adb devices
```

Expected: one line showing `<PHONE_IP>:<port>   device`. If it shows `offline` or you get `No route to host`:

- Phone may be asleep → ask developer to wake.
- Connect port may have rotated → ask for a fresh one (it changes every Wireless-debugging toggle).
- Wireless debugging may be off → ask developer to re-enable.
- Phone and host may be on different LAN segments → ask developer to verify Wi-Fi network.

Never guess a new port. The pair port (used once during initial pairing) is also separate from the connect port; don't conflate them.

### 3. Pull latest main (unless `--no-pull` or on a non-main branch)

```bash
git fetch origin main
git log HEAD..origin/main --oneline   # show what's new (if anything)
git pull --ff-only origin main        # only if HEAD is behind
```

If on a feature branch, **skip the pull** and flag: "On branch `<X>` — not pulling main. Pass `--no-pull` to silence this." If `--apk` was given, skip this phase entirely.

### 4. Build (unless `--apk` given)

The consumer repo owns the build recipe. Default invocation:

```bash
just mobile-build-apk
```

For `--staging`: the consumer's recipe must accept the staging variant via env vars. Reference the consumer's `CLAUDE.md` or `justfile` for the exact set; common patterns are an API URL override and a different Clerk publishable key. If the staging variant isn't documented, ask the developer.

**Run the build in background** (`run_in_background: true`) and tell the developer the time estimate up front: ~8–12 min cold, ~4–6 min with the Gradle cache warm.

After the build completes, derive the APK path:

```bash
VERSION=$(grep -oP "version:\s*'\K[^']+" apps/mobile/app.config.ts)
GIT_SHA=$(git rev-parse --short HEAD)
APP_NAME=$(grep -oP "name:\s*'\K[^']+" apps/mobile/app.config.ts | head -1)
# Recipe-honored APK_OUTPUT_DIR wins; otherwise grep the build's "✓ APK: …" line from build output.
APK="${APK_OUTPUT_DIR:?}/${VERSION}/${APP_NAME}-${VERSION}-${GIT_SHA}.apk"
```

If the recipe doesn't follow the `${APP_NAME}-${VERSION}-${GIT_SHA}.apk` convention, parse the path from the build's stdout (most recipes echo `✓ APK: <path>` on success). Fail loudly if the APK is missing rather than guessing.

### 5. Install

Derive the package name from the consumer repo (don't hardcode):

```bash
APP_PACKAGE=$(grep -oP "package:\s*'\K[^']+" apps/mobile/app.config.ts | head -1)
adb -s "$PHONE_IP:<port>" install -r "$APK"
```

Common failures:

- `device '...' not found` → ADB dropped during long build. Reconnect (developer may need to wake phone + supply fresh port).
- `INSTALL_FAILED_UPDATE_INCOMPATIBLE` → signature mismatch (e.g. switching between dev-signed and release-signed builds). **Ask the developer before** running `adb uninstall "$APP_PACKAGE"`, because uninstall wipes app data including any unsubmitted entries.
- `INSTALL_FAILED_VERSION_DOWNGRADE` → use `adb install -d -r "$APK"` to allow downgrade.
- `INSTALL_FAILED_INSUFFICIENT_STORAGE` → device storage full; not something this skill should auto-clean.

### 6. Launch (unless `--no-launch`)

```bash
adb -s "$PHONE_IP:<port>" shell am start -n "$APP_PACKAGE/.MainActivity"
```

Expo apps on RN ≥ 0.78 use `.MainActivity`; older Expo bare workflows used `.MainApplication$MainActivity`. If `am start` fails with `Activity class … does not exist`, fall back to `monkey -p "$APP_PACKAGE" -c android.intent.category.LAUNCHER 1` which doesn't require knowing the activity name.

### 7. Report

Concise summary back to the developer:

- APK path + size
- Version + git SHA + branch
- Install status (success / what failed)
- Launch status
- Any notable build warnings (Gradle deprecations, deprecated plugins) — summarize, don't dump full logs

## Hard rules

- **Never commit secret values to skill docs, memory, or logs.** Read build secrets (Clerk keys, API tokens) from the consumer's env files at invocation time only. Public test/sandbox keys committed in the consumer's repo are OK to reference literally.
- **Never push to remote during this flow.** Build-and-install only; local state only.
- **Never modify `apps/mobile/android/`** manually — Expo regenerates it on every `expo prebuild --clean`. If the consumer's recipe pins Gradle (e.g. RN 0.83 has a 9.0 ceiling), respect that pin; don't bump it as a side-effect of a phone-install run.
- **Never uninstall the app without asking.** Uninstall wipes local data; for products with offline / unsubmitted state, that data may be unrecoverable.
- **Never silently fall back to a different build profile.** If `--staging` is passed and the consumer recipe doesn't support it, fail loudly with the recipe location for the developer to inspect.

## When NOT to use this skill

- Building an AAB for Play Store submission → use `eas build --profile production --platform android` (or the consumer's local AAB recipe). This skill produces an APK only.
- Testing on a physical iPhone → out of scope; iOS device installs go through Xcode or TestFlight.
- Running the consumer's mobile e2e suite (Maestro / Detox) — those are separate recipes (e.g. `just maestro-smoke`) and assume the APK is already installed.
- First-time pairing of a new device → use `adb pair` interactively with the **pair** port (different from connect port), then come back here.

## Notes on consumer integration

This skill is synced from the upstream repo. Consumer repos using it should ensure:

1. `just mobile-build-apk` exists and emits the APK path on stdout.
2. `apps/mobile/app.config.ts` exposes `version`, `name`, and `android.package`.
3. Developer sets `PHONE_IP` in their shell rc.
4. Build env vars required by the recipe are documented in the consumer's CLAUDE.md (so this skill's "ask the developer" fallback has somewhere to point).

If your consumer repo deviates from these conventions, prefer fixing the recipe over forking this skill — the whole point of upstream sync is that one improvement to the flow lands in every consumer on the next sync run.
