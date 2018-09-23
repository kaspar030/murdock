#!/usr/bin/env python
from enum import Enum
from threading import Lock
import time

from log import log

class JobState(Enum):
    created = 0
    queued = 1
    running = 2
    finished = 3

class JobResult(Enum):
    passed = 0
    canceled = 1
    errored = 2
    timeout = 3
    unknown = 4

class Job(object):
    id = 0
    def __init__(self, name, cmd, env=None, hook=None, arg=None):
        self.lock = Lock()
        self.id = Job.id
        Job.id += 1

        self.name = name
        self.cmd = cmd
        self.env = env
        self.worker = None

        self.hook = hook
        self.arg = arg

        self.result = JobResult.unknown

        self.time_created = -1
        self.time_queued = -1
        self.time_started = -1
        self.time_finished = -1

        self.set_state(JobState.created)

    def data_dir(self):
        return self.name #,os.path.join(config.data_dir, s.name + "." + str(s.id))

    def set_state(self, state, result=JobResult.unknown):
        with self.lock:
            self.state = state
            self.result = result
            if self.state == JobState.created:
                self.time_created = time.time()
            elif self.state == JobState.queued:
                self.time_queued = time.time()
            elif self.state == JobState.running:
                self.time_started = time.time()
            elif self.state == JobState.finished:
                self.time_finished = time.time()

            log.info("Job %s: new state: %s %s %s %s %s %s " ,
                     self.name, self.state, self.result, self.time_created,
                     self.time_queued, self.time_started, self.time_finished)

        if self.hook:
            self.hook(self.arg, self)

    def stopped(self, result):
        with self.lock:
            self.state = result

    def cancel(self):
        if self.worker:
            self.worker.cancel(self)
        else:
            self.set_state(JobState.finished, JobResult.canceled)
