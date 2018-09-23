import tornado
import json
import os

from log import log

from util import config
from threading import Lock

config.set_default("url_prefix", r"")


class PullRequestHandler(tornado.web.RequestHandler):
    def initialize(self, prs):
        self.prs = prs

    def get(self):
        def gen_pull_entry(pr, job, time, extra = None):
            res = extra or {}
            res.update({
                    "title" : pr.title,
                    "user" : pr.user,
                    "url" : pr.url,
                    "commit" : job.arg,
                    "since" : time,
                    })
            return res

        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.set_header("Access-Control-Allow-Credentials", "false")
        self.set_header("Access-Control-Allow-Origin", "*")

        building, queued, finished = self.prs.list()
        response = {}

        if building:
            _building = []
            for pr, job in building:
                _building.append(
                        gen_pull_entry(pr, job, job.time_started))
            response['building'] = _building

        if queued:
            _queued = []
            for pr, job in queued:
                _queued.append(
                        gen_pull_entry(pr, job, job.time_queued))

            response['queued'] = _queued

        if finished:
            _finished = []
            for pr, job in finished:
                job_path_rel = os.path.join(pr.base_full_name, str(pr.nr), job.arg)
                job_path_local = os.path.join(config.data_dir, job_path_rel)
                job_path_url = os.path.join(config.http_root, job_path_rel)

                extras = {
                    "output_url": os.path.join(job_path_url, "output.html"),
                    "result": job.result.name,
                    "runtime": (job.time_finished - job.time_started),
                  }
                status_jsonfile = os.path.join(job_path_local,
                                               "prstatus.json")
                if os.path.isfile(status_jsonfile):
                    with open(status_jsonfile) as f:
                        # Content is up for interpretation between backend
                        # and frontend scripting
                        extras["status"] = json.load(f)
                if "status" not in extras:
                    status_html_snipfile = os.path.join(
                            job_path_local, "prstatus.html.snip"
                        )

                    status_html = ""
                    if os.path.isfile(status_html_snipfile):
                        with open(status_html_snipfile, "r") as f:
                            status_html = f.read()
                    extras["status_html"] = status_html

                _finished.append(
                        gen_pull_entry(pr, job, job.time_finished, extras)
                    )

            response['finished'] = _finished

        self.write(json.dumps(response, sort_keys=False, indent=4))


class GithubWebhookHandler(tornado.web.RequestHandler):
    def initialize(self, handler):
        self.handler = handler

    def post(self):
        self.write("ok")
        hook_type = self.request.headers.get('X-Github-Event')

        handler = self.handler.get(hook_type)
        if handler:
            handler(self.request)
        else:
            log.warning("unhandled github event: %s", hook_type)


class StatusWebSocket(tornado.websocket.WebSocketHandler):
    lock = Lock()
    websockets = set()
    keeper = None

    # passive read only websocket, so anyone can read
    def check_origin(self, origin):
        return True

    @staticmethod
    def write_message_all(message, binary=False):
        s = StatusWebSocket
        with s.lock:
            for websocket in s.websockets:
                websocket.write_message(message, binary)

    @staticmethod
    def keep_alive():
        s = StatusWebSocket
        with s.lock:
            for websocket in s.websockets:
                websocket.ping("ping".encode("ascii"))

    def open(self):
        print("websocket opened")
        with self.lock:
            if not self.websockets:
                s = StatusWebSocket
                s.keeper = tornado.ioloop.PeriodicCallback(s.keep_alive, 30*1000)
                s.keeper.start()
            self.websockets.add(self)

    def on_message(self, message):
        pass

    def on_close(self):
        with self.lock:
            self.websockets.discard(self)
            if not self.websockets:
                self.keeper.stop()


class ControlHandler(tornado.web.RequestHandler):
    def post(self):
#       data = json.loads(self.request.body)
        s = StatusWebSocket
        s.write_message_all(self.request.body)


class GithubWebhook(tornado.web.Application):

    def __init__(self, prs, github_handlers):
        self.secret = "__secret"
        handlers = [
            (config.url_prefix + r"/api/pull_requests", PullRequestHandler, dict(prs=prs)),
            (config.url_prefix + r"/github", GithubWebhookHandler, dict(handler=github_handlers)),
            (config.url_prefix + r"/status", StatusWebSocket),
            (config.url_prefix + r"/control", ControlHandler),
        ]

        self.websocket_lock = Lock()
        self.status_websockets = set()
        settings = {'debug': True}
        super(GithubWebhook, self).__init__(handlers, **settings)

