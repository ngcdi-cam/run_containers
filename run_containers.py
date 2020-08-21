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


class AgentContainer(object):
    def __init__(self, container: docker.models.containers.Container, **kwargs):
        self.container = container
        self.props = kwargs

    @staticmethod
    def run_container(client: docker.DockerClient, no_detach: bool = False, **kwargs):
        return AgentContainer(client.containers.run(**kwargs, detach=(not no_detach)))


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
    def create_containers_from_config(client: docker.DockerClient, config: dict, external_constants: dict = {}, dry_run: bool = False, no_detach: bool = False):
        assert type(config) is dict
        global_constants = config.get("constants", {})
        assert type(global_constants) is dict
        global_constants.update(external_constants)
        global_constants["base_dir"] = os.getcwd()
        constants = {}

        def __eval_expr(expr, cast_to_str: bool = False, sep: str = "++"):
            if type(expr) != str:
                return expr

            expr_splitted = expr.split(sep)

            # preserve the type
            if not cast_to_str and len(expr_splitted) == 1:
                token = expr_splitted[0]
                if len(token) >= 2 and token[0] == '$':
                    try:
                        return constants[token[1:]]
                    except KeyError as e:
                        raise AgentContainerGroup.UndefinedConstantError(
                            e.__str__())
                else:
                    return token

            ret = ""

            for token in expr_splitted:
                if len(token) >= 2 and token[0] == '$':
                    try:
                        ret += str(constants[token[1:]])
                    except KeyError as e:
                        raise AgentContainerGroup.UndefinedConstantError(e)
                else:
                    ret += str(token)
            return ret

        def __eval_expr_recursive(data):
            if type(data) == str:
                return __eval_expr(data)
            elif type(data) == dict:
                result = {}
                for k, v in data.items():
                    result[__eval_expr(str(k))] = __eval_expr_recursive(v)
                return result
            elif type(data) == list:
                result = []
                for v in data:
                    result.append(__eval_expr_recursive(v))
                return result
            else:
                return data

        def __get_value_by_path(path: str, data: dict, sep: str = "->"):
            return functools.reduce(operator.getitem, map(__eval_expr, path.split(sep)), data)

        def __set_value_by_path(path: str, data: dict, new_value, sep: str = "->"):
            if path is None or path == "":
                data.update(new_value)
                return
            path_tokenized = list(map(__eval_expr, path.split(sep)))
            p = data
            for key in path_tokenized[:-1]:
                if key not in p:
                    p[key] = {}
                p = p[key]
            p[path_tokenized[-1]] = new_value

        available_local_constant_evaluators = {
            "auto_increment": lambda index, element, options: index + __eval_expr(options.get("start", 0)),
            "from_property": lambda index, element, options: __get_value_by_path(options["source"], element)
        }

        def __eval_local_constants(constants: list, index: int, container: dict):
            values = {}
            for constant in constants:
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
                    index, container, constant)
            return values

        def __eval_environment(environment_definition: list):
            environment = {}
            for env_definition in environment_definition:
                assert "value" in env_definition
                assert "name" in env_definition
                eexpr = env_definition["value"]
                ename = env_definition["name"]
                environment[ename] = __eval_expr(eexpr, True)
            return environment

        def __run_hooks(name: str, config: dict, index: int = -1, container: dict = {}):
            nonlocal constants, global_constants

            if "hooks" in config and name in config["hooks"]:
                print("Running {} hook...".format(name))
                hooks = config["hooks"][name]

                local_constants = __eval_local_constants(
                    hooks.get("local_constants", []), index, container) if index != -1 else {}

                constants = {**local_constants, **global_constants}

                environment = __eval_environment(hooks.get("environment", []))
                print("Environment is {}".format(environment))

                assert "commands" in hooks
                for command in hooks["commands"]:
                    print("Running command: {}".format(command))
                    subprocess.run(command, shell=True, env=environment)

        container_collection = AgentContainerGroup()

        constants = global_constants
        assert "containers" in config
        assert type(config["containers"]) in (list, int, str)

        containers = []
        if type(config["containers"]) is int:
            containers = [{} for _ in range(config["containers"])]
        elif type(config["containers"]) is str:
            containers_q = __eval_expr(config["containers"])
            assert type(containers_q) is int
            containers = [{} for _ in range(containers_q)]
        else:
            containers = __eval_expr_recursive(config["containers"])

        # process rules
        if "rules" in config:
            rules = config["rules"]
            for rule in rules:
                assert "value" in rule

                rule_target = rule.get("target")
                rule_expr = rule.get("value")

                for index in range(len(containers)):
                    container = containers[index]

                    rule_local_constants = {}
                    if "local_constants" in rule:
                        rule_local_constants = __eval_local_constants(
                            rule["local_constants"], index, container)

                    constants = {**rule_local_constants, **global_constants}
                    new_value = __eval_expr_recursive(rule_expr)
                    __set_value_by_path(
                        rule_target, container, new_value)

        print("Containers to start: ")
        print(containers)

        __run_hooks("preup-global", config)

        for index in range(len(containers)):
            container = containers[index]

            __run_hooks("preup", config, index, container)

            container_identifier = container["name"] if "name" in container else (
                "#" + str(index))

            print("Starting container {}...".format(container_identifier))
            if dry_run:
                print("Not running container because of dry-run mode is enabled")
            else:
                container_collection.append(
                    AgentContainer.run_container(client=client, no_detach=no_detach, **container))
            print("Container {} started.".format(container_identifier))
        return container_collection


class AgentContainerGroupCollection(collections.UserDict):
    def __init__(self):
        self.data = {}

    @staticmethod
    def create_container_groups_from_config(client: docker.DockerClient, config: dict, dry_run: bool = False, no_detach: bool = False):
        assert type(config) is dict
        container_group_collection = AgentContainerGroupCollection()

        if "containers" in config:
            assert "groups" not in config
            container_group_collection["DEFAULT"] = AgentContainerGroup.create_containers_from_config(
                client, config, {}, dry_run, no_detach)

        elif "groups" in config:
            assert "rules" not in config
            assert "hooks" not in config

            global_constants = config.get("constants", {})
            assert type(global_constants) is dict

            for name, group in config["groups"].items():
                print("Starting group {}".format(name))
                container_group_collection[name] = AgentContainerGroup.create_containers_from_config(
                    client, group, global_constants, dry_run, no_detach)

        else:
            raise Exception("No containers or groups specified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Runs containers. By default uses: config.yaml')
    parser.add_argument('-c', '--config', action='store',
                        help='run with a specific configuration file', default="config.yaml")
    parser.add_argument('-d', '--dry-run', action='store_true',
                        help='dry run without running containers')
    parser.add_argument('-n', '--no-detach', action='store_true',
                        help='do not detach from containers')

    # TODO: support configuration file overlay
    args = parser.parse_args()

    config = hiyapyco.load(
        args.config,
        method=hiyapyco.METHOD_SIMPLE,
        interpolate=True,
        usedefaultyamlloader=True,
    )

    docker_client = docker.from_env()

    container_collection = AgentContainerGroupCollection.create_container_groups_from_config(
        docker_client, config, args.dry_run, args.no_detach)
