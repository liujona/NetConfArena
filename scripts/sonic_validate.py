# scripts/sonic_validate.py
import sys, os, re, json
from dotenv import load_dotenv
load_dotenv()

from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch
from evaluator.evaluate_task import Evaluator

ERR = re.compile(r'Error|failed|Exiting|Usage:|^%|not running|Unknown command|Ambiguous|incomplete', re.M | re.I)
OK_ERR = re.compile(r'does not exist|not found|No such', re.I)

def apply_verbose(env, configs):
    bad = False
    for dev, cmds in configs.items():
        env.node_current_configs.pop(dev, None)
        for line in sonic_wrap_batch(list(cmds)):
            out = env.send_command_to_node(dev, line) or ""
            if ERR.search(out) and not OK_ERR.search(out):
                bad = True
                print(f"\n!! {dev}: {line[:400]}\n{out.strip()[-800:]}")
    return bad

path = sys.argv[1]
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1

task = render_task_file(path, randomize=True, seed=seed)
env = TaskEnvironment(task_data=task)
try:
    if apply_verbose(env, task["groundtruth_configs"]) and not os.environ.get("CONTINUE"):
        print("\nGroundtruth did not apply cleanly. Fix the converter, or set CONTINUE=1 to evaluate anyway.")
        sys.exit(1)
    ev = Evaluator()
    ev.evaluate_task(env.evaluator_interface, task)
    res = ev.task_evaluation_result
    passed = res["testcase_execution_results"]["testcases_passed"]
    print("score:", res["testcase_execution_results"]["score"])
    print(json.dumps(passed, indent=2))
    print(json.dumps(res.get("config_evaluation_results", {}).get("evaluation_result", {}), indent=2, default=str))

    # Raw device output for every failed testcase
    gi = 0
    for g in task["testcases"]:
        if not g.get("testcase"):
            continue
        gi += 1
        for ti, t in enumerate(g["testcase"], 1):
            if passed.get(f"{gi}-{ti}") is False:
                print(f"\n##### RAW {gi}-{ti} {t['node']} $ {t['command']}")
                raw = env.send_command_to_node(t["node"], t["command"]) or ""
                print("\n".join(raw.splitlines()[:30]))
finally:
    if os.environ.get("KEEP_LAB"):
        print("KEEP_LAB set: lab left running (project:", env.project_name, ")")
    else:
        env.close()
