---
name: phone-install
description: Build a release APK from the consumer repo and install it on a tethered Android device over wireless ADB. Use for phone sideload smoke tests, including consumer-specific build env overrides documented in the consumer repo. Accepts an ADB port and optional staging, development, production, APK-path, no-launch, or no-pull choices from the user's request.
---

# /phone-install — release-build + wireless-install on Android

You are building a release-signed Android APK from the **current consumer repo** and installing it on the developer's phone over wireless ADB. The flow is generic: any product whose mobile app exposes `just mobile-build-apk` (or accepts a `--apk` override) can use this skill.

The skill is the answer to "I want this branch on my phone right now" — typical run is ~5–10 minutes end-to-end with the cache warm.

## Arguments

First positional arg — the ADB **connect** port from the phone's Wireless debugging screen. **Always ask the developer for this if not provided; the connect port rotates every time Wireless debugging is toggled.** Never assume a previously-used port.

Flags (any order, after the port):

- `--prod` — build against production config. This is the default unless the consumer recipe documents a different default.
- `--staging` — build against staging config by passing the consumer-documented env overrides.
- `--dev` — build against local/dev config by passing the consumer-documented env overrides.
- `--apk <path>` — install an existing APK instead of building. Skips Phase 3 (pull) and Phase 4 (build).
- `--no-launch` — don't `am start` the app after install.
- `--no-pull` — skip the `git pull --ff-only origin main` step (use current working-tree state).

## Required environment

The skill reads these once at start; surface anything missing as an actionable error rather than silently defaulting:

| Variable         | Purpose                                                                                                         | Default                                                 |
| ---------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `PHONE_IP`       | LAN IP of the device. Set once per developer machine (e.g. in `~/.bashrc`).                                     | _required_ — fail fast if unset                         |
| `ANDROID_HOME`   | Path to Android SDK. The consumer's `just mobile-build-apk` recipe normally exports this; respect it if set.    | recipe-dependent                                        |
| `APK_OUTPUT_DIR` | Where to drop the built APK. The consumer's recipe should honor this; if not, the recipe's hardcoded path wins. | recipe-dependent (e.g. `~/builds`)                      |
| Build config     | Whatever the consumer's `just mobile-build-apk` requires (e.g. app env, public client keys, API URL overrides). | consumer-specific — see consumer AGENTS.md or CLAUDE.md |

If `PHONE_IP` is unset, ask the developer for it and suggest adding `export PHONE_IP=…` to their shell rc so they don't have to repeat themselves.

Never ask the developer for server-side secrets for this flow. Mobile sideload builds should use public client config or local env already documented by the consumer repo.

## Prerequisites

- Wireless debugging ON on the phone (Settings → Developer options → Wireless debugging).
- `adb` in PATH (Linux / macOS / WSL2 all work — the flow is OS-agnostic once the SDK is reachable).
- Consumer repo defines `just mobile-build-apk` (or the developer is invoking with `--apk <path>`).
- Consumer repo defines `apps/mobile/app.config.ts` exposing both `version` and `android.package` (Expo convention). The skill derives both at runtime.

## Consumer overrides

This upstream skill must stay public-safe and consumer-agnostic. Consumer-specific app names, package IDs, output paths, environment defaults, public client keys, and local path conventions belong in the consumer repo's `AGENTS.md`, mobile README, or `justfile`, not in this upstream repo.

When a consumer needs product-specific behavior, read its local instructions before building and apply only the documented overrides. Useful override fields:

- default `APP_ENV` or equivalent build profile,
- exact env vars for `--dev`, `--staging`, and `--prod`,
- APK output naming convention if stdout does not include `APK: <path>`,
- package ID or activity notes if Expo defaults do not apply,
- data-loss risks before uninstalling or clearing app storage.

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

For `--dev`, `--staging`, and `--prod`: the consumer's recipe must accept the selected variant via env vars or documented build profiles. Reference the consumer's `AGENTS.md` or `CLAUDE.md`, mobile README, or `justfile` for the exact set. If a requested variant is not documented, ask the developer before guessing.

**Run the build in background** (`run_in_background: true`) and tell the developer the time estimate up front: ~8–12 min cold, ~4–6 min with the Gradle cache warm.

After the build completes, parse the APK path from stdout first. Most consumer recipes echo `APK: <path>` or `✓ APK: <path>` on success.

If stdout does not expose a path, derive it from the consumer's documented convention:

```bash
VERSION=$(grep -oP '"version":\s*"\K[^"]+' apps/mobile/package.json | head -1)
[ -n "$VERSION" ] || VERSION=$(grep -oP "version:\s*'\K[^']+" apps/mobile/app.config.ts | head -1)
GIT_SHA=$(git rev-parse --short HEAD)
APP_NAME=$(grep -oP "name:\s*'\K[^']+" apps/mobile/app.config.ts | head -1)
# Recipe-honored APK_OUTPUT_DIR wins when the consumer documents this convention.
APK="${APK_OUTPUT_DIR:?}/${VERSION}/${APP_NAME}-${VERSION}-${GIT_SHA}.apk"
```

Fail loudly if the APK is missing rather than guessing another path.

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

- **Never commit secret values to skill docs, memory, or logs.** Read build secrets (auth-provider keys, API tokens) from the consumer's env files at invocation time only. Public test/sandbox keys committed in the consumer's repo are OK to reference literally.
- **Never push to remote during this flow.** Build-and-install only; local state only.
- **Never modify `apps/mobile/android/`** manually — Expo regenerates it on every `expo prebuild --clean`. If the consumer's recipe pins Gradle (e.g. RN 0.83 has a 9.0 ceiling), respect that pin; don't bump it as a side-effect of a phone-install run.
- **Never uninstall the app without asking.** Uninstall wipes local data; for products with offline / unsubmitted state, that data may be unrecoverable.
- **Never silently fall back to a different build profile.** If `--dev`, `--staging`, or `--prod` is passed and the consumer recipe doesn't support it, fail loudly with the recipe location for the developer to inspect.
- **Never put private consumer repo names or consumer-specific operational details in this upstream skill.** Keep those details in the consumer repo.

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
4. Build env vars required by the recipe are documented in the consumer's AGENTS.md or CLAUDE.md, mobile README, or `justfile` (so this skill's "ask the developer" fallback has somewhere to point).

If your consumer repo deviates from these conventions, prefer fixing the recipe over forking this skill — the whole point of upstream sync is that one improvement to the flow lands in every consumer on the next sync run.
