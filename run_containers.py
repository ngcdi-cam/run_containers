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


class AgentContainerCollection(collections.UserList):
    class UndefinedConstantError(Exception):
        pass

    class UnknownLocalConstantTypeError(Exception):
        pass

    def __init__(self):
        self.data = []

    @staticmethod
    def create_containers_from_config(client: docker.DockerClient, config: dict, dry_run: bool = False, no_detach: bool = False):
        global_constants = config.get("constants", {})
        global_constants["base_dir"] = os.getcwd()
        constants = {}

        # substitute constants if needed
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
                        raise AgentContainerCollection.UndefinedConstantError(e)
                else:
                    return token

            ret = ""

            for token in expr_splitted:
                if len(token) >= 2 and token[0] == '$':
                    try:
                        ret += str(constants[token[1:]])
                    except KeyError as e:
                        raise AgentContainerCollection.UndefinedConstantError(e)
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
            path_tokenized = list(map(__eval_expr, path.split(sep)))
            p = data
            for key in path_tokenized[:-1]:
                if key not in p:
                    p[key] = {}
                p = p[key]
            p[path_tokenized[-1]] = new_value

        available_constant_evaluators = {
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
                if vtype not in available_constant_evaluators:
                    raise AgentContainerCollection.UnknownLocalConstantTypeError()

                evaluator = available_constant_evaluators.get(vtype)
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

        container_collection = AgentContainerCollection()

        constants = global_constants
        assert "containers" in config
        containers: list = __eval_expr_recursive(config["containers"])

        # process rules
        if "rules" in config:
            rules = config["rules"]
            for rule in rules:
                assert "target" in rule
                assert "value" in rule

                rule_target = rule["target"]
                rule_expr = rule["value"]

                for index in range(len(containers)):
                    container = containers[index]

                    rule_local_constants = {}
                    if "local_constants" in rule:
                        rule_local_constants = __eval_local_constants(
                            rule["local_constants"], index, container)

                    constants = {**rule_local_constants, **global_constants}
                    new_value = __eval_expr(rule_expr)
                    __set_value_by_path(
                        rule_target, container, new_value)

        print("Containers to start: ")
        print(containers)

        if "hooks" in config and "preup-global" in config["hooks"]:
            print("Running preup-global hook...")
            preup_global_hooks = config["hooks"]["preup-global"]

            constants = global_constants
            environment = __eval_environment(preup_global_hooks.get("environment", {}))

            print("Environment is {}".format(environment))

            assert "commands" in preup_global_hooks
            for command in preup_global_hooks["commands"]:
                print("Running command: {}".format(command))
                subprocess.run(command, shell=True, env=environment)

        for index in range(len(containers)):
            container = containers[index]
            if "hooks" in config and "preup" in config["hooks"]:
                print("Running preup hook...")
                preup_hooks = config["hooks"]["preup"]
                local_constants = __eval_local_constants(
                    preup_hooks.get("local_constants", []), index, container)
                constants={**local_constants, **global_constants}
                environment = __eval_environment(
                    preup_hooks.get("environment", []))
                print("Environment is {}".format(environment))

                assert "commands" in preup_hooks
                for command in preup_hooks["commands"]:
                    print("Running command: {}".format(command))
                    subprocess.run(command, shell=True, env=environment)

            print("Starting container {}...".format(
                container["name"] if "name" in container else "#" + index))
            if dry_run:
                print("Not running container because of dry-run mode is enabled")
            else:
                container_collection.append(
                    AgentContainer.run_container(client=client, no_detach=no_detach, **container))
            print("Container {} started.".format(container["name"]))
        return container_collection


if __name__ == "__main__":
    parser=argparse.ArgumentParser(
        description='Runs containers. By default uses: config.yaml')
    parser.add_argument('-c', '--config', action='store',
                        help='run with a specific configuration file', default="config.yaml")
    parser.add_argument('-d', '--dry-run', action='store_true', help='dry run without running containers')
    parser.add_argument('-n', '--no-detach', action='store_true', help='do not detach from containers')

    # TODO: support configuration file overlay
    args=parser.parse_args()

    config=hiyapyco.load(
        args.config,
        method=hiyapyco.METHOD_SIMPLE,
        interpolate=True,
        usedefaultyamlloader=True,
    )

    docker_client=docker.from_env()

    container_collection=AgentContainerCollection.create_containers_from_config(
        docker_client, config, args.dry_run, args.no_detach)
