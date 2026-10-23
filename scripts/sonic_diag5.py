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
    run("Linen", "ping -c 3 -I Ethernet0 3.253.169.3")          # source = link IP, dest = remote loopback
    run("Linen", "ping -c 3 -I Loopback0 192.168.107.3")        # loopback source, directly connected dest
    run("Kenaf", "cat /proc/sys/net/ipv4/conf/all/rp_filter /proc/sys/net/ipv4/conf/Ethernet0/rp_filter /proc/sys/net/ipv4/conf/Loopback0/rp_filter")
    run("Kenaf", "sudo timeout 12 tcpdump -ni Ethernet0 -c 6 icmp & sleep 1; echo started")
    run("Linen", "ping -c 3 -I Loopback0 3.253.169.3")
    run("Kenaf", "sleep 8; echo done")
    run("Kenaf", "sudo iptables -S | head -20")
finally:
    env.close()
