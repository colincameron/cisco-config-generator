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
import time
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
secure_proxy_port = 5061
# Title shown at the top of the phone directory
directory_title = My Office
# Path where directory.xml is written
directory_path = /var/www/html/directory.xml

[phone]
# Timezone string used in the XML (Olson format)
timezone     = GMT Standard/Daylight Time
date_format  = D/M/Y
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


def fetch_all_extensions(cursor) -> list[dict]:
    """Return all extensions from the FreePBX DB, sorted by extension number."""
    cursor.execute(
        "SELECT extension, name FROM users ORDER BY extension"
    )
    return cursor.fetchall()


def fetch_extension_details(cursor, extension: str) -> dict | None:
    """
    Query FreePBX tables for SIP credentials and display name.

    FreePBX (asterisk DB) relevant tables:
      users     – extension, name (display name)
      sip       – id (=extension), keyword, data, flags  (EAV format)
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

    return {
        "extension":    extension,
        "display_name": display_name,
        "sip_secret":   sip.get("secret") or "",
        "context":      sip.get("context") or "from-internal",
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
    secure_proxy_port = cfg["pbx"].get("secure_proxy_port", "5061")
    timezone    = cfg["phone"].get("timezone", "America/New_York")
    date_fmt    = cfg["phone"].get("date_format", "M/D/Y")
    time_fmt    = cfg["phone"].get("time_format", "12hr")
    ntp_server  = cfg["phone"].get("ntp_server", "pool.ntp.org")

    ext          = details["extension"]
    display_name = details["display_name"]
    sip_secret   = details["sip_secret"]

    root = ET.Element("device")

    _sub(root, "deviceProtocol", "SIP")
    _sub(root, "sshUserId", "cisco")
    _sub(root, "sshPassword", "cisco")
    _sub(root, "ipAddressMode", "0")

    device_settings = _sub(root, "devicePool")

    dt = _sub(device_settings, "dateTimeSetting")
    _sub(dt, "dateTemplate", date_fmt)
    _sub(dt, "timeZone", timezone)
    ntps = _sub(dt, "ntps")
    ntp = _sub(ntps, "ntp")
    _sub(ntp, "name", ntp_server)
    _sub(ntp, "ntpMode", "unicast")

    cm_group = _sub(device_settings, "callManagerGroup")
    members  = _sub(cm_group, "members")
    member   = _sub(members, "member")
    member.set("priority", "0")
    cm       = _sub(member, "callManager")
    _sub(cm, "processNodeName", proxy_host)
    _sub(cm, "name", proxy_host)
    ports    = _sub(cm, "ports")
    _sub(ports, "sipPort", proxy_port)
    _sub(ports, "securedSipPort", secure_proxy_port)
    _sub(ports, "ethernetPhonePort", "2000")

    # SIP configuration
    sip_cfg = _sub(root, "sipProfile")
    sip_proxies = _sub(sip_cfg, "sipProxies")
    _sub(sip_proxies, "registerWithProxy", "true")

    sip_call_features = _sub(sip_cfg, "sipCallFeatures")
    _sub(sip_call_features, "cnfJoinEnabled",       "true")
    _sub(sip_call_features, "rfc2543Hold",          "false")
    _sub(sip_call_features, "callHoldRingback",     "2")
    _sub(sip_call_features, "localCfwdEnable",      "true")
    _sub(sip_call_features, "semiAttendedTransfer", "true")
    _sub(sip_call_features, "anonymousCallBlock",   "2")
    _sub(sip_call_features, "callerIdBlocking",     "2")
    _sub(sip_call_features, "dndControl",           "0")
    _sub(sip_call_features, "remoteCcEnable",       "true")

    sip_stack = _sub(sip_cfg, "sipStack")
    _sub(sip_stack, "sipInviteRetx",         "6")
    _sub(sip_stack, "sipRetx",               "10")
    _sub(sip_stack, "timerInviteExpires",    "180")
    _sub(sip_stack, "timerRegisterExpires",  "3600")
    _sub(sip_stack, "timerRegisterDelta",    "5")
    _sub(sip_stack, "timerKeepAliveExpires", "120")
    _sub(sip_stack, "timerSubscribeExpires", "120")
    _sub(sip_stack, "timerSubscribeDelta",   "5")
    _sub(sip_stack, "timerT1",               "500")
    _sub(sip_stack, "timerT2",               "4000")
    _sub(sip_stack, "maxRedirects",          "70")
    _sub(sip_stack, "remotePartyID",         "true")
    _sub(sip_stack, "userInfo",              "None")

    _sub(sip_cfg, "autoAnswerTimer",                      "1")
    _sub(sip_cfg, "autoAnswerAltBehavior",                "false")
    _sub(sip_cfg, "autoAnswerOverride",                   "true")
    _sub(sip_cfg, "transferOnhookEnabled",                "false")
    _sub(sip_cfg, "enableVad",                            "false")
    _sub(sip_cfg, "preferredCodec",                       "g711ulaw")
    _sub(sip_cfg, "dtmfAvtPayload",                       "101")
    _sub(sip_cfg, "dtmfDbLevel",                          "3")
    _sub(sip_cfg, "dtmfOutofBand",                        "avt")
    _sub(sip_cfg, "alwaysUsePrimeLine",                   "false")
    _sub(sip_cfg, "alwaysUsePrimeLineVoiceMail",          "false")
    _sub(sip_cfg, "kpml",                                 "3")
    _sub(sip_cfg, "natEnabled",                           "false")
    _sub(sip_cfg, "phoneLabel",                           display_name)
    _sub(sip_cfg, "stutterMsgWaiting",                    "0")
    _sub(sip_cfg, "callStats",                            "false")
    _sub(sip_cfg, "silentPeriodBetweenCallWaitingBursts", "10")
    _sub(sip_cfg, "disableLocalSpeedDialConfig",           "false")
    _sub(sip_cfg, "startMediaPort",                       "10000")
    _sub(sip_cfg, "stopMediaPort",                        "20000")
    _sub(sip_cfg, "voipControlPort",                      proxy_port)
    _sub(sip_cfg, "dscpForAudio",                         "184")
    _sub(sip_cfg, "ringSettingBusyStationPolicy",         "0")
    _sub(sip_cfg, "dialTemplate",                         "dialplan.xml")

    # SIP lines
    sip_lines = _sub(sip_cfg, "sipLines")

    # Line 1 — primary extension
    line1 = ET.SubElement(sip_lines, "line")
    line1.set("button", "1")
    _sub(line1, "featureID",       "9")           # 9 = Line
    _sub(line1, "featureLabel",    f"{display_name} ({ext})")
    _sub(line1, "proxy",           "USECALLMANAGER")
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
    auto_answer = _sub(line1, "autoAnswer")
    _sub(auto_answer, "autoAnswerEnabled", "2")
    forward_disp = _sub(line1, "forwardCallInfoDisplay")
    _sub(forward_disp, "callerName", "true")
    _sub(forward_disp, "callerNumber", "true")
    _sub(forward_disp, "redirectedNumber", "true")
    _sub(forward_disp, "dialedNumber", "true")


    # Common profile
    common_profile = _sub(root, "commonProfile")
    _sub(common_profile, "phonePassword")
    _sub(common_profile, "backgroundImageAccess", "true")
    _sub(common_profile, "callLogBlfEnabled", "1")


    # Firmware / load info
    _sub(root, "loadInformation", "SIP41.8-5-4S")

    # Vendor config
    vendor = _sub(root, "vendorConfig")
    _sub(vendor, "disableSpeaker",           "false")
    _sub(vendor, "disableSpeakerAndHeadset", "false")
    _sub(vendor, "pcPort",                   "0")
    _sub(vendor, "settingsAccess",           "1")
    _sub(vendor, "garp",                     "0")
    _sub(vendor, "voiceVlanAccess",          "0")
    _sub(vendor, "videoCapability",          "0")
    _sub(vendor, "autoSelectLineEnable",     "0")
    _sub(vendor, "webAccess",                "0")
    _sub(vendor, "spanToPCPort",             "1")
    _sub(vendor, "loggingDisplay",           "1")
    _sub(vendor, "loadServer")
    _sub(vendor, "sshAccess",                "0")

    # Version - use timestamp
    _sub(root, "versionStamp", str(int(time.time())))

    # Network locale / user locale
    _sub(root, "networkLocale", "United_Kingdom")
    network_locale = _sub(root, "networkLocaleInfo")
    _sub(network_locale, "name", "United_Kingdom")
    _sub(network_locale, "uid", "64")
    _sub(network_locale, "version", "1.0.0.0-4")

    _sub(root, "deviceSecurityMode", "1")
    _sub(root, "authenticationURL")
    _sub(root, "servicesURL", f"http://{proxy_host}/directory.xml")
    _sub(root, "transportLayerProtocol", "2")
    _sub(root, "certHash")
    _sub(root, "encrConfig", "false")
    _sub(root, "dialToneSetting", "2")

    # Pretty-print
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    reparsed = minidom.parseString(f'{raw}')
    return reparsed.toprettyxml(indent="  ", encoding=None).replace(
        '<?xml version="1.0" ?>', ''
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


def action_list_extensions(cfg: configparser.ConfigParser) -> None:
    try:
        conn = get_db_connection(cfg)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        return

    with conn:
        with conn.cursor() as cursor:
            rows = fetch_all_extensions(cursor)

    if not rows:
        print("\nNo extensions found in the FreePBX database.")
        return

    col_ext  = max(len("Extension"), max(len(r["extension"] or "") for r in rows))
    col_name = max(len("Display Name"), max(len(r["name"] or "")      for r in rows))

    header = f"  {'Extension':<{col_ext}}  {'Display Name':<{col_name}}"
    rule   = f"  {'-' * col_ext}  {'-' * col_name}"
    print(f"\nFound {len(rows)} extension(s) in the FreePBX database:\n")
    print(header)
    print(rule)
    for r in rows:
        print(f"  {(r['extension'] or ''):<{col_ext}}  {(r['name'] or ''):<{col_name}}")
    print()


def build_directory_xml(rows: list[dict], title: str) -> str:
    """Return a pretty-printed CiscoIPPhoneDirectory XML string."""
    root = ET.Element("CiscoIPPhoneDirectory")
    ET.SubElement(root, "Title").text = title
    for r in rows:
        entry = ET.SubElement(root, "DirectoryEntry")
        ET.SubElement(entry, "Name").text = r["name"] or r["extension"]
        ET.SubElement(entry, "Telephone").text = r["extension"]
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    reparsed = minidom.parseString(raw)
    return reparsed.toprettyxml(indent="   ", encoding=None).replace('<?xml version="1.0" ?>', '').lstrip("\n")


def action_generate_phonebook(cfg: configparser.ConfigParser, dry_run: bool) -> None:
    title = cfg["pbx"].get("directory_title", "My Office")
    dest  = Path(cfg["pbx"].get("directory_path", "/var/www/html/directory.xml"))

    try:
        conn = get_db_connection(cfg)
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        return

    with conn:
        with conn.cursor() as cursor:
            rows = fetch_all_extensions(cursor)

    if not rows:
        print("\nNo extensions found in the FreePBX database.")
        return

    xml_content = build_directory_xml(rows, title)

    if dry_run:
        print(f"\n{'─' * 60}")
        print(f"File: {dest}")
        print(xml_content)
        return

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(xml_content, encoding="utf-8")
    except PermissionError:
        print(f"ERROR: Permission denied writing to {dest}. Try running with sudo.")
        return

    print(f"\nPhonebook with {len(rows)} entry/entries written → {dest}")


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
    print("  4  List all extensions in FreePBX DB")
    print("  5  Generate phone directory (directory.xml)")
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
        elif choice == "4":
            action_list_extensions(cfg)
        elif choice == "5":
            action_generate_phonebook(cfg, dry_run=args.dry_run)
        elif choice == "q":
            break
        else:
            print("  Invalid choice. Enter 1, 2, 3, 4, 5, or q.")


if __name__ == "__main__":
    main()
