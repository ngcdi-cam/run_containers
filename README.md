# run_containers.py

A Python script that runs a list of Docker containers based on configuration files.

## Features

* Support user-defined constants
* Support rules for dynamic property generation
* Support hooks [ preup | preup-global ]

## Prerequisite

```
$ pip3 install -r requirements.txt
```

## Install

```
$ sudo install -m 755 run_containers.py /usr/local/bin
```

## Getting Started

```
$ run_containers.py -c configs/examples/1-basic.yaml
```

## Writing Your Own Configuration files

See `configs/examples` for a walkthrough.

## Notes

In `constants` section of the config
prepend `^` to the constant name to disable expression evaluation (for example in case of using a common rule as the constant)

## Contributors

* Peter Zhang
* Marco Perez Hernandez
