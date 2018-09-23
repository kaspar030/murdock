#!/usr/bin/env python3

import os
import subprocess
import json
import signal
import shutil
import tornado.ioloop
import traceback

from log import log

from agithub.GitHub import GitHub

import threading
from queue import Queue, Empty

from jobs import Job, JobResult, JobState
from github_webhook import GithubWebhook

from util import config
from threading import Lock

known_actions = { "labeled", "unlabeled", "synchronize", "created", "assigned",
        "closed", "edited", "unassigned", "opened", "status", "reopened", "review_requested" }

def nicetime(time):
    secs = round(time)
    minutes = secs/60
    hrs = minutes/60
    days = int(hrs/24)
    secs = int(secs % 60)
    minutes = int(minutes % 60)
    hrs = int(hrs % 24)
    res = ""
    if days:
        res += "%sd:" % days
    if hrs:
        res += "%sh:" % hrs
    if minutes:
        res += "%sm:" % minutes
    res += "%ss" % secs
    return res

class ShellWorker(threading.Thread):
    _lock = Lock()
    num_workers = 0

    def __init__(self, queue):
        threading.Thread.__init__(self, daemon=True)
        self.process = None
        self.queue = queue
        self.canceled = False
        self.job = None
        with ShellWorker._lock:
            ShellWorker.num_workers += 1
            self.num = ShellWorker.num_workers
        self.start()

    def run(s):
        log.info("ShellWorker %s: started.", s.num)
        while True:
            try:
                try:
                    s.job = None
                    s.process = None
                    s.canceled = False
                    job = s.queue.get()
                    s.job = job
                except Empty:
                    return
                if job.state == JobState.finished:
                    log.info("ShellWorker %s: skipping finished job %s", s.num, job.name)
                    s.queue.task_done()
                    continue
                else:
                    log.info("ShellWorker %s: building job %s", s.num, job.name)

                s.job = job
                job.worker = s
                s.job.set_state(JobState.running)
                s.job.env["CI_BUILD_ID"] = str(s.job.time_started)

                build_dir = os.path.join(s.job.data_dir(), "build")
                try:
                    os.makedirs(build_dir)
                except FileExistsError:
                    shutil.rmtree(build_dir)
                    os.makedirs(build_dir)

                _env = os.environ.copy()
                _env.update(s.job.env)

                output_file = open(os.path.join(s.job.data_dir(), "output.txt"), mode='wb')
                s.process = p = subprocess.Popen([ s.job.cmd, "build" ],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             cwd=s.job.data_dir(), env=_env, start_new_session=True)
                s.job.worker = s
                try:
                    for line in p.stdout:
                        output_file.write(line)
                except Exception as e:
                    log.info(e)
                finally:
                    output_file.close()

                log.info("ShellWorker %s: Job %s finished. result: %s", s.num, s.job.name, s.job.result)

                if s.process:
                    s.process.wait()

                try:
                    subprocess.check_call([s.job.cmd, "post_build"], cwd=s.job.data_dir(), env=_env)
                except subprocess.CalledProcessError:
                    log.warning("Job %s: post build script failed.", s.job.name)
                    pass

                if s.canceled:
                    s.job.set_state(JobState.finished, JobResult.canceled)
                else:
                    ret = s.process.returncode
                    s.process = None
                    if ret == 0:
                        s.job.set_state(JobState.finished, JobResult.passed)
                    else:
                        s.job.set_state(JobState.finished, JobResult.errored)

                try:
                    shutil.rmtree(build_dir)
                except FileNotFoundError:
                    pass

                s.queue.task_done()

            except Exception as e:
               log.warning("ShellWorker %s: uncaught exception: %s", s.num, e)
               traceback.print_exc()

    def cancel(s, job):
        if s.process is not None and s.job==job:
            threading.Thread(target=ShellWorker.graceful_kill, args=(s.process,)).start()
            s.process=None
            s.canceled=True

    def graceful_kill(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(config.sigterm_timeout)
        except subprocess.TimeoutExpired:
            log.warning("ShellWorker: killing process")
            process.kill()
            process.wait()

class PullRequest(object):
    _map = {}

    def __init__(s, data):
        s.data = data
        s._map[data["_links"]["html"]["href"]] = s
        s.current_job = None
        s.jobs = []
        s.labels = set()
        s.old_head = None

    def get(data, create=True):
        if "pull_request" in data:
            data = data["pull_request"]

        pull_url = data["_links"]["html"]["href"]
        pr = PullRequest._map.get(pull_url)
        if pr:
            pr.data = data
            log.info("PR %s updated", pr.url)
        else:
            if not create:
                return None

            pr = PullRequest(data)
            log.info("PR %s new to Murdock (state=%s, mergeable=%s, merge_commit_sha=%s)", pr.url, pr.state, pr.mergeable, pr.merge_commit)
            pr.update_labels()
        return pr

    def close(data):
        pr = PullRequest.get(data, False)
        if pr:
            pr.cancel_job()
            log.info("PR %s: closed.", pr.url)
        else:
            log.warning("PR %s unknown, but tried to close!", data["_links"]["html"]["href"])

    def update(s):
        if s.head != s.old_head:
            s.old_head = s.head
            log.info("PR %s has new head: %s", s.url, s.head)
            if (s.state=="open") \
                and config.ci_ready_label in s.labels \
                and s.mergeable in { True, None }:
                s.start_job()
        return s

    def cancel_job(s):
        if s.current_job and s.current_job.state!=JobState.finished:
            log.info("PR %s: canceling build of commit %s", s.url, s.current_job.arg)
            s.current_job.cancel()
            s.current_job = None
        return s

    def start_job(s):
        s.cancel_job()

        log.info("PR %s: queueing build of commit %s", s.url, s.head)

        env = { "CI_PULL_COMMIT" : s.head,
                "CI_PULL_REPO" : s.repo,
                "CI_PULL_BRANCH" : s.branch,
                "CI_PULL_NR" : str(s.nr),
                "CI_PULL_URL" : s.url,
                "CI_PULL_TITLE" : s.title,
                "CI_PULL_USER" : s.user,
                "CI_BASE_REPO" : s.base_repo,
                "CI_BASE_BRANCH" : s.base_branch,
                "CI_BASE_COMMIT" : s.base_commit,
                "CI_SCRIPTS_DIR" : config.scripts_dir,
                "CI_PULL_LABELS" : ";".join(sorted(list(s.labels))),
                "CI_BUILD_HTTP_ROOT" : os.path.join(config.http_root,
                                       s.base_full_name, str(s.nr), s.head),
                }

        if s.mergeable:
            env["CI_MERGE_COMMIT"] = s.merge_commit

        for key, value in env.items():
            if not value:
                log.warning("PR %s: env %s has NoneType!", s.url, key)
                return s

        s.current_job = Job(s.get_job_path(s.head), os.path.join(config.scripts_dir, "build.sh"), env, s.job_hook, s.head)
        s.jobs.append(s.current_job)
        queue.put(s.current_job)

        s.current_job.set_state(JobState.queued)
        return s

    def get_job_path(s, commit):
        return os.path.join(config.data_dir, s.base_full_name, str(s.nr), commit)

    def update_labels(s):
        code, result = github.repos[s.base_full_name].issues[s.nr].labels.get()
        if code == 200:
            s.labels = set()
            for label in result:
                log.info("PR %s set label: %s", s.url, label["name"])
                s.labels.add(label["name"])
        return s

    def add_label(s, label):
        log.info("PR %s added label: %s", s.url, label)
        if label in s.labels:
            log.warning("PR %s label already present.", s.url)
            return
        s.labels.add(label)
        if label == config.ci_ready_label:
            s.start_job()
        return s

    def remove_label(s, label):
        log.info("PR %s removed label: %s", s.url, label)
        s.labels.discard(label)
        if label == config.ci_ready_label:
            s.cancel_job()
        return s

    def __getattr__(s, field):
        if field == "url":
            return s.data["_links"]["html"]["href"]
        elif field == "base_repo":
            return s.data["base"]["repo"]["clone_url"]
        elif field == "base_branch":
            return s.data["base"]["ref"]
        elif field == "base_commit":
            return s.data["base"]["sha"]
        elif field == "base_full_name":
            return s.data["base"]["repo"]["full_name"]
        elif field == "nr":
            return s.data["number"]
        elif field == "branch":
            return s.data["head"]["ref"]
        elif field == "repo":
            return s.data["head"]["repo"]["clone_url"]
        elif field == "head":
            return s.data["head"]["sha"]
        elif field == "user":
            return s.data["head"]["user"]["login"]
        elif field == "title":
            return s.data["title"]
        elif field == "state":
            return s.data["state"]
        elif field == "merge_commit":
            return s.data["merge_commit_sha"]
        elif field == "mergeable":
            return s.data["mergeable"]
        else:
            raise AttributeError

    def job_hook(s, arg, job):
        target_url = None
        runtime = None
        if job.state == JobState.created:
            state = "pending"
            description = "The build has been queued."
        elif job.state == JobState.running:
            state = "pending"
            description = "The build has been started."
        elif job.state == JobState.finished:
            runtime = job.time_finished - job.time_started
            target_url = os.path.join(config.http_root, s.base_full_name, str(s.nr), arg, "output.html")
            if job.result == JobResult.passed:
                if not s.labels & config.fail_labels:
                    state = "success"
                    description = "The build succeeded. runtime: %s" % nicetime(runtime)
                else:
                    state = "error"
                    description = "The build only failed the label check. runtime: %s" % nicetime(runtime)
                    job.result = JobResult.errored
            elif job.result == JobResult.errored:
                state = "error"
                description = "The build failed. runtime: %s" % nicetime(runtime)
            else:
                state = "failure"
                target_url = None
                if job.result == JobResult.canceled:
                    description = "The build has been canceled."
                else:
                    description = "The build has failed for an unknown reason."
        else:
            return

        status = {
                "state": state,
                "description": description,
                }

        if target_url:
            status["target_url"] = target_url
        else:
            status["target_url"] = config.http_root

        s.set_status(arg, **status)

        if runtime:
            log.info("PR %s runtime: %s", s.url, nicetime(runtime))

        log.info("PR %s notifying webhooks", s.url)
        GithubWebhook.StatusWebSocket.write_message_all('{ "cmd" :"reload_prs" }')

    def set_status(s, commit, **kwargs):
        status = {
                "state" : "failure",
                "description" : "unknown reason",
                "context" : config.context
                }

        status.update(kwargs)

        if not config.set_status:
            log.info("PR %s not setting github status: %s \"%s\"", s.url, status["state"], status["description"])
            return

        log.info("PR %s setting github status: %s \"%s\"", s.url, status["state"], status["description"])
        github.repos[s.base_full_name].statuses[commit].post(body=status)

    def cancel_all():
        log.info("canceling jobs...")
        for url, pr in PullRequest._map.items():
            if pr.current_job:
                pr.current_job.cancel()

    def load(repo):
        code, result = github.repos[repo].pulls.get()
        if code==200:
            for data in result:
                if data["state"] == "open":
                    pr = PullRequest.get(data)
                    if pr.current_job:
                        continue
                    if not config.ci_ready_label in pr.labels:
                        continue
                    state = pr.get_state()
                    if state == "canceled" or state == "pending":
                        pr.start_job()

    def get_state(s):
        code, result = github.repos[s.base_full_name].statuses[s.head].get()
        if code==200:
            for data in result:
                if data["context"] == config.context:
                    if data["description"] == "The build has been canceled.":
                        return "canceled"
                    else:
                        return data["state"]
        else:
            log.warning("PullRequest: couldn't get statuses: code %s", code)

        return "unknown"

    def list():
        building = []
        queued = []
        finished = []
        for name, pr in PullRequest._map.items():
            job = pr.current_job
            if job:
                if job.state==JobState.finished:
                    finished.append((pr, job))
                elif job.state==JobState.running:
                    building.append((pr, job))
                else:
                    queued.append((pr, job))

        building = sorted(building, key=lambda x: x[1].time_started)
        queued = sorted(queued, key=lambda x: x[1].time_queued)
        finished = sorted(finished, key=lambda x: x[1].time_finished)

        return (building, queued, finished)

handle_pull_request_lock = Lock()

def handle_pull_request(request):
    with handle_pull_request_lock:
        data = json.loads(request.body.decode("utf-8"))
        pr_data = data["pull_request"]
        repo = pr_data["base"]["repo"]["full_name"]
        if not repo in config.repos:
            log.warning("ignoring PR for repo %s", repo)
            return

        #print(json.dumps(data, sort_keys=False, indent=4))
        action = data["action"]

        pr_url = pr_data["_links"]["html"]["href"]
        log.info("PR %s hook action %s", pr_url, action)

        if not action in known_actions:
            log.warning("PR %s unknown action %s", pr_url, action)
            log.debug(json.dumps(data, sort_keys=False, indent=4))

        if action in { "closed" }:
            PullRequest.close(pr_data)
            return

        pr = PullRequest.get(pr_data).update()
        if action == "unlabeled":
            pr.remove_label(data["label"]["name"])
        elif action == "labeled":
            pr.add_label(data["label"]["name"])
        elif action in { "created", "opened", "reopened" } and not config.ci_ready_label in pr.labels:
            status = {
                    "description": "\"%s\" label not set" % config.ci_ready_label,
                    "target_url" : config.http_root,
                    }

            pr.set_status(pr_data["head"]["sha"], **status)

def handle_push(request):
    data = json.loads(request.body.decode("utf-8"))

    log.info(json.dumps(data, sort_keys=False, indent=4))

github_handlers = {
        "pull_request" : handle_pull_request,
#        "push" : handle_push,
        }

github = GitHub(config.github_username, config.github_password)
queue = Queue()
ShellWorker(queue)

def shutdown():
    log.info("murdock: shutting down.")
    tornado.ioloop.IOLoop.instance().stop()

def sig_handler(sig, frame):
    log.warning('Caught signal: %s', sig)
    shutdown()

def startup_load_pull_requests():
    log.info("Loading pull requests...")
    for repo in config.repos:
        PullRequest.load(repo)
    log.info("All pull request loaded.")

def main():
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)
    log.info("murdock initialized.")

#    threading.Thread(target=startup_load_pull_requests, daemon=True).start()

    webhook = GithubWebhook(PullRequest, github_handlers)
    webhook.listen(config.port)
    log.info("Start tornado IOLoop")
    tornado.ioloop.IOLoop.instance().start()

    # tornado loop ended

    PullRequest.cancel_all()
    log.info("murdock shut down.")

if __name__ == "__main__":
    main()

