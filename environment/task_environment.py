import time
import os
import threading
from .agent_interface import AgentInterface
from .evaluator_interface import EvaluatorInterface
from .gns3_project import delete_project, create_new_project, create_topology
from .telnet_node import connect_to_all_nodes, connect_to_node, send_command, run_parallel
from .utils import remove_redundant_prompt, clean_configuration, remove_banner_sections, clean_command_output
from logs.mylogger import logger

import re
from .device_profile import IS_SONIC, INIT_COMMANDS, RUNNING_CONFIG_CMD, sonic_wrap, sonic_wrap_batch

class TaskEnvironment:
    def __init__(self, task_data):
        # Open the corresponding GNS3 project in the emulator.
        self.task_data = task_data
        self.project_name = (f"SONiC-{self.task_data['scenario']}" if IS_SONIC else self.task_data["scenario"])
        self.last_update_time = time.time()
        self.node_current_configs = {}
        self.tns = {}
        # One re-entrant lock per node. Anything that touches a node's telnet
        # connection (send_command / health-check / reconnect) holds this lock,
        # so different nodes run concurrently while a single node stays serial.
        self._node_locks = {}
        self._node_locks_guard = threading.Lock()
        self.agent_interface = AgentInterface(self)
        self.evaluator_interface = EvaluatorInterface(self)
        
        # Open the GNS3 project.
        delete_project(self.project_name)
        self.project = create_new_project(self.project_name)

        try:
            create_topology(self.project, self.task_data["topology"])
            self.project.start_nodes()
            self.tns = connect_to_all_nodes(self.project)

            if IS_SONIC:
                self.wait_for_sonic_ready()

            self.apply_config(self.task_data['startup_configs'], strict=True)
            self.evaluator_interface.node_startup_configs = self.evaluator_interface.get_all_node_current_configs()
        except BaseException:
            self.close()      # stop nodes and close the project even on failure
            raise

        logger.info(f"Task environment for task {self.task_data['scenario']} has been deployed successfully.")

    def apply_config(self, configs, strict=False):
        def worker(node_name):
            self.node_current_configs.pop(node_name, None)
            if self._health_check_node(node_name) is None:
                return
            cmds = list(configs[node_name])
            if IS_SONIC:
                cmds = sonic_wrap_batch(cmds)
            else:
                if not cmds or cmds[0] != "configure terminal":
                    cmds.insert(0, "configure terminal")
                if cmds[-1] != "end":
                    cmds.append("end")
                self.send_command_to_node(node_name, "enable")

            for line in cmds:
                out = self.send_command_to_node(node_name, line) or ""
                if not IS_SONIC:
                    continue

                # FRR can refuse commands for a few seconds after boot. Retry those.
                for _ in range(3):
                    if "failed to connect" not in out:
                        break
                    time.sleep(5)
                    out = self.send_command_to_node(node_name, line) or ""

                if not re.search(r'Error|failed|Exiting|Usage:|^%', out, re.M):
                    continue
                # Removing an address that is already gone is not a failure.
                if re.search(r'does not exist|not found|No such', out, re.IGNORECASE):
                    continue

                msg = f"{node_name}: `{line}` -> {out.strip()[-200:]}"
                if strict:
                    raise RuntimeError(msg)
                logger.warning(msg)

        # run_parallel re-raises the first worker error after all nodes finish.
        run_parallel(list(configs.keys()), worker)

    def get_all_node_current_configs(self):
        # Fetch every node's current configuration, refreshing cached values.
        all_node = []

        for node in self.get_connections():
            all_node.append(node)
            if self.node_current_configs.get(node) != None and self.node_current_configs[node].get("is_set") == True:
                self.node_current_configs[node]["is_set"] = False

        def worker(node_name):
            if self.tns.get(node_name) is None:
                return
            running_config = self._fetch_complete_running_config(node_name)
            self.node_current_configs[node_name] = {"is_set" : True, "running-config" : running_config}

        # Fetch every node's running-config in parallel.
        run_parallel(all_node, worker)

        all_node_current_configs = {node: configs["running-config"] for node, configs in self.node_current_configs.items() if configs.get("is_set")}
        return all_node_current_configs

    def _fetch_complete_running_config(self, node_name, max_attempts=3, settle_delay=1.0):
        # The device Telnet pipeline may still be processing commands, causing
        # show running-config to return truncated output. A complete IOS config
        # must contain a hostname line and a final end marker; retry otherwise.
        cleaned = ""
        for attempt in range(max_attempts):

            if attempt > 0:
                time.sleep(settle_delay)

            if not IS_SONIC:
                self.send_command_to_node(node_name, "enable")
            raw = self.send_command_to_node(node_name, RUNNING_CONFIG_CMD)
            raw = remove_redundant_prompt(raw)
            cleaned = clean_configuration(remove_banner_sections(raw))
            if IS_SONIC:
                complete = bool(cleaned.strip())
            else:
                lines = cleaned.split("\n")
                complete = (any(l.strip() == "end" for l in lines)
                            and any(l.startswith("hostname ") for l in lines))
            if complete:
                return cleaned


            lines = cleaned.split("\n")
            has_end = any(l.strip() == "end" for l in lines)
            has_hostname = any(l.startswith("hostname ") for l in lines)
            if has_end and has_hostname:
                if attempt > 0:
                    logger.info(f"Got complete running-config from {node_name} on attempt {attempt+1}")
                return cleaned
            logger.warning(
                f"Incomplete running-config from {node_name} "
                f"(attempt {attempt+1}/{max_attempts}): has_end={has_end} has_hostname={has_hostname} bytes={len(cleaned)}"
            )
        logger.error(f"Failed to fetch complete running-config from {node_name} after {max_attempts} attempts; using best-effort output")
        return cleaned
    
    
    def _node_lock(self, node_name):
        # Re-entrant so a thread already holding a node's lock (e.g. inside
        # send_command_to_node) can call _health_check_node / reconnect_node.
        with self._node_locks_guard:
            lock = self._node_locks.get(node_name)
            if lock is None:
                lock = threading.RLock()
                self._node_locks[node_name] = lock
            return lock

    def _health_check_node(self, node_name):
        # Health-check (and if needed reconnect) a single node. Holding the
        # node lock keeps concurrent workers on the same node from racing.
        with self._node_lock(node_name):
            tn = self.tns.get(node_name)
            try:
                if tn is None or getattr(tn, "eof", False):
                    raise ConnectionError("Telnet connection missing or marked closed")
                tn.write(b"\r")
                _ = tn.read_very_eager()
                if getattr(tn, "eof", False):
                    raise ConnectionError("Telnet connection closed during health check")
                return tn
            except Exception:
                logger.debug(f"Connection to {node_name} is unhealthy, reconnecting...")
                return self.reconnect_node(node_name)

    def get_connections(self):
        for node_name in list(self.tns.keys()):
            self._health_check_node(node_name)
        return self.tns

    def reconnect_node(self, node_name):
        with self._node_lock(node_name):
            old_tn = self.tns.pop(node_name, None)
            if old_tn:
                old_tn.close()

            node = next((node for node in self.project.nodes if node.name == node_name), None)
            if node is None:
                raise KeyError(f"Node {node_name} not found in project")
            if node.console_type == "none":
                logger.warning(f"Node {node.name} does not have a console port.")
                return None

            if node.console_host == "0.0.0.0":
                node.console_host = os.environ.get('GNS3_SERVER', '127.0.0.1')

            retry_delay = 1
            for attempt in range(3):
                tn = connect_to_node(node.console_host, node.console, timeout=10, retry_delay=retry_delay, cli_timeout=30)
                if tn is not None:
                    self.tns[node_name] = tn
                    # for command in ("enable", "terminal length 0", "terminal width 0", "conf t", "no ip domain lookup", "no logging console", "end"):
                    for command in INIT_COMMANDS:
                        if send_command(command, tn) is None:
                            logger.debug(f"Initialization command failed after reconnecting to {node_name}: {command}")
                            tn.close()
                            self.tns.pop(node_name, None)
                            break
                    else:
                        self.node_current_configs.pop(node_name, None)
                        return tn

                if attempt < 2:
                    time.sleep(retry_delay)

            logger.warning(f"Failed to reconnect to {node_name}")
            return None

    def send_command_to_node(self, node_name, command, retries=1):
        # Serial per node (node lock), parallel across nodes.
        with self._node_lock(node_name):
            for attempt in range(retries + 1):
                tn = self._health_check_node(node_name)

                if tn is not None:
                    result = send_command(command, tn)
                    if result is not None:
                        return result

                if attempt < retries:
                    logger.debug(f"Command failed on {node_name}, reconnecting and retrying: {command}")
                    self.reconnect_node(node_name)

            raise ConnectionError(f"Failed to execute command on {node_name}: {command}")

    
    def close(self):
        # Cleanup is best effort. A node or project teardown failure (for
        # example, gns3fy parsing an empty GNS3 close response) must not turn a
        # successfully evaluated task into a process-level failure.
        for tn in self.tns.values():
            try:
                tn.close()
            except Exception as e:
                logger.warning(f"Failed to close telnet connection during teardown: {e}")
        self.tns = {}
        try:
            self.project.stop_nodes()
        except Exception as e:
            logger.warning(f"Failed to stop nodes during teardown: {e}")
        try:
            self.project.close()
        except Exception as e:
            logger.warning(f"Failed to close project during teardown: {e}")


    def wait_for_sonic_ready(self, timeout=900):
        from .device_profile import SONIC_READY_CMD, SONIC_REQUIRED_CONTAINERS
        deadline = time.time() + timeout
        for node_name in list(self.tns.keys()):
            while True:
                out = self.send_command_to_node(node_name, SONIC_READY_CMD) or ""
                up = {line.split()[0] for line in out.splitlines() if " Up " in line}
                frr = self.send_command_to_node(node_name, 'sudo vtysh -c "show version"') or ""
                if set(SONIC_REQUIRED_CONTAINERS) <= up and "FRRouting" in frr:
                    logger.info(f"{node_name}: SONiC containers and FRR are up")
                    self.send_command_to_node(node_name, "sudo docker exec bgp /usr/lib/frr/ospfd -d -F traditional -A 127.0.0.1")
                    break
                if time.time() > deadline:
                    raise RuntimeError(
                        f"{node_name}: SONiC not ready after {timeout}s "
                        f"(containers up: {sorted(up)}, FRR seen: {'FRRouting' in frr})"
                    )
                time.sleep(10)