"""Local scheduler driver. Never turns a prompt into an executable command."""
import json
import logging
import threading
import urllib.request

log = logging.getLogger(__name__)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Scheduler refuses credential-bearing redirects")


class TriggerSender:
    def __init__(self, port, token):
        if not 1 <= port <= 65535:
            raise ValueError("Invalid local service port")
        self.base = "http://127.0.0.1:" + str(port)
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, custom_id, outbox_id, payload):
        # Both IDs originate from validated/persisted service data, never from
        # a user-supplied destination URL. Do not expose a generic HTTP proxy.
        req = urllib.request.Request(self.base + "/api/v1/triggers/" + custom_id + "/invoke",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token,
                     "X-Schedule-Event-ID": outbox_id})
        with self.opener.open(req, timeout=5) as response:
            return json.loads(response.read(65536))


def start_scheduler(store, port, token):
    stop = threading.Event()
    sender = TriggerSender(port, token)

    def run():
        while not stop.is_set():
            try:
                store.materialize()
                store.deliver_pending(sender)
            except Exception as error:
                # No raw HTTP request, auth header, user payload or exception body.
                log.warning("Scheduler iteration failed: %s", type(error).__name__)
            stop.wait(1)

    thread = threading.Thread(target=run, name="custom-task-scheduler", daemon=True)
    thread.start()
    return stop, thread
