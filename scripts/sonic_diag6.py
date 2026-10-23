import time
from dotenv import load_dotenv
load_dotenv()
from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch
task = render_task_file("sonic-gen/20.EBGP-External BGP/20.EBGP-External BGP.json", randomize=True, seed=1)
env = TaskEnvironment(task_data=task)
def run(dev, c):
    print(f"\n##### {dev} $ {c}"); print(env.send_command_to_node(dev, c))
try:
    for dev, cmds in task["groundtruth_configs"].items():
        for line in sonic_wrap_batch(list(cmds)):
            env.send_command_to_node(dev, line)
    time.sleep(75)
    run("Kenaf", 'sudo vtysh -c "show ip bgp json"')
    run("Kenaf", 'sudo vtysh -c "show ip bgp 4.60.95.0/24 json"')
    run("Kenaf", 'sudo vtysh -c "show ip bgp neighbors json"')
    run("Kenaf", 'sudo vtysh -c "show ip bgp summary json"')
    run("Kenaf", 'sudo vtysh -c "show running-config"')
finally:
    env.close()
