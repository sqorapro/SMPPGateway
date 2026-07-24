# SMPP Gateway — USB Modem SMS & Voice Call Platform

*[Русская версия](README.ru.md)*

A Windows desktop application that turns a bank of USB GSM modems (Huawei E173/E3372)
into a full SMPP gateway: a custom SMPP 3.4 server, mass SMS campaigns, and mass
voice-call campaigns with pre-recorded audio playback — built for a taxi-dispatch
software integration handling tens/hundreds of thousands of numbers per campaign
across 8+ modems running in parallel.

## Features

- **SMPP 3.4 server implemented from scratch** (no third-party SMPP library) —
  bind/submit_sm/deliver_sm, UCS-2 (Cyrillic) encoding, multipart SMS reassembly
  via UDH, delivery receipts (DLR)
- **Mass SMS campaigns** — load a number list (CSV/TXT/Excel), round-robin
  dispatch across all connected modems
- **Mass voice-call campaigns** — load numbers + a WAV file, parallel outbound
  calls across all voice-capable modems with live PCM audio playback
- **Reverse SSH tunnel** (Paramiko) — exposes the local SMPP server on a public
  domain without a static IP, for remote SMPP clients behind NAT
- **SQLite logging** — full history of every SMS and call

## Engineering challenges solved

- **Voice calls over a USB modem with no documented voice API.** Each E173 modem
  exposes two separate COM ports (AT command port + voice/PCM port). Getting audio
  into a live call required reverse-engineering the AT command sequence
  (`ATD` → wait `^CONN` → `AT^DDSETEX=2` on the command port → stream raw
  8kHz/mono/16-bit PCM into the voice port on its own thread → `ATH`).
- **Custom SMPP protocol implementation.** Built the SMPP 3.4 wire protocol by
  hand — PDU encoding/decoding, UCS-2 vs GSM-7 handling, multipart concatenation
  via UDH parsing, and the `sc_interface_version` TLV (without it, strict SMPP
  clients unbind without ever sending traffic).
- **O(n²) → O(1) refactor for 100k+ row campaigns.** The original number→row
  lookup scanned the whole table on every status update, causing 20+ minute
  freezes on 100k-number campaigns. Fixed with an indexed `number → QTableWidgetItem`
  map, plus swapping widget-based checkboxes for native checkable items (widget
  checkboxes are catastrophically slow past ~10k rows in Qt).
- **Modem health circuit breaker, tuned to avoid false positives.** A modem that
  hard-fails on dialing (AT-level rejection) gets pulled from the current campaign
  after 5 consecutive failures — but normal call outcomes (no answer, busy, no
  line — all expected at scale) are explicitly excluded from the count, after an
  earlier version wrongly tripped on those and knocked out all 8 modems mid-campaign.
- **COM port handle leaks under a connect timeout.** A timeout guard added to stop
  slow `modem.connect()` calls from hanging the UI could still leave the port open
  if the connection finished just after the timeout fired — silently leaking OS
  handles until every port looked "busy" to the app itself.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

## Configuration

### 1. "Modems" tab
- Click "Scan ports" to discover connected USB modems
- Select a port and click "Connect selected"
- The "Voice" column shows whether the modem supports voice calls (E173 — yes, E3372 — no)

### 2. "SMPP server" tab
- Set host (`0.0.0.0` for all interfaces), port (default `2775`)
- Set System ID and password
- Click "Start SMPP server"
- Point your remote system at this SMPP server (via your own domain/tunnel)

### 3. "Mass calls" tab
- Load a number list (CSV/TXT — one number per line, or Excel)
- Select a WAV audio file
- Set the call timeout
- Click "Start campaign" — calls run in parallel across all voice-capable (E173) modems

### 4. "SMS campaign" tab
- Load a number list
- Enter the message text
- Click "Start campaign" — SMS is sent round-robin across all connected modems

### 5. "Logs" tab
- Browse full SMS/call history from the SQLite database

## Requirements

- Windows 10/11
- Python 3.8+
- Huawei E173 (voice + SMS) and/or E3372 (SMS only) USB modems
- SIM cards with active voice + SMS service

## Project structure

```
SMPPGateway/
├── main.py                  # Entry point
├── requirements.txt
├── src/
│   ├── modem_manager.py     # Modem pool management (AT commands)
│   ├── smpp_server.py       # SMPP 3.4 server
│   ├── call_handler.py      # Mass call campaigns
│   ├── sms_handler.py       # Mass SMS campaigns
│   ├── tunnel.py            # Reverse SSH tunnel (Paramiko)
│   ├── database.py          # SQLite logging
│   └── gui/
│       └── main_window.py   # PyQt5 GUI
└── tools/                   # Standalone diagnostic scripts (modem/port scanning)
```

---

*Built as a freelance project; client-specific configuration (VPS host, domain,
credentials) has been redacted/replaced with placeholders for this portfolio copy.*
