#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import docker
import collections
import copy
import operator
import functools
import hiyapyco
import subprocess
import pprint
import logging

logging.basicConfig(level='INFO')

logger = logging.getLogger()

pp = pprint.PrettyPrinter(indent=4)

class AgentContainer(object):
    def __init__(self, container: docker.models.containers.Container, **kwargs):
        self.container = container
        self.props = kwargs

    @staticmethod
    def run_container(client: docker.DockerClient, no_detach: bool = False, **kwargs):
        return AgentContainer(client.containers.run(**kwargs, detach=(not no_detach)))
    
    @staticmethod
    def get_container(client: docker.DockerClient, id: str):
        return AgentContainer(client.containers.get(id))
    
    def stop(self, **kwargs):
        self.container.stop(**kwargs)
    
    def remove(self, **kwargs):
        self.container.remove(**kwargs)


class AgentContainerGroup(collections.UserList):
    class UndefinedConstantError(Exception):
        def __init__(self, constant: str):
            super().__init__(constant)

    class UnknownLocalConstantTypeError(Exception):
        def __init__(self, t: str):
            super().__init__(t)

    def __init__(self):
        self.data = []

    @staticmethod
    def _parse_config(config: dict, external_constants: dict = {}, override_constants: dict = {}, no_detach: bool = False):
        def __eval_expr(constants: dict, expr, cast_to_str: bool = False, sep: str = "++", no_recursive: bool = False):
            def __eval_token(token: str):
                if len(token) >= 2 and token[0] == '$':
                    try:
                        var_name = token[1:]
                        var_value = constants[var_name]

                        # lazy var
                        if len(var_name) >= 2 and var_name[0] == '^':
                            if no_recursive:
                                return __eval_expr(constants, var_value)
                            else:
                                return __eval_expr_recursive(constants, var_value)
                        else:
                            return var_value
                        
                    except KeyError as e:
                        raise AgentContainerGroup.UndefinedConstantError(
                            e.__str__())
                else:
                    return token
            
            if type(expr) != str:
                return expr

            expr_splitted = expr.split(sep)

            # preserve the type
            if not cast_to_str and len(expr_splitted) == 1:
                return __eval_token(expr_splitted[0])
            
            ret = ""
            
            # cast to string
            for token in expr_splitted:
                ret += str(__eval_token(token))
            return ret

        def __eval_expr_recursive(constants: dict, data):
            if type(data) == str:
                return __eval_expr(constants, data)
            elif type(data) == dict:
                result = {}
                for k, v in data.items():
                    result[__eval_expr(constants, str(k))] = __eval_expr_recursive(constants, v)
                return result
            elif type(data) == list:
                result = []
                for v in data:
                    result.append(__eval_expr_recursive(constants, v))
                return result
            else:
                return data

        def __get_value_by_path(constants: dict, path: str, data: dict, sep: str = "->"):
            return functools.reduce(operator.getitem, map(lambda k: __eval_expr(constants, k), path.split(sep)), data)

        def __set_value_by_path(constants: dict, path: str, data: dict, new_value, sep: str = "->"):
            if path is None or path == "":
                data.update(new_value)
                return
            path_tokenized = list(map(lambda k: __eval_expr(constants, k), path.split(sep)))
            p = data
            for key in path_tokenized[:-1]:
                if key not in p:
                    p[key] = {}
                p = p[key]
            p[path_tokenized[-1]] = new_value

        available_local_constant_evaluators = {
            "auto_increment": lambda constants, index, element, options: index + __eval_expr(constants, options.get("start", 0)),
            "from_property": lambda constants, index, element, options: __get_value_by_path(constants, options["source"], element)
        }

        def __eval_local_constants(global_constants: dict, local_constants_def: list, index: int, container: dict):
            values = {}
            for constant in local_constants_def:
                assert "name" in constant
                assert "type" in constant
                vname = constant["name"]
                vtype = constant["type"]

                try:
                    evaluator = available_local_constant_evaluators[vtype]
                except KeyError:
                    raise AgentContainerGroup.UnknownLocalConstantTypeError(
                        vtype)

                values[vname] = evaluator(
                    global_constants, index, container, constant)
            return values

        def __eval_environment(constants: dict, environment_definition: list):
            environment = {}
            for env_definition in environment_definition:
                assert "value" in env_definition
                assert "name" in env_definition
                eexpr = env_definition["value"]
                ename = env_definition["name"]
                environment[ename] = __eval_expr(constants, eexpr, True)
            return environment

        def __run_hooks(global_constants: dict, name: str, config: dict, index: int = -1, container: dict = {}):
            if "hooks" in config and name in config["hooks"]:
                logger.info("Running {} hook...".format(name))
                # logger.info("{} {}".format(index, container))
                hooks = config["hooks"][name]

                local_constants = __eval_local_constants(global_constants,
                    hooks.get("local_constants", []), index, container) if index != -1 else {}

                # logger.info("local_constants = {}".format(local_constants))

                constants = {**global_constants, **local_constants}

                environment = __eval_environment(constants, hooks.get("environment", []))
                logger.debug("Environment is {}".format(environment))

                assert "commands" in hooks
                for command in hooks["commands"]:
                    logger.info("Running command: {}".format(command))
                    subprocess.run(command, shell=True, env=environment)

        assert type(config) is dict
        assert "constants" not in config or type(config["constants"]) is dict
        global_constants = {}
        assert type(global_constants) is dict
        global_constants.update(external_constants)
        global_constants["base_dir"] = os.getcwd()

        for k, v in config.get("constants", {}).items():
            assert type(k) is str
            assert len(k) >= 1
            if k[0] != '^': # lazy constant
                v = __eval_expr_recursive(global_constants, v)
            global_constants[k] = v
        
        global_constants.update(override_constants)
        
        assert "containers" in config
        assert type(config["containers"]) in (list, int, str)

        containers = []
        if type(config["containers"]) is int:
            containers = [{} for _ in range(config["containers"])]
        elif type(config["containers"]) is str:
            containers_q = __eval_expr(global_constants, config["containers"])
            assert type(containers_q) is int
            containers = [{} for _ in range(containers_q)]
        else:
            containers = __eval_expr_recursive(global_constants, config["containers"])

        # process rules
        if "rules" in config:
            rules = config["rules"]
            if type(rules) is str:
                rules = __eval_expr(global_constants, rules, no_recursive=True)
            
            for rule in rules:
                assert "value" in rule

                rule_target = rule.get("target")
                rule_expr = rule.get("value")

                for index in range(len(containers)):
                    container = containers[index]

                    rule_local_constants = {}
                    if "local_constants" in rule:
                        rule_local_constants = __eval_local_constants(constants,
                            rule["local_constants"], index, container)

                    constants = {**rule_local_constants, **global_constants}
                    new_value = __eval_expr_recursive(constants, rule_expr)
                    if type(rule_target) is list:
                        for target in rule_target:
                            __set_value_by_path(
                                constants, target, container, new_value)
                    else:
                        __set_value_by_path(
                            constants, rule_target, container, new_value)
        logger.debug("Containers to start: ")
        logger.debug(pp.pformat(containers))

        if "hooks" in config and type(config["hooks"]) is str:
            config["hooks"] = __eval_expr(global_constants, config["hooks"], no_recursive=True)
        
        hooks = {
            "preup-global": lambda: __run_hooks(global_constants, "preup-global", config),
            "postup-global": lambda: __run_hooks(global_constants, "postup-global", config),
            "preup": lambda index, container: __run_hooks(global_constants, "preup", config, index, container),
            "postup": lambda index, container: __run_hooks(global_constants, "postup", config, index, container)
        }

        return containers, hooks



    @staticmethod
    def create_containers_from_config(client: docker.DockerClient, config: dict, external_constants: dict = {}, override_constants: dict = {}, dry_run: bool = False, no_detach: bool = False, action="run"):
        container_collection = AgentContainerGroup()
        containers, hooks = AgentContainerGroup._parse_config(config, external_constants, override_constants, no_detach)

        hooks["preup-global"]()
        
        for index in range(len(containers)):
            container = containers[index]

            hooks["preup"](index, container)

            container_identifier = container["name"] if "name" in container else (
                "#" + str(index))

            logger.info("Starting container {}...".format(container_identifier))
            if dry_run:
                logger.warning("Not running container because of dry-run mode is enabled")
                logger.info("Here is the configuration of the container")
                logger.info(pp.pformat(container))
            else:
                container_collection.append(
                    AgentContainer.run_container(client=client, no_detach=no_detach, **container))
            logger.info("Container {} started.".format(container_identifier))

            hooks["postup"](index, container)

        hooks["postup-global"]()

        return container_collection
    
    @staticmethod
    def get_containers_from_config(client: docker.DockerClient, config: dict, external_constants: dict = {}, override_constants: dict = {}):
        containers, _ = AgentContainerGroup._parse_config(config, external_constants)
        
        agent_containers = []
        for container in containers:
            assert "name" in container
            agent_containers.append(AgentContainer(client.containers.get(container["name"])))
        
        return agent_containers
        

class AgentContainerGroupCollection(collections.UserDict):
    def __init__(self):
        self.data = {}

    @staticmethod
    def create_container_groups_from_config(client: docker.DockerClient, config: dict, override_constants: dict = {}, dry_run: bool = False, no_detach: bool = False):
        assert type(config) is dict
        container_group_collection = AgentContainerGroupCollection()

        if "containers" in config:
            assert "groups" not in config
            container_group_collection["DEFAULT"] = AgentContainerGroup.create_containers_from_config(
                client, config, {}, override_constants, dry_run, no_detach)
        elif "groups" in config:
            assert "rules" not in config
            assert "hooks" not in config

            global_constants = config.get("constants", {})
            assert type(global_constants) is dict

            for name, group in config["groups"].items():
                logger.info("Starting group {}".format(name))
                container_group_collection[name] = AgentContainerGroup.create_containers_from_config(
                    client, group, global_constants, override_constants, dry_run, no_detach)
        else:
            raise Exception("No containers or groups specified")
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Runs containers.')
    parser.add_argument('config', type=str, nargs='*', help='run with a specific configuration file', default='config.yaml')
    parser.add_argument('-d', '--dry-run', action='store_true',
                        help='dry run without running containers')
    parser.add_argument('-n', '--no-detach', action='store_true',
                        help='do not detach from containers')
    parser.add_argument('-w', '--constant', action='append', nargs=2, metavar=('name', 'value'), help='override constants in config', default=[])

    args = parser.parse_args()

    override_constants = {}

    for name, value in args.constant:
        override_constants[name] = value

    config = hiyapyco.load(
        args.config,
        method=hiyapyco.METHOD_MERGE,
        failonmissingfiles=True,
        usedefaultyamlloader=True
    )

    docker_client = docker.from_env()

    container_collection = AgentContainerGroupCollection.create_container_groups_from_config(
        docker_client, config, override_constants, args.dry_run, args.no_detach)
