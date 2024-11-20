# ⚠️  archived ⚠️

This has been superseded by [murdock-ng](https://github.com/murdock-ng/murdock).

# murdock
A simple CI (continuous integration) server written in Python.
Developed for RIOT (riot-os.org).

# Requirements

- python3
- agithub python module (tested with v2.1 from pip)
- pytoml python module

# Installation

- make sure requirements are in place
- clone into directory of choice
- create murdock.toml (adapt murdock.toml.example to your needs)
- launch murdock as "murdock <path-to-your-murdock.toml>
- set up a frontend https server proxying to murdock
- point github webhooks to https://host/<murdock-prefix>/github
- create "build.sh" in script_dir that accepts "build" and "post_build" as
  first parameter, building your project
