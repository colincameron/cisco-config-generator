#!/usr/bin/env python3
"""
Cisco 7941G SIP Phone Config Generator
Queries a FreePBX/Asterisk MySQL database and generates SEP{MAC}.cnf.xml
provisioning files for Cisco 7941G phones running SIP firmware.
"""

import argparse
import configparser
import os
import re
import sys
from pathlib import Path
from xml.dom import minidom
import xml.etree.ElementTree as ET

try:
    import pymysql
except ImportError:
    print("ERROR: pymysql is required. Install it with: pip install pymysql")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "config.ini"

DEFAULT_CONFIG = """\
[database]
host     = localhost
port     = 3306
name     = asterisk
user     = asteriskuser
password = asteriskpassword

[pbx]
# SIP proxy / Asterisk server that phones register to
proxy_host = 192.168.1.1
proxy_port = 5060

[phone]
# Timezone string used in the XML (Olson format)
timezone     = America/New_York
date_format  = M/D/Y
time_format  = 12hr
ntp_server   = pool.ntp.org
# Output directory for generated XML files (default: ./output)
output_dir   = output
"""


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        print(f"Config file not found — creating default at {path}")
        path.write_text(DEFAULT_CONFIG)
        print("Edit config.ini with your database credentials and PBX settings, then re-run.\n")
        sys.exit(0)

    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


# ---------------------------------------------------------------------------
# MAC address helpers
# ---------------------------------------------------------------------------

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-.]?){5}[0-9A-Fa-f]{2}$")


def normalise_mac(raw: str) -> str:
    """Return MAC as 12 uppercase hex digits, no separators."""
    digits = re.sub(r"[:\-.\s]", "", raw).upper()
    if len(digits) != 12 or not re.fullmatch(r"[0-9A-F]{12}", digits):
        raise ValueError(f"Invalid MAC address: {raw!r}")
    return digits


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def get_db_connection(cfg: configparser.ConfigParser):
    return pymysql.connect(
        host=cfg["database"]["host"],
        port=int(cfg["database"].get("port", 3306)),
        db=cfg["database"]["name"],
        user=cfg["database"]["user"],
        password=cfg["database"]["password"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


def fetch_extension_details(cursor, extension: str) -> dict | None:
    """
    Query FreePBX tables for SIP credentials and display name.

    FreePBX (asterisk DB) relevant tables:
      users     – extension, name (display name)
      sip       – id (=extension), keyword, data, flags  (EAV format)
      voicemail – mailbox, password, fullname, email
    """
    # Fetch all sip rows for this extension and pivot keyword→data
    cursor.execute(
        "SELECT keyword, data FROM sip WHERE id = %s",
        (extension,),
    )
    sip_rows = cursor.fetchall()

    if not sip_rows:
        return None

    sip = {r["keyword"]: r["data"] for r in sip_rows}

    # Fetch display name from users table
    cursor.execute(
        "SELECT name FROM users WHERE extension = %s LIMIT 1",
        (extension,),
    )
    user_row = cursor.fetchone() or {}

    # Resolve display name: prefer users.name, then parse callerid "Name" <number>
    display_name = user_row.get("name") or ""
    if not display_name:
        cid = sip.get("callerid") or ""
        m = re.match(r'"?([^"<]+)"?\s*<', cid)
        display_name = m.group(1).strip() if m else f"Extension {extension}"

    # Optionally fetch voicemail password (useful as a secondary credential check)
    cursor.execute(
        "SELECT password AS vm_password, email FROM voicemail WHERE mailbox = %s LIMIT 1",
        (extension,),
    )
    vm_row = cursor.fetchone() or {}

    return {
        "extension":    extension,
        "display_name": display_name,
        "sip_secret":   sip.get("secret") or "",
        "context":      sip.get("context") or "from-internal",
        "vm_password":  vm_row.get("vm_password") or "",
        "email":        vm_row.get("email") or "",
    }


# ---------------------------------------------------------------------------
# XML generation
# ---------------------------------------------------------------------------

def _sub(parent: ET.Element, tag: str, text: str = "") -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = text
    return el


def build_xml(mac: str, details: dict, cfg: configparser.ConfigParser) -> str:
    """Return a pretty-printed XML string for SEP{mac}.cnf.xml."""
    proxy_host = cfg["pbx"]["proxy_host"]
    proxy_port = cfg["pbx"].get("proxy_port", "5060")
    timezone    = cfg["phone"].get("timezone", "America/New_York")
    date_fmt    = cfg["phone"].get("date_format", "M/D/Y")
    time_fmt    = cfg["phone"].get("time_format", "12hr")
    ntp         = cfg["phone"].get("ntp_server", "pool.ntp.org")

    ext          = details["extension"]
    display_name = details["display_name"]
    sip_secret   = details["sip_secret"]

    root = ET.Element("device")

    # Protocol
    _sub(root, "deviceProtocol", "SIP")
    _sub(root, "sshUserId", "cisco")
    _sub(root, "sshPassword", "cisco")

    # Firmware / load info
    load_info = _sub(root, "loadInformation")
    loads = _sub(load_info, "loads")
    load = _sub(loads, "load", "SIP45.9-4-2SR3-1S")
    load.set("index", "0")

    # Device pool / settings
    device_settings = _sub(root, "deviceSettings")
    dt = _sub(device_settings, "dateTime")
    _sub(dt, "dateTemplate", date_fmt)
    _sub(dt, "timeFormat", time_fmt)
    _sub(dt, "timeZone", timezone)
    _sub(dt, "ntpServer1", ntp)

    # Vendor config
    vendor = _sub(root, "vendorConfig")
    _sub(vendor, "disableSpeaker",     "0")
    _sub(vendor, "disableHeadset",     "0")
    _sub(vendor, "disableVideo",       "1")
    _sub(vendor, "disableSshPort",     "0")
    _sub(vendor, "webAccess",          "0")
    _sub(vendor, "powerSaveDisplay",   "1800")

    # Call manager group (required — phones show "Unprovisioned" without this)
    cm_group = _sub(root, "callManagerGroup")
    members  = _sub(cm_group, "members")
    member   = _sub(members, "member")
    member.set("priority", "0")
    cm       = _sub(member, "callManager")
    cm_name  = _sub(cm, "processNodeName", proxy_host)
    ports    = _sub(cm, "ports")
    _sub(ports, "sipPort", proxy_port)
    _sub(ports, "mgcpPorts")

    # SIP configuration
    sip_cfg = _sub(root, "sipProfile")
    _sub(sip_cfg, "sipProxies")           # placeholder; proxy per-line below
    sip_stack = _sub(sip_cfg, "sipStack")
    _sub(sip_stack, "sipInviteRetx",           "6")
    _sub(sip_stack, "sipTimerT1",              "500")
    _sub(sip_stack, "sipTimerT2",              "4000")
    _sub(sip_stack, "sipTimerInviteExpires",   "180")
    _sub(sip_stack, "sipTimerRegisterExpires", "3600")
    _sub(sip_stack, "sipTimerRegisterDelta",   "5")
    _sub(sip_stack, "transportLayerProtocol",  "4")  # 4 = UDP+TCP

    # SIP lines
    sip_lines = _sub(root, "sipLines")

    # Line 1 — primary extension
    line1 = ET.SubElement(sip_lines, "line")
    line1.set("button", "1")
    _sub(line1, "featureID",       "9")           # 9 = Line
    _sub(line1, "featureLabel",    display_name)
    _sub(line1, "proxy",           proxy_host)
    _sub(line1, "port",            proxy_port)
    _sub(line1, "name",            ext)
    _sub(line1, "displayName",     display_name)
    _sub(line1, "contact",         ext)
    _sub(line1, "authName",        ext)
    _sub(line1, "authPassword",    sip_secret)
    _sub(line1, "sharedLine",      "false")
    _sub(line1, "messageWaitingLampPolicy", "3")
    _sub(line1, "messagesNumber",  "*97")
    _sub(line1, "ringSettingIdle", "4")
    _sub(line1, "ringSettingActive", "5")
    _sub(line1, "callWaiting",     "3")
    _sub(line1, "autoAnswer",      "0")

    # Line 2 — spare (no auth, acts as second appearance or spare)
    line2 = ET.SubElement(sip_lines, "line")
    line2.set("button", "2")
    _sub(line2, "featureID",    "9")
    _sub(line2, "featureLabel", display_name)
    _sub(line2, "proxy",        proxy_host)
    _sub(line2, "port",         proxy_port)
    _sub(line2, "name",         ext)
    _sub(line2, "displayName",  display_name)
    _sub(line2, "contact",      ext)
    _sub(line2, "authName",     ext)
    _sub(line2, "authPassword", sip_secret)
    _sub(line2, "sharedLine",   "false")
    _sub(line2, "callWaiting",  "3")
    _sub(line2, "autoAnswer",   "0")

    # Network locale / user locale
    _sub(root, "networkLocale",     "United_States")
    _sub(root, "networkLocaleInfo")
    _sub(root, "userLocale",        "English_United_States")

    # Pretty-print
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    reparsed = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    return reparsed.toprettyxml(indent="  ", encoding=None).replace(
        '<?xml version="1.0" ?>', '<?xml version="1.0" encoding="UTF-8"?>'
    )


# ---------------------------------------------------------------------------
# Config file inspection helpers
# ---------------------------------------------------------------------------

def parse_config_file(path: Path) -> dict:
    """Extract MAC, extension, and display name from a SEP{MAC}.cnf.xml file."""
    mac = path.stem[3:]  # strip leading "SEP"
    extension = ""
    display_name = ""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        # Find the first line button in sipLines
        line = root.find(".//sipLines/line[@button='1']")
        if line is not None:
            extension    = (line.findtext("name")        or "").strip()
            display_name = (line.findtext("displayName") or "").strip()
    except ET.ParseError:
        display_name = "(unreadable XML)"
    return {"mac": mac, "extension": extension, "display_name": display_name, "path": path}


def find_existing_configs(output_dir: Path) -> list[dict]:
    """Return parsed info for every SEP*.cnf.xml in output_dir, sorted by extension."""
    if not output_dir.exists():
        return []
    files = sorted(output_dir.glob("SEP*.cnf.xml"))
    return [parse_config_file(f) for f in files]


def format_mac(mac: str) -> str:
    """Format a 12-digit MAC as XX:XX:XX:XX:XX:XX."""
    return ":".join(mac[i:i+2] for i in range(0, 12, 2))


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def action_list(output_dir: Path) -> None:
    configs = find_existing_configs(output_dir)
    if not configs:
        print(f"No config files found in {output_dir}/")
        return

    col_mac  = max(len("MAC Address"),  max(len(format_mac(c["mac"]))   for c in configs))
    col_ext  = max(len("Extension"),    max(len(c["extension"])          for c in configs))
    col_name = max(len("Display Name"), max(len(c["display_name"])       for c in configs))

    header = f"  {'MAC Address':<{col_mac}}  {'Extension':<{col_ext}}  {'Display Name':<{col_name}}"
    rule   = f"  {'-' * col_mac}  {'-' * col_ext}  {'-' * col_name}"
    print(f"\nFound {len(configs)} config(s) in {output_dir}/\n")
    print(header)
    print(rule)
    for c in sorted(configs, key=lambda x: x["extension"]):
        print(f"  {format_mac(c['mac']):<{col_mac}}  {c['extension']:<{col_ext}}  {c['display_name']:<{col_name}}")
    print()


def action_delete(output_dir: Path) -> None:
    configs = find_existing_configs(output_dir)
    if not configs:
        print(f"No config files found in {output_dir}/")
        return

    configs_sorted = sorted(configs, key=lambda x: x["extension"])

    print(f"\nFound {len(configs_sorted)} config(s):\n")
    for i, c in enumerate(configs_sorted, 1):
        print(f"  {i:>2}.  {format_mac(c['mac'])}  ext {c['extension']}  {c['display_name']}")

    # Build a lookup map for MAC-based selection (normalised → config dict)
    mac_lookup = {c["mac"]: c for c in configs_sorted}

    print()
    print("  a        Delete ALL")
    print("  b        Back to menu")
    print()

    while True:
        try:
            choice = input("Select (number(s), MAC address(es), a, or b): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "b":
            return

        if choice == "a":
            confirm = input(f"Delete all {len(configs_sorted)} config(s)? [y/N]: ").strip().lower()
            if confirm == "y":
                for c in configs_sorted:
                    c["path"].unlink()
                    print(f"  Deleted {c['path'].name}")
                print(f"\n{len(configs_sorted)} file(s) deleted.")
            else:
                print("Cancelled.")
            return

        # Each whitespace/comma-separated token is either a number or a MAC address
        tokens = re.split(r"[\s,]+", choice)
        targets = []
        valid = True
        seen = set()
        for t in tokens:
            if not t:
                continue
            # Try as list number first
            if t.isdigit():
                idx = int(t) - 1
                if not (0 <= idx < len(configs_sorted)):
                    print(f"  Invalid number: {t!r}. Enter 1–{len(configs_sorted)}.")
                    valid = False
                    break
                c = configs_sorted[idx]
            else:
                # Try to interpret as a MAC address
                try:
                    mac = normalise_mac(t)
                except ValueError:
                    print(f"  Unrecognised input: {t!r}. Enter a list number or a MAC address.")
                    valid = False
                    break
                if mac not in mac_lookup:
                    print(f"  MAC {format_mac(mac)} not found in the config list.")
                    valid = False
                    break
                c = mac_lookup[mac]
            if c["mac"] not in seen:
                seen.add(c["mac"])
                targets.append(c)

        if not valid:
            continue
        if not targets:
            continue

        print()
        for c in targets:
            print(f"  {c['path'].name}  ({c['display_name']})")
        confirm = input(f"\nDelete {len(targets)} file(s)? [y/N]: ").strip().lower()
        if confirm == "y":
            for c in targets:
                c["path"].unlink()
                print(f"  Deleted {c['path'].name}")
            print(f"\n{len(targets)} file(s) deleted.")
        else:
            print("Cancelled.")
        return


def action_generate(cfg: configparser.ConfigParser, output_dir: Path, dry_run: bool) -> None:
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    entries = prompt_phone_entries([])
    if not entries:
        print("No phone entries provided.")
        return

    try:
        conn = get_db_connection(cfg)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        return

    success_count = 0
    with conn:
        with conn.cursor() as cursor:
            for mac, ext in entries:
                print(f"Processing MAC {mac} → extension {ext} ...", end=" ")

                details = fetch_extension_details(cursor, ext)
                if details is None:
                    print("NOT FOUND in database. Skipping.")
                    continue

                xml_content = build_xml(mac, details, cfg)
                filename = f"SEP{mac}.cnf.xml"

                if dry_run:
                    print(f"\n{'─' * 60}")
                    print(f"File: {filename}")
                    print(xml_content)
                else:
                    out_path = output_dir / filename
                    out_path.write_text(xml_content, encoding="utf-8")
                    print(f"Written → {out_path}")

                success_count += 1

    print(f"\nDone. {success_count}/{len(entries)} config(s) generated.")


# ---------------------------------------------------------------------------
# Interactive input helpers
# ---------------------------------------------------------------------------

def prompt_phone_entries(args_phones: list[str]) -> list[tuple[str, str]]:
    """
    Return a list of (normalised_mac, extension) tuples.
    Sources: --phone CLI args first, then interactive prompts.
    """
    entries: list[tuple[str, str]] = []

    # Parse any values passed on the command line: MAC:EXT pairs
    for token in (args_phones or []):
        if ":" not in token:
            print(f"WARNING: Skipping malformed --phone value {token!r} (expected MAC:EXT)")
            continue
        mac_raw, ext = token.split(":", 1)
        try:
            entries.append((normalise_mac(mac_raw), ext.strip()))
        except ValueError as e:
            print(f"WARNING: {e}")

    # Interactive mode
    if not entries:
        print("Enter phone entries. Leave MAC blank to finish.\n")

    while True:
        try:
            mac_raw = input("MAC address (or blank to finish): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not mac_raw:
            break

        try:
            mac = normalise_mac(mac_raw)
        except ValueError as e:
            print(f"  {e}. Try again.")
            continue

        ext = input("Extension number: ").strip()
        if not ext:
            print("  Extension cannot be empty. Try again.")
            continue

        entries.append((mac, ext))
        print()

    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def show_menu(output_dir: Path) -> None:
    existing = find_existing_configs(output_dir)
    count_str = f"{len(existing)} existing" if existing else "none"
    print(f"\nCisco 7941G Config Generator  [{output_dir}/ — {count_str}]")
    print("─" * 50)
    print("  1  List existing configs")
    print("  2  Delete configs")
    print("  3  Generate new configs")
    print("  q  Quit")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Cisco 7941G SIP XML configs from FreePBX database."
    )
    parser.add_argument(
        "--phone",
        metavar="MAC:EXT",
        action="append",
        help="MAC address and extension as a pair (repeatable). Skips menu, runs generate directly.",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default=str(CONFIG_FILE),
        help=f"Path to config.ini (default: {CONFIG_FILE})",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default=None,
        help="Directory for XML files (overrides config.ini setting).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated XML to stdout without writing files.",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    output_dir = Path(args.output_dir or cfg["phone"].get("output_dir", "output"))

    # Non-interactive: --phone flags bypass the menu and go straight to generate
    if args.phone:
        if not args.dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        entries = prompt_phone_entries(args.phone)
        if not entries:
            print("No valid phone entries provided. Exiting.")
            sys.exit(0)
        try:
            conn = get_db_connection(cfg)
        except Exception as e:
            print(f"ERROR: Could not connect to database: {e}")
            sys.exit(1)
        success_count = 0
        with conn:
            with conn.cursor() as cursor:
                for mac, ext in entries:
                    print(f"Processing MAC {mac} → extension {ext} ...", end=" ")
                    details = fetch_extension_details(cursor, ext)
                    if details is None:
                        print("NOT FOUND in database. Skipping.")
                        continue
                    xml_content = build_xml(mac, details, cfg)
                    filename = f"SEP{mac}.cnf.xml"
                    if args.dry_run:
                        print(f"\n{'─' * 60}\nFile: {filename}\n{xml_content}")
                    else:
                        out_path = output_dir / filename
                        out_path.write_text(xml_content, encoding="utf-8")
                        print(f"Written → {out_path}")
                    success_count += 1
        print(f"\nDone. {success_count}/{len(entries)} config(s) generated.")
        return

    # Interactive menu loop
    while True:
        show_menu(output_dir)
        try:
            choice = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "1":
            action_list(output_dir)
        elif choice == "2":
            action_delete(output_dir)
        elif choice == "3":
            action_generate(cfg, output_dir, dry_run=args.dry_run)
        elif choice == "q":
            break
        else:
            print("  Invalid choice. Enter 1, 2, 3, or q.")


if __name__ == "__main__":
    main()
