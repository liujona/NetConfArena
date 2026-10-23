import json
import os
import re
from time import sleep
from .base import BaseAgent, OutputParserError
from .utils import build_physical_topology
from logs.mylogger import logger

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")


class OneShotAgent(BaseAgent):
    def __init__(self, agent_name):
        super().__init__(agent_name)
        self.output_parser = OneShotDiag()
        # self.load_config(os.path.join(CONFIG_DIR, "OneShot.yaml"))

        from environment.device_profile import IS_SONIC
        self.load_config(os.path.join(CONFIG_DIR, "OneShot_SONiC.yaml" if IS_SONIC else "OneShot.yaml"))

    def execute_task(self, task_index, interface, task_data):
        current_config = json.dumps(
            {name: "\n".join(lines) for name, lines in task_data['startup_configs'].items()},
            indent=4
        )
        topology = json.dumps(build_physical_topology(task_data), indent=4)
        self.task_prompt = self.task_prompt_template.safe_substitute(
            scenario=task_data['scenario'],
            goals=task_data['goals'],
            topology=topology,
            current_config=current_config
        )
        logger.debug(f"{self.task_prompt}")

        self.llm.messages.append(self.llm._construct_system_message(self.system_prompt))
        self.llm.messages.append(self.llm._construct_message(self.task_prompt))

        parsed_response = None
        
        for _ in range(3):
            _, answer = self.llm.parse()
            
            parsed_response = self.output_parser.parse(answer)
            if parsed_response:
                logger.debug(f"## assistant ## \n{answer}")
                break
            sleep(5)

        if parsed_response:
            interface.apply_config(parsed_response)
        else:
            print("No valid configs to apply.")
            exit(1)

class OneShotDiag():
    def parse(self, output) -> dict:
        if isinstance(output, str):
            text = output
        else:
            try:
                text = output['content']
            except KeyError:
                raise OutputParserError("llm output is not str or dict")
        
        cleaned_output = text.strip()

        json_data = self.parse_json(cleaned_output)

        return json_data
    
    def parse_json(self, respnse):
        try:
            # Try Markdown-style code blocks first.
            code_blocks = re.findall(r"```(?:json)?(.*?)```", respnse, re.DOTALL)
            for block in code_blocks:
                try:
                    return json.loads(block.strip())
                except Exception:
                    continue  # Try the next block.

            # Extract a possible JSON object with a greedy brace match.
            json_candidates = re.findall(r"\{.*\}", respnse, re.DOTALL)
            for candidate in json_candidates:
                try:
                    return json.loads(candidate)
                except Exception:
                    continue

            # Finally, try parsing the entire input directly.
            return json.loads(respnse.strip())

        except Exception as e:
            print(f"Failed to extract and parse JSON: {e}")
            print(f"Original response: {respnse}")
            return None
