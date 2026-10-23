import time
import json
from .parse_cli_output import parse_cli_output
from .utils import generate_configs_diff, compute_precision_and_recall, resolve_references, compare_results, is_subdict, is_operator_assertion
from environment.telnet_node import run_parallel
from logs.mylogger import logger

from environment.device_profile import ROUTE_SNAPSHOT_CMDS

class Evaluator:
    def __init__(self):
        self.task_evaluation_result = {}

    def evaluate_task(self, evaluator_interface, task_data):
        self.task_data = task_data
        self.evaluator_interface = evaluator_interface
        self.evaluate_configs()
        self.execute_testcases()

    def evaluate_configs(self):
        logger.info(f"Evaluating the configurations...")
        configs_evaluation_result = {}
        current_configs = self.evaluator_interface.get_all_node_current_configs()
        
        # Compute the difference between current and startup configurations.
        configs_diff = generate_configs_diff(self.evaluator_interface.node_startup_configs, current_configs)
        configs_evaluation_result.update({"diff_against_startup": configs_diff})

        configs_evaluation_result.update({"groundtruth_configs": self.task_data["groundtruth_configs"]})

        total_lines = 0
        for _, value in configs_evaluation_result["groundtruth_configs"].items():
            total_lines += len(value)
        
        precision_and_recall = compute_precision_and_recall(configs_evaluation_result['diff_against_startup'], self.task_data['groundtruth_configs']) 
        configs_evaluation_result.update({"total_lines": total_lines})
        configs_evaluation_result.update({"evaluation_result": precision_and_recall})
        
        logger.debug(f"{json.dumps(configs_evaluation_result, indent=2)}")
        self.task_evaluation_result.update({"config_evaluation_results": configs_evaluation_result})

    def _snapshot_routes(self, all_node):
        # Fetch routing tables in parallel; nodes are independent.
        #
        # Fetch VRF tables as well as the global table. In L3VPN tasks the
        # OSPF/LDP global table stabilizes early, while VPNv4 RT imports can
        # take about 100 seconds to install remote prefixes in the VRF RIB.
        # Looking only at the global table would declare convergence too soon.
        def worker(node_name):
            routes = []
            for command in ROUTE_SNAPSHOT_CMDS:
                output = self.evaluator_interface.execute_command(node_name, command)
                routes.extend(parse_cli_output(command, output))
            for route in routes:
                route.pop("uptime", None)
            return routes
        return run_parallel(all_node, worker)

    @staticmethod
    def _has_learned_route(all_node_routes):
        # Check for learned routes (anything except connected/local). OSPF,
        # RIP, BGP, and static routes all qualify. A table containing only C/L
        # routes is a false stable state before convergence.
        for routes in all_node_routes.values():
            for route in routes:
                if str(route.get("protocol", "")).strip().upper() not in ("", "C", "L"):
                    return True
        return False

    def wait_for_convergence(self, max_wait=300, min_wait=60, poll_interval=5, stable_required=5):
        # Convergence rule:
        #   1. Wait min_wait first. BGP advertises in roughly 30-second bursts,
        #      so a short stability window can land between updates.
        #   2. Poll all routing tables every poll_interval seconds. Convergence
        #      requires stable_required identical snapshots containing at least
        #      one learned route.
        logger.info(f"Waiting for routing convergence...")

        time.sleep(min_wait)
        elapsed = min_wait

        all_node = self.evaluator_interface.get_all_node()
        all_node_routes = self._snapshot_routes(all_node)
        stable_count = 0

        while stable_count < stable_required:
            if elapsed >= max_wait:
                logger.warning(f"Routing did not converge after {max_wait}s, proceeding with testcases anyway.")
                self.task_evaluation_result["routing_converged"] = False
                return
            time.sleep(poll_interval)
            elapsed += poll_interval
            tmp_all_node_routes = self._snapshot_routes(all_node)
            if tmp_all_node_routes == all_node_routes and self._has_learned_route(all_node_routes):
                stable_count += 1
                logger.debug(f"Routing tables are stable for {stable_count} times (elapsed={elapsed}s).")
            else:
                stable_count = 0
                all_node_routes = tmp_all_node_routes
                logger.debug(f"Routing tables changed, reset stable count to 0 (elapsed={elapsed}s).")

        self.task_evaluation_result["routing_converged"] = True
    
    def execute_testcases(self):
        # Wait for routing convergence.
        self.wait_for_convergence()
        
        testcases_execution_result = {
            "goals_achieved": {},
            "testcases_passed": {}
        }
        testcases = self.task_data.get("testcases", None)
        
        if testcases is None:
            logger.info("No testcases found in the task data.")
            return
        
        logger.info(f"Executing testcases...")

        shared_data = {}  # Data shared across test cases.
        testcase_result = {}  # Results shared across test cases.

        # Test-unit commands do not reference shared_data, so every
        # (node, command) pair is known and independent before execution. Fetch
        # them in parallel, then compare in original order so shared_data
        # semantics remain unchanged. A per-node lock serializes local commands.
        prefetch_pairs = []
        seen_pairs = set()
        for testcase in testcases:
            for testunit in testcase.get("testcase", []):
                key = (testunit["node"], testunit["command"])
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    prefetch_pairs.append(key)

        def _prefetch(pair):
            node_name, command = pair
            output = self.evaluator_interface.execute_command(node_name, command)
            return parse_cli_output(command, output)

        for (node_name, command), result in run_parallel(prefetch_pairs, _prefetch).items():
            testcase_result[node_name + "_" + command] = result

        for testcase in testcases:
            goal = testcase.get("goal", None)
            logger.debug(f"Goal: {goal}")
            
            for testunit in testcase.get("testcase", []):
                id = testunit.get("id", None)
                node_name = testunit["node"]
                command = testunit["command"]
                mode = testunit.get("mode", "default")
                expected_result = testunit.get("expected_result", {})
                output_keys = testunit.get("output", None)
                
                # Resolve parameter references to shared_data.
                expected_result = resolve_references(expected_result, shared_data)

                actual_result = testcase_result.get(node_name + "_" + command, None)
                if actual_result is None:
                    # Execute the test case.
                    output = self.evaluator_interface.execute_command(node_name, command)
                    actual_result = parse_cli_output(command, output)
                    testcase_result[node_name + "_" + command] = actual_result
                
                logger.debug(f"#### expected_result: #####\n{json.dumps(expected_result, indent=2)}")
                logger.debug(f"#### actual_result: #####\n{json.dumps(actual_result, indent=2)}")

                is_passed = compare_results(expected_result, actual_result, mode)

                # Save dictionary or tuple results in shared_data.
                if output_keys and is_passed:
                    target_keys = f"{id}_{node_name}_{output_keys}"
                    if is_operator_assertion(expected_result) and expected_result.get("op") in ("contains_all", "contains_any"):
                        expected_items = expected_result.get("value", [])
                    elif type(expected_result) == list:
                        expected_items = expected_result
                    else:
                        expected_items = [expected_result]
                    for item2 in actual_result:
                        for item1 in expected_items:
                            if is_subdict(item1, item2):
                                shared_data.update({target_keys: item2[output_keys]})
                                break
                        if target_keys in shared_data:
                            break

                if is_passed:
                    logger.debug(f"\tID {id} passed.")
                else:
                    logger.debug(f"\tID {id} failed.")
                
                if testcases_execution_result["goals_achieved"].get(goal, True):
                    testcases_execution_result["goals_achieved"][goal] = is_passed
                testcases_execution_result["testcases_passed"].update({id: is_passed})

        testcases_passed = testcases_execution_result.get("testcases_passed", {})
        groups = {}
        
        for tid, passed in testcases_passed.items():
            group = tid.split('-', 1)[0] if '-' in tid else tid
            if group not in groups:
                groups[group] = {"passed": 0, "total": 0}
            groups[group]["total"] += 1
            if passed:
                groups[group]["passed"] += 1

        group_scores = {}
        sum_group_scores = 0.0
        for g, vals in groups.items():
            if vals["total"] > 0:
                gs = vals["passed"] / vals["total"]
            else:
                gs = 0.0
            group_scores[g] = {"passed": vals["passed"], "total": vals["total"], "score": gs}
            sum_group_scores += gs

        overall_score = 0.0
        if len(groups) > 0:
            overall_score = sum_group_scores / len(groups)

        testcases_execution_result.update({
            "score": overall_score
        })

        self.task_evaluation_result.update({"testcase_execution_results": testcases_execution_result})
