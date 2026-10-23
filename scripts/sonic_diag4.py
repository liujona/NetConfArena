import time
from dotenv import load_dotenv
load_dotenv()
from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch

task = render_task_file("sonic-gen/20.EBGP-External BGP/20.EBGP-External BGP.json", randomize=True, seed=1)
env = TaskEnvironment(task_data=task)
def run(dev, c):
    print(f"\n##### {dev} $ {c}")
    print(env.send_command_to_node(dev, c))
try:
    for dev, cmds in task["groundtruth_configs"].items():
        for line in sonic_wrap_batch(list(cmds)):
            env.send_command_to_node(dev, line)
    time.sleep(75)
    for dev in ("Linen", "Kenaf"):
        run(dev, 'sudo vtysh -c "show running-config" | grep -E "route-map|set src|ip protocol"')
        run(dev, 'sudo vtysh -c "show ip route"')
        run(dev, "ip route")
    run("Kenaf", 'sudo vtysh -c "show ip route 4.60.95.0/24 json"')
    # experiment: remove zebra's route-map entirely
    for dev in ("Linen", "Kenaf"):
        run(dev, 'sudo vtysh -c "configure terminal" -c "no ip protocol bgp route-map RM_SET_SRC"')
    time.sleep(15)
    for dev in ("Linen", "Kenaf"):
        run(dev, 'sudo vtysh -c "show ip route"')
        run(dev, "ip route")
    run("Linen", "ping -c 3 -I Loopback0 3.253.169.3")
finally:
    env.close()
