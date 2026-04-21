import ansible_runner

def run_playbook(playbook, extravars):
    r = ansible_runner.run(
        private_data_dir=".",
        playbook=playbook,
        extravars=extravars
    )
    return r
