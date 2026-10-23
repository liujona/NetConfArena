# scripts/sonic_probe.py
import sys, time
from dotenv import load_dotenv
load_dotenv()

from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch

path = sys.argv[1]
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1

task = render_task_file(path, randomize=True, seed=seed)
env = TaskEnvironment(task_data=task)
try:
    # 1. Send the reference config and print the RAW reply of each call.
    for dev, cmds in task["groundtruth_configs"].items():
        for line in sonic_wrap_batch(list(cmds)):
            print(f"\n##### {dev} $ {line}")
            print(env.send_command_to_node(dev, line))

    time.sleep(60)  # give OSPF time to form adjacencies

    # ping_pairs = {
    #     "Daisy":   "2.230.35.2",
    #     "Fern":    "4.60.95.4",
    #     "Rose":    "4.60.95.4",
    #     "Jasmine": "2.230.35.2",
    # }
    # for dev, target in ping_pairs.items():
    #     cmd = f"ping -c 3 -I Loopback0 {target}"
    #     print(f"\n##### {dev} $ {cmd}")
    #     print(env.send_command_to_node(dev, cmd))

    direct_pairs = {
        "Daisy":   "192.168.199.3",   # Rose, directly connected via Ethernet0
        "Fern":    "192.168.228.4",   # Daisy, directly connected via Ethernet0
        "Rose":    "192.168.199.4",   # Daisy, directly connected via Ethernet0
        "Jasmine": "192.168.52.2",    # Fern, directly connected via Ethernet0
    }
    for dev, target in direct_pairs.items():
        cmd = f"ping -c 3 {target}"
        print(f"\n##### {dev} $ {cmd}")
        print(env.send_command_to_node(dev, cmd))

    for dev in task["groundtruth_configs"]:
        print(f"\n##### {dev} $ ip neigh")
        print(env.send_command_to_node(dev, "ip neigh"))
        print(f"\n##### {dev} $ ip route")
        print(env.send_command_to_node(dev, "ip route"))

    # 2. Look at what the device actually has.


    # checks = [
    #     "sudo docker exec bgp which ospfd",
    #     "sudo docker exec bgp find / -iname 'daemons' 2>/dev/null",
    #     "sudo docker exec bgp find /etc/supervisor* -iname '*.conf' 2>/dev/null",
    #     "sudo docker exec bgp supervisorctl status",
    #     "show feature status",
    #     "sudo sonic-cfggen -d --var-json 'FEATURE' 2>/dev/null",
    # ]

    checks = [
        'sudo vtysh -c "show ip route"',
        'sudo vtysh -c "show interface brief"',
        'sudo vtysh -c "show bgp summary"',
    ]



    for dev in task["groundtruth_configs"]:
        for c in checks:
            print(f"\n##### {dev} $ {c}")
            print(env.send_command_to_node(dev, c))
finally:
    env.close()