import sys, time
from dotenv import load_dotenv
load_dotenv()
from task.task_loader import render_task_file
from environment.task_environment import TaskEnvironment
from environment.device_profile import sonic_wrap_batch

path = "sonic-gen/20.EBGP-External BGP/20.EBGP-External BGP.json"
task = render_task_file(path, randomize=True, seed=1)   # seed 1 -> Linen/Kenaf, 192.168.107.x
env = TaskEnvironment(task_data=task)
def run(dev, c):
    print(f"\n##### {dev} $ {c}")
    print(env.send_command_to_node(dev, c))
try:
    for dev, cmds in task["groundtruth_configs"].items():
        for line in sonic_wrap_batch(list(cmds)):
            env.send_command_to_node(dev, line)
    time.sleep(90)

    # ---- A. why does ping fail?
    for dev in ("Linen", "Kenaf"):
        for c in ["ip -br addr", "ip route", 'sudo vtysh -c "show ip route"',
                  "sysctl net.ipv4.conf.all.rp_filter net.ipv4.ip_forward"]:
            run(dev, c)
    run("Linen", "ping -c 2 192.168.107.3")                      # direct link
    run("Linen", "ping -c 2 -I Loopback0 192.168.107.3")         # loopback source, direct dst
    run("Linen", "ping -c 2 3.253.169.3")                        # remote loopback, no -I
    run("Kenaf", "ping -c 2 -I Loopback0 192.168.107.4")

    # ---- B. can we start ospfd by hand?
    run("Linen", 'sudo docker exec bgp grep -rA6 "program:zebra" /etc/supervisor/')
    run("Linen", "sudo docker exec bgp ls -l /var/run/frr")
    for dev in ("Linen", "Kenaf"):
        run(dev, "sudo docker exec bgp /usr/lib/frr/ospfd -d -F traditional -A 127.0.0.1")
    time.sleep(5)
    for dev in ("Linen", "Kenaf"):
        run(dev, 'sudo vtysh -c "show daemons"')
        run(dev, 'sudo vtysh -c "configure terminal" -c "router ospf" -c "network 192.168.107.0/24 area 0" -c "exit"')
    time.sleep(50)
    for dev in ("Linen", "Kenaf"):
        run(dev, 'sudo vtysh -c "show ip ospf neighbor"')
finally:
    env.close()
