# Capturing the Strava mobile API (bearer token + leaderboard routes)

Goal: get a logged-in Strava **mobile** session through an HTTPS proxy so we can see
what `api.strava.com` actually serves — specifically the OAuth **Bearer** token and
the segment **leaderboard / local-legend** routes the public developer API strips out.

Strava **pins its TLS certificate**, so a plain mitmproxy CA in the trust store is not
enough — the app rejects the proxy cert at the handshake and "nothing loads." We defeat
the pinning at runtime with **Frida**, on a device we control.

> Scope: this is for **your own device, your own account**, to understand the API behind
> a project you already pull data for. It impersonates Strava's first-party client; the
> token rotates, so treat any capture as short-lived. Don't redistribute tokens or hammer
> the mobile endpoints — they have their own limits.

Two paths below. **Path A (old physical phone, rooted)** is what you have hardware for.
**Path B (emulator)** is the fallback if rooting the old phone fights back.

---

## What you need on the laptop (both paths)

```bash
# macOS
brew install mitmproxy android-platform-tools     # mitmproxy + adb/fastboot
pipx install frida-tools objection                # or: pip install --user frida-tools objection
```

- `mitmdump --version` → confirms mitmproxy.
- `adb version` → confirms platform-tools.
- `frida --version` and `objection version` → confirm the instrumentation stack.

Start the proxy once and leave it running while you work:

```bash
mitmweb --listen-port 8080      # web UI at http://127.0.0.1:8080, flows visible live
# (or `mitmproxy` for the terminal UI, or `mitmdump -w strava.flows` to just record)
```

Note your laptop's LAN IP (the phone will point at it):

```bash
ipconfig getifaddr en0          # e.g. 192.168.1.50
```

---

## Path A — old physical Android phone (rooted)

### A1. Enable developer mode + USB debugging
1. Settings → About phone → tap **Build number** 7 times.
2. Settings → Developer options → enable **USB debugging**.
3. Plug into the laptop, `adb devices`, accept the RSA prompt on the phone. You should
   see the device listed as `device` (not `unauthorized`).

### A2. Root it
Rooting is model-specific — there is no one command. The mainstream 2024-era route is
**Magisk**:
1. Find your exact model + Android version, and a guide for **unlocking the bootloader**
   (`fastboot oem unlock` / `fastboot flashing unlock` — **this wipes the device**).
2. Pull the stock **boot image** for your build, patch it in the **Magisk** app, and
   `fastboot flash boot magisk_patched.img`.
3. Boot, open Magisk, confirm it shows "installed."

> If the bootloader **can't** be unlocked (some carrier/OEM locked phones), stop here and
> use **Path B (emulator)** instead — you can't get a system cert or frida-server on it
> reliably without root.

Verify root:
```bash
adb shell su -c id        # should print uid=0(root)
```

### A3. Install the mitmproxy CA as a *system* cert
User certs alone won't satisfy pinning bypass cleanly; install it system-wide (root makes
this possible). mitmproxy generates its CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` the
first time it runs.

```bash
# Android wants the cert named by the hash of its subject, with a .0 suffix.
HASH=$(openssl x509 -inform PEM -subject_hash_old -in ~/.mitmproxy/mitmproxy-ca-cert.pem | head -1)
cp ~/.mitmproxy/mitmproxy-ca-cert.pem "${HASH}.0"

adb root && adb remount                 # needs root; remounts /system writable
adb push "${HASH}.0" /system/etc/security/cacerts/
adb shell chmod 644 "/system/etc/security/cacerts/${HASH}.0"
adb reboot
```

On Android 10+ where `/system` is hard to remount, the alternative is a Magisk module that
mounts certs into the system store (search "Magisk **MagiskTrustUserCerts** / move-certs"
module) — install the module, drop the cert as a user cert (Settings → Security → Install
from storage), reboot, and the module promotes it to the system store.

### A4. Point the phone's Wi-Fi at the proxy
On the phone: Wi-Fi → your network → **Modify** → Advanced → Proxy = **Manual**:
- Host: your laptop LAN IP (from `ipconfig getifaddr en0`)
- Port: `8080`

Sanity check: open the phone browser to `http://mitm.it` — it should load the mitmproxy
cert page (proves traffic is flowing through the proxy). Plain-HTTPS sites should now work
in the browser. Strava will still fail until we kill pinning.

### A5. Run frida-server on the phone
1. Check the phone's CPU arch: `adb shell getprop ro.product.cpu.abi`
   (`arm64-v8a` on basically any modern phone).
2. Download the matching **`frida-server-<ver>-android-arm64`** from Frida's GitHub
   releases (version must match your laptop `frida --version`).
3. Push and run:
```bash
adb push frida-server-*-android-arm64 /data/local/tmp/frida-server
adb shell chmod 755 /data/local/tmp/frida-server
adb shell su -c '/data/local/tmp/frida-server &'
```
4. From the laptop, confirm it's reachable:
```bash
frida-ps -U | grep -i strava        # lists processes on the USB device
```

### A6. Defeat the pinning and capture
Find the package name if unsure (`adb shell pm list packages | grep -i strava` →
`com.strava`).

**Easiest — objection:**
```bash
objection -g com.strava explore
# at the prompt:
android sslpinning disable
```

**More robust — a universal Frida unpinning script** (hooks OkHttp `CertificatePinner`,
Conscrypt/TrustManager, *and* native BoringSSL — Strava pins below the Java layer, so the
native hook matters):
```bash
# grab a well-maintained universal script, e.g. "frida-multiple-unpinning"
frida -U -f com.strava -l frida-multiple-unpinning.js
```

With pinning bypassed, **use the app**: open a starred segment, scroll its leaderboard,
view a local legend. Watch the flows in the mitmweb UI.

### A7. Pull out what we came for
In the mitmweb UI, filter to `api.strava.com`. On any request, inspect the **Request**
headers for:

```
Authorization: Bearer <long token>      <-- the first-party mobile token
```

and note the **route shapes** that return leaderboard data (these are what the public API
doesn't expose). Likely candidates to look for:
- `GET /api/v3/segments/{id}` (richer than public — includes leaderboard/effort blocks)
- `GET /api/v3/segments/{id}/leaderboard?...`
- `GET /api/v3/segments/{id}/local_legend`
- `POST /graphql` (some leaderboard/feed data is GraphQL — capture the query body)

Save the flows for later reference:
```bash
# if you used mitmdump -w earlier you already have strava.flows; otherwise
# File → Save in mitmweb, or re-run with `mitmdump -w strava.flows`.
```

Then test the token straight from the laptop (no phone in the loop):
```bash
curl -H "Authorization: Bearer <token>" \
     -H "User-Agent: Strava/<ver> (Android)" \
     "https://api.strava.com/api/v3/segments/<id>/leaderboard"
```
If that returns leaderboard JSON, the mobile path is confirmed and we can decide whether
it's worth wiring into the project.

---

## Path B — Android emulator (no physical phone needed)

The emulator is often *less* painful than rooting an old phone, with one wrinkle: you need
a **rootable** image.

### B1. Create a rootable AVD
- In Android Studio's Device Manager (or `sdkmanager`/`avdmanager` CLI), create a device
  using a **"Google APIs"** system image — **NOT "Google Play."** Play images are
  production-signed and **not rootable** (`adb root` is blocked). Google APIs images allow
  `adb root`.
- A recent-ish API level (e.g. API 30–33), **x86_64** for speed on Intel Macs; on Apple
  Silicon use an **arm64** image (slower but native).

### B2. Root-equivalent + system cert
```bash
adb root && adb remount
# then the same system-cert install as A3:
HASH=$(openssl x509 -inform PEM -subject_hash_old -in ~/.mitmproxy/mitmproxy-ca-cert.pem | head -1)
cp ~/.mitmproxy/mitmproxy-ca-cert.pem "${HASH}.0"
adb push "${HASH}.0" /system/etc/security/cacerts/
adb shell chmod 644 "/system/etc/security/cacerts/${HASH}.0"
adb reboot
```
Google APIs images give you `adb root` directly, so no Magisk needed for the cert.

### B3. Proxy the emulator
Launch with the proxy baked in (mitmproxy on the host):
```bash
emulator -avd <name> -http-proxy http://127.0.0.1:8080 -writable-system
```
(`-writable-system` keeps the system partition writable across boot so the cert sticks.)

### B4. Install Strava + frida-server + capture
- Install the app: download the **APK** (e.g. from APKMirror) and `adb install strava.apk`
  — Google APIs images have no Play Store. Match the arch to your image (x86_64 vs arm64).
- frida-server: push the **emulator's** arch build (`adb shell getprop ro.product.cpu.abi`
  → usually `x86_64` on Intel, `arm64-v8a` on Apple Silicon). Same run steps as **A5**.
- Bypass pinning + capture: identical to **A6 / A7**.

> Caveat: some app builds **detect the emulator** (and Frida/root) and refuse to run or
> log in. If Strava bails on the emulator, fall back to the rooted physical phone (Path A).

---

## Known walls (don't be surprised)

- **Native pinning.** Strava pins in BoringSSL, below the Java/OkHttp layer. Config-only
  tricks (`apk-mitm`, network-security-config edits) often **don't** work — that's why we
  use Frida's native hook, not just the Java one.
- **Frida / root / emulator detection.** Some builds probe for `frida-server` ports,
  `su`, or emulator props and quietly refuse. If the universal script doesn't immediately
  work, this is the next layer — version-dependent, an arms race. "Gadget injection"
  (repackaging the APK with the Frida gadget via `objection patchapk`) sidesteps a
  running-server check.
- **App attestation.** Newer builds may add Play Integrity / attestation. Capturing a
  token still works (you're a real logged-in session); *replaying* it long-term from curl
  is where attestation can bite. Tokens rotate regardless — plan to re-capture.

---

## Is it worth it? (project context)

The **web** path (`strava.py`, `_strava4_session` cookie) already returns the leaderboard
data this project needs and is only throttled when we burst — the standing decision is to
**pivot to the mobile API only if the web path becomes unusable**. Treat this doc as the
runbook for *if/when* that day comes, or as a one-time recon to confirm the mobile
leaderboard/local-legend route shapes. Don't wire the bearer token into the pipeline
unless the web 429s become a daily blocker — the token's short life makes it higher-
maintenance than the cookie.

See also: `reference_strava_internal_api.md` in the Claude memory dir for the web-vs-mobile
host/auth split and the rate-limit findings.
