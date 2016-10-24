# Murdock
A simple CI (continuous integration) server written in Python.
Developed for RIOT (riot-os.org).

# Setup

- checkout into a directory of your choice
- copy config.py.example to config.py, set github login credentials and put
  your github project name in "repos"
- set up an http server to forward SSL requests to port 3000 (see nginx example config)
- set up a github hook to point to the outside SSL url
- in your Murdock folder, create a file scripts/build_local.sh that defines a shell function "build()"
  That function will be called on every PR to do the actual building/testing.
  See https://github.com/RIOT/murdock-scripts for an example launching a docker container.
- start murdock.py using a method of your choice
