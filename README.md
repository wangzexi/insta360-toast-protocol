# Insta360 Toast Protocol

Unofficial documentation and Python CLI for local photo delivery to the Insta360 Toast e-paper display. No Insta360 account is needed for the tested local access path. This project is not affiliated with Insta360.

## How Toast gets and displays photos

**Local delivery without Internet.** Long-press the function button until the blue LED flashes. The app connects over BLE and asks Toast to open a temporary Wi-Fi hotspot. The phone joins that hotspot and sends converted photos over a binary TCP connection. Uploading a file, committing its place in the playlist, and selecting which photo to display are separate operations.

**Offline playback.** After delivery, Toast can display and cycle through its locally stored photos without the phone, router or cloud. The playlist determines which photos are available and their order; playback settings control the interval and whether to pin the current photo. This is offline playback of photos already on the device, not a cloud sync.

**Remote delivery over the cloud.** The official app binds Toast to an account and gives it credentials for a home Wi-Fi network. Here **Toast joins the router**, rather than the phone joining Toast. The app submits a remote update; Toast can then fetch it over Wi-Fi without the phone staying connected. We verified that a short press fetched a new photo and refreshed the screen after the phone app was closed. The app also showed a next scheduled update time, but we did not observe an unattended update or establish its polling interval.

These workflows explain why the protocol has separate commands for Bluetooth setup, image transfer, playlist changes and display selection. This CLI implements the local path; it does not replicate official cloud sign-in or home Wi-Fi provisioning. See [protocol details](PROTOCOL.md) for observed commands and open questions.

## Quick start

Use macOS with Bluetooth access, Python 3, and `uv`. For TCP operations without changing the Mac's Wi-Fi, connect a USB-debugging-enabled Android phone with `adb` and Toybox `nc`. The phone temporarily joins the Toast hotspot. Use `adb devices` to identify the phone. Long-press the Toast function button until the blue LED flashes when the CLI starts scanning.

```sh
TOAST='Insta360 Toast EXAMPLE'
SERIAL='YOUR_ADB_SERIAL'
uv run --with bleak python insta360-toast-probe.py "$TOAST" --battery
uv run --with bleak python insta360-toast-probe.py "$TOAST" --android-serial "$SERIAL" --list
uv run --with bleak --with pillow python insta360-toast-probe.py "$TOAST" --android-serial "$SERIAL" --image photo.png another.jpg
```

Replace the example name with the exact BLE advertised name. Increase `--scan-timeout` if needed. Use only trusted computers for USB debugging. Hotspot credentials are not printed, but the Android Wi-Fi shell command can appear in system diagnostics.

Pass TCP operations after `--tcp`: `--show ID`, `--reorder ID ...` (every existing ID), `--delete ID ... --yes` (permanent), or `--playback HOURS MODE`. Use the IDs returned by `--list`. An already-connected host can run `insta360-toast-upload.py` directly against the device address and port.

## Verification and scope

- Real device: BLE battery, hotspot setup and TCP connectivity. Battery, hotspot and list reads also worked with the device account-bound and the official app closed.
- Real device: PNG upload, playlist commit, and display selection; the uploaded pattern was visually confirmed on the physical screen. Batch upload, selection of every batch item, reorder, single deletion and batch deletion were confirmed through device readback.
- All six playback intervals and four modes returned success from the device. Automatic timing and persisted settings readback were not independently verified.
- `--clear-wifi --confirm-clear-wifi` is experimental: the command was observed with the official app, not independently verified with the CLI. Clearing home Wi-Fi interrupts remote delivery. Reconfigure with the official app.
- No mobile app, independent cloud sign-in, home Wi-Fi provisioning, or firmware update implementation. See [protocol notes](PROTOCOL.md) for evidence and limitations.

No proprietary packages, raw captures, personal device identifiers, or credentials are distributed.
