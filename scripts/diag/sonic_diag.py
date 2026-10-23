import sys
from dotenv import load_dotenv
load_dotenv()
from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment

path = sys.argv[1] if len(sys.argv) > 1 else "sonic-test/01.Static Routing/01.Static Routing.json"
task = render_task_file(path, randomize=True, seed=1)
env = TaskEnvironment(task_data=task)
try:
    dev = next(iter(task["groundtruth_configs"]))
    for c in [
        'sudo docker exec bgp ls /usr/lib/frr',
        "sudo docker exec bgp cat /etc/frr/daemons | grep -v -e '^#' -e '^$'",
        'sudo docker exec bgp supervisorctl status',
        'sudo vtysh -c "show daemons"',
        'sudo vtysh -c "show version"',
        'sudo vtysh -c "configure terminal" -c "router ospf" -c "network 10.99.0.0/24 area 0" -c "exit"',
    ]:
        print(f"\n##### {dev} $ {c}")
        print(env.send_command_to_node(dev, c))
finally:
    env.close()
