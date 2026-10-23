import time
from .telnet_node import run_parallel
from .utils import remove_redundant_prompt, clean_configuration, remove_banner_sections, extract_prompt, ensure_start_end
from .device_profile import IS_SONIC, RUNNING_CONFIG_CMD, sonic_wrap, sonic_wrap_batch

import re


_RO_IP = re.compile(r'^ip\s+(-br\s+|-4\s+)*(addr|link|route)(\s+show)?\s*$')

def _is_validation(cmd):
    c = cmd.strip()
    if c.startswith(("show", "ping", "traceroute")):
        return True
    if IS_SONIC:
        if c.startswith(('sudo vtysh -c "show', "sudo vtysh -c 'show")):
            return True
        if _RO_IP.match(c):
            return True
    return False

class AgentInterface:
    def __init__(self, parent):
        self.parent = parent

    def _unknown_devices(self, names):
        """Return an error observation if any device name isn't in the project, else None."""
        if isinstance(names, str):
            names = [names]
        valid = sorted(n.name for n in self.parent.project.nodes)
        bad = [n for n in names if n not in valid]
        if bad:
            return {"status": "Failed",
                    "observation": f"Unknown device(s): {bad}. Valid devices: {valid}"}
        return None

    def get_current_config(self, node_names):
        if isinstance(node_names, str):
            node_names = [node_names]
        err = self._unknown_devices(node_names)
        if err:
            return err
        result = {"status": "Success", "observation": {}}

        def worker(node_name):
            if IS_SONIC:
                tn = self.parent._health_check_node(node_name)
                if not tn:
                    return ""
                ifaces = self.parent.send_command_to_node(node_name, "show runningconfiguration interfaces")
                routes = self.parent.send_command_to_node(node_name, RUNNING_CONFIG_CMD)
                return remove_redundant_prompt(ifaces) + "\n" + remove_redundant_prompt(routes)
            cfg = self.parent.node_current_configs.get(node_name)
            if cfg != None and cfg.get("is_set") == True:
                return cfg["running-config"]

            tn = self.parent._health_check_node(node_name)
            if not tn:
                return ""
            if not IS_SONIC:
                self.parent.send_command_to_node(node_name, "enable")
            running_config = self.parent.send_command_to_node(node_name, RUNNING_CONFIG_CMD)
            running_config = remove_redundant_prompt(running_config)
            running_config = clean_configuration(remove_banner_sections(running_config))
            self.parent.node_current_configs[node_name] = {"is_set": True, "running-config": running_config}
            return running_config

        outputs = run_parallel(node_names, worker)
        for node_name, running_config in outputs.items():
            if running_config != "":
                result["observation"].update({node_name: running_config})
        return result

    def apply_config(self, config_commands):
        node_names = list(config_commands.keys())
        err = self._unknown_devices(node_names)
        if err:
            return err
        result = {"status": "Success", "observation": {}}

        def worker(node_name):
            cfg = self.parent.node_current_configs.get(node_name)
            if cfg != None and cfg["is_set"] == True:
                cfg["is_set"] = False

            node_commnads_output = ""
            command_result = ""
            tn = self.parent._health_check_node(node_name)

            if tn:
                raw = config_commands[node_name]
                lines = raw if isinstance(raw, list) else raw.split("\n")
                if IS_SONIC:
                    commands = sonic_wrap_batch(lines)      # whole list at once
                else:
                    self.parent.send_command_to_node(node_name, "enable")
                    commands = ensure_start_end(lines, "configure terminal", "end")
                for line in commands:
                    command_result += self.parent.send_command_to_node(node_name, line)

                prompt = extract_prompt(command_result)
                command_result = prompt + remove_redundant_prompt(command_result)
                node_commnads_output += command_result

            return node_commnads_output

        outputs = run_parallel(list(config_commands.keys()), worker)
        for node_name, node_commnads_output in outputs.items():
            result["observation"].update({node_name: node_commnads_output})
        self.parent.last_update_time = time.time()
        return result

    def execute_validation(self, validation_commands):
        node_names = list(validation_commands.keys())
        err = self._unknown_devices(node_names)
        if err:
            return err
        result = {"status": "Success", "observation": {}}

        def worker(node_name):
            node_commnads_output = ""
            command_result = ""
            tn = self.parent._health_check_node(node_name)

            if tn:
                for command in validation_commands[node_name].split("\n"):
                    if not _is_validation(command):
                        return "You are only allowed to use validation commands such as show, ping, and traceroute."
                    command_result += self.parent.send_command_to_node(node_name, command.strip())

                prompt = extract_prompt(command_result)
                command_result = prompt + remove_redundant_prompt(command_result)
                node_commnads_output += command_result

            return node_commnads_output

        outputs = run_parallel(list(validation_commands.keys()), worker)
        for node_name, node_commnads_output in outputs.items():
            result["observation"].update({node_name: node_commnads_output})
        return result

    def wait_for_stabilization(self, seconds):
        time.sleep(seconds)
        return {"status": "Success", "observation": f"Waited {seconds} seconds for stabilization"}

    def submit(self):
        return {"status": "Finished"}

    # --- Action aliases -----------------------------------------------------
    def get_running_config(self, node_names):
        return self.get_current_config(node_names)

    def wait(self, seconds):
        return self.wait_for_stabilization(seconds)