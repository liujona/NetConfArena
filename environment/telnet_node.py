import time
import gns3fy
import re
import os
import socket
import select
from concurrent.futures import ThreadPoolExecutor
from logs.mylogger import logger
from .device_profile import PROMPT_RE, LOGIN_USER, LOGIN_PASSWORD, INIT_COMMANDS, CLI_TIMEOUT


def run_parallel(items, worker, max_workers=None):
    """Run worker(item) for each item concurrently — one thread per node.

    send_command on a single telnet connection must stay sequential (shared
    socket), so callers must only ever schedule one item per node. Different
    nodes are fully independent and run in parallel here.

    Returns {item: result}. The first exception raised by any worker is
    re-raised after every worker has finished.
    """
    items = list(items)
    if not items:
        return {}
    results = {}
    first_error = None
    with ThreadPoolExecutor(max_workers=max_workers or len(items)) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        for future, item in futures.items():
            try:
                results[item] = future.result()
            except Exception as e:
                logger.warning(f"Parallel worker for {item!r} failed: {e}")
                if first_error is None:
                    first_error = e
    if first_error is not None:
        raise first_error
    return results


class TelnetClient:
    """Minimal Telnet client using stdlib socket + select, replacing telnetlib."""
    _IAC  = 0xFF
    _DONT = 0xFE
    _DO   = 0xFD
    _WONT = 0xFC
    _WILL = 0xFB

    def __init__(self, host: str, port: int, timeout: float = 10):
        self.host = host
        self.port = port
        self.eof = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self.sock.settimeout(None)  # blocking for writes

    def _process(self, data: bytes) -> bytes:
        """Strip IAC sequences and respond to option negotiations."""
        result = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == self._IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (self._DO, self._DONT, self._WILL, self._WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == self._DO:
                        self.sock.send(bytes([self._IAC, self._WONT, opt]))
                    elif cmd == self._WILL:
                        self.sock.send(bytes([self._IAC, self._DONT, opt]))
                    i += 3
                elif cmd == self._IAC:
                    result.append(self._IAC)
                    i += 2
                else:
                    i += 2
            else:
                result.append(b)
                i += 1
        return bytes(result)

    def read_very_eager(self) -> bytes:
        chunks = []
        try:
            while select.select([self.sock], [], [], 0)[0]:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.eof = True
                    break
                chunks.append(chunk)
        except Exception:
            self.eof = True
        return self._process(b''.join(chunks))

    def write(self, data: bytes) -> None:
        if self.eof:
            raise ConnectionError(f"Telnet connection to {self.host}:{self.port} is closed")
        self.sock.sendall(data.replace(b'\xff', b'\xff\xff'))

    def close(self) -> None:
        self.eof = True
        try:
            self.sock.close()
        except Exception:
            pass

# These were for SONiC specific 
# _INIT_COMMANDS = (
#     "enable",
#     "terminal length 0",
#     "terminal width 0",
#     "conf t",
#     "no ip domain lookup",
#     "no logging console",
#     "end",
# )


def connect_to_all_nodes(project):
    # Collect the consoled nodes first, then connect to all of them in parallel:
    # each node owns its own socket, so there is nothing to serialize between them.
    node_map = {}
    for node in project.nodes:
        if node.console_type == "none":
            logger.debug(f"Node {node.name} does not have a console port.")
            continue
        if node.console_host == "0.0.0.0":
            node.console_host = os.environ.get('GNS3_SERVER', '127.0.0.1')
        node_map[node.name] = node

    def worker(node_name):
        node = node_map[node_name]
        max_retries = 3
        retry_delay = 5
        tn = None
        for attempt in range(max_retries):
            tn = connect_to_node(node.console_host, node.console, 10, retry_delay)
            if tn is not None:
                break
            if attempt < max_retries - 1:
                logger.debug(f"Retrying {node_name} in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)

        if tn is None:
            logger.warning(f"Failed to connect to {node_name} after {max_retries} attempts")
            return None

        for command in INIT_COMMANDS:
            send_command(command, tn)
        return tn

    results = run_parallel(list(node_map), worker)
    return {name: tn for name, tn in results.items() if tn is not None}

def connect_to_node(host, port, timeout=10, retry_delay=5, cli_timeout=CLI_TIMEOUT):
    try:
        tn = TelnetClient(host, port, timeout)
        logger.debug(f"Connecting to {host}:{port}...")

        CLI_entered = False
        start_time = time.time()
        output = ""
        login_sent = 0

        while not CLI_entered:
            if time.time() - start_time > cli_timeout:
                raise TimeoutError("Timed out waiting for the CLI prompt")

            chunk = tn.read_very_eager().decode('ascii', errors='ignore')
            if chunk:
                output += chunk

            # SONiC consoles ask for credentials. Answer at most twice in total
            # so a wrong password cannot lock the account.
            if LOGIN_USER is not None and login_sent < 2:
                if re.search(r'login:\s*$', output):
                    tn.write(LOGIN_USER.encode() + b"\r")
                    output, login_sent = "", login_sent + 1
                    time.sleep(retry_delay)
                    continue
                if re.search(r'Password:\s*$', output):
                    tn.write(LOGIN_PASSWORD.encode() + b"\r")
                    output, login_sent = "", login_sent + 1
                    time.sleep(retry_delay)
                    continue

            if re.search(PROMPT_RE, output, flags=re.MULTILINE):
                logger.debug("Successfully entered CLI!")
                CLI_entered = True
            else:
                tn.write(b"\r")
                time.sleep(retry_delay)

        return tn

    except Exception as e:
        logger.debug(f"Failed to connect to {host}:{port}: {e}")
        return None

    except Exception as e:
        logger.debug(f"Failed to connect to {host}:{port}: {e}")
        return None

def send_command(command, tn, timeout=60):
    # command_prompts = r'^[A-Za-z0-9_\-]+(?:\([A-Za-z0-9_\-]+\))?[>#]\s?$'

    command_prompts = PROMPT_RE

    # Phase 1: drain any leftover output from prior commands.
    # A single empty read is insufficient because read_very_eager is
    # non-blocking and can return empty between packets. Require N consecutive
    # empty reads before treating the buffer as drained.
    consecutive_empty = 0
    drain_start = time.time()
    while consecutive_empty < 3:
        if getattr(tn, "eof", False):
            logger.debug(f"Commad execution failed: connection closed before command '{command}'")
            return None
        chunk = tn.read_very_eager().decode('ascii', errors='ignore')
        if chunk == "":
            consecutive_empty += 1
        else:
            consecutive_empty = 0
        time.sleep(0.1)
        if time.time() - drain_start > timeout:
            logger.debug(f"Drain phase timed out before command '{command}'")
            break

    try:
        # Phase 2: send the command, anchor on this command's echo, and look for
        # a prompt only after that echo. This avoids matching a stale prompt
        # from the previous command and returning prematurely.
        start_time = time.time()
        tn.write(command.encode('ascii') + b"\r")
        output = ""
        echo_seen = False
        echo_end = 0
        last_kick = start_time
        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Command '{command}' timed out after {timeout}s")
            if getattr(tn, "eof", False):
                raise ConnectionError(f"Connection closed while executing '{command}'")
            time.sleep(0.5)
            output += tn.read_very_eager().decode('ascii', errors='ignore')

            if not echo_seen:
                idx = output.find(command)
                if idx >= 0:
                    echo_seen = True
                    echo_end = idx + len(command)

            if echo_seen:
                match = re.search(command_prompts, output[echo_end:], flags=re.MULTILINE)
                if match:
                    return output

            # Send Enter every five seconds until both echo and prompt appear.
            if time.time() - last_kick > 5:
                tn.write(b"\r")
                last_kick = time.time()
    except Exception as e:
        logger.debug(f"Commad execution failed: {e}")
        return None

if __name__ == "__main__":
    GNS3_SERVER_IP = "127.0.0.1"
    gns3_connector = gns3fy.Gns3Connector(f"http://{GNS3_SERVER_IP}:3080")

    project = gns3fy.Project(name="inter-vlan-routing", connector=gns3_connector)

    try:
        project.get()
        project.open()
    except Exception as e:
        print(f"Error: {e}")
    
    if project.status != "opened":
        exit(1)
    
    device_name = "router"
    command = "show running-config"
    for node in project.nodes:
        if node.name != device_name:
            continue
        tn = connect_to_node(node.console_host, node.console)
        if tn:
            print(f"Executing: {command}")
            print("Command execution result:")
            print(send_command(command, tn))
