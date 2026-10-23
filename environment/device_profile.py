# environment/device_profile.py
import os
import shlex

_FRR_NOISE = {"configure terminal", "conf t", "end", "enable", "!"}

PLATFORM = os.environ.get("NETCONF_PLATFORM", "cisco").lower()
IS_SONIC = PLATFORM == "sonic"

if IS_SONIC:
    # SONiC shell prompt, e.g. "admin@sonic:~$ "
    PROMPT_RE = r'^[A-Za-z0-9_.\-]+@[A-Za-z0-9_.\-]+:[^\r\n]*[$#]\s?$'
    LOGIN_USER = os.environ.get("SONIC_USER", "admin")
    LOGIN_PASSWORD = os.environ.get("SONIC_PASSWORD", "")
    INIT_COMMANDS = ()
    CLI_TIMEOUT = 600  # SONiC-VS boots much slower than IOS
    RUNNING_CONFIG_CMD = 'sudo vtysh -c "show running-config"'
    ROUTE_SNAPSHOT_CMDS = ('sudo vtysh -c "show ip route"',)
    SONIC_READY_CMD = "sudo docker ps --format '{{.Names}} {{.Status}}'"
    SONIC_REQUIRED_CONTAINERS = ("database", "swss", "syncd", "bgp")
else:
    PROMPT_RE = r'^[A-Za-z0-9_\-]+(?:\([A-Za-z0-9_\-]+\))?[>#]\s?$'
    LOGIN_USER = None
    LOGIN_PASSWORD = None
    INIT_COMMANDS = ("enable", "terminal length 0", "terminal width 0", "conf t",
                     "no ip domain lookup", "no logging console", "end")
    CLI_TIMEOUT = 120
    RUNNING_CONFIG_CMD = "show running-config"
    ROUTE_SNAPSHOT_CMDS = ("show ip route", "show ip route vrf *")


def sonic_wrap_batch(lines):
    """config ...        -> one `sudo config ...` each
       sudo ...          -> passed through unchanged
       everything else   -> FRR lines, batched into ONE vtysh call so that
                            context ('router ospf 1' -> 'network ...') survives."""
    out, frr = [], []

    def flush():
        if frr:
            args = " ".join(f"-c {shlex.quote(l)}" for l in ["configure terminal", *frr])
            out.append(f"sudo vtysh {args}")
            frr.clear()

    for raw in lines:
        s = raw.strip()
        if not s or s in _FRR_NOISE:
            continue
        if s.startswith("config "):
            flush(); out.append("sudo " + s)
        elif s.startswith("sudo "):
            flush(); out.append(s)
        else:
            frr.append(s)
    flush()
    return out

def sonic_wrap(line):
    r = sonic_wrap_batch([line])
    return r[0] if r else ""