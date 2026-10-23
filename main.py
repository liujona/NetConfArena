import os
import json
import time
import argparse

from dotenv import load_dotenv
load_dotenv()

from environment.task_environment import TaskEnvironment
from evaluator.evaluate_task import Evaluator
from logs.mylogger import setup_logger
from task.task_loader import render_task

def create_agent(agent_name):
    if agent_name.startswith("OneShot"):
        from agent.oneshot import OneShotAgent
        return OneShotAgent(agent_name)
    else:
        from agent.react import ReActAgent
        return ReActAgent(agent_name)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run one agent on one NetConfArena task instance and evaluate the result."
    )
    parser.add_argument('--mode', type=str, required=True,
                        help="Agent harness: ReAct (closed-loop) or OneShot (single-shot)")
    parser.add_argument('--llm_model', type=str, required=True,
                        help="Model identifier passed to the OpenAI-compatible endpoint")
    parser.add_argument('--no_thinking', type=lambda x: x.lower() == 'true', default=False,
                        help="Disable the model's reasoning channel where supported (true/false, default false)")
    parser.add_argument('--task_index', type=str, required=True,
                        help="1-based task index into task/all_tasks.json")
    parser.add_argument('--trial', type=str, required=True,
                        help="Trial number; independent repetition of the same task instance")
    parser.add_argument('--seed', type=int, default=None,
                        help="Task-instantiation seed. The same seed yields the same task "
                             "instance (device names, interfaces, IP addressing). Omit for a "
                             "random, non-reproducible instance.")

    parser.add_argument('--task_file', type=str, default=None,
                        help="Path to a task JSON, e.g. sonic-test/01.Static Routing/01.Static Routing.json")
    
    args = parser.parse_args()

    mode = args.mode
    llm_model = args.llm_model
    no_thinking = args.no_thinking
    task_index = int(args.task_index) - 1
    trial = int(args.trial)
    seed = args.seed

    # results_root = "results_sonic" if os.environ.get("NETCONF_PLATFORM", "").lower() == "sonic" else "results"
    # data_dir = f"{results_root}/{int(args.task_index):02d}.{scenario}/{llm_model.split('/')[-1]}/{mode}"

    if no_thinking:
        llm_model = llm_model + '_no_thinking'
    agent_name = mode + '___' + llm_model

    # Instantiate the task template: sample parameters and render placeholders.
    # task_data = render_task(task_index, randomize=True, seed=seed)

    if args.task_file:
        from task.task_loader import render_task_file
        task_data = render_task_file(args.task_file, randomize=True, seed=seed)
    else:
        task_data = render_task(task_index, randomize=True, seed=seed)
    
    scenario = task_data['scenario']

    # Build the output directory (the task index is padded to two digits).
    # data_dir = f"results/{int(args.task_index):02d}.{scenario}/{llm_model.split('/')[-1]}/{mode}"
    # os.makedirs(data_dir, exist_ok=True)

    scenario = task_data['scenario']

    results_root = "results_sonic" if os.environ.get("NETCONF_PLATFORM", "").lower() == "sonic" else "results"
    data_dir = f"{results_root}/{int(args.task_index):02d}.{scenario}/{llm_model.split('/')[-1]}/{mode}"
    os.makedirs(data_dir, exist_ok=True)

    seed_tag = "random" if seed is None else str(seed)
    data_path = os.path.join(data_dir, f"seed_{seed_tag}_trial_{trial}.json")
    if os.path.exists(data_path):
        print(f"Data for seed {seed_tag} trial {trial} already exists at {data_path}. Skipping.")
        exit(0)

    # Initialise the logger (one log file per run, alongside the result JSON).
    logger = setup_logger(data_dir, f"seed_{seed_tag}_trial_{trial}")

    ### Create the task environment ###
    task_env = TaskEnvironment(task_data=task_data)

    #### The agent executes the task ####
    logger.info(f"Executing the task: {scenario}")
    logger.info(f"Agent: {agent_name}")
    logger.info(f"Seed: {seed_tag}, Trial: {trial}")

    agent = create_agent(agent_name)

    start_time = time.time()
    agent.execute_task(task_index, task_env.agent_interface, task_data=task_data)
    end_time = time.time()
    agent_duration = end_time - start_time

    logger.info(f"Completed in {agent_duration} seconds!")

    #### The evaluator scores the resulting network state ####
    logger.info(f"Evaluating the task: {scenario}")

    evaluator = Evaluator()

    start_time = time.time()
    evaluator.evaluate_task(task_env.evaluator_interface, task_data)
    end_time = time.time()

    task_env.close()
    logger.info(f"Task evaluation completed in {end_time - start_time} seconds!")

    ### Persist the run ####
    trajectory = agent.trajectory
    evaluation_data = evaluator.task_evaluation_result

    result = {
        "scenario": scenario,
        "task_index": int(args.task_index),
        "llm_model": llm_model,
        "mode": mode,
        "seed": seed,
        "trial": trial,
        "agent_duration_seconds": agent_duration,
        "resolved_params": task_data.get('resolved_params', {}),
        "trajectory": trajectory,
        "evaluation_data": evaluation_data
    }

    with open(data_path, 'w') as f:
        json.dump(result, f, indent=2)

    logger.info(f"Task data has been saved to {data_path}")
