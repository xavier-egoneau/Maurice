"""Fail-closed shell command risk analyzer.

Before executing any shell.exec tool call, this module assesses the command's
risk level. Unknown or complex constructs are treated as too_complex, which
requires new explicit approval even when a saved approval already exists.

Risk levels:
  safe       — simple command, no detected hazard, no structural complexity
  elevated   — detected risk (dangerous pattern, structural complexity); needs approval
  critical   — detected high-severity hazard; needs approval with strong warning

Integration rule:
  - safe     → pass through normally
  - elevated → require new explicit approval (bypass saved approval)
  - critical → require new explicit approval with a prominent warning
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParseResult:
    safe: bool
    risk_level: str     # "safe" | "elevated" | "critical"
    reason: str
    too_complex: bool   # True when the structure could not be fully analyzed


# --- pattern tables ---

_CRITICAL: list[tuple[str, str]] = [
    # fork bomb
    (r":\s*\(\s*\)\s*\{", "Fork bomb pattern."),
    # network fetch piped directly to interpreter
    (r"(curl|wget)\b[^|]*\|\s*(bash|sh|python\d*|perl|ruby|node|zsh|fish)\b",
     "Network fetch piped to interpreter."),
    # encoded payload piped to interpreter
    (r"base64\s+-d\b[^|]*\|\s*(bash|sh|python\d*|perl|ruby|node)\b",
     "Encoded payload piped to interpreter."),
    # overwrite /dev/sd* or /dev/nvme*
    (r">\s*/dev/(sd|nvme|hd|vd)", "Overwrite of block device."),
]

_ELEVATED: list[tuple[str, str]] = [
    # pipe to any shell or interpreter
    (r"\|\s*(bash|sh|zsh|fish|ksh|tcsh|csh|dash|python\d*|perl|ruby|node|php)\b",
     "Pipe to interpreter."),
    # recursive remove of root-level or home paths
    (r"\brm\b[^;|&]*-[a-zA-Z]*[rRfF][a-zA-Z]*\s+/", "Recursive remove from /."),
    (r"\brm\b[^;|&]*-[a-zA-Z]*[rRfF][a-zA-Z]*\s+~", "Recursive remove from ~."),
    (r"\brm\b[^;|&]*-[a-zA-Z]*[rRfF][a-zA-Z]*\s+\$HOME", "Recursive remove from $HOME."),
    # privilege escalation
    (r"\bsudo\b", "sudo usage."),
    (r"\bsu\s+-\b", "su - usage."),
    # block device operations
    (r"\bdd\b[^|]*\bof=/dev/", "dd writing to block device."),
    (r"\bdd\b[^|]*\bif=/dev/", "dd reading from block device."),
    # filesystem operations
    (r"\b(rm|unlink)\b", "File deletion."),
    (r"\bmkfs\b", "Filesystem creation."),
    (r"\bfdisk\b", "Partition editor."),
    (r"\bparted\b", "Partition editor."),
    (r"\bshred\b", "Secure file overwrite."),
    (r"\bwipefs\b", "Wipe filesystem signatures."),
    # redirects to sensitive directories
    (r">\s*/etc/", "Redirect to /etc/."),
    (r">>\s*/etc/", "Append to /etc/."),
    (r">\s*/usr/", "Redirect to /usr/."),
    (r">>\s*/usr/", "Append to /usr/."),
    (r">\s*/bin/", "Redirect to /bin/."),
    (r">>\s*/bin/", "Append to /bin/."),
    (r">\s*/sbin/", "Redirect to /sbin/."),
    (r">>\s*/sbin/", "Append to /sbin/."),
    (r">\s*/lib(64)?/", "Redirect to /lib/."),
    (r">>\s*/lib(64)?/", "Append to /lib/."),
    (r">\s*/boot/", "Redirect to /boot/."),
    (r">\s*/sys/", "Redirect to /sys/."),
    (r">\s*/proc/", "Redirect to /proc/."),
    (r">\s*/dev/", "Redirect to /dev/."),
    (r">>\s*/dev/", "Append to /dev/."),
    (r">\s*/root/", "Redirect to /root/."),
    (r">>\s*/root/", "Append to /root/."),
    (r">\s*~/\.ssh/", "Redirect to ~/.ssh/."),
    (r">\s*\$HOME/\.ssh/", "Redirect to ~/.ssh/."),
    # mutations in operating system locations
    (r"\b(rm|mv|cp|chmod|chown|touch|mkdir|tee)\b[^|;]*\s/(etc|usr|bin|sbin|lib|lib64|boot|sys|proc|dev|root)\b",
     "Mutation of operating system location."),
    # dotenv reads often expose credentials
    (r"\b(cat|less|more|head|tail|grep|rg|sed|awk)\b[^|;]*(^|\s)(\.env(\.\S*)?|\S+/\.env(\.\S*)?)\b",
     "Read of dotenv file."),
    # data exfiltration or network upload tools
    (r"\b(curl|wget)\b[^|;]*\s(-T|--upload-file|--post-file|-F|--form|-d|--data|--data-binary|--data-raw)\b",
     "Network data submission."),
    (r"\b(scp|sftp|ftp|rsync|rclone|nc|netcat|socat)\b", "Network transfer tool."),
    # dangerous permission changes
    (r"\bchmod\b[^|;]*\b(777|a\+w|o\+w|a\+x)\b", "World-writable or executable permission."),
    (r"\bchown\b[^|;]*\broot\b", "Ownership change to root."),
    # code execution constructs
    (r"\beval\b", "eval construct."),
    (r"\bexec\b\s+\w", "exec construct."),
    # package managers with scripts
    (r"\b(pip|pip3)\s+install\b.*--pre\b", "pip install pre-release."),
    (r"\bnpm\s+install\b.*-g\b", "npm global install."),
    # kill all or kill -9 1
    (r"\bkill\s+-9\s+1\b", "Kill PID 1."),
    (r"\bkillall\s+-9\b", "killall -9."),
    (r"\bpkill\s+-9\b", "pkill -9."),
]

# structural markers that make a command too complex to safely analyze
_COMPLEXITY: list[tuple[str, str]] = [
    # command substitution
    (r"\$\(", "Command substitution $(...)."),
    (r"`[^`]+`", "Command substitution with backticks."),
    # compound commands
    (r"(?<![|&])\|\|", "OR-conditional (||)."),
    (r"(?<![|])&&", "AND-conditional (&&)."),
    # background execution
    (r"&\s*$", "Background process (&)."),
    (r"&\s+\w", "Background execution."),
    # process substitution
    (r"<\s*\(", "Process substitution <(...)."),
    # here-doc
    (r"<<\s*\w", "Here-document (<<)."),
    # subshell
    (r"\([^)]{5,}\)", "Subshell expression."),
    # multiple statements via semicolon
    (r"[^;];[^;]", "Multiple statements (;)."),
]


def parse(command: str) -> ParseResult:
    """Analyze a shell command string and return a risk assessment."""
    if not command or not command.strip():
        return ParseResult(safe=True, risk_level="safe", reason="Empty command.", too_complex=False)

    cmd = command.strip()

    for pattern, reason in _CRITICAL:
        if re.search(pattern, cmd, re.IGNORECASE):
            return ParseResult(safe=False, risk_level="critical", reason=reason, too_complex=False)

    for pattern, reason in _COMPLEXITY:
        if re.search(pattern, cmd):
            return ParseResult(safe=False, risk_level="elevated", reason=reason, too_complex=True)

    for pattern, reason in _ELEVATED:
        if re.search(pattern, cmd, re.IGNORECASE):
            return ParseResult(safe=False, risk_level="elevated", reason=reason, too_complex=False)

    return ParseResult(safe=True, risk_level="safe", reason="No hazard detected.", too_complex=False)
