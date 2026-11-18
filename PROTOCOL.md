# Toast protocol notes

The format below is based on observed traffic and independent tests, not a vendor specification.

## Transport

The phone first connects to BLE GATT service `CE80` (Bluetooth base UUID); write requests to `CE81`, receive notifications on `CE82`. With the device awake and advertising, command `0x1001` requests the temporary access point. Keep BLE alive while the Wi-Fi client associates. The observed access point used 2.4 GHz, channel 6. Connect to the returned address and port (observed: `192.168.4.1:8899`). This is binary TCP, not HTTP.

Each frame starts with `FE EF 01`, then little-endian `u16 command, u16 flags, u16 body_length`. Ordinary request bodies start with a little-endian `u16 sequence`; replies echo it followed by a one-byte status (zero means success), then optional command data. TCP receives must handle partial reads. Playlist replies can span frames; file-data and keepalive frames have distinct conventions, so do not apply the ordinary reply layout to every frame.

| Command | Observed operation |
| --- | --- |
| `0x1028` | Battery query, no extra request data; first result byte is percentage |
| `0x1001` | BLE hotspot request, no extra request data; result is a zero byte then fixed NUL-padded SSID (32 B), password (32 B), gateway (16 B), port (little-endian u16) |
| `0x100A` | Read ordered playlist; one-byte count followed by 32-byte ASCII hex IDs |
| `0x100B` / `0x100D` | Stage playlist using the same count/IDs encoding, then commit |
| `0x101D` / `0x101C` | Set current image by ASCII ID / read current image; readback can lag |
| `0x1009` | Delete IDs: little-endian u32 count, then concatenated 32-byte ASCII IDs |
| `0x100F` | Write playback interval and mode |
| `0x1019` | Observed keepalive; the TCP client skips unsolicited keepalive frames |

## Upload

1. Read the existing playlist. Send `0x1003` with the batch count (u16).
2. For each image send `0x1007` with its 32-byte ASCII hex ID and two u64 timestamps, then `0x102A` with the image descriptor (see encoder for observed layout).
3. Send `0x0020` with file type byte 2, u32 transfer ID, u32 compressed size, u16 file CRC, u16 value 50 as observed, and u32 chunk count. Send `0x0021` frames with a frame counter in the flags field. Each body is u32 zero-based index, u16 data length, u16 data CRC, then up to 4,079 bytes. The observed final ACK body has u32 last index and u32 zero.
4. Send `0x0022` with file type and transfer ID; after the batch send `0x1027`. Read the list, merge without dropping original IDs, stage and commit. Read back the order, then select and read back the desired photo.

Images are 720 × 480, packed two 4-bit palette indices per byte (high nibble first in the CLI), zlib-compressed before transfer. Observed codes are 0, 1, 2, 3, 5, 6. The CLI approximates the six display colors with a fixed palette. Both checksums use CRC-16/IBM-SDLC; the file CRC covers the compressed file. All multibyte fields listed above are little-endian.

## Playback

The nine bytes after the sequence in `0x100F` are `01 02 00 [interval minutes: u16] 02 01 00 [mode: u8]`. Observed intervals: 1, 4, 8, 12, 24, 72 hours. Observed modes: 4 upload date, 5 manual, 6 random, 7 pin current. The official app also submits a playlist when changing sort order; writing a mode alone is not equivalent to reordering it immediately.

## Home Wi-Fi and cloud

On firmware 0.2.187, the official app used BLE `0x1020` to submit home-network configuration and `0x1021` in two frames to finish provisioning. An incorrect Wi-Fi password failed at the first stage. Identical second-stage data could fail and then succeed on retry; the reason was not established. Switching between two networks changed the first stage but not the second.

The second stage contained a JWT whose header declared ES256 with a key ID. It had audience, issue and expiry times, device serial and user ID claims. This is consistent with a server-issued device/account authorization credential, but signature, issuer, acquisition flow and renewal were not verified. The evidence does not establish that implementing the entire official login flow is required; there is no verified independent provisioning flow. This project does not obtain or replay captured credentials.

Two official app delete actions matched `0x101A` with a sequence-only body and a successful reply. The official app's account-binding rejection does not imply link-layer BLE rejection: local read-only CLI operations worked on the bound device without app login. Bound-device write operations were not separately verified.

After remote delivery was queued and the official phone app was terminated, a short button press on Toast caused the new photo to appear on the physical display. This demonstrates that the phone app need not stay running. A scheduled update time was shown in the app, but the actual unattended schedule and device-side cloud request details were not measured.
