import base64
import json
import os
import shutil
import sys
import tempfile

import click
import colorama
import docker
import kubernetes.client
import pip
import yaml

import docker_registry_client as registry
import git

from .kube import Kube


# Use a colorized prompt to differenciate output of this script from output
# that is generated by called programms and libraries
colorama.init(autoreset=True)
PROMPT = '>> '


def prompt(msg: str, indent: int=0):
    indentation = ' ' * indent
    sys.stdout.write(colorama.Fore.GREEN + PROMPT + indentation)
    print(msg)


def error_prompt(msg: str):
    sys.stdout.write(colorama.Fore.RED + PROMPT)
    print(msg)



def download_requirements(force: bool=False):
    # Create temporary directory as download target for requirements then
    # download to this temporary directory and move it into the docker
    # context. The docker context is the current directory that can not be
    # used as initial destination as it itself is part of the requirements.txt
    dest = os.path.join(os.getcwd(), 'pip-cache')

    if os.path.isdir(dest):
        if not force:
            prompt('pip-cache exists. Skipping download of requirements.')
            return

        # Remove the existing pip-cache if any
        prompt('pip-cache exists. Removing for fresh download of '
               'requirements.')
        shutil.rmtree(dest)

    tmp = tempfile.mkdtemp()
    prompt('Downloading requirements.')
    with open('requirements.txt') as f:
        deps = [line for line in f if line.startswith('git+ssh')]
    pip.main(['download', '-q', '--dest', tmp, *deps])
    shutil.move(tmp, dest)


def make_tag(registry: str, name: str, version: str) -> str:
    return "{}/{}:{}".format(registry, name, version)


def tag_components(tag: str) -> (str, str, str):
    domain, rest = tag.split('/', 1)
    repository, version = rest.split(':', 1)

    return domain, repository, version


def docker_image(op: str, tag: str):
    # The registry part of the tag will be used to determine the push
    # destination domain.
    client = docker.from_env(version='1.24')

    if op == "build":
        prompt('Building image: {}'.format(tag))
        client.images.build(tag=tag, path=os.getcwd())
    elif op == "push":
        prompt('Pushing image: {}'.format(tag))
        client.images.push(tag)


def docker_image_exists(tag: str) -> bool:
    # This one assumes a logged in local docker to read the credentials from
    home = os.path.expanduser('~')
    docker_auth_file = os.path.join(home, '.docker', 'config.json')
    with open(docker_auth_file) as fd:
        docker_auth_data = json.load(fd)

    # Extract the credentials for the docker json.
    domain_part, repository, version = tag_components(tag)
    base64_credentials = docker_auth_data['auths'][domain_part]['auth']
    # dXNlcm5hbWU6cGFzc3dvcmQK= -> username:password
    credentials = base64.b64decode(base64_credentials).decode('utf8')
    # username:password -> [username, password]
    username, password = credentials.split(':', 1)

    client = registry.DockerRegistryClient("https://{}".format(domain_part),
                                           username=username,
                                           password=password)

    return version in client.repository(repository).tags()


def fill_deployment_definition(
        deployment: kubernetes.client.ExtensionsV1beta1Deployment,
        tag: str):
    deployment_name = '???'

    # Set name
    deployment.metadata.name = deployment_name
    deployment.spec.template.metadata.labels['name'] = deployment_name
    deployment.spec.revisionHistoryLimit = 5

    # Set image. For now just grab the first container as there is only one
    # TODO: find a way to properly decide on the container to update here
    deployment.spec.template.spec.containers[0].image = tag

    return deployment


def head_of(working_directory: str, branch: str=None, local: bool=False) -> str:
    repo = git.Repo(working_directory)
    if branch is None:
        try:
            branch = repo.active_branch
        # The type error gets thrown for example on detached HEAD states and
        # during unfinished rebases and cherry-picks.
        except TypeError as e:
            error_prompt('No branch given and current status is inconclusive: '
                         '{}'.format(str(e)))
            sys.exit(1)

    if local:
        return repo.git.rev_parse(repo.head.commit, short=8)

    prompt("Getting remote HEAD of {}".format(branch))

    # Fetch all remotes (usually one?!) to make sure the latest refs are known
    # to git. Save remote refs that match current branch to make sure to avoid
    # ambiguities and bail out if a branch exists in multiple remotes with
    # different HEADs.
    remote_refs = []
    for remote in repo.remotes:
        remote.fetch()
        for ref in remote.refs:
            # Finding the remote tracking branch this way is a simplification
            # already that assumes the remote tracking branches are of the
            # format refs/remotes/foo/bar (indicating that it tracks a branch
            # named bar in a remote named foo), and matches the right-hand-side
            # of a configured fetch refspec. To actually do it correctly
            # involves reading local git config and use `git remote show
            # <remote-name>`. The main issue with that approach is it would
            # involve porcelain commands as there are no plumbing commands
            # available to get the remote tracking branches currently. Long
            # story short: follow the conventions and this script will work.
            if ref.name == '{remote}/{branch}'.format(remote=remote.name,
                                                      branch=branch):
                prompt('Found "{}" at {}'.format(ref.name,
                                                 str(ref.commit)[:8]))
                remote_refs.append(ref)

    # Bail out if no remote tracking branches where found.
    if len(remote_refs) < 1:
        error_prompt('No remote tracking branch matching "{}" found'.format(
            branch))
        sys.exit(1)

    # Iterate over found remote tracking branches and compare commit IDs; bail
    # out if there is more than one and they differ.
    if len(remote_refs) > 1:
        seen = {}
        for ref in remote_refs:
            seen[repo.git.rev_parse(ref.name)] = True
        if seen.keys() != 1:
            error_prompt('Multiple matching remote tracking branches with'
                         ' different commit IDs found. Can not go on. Make'
                         ' sure requested deployments are unambiguous.')
            sys.exit(1)

    # At this point the head commit of the first remote tracking branch can be
    # returned as it is the same as the others if they exist.
    return repo.git.rev_parse(remote_refs[0].commit, short=8)


def load_options(base_path):
    """Load the options for a service deployment. base_path is the
    directory in which the command is called."""
    default_service_name = base_path.rstrip(os.sep).rsplit(os.sep, 1)[-1]
    default_options = {'namespace': 'default',
                       'service_name': default_service_name}
    config_path = os.path.join(base_path, '.kubedeploy')
    if os.path.isfile(config_path):
        with open(config_path, 'r') as config_file:
            default_options.update(yaml.load(config_file.read()))
    return default_options


@click.group()
@click.pass_context
def cli(ctx: click.Context):
    ctx.obj = {}


@cli.command()
@click.option('--registry')
@click.option('--image', default='???')
@click.option('--branch', help='The git branch to deploy. Defaults to master.',
              default='master')
@click.option('--version', help='Version of API to build and deploy. Will'
              'replace if it already exists.')
@click.option('--environment', default='???')
@click.option('--dry/--no-dry', help='Run without building, pushing, and'
              ' deploying anything',
              default=False)
@click.option('--local/--no-local', help='If set then the local state of the'
              ' service will be used to create, push, and deploy a Docker'
              ' image.',
              default=False)
def deploy(registry: str, image: str, branch: str, version: str,
           environment: str, local: bool, dry: bool):
    working_directory = os.getcwd()
    options = load_options(working_directory)
    if registry is None:
        registry = options['registry']
    if local:
        # Reset branch when using local.
        branch = None
    if version is None:
        version = head_of(working_directory, branch, local=local)
    kube = Kube(namespace=options['namespace'],
                printer=prompt,
                error_printer=error_prompt)
    tag = make_tag(registry, image, version)

    if local and not dry:
        download_requirements()
        docker_image('build', tag)
        docker_image('push', tag)

    if not docker_image_exists(tag):
        error_prompt('Image not found: {}'.format(tag))
        if not dry:
            sys.exit(1)

    kube.info()

    if dry:
        prompt('Dry run finished. Not deploying.')
        return

    kube.deploy(tag, environment)


@cli.command()
@click.option('--registry', default='???')
@click.option('--image', default='???')
@click.option('--version', help='Version of API to build. Will replace if it'
              ' already exists.', default=None)
def build(registry: str, image: str, version: str):
    if version is None:
        version = head_of(None, local=True)

    tag = make_tag(registry, image, version)
    download_requirements()
    docker_image('build', tag)


@cli.command()
@click.option('--registry', default='???')
@click.option('--image', default='???')
@click.option('--version', help='Git commit ID or branch to build and deploy.'
              ' Will replace if it already exists.', default=None)
def push(registry: str, image: str, version: str):
    if version is None:
        version = head_of(None, local=True)

    tag = make_tag(registry, image, version)
    docker_image('push', tag)


@cli.command()
@click.pass_obj
def info(kube: Kube):
    kube = Kube(namespace='???',
                printer=prompt,
                error_printer=error_prompt)
    kube.info()


def main():
    cli(obj={})


if __name__ == '__main__':
    main()
