import os
import re
import ast
import json
from string import Template
from .base import BaseAgent, OutputParserError
from .utils import build_physical_topology
from logs.mylogger import logger

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")

class ReActAgent(BaseAgent):
    DEFAULT_MAX_ACTION_TURNS = 20

    def __init__(self, agent_name):
        super().__init__(agent_name)
        self.output_parser = ReActDiag()
        mode = self.name.split("___")[0]
        # self.load_config(os.path.join(CONFIG_DIR, f"{mode}.yaml"))
        from environment.device_profile import IS_SONIC
        cfg_name = f"{mode}_SONiC.yaml" if IS_SONIC else f"{mode}.yaml"
        self.load_config(os.path.join(CONFIG_DIR, cfg_name))
        
        self.max_action_turns = int(
            self.agent_config.get("agent", {}).get(
                "max_action_turns",
                self.DEFAULT_MAX_ACTION_TURNS,
            )
        )
        self.show_turn_budget_each_turn = bool(
            self.agent_config.get("agent", {}).get("show_turn_budget_each_turn", False)
        )
        self.system_prompt = Template(self.system_prompt).safe_substitute(
            max_action_turns=self.max_action_turns
        )

    def _turn_budget_prompt(self, completed_turn):
        remaining_turns = max(self.max_action_turns - completed_turn, 0)
        prompt = (
            f"Turn budget: completed action turn {completed_turn} of {self.max_action_turns}; "
            f"{remaining_turns} action turns remain."
        )
        if remaining_turns <= 3:
            prompt += (
                " The remaining turn budget is low; avoid non-essential validation, "
                "apply any final necessary configuration, and submit when the goals are satisfied."
            )
        return prompt

    def execute_task(self, task_index, interface, task_data):

        topology = json.dumps(build_physical_topology(task_data), indent=4)
        self.task_prompt = self.task_prompt_template.safe_substitute(
            scenario=task_data['scenario'],
            goals=task_data['goals'],
            topology=topology,
        )
        logger.debug(f"\n--------- Task Prompt ---------\n")
        logger.debug(f"{self.task_prompt}")

        # Build the message list.
        self.llm.messages.append(self.llm._construct_system_message(self.system_prompt))
        self.llm.messages.append(self.llm._construct_message(self.task_prompt))

        turn = 1
        while True:
            logger.debug(f"\n--------- Turns: {turn} ---------\n")

            thought = None
            action = None
            action_result = None
            
            for _ in range(3):
                new_message, answer = self.llm.parse()
                
                if "content" in new_message and new_message["content"] == "Failed to generate response, please reduce the length of the input prompt.":
                    logger.debug("Task execution stopped as the number of input tokens exceeds the limit.")
                    return

                if answer:
                    parsed_response = self.output_parser.parse(answer)

                    if parsed_response:
                        self.llm.messages.append(new_message)
                        logger.debug(f"{parsed_response}")

                        action_num = parsed_response.count("Action:")
                        if action_num > 1:
                            action_result = {
                                "status": "Failed",
                                "observation": f"Failed to execute the action, you can execute only one action at a time, retrying..."
                            }
                            break
                        thought_index = parsed_response.find("Thought:")
                        action_index = parsed_response.find("Action:")
                        # "Thought:" is optional; only "Action:" is required.
                        if action_index != -1:
                            if thought_index != -1 and thought_index < action_index:
                                thought = parsed_response[thought_index + len("Thought:"):action_index].strip()
                            else:
                                thought = None
                            action = parsed_response[action_index + len("Action:"):].strip()
                            try:
                                action_result = self.call_agent_network_interface(action, interface)
                                break
                            except SyntaxError as e:
                                action_result = {
                                    "status": "Failed",
                                    "observation": "Failed to parse the action, please pay attention to the format, the action must be a valid Python function call that adheres to standard Python syntax, retrying ..."
                                }
                                break
                            except Exception as e:
                                action_result = {
                                    "status": "Failed",
                                    "observation": f"Failed to execute the action, Error Type: {type(e).__name__}, Error Message: {e}, retrying..."
                                }
                                break
                        else:
                            logger.debug(f"[Format mismatch] thought_index={thought_index}, action_index={action_index}. Raw response:\n{parsed_response}")
                            action_result = {
                                "status": "Failed",
                                "observation": "No 'Action:' found in your response. Every response must contain exactly one 'Action:' line with an executable tool call, retrying..."
                            }
                            break
                    else:
                        logger.debug(f"[Empty parsed_response] Raw answer:\n{answer}")
                        self.llm.messages.append(new_message)
                        action_result = {
                            "status": "Failed",
                            "observation": "Your response was empty after parsing. You must respond with an 'Action:' line containing an executable tool call, retrying..."
                        }
                        break
                else:
                    logger.debug(f"[Empty answer] new_message content:\n{new_message.get('content', '')}")
                    if new_message.get('content', '').strip():
                        self.llm.messages.append(new_message)
                    action_result = {
                        "status": "Failed",
                        "observation": "Your response did not contain a usable reply. You must respond with an 'Action:' line containing an executable tool call, retrying..."
                    }
                    break
            
            if type(action_result) == dict:
                status = action_result.get("status", None)
                observation = action_result.get("observation", None)
                self.trajectory.append({
                    "turn": turn,
                    "Thought": thought,
                    "Action": action,
                    "Observation": observation
                })
                
                if status == "Finished":
                    break
                
                completed_turn = turn
                turn += 1
                if completed_turn >= self.max_action_turns:
                    logger.debug("Task execution stopped as the number of actions reached the limit.")
                    break
                if self.show_turn_budget_each_turn:
                    observation = f"{observation}\n\n{self._turn_budget_prompt(completed_turn)}"
                new_message = self.llm._construct_message(
                    f"Observation: {observation}"
                )
                self.llm.messages.append(new_message)
                logger.debug(f"{new_message['content']}")
            else:
                logger.debug(f"No valid action result to continue. action_result={action_result!r}. Exiting task.")
                return
    
    def call_agent_network_interface(self, command_str, task_env):
        # Remove Markdown markers.
        command_str_cleaned = re.sub(r"^```[a-zA-Z0-9]*\n", "", command_str.strip())
        command_str_cleaned = re.sub(r"\n```$", "", command_str_cleaned)
        
        # Parse the string after normalizing comment markers.
        command_str_cleaned = re.sub(r'//.*', '', command_str_cleaned)
        tree = ast.parse(command_str_cleaned, mode='eval')
        
        if not isinstance(tree, ast.Expression) or not isinstance(tree.body, ast.Call):
            raise ValueError("Invalid command format")

        # Extract the function name and arguments.
        func_name = tree.body.func.id
        args = [ast.literal_eval(arg) for arg in tree.body.args]

        # Invoke the method.
        if hasattr(task_env, func_name):
            method = getattr(task_env, func_name)
            result = method(*args)
            return result
        else:
            raise AttributeError(f"Method {func_name} not found in {type(task_env).__name__}")

class ReActDiag():
    def parse(self, output) -> dict:
        if isinstance(output, str):
            text = output
        else:
            try:
                text = output['content']
            except KeyError:
                raise OutputParserError("llm output is not str or dict")
        
        cleaned_output = text.strip()
        
        return cleaned_output
