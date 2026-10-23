import time
from dotenv import load_dotenv
load_dotenv()
from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch

path = "sonic-gen/20.EBGP-External BGP/20.EBGP-External BGP.json"
task = render_task_file(path, randomize=True, seed=1)
env = TaskEnvironment(task_data=task)
LO = {"Linen": "4.60.95.4", "Kenaf": "3.253.169.3"}
def run(dev, c):
    print(f"\n##### {dev} $ {c}")
    print(env.send_command_to_node(dev, c))
try:
    for dev, cmds in task["groundtruth_configs"].items():
        for line in sonic_wrap_batch(list(cmds)):
            env.send_command_to_node(dev, line)
    time.sleep(60)

    # ---- A. route-map theory
    for dev in LO:
        run(dev, 'sudo vtysh -c "show route-map RM_SET_SRC"')
        run(dev, 'sudo vtysh -c "show running-config" | grep -E "route-map|set src|ip protocol"')
    for dev, ip in LO.items():
        run(dev, f'sudo vtysh -c "configure terminal" -c "route-map RM_SET_SRC permit 10" -c "set src {ip}"')
    time.sleep(15)
    for dev in LO:
        run(dev, 'sudo vtysh -c "show ip route"')
        run(dev, "ip route")
    run("Linen", "ping -c 3 -I Loopback0 3.253.169.3")
    run("Kenaf", "ping -c 3 -I Loopback0 4.60.95.4")

    # ---- B. OSPF
    for dev in LO:
        run(dev, "sudo docker exec bgp /usr/lib/frr/ospfd -d -F traditional -A 127.0.0.1")
    time.sleep(5)
    for dev in LO:
        run(dev, 'sudo vtysh -c "configure terminal" -c "router ospf" -c "ospf router-id %s" -c "network 192.168.107.0/24 area 0" -c "exit"' % LO[dev])
    time.sleep(40)
    for dev in LO:
        run(dev, 'sudo vtysh -c "show ip ospf"')
        run(dev, 'sudo vtysh -c "show ip ospf interface Ethernet0"')
        run(dev, "sudo timeout 25 tcpdump -ni Ethernet0 -c 6 ip proto 89")
        run(dev, 'sudo vtysh -c "show ip ospf neighbor"')
finally:
    env.close()
