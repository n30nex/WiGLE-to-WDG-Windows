# WiGLE to WDG — Windows

A dependency-free Windows uploader for one conservative workflow:

**single newest WiGLE upload → WDG Wars**

It never pages through WiGLE history, backfills, catches up missed days, or uses an all-time mode. Each run requests exactly `pagestart=0&pageend=1`, downloads only that transaction, and keeps only Wi-Fi/GPS rows whose `FirstSeen` time is within the last 24 hours.

## Safety behaviour

- Queries exactly one WiGLE transaction: the newest upload.
- Never requests older transaction pages.
- Never includes observations older than 24 hours.
- Excludes Bluetooth, cellular, invalid GPS, invalid timestamps, and malformed rows.
- Records the transaction ID only after WDG reports the upload job as successfully completed.
- If the newest transaction ID was already accepted, logs `No new WiGLE upload` and exits successfully without downloading or posting it again.
- A dry run validates both API credentials and parses the newest CSV, but never sends a WDG upload or changes duplicate state.

This program does not perform Wi-Fi scanning. Submit only observations you collected and are authorized to upload.

## Install

1. Download `wigle-to-wdg-windows.zip` from the [latest release](https://github.com/n30nex/WiGLE-to-WDG-Windows/releases/latest) and extract it somewhere permanent, such as `Documents\WiGLE-to-WDG-Windows`.
2. Install Python 3.10 or newer for Windows and enable **Add Python to PATH**.
3. Double-click `setup_config.bat`.
4. Fill in the blank `.env` file, save it, and close Notepad.

Use either WiGLE credential format:

```text
WIGLE_BASIC_TOKEN=your_single_base64_wigle_value
WDG_API_KEY=your_wdg_key
```

or:

```text
WIGLE_API_NAME=your_wigle_api_name
WIGLE_API_TOKEN=your_wigle_api_token
WDG_API_KEY=your_wdg_key
```

The `.env` file is ignored by Git. Credentials are never placed in source code, batch files, task command lines, state files, output CSVs, or logs.

## Verify before uploading

Double-click `run_dry_test.bat`.

The dry run:

1. Validates WiGLE authentication.
2. Requests only the newest transaction.
3. Validates WDG authentication using `/api/me`.
4. Downloads and parses only that newest CSV.
5. Writes a filtered local audit copy.
6. Performs no WDG upload and changes no duplicate state.

After it succeeds, double-click `run_wigle_to_wdg_now.bat` for the first live upload. The script waits for WDG's asynchronous job to reach a successful terminal result before recording the transaction ID.

## Daily unattended run

Create a Windows Task Scheduler **Basic Task** that runs daily, for example at 03:00 local time.

- Program/script: the full path to `run_wigle_to_wdg.bat`
- Start in: the extracted package folder

The task command contains no credential. If several days were missed, the next run still considers only whatever WiGLE reports as the single newest upload; it never catches up older transactions.

## Files created locally

- `.env` — your private credentials.
- `.wigle_to_wdg_state.json` — duplicate-prevention state.
- `wigle_to_wdg.log` — activity and error messages without credentials.
- `out\` — filtered CSV/GZIP audit copies.

All are excluded from Git and from the published release ZIP.

## Command line

```bat
run_wigle_to_wdg.bat --dry-run --verbose
run_wigle_to_wdg.bat
run_wigle_to_wdg.bat --hours 12
run_wigle_to_wdg.bat --help
```

`--hours` may be reduced but cannot exceed 24. There is deliberately no all-time, backfill, multi-transaction, or force-resubmit option.

## Troubleshooting

- **WiGLE HTTP 401:** use the WiGLE API credential, not the website password.
- **WDG HTTP 401/403:** replace `WDG_API_KEY` in `.env`.
- **Newest transaction still processing:** run again later; the tool does not fall back to an older transaction.
- **No new WiGLE upload:** the newest transaction was already accepted by WDG.
- **Python not found:** reinstall Python and enable its PATH option.

## Development

```powershell
py -3 -m unittest discover -s tests -v
```

MIT licensed. This community project is not affiliated with WiGLE or WDG Wars.
