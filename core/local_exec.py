"""Validated local command lane.

Runs allowlisted command-line tools directly via ``subprocess`` — for tools
whose binaries exist on this machine. Every command is validated against a
per-tool argument allowlist before execution; raw shell fragments
(``sh -c``, ``bash -c``, ``python -c``) are deliberately NOT supported, so
the allowlist cannot be bypassed.

Responses use one uniform shape so every caller treats results identically:

    {"stdout": str, "stderr": str, "return_code": int, "success": bool}
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

TIMEOUT_DEFAULT = 300
TIMEOUT_MIN = 5
TIMEOUT_MAX = 1800

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./@:\-+=,\[\]{}()\\!?*^$~]+$")
_IP = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
)

# Allowlist: tool -> permitted options.  Values starting with "-" must match
# exactly, as a combined short-flag prefix (-t4 matches -t), or as flag=value
# (--severity=critical matches --severity).  Bare words (http, dir) must
# match exactly.
ALLOWLIST: dict[str, dict[str, Any]] = {
    "nmap": {"flags": ["-sV", "-sC", "-sS", "-sT", "-sU", "-sL", "-sn", "-sP", "-Pn", "-n", "-T4", "-T5", "-T3", "-p", "-p-", "--top-ports", "-A", "-O", "-v", "-vv", "--open", "-oN", "-oX", "-oG", "--host-timeout", "--max-retries", "--version-intensity", "--version-light", "-e", "-iL", "--script"], "words": [], "help": "Network scanner."},
    "arp-scan": {"flags": ["-I", "-l", "-n", "-q", "-g", "-x", "--interface", "--retry", "--timeout", "-localnet"], "words": [], "help": "ARP host discovery."},
    "masscan": {"flags": ["-p", "--rate", "--range", "-oG", "-oX", "--banners", "-e", "--offline", "-T", "-c"], "words": [], "help": "Mass port scanning."},
    "traceroute": {"flags": ["-n", "-m", "-w", "-I", "-T", "-p", "-i", "-s"], "words": [], "help": "Network path discovery."},
    "ping": {"flags": ["-c", "-n", "-s", "-W", "-i", "-a", "-t"], "words": [], "help": "Host reachability check."},
    "netdiscover": {"flags": ["-r", "-i", "-p", "-f", "-l", "-s", "-n"], "words": [], "help": "ARP-based host discovery."},
    "hping3": {"flags": ["-S", "-A", "-F", "-U", "-p", "-c", "-i", "--data", "-n", "-e", "-w", "-d", "-z", "-T", "--flood"], "words": [], "help": "Packet crafting."},
    "dig": {"flags": ["+short", "+time=1", "+tries=1", "-x", "@", "-t"], "words": [], "help": "DNS lookups."},
    "host": {"flags": ["-a", "-t", "-v", "-W"], "words": [], "help": "DNS resolution."},
    "whois": {"flags": ["-h"], "words": [], "help": "Domain ownership lookup."},
    "ss": {"flags": ["-l", "-n", "-t", "-u", "-p", "-a", "-x", "-h", "-H"], "words": [], "help": "Open socket listing."},
    "lsblk": {"flags": ["-f", "-o", "-a", "-p", "-J", "--json", "--fs"], "words": [], "help": "Block device and filesystem listing."},
    "notify-send": {"flags": ["-u", "-i", "-t", "-a", "-h", "-e"], "words": ["low", "normal", "critical"], "help": "Desktop notification."},
    "holehe": {"flags": ["--only-used", "--no-color", "--no-clear", "-v", "-o", "--only-used"], "words": [], "help": "Check where an email address is registered (own identifiers only)."},
    "sherlock": {"flags": ["--timeout", "--print-all", "--output", "--csv", "--folderoutput", "--tor", "--unique-tor", "-v", "-r", "-b", "-s", "--site"], "words": [], "help": "Username search across sites (own identifiers only)."},
    "maigret": {"flags": ["--timeout", "--print-all", "--output", "--csv", "--json", "--proxy", "--tor", "-v", "-s", "-a", "--no-color"], "words": [], "help": "Username OSINT report (own identifiers only)."},
    "shodan": {"flags": ["host", "search", "count", "info", "domain", "myip", "init", "alert", "-O", "-o", "-f", "-v", "--fields", "--limit"], "words": [], "help": "Shodan exposure lookup (needs 'shodan init <api-key>' first)."},
    "censys": {"flags": ["search", "view", "host", "history", "asm", "certificates", "-o", "-f", "-v", "-h", "--help", "--index-type"], "words": [], "help": "Censys certificate/host lookup (needs API credentials first)."},
    "curl": {"flags": ["-s", "-L", "-I", "-A", "-H", "-X", "-o", "-u", "-k", "-m", "--data", "--data-raw", "--request", "-fsSL", "-D"], "words": [], "help": "HTTP requests."},
    "gobuster": {"flags": ["-u", "-w", "-p", "-t", "-o", "-x", "-r", "-k", "-s", "-b", "-H", "-m", "-c", "-q", "-n", "-a"], "words": ["dir", "dns", "vhost", "fuzz", "s3", "gcs"], "help": "Content discovery."},
    "nuclei": {"flags": ["-u", "-l", "-t", "-o", "-s", "-severity", "-c", "-silent", "-v", "-json", "-rl", "-w", "-e", "-et", "-duc", "-ni", "-nf", "-nc", "-exclude", "-tl"], "words": [], "help": "Template vulnerability scanning."},
    "httpx": {"flags": ["-l", "-u", "-o", "-t", "-title", "-tech-detect", "-status-code", "-ip", "-cdn", "-json", "-silent", "-follow-redirects", "-p", "-v"], "words": [], "help": "Web asset probing."},
    "subfinder": {"flags": ["-d", "-o", "-all", "-passive", "-active", "-sources", "-recursive", "-ip", "-silent", "-v", "-t"], "words": [], "help": "Passive subdomain discovery."},
    "amass": {"flags": ["enum", "intel", "viz", "track", "-d", "-o", "-passive", "-active", "-brute", "-w", "-ip", "-src", "-oN", "-json"], "words": [], "help": "Attack surface mapping."},
    "nikto": {"flags": ["-h", "-p", "-o", "-oN", "-oX", "-oH", "-oC", "-T", "-ssl", "-C", "-v", "-e", "-n", "-w", "-c", "-u", "-f"], "words": [], "help": "Web server assessment."},
    "dirb": {"flags": ["-w", "-o", "-t", "-r", "-S", "-X", "-H", "-a", "-p", "-N", "-m"], "words": [], "help": "Directory brute force."},
    "dirsearch": {"flags": ["-u", "-w", "-t", "-o", "-e", "-x", "-r", "-H", "-i", "-f", "-F", "-m", "-p", "-q"], "words": [], "help": "Web path scanning."},
    "feroxbuster": {"flags": ["-u", "-w", "-t", "-d", "-o", "-x", "-k", "-s", "-H", "-q", "-A", "-r", "-p", "-P", "-a", "-f", "-v"], "words": [], "help": "Content discovery."},
    "ffuf": {"flags": ["-u", "-w", "-X", "-H", "-d", "-t", "-o", "-x", "-r", "-k", "-fc", "-fl", "-fs", "-fw", "-hh", "-ac", "-of", "-p", "-m", "-se"], "words": [], "help": "Web fuzzer."},
    "whatweb": {"flags": ["-v", "-a", "-p", "-o", "-H", "--log-brief", "-i", "-q"], "words": [], "help": "Technology fingerprinting."},
    "wafw00f": {"flags": ["-v", "-a", "-o", "-i", "-l", "-t"], "words": [], "help": "WAF detection."},
    "arjun": {"flags": ["-u", "-o", "-t", "-m", "-w", "-i", "-oJ", "-oT", "-v"], "words": [], "help": "Hidden parameter discovery."},
    "paramspider": {"flags": ["-d", "-o", "-w", "-e", "-i"], "words": [], "help": "Archived parameter mining."},
    "waybackurls": {"flags": [], "words": [], "help": "Wayback URL mining."},
    "gau": {"flags": ["-o", "-t", "-v", "-from", "-subs"], "words": [], "help": "Known-URL fetching."},
    "hakrawler": {"flags": ["-d", "-t", "-kf", "-h", "-u", "-s", "-i"], "words": [], "help": "Web crawling."},
    "testssl.sh": {"flags": ["-p", "-s", "-e", "-f", "-S", "-P", "-U", "--quiet", "--color", "-o", "-oJ", "-oA", "--severity", "-v"], "words": [], "help": "TLS configuration scan."},
    "sslscan": {"flags": ["--show-certificate", "--no-colour", "--no-failed", "--tlsall", "-c", "--xml", "--version", "-h"], "words": [], "help": "TLS cipher scan."},
    "sslyze": {"flags": ["--regular", "--cert", "--compression", "--heartbleed", "--xml_out", "-h"], "words": [], "help": "TLS analysis."},
    "smbmap": {"flags": ["-H", "-u", "-p", "-d", "-P", "-R", "-r", "-x", "-L", "-N", "-v", "-A"], "words": [], "help": "SMB share enumeration."},
    "enum4linux-ng": {"flags": ["-A", "-a", "-u", "-p", "-o", "-v", "-oJ"], "words": [], "help": "SMB enumeration."},
    "enum4linux": {"flags": ["-a", "-U", "-S", "-G", "-P", "-O", "-o", "-v", "-n", "-p"], "words": [], "help": "SMB enumeration (legacy)."},
    "smbclient": {"flags": ["-L", "-U", "-N", "-p", "-I", "-m", "-c", "-t", "-A"], "words": [], "help": "SMB client."},
    "rpcclient": {"flags": ["-U", "-N", "-c", "-I", "-p"], "words": [], "help": "MS-RPC queries."},
    "nbtscan": {"flags": ["-r", "-v", "-f", "-h"], "words": [], "help": "NetBIOS scan."},
    "searchsploit": {"flags": ["-t", "-e", "-j", "-w", "-p", "-x", "-m", "-c", "-s", "-d", "-u", "-n", "--nmap", "-v", "-h"], "words": [], "help": "Exploit-DB search."},
    "msfvenom": {"flags": ["-p", "-f", "-o", "-e", "-i", "-b", "-a", "--platform", "--arch", "--encoder", "--iterations", "-l", "--list", "--payload-options", "-n", "--nopsled", "-s", "--smallest", "-v", "-h"], "words": [], "help": "Payload generation (writes a file; treat the artefact as hostile)."},
    "msfconsole": {"flags": ["-q", "-v", "--version", "-h", "--help"], "words": [], "help": "Metasploit console version/info only. Module execution (-x/-r) is deliberately NOT permitted in the fast lane; drive it through the server lane or a reviewed resource file."},
    "binwalk": {"flags": ["-e", "-Me", "-o", "-y", "-w", "-A", "-R", "-D", "-C", "-q", "-z", "-f", "-t"], "words": [], "help": "Firmware analysis."},
    "strings": {"flags": ["-a", "-n", "-f"], "words": [], "help": "String extraction."},
    "exiftool": {"flags": ["-a", "-u", "-g1", "-j", "-n"], "words": [], "help": "Metadata reading."},
    "file": {"flags": ["-b", "-i", "-z", "-L"], "words": [], "help": "File type identification."},
    "xxd": {"flags": ["-l", "-s", "-r", "-p"], "words": [], "help": "Hex dump."},
    "checksec": {"flags": ["--file", "--dir", "--fortify-file", "--kernel"], "words": [], "help": "Binary hardening check."},
    "objdump": {"flags": ["-d", "-M", "-f", "-h", "-x"], "words": [], "help": "Disassembly."},
    "nm": {"flags": ["-a", "-D", "-l", "-n", "-C", "-U"], "words": [], "help": "Symbol listing."},
    "radare2": {"flags": ["-A", "-q", "-c", "-d", "-e", "-i", "-p", "-n", "-w", "-v", "-0", "-l"], "words": [], "help": "RE framework."},
    "r2": {"flags": ["-A", "-q", "-c", "-d", "-e", "-i", "-p", "-n", "-w", "-v"], "words": [], "help": "Radare2 alias."},
    "rabin2": {"flags": ["-I", "-i", "-s", "-S", "-z", "-e", "-l", "-H", "-x", "-c"], "words": [], "help": "Binary info."},
    "yara": {"flags": ["-r", "-w", "-g", "-s", "-m", "-f", "-p", "-a", "-d"], "words": [], "help": "Pattern matching."},
    "gdb": {"flags": ["-batch", "-ex", "-q"], "words": [], "help": "Binary debugging."},
    "ropper": {"flags": ["--file", "-i", "-a", "--nocolor"], "words": [], "help": "Gadget search."},
    "ROPgadget": {"flags": ["--binary", "--only", "--opcode", "--depth", "--all", "--silent", "--nojop", "--nosys", "--string", "--re", "--badbytes"], "words": [], "help": "Gadget search (capitalised binary name)."},
    "trivy": {"flags": ["image", "fs", "repo", "config", "kubernetes", "--severity", "--format", "--output", "--quiet", "--scanners", "--skip-dirs", "--ignore-unfixed", "-v"], "words": [], "help": "Vulnerability scanning."},
    "grype": {"flags": ["image", "dir", "sbom", "-o", "-f", "-v"], "words": [], "help": "Container vuln matching."},
    "syft": {"flags": ["image", "dir", "-o"], "words": [], "help": "SBOM generation."},
    "dive": {"flags": ["image", "ci"], "words": [], "help": "Image layer analysis."},
    "hash-identifier": {"flags": [], "words": [], "help": "Hash identification."},
    "hashid": {"flags": ["-m", "-j", "-e", "-f", "-o"], "words": [], "help": "Hash identification."},
    "steghide": {"flags": ["info", "extract", "embed", "-cf", "-ef", "-sf", "-xf", "-p", "-f"], "words": [], "help": "Steganography."},
    "zsteg": {"flags": ["-v", "-a", "-c", "-m", "-l", "-o", "-b", "-f", "-r"], "words": [], "help": "PNG/BMP steg detection."},
    "outguess": {"flags": ["-k", "-r", "-d", "-s", "-i", "-p"], "words": [], "help": "Steganography."},
    "foremost": {"flags": ["-i", "-o", "-t", "-w", "-q", "-v", "-c"], "words": [], "help": "File carving."},
    "bulk_extractor": {"flags": ["-o", "-e", "-x", "-V", "-S"], "words": [], "help": "Forensic extraction."},
    "fls": {"flags": ["-r", "-o", "-f", "-p", "-m"], "words": [], "help": "Sleuth Kit listing."},
    "icat": {"flags": ["-r", "-o", "-f", "-s"], "words": [], "help": "Sleuth Kit extraction."},
    "mmls": {"flags": ["-o", "-b", "-B", "-a", "-t"], "words": [], "help": "Partition analysis."},
    "tsk_recover": {"flags": ["-e", "-a", "-o", "-f"], "words": [], "help": "File recovery."},
    "zip2john": {"flags": [], "words": [], "help": "Zip hash conversion."},
    "ssh2john": {"flags": [], "words": [], "help": "SSH key hash conversion."},
    "keepass2john": {"flags": [], "words": [], "help": "KeePass hash conversion."},
    "gpg": {"flags": ["--decrypt", "--encrypt", "--import", "--armor", "-d", "-e", "-c", "--list-keys"], "words": [], "help": "OpenPGP operations."},
    "openssl": {"flags": ["s_client", "s_server", "enc", "dgst", "genrsa", "req", "x509", "asn1parse", "version", "-connect", "-showcerts"], "words": [], "help": "TLS toolkit."},
    "ss": {"flags": ["-l", "-n", "-t", "-u", "-p", "-a", "-x", "-h", "-lntup"], "words": [], "help": "Socket listing."},
    "ps": {"flags": ["-eo", "-aux", "-ef", "-o", "pid,comm,args"], "words": [], "help": "Process listing."},
    "lsof": {"flags": ["-i", "-n", "-P", "-p"], "words": [], "help": "Open files."},
    "uname": {"flags": ["-a", "-s", "-m", "-r", "-v"], "words": [], "help": "Kernel info."},
    "iwconfig": {"flags": ["-a", "-s", "-v"], "words": [], "help": "Wireless interfaces."},
    "iwlist": {"flags": ["scan", "channel", "freq", "ap"], "words": [], "help": "Wireless info."},
    "iw": {"flags": ["dev", "link", "scan", "phy"], "words": [], "help": "Wireless management."},
    "wash": {"flags": ["-i", "-c", "-f", "-n", "-u", "-s", "-v", "-o"], "words": [], "help": "WPS AP discovery."},
    "airodump-ng": {"flags": ["-c", "-w", "-a", "-b", "-M", "-I", "--manufacturer"], "words": [], "help": "WiFi observation."},
    "airmon-ng": {"flags": ["start", "stop", "check"], "words": [], "help": "Monitor mode."},
    "airdecap-ng": {"flags": ["-b", "-e", "-p", "-k", "-w"], "words": [], "help": "Capture decryption."},
    "proxychains": {"flags": ["-q", "-f"], "words": [], "help": "Proxy-wrapped execution."},
    "proxychains4": {"flags": ["-q", "-f"], "words": [], "help": "Proxy-wrapped execution."},
    "linpeas": {"flags": ["-a", "-s", "-o", "-q", "-m", "-d", "-v"], "words": [], "help": "Priv-esc enumeration."},
    "journalctl": {"flags": ["--no-pager", "-n", "-u", "-p", "-g", "-f"], "words": [], "help": "Systemd journal."},
    "tail": {"flags": ["-n", "-f"], "words": [], "help": "File tail."},

    # ========================================================================
    # Development / repair tools.  These exist so the on-screen error doctor can
    # inspect a broken build through the SAME validated lane as everything else,
    # instead of getting its own subprocess escape hatch.
    #
    # They are deliberately inspection-only.  Tools that mutate a repository
    # (git add/commit/reset/clean), run project scripts (npm run, node file.js,
    # plain make) or install anything are NOT here: those need a human, and the
    # doctor prints the exact command for the user to run instead of running it.
    # ========================================================================
    "git": {
        "flags": ["--oneline", "--porcelain", "--stat", "--short", "--name-only",
                  "--name-status", "--no-color", "--no-pager", "--cached", "--staged",
                  "--follow", "--untracked-files", "--version", "--no-renames",
                  "--diff-filter", "--max-count", "--get", "--list", "-n", "-l",
                  "--abbrev-ref", "--show-toplevel", "--quiet"],
        "value_flags": ["--diff-filter", "--max-count", "--get", "-n", "-l",
                        "--abbrev-ref"],
        "words": [],
        "verbs": ["status", "diff", "log", "show", "branch", "rev-parse",
                  "ls-files", "blame", "describe", "remote", "config", "shortlog",
                  "reflog", "symbolic-ref", "count-objects", "--version"],
        "help": "Read-only repository inspection (no add/commit/push/reset/clean).",
    },
    "pytest": {
        "flags": ["-x", "-q", "-v", "-k", "-s", "-m", "-r", "-p", "--tb",
                  "--maxfail", "--no-header", "--no-summary", "--collect-only", "--co",
                  "--timeout", "--durations", "--no-cov", "--color"],
        "words": [],
        "help": "Run the project's own test suite (executes project code).",
    },
    "rg": {
        "flags": ["-n", "-i", "-w", "-c", "-l", "-o", "-v", "-A", "-B", "-C",
                  "-S", "-F", "-m", "-t", "-g", "--hidden", "--no-ignore", "--type",
                  "--glob", "--json", "--vimgrep", "--fixed-strings", "--max-count",
                  "--files", "--stats", "--count-matches", "--no-heading", "--version"],
        "words": [],
        "help": "Search file contents (read-only).",
    },
    "jq": {
        "flags": [".", "-r", "-c", "-e", "-n", "-j", "-s", "-R", "--tab",
                  "--compact-output", "--raw-output", "--arg", "--argjson",
                  "--exit-status", "--join-output", "--version"],
        "words": [],
        "help": "JSON inspection (read-only).",
    },
    "tsc": {
        "flags": ["--noEmit", "--pretty", "--project", "-p", "--version", "-v",
                  "--noColor", "--diagnostics", "--listFilesOnly"],
        "words": [],
        "help": "TypeScript typecheck (--noEmit: nothing is written).",
    },
    "node": {
        "flags": ["--version", "-v", "--check", "--no-warnings"],
        "value_flags": ["--check"],   # node --check <file> parses without running
        "words": [],
        "verbs": [],   # a bare script path is otherwise refused: never runs a file here
        "help": "Node version and syntax check (never executes a script).",
    },
    "npm": {
        "flags": ["--version", "-v", "--json", "--depth", "--long", "--parseable",
                  "--global", "-g", "--all", "--omit"],
        "words": [],
        "verbs": ["ls", "list", "outdated", "cache"],
        "help": "Inspect the npm environment (no install, no run).",
    },
    "pip": {
        "flags": ["--version", "-V", "--format", "-f", "--outdated", "-o", "--user",
                  "--disable-pip-version-check", "--no-color"],
        "value_flags": ["--format", "-f", "--disable-pip-version-check", "--no-color"],
        "words": [],
        "verbs": ["list", "show", "freeze", "check"],
        "help": "Inspect installed Python packages (no install).",
    },
    "pip3": {
        "flags": ["--version", "-V", "--format", "-f", "--outdated", "-o", "--user",
                  "--disable-pip-version-check", "--no-color"],
        "value_flags": ["--format", "-f", "--disable-pip-version-check", "--no-color"],
        "words": [],
        "verbs": ["list", "show", "freeze", "check"],
        "help": "Inspect installed Python packages (no install).",
    },
}

RAW_FRAGMENT_FORBIDDEN = {"sh", "bash", "sudo", "python", "python3", "zsh"}


def available_locally() -> dict[str, bool]:
    """Return which allowlisted binaries exist on this machine right now."""
    return {name: shutil.which(name) is not None for name in ALLOWLIST}


# A few Kali package names collide with unrelated tools.  ``httpx`` on PATH is
# the Python HTTP client, while ProjectDiscovery's web prober installs as
# ``httpx-toolkit``.  Dispatching the wrong one yields plausible-looking output
# that means nothing, so the flavour is checked before a binary is trusted.
BINARY_ALTERNATIVES: dict[str, tuple[str, ...]] = {"httpx": ("httpx-toolkit",)}


def _is_script_binary(path: str) -> bool:
    """True when ``path`` is a script (Python console entry point, shell)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def resolve_binary(tool: str) -> tuple[Optional[str], str]:
    """Return a trustworthy executable for ``tool`` and a note on failure.

    Falls through to a known-good alternative name when the tool on PATH is a
    same-named stranger, which is how the Python ``httpx`` client used to get
    dispatched as if it were ProjectDiscovery's prober.
    """
    alternatives = BINARY_ALTERNATIVES.get(tool, ())
    binary = shutil.which(tool)
    if not alternatives:
        # No known name collision: whatever PATH resolves to is the tool.
        if binary is None:
            return None, f"{tool!r} is not installed on this machine"
        return binary, ""
    if binary is not None and not _is_script_binary(binary):
        return binary, ""
    for alternative in alternatives:
        found = shutil.which(alternative)
        if found is not None:
            return found, ""
    wanted = alternatives[0]
    return None, (
        f"{tool!r} is not installed as ProjectDiscovery's prober on this machine; "
        f"the {tool!r} on PATH is the Python HTTP client. Install {wanted} to run "
        "this locally"
    )


def local_count() -> int:
    return sum(1 for ok in available_locally().values() if ok)


def _flag_allowed(tool: str, arg: str) -> bool:
    """Match one argument against the tool's allowlist entry."""
    entry = ALLOWLIST[tool]
    flags, words = entry["flags"], entry["words"]
    if arg in flags or arg in words:
        return True
    if "=" in arg and not arg.startswith("+"):
        if arg.split("=", 1)[0] in flags:
            return True
    if arg.startswith("-") and not arg.startswith("--"):
        for allowed in flags:
            if len(allowed) == 2 and arg.startswith(allowed):
                return True
    return False


# A pipe character is inert here because execution never goes through a shell
# (subprocess is called with an argument list).  A few tools still need one in a
# *value* -- ROPgadget's --only "pop|ret" being the canonical case -- so pipes are
# permitted only immediately after one of these recognised value-taking flags.
INERT_PIPE_VALUE_FLAGS: dict[str, set[str]] = {
    "ROPgadget": {"--only", "--re"},
}


def validate_args(tool: str, args: list[str]) -> list[str]:
    """Validate every argument for a tool; raise ValueError on violations."""
    if tool in RAW_FRAGMENT_FORBIDDEN:
        raise ValueError(f"{tool!r} is not permitted: raw shell execution is disabled.")
    if tool not in ALLOWLIST:
        raise ValueError(f"Command {tool!r} is not in the local allowlist.")
    if not isinstance(args, list) or len(args) > 48:
        raise ValueError("args must be a list of at most 48 strings.")
    pipe_value_flags = INERT_PIPE_VALUE_FLAGS.get(tool, set())
    # Tools that take a subcommand (git, npm, pip) declare which ones are
    # permitted.  The first bare argument is the subcommand and must be on that
    # list; without this, `git clean` would pass because bare words were not
    # checked at all.  Tools without a "verbs" key are unaffected.
    entry = ALLOWLIST[tool]
    verbs = entry.get("verbs")
    value_flags = set(entry.get("value_flags", ()))
    bare_seen = 0
    clean: list[str] = []
    for index, arg in enumerate(args):
        if not isinstance(arg, str) or not arg:
            raise ValueError("All args must be non-empty strings.")
        # `|` is only tolerated as the value of a known flag; every other
        # shell metacharacter is always rejected.
        pipe_allowed = index > 0 and args[index - 1] in pipe_value_flags
        forbidden = "\n\r;&`$" if pipe_allowed else "\n\r;|&`$"
        if len(arg) > 512 or any(ch in arg for ch in forbidden):
            raise ValueError(f"Argument rejected: {arg[:80]}")
        if arg.startswith("-") or arg.startswith("+"):
            if not _flag_allowed(tool, arg):
                raise ValueError(f"Option {arg!r} is not permitted for {tool!r}.")
        elif index > 0 and args[index - 1] in value_flags:
            # A value belonging to a declared value-flag (e.g. node --check
            # main.js) is data, not a subcommand, so it does not consume the
            # verb slot.  Without this, `node --check file.js` would be refused
            # as an unknown subcommand -- a hole that broke real use.
            pass
        else:
            bare_seen += 1
            if verbs is not None and bare_seen == 1 and arg not in verbs:
                allowed = ", ".join(verbs) or "(none)"
                raise ValueError(
                    f"{tool!r} does not permit the subcommand {arg!r}; allowed: {allowed}"
                )
        clean.append(arg)
    return clean


def _hydra_style_timeout(tool: str, args: list[str], requested: int) -> int:
    """Extend the timeout for tools known to run long."""
    if tool in {"nmap", "masscan", "autorecon"}:
        return max(requested, 900)
    return max(TIMEOUT_MIN, min(requested, TIMEOUT_MAX))


def run(tool: str, args: Optional[list[str]] = None,
        timeout: int = TIMEOUT_DEFAULT) -> dict[str, Any]:
    """Validate and execute one allowlisted tool locally.

    Returns a uniform result dict; never raises past the caller for
    execution failures.
    """
    tool = str(tool or "").strip()
    try:
        args = validate_args(tool, list(args or []))
    except ValueError as exc:
        return {"error": str(exc), "stdout": "", "stderr": "", "return_code": -1, "success": False}
    binary, problem = resolve_binary(tool)
    if binary is None:
        return {
            "error": problem,
            "stdout": "", "stderr": "", "return_code": -1, "success": False,
        }
    effective = _hydra_style_timeout(tool, args, int(timeout))
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [binary, *args], capture_output=True, text=True,
            timeout=effective, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": f"{tool} timed out after {effective}s",
            "stdout": "", "stderr": "", "return_code": -1,
            "success": False, "timed_out": True,
        }
    except OSError as exc:
        return {"error": f"{tool} failed to run: {exc}", "stdout": "", "stderr": "",
                "return_code": -1, "success": False}
    return {
        "stdout": completed.stdout[:20000],
        "stderr": completed.stderr[:20000],
        "return_code": completed.returncode,
        "success": completed.returncode == 0,
        "execution_time": round(time.monotonic() - started, 2),
        "lane": "local",
    }


def smart_nmap_args(
    target: str,
    ports: str = "",
    scan_type: str = "",
    service_detection: bool = False,
) -> list[str]:
    """Fast, hard-bounded nmap defaults for every pipeline stage.

    Two rules keep scans fast and stop the "stuck at 4 B/s" symptom:

    1.  Never ask for service versions on ports we have not already found
        open.  Routers and IoT devices tarpit ``-sV`` probes (they accept the
        connection, then never answer), so nmap waits out its own timeout
        while printing almost nothing.  A progress bar that divides output
        bytes by elapsed seconds reads single digits for minutes -- it is
        measuring silence, not throughput.
    2.  Every invocation carries ``--max-retries 1``, ``--host-timeout 25s``
        and ``--open``, so even a hostile target can only waste 25 seconds.

    ``service_detection`` is only meaningful together with explicit ``ports``
    (a targeted follow-up on ports already known to be open).  Service naming
    for the general case comes from :func:`grab_banner` instead.
    """
    args = ["-Pn", "-n", "-T4", "--max-retries", "1", "--host-timeout", "25s", "--open"]
    wanted = str(scan_type or "").strip()
    if service_detection and ports:
        args.extend(["-sV", "--version-intensity", "2"])
        for flag in wanted.split():
            if flag not in {"-sV", "-sC", "-A", "-O"}:
                args.append(flag)
    else:
        for flag in wanted.split():
            if flag not in {"-sV", "-sC", "-A", "-O"}:
                args.append(flag)
        args.extend(["--top-ports", "100"] if not ports else [])
    if ports:
        args.extend(["-p", str(ports)])
    args.append(target)
    return args


def grab_banner(host: str, port: int, timeout: float = 2.0) -> str:
    """Grab one service banner with a short socket read.

    Router/IoT devices tarpit nmap's ``-sV`` probes for minutes; a direct
    connect + short read gets the same (often better) identification in
    about two seconds.  Read-only, no injection of anything but a newline.
    """
    import socket as _socket
    try:
        with _socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                sock.sendall(b"\r\n")
            except OSError:
                pass
            data = sock.recv(256)
        return data.decode("utf-8", "replace").strip()
    except OSError:
        return ""


def is_valid_ip_or_cidr(value: str) -> bool:
    """True when value is a plain IPv4 address or a /nn CIDR range."""
    value = str(value or "").strip()
    if _IP.match(value):
        return True
    if "/" in value and value.count("/") == 1:
        base, _, prefix = value.partition("/")
        return bool(_IP.match(base) and prefix.isdigit() and 0 <= int(prefix) <= 32)
    return False
