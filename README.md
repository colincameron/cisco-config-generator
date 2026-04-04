# Cisco 7941G Config Generator

Generates `SEP{MAC}.cnf.xml` provisioning files for Cisco 7941G phones running SIP firmware. User details (display name, SIP credentials) are pulled directly from a FreePBX/Asterisk MySQL database.

## Requirements

- Python 3.10+
- A FreePBX/Asterisk server with MySQL access
- Cisco 7941G phones flashed with SIP firmware (not SCCP)

## Installation

```bash
pip install -r requirements.txt
```

## First Run

On the first run the script will create a `config.ini` file with default values and exit:

```bash
python generate_configs.py
# Config file not found — creating default at config.ini
# Edit config.ini with your database credentials and PBX settings, then re-run.
```

Edit `config.ini` before continuing:

```ini
[database]
host     = localhost       # MySQL host
port     = 3306
name     = asterisk        # FreePBX database name (usually "asterisk")
user     = asteriskuser
password = asteriskpassword

[pbx]
proxy_host        = 192.168.1.1   # IP/hostname of your Asterisk/FreePBX server
proxy_port        = 5060
secure_proxy_port = 5061          # TLS SIP port

[phone]
timezone    = GMT Standard/Daylight Time   # Olson timezone string
date_format = D/M/Y
time_format = 12hr               # 12hr or 24hr
ntp_server  = pool.ntp.org
output_dir  = output             # Directory to write XML files into
```

## Usage

### Interactive menu

Running without `--phone` arguments opens the main menu:

```
Cisco 7941G Config Generator  [output/ — 3 existing]
──────────────────────────────────────────────────
  1  List existing configs
  2  Delete configs
  3  Generate new configs
  4  List all extensions in FreePBX DB
  q  Quit
```

The header shows the configured output directory and the number of config files currently present. The menu loops until you quit with `q`.

---

#### Option 1 — List existing configs

Parses every `SEP*.cnf.xml` file in the output directory and prints a table of MAC address, extension, and display name:

```
Found 3 config(s) in output/

  MAC Address        Extension  Display Name
  -----------------  ---------  ------------
  00:26:0B:5D:CB:52  201        Alice Smith
  00:26:0B:5D:CB:53  202        Bob Jones
  AA:BB:CC:DD:EE:FF  203        Reception
```

---

#### Option 2 — Delete configs

Lists all existing configs with index numbers, then offers:

```
Found 3 config(s):

   1.  00:26:0B:5D:CB:52  ext 201  Alice Smith
   2.  00:26:0B:5D:CB:53  ext 202  Bob Jones
   3.  AA:BB:CC:DD:EE:FF  ext 203  Reception

  a        Delete ALL
  b        Back to menu

Select (number(s), MAC address(es), a, or b):
```

- Enter one or more **list numbers** (`1`, `1 3`, `1,3`) to select specific files
- Enter one or more **MAC addresses** in any common format (`AA:BB:CC:DD:EE:FF`, `AABBCCDDEEFF`, etc.)
- Numbers and MAC addresses can be **mixed** in a single input (`1 AA:BB:CC:DD:EE:FF`)
- `a` deletes all files (requires `y` confirmation)
- `b` returns to the main menu

All destructive actions require `y` confirmation before anything is removed.

---

#### Option 4 — List all extensions in FreePBX DB

Connects to the database and lists every extension from the `users` table — useful for looking up extension numbers before generating configs:

```
Found 5 extension(s) in the FreePBX database:

  Extension  Display Name
  ---------  ------------
  101        Alice Smith
  102        Bob Jones
  201        Conference Room
  300        Reception
  999        Test Extension
```

---

#### Option 3 — Generate new configs

Prompts for MAC address and extension pairs one at a time. Leave the MAC field blank to finish, then the script queries the database and writes the files.

```
Enter phone entries. Leave MAC blank to finish.

MAC address (or blank to finish): 00:26:0B:5D:CB:52
Extension number: 201

MAC address (or blank to finish):

Processing MAC 00260B5DCB52 → extension 201 ... Written → output/SEP00260B5DCB52.cnf.xml

Done. 1/1 config(s) generated.
```

MAC addresses are accepted in any common format (`00:26:0B:5D:CB:52`, `00-26-0B-5D-CB-52`, `00260B5DCB52`, etc.) and are normalised to uppercase with no separators in the output filename.

---

### Non-interactive (batch) mode

Pass `--phone MAC:EXT` pairs as arguments to skip the menu entirely and go straight to generation. The flag can be repeated for multiple phones:

```bash
python generate_configs.py \
  --phone 00:26:0B:5D:CB:52:201 \
  --phone AA:BB:CC:DD:EE:FF:202
```

### All flags

| Flag | Description |
|---|---|
| `--phone MAC:EXT` | Phone entry (repeatable). Bypasses the menu and runs generate directly. |
| `--config FILE` | Path to an alternative `config.ini` (default: `config.ini` next to the script). |
| `--output-dir DIR` | Override the output directory from `config.ini`. |
| `--dry-run` | Print generated XML to stdout instead of writing files. Works in both menu and batch mode. |

## Output files

Files are written to `output/` (or the directory set in `config.ini` / `--output-dir`). Each file is named after the phone's MAC address in the format Cisco expects:

```
output/
  SEP00260B5DCB52.cnf.xml
  SEPAABBCCDDEEFF.cnf.xml
```

These files are served to the phones via TFTP. Copy them to your TFTP root directory (commonly `/srv/tftp/` on Linux).

## How it works

### 1. Database query

The script connects to the FreePBX `asterisk` MySQL database and queries three tables:

| Table | Columns used | Purpose |
|---|---|---|
| `users` | `extension`, `name` | Display name shown on the phone screen |
| `sip` | `id`, `secret`, `callerid`, `context` | SIP credentials for registration |

The primary query joins `users` and `sip` on the extension number. If the `users` table has no matching row (some minimal FreePBX setups), it falls back to querying `sip` alone. The display name is resolved from `users.name` first, then parsed from the `callerid` field (`"Name" <number>` format), and defaults to `Extension NNN` if neither is available.

### 2. XML generation

Each config file follows the `SEP{MAC}.cnf.xml` format that Cisco phones request from a TFTP server on boot. The generated XML contains:

- **`<deviceProtocol>SIP</deviceProtocol>`** — tells the phone to use SIP rather than SCCP
- **`<loadInformation>`** — specifies the SIP firmware image name
- **`<devicePool>`** — date/time format, timezone, NTP server
- **`<vendorConfig>`** — hardware feature toggles (speaker, headset, display sleep timer, etc.)
- **`<callManagerGroup>`** — IP and SIP port of the Asterisk server; required or the phone displays "Unprovisioned"
- **`<sipProfile>`** — SIP stack timers and transport settings (UDP+TCP)
- **`<sipLines>`** — two line buttons, both configured with the extension number, display name, and SIP auth credentials pulled from the database

### 3. Deployment

```
Phone boots
  └─> DHCP option 150 points to TFTP server
        └─> Phone requests SEP{MAC}.cnf.xml via TFTP
              └─> Phone reads config, registers with Asterisk using SIP credentials
```

Ensure DHCP option 150 (or option 66) points to your TFTP server's IP address, and that the generated XML files are in the TFTP root.

## Troubleshooting

**Phone displays "Unprovisioned"** — the `<callManagerGroup>` block is missing or malformed, or the XML file was not found via TFTP. Use `--dry-run` to inspect the generated XML.

**Extension not found** — the extension number does not exist in the `users` or `sip` table. Verify it in FreePBX Admin > Applications > Extensions.

**Database connection error** — check `config.ini` credentials and that the MySQL user has `SELECT` access to the `asterisk` database.

**Wrong display name** — if `users.name` is blank in FreePBX, the script falls back to parsing the `callerid` field. Set the user's name in FreePBX Admin to fix it.
